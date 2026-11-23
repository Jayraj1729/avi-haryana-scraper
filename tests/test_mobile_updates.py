'''Missing-mobile updates, preserved baselines, report isolation and batch validation.'''

# Standard Library Imports
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

# Project Imports
from ulb_scraper import datasets
from ulb_scraper.engine import Engine
from ulb_scraper.exporter import exportJob, iterExportRows
from ulb_scraper.property_data import compactExportRow, personDescription
from ulb_scraper.store import Store


BOUNDARY = 'POLYGON ((76 30,76.0001 30,76.0001 30.0001,76 30.0001,76 30))'


class MobileClient:
    '''Record request scope while serving deterministic catalog and property data.'''

    def __init__(self, **kwargs):
        self.calls = []
        self.fail = False

    def requestJson(self, method, url, **kwargs):
        self.calls.append(url)
        if 'GetULBList' in url:
            return [{'Value': '1', 'Text': 'Town'}]
        if 'GetULBColony' in url:
            return [{'Value': '2', 'Text': 'Colony'}, {'Value': '3', 'Text': 'Other'}]
        if 'GetColonyBoundry' in url:
            return {'Wkt': BOUNDARY}
        return [{'PID': pid, 'pkPropertyId': pid, 'Token': 'fresh', 'Lat': 30.00005,
                 'Long': 76.00005, 'ImageViewLink': ['https://example.invalid/photo.jpg']}
                for pid in ['EMPTY', 'KNOWN', 'STILL_EMPTY', 'NEW']]

    def fetchDetails(self, raw):
        self.calls.append('details:' + raw['PID'])
        if self.fail:
            raise ValueError('Simulated detail failure')
        fields = {'Address': 'New address must not replace baseline'}
        if raw['PID'] == 'EMPTY':
            fields['Mobile No'] = '9000000001'
        return {'Detail': {'FetchStatus': 'ok', 'Fields': fields}}

    def downloadPhoto(self, *args):
        raise AssertionError('Mobile-only updates must not download photos')


class MobileUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / 'output'
        self.folder = self.root / 'town_1' / 'colony_2'
        self.folder.mkdir(parents=True)
        rows = [{'PID': pid, 'Municipality': 'Town', 'Colony': 'Colony', 'Address': 'Original address',
                 'Mobile Numbers': mobile, 'Data Fetched At (IST)': '2020-01-01T00:00:00+05:30'}
                for pid, mobile in [('EMPTY', '-'), ('KNOWN', '9000000002'), ('STILL_EMPTY', '')]]
        self.writeCsv(self.folder, rows)
        self.store = Store(self.home / 'state.sqlite3')
        self.store.setSetting('requestDelayMs', 0)
        self.events = []
        self.engine = Engine(self.store, self.home, lambda *event: self.events.append(event), MobileClient)
        self.engine.authenticated = True

    def tearDown(self):
        self.temporary.cleanup()

    def writeCsv(self, folder, rows):
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'properties.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def startUpdate(self):
        self.engine.importDataset(datasets.inspectDataset(self.root), mobileOnly=True)
        return self.store.jobs()[0]['id']

    def finish(self, jobId):
        for attempt in range(30):
            job = self.store.job(jobId)
            if job['status'] in ('complete', 'complete_warnings'):
                return
            self.engine.advance(job)
        self.fail('Mobile update did not finish')

    def testOnlyMissingTargetsAreFetchedAndOnlyNewNumbersAreReported(self):
        jobId = self.startUpdate()
        self.finish(jobId)
        self.assertEqual({call for call in self.engine.client.calls if call.startswith('details:')},
                         {'details:EMPTY', 'details:STILL_EMPTY'})
        with self.store.connect() as connection:
            rows = {row['PID']: row for row in iterExportRows(connection, jobId)}
        self.assertEqual(set(rows), {'EMPTY', 'KNOWN', 'STILL_EMPTY'})
        self.assertEqual(rows['EMPTY']['Mobile Numbers'], '9000000001')
        self.assertEqual(rows['KNOWN']['Mobile Numbers'], '9000000002')
        self.assertEqual(rows['EMPTY']['Address'], 'Original address')
        self.assertEqual(rows['KNOWN']['Data Fetched At (IST)'], '2020-01-01T00:00:00+05:30')
        exportJob(self.store, self.store.job(jobId))
        reportRoot = Path(self.store.job(jobId)['mobileReportRoot'])
        with (reportRoot / 'town_1' / 'colony_2' / 'properties.csv').open(encoding='utf-8-sig') as stream:
            report = list(csv.DictReader(stream))
        self.assertEqual([row['PID'] for row in report], ['EMPTY'])
        self.assertEqual(report[0]['Mobile Numbers'], '9000000001')
        self.assertEqual(len(datasets.inspectDataset(self.root)['entries']), 1)

    def testResumeAfterQueueRemovalUsesSameTargetsAndReportFolder(self):
        jobId = self.startUpdate()
        self.engine.advance(self.store.job(jobId))
        self.engine.advance(self.store.job(jobId))
        reportRoot = self.store.job(jobId)['mobileReportRoot']
        self.store.deleteJobs([jobId])
        self.engine.importDataset(datasets.inspectDataset(self.root), mobileOnly=True)
        restored = self.store.jobs()[0]
        self.assertEqual(restored['mobileReportRoot'], reportRoot)
        self.assertEqual(restored['updateScope'], 'missing_mobile')
        self.finish(restored['id'])
        with self.store.connect() as connection:
            targets = connection.execute('SELECT count(*) FROM baselines WHERE mobileTarget=1').fetchone()[0]
        self.assertEqual(targets, 2)

    def testMovingDatasetRebasesTheResumedReportFolder(self):
        jobId = self.startUpdate()
        reportName = Path(self.store.job(jobId)['mobileReportRoot']).name
        self.store.deleteJobs([jobId])
        moved = self.home / 'moved-records'
        shutil.move(str(self.root), moved)
        self.engine.importDataset(datasets.inspectDataset(moved), mobileOnly=True)
        restored = self.store.jobs()[0]
        self.assertEqual(Path(restored['mobileReportRoot']), (moved / reportName).resolve())
        self.finish(restored['id'])
        exportJob(self.store, self.store.job(restored['id']))
        self.assertTrue((moved / reportName / 'town_1' / 'colony_2' / 'properties.csv').exists())
        self.assertFalse(self.root.exists())

    def testMismatchedResumeScopeIsRejectedWithoutLosingSavedData(self):
        jobId = self.startUpdate()
        self.store.deleteJobs([jobId])
        with self.assertRaisesRegex(ValueError, 'original scope'):
            self.engine.importDataset(datasets.inspectDataset(self.root))
        self.assertFalse(self.store.jobs())
        self.assertTrue((self.folder / datasets.CHECKPOINT).exists())

    def testNoEligiblePropertiesFinishWithoutPortalPropertyRequests(self):
        with (self.folder / 'properties.csv').open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row['Mobile Numbers'] = '9000000002'
        self.writeCsv(self.folder, rows)
        jobId = self.startUpdate()
        self.assertEqual(self.store.job(jobId)['status'], 'complete')
        self.assertFalse(any(call.startswith('details:') for call in self.engine.client.calls))

    def testLegacyInvalidNumberIsEligibleForMissingMobileUpdate(self):
        jobId = self.startUpdate()
        # Simulate a baseline saved by an older version, before validation.
        with self.store.connect() as connection:
            baseline = json.loads(connection.execute(
                "SELECT row FROM baselines WHERE jobId=? AND pid='EMPTY'", (jobId,)).fetchone()['row'])
            baseline['Mobile Numbers'] = '123456789, 00XXXXX001, 1111111111, +91 0000000000'
            connection.execute("UPDATE baselines SET row=? WHERE jobId=? AND pid='EMPTY'",
                               (json.dumps(baseline), jobId))
        self.engine.importDataset(datasets.inspectDataset(self.root), restart=True, mobileOnly=True)
        with self.store.connect() as connection:
            targets = {row['pid']: row['mobileTarget'] for row in connection.execute(
                'SELECT pid,mobileTarget FROM baselines WHERE jobId=?', (jobId,))}
        self.assertEqual(targets, {'EMPTY': 1, 'KNOWN': 0, 'STILL_EMPTY': 1})

    def testOneCatalogCallPerMunicipalityAndVisibleProgress(self):
        other = self.root / 'town_1' / 'other_3'
        self.writeCsv(other, [{'PID': 'OTHER', 'Municipality': 'Town', 'Colony': 'Other'}])
        self.startUpdate()
        self.assertEqual(sum('GetULBColony' in call for call in self.engine.client.calls), 1)
        self.assertTrue(any(event == 'operationProgress' for event, payload in self.events))

    def testReportDirectoriesAreNotReadAsSourceDatasets(self):
        for prefix in ('Updated-mobileNos-', 'Mobile-updates-'):
            bad = self.root / (prefix + 'old') / 'town_1'
            bad.mkdir(parents=True)
            (bad / 'properties.csv').write_text('invalid')
        self.assertEqual(len(datasets.inspectDataset(self.root)['entries']), 1)

    def testFailedMobileRequestRetainsOriginalValuesAndProducesNoReportRows(self):
        jobId = self.startUpdate()
        self.engine.client.fail = True
        for attempt in range(30):
            job = self.store.job(jobId)
            if job['status'] in ('complete', 'complete_warnings'):
                break
            try:
                self.engine.advance(job)
            except ValueError as exception:
                self.engine.handleFailure(job, exception)
            pending = self.store.pendingProperty(jobId)
            if pending:
                with self.store.connect() as connection:
                    connection.execute('UPDATE properties SET retryAt=0 WHERE jobId=?', (jobId,))
        with self.store.connect() as connection:
            rows = {row['PID']: row for row in iterExportRows(connection, jobId)}
            reportRows = list(iterExportRows(connection, jobId, mobileReport=True))
        self.assertFalse(rows['EMPTY']['Mobile Numbers'])
        self.assertEqual(rows['EMPTY']['Address'], 'Original address')
        self.assertEqual(reportRows, [])

    def testReportCopiesOnlyRelevantExistingPhotos(self):
        with (self.folder / 'properties.csv').open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row['Photo 1'] = 'photos/old.jpg'
        (self.folder / 'photos').mkdir()
        (self.folder / 'photos' / 'old.jpg').write_bytes(b'original image')
        self.writeCsv(self.folder, rows)
        jobId = self.startUpdate()
        self.finish(jobId)
        exportJob(self.store, self.store.job(jobId))
        folder = Path(self.store.job(jobId)['mobileReportRoot']) / 'town_1' / 'colony_2'
        self.assertEqual((folder / 'photos' / 'old.jpg').read_bytes(), b'original image')

    def testUnchangedCheckpointsAreNotRewritten(self):
        jobId = self.startUpdate()
        self.store.persistDataset(jobId)
        with patch.object(datasets, 'upsert', wraps=datasets.upsert) as write:
            self.store.persistDataset(jobId)
        write.assert_not_called()

    def testConfirmationChecksFingerprintsWithoutParsingSnapshotsAgain(self):
        inventory = datasets.inspectDataset(self.root)
        with patch.object(datasets, 'readRows', side_effect=AssertionError('Unexpected second parse')):
            datasets.revalidateDataset(inventory)
        with (self.folder / 'properties.csv').open('a') as stream:
            stream.write('changed')
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            datasets.revalidateDataset(inventory)

    def testConfirmationRejectsAddedDatasetFolders(self):
        inventory = datasets.inspectDataset(self.root)
        self.writeCsv(self.root / 'town_1' / 'other_3', [{'PID': 'EXTRA'}])
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            datasets.revalidateDataset(inventory)

    def testOlderCombinedNamesAreCleanedWithoutNaOrDoublePrefixes(self):
        source = {'Owner 1: Name': 'RANJANA DUGGAL [Gender: F; Relation: NA; Guardian: NA] (NA)',
                  'Owner 2: Name': 'DEEP SHIKHA W/O PARVEEN KUMAR GARG (1/2)',
                  'Floor 1: Occupier Name': 'Ms. Existing'}
        row = compactExportRow(source, peopleCombined=True)
        self.assertEqual(row['Owner 1: Name'], 'Ms. RANJANA DUGGAL')
        self.assertEqual(row['Owner 2: Name'], 'Mrs. DEEP SHIKHA W/O PARVEEN KUMAR GARG (1/2)')
        self.assertEqual(row['Floor 1: Occupier Name'], 'Ms. Existing')
        self.assertEqual(personDescription('RANJANA', 'F', 'JASPREET', 'NA', '1/2'),
                         'Ms. RANJANA R/O JASPREET (1/2)')


if __name__ == '__main__':
    unittest.main()
