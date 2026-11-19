'''Legacy imports, retained baselines, portable resume and update generations.'''

# Standard Library Imports
from contextlib import closing
import csv
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

# Third Party Imports
from openpyxl import Workbook

from ulb_scraper import datasets
from ulb_scraper.engine import DatasetCatalogError, Engine
from ulb_scraper.exporter import exportJob, iterExportRows
from ulb_scraper.store import Store


BOUNDARY = 'POLYGON ((76 30,76.0001 30,76.0001 30.0001,76 30.0001,76 30))'
PAIR = ({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})


class UpdateClient:
    '''Deterministic catalog/map/detail/photo transport with no live requests.'''

    def __init__(self, **kwargs):
        self.rows = [{'pkPropertyId': '101', 'PID': 'P1', 'Lat': 30.00005, 'Long': 76.00005,
                      'Token': 'fresh-token'}]
        self.calls = []
        self.address = 'New address'
        self.unavailable = False
        self.retired = False

    def requestJson(self, method, url, **kwargs):
        self.calls.append(url)
        if 'GetULBList' in url:
            return [{'Value': '1', 'Text': 'Town'}]
        if 'GetULBColony' in url:
            return [] if self.retired else [{'Value': '2', 'Text': 'Colony'}]
        if 'GetColonyBoundry' in url:
            return {'Wkt': BOUNDARY}
        return self.rows

    def fetchDetails(self, raw):
        self.calls.append('details:' + raw['PID'])
        if self.unavailable:
            return {'Detail': {'FetchStatus': 'unavailable', 'ParseVersion': 2}}
        return {'Detail': {'FetchStatus': 'ok', 'ParseVersion': 2,
                           'Fields': {'PID': raw['PID'], 'Address': self.address},
                           'OwnerRows': [{'Name': 'Owner', 'Gender': 'Male', 'Relation': 'Father',
                                          'Father / Husband Name': 'Parent', 'Share': '1/1'}]}}

    def downloadPhoto(self, url, folder, identity):
        self.calls.append('photo:' + identity)
        relative = f'photos/{identity}.jpg'
        (Path(folder) / 'photos').mkdir(exist_ok=True)
        (Path(folder) / relative).write_bytes(b'fresh photograph')
        return relative


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / 'records'
        self.folder = self.root / 'town_1' / 'colony_2'
        self.folder.mkdir(parents=True)
        self.store = Store(self.home / 'app.sqlite3')
        self.store.setSetting('requestDelayMs', 0)
        self.events = []
        self.engine = Engine(self.store, self.home, lambda *event: self.events.append(event), UpdateClient)
        self.engine.authenticated = True
        self.writeBaseline([{'PID': 'P1', 'Municipality': 'Town', 'Colony': 'Colony',
                             'Address': 'Old address', 'Owner 1: Name': 'Old owner S/O Parent (1/1)',
                             'Data Fetched At (IST)': '2000-01-01T05:30:00+05:30'},
                            {'PID': 'P2', 'Municipality': 'Town', 'Colony': 'Colony',
                             'Address': 'Retain absent property', 'Owner 1: Name': 'Retained owner',
                             'Data Fetched At (IST)': '2000-01-01T05:30:00+05:30'}])

    def tearDown(self):
        self.temporary.cleanup()

    def writeBaseline(self, rows, excel=True):
        headers = list(dict.fromkeys(label for row in rows for label in row))
        with (self.folder / 'properties.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, headers)
            writer.writeheader()
            writer.writerows(rows)
        if excel:
            workbook = Workbook()
            workbook.active.append(headers)
            for row in rows:
                workbook.active.append([row.get(label) for label in headers])
            workbook.save(self.folder / 'properties.xlsx')
            workbook.close()

    def importLegacy(self):
        inventory = datasets.inspectDataset(self.root)
        self.engine.importDataset(inventory)
        return self.store.jobs()[0]['id']

    def finish(self, jobId):
        for attempt in range(100):
            job = self.store.job(jobId)
            if job['status'] in ('complete', 'complete_warnings'):
                return job
            self.engine.advance(job)
        self.fail('Update did not finish')

    def exportedRows(self, jobId):
        with self.store.connect() as connection:
            return {row['PID']: row for row in iterExportRows(connection, jobId)}

    def testValidationAndConfirmationDoNotModifyOriginalFiles(self):
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        inventory = datasets.inspectDataset(self.root)
        self.assertEqual(inventory['entries'][0]['rowCount'], 2)
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir()}, before)
        self.assertFalse(self.store.jobs())
        self.engine.importDataset(inventory)
        for name, content in before.items():
            self.assertEqual((self.folder / name).read_bytes(), content)
        self.assertTrue((self.folder / datasets.CHECKPOINT).is_file())

    def testChangedNewAndMissingRowsWithFreshMapGeneration(self):
        jobId = self.importLegacy()
        self.engine.client.rows.append(dict(self.engine.client.rows[0], pkPropertyId='103', PID='P3'))
        self.assertEqual(self.store.job(jobId)['coverage'], 0)
        self.assertEqual(self.store.job(jobId)['generation'], 1)
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'Old address')
        job = self.finish(jobId)
        self.assertEqual(job['status'], 'complete_warnings')
        self.assertAlmostEqual(job['coverage'], 1)
        rows = self.exportedRows(jobId)
        self.assertEqual(set(rows), {'P1', 'P2', 'P3'})
        self.assertEqual(rows['P1']['Address'], 'New address')
        self.assertEqual(rows['P1']['Update Review'], 'Updated')
        self.assertEqual(rows['P3']['Update Review'], 'New property')
        self.assertEqual(rows['P2']['Address'], 'Retain absent property')
        self.assertIn('Not found', rows['P2']['Update Review'])
        self.assertEqual(rows['P2']['Data Fetched At (IST)'], '2000-01-01T05:30:00+05:30')
        self.assertTrue(rows['P1']['Data Last Checked At (IST)'].endswith('+05:30'))
        exportJob(self.store, job)

    def testFailureRetainsGoodHistoricalDataAndOriginalTimestamp(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.engine.advance(self.store.job(jobId))
        for attempt in range(3):
            self.engine.handleFailure(self.store.job(jobId), ValueError('detail fixture failed'))
        row = self.exportedRows(jobId)['P1']
        self.assertEqual(row['Address'], 'Old address')
        self.assertEqual(row['Owner 1: Name'], 'Mr. Old owner S/O Parent (1/1)')
        self.assertEqual(row['Data Fetched At (IST)'], '2000-01-01T05:30:00+05:30')
        self.assertIn('failed', row['Update Review'])

    def testPublicRestrictionPreservesPreviouslyAvailableDetailValues(self):
        jobId = self.importLegacy()
        self.engine.client.unavailable = True
        self.finish(jobId)
        row = self.exportedRows(jobId)['P1']
        self.assertEqual(row['Address'], 'Old address')
        self.assertIn('Public details unavailable', row['Update Review'])
        self.assertNotEqual(row['Data Last Checked At (IST)'], '2000-01-01T05:30:00+05:30')

    def testDeletedQueueRestoresDetailsAndPhotoStagesWithoutAnotherDetailRequest(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.engine.advance(self.store.job(jobId))
        record = self.store.pendingProperty(jobId)
        parsed = self.engine.client.fetchDetails(json.loads(record['raw']))
        self.store.saveDetail(jobId, record['identity'], parsed, ['https://example.test/photo.jpg'])
        self.store.control([jobId], 'pause')
        self.store.deleteJobs([jobId])
        self.assertFalse(self.store.jobs())
        self.engine.addQueuedPairs([PAIR], self.root)
        restored = self.store.jobs()[0]
        self.assertEqual(restored['mode'], 'update')
        self.assertEqual(restored['generation'], 1)
        self.assertEqual(restored['status'], 'paused')
        self.assertEqual(self.store.pendingProperty(restored['id'])['detailState'], 'ok')
        self.store.control([restored['id']], 'start')
        self.engine.client.calls.clear()
        self.engine.advance(self.store.job(restored['id']))
        self.assertFalse(any(call.startswith('details:') for call in self.engine.client.calls))
        self.assertTrue(any(call.startswith('photo:') and '_g1' in call for call in self.engine.client.calls))

    def testPortableDatasetCanMoveAndResumeWithAnEmptyApplicationDatabase(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.store.persistDataset(jobId)
        moved = self.home / 'moved'
        shutil.move(str(self.root), moved)
        other = Store(self.home / 'new-app.sqlite3')
        inventory = datasets.inspectDataset(moved)
        restoredId = other.importDataset(inventory)[0]
        restored = other.job(restoredId)
        self.assertEqual(restored['generation'], 1)
        self.assertEqual(restored['mode'], 'update')
        self.assertTrue(Path(restored['folder']).is_relative_to(moved.resolve()))
        self.assertTrue(other.pendingTile(restoredId))
        other.persistDataset(restoredId)

    def testCompletedDatasetStartsNewGenerationAndKeepsPreviousBaseline(self):
        jobId = self.importLegacy()
        self.finish(jobId)
        exportJob(self.store, self.store.job(jobId))
        generation = self.store.job(jobId)['generation']
        inventory = datasets.inspectDataset(self.root)
        self.engine.importDataset(inventory)
        job = self.store.job(jobId)
        self.assertEqual(job['generation'], generation + 1)
        self.assertEqual(job['coverage'], 0)
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'New address')
        with self.store.connect() as connection:
            row = connection.execute('SELECT row FROM datasetHistory WHERE jobId=? AND pid=\'P1\'', (jobId,)).fetchone()
            self.assertEqual(json.loads(row['row'])['Address'], 'New address')

    def testDeletingCompletedWarningJobKeepsCompletedGenerationInPortableCopy(self):
        jobId = self.importLegacy()
        self.finish(jobId)
        self.engine.handleCommand('delete', {'jobIds': [jobId]})
        self.assertFalse(self.store.jobs())
        inventory = datasets.inspectDataset(self.root)
        self.assertEqual(inventory['entries'][0]['portable']['status'], 'complete_warnings')
        self.engine.importDataset(inventory)
        self.assertEqual(self.store.jobs()[0]['generation'], 2)

    def testUnchangedPropertyDoesNotAdvanceLastChangedTime(self):
        jobId = self.importLegacy()
        self.finish(jobId)
        with self.store.connect() as connection:
            connection.execute("UPDATE baselines SET lastChanged='2000-01-01T00:00:00+00:00' WHERE jobId=? AND pid='P1'", (jobId,))
        exportJob(self.store, self.store.job(jobId))
        self.engine.importDataset(datasets.inspectDataset(self.root))
        self.finish(jobId)
        row = self.exportedRows(jobId)['P1']
        self.assertEqual(row['Update Review'], 'Unchanged')
        self.assertEqual(row['Data Last Changed At (IST)'], '2000-01-01T05:30:00+05:30')

    def testFailedMapDoesNotClaimHistoricalPropertiesAreAbsent(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        with self.store.connect() as connection:
            connection.execute("UPDATE tiles SET state='failed' WHERE jobId=?", (jobId,))
        self.engine.advance(self.store.job(jobId))
        self.assertTrue(all(row['Update Review'] == 'Not checked in this update' for row in self.exportedRows(jobId).values()))

    def testDuplicatePidsAndMismatchedSnapshotsAreRejectedWithoutJobs(self):
        self.writeBaseline([{'PID': 'P1'}, {'PID': 'P1'}])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            datasets.inspectDataset(self.root)
        self.writeBaseline([{'PID': 'P1', 'Address': 'csv'}])
        workbook = Workbook()
        workbook.active.append(['PID', 'Address'])
        workbook.active.append(['P1', 'different workbook'])
        workbook.save(self.folder / 'properties.xlsx')
        workbook.close()
        with self.assertRaisesRegex(ValueError, 'disagree'):
            datasets.inspectDataset(self.root)
        self.assertFalse(self.store.jobs())

    def testPhotoTraversalAndChangedConfirmationAreRejected(self):
        self.writeBaseline([{'PID': 'P1', 'Photo 1': '../../outside.jpg'}])
        with self.assertRaisesRegex(ValueError, 'escapes'):
            datasets.inspectDataset(self.root)
        self.writeBaseline([{'PID': 'P1', 'Address': 'old'}])
        inventory = datasets.inspectDataset(self.root)
        self.writeBaseline([{'PID': 'P1', 'Address': 'changed after review'}])
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            self.engine.importDataset(inventory)
        self.assertFalse(self.store.jobs())

    def testConflictingOutputLocationDoesNotReplaceExistingQueue(self):
        originalId = self.store.addJobs([PAIR], self.home / 'elsewhere')[0]
        before = self.store.job(originalId)
        with self.assertRaisesRegex(ValueError, 'another output folder'):
            self.engine.importDataset(datasets.inspectDataset(self.root))
        self.assertEqual(self.store.job(originalId), before)

    def testOfflineImportWaitsForCatalogValidationBeforeScanning(self):
        self.engine.authenticated = False
        jobId = self.importLegacy()
        self.assertFalse(self.store.job(jobId)['catalogChecked'])
        self.engine.authenticated = True
        self.engine.client.retired = True
        with self.assertRaises(DatasetCatalogError):
            self.engine.advance(self.store.job(jobId))
        self.assertFalse(self.store.pendingTile(jobId))
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'Old address')

    def testPortableCheckpointExcludesLoginPreferencesAndCookies(self):
        self.store.setSetting('phone', 'fixture-private-value')
        jobId = self.importLegacy()
        with closing(sqlite3.connect(self.folder / datasets.CHECKPOINT)) as connection:
            keys = {row[0] for row in connection.execute('SELECT key FROM settings')}
            self.assertEqual(keys, {'propertyParserVersion', 'exportSchemaVersion'})
        self.assertEqual(datasets.inspectDataset(self.root)['entries'][0]['portable']['id'], jobId)

    def testXlsxOnlyBaselineAndNormalAddOverwriteGuard(self):
        (self.folder / 'properties.csv').unlink()
        self.assertEqual(datasets.inspectDataset(self.root)['entries'][0]['rowCount'], 2)
        with self.assertRaisesRegex(ValueError, 'Use Update data'):
            self.engine.addQueuedPairs([PAIR], self.root)
        self.assertFalse(self.store.jobs())

    def testIncrementalCheckpointRetainsNewerDirtyRecordsDuringSnapshot(self):
        jobId = self.importLegacy()
        self.store.setStatus(jobId, 'paused', desired=0)
        original = datasets.upsert
        changed = False

        def saveAndChange(connection, table, row):
            nonlocal changed
            original(connection, table, row)
            if table == 'jobs' and not changed:
                changed = True
                with self.store.connect() as source:
                    source.execute("UPDATE baselines SET review='error' WHERE jobId=? AND pid='P1'", (jobId,))

        with patch.object(datasets, 'upsert', side_effect=saveAndChange):
            self.store.persistDataset(jobId)
        with self.store.connect() as connection:
            self.assertTrue(connection.execute('SELECT 1 FROM datasetChanges WHERE jobId=?', (jobId,)).fetchone())
        self.store.persistDataset(jobId)
        with closing(sqlite3.connect(self.folder / datasets.CHECKPOINT)) as connection:
            self.assertEqual(connection.execute("SELECT review FROM baselines WHERE pid='P1'").fetchone()[0], 'error')
        with self.store.connect() as connection:
            self.assertFalse(connection.execute('SELECT 1 FROM datasetChanges WHERE jobId=?', (jobId,)).fetchone())

    def testCheckpointFailurePreventsQueueDeletion(self):
        jobId = self.importLegacy()
        with patch.object(self.store, 'persistDataset', side_effect=OSError('fixture full disk')):
            with self.assertRaises(OSError):
                self.store.deleteJobs([jobId])
        self.assertIsNotNone(self.store.job(jobId))

    def testInvalidPortableBaselineIsRejectedBeforeImport(self):
        self.importLegacy()
        with closing(sqlite3.connect(self.folder / datasets.CHECKPOINT)) as connection:
            with connection:
                connection.execute("UPDATE baselines SET row=? WHERE pid='P1'",
                                   (json.dumps({'PID': 'P1', 'Photo 1': '../../outside.jpg'}),))
        with self.assertRaisesRegex(ValueError, 'escapes'):
            datasets.inspectDataset(self.root)

    def testResumeKeepsGenerationAndExplicitRestartResetsCoverage(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.engine.advance(self.store.job(jobId))
        self.store.control([jobId], 'pause')
        self.store.persistDataset(jobId)
        self.engine.importDataset(datasets.inspectDataset(self.root))
        self.assertEqual(self.store.job(jobId)['generation'], 1)
        self.assertFalse(self.store.job(jobId)['desired'])
        self.engine.importDataset(datasets.inspectDataset(self.root), restart=True)
        self.assertEqual(self.store.job(jobId)['generation'], 2)
        self.assertEqual(self.store.job(jobId)['coverage'], 0)
        self.assertTrue(self.store.job(jobId)['desired'])
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'Old address')

    def testDuplicateMapPidsRollBackTileAndPreserveBaseline(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.engine.client.rows.append(dict(self.engine.client.rows[0], pkPropertyId='102'))
        with self.assertRaisesRegex(ValueError, 'same PID'):
            self.engine.advance(self.store.job(jobId))
        self.assertEqual(self.store.job(jobId)['uniqueCount'], 0)
        self.assertTrue(self.store.pendingTile(jobId))
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'Old address')

    def testPhotoContentComparisonIgnoresGenerationFilenames(self):
        (self.folder / 'photos').mkdir()
        (self.folder / 'photos' / 'old.jpg').write_bytes(b'same photo')
        (self.folder / 'photos' / 'new.jpg').write_bytes(b'same photo')
        old = {'PID': 'P1', 'Photo 1': 'photos/old.jpg', 'Photos': 'photos/old.jpg'}
        new = {'PID': 'P1', 'Photo 1': 'photos/new.jpg', 'Photos': 'photos/new.jpg'}
        self.assertEqual(datasets.contentHash(old, self.folder), datasets.contentHash(new, self.folder))
        (self.folder / 'photos' / 'new.jpg').write_bytes(b'changed photo')
        self.assertNotEqual(datasets.contentHash(old, self.folder), datasets.contentHash(new, self.folder))

    def testFailedPhotoKeepsHistoricalRowAndItsPhotos(self):
        jobId = self.importLegacy()
        self.engine.advance(self.store.job(jobId))
        self.engine.advance(self.store.job(jobId))
        record = self.store.pendingProperty(jobId)
        parsed = self.engine.client.fetchDetails(json.loads(record['raw']))
        self.store.saveDetail(jobId, record['identity'], parsed, ['https://example.test/broken.jpg'])
        self.store.photoError(jobId, record['identity'], 'https://example.test/broken.jpg', 'fixture failure')
        self.store.finishProperty(jobId, record['identity'])
        self.assertEqual(self.exportedRows(jobId)['P1']['Address'], 'Old address')
        self.assertIn('failed', self.exportedRows(jobId)['P1']['Update Review'])

    def testOrdinaryScrapeSurvivesDeletionAndReaddWithoutBecomingUpdate(self):
        shutil.rmtree(self.root)
        jobId = self.engine.addQueuedPairs([PAIR], self.root)[0]
        self.engine.advance(self.store.job(jobId))
        self.store.control([jobId], 'pause')
        self.store.deleteJobs([jobId])
        restoredId = self.engine.addQueuedPairs([PAIR], self.root)[0]
        job = self.store.job(restoredId)
        self.assertEqual(job['mode'], 'scrape')
        self.assertEqual(job['generation'], 0)
        self.assertEqual(job['status'], 'paused')
        self.assertTrue(self.store.pendingTile(restoredId))

    def testCatalogRenamesReuseDatasetIdsInsteadOfCreatingAnotherFolder(self):
        jobId = self.importLegacy()
        renamed = ({'id': '1', 'name': 'Renamed Town'}, {'id': '2', 'name': 'Renamed Colony'})
        self.assertEqual(self.engine.addQueuedPairs([renamed], self.root), [])
        self.store.deleteJobs([jobId])
        restoredId = self.engine.addQueuedPairs([renamed], self.root)[0]
        self.assertEqual(Path(self.store.job(restoredId)['folder']), self.folder.resolve())
        self.assertEqual(len(datasets.inspectDataset(self.root)['entries']), 1)
