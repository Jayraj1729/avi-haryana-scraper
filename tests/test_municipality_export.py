'''Municipality snapshot exports, retained folders, deduplication and safe replacement.'''

# Standard Library Imports
from concurrent.futures import Future, ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

# Third Party Imports
from openpyxl import load_workbook

from ulb_scraper import datasets
from ulb_scraper.engine import Engine
from ulb_scraper.municipality_export import exportMunicipality
from ulb_scraper.store import Store


class MunicipalityExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / 'output'
        self.store = Store(self.home / 'state.sqlite3')
        self.first = self.addColony('1', 'First')
        self.second = self.addColony('2', 'Second')

    def tearDown(self):
        self.temporary.cleanup()

    def addColony(self, colonyId, name, municipalityId='60', municipalityName='PANCHKULA'):
        return self.store.addJobs([({'id': municipalityId, 'name': municipalityName},
                                   {'id': colonyId, 'name': name})], self.root)[0]

    def addProperty(self, jobId, pid, stamp='2026-11-01T12:00:00+05:30', address='Saved address', failed=False):
        raw = {'PID': pid, 'pkPropertyId': pid, 'Address1': address, 'MobileNo': '+91 90000 00001'}
        detail = {'Detail': {'ParseVersion': 2, 'FetchStatus': 'error' if failed else 'ok'}}
        with self.store.connect() as connection:
            connection.execute('''INSERT OR REPLACE INTO properties
                (jobId,identity,pid,raw,detail,detailState,processingState,fetchedAt)
                VALUES (?,?,?,?,?,?,?,?)''', (jobId, pid, pid, json.dumps(raw), json.dumps(detail),
                    'error' if failed else 'ok', 'error' if failed else 'complete', stamp))
            connection.execute('UPDATE jobs SET dataVersion=dataVersion+1 WHERE id=?', (jobId,))

    def queued(self):
        return [job for job in self.store.jobs() if job['municipalityId'] == '60']

    def readWorkbook(self, result):
        workbook = load_workbook(result['path'])
        self.addCleanup(workbook.close)
        rows = []
        for sheet in workbook.worksheets:
            if not sheet.title.startswith('Properties'):
                continue
            values = iter(sheet.values)
            headers = next(values)
            self.assertEqual(headers[:4], ('PID', 'State', 'Municipality', 'Colony'))
            rows.extend(dict(zip(headers, row)) for row in values)
        return workbook, {row['PID']: row for row in rows}

    def testIncludesRetainedCheckpointAndLegacyColonyWithLatestSuccessfulPid(self):
        self.addProperty(self.first, 'DUP', address='Old address')
        self.addProperty(self.second, 'DUP', '2026-11-02T12:00:00+05:30', 'New address')
        self.addProperty(self.second, 'RETAINED')
        self.store.persistDataset(self.second)
        folder = Path(self.store.job(self.second)['folder'])
        checkpoint = folder / datasets.CHECKPOINT
        before = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        self.store.deleteJobs([self.second])
        legacy = folder.parent / 'legacy_3'
        legacy.mkdir()
        with (legacy / 'properties.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, ['PID', 'Colony', 'Address', 'Data Fetched At (IST)'])
            writer.writeheader()
            writer.writerows([{'PID': 'LEGACY', 'Colony': 'Legacy', 'Address': 'Legacy address'},
                              {'PID': 'DUP', 'Colony': 'Legacy', 'Address': 'Unknown-date address'}])
        progress = []
        result = exportMunicipality(self.store, self.queued(), self.root, lambda *args: progress.append(args))
        workbook, rows = self.readWorkbook(result)
        self.assertEqual(set(rows), {'DUP', 'RETAINED', 'LEGACY'})
        self.assertTrue(all(row['State'] == 'Haryana' for row in rows.values()))
        self.assertEqual(rows['DUP']['Address'], 'New address')
        self.assertEqual(set(rows['DUP']['Colony'].split(', ')), {'First', 'Second', 'Legacy'})
        self.assertEqual(result['rows'], 3)
        self.assertEqual(result['colonies'], 3)
        self.assertIn('Export details', workbook.sheetnames)
        self.assertTrue(result['exportedAt'].endswith('+05:30'))
        self.assertEqual(hashlib.sha256(checkpoint.read_bytes()).hexdigest(), before)
        self.assertEqual(progress[-1], (1, 1, 'Municipality export complete'))

    def testNewFailureDoesNotReplaceSuccessfulDuplicate(self):
        self.addProperty(self.first, 'DUP', address='Good address')
        self.addProperty(self.second, 'DUP', '2026-11-22T12:00:00+05:30', 'Failed address', failed=True)
        unusedWorkbook, rows = self.readWorkbook(exportMunicipality(self.store, self.queued(), self.root))
        self.assertEqual(rows['DUP']['Address'], 'Good address')

    def testEmptyMunicipalityProducesAValidWorkbookAndZeroRows(self):
        result = exportMunicipality(self.store, self.queued(), self.root)
        workbook, rows = self.readWorkbook(result)
        self.assertEqual(result['rows'], 0)
        self.assertEqual(rows, {})
        self.assertEqual(workbook.sheetnames, ['Properties', 'Export details'])

    def testMultipleRootsAreCombinedIntoTheChosenMasterFolder(self):
        self.addProperty(self.first, 'FIRST')
        otherRoot = self.home / 'other-output'
        other = self.store.addJobs([({'id': '60', 'name': 'PANCHKULA'}, {'id': '3', 'name': 'Third'})], otherRoot)[0]
        self.addProperty(other, 'THIRD')
        result = exportMunicipality(self.store, self.queued(), self.root)
        unusedWorkbook, rows = self.readWorkbook(result)
        self.assertEqual(set(rows), {'FIRST', 'THIRD'})
        self.assertEqual(Path(result['path']).parent, (self.root / 'Master').resolve())

    def testPhotoLinksPointToExistingFilesAndRepeatedExportReplacesPrevious(self):
        self.addProperty(self.first, 'A')
        folder = Path(self.store.job(self.first)['folder'])
        photo = folder / 'photos' / 'test photo.jpg'
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b'unchanged photo')
        with self.store.connect() as connection:
            connection.execute("INSERT INTO photos(jobId,identity,url,path,state) VALUES (?,?,?,?, 'complete')",
                               (self.first, 'A', 'https://example.invalid/photo.jpg', 'photos/test photo.jpg'))
        firstResult = exportMunicipality(self.store, self.queued(), self.root)
        workbook, rows = self.readWorkbook(firstResult)
        sheet = workbook['Properties']
        headers = [cell.value for cell in sheet[1]]
        cell = sheet.cell(2, headers.index('Photo 1') + 1)
        self.assertEqual(cell.hyperlink.target, '../panchkula_60/first_1/photos/test%20photo.jpg')
        self.assertEqual((Path(firstResult['path']).parent / rows['A']['Photo 1']).resolve(), photo)
        workbook.close()
        self.addProperty(self.first, 'B')
        secondResult = exportMunicipality(self.store, self.queued(), self.root)
        self.assertNotEqual(firstResult['path'], secondResult['path'])
        self.assertFalse(Path(firstResult['path']).exists())
        self.assertEqual([path.resolve() for path in (self.root / 'Master').glob('*.xlsx')], [Path(secondResult['path'])])
        self.assertEqual(secondResult['rows'], 2)
        self.assertEqual(photo.read_bytes(), b'unchanged photo')

    def testPublicationFailureRetainsPreviousWorkbookAndTimestamp(self):
        self.addProperty(self.first, 'A')
        result = exportMunicipality(self.store, self.queued(), self.root)
        previous = Path(result['path'])
        oldContent = previous.read_bytes()
        marker = previous.parent / '.municipality-60.json'
        oldMetadata = marker.read_bytes()
        realReplace = Path.replace

        def failMarker(path, destination):
            if Path(destination) == marker:
                raise PermissionError('Simulated locked metadata')
            return realReplace(path, destination)

        with patch.object(Path, 'replace', failMarker), self.assertRaises(PermissionError):
            exportMunicipality(self.store, self.queued(), self.root)
        self.assertEqual(previous.read_bytes(), oldContent)
        self.assertEqual(marker.read_bytes(), oldMetadata)
        self.assertEqual(list(previous.parent.glob('*.xlsx')), [previous])

    def testLargeExportSplitsPropertySheetsAndKeepsOneSummary(self):
        for index in range(5):
            self.addProperty(self.first, str(index))
        with patch('ulb_scraper.municipality_export.SHEETROWS', 2):
            result = exportMunicipality(self.store, self.queued(), self.root)
        workbook, rows = self.readWorkbook(result)
        self.assertEqual(workbook.sheetnames, ['Properties', 'Properties 2', 'Properties 3', 'Export details'])
        self.assertEqual(len(rows), 5)

    def testMasterAndExportedFoldersAreIgnoredByBothImportChecks(self):
        self.addProperty(self.first, 'A')
        self.store.persistDataset(self.first)
        inventory = datasets.inspectDataset(self.root)
        for name in ('Master', 'Exported'):
            invalid = self.root / name / 'invalid_99'
            invalid.mkdir(parents=True)
            (invalid / 'properties.csv').write_text('invalid')
        self.assertEqual(len(datasets.inspectDataset(self.root)['entries']), 1)
        datasets.revalidateDataset(inventory)

    def testBackgroundExportWaitsForScopedWorkAndLeavesOtherMunicipalityRunning(self):
        self.addProperty(self.first, 'A')
        other = self.addColony('3', 'Other', '36', 'JIND')
        self.store.control([self.first, other], 'start')
        events = []
        engine = Engine(self.store, self.home, lambda *args: events.append(args))
        engine.inFlight[self.first] = Future()
        engine.beginMunicipalityExport('60')
        self.assertFalse(self.store.job(self.first)['desired'])
        self.assertTrue(self.store.job(other)['desired'])
        engine.collectMunicipalityExport()
        self.assertIsNone(engine.municipalityExport['future'])
        del engine.inFlight[self.first]
        started = threading.Event()
        release = threading.Event()
        realExport = exportMunicipality

        def slowExport(*args):
            started.set()
            release.wait(5)
            return realExport(*args)

        with ThreadPoolExecutor(max_workers=1) as writer, patch('ulb_scraper.engine.exportMunicipality', slowExport):
            engine.masterWriter = writer
            engine.collectMunicipalityExport()
            self.assertTrue(started.wait(2))
            self.assertIsNotNone(engine.municipalityExport)
            self.assertTrue(self.store.job(other)['desired'])
            release.set()
            engine.collectMunicipalityExport(wait=True)
        result = next(payload for event, payload in events if event == 'municipalityExported')
        self.assertEqual(result['rows'], 1)
        self.assertIsNone(engine.municipalityExport)
        self.assertEqual(self.store.setting('municipalityExports')['60']['path'], result['path'])
        self.assertFalse(self.store.job(self.first)['desired'])
