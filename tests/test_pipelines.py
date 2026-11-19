'''End-to-end property pipelines, concurrent request gates, and state migrations.'''

# Standard Library Imports
from base64 import b64decode
from contextlib import contextmanager
from email.message import Message
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

# Third Party Imports
from openpyxl import load_workbook

from scraper import AuthenticationError, isLoginHtml, PortalHttpError, PortalResponseError
from ulb_scraper.engine import Engine, WorkSuspended
from ulb_scraper.geometry import readBoundary
from ulb_scraper.portal import PortalClient, propertyPhotoUrls
from ulb_scraper.store import SCHEMA, Store


BOUNDARY = 'POLYGON ((76 30,76.001 30,76.001 30.001,76 30.001,76 30))'
LOGINHTML = '<html><title>Login</title><input id="txtUser"><input id="txtOTP"></html>'
PNG = b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY1sAAAAASUVORK5CYII=')
FIXTUREDIR = Path(__file__).resolve().parents[1] / 'refrence'


class Response(io.BytesIO):
    def __init__(self, content, url):
        super().__init__(content)
        self.url = url
        self.headers = Message()
        self.headers['Content-Type'] = 'image/png'

    def geturl(self):
        return self.url


class Simulation:
    '''Recorded HTML and binary responses with deterministic fault injection.'''

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.calls = []
        self.starts = []
        self.delay = 0.02
        self.expired = False
        self.detailFault = False
        self.photoFault = False
        self.mapFault = False
        self.block = threading.Event()
        self.release = threading.Event()
        self.blockDetails = False
        self.clientCount = 0

    @contextmanager
    def request(self, kind, identity=''):
        with self.lock:
            self.active += 1
            self.maximum = max(self.active, self.maximum)
            self.calls.append((kind, identity, threading.current_thread().name))
            self.starts.append(time.monotonic())
        try:
            time.sleep(self.delay)
            yield
        finally:
            with self.lock:
                self.active -= 1


class SimulatedClient(PortalClient):
    '''Real detail parser and image downloader over simulated HTTP responses.'''

    def __init__(self, simulation, **kwargs):
        super().__init__(**kwargs)
        self.simulation = simulation
        self.opener = self
        with simulation.lock:
            simulation.clientCount += 1

    def clone(self):
        return SimulatedClient(self.simulation, cookieFile=self.cookieFile, fresh=True, persist=False)

    def requestText(self, method, url, **kwargs):
        if 'MyProperties' in url:
            with self.simulation.request('session'):
                return LOGINHTML if self.simulation.expired else '<title>My Properties</title><script>getSearchPropertiesDataMyProperties()</script>'
        if 'token=stale' in url:
            return LOGINHTML
        with self.simulation.request('detail', url.rsplit('=', 1)[-1]):
            if self.simulation.blockDetails:
                self.simulation.block.set()
                self.simulation.release.wait(3)
            if self.simulation.expired:
                return LOGINHTML
            if self.simulation.detailFault and '5591106' in url:
                return '<title>Property</title><main>Changed markup</main>'
            name = 'jaigopal.html' if '5591106' in url else 'embros.html'
            return (FIXTUREDIR / name).read_text(encoding='utf-8')

    def requestJson(self, method, url, **kwargs):
        with self.simulation.request('map' if 'getSearchPropertiesData' in url else 'catalog'):
            if self.simulation.expired:
                raise PortalHttpError(500)
            if 'GetULBList' in url:
                return [{'Value': '60', 'Text': 'PANCHKULA'}, {'Value': '52', 'Text': 'JIND'}]
            if 'GetULBColony' in url:
                return [{'Value': '18949', 'Text': 'Sector 6'}, {'Value': '19036', 'Text': 'Jalauli'}] if '60' in url else [{'Value': '23485', 'Text': 'Lal Dora Ramra Gate'}]
            if 'GetColonyBoundry' in url:
                return {'Wkt': BOUNDARY}
            if self.simulation.mapFault:
                raise PortalHttpError(500)
            return [
                {'pkPropertyId': identity, 'Lat': 30.0005, 'Long': 76.0005,
                 'Token': str(identity), 'OwnerName': 'Map owner',
                 'ImageViewLink': f'https://property.ulbharyana.gov.in/NDCImages/{identity}/thumb.jpg'}
                for identity in (5591106, 5582928)
            ]

    def open(self, req, timeout=45):
        with self.simulation.request('photo', req.full_url):
            if self.simulation.photoFault:
                raise HTTPError(req.full_url, 503, 'Unavailable', Message(), io.BytesIO(b''))
            return Response(PNG, req.full_url)

    def generateOtp(self, phone):
        self.phone = phone

    def verifyOtp(self, otp):
        if otp != '123456':
            raise PortalResponseError('Incorrect OTP')
        self.simulation.expired = False


