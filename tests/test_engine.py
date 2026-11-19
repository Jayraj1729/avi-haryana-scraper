'''Offline regression tests for coverage, checkpoints, sessions, and exports.'''

# Standard Library Imports
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

# Third Party Imports
from openpyxl import load_workbook
from shapely.geometry import box
from shapely.ops import unary_union

from scraper import AuthenticationError, isLoginHtml, parsePropertyDetailHtml, PortalHttpError, PortalResponseError
from ulb_scraper.engine import Engine
from ulb_scraper.exporter import exportJob
from ulb_scraper.geometry import containsProperty, readBoundary, seedBounds
from ulb_scraper.portal import PortalClient, propertyPhotoUrls
from ulb_scraper.store import Store


BOUNDARY = 'POLYGON ((76 30, 76.001 30, 76.001 30.001, 76 30.001, 76 30))'
MUNICIPALITY = {'id': '60', 'name': 'PANCHKULA'}
COLONY = {'id': '18949', 'name': 'Sector 6'}


class FakeClient:
    def __init__(self, **kwargs):
        self.rows = [{'pkPropertyId': 7, 'Lat': 30.0005, 'Long': 76.0005,
                      'OwnerName': 'Test Owner', 'Token': 'fixture-token'}]
        self.expired = False
        self.failMap = False
        self.calls = []

    def checkSession(self):
        if self.expired:
            raise AuthenticationError('expired')

    def requestJson(self, method, url, **kwargs):
        self.calls.append(url)
        if 'GetULBList' in url:
            return [{'Value': '60', 'Text': 'PANCHKULA'}, {'Value': '61', 'Text': 'OTHER'}]
        if 'GetULBColony' in url:
            return [{'Value': '18949', 'Text': 'Sector 6'}] if '60' in url else [{'Value': '2', 'Text': 'Other colony'}]
        if 'GetColonyBoundry' in url:
            return {'Wkt': BOUNDARY}
        if self.failMap:
            raise PortalHttpError(500)
        if self.expired:
            raise AuthenticationError('expired')
        return self.rows

    def fetchDetails(self, raw):
        if self.expired:
            raise AuthenticationError('expired')
        return {'Detail': {'FetchStatus': 'ok', 'OwnerNames': 'Test Owner',
                           'MobileNumberLinkedWithThePID': '11XXXXX111, 22XXXXX222'}}

    def generateOtp(self, phone):
        self.phone = phone

    def verifyOtp(self, otp):
        self.expired = False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.store.setSetting('requestDelayMs', 0)
        self.events = []
        self.engine = Engine(self.store, self.home, lambda *event: self.events.append(event), FakeClient)
        self.jobId = self.store.addJobs([(MUNICIPALITY, COLONY)], self.home / 'exports')[0]

    def tearDown(self):
        self.temporary.cleanup()

    def initialize(self):
        self.store.initializeTiles(self.jobId, BOUNDARY, [(76, 30, 76.001, 30.001)])

    def testPolygonCoverageIncludesEdgesAndRejectsOutside(self):
        geometry = readBoundary(BOUNDARY)
        coverage = unary_union([box(*bounds) for bounds in seedBounds(geometry)])
        self.assertLess(geometry.difference(coverage).area, 1e-14)
        self.assertTrue(containsProperty(geometry, {'Long': 76, 'Lat': 30}))
        self.assertFalse(containsProperty(geometry, {'Long': 76.01, 'Lat': 30}))
        self.assertFalse(containsProperty(geometry, {'Long': 'NaN', 'Lat': 30}))

    def testMultipolygonAndInteriorHoles(self):
        geometry = readBoundary('MULTIPOLYGON (((0 0,2 0,2 2,0 2,0 0),'
                                '(0.5 0.5,1.5 0.5,1.5 1.5,0.5 1.5,0.5 0.5)),'
                                '((3 0,4 0,4 1,3 1,3 0)))')
        self.assertFalse(containsProperty(geometry, {'Long': 1, 'Lat': 1}))
        self.assertTrue(containsProperty(geometry, {'Long': 3.5, 'Lat': 0.5}))

    def testFailedTileDoesNotAdvanceCheckpoint(self):
        self.initialize()
        self.engine.client.failMap = True
        with self.assertRaises(PortalHttpError):
            self.engine.advance(self.store.job(self.jobId))
        self.assertIsNotNone(self.store.pendingTile(self.jobId))
        self.assertFalse(self.store.job(self.jobId)['uniqueCount'])

    def testTransactionRollsBackPropertiesAndTileTogether(self):
        self.initialize()
        tile = self.store.pendingTile(self.jobId)
        with self.assertRaises(ValueError):
            self.store.saveTile(self.jobId, tile, [self.engine.client.rows[0], {}], propertyPhotoUrls)
        self.assertIsNotNone(self.store.pendingTile(self.jobId))
        with self.store.connect() as connection:
            self.assertFalse(connection.execute('SELECT COUNT(*) FROM properties').fetchone()[0])

    def testDuplicateRowsAndOutOfPolygonRows(self):
        self.initialize()
        row = self.engine.client.rows[0]
        self.engine.client.rows = [row, row, {'pkPropertyId': 8, 'Long': 77, 'Lat': 31}]
        self.engine.advance(self.store.job(self.jobId))
        self.assertEqual(self.store.job(self.jobId)['uniqueCount'], 1)
        self.assertIsNone(self.store.pendingTile(self.jobId))

    def testDenseTileSplitsAndRetainsCoverageWeights(self):
        self.initialize()
        self.engine.client.rows = [dict(self.engine.client.rows[0], pkPropertyId=index) for index in range(1, 501)]
        self.engine.advance(self.store.job(self.jobId))
        job = self.store.jobs()[0]
        self.assertEqual(job['tileTotal'], 4)
        self.assertFalse(job['coverage'])
        with self.store.connect() as connection:
            weight = connection.execute("SELECT SUM(weight) FROM tiles WHERE state='pending'").fetchone()[0]
        self.assertAlmostEqual(weight, 1)

    def testRepeatedPaginationDoesNotSilentlyFinish(self):
        self.initialize()
        with self.store.connect() as connection:
            connection.execute('UPDATE tiles SET depth=8')
        self.engine.client.rows = [dict(self.engine.client.rows[0], pkPropertyId=index) for index in range(1, 501)]
        self.engine.advance(self.store.job(self.jobId))
        self.assertEqual(self.store.pendingTile(self.jobId)['page'], 2)
        with self.assertRaisesRegex(PortalResponseError, 'repeated'):
            self.engine.scanTile(self.store.job(self.jobId), self.store.pendingTile(self.jobId), readBoundary(BOUNDARY))
        self.assertEqual(self.store.pendingTile(self.jobId)['page'], 2)

    def testPauseAndRestartRetainDesiredStateAndPendingWork(self):
        self.initialize()
        self.store.control([self.jobId], 'pause')
        reloaded = Store(self.home / 'state.sqlite3')
        self.assertFalse(reloaded.job(self.jobId)['desired'])
        self.assertIsNotNone(reloaded.pendingTile(self.jobId))
        reloaded.control([self.jobId], 'start')
        self.assertTrue(reloaded.job(self.jobId)['desired'])

    def testSessionExpirySuspendsDesiredJobsOnly(self):
        otherId = self.store.addJobs([(MUNICIPALITY, {'id': '2', 'name': 'Sector 7'})], self.home / 'exports')[0]
        self.store.control([otherId], 'pause')
        self.engine.expireSession()
        self.assertEqual(self.store.job(self.jobId)['status'], 'waiting_login')
        self.assertTrue(self.store.job(self.jobId)['desired'])
        self.assertEqual(self.store.job(otherId)['status'], 'paused')
        self.assertFalse(self.store.job(otherId)['desired'])

    def testLoginVerificationResumesQueueWithoutRestartingPausedJobs(self):
        self.store.waitForLogin()
        self.engine.handleCommand('generateOtp', {'phone': '9999999999'})
        self.engine.handleCommand('verifyOtp', {'otp': '123456'})
        self.assertTrue(self.engine.authenticated)
        self.assertTrue(self.store.job(self.jobId)['desired'])
        self.assertIn(('otpVerified', None), self.events)

    def testCatalogRetainsRemovedOptions(self):
        self.store.updateCatalog('colony', '60', [{'Value': '1', 'Text': 'Old'}, {'Value': '2', 'Text': 'Current'}])
        self.store.updateCatalog('colony', '60', [{'Value': '2', 'Text': 'Current'}])
        entries = {item['id']: item for item in self.store.catalog('colony', '60')}
        self.assertFalse(entries['1']['active'])
        self.assertTrue(entries['2']['active'])

    def testAllColoniesOnlyReplaceSelectedMunicipality(self):
        otherId = self.store.addJobs([({'id': '61', 'name': 'OTHER'}, {'id': '2', 'name': 'Other colony'})], self.home / 'exports')[0]
        self.engine.addSelection('60', 'ALL', str(self.home / 'exports'))
        self.assertIsNone(self.store.job(self.jobId))
        self.assertIsNotNone(self.store.job(otherId))
        self.assertEqual(len(self.store.jobs()), 2)

    def testAllMunicipalitiesExpandCorrectDependentColonies(self):
        self.engine.addSelection('ALL', 'ALL', str(self.home / 'exports'))
        pairs = {(job['municipalityId'], job['colonyId']) for job in self.store.jobs()}
        self.assertEqual(pairs, {('60', '18949'), ('61', '2')})

    def testSavedMultiMunicipalitySessionRefreshesEachSelectedCatalog(self):
        self.store.setSetting('selectedMunicipalities', ['60', '61'])
        self.engine.restoreSession()
        for municipalityId in ('60', '61'):
            self.assertTrue(any(f'fkULBId={municipalityId}' in url for url in self.engine.client.calls))
        self.assertTrue(self.engine.authenticated)

    def testBatchValidationFailurePreservesAllExistingJobs(self):
        before = self.store.jobs()
        with self.assertRaises(ValueError):
            self.engine.addSelections({'60': ['ALL'], '61': ['missing']}, self.home / 'exports')
        self.assertEqual(self.store.jobs(), before)

    def testBatchAllReplacesOnlyCheckedMunicipalitiesAndArchivesFolders(self):
        otherId = self.store.addJobs([({'id': '61', 'name': 'OTHER'}, {'id': '2', 'name': 'Other colony'})], self.home / 'exports')[0]
        folder = Path(self.store.job(self.jobId)['folder'])
        folder.mkdir(parents=True)
        (folder / 'keep.txt').write_text('old run')
        self.engine.addSelections({'60': ['ALL']}, self.home / 'exports')
        self.assertIsNotNone(self.store.job(otherId))
        self.assertIsNone(self.store.job(self.jobId))
        self.assertEqual(len(self.store.jobs()), 2)
        self.assertEqual(len(list((self.home / 'exports' / '_archive').rglob('keep.txt'))), 1)

    def testResetArchivesExistingOutput(self):
        folder = Path(self.store.job(self.jobId)['folder'])
        folder.mkdir(parents=True)
        (folder / 'properties.xlsx').write_bytes(b'old export')
        self.engine.addSelection('60', 'ALL', str(self.home / 'exports'))
        archives = list((self.home / 'exports' / '_archive').rglob('properties.xlsx'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b'old export')

    def testFullEngineCompletionAndExport(self):
        self.initialize()
        self.engine.authenticated = True
        self.engine.lastSessionCheck = time.monotonic()
        self.engine.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.store.job(self.jobId)['status'] != 'complete':
            time.sleep(0.02)
        self.engine.stop()
        self.engine.join(5)
        self.assertFalse(self.engine.is_alive())
        job = self.store.job(self.jobId)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['uniqueCount'], 1)
        self.assertEqual(job['detailCount'], 1)
        self.assertTrue((Path(job['folder']) / 'properties.xlsx').exists())

    def testPauseExportsCommittedPartialData(self):
        self.initialize()
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.store.control([self.jobId], 'pause')
        self.engine.flushExports()
        job = self.store.job(self.jobId)
        self.assertEqual(job['exportVersion'], job['dataVersion'])
        self.assertTrue((Path(job['folder']) / 'properties.xlsx').exists())

    def testRelativeHyperlinksAndFormulaTextSurviveMove(self):
        self.initialize()
        row = dict(self.engine.client.rows[0], OwnerName='=1+1', ImageViewLink='https://example.test/property.jpg')
        self.store.saveTile(self.jobId, self.store.pendingTile(self.jobId), [row], propertyPhotoUrls)
        self.store.savePhoto(self.jobId, '7', row['ImageViewLink'], 'photos/7_test.jpg')
        self.store.saveDetail(self.jobId, '7', {'Detail': {'FetchStatus': 'ok'}}, [])
        self.store.finishProperty(self.jobId, '7')
        path = exportJob(self.store, self.store.job(self.jobId))
        workbook = load_workbook(path)
        headers = [cell.value for cell in workbook.active[1]]
        owner = workbook.active.cell(2, headers.index('Owner 1: Name') + 1)
        photo = workbook.active.cell(2, headers.index('Photo 1') + 1)
        self.assertTrue(owner.value.startswith('=1+1'))
        self.assertEqual(owner.data_type, 's')
        self.assertEqual(photo.hyperlink.target, 'photos/7_test.jpg')
        self.assertEqual(headers[0], 'PID')
        workbook.close()

    def testFailedExportRetainsPreviousWorkbookAndDirtyState(self):
        self.initialize()
        self.engine.client.rows.append(dict(self.engine.client.rows[0], pkPropertyId=8))
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        job = self.store.job(self.jobId)
        exportJob(self.store, job)
        existing = (Path(job['folder']) / 'properties.xlsx').read_bytes()
        self.engine.advance(self.store.job(self.jobId))
        with self.assertLogs(level='ERROR'), patch('ulb_scraper.exporter.Workbook.save', side_effect=PermissionError('file is open')):
            self.engine.exportSelected([self.jobId])
        refreshed = self.store.job(self.jobId)
        self.assertTrue(refreshed['exportError'])
        self.assertNotEqual(refreshed['dataVersion'], refreshed['exportVersion'])
        self.assertEqual((Path(job['folder']) / 'properties.xlsx').read_bytes(), existing)

    def testLockedWorkbookRecoversAutomaticallyWithoutExportButton(self):
        self.initialize()
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        with self.assertLogs(level='ERROR'), patch('ulb_scraper.exporter.Workbook.save', side_effect=PermissionError('file is open')):
            self.engine.flushExports()
        self.assertTrue(self.store.job(self.jobId)['exportError'])
        self.engine.exportAttempts[self.jobId] = time.monotonic() - 61
        self.engine.flushExports()
        job = self.store.job(self.jobId)
        self.assertFalse(job['exportError'])
        self.assertEqual(job['dataVersion'], job['exportVersion'])
        self.assertTrue((Path(job['folder']) / 'properties.xlsx').exists())

    def testPreparationFailuresRetainResumeAndBoundedRetries(self):
        for attempt in range(1, 6):
            self.engine.handleFailure(self.store.job(self.jobId), PortalResponseError('Boundary unavailable'))
            job = self.store.job(self.jobId)
            self.assertEqual(job['attempts'], attempt)
            if attempt < 5:
                self.assertTrue(job['desired'])
                self.assertFalse(self.engine.jobReady(job))
        self.assertEqual(job['status'], 'error')
        self.assertFalse(job['desired'])
        self.store.control([self.jobId], 'start')
        self.assertTrue(self.engine.jobReady(self.store.job(self.jobId)))

    def testPreparationFailureDoesNotOverwriteManualPause(self):
        oldJob = self.store.job(self.jobId)
        self.store.control([self.jobId], 'pause')
        self.engine.handleFailure(oldJob, PortalResponseError('Boundary unavailable'))
        self.assertEqual(self.store.job(self.jobId)['status'], 'paused')

    def testCoordinatorTransportFailureAlsoSuspendsWorkers(self):
        self.engine.authenticated = True
        with self.assertRaises(OSError):
            self.engine.network(lambda: (_ for _ in ()).throw(OSError('Offline')))
        self.assertTrue(self.engine.servicePaused)
        self.assertIn(('session', 'unavailable'), self.events)

    def testDeletedJobCascadesCheckpoints(self):
        self.initialize()
        self.engine.advance(self.store.job(self.jobId))
        self.store.deleteJobs([self.jobId])
        with self.store.connect() as connection:
            self.assertFalse(connection.execute('SELECT COUNT(*) FROM tiles').fetchone()[0])
            self.assertFalse(connection.execute('SELECT COUNT(*) FROM properties').fetchone()[0])

    def testDetailFixtureAndPhotoFiltering(self):
        fixture = Path(__file__).resolve().parents[1] / 'refrence' / 'jaigopal.html'
        if not fixture.exists():
            self.skipTest('Optional recorded HTML fixture not shipped')
        detail = parsePropertyDetailHtml(fixture.read_text())
        self.assertFalse(isLoginHtml(fixture.read_text()))
        self.assertEqual(detail['Detail']['AsPerRecordPkPropertyId'], '5591106')
        self.assertIn('X', detail['Detail']['MobileNumberLinkedWithThePID'])
        self.assertNotIn('InputValues', detail['Detail'])
        urls = propertyPhotoUrls({}, detail)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all('/NDCImages/' in url for url in urls))

    def testLoginHtmlDistinguishesSharedScripts(self):
        self.assertTrue(isLoginHtml('<form action="/Account/Login"><input name="userName"></form>'))
        self.assertFalse(isLoginHtml("<script>var loginusing = 'Mobile';</script>"))

    def testPendingEmptyJobsDoNotGenerateThousandsOfEmptySnapshots(self):
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connect:
            self.engine.flushExports(force=True)
            # Empty jobs must not open another connection for each property scan.
            self.assertEqual(connect.call_count, 1)
        folder = Path(self.store.job(self.jobId)['folder'])
        self.assertFalse(folder.exists())

    def testUnavailableDetailsRetainProperty(self):
        client = PortalClient(cookieFile=self.home / 'test-cookies')
        parsed = client.fetchDetails({'pkPropertyId': 7})
        self.assertEqual(parsed['Detail']['FetchStatus'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