@unittest.skipUnless((FIXTUREDIR / 'jaigopal.html').exists(), 'Recorded HTML fixtures unavailable')
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.store.setSetting('requestDelayMs', 0)
        self.store.setSetting('parallelRequests', 2)
        self.simulation = Simulation()
        self.events = []
        self.factory = lambda **kwargs: SimulatedClient(self.simulation, **kwargs)
        self.engine = Engine(self.store, self.home, lambda *event: self.events.append(event), self.factory)
        self.jobId = self.addJob('60', '18949', 'Sector 6')

    def tearDown(self):
        self.simulation.release.set()
        if self.engine.is_alive():
            self.engine.stop()
            self.engine.join(5)
        self.assertFalse(self.engine.is_alive())
        self.temporary.cleanup()

    def addJob(self, municipalityId, colonyId, colonyName):
        identity = self.store.addJobs([({'id': municipalityId, 'name': 'PANCHKULA' if municipalityId == '60' else 'JIND'},
                                       {'id': colonyId, 'name': colonyName})], self.home / 'exports')[0]
        self.store.initializeTiles(identity, BOUNDARY, [(76, 30, 76.001, 30.001)])
        return identity

    def waitFor(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail('Condition was not reached before timeout')

    def testPropertyDetailsAndPhotosFinishBeforeNextPropertyOrTile(self):
        self.engine.advance(self.store.job(self.jobId))
        self.assertFalse(self.store.job(self.jobId)['completedCount'])
        self.engine.advance(self.store.job(self.jobId))
        record = self.store.pendingProperty(self.jobId)
        self.assertEqual(record['identity'], '5582928')
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 1)
        photos = self.store.propertyPhotos(self.jobId, '5591106')
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(photo['state'] == 'ok' for photo in photos))
        folder = Path(self.store.job(self.jobId)['folder'])
        self.assertTrue(all((folder / photo['path']).read_bytes() == PNG for photo in photos))
        kinds = [item[0] for item in self.simulation.calls]
        self.assertEqual(kinds, ['map', 'detail', 'photo', 'photo', 'photo'])

    def testRestrictedPropertySkipsDetailRequestAndThrottleSlot(self):
        self.engine.advance(self.store.job(self.jobId))
        with self.store.connect() as connection:
            record = connection.execute("SELECT raw FROM properties WHERE identity='5591106'").fetchone()
            raw = json.loads(record['raw'])
            raw['IsDataVerified2023'] = True
            connection.execute("UPDATE properties SET raw=? WHERE identity='5591106'", (json.dumps(raw),))
        with patch.object(self.engine, 'network', wraps=self.engine.network) as network:
            self.engine.advance(self.store.job(self.jobId))
        stages = [call.args[0].__name__ for call in network.call_args_list]
        self.assertEqual(stages, ['downloadPhoto'])
        self.assertNotIn('detail', [item[0] for item in self.simulation.calls])
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 1)
        self.assertFalse(self.store.problemCount(self.jobId))

    def testMultiColonyRunHasRealParsedFieldsPhotosAndRelativeLinks(self):
        self.addJob('60', '19036', 'Jalauli')
        self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.engine.start()
        self.waitFor(lambda: all(job['status'] == 'complete' for job in self.store.jobs()))
        self.engine.stop()
        self.engine.join(5)
        self.assertGreaterEqual(self.simulation.maximum, 2)
        self.assertLessEqual(self.simulation.maximum, 2)
        self.assertGreaterEqual(self.simulation.clientCount, 3)
        for job in self.store.jobs():
            self.assertEqual(job['completedCount'], 2)
            folder = Path(job['folder'])
            workbook = load_workbook(folder / 'properties.xlsx')
            sheet = workbook.active
            headers = [cell.value for cell in sheet[1]]
            self.assertEqual(sheet.max_row, 3)
            for cells in list(sheet.iter_rows())[1:]:
                values = dict(zip(headers, [cell.value for cell in cells]))
                self.assertNotIn('Property Availability', values)
                self.assertNotIn('Processing Status', values)
                self.assertTrue(values['Owner 1: Name'])
                self.assertNotEqual(values['Data Fetched At (IST)'], '-')
                for photoIndex, header in enumerate(headers):
                    if header.startswith('Photo ') and header[6:].isdigit() and cells[photoIndex].value != '-':
                        self.assertTrue(cells[photoIndex].hyperlink.target.startswith('photos/'))
                        self.assertTrue((folder / cells[photoIndex].value).exists())
            workbook.close()

    def testParallelSettingOneSerializesEveryRequest(self):
        self.store.setSetting('parallelRequests', 1)
        self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.engine.start()
        self.waitFor(lambda: all(job['status'] == 'complete' for job in self.store.jobs()))
        self.assertEqual(self.simulation.maximum, 1)

    def testParallelCeilingCanUseMoreThanEightColonyWorkers(self):
        self.store.setSetting('parallelRequests', 12)
        # Simulate a ceiling reached after a healthy ramp; startup is bounded to four.
        self.engine.traffic.limit = 12
        self.simulation.delay = 0.15
        for index in range(11):
            self.addJob('60', str(20000 + index), f'Colony {index}')
        self.engine.start()
        self.waitFor(lambda: self.simulation.maximum > 8)
        self.store.control([job['id'] for job in self.store.jobs()], 'pause')
        self.engine.stop()
        self.engine.join(5)
        self.assertLessEqual(self.simulation.maximum, 12)

    def testGlobalRequestIntervalSpacesConcurrentWorkers(self):
        self.store.setSetting('requestDelayMs', 30)
        self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.engine.start()
        self.waitFor(lambda: all(job['status'] == 'complete' for job in self.store.jobs()))
        intervals = [later - earlier for earlier, later in zip(self.simulation.starts, self.simulation.starts[1:])]
        self.assertTrue(intervals)
        self.assertGreaterEqual(min(intervals), 0.025)

    def testSingleWorkerMakesPropertyProgressBeforeOpeningOtherQueuedMaps(self):
        self.store.setSetting('parallelRequests', 1)
        self.addJob('60', '19036', 'Jalauli')
        self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.engine.start()
        self.waitFor(lambda: all(job['status'] == 'complete' for job in self.store.jobs()))
        kinds = [item[0] for item in self.simulation.calls if item[0] in ('map', 'detail', 'photo')]
        self.assertEqual(kinds[:3], ['map', 'detail', 'photo'])

    def testHundredQueuedJobsStartWithOnlyFourActiveColonies(self):
        self.store.setSetting('parallelRequests', 20)
        self.simulation.blockDetails = True
        for index in range(99):
            self.addJob('60', str(30000 + index), f'Queued colony {index}')
        self.engine.start()
        self.assertTrue(self.simulation.block.wait(5))
        self.assertLessEqual(self.simulation.maximum, 4)
        self.assertLessEqual(sum(bool(job['uniqueCount']) for job in self.store.jobs()), 4)
        self.store.control([job['id'] for job in self.store.jobs()], 'pause')
        self.simulation.release.set()

    def testSharedMapFailuresRecoverThroughWorkRequestsWithoutLosingTiles(self):
        self.addJob('60', '19036', 'Jalauli')
        self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.store.setSetting('parallelRequests', 4)
        self.simulation.mapFault = True
        self.engine.start()
        self.waitFor(lambda: self.engine.servicePaused)
        self.waitFor(lambda: not self.engine.inFlight)
        self.assertTrue(all(self.store.pendingTile(job['id']) for job in self.store.jobs()))
        self.simulation.mapFault = False
        self.engine.traffic.minimumDelay = 0
        self.engine.retryUntil = 0
        self.engine.nextRequest = 0
        with self.store.connect() as connection:
            connection.execute('UPDATE tiles SET retryAt=0')
        self.waitFor(lambda: all(job['status'] == 'complete' for job in self.store.jobs()), timeout=10)
        self.assertFalse(self.engine.servicePaused)
        self.assertTrue(all(job['completedCount'] == 2 for job in self.store.jobs()))

    def testRejectedSavedTokenRefreshesMarkerWithoutExpiringValidSession(self):
        self.engine.advance(self.store.job(self.jobId))
        record = self.store.pendingProperty(self.jobId)
        raw = json.loads(record['raw'])
        raw['Token'] = 'stale'
        with self.store.connect() as connection:
            connection.execute('UPDATE properties SET raw=? WHERE jobId=? AND identity=?',
                               (json.dumps(raw), self.jobId, record['identity']))
        self.engine.start()
        self.waitFor(lambda: self.store.job(self.jobId)['status'] == 'complete')
        self.assertNotIn(('session', 'expired'), self.events)
        self.assertTrue(self.engine.authenticated)
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 2)
        with self.store.connect() as connection:
            current = connection.execute('SELECT * FROM properties WHERE jobId=? AND identity=?',
                                         (self.jobId, record['identity'])).fetchone()
            self.assertEqual(current['tokenRefreshes'], 1)
            self.assertNotEqual(json.loads(current['raw'])['Token'], 'stale')

    def testShutdownMidPropertyRestartsFromDurableDetailCheckpoint(self):
        self.simulation.blockDetails = True
        self.engine.start()
        self.assertTrue(self.simulation.block.wait(3))
        self.engine.stop()
        self.simulation.release.set()
        self.engine.join(5)
        self.assertFalse(self.engine.is_alive())
        self.assertTrue(self.store.job(self.jobId)['desired'])
        self.assertIsNotNone(self.store.pendingProperty(self.jobId))
        self.engine = Engine(Store(self.home / 'state.sqlite3'), self.home,
                             lambda *event: self.events.append(event), self.factory)
        self.engine.start()
        self.waitFor(lambda: self.store.job(self.jobId)['status'] == 'complete')
        self.assertEqual(self.store.job(self.jobId)['uniqueCount'], 2)
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 2)
        self.assertEqual(sum(item[:2] == ('detail', '5591106') for item in self.simulation.calls), 1)

    def testEachBadPhotoGetsItsOwnRetriesAndDoesNotBlockLaterProperties(self):
        self.simulation.photoFault = True
        self.engine.advance(self.store.job(self.jobId))
        first = self.store.pendingProperty(self.jobId)['identity']
        failures = 0
        while self.store.pendingProperty(self.jobId)['identity'] == first:
            failures += 1
            self.assertLessEqual(failures, 9)
            with self.assertRaises(PortalHttpError) as context:
                self.engine.advance(self.store.job(self.jobId))
            self.engine.handleFailure(self.store.job(self.jobId), context.exception)
            with self.store.connect() as connection:
                connection.execute('UPDATE properties SET retryAt=0')
        self.assertEqual(failures, 9)
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 1)
        self.assertTrue(all(photo['state'] == 'error' for photo in self.store.propertyPhotos(self.jobId, first)))
        self.simulation.photoFault = False
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.assertEqual(self.store.job(self.jobId)['status'], 'complete_warnings')
        self.store.control([self.jobId], 'start')
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.assertEqual(self.store.job(self.jobId)['status'], 'complete')
        self.assertEqual(self.store.job(self.jobId)['completedCount'], 2)
        self.assertFalse(self.store.problemCount(self.jobId))

    def testStaleWorkerCannotExpireANewlyVerifiedSession(self):
        self.engine.authenticated = True
        self.engine.local.jobId = self.jobId
        self.engine.workerClient()

        def earlierSessionResponse():
            self.engine.sessionEpoch += 1
            raise AuthenticationError('Old session expired')

        with self.assertRaises(WorkSuspended):
            self.engine.network(earlierSessionResponse)
        self.assertTrue(self.engine.authenticated)
        self.assertNotIn(('session', 'expired'), self.events)
        self.engine.local.jobId = None

    def testPhotoReturningRootLoginPageTriggersAuthenticationNotPhotoError(self):
        client = self.factory(cookieFile=self.home / 'cookies')
        response = Response(LOGINHTML.encode(), 'https://property.ulbharyana.gov.in/')
        response.headers.replace_header('Content-Type', 'text/html')
        with patch.object(client, 'open', return_value=response):
            with self.assertRaises(AuthenticationError):
                client.downloadPhoto('https://property.ulbharyana.gov.in/photo.jpg', self.home, '7')

    def testExpiredSessionReturningHTTP500StopsAllWithoutDiscardingTiles(self):
        otherId = self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.engine.authenticated = True
        self.simulation.expired = True
        self.engine.local.jobId = self.jobId
        with self.assertRaises(PortalHttpError):
            self.engine.advance(self.store.job(self.jobId))
        self.engine.local.jobId = None
        self.engine.handleFailure(self.store.job(self.jobId), PortalHttpError(500))
        self.assertFalse(self.engine.authenticated)
        self.assertIn(('session', 'expired'), self.events)
        for identity in (self.jobId, otherId):
            self.assertTrue(self.store.job(identity)['desired'])
            self.assertIsNotNone(self.store.pendingTile(identity))
            self.assertFalse(self.store.job(identity)['completedCount'])

    def testExpiryDuringParallelWorkThenOTPResumesSavedCheckpoints(self):
        otherId = self.addJob('52', '23485', 'Lal Dora Ramra Gate')
        self.simulation.blockDetails = True
        self.engine.start()
        self.assertTrue(self.simulation.block.wait(3))
        self.simulation.expired = True
        self.simulation.release.set()
        self.waitFor(lambda: not self.engine.authenticated)
        self.assertFalse(self.store.job(self.jobId)['completedCount'])
        self.assertFalse(self.store.job(otherId)['completedCount'])
        self.store.control([otherId], 'pause')
        self.engine.submit('generateOtp', phone='9999999999')
        self.waitFor(lambda: any(event[0] == 'otpSent' for event in self.events))
        self.engine.submit('verifyOtp', otp='123456')
        self.waitFor(lambda: self.store.job(self.jobId)['status'] == 'complete')
        self.assertFalse(self.store.job(otherId)['desired'])
        self.assertEqual(self.store.job(self.jobId)['uniqueCount'], 2)

    def testPauseDuringDetailRequestAndResumeWithoutLosingPhotoProgress(self):
        self.simulation.blockDetails = True
        self.engine.start()
        self.assertTrue(self.simulation.block.wait(3))
        self.store.control([self.jobId], 'pause')
        self.simulation.release.set()
        self.waitFor(lambda: self.jobId not in self.engine.inFlight)
        self.assertEqual(self.store.job(self.jobId)['status'], 'paused')
        self.assertIsNotNone(self.store.pendingProperty(self.jobId))
        self.store.control([self.jobId], 'start')
        self.waitFor(lambda: self.store.job(self.jobId)['status'] == 'complete')
        details = [item for item in self.simulation.calls if item[:2] == ('detail', '5591106')]
        self.assertEqual(len(details), 1)

    def testDeleteDuringRequestDrainsWorkerAndNeverRecreatesJob(self):
        self.simulation.blockDetails = True
        self.engine.start()
        self.assertTrue(self.simulation.block.wait(3))
        self.store.control([self.jobId], 'pause')
        self.engine.submit('delete', jobIds=[self.jobId])
        self.simulation.release.set()
        self.waitFor(lambda: self.store.job(self.jobId) is None)
        time.sleep(0.1)
        self.assertIsNone(self.store.job(self.jobId))
        with self.store.connect() as connection:
            self.assertFalse(connection.execute('SELECT COUNT(*) FROM properties').fetchone()[0])

    def testMalformedPropertyRetainedAsWarningAndNextPropertyContinues(self):
        self.simulation.detailFault = True
        self.engine.advance(self.store.job(self.jobId))
        for unusedAttempt in range(3):
            with self.assertRaises(PortalResponseError) as context:
                self.engine.advance(self.store.job(self.jobId))
            self.engine.handleFailure(self.store.job(self.jobId), context.exception)
            with self.store.connect() as connection:
                connection.execute('UPDATE properties SET retryAt=0')
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        job = self.store.job(self.jobId)
        self.assertEqual(job['status'], 'complete_warnings')
        self.assertEqual(job['completedCount'], 2)
        self.assertEqual(self.store.problemCount(self.jobId), 1)
        self.simulation.detailFault = False
        self.store.control([self.jobId], 'start')
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.assertEqual(self.store.job(self.jobId)['status'], 'complete')
        self.assertFalse(self.store.problemCount(self.jobId))

    def testPortableColonyFolderIncludesAllPhotoTargetsAfterMove(self):
        self.engine.advance(self.store.job(self.jobId))
        self.engine.advance(self.store.job(self.jobId))
        self.engine.flushExports(force=True)
        folder = Path(self.store.job(self.jobId)['folder'])
        destination = self.home / 'moved-colony'
        folder.rename(destination)
        workbook = load_workbook(destination / 'properties.xlsx')
        for row in workbook.active.iter_rows(min_row=2):
            for cell in row:
                if cell.hyperlink:
                    self.assertTrue((destination / cell.hyperlink.target).exists())
        workbook.close()

    def testSavedLoginFixtureIsRecognizedDespiteUnusualOTPInputNames(self):
        self.assertTrue(isLoginHtml(LOGINHTML))
        client = self.factory(cookieFile=self.home / 'cookies')
        self.simulation.expired = True
        with self.assertRaises(AuthenticationError):
            client.checkSession()
        with self.assertRaises(AuthenticationError):
            client.fetchDetails({'Token': '5591106'})


class MigrationTests(unittest.TestCase):
    def testThrottlePreferencesUseCacheAndChangeImmediately(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'state.sqlite3')
            store.setSetting('parallelRequests', 2)
            with patch.object(store, 'connect', side_effect=AssertionError('Should use cached setting')):
                self.assertEqual(store.setting('parallelRequests'), 2)
            store.setSetting('parallelRequests', 4)
            self.assertEqual(store.setting('parallelRequests'), 4)
            store.settingCache['parallelRequests'] = (time.monotonic() - 2, 2)
            self.assertEqual(store.setting('parallelRequests'), 4)

    def testOldCheckpointMigratesWithoutDiscardingRowsOrPauseState(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'old.sqlite3'
            oldSchema = SCHEMA.replace('    completedCount INTEGER NOT NULL DEFAULT 0,\n', '').replace(
                "    processingState TEXT NOT NULL DEFAULT 'pending', processingError TEXT NOT NULL DEFAULT '',\n", '').replace(
                '    attempts INTEGER NOT NULL DEFAULT 0, retryAt REAL NOT NULL DEFAULT 0,\n', '').replace(
                "    error TEXT NOT NULL DEFAULT '',\n", '')
            connection = sqlite3.connect(path)
            connection.executescript(oldSchema)
            connection.execute('''INSERT INTO jobs (id,municipalityId,municipalityName,colonyId,colonyName,folder,created,desired,status)
                VALUES ('old','60','PANCHKULA','1','Colony',?,0,0,'paused')''', (temporary,))
            connection.execute("INSERT INTO properties(jobId,identity,raw,detailState) VALUES ('old','7','{}','ok')")
            connection.commit()
            connection.close()
            store = Store(path)
            self.assertEqual(store.job('old')['completedCount'], 1)
            self.assertFalse(store.job('old')['desired'])
            self.assertEqual(store.job('old')['status'], 'paused')
            with store.connect() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM properties').fetchone()[0], 1)

    def testAdditionalMigrationDoesNotTurnExistingErrorsIntoSuccesses(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'prior.sqlite3'
            store = Store(path)
            identity = store.addJobs([({'id': '60', 'name': 'PANCHKULA'},
                                      {'id': '1', 'name': 'Colony'})], temporary)[0]
            with store.connect() as connection:
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detailState,processingState,processingError)
                    VALUES (?, '7', '{}', 'error', 'error', 'Retry required')''', (identity,))
                connection.execute('UPDATE jobs SET completedCount=1 WHERE id=?', (identity,))
                connection.execute('ALTER TABLE jobs DROP COLUMN attempts')
                connection.execute('ALTER TABLE jobs DROP COLUMN retryAt')
            reloaded = Store(path)
            self.assertEqual(reloaded.job(identity)['completedCount'], 1)
            self.assertEqual(reloaded.problemCount(identity), 1)
            with reloaded.connect() as connection:
                row = connection.execute('SELECT * FROM properties').fetchone()
                self.assertEqual(row['processingState'], 'error')
                self.assertEqual(row['processingError'], 'Retry required')


if __name__ == '__main__':
    unittest.main()
