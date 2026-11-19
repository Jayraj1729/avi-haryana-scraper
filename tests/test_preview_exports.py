'''Preview upgrades must not modify source checkpoints or existing exports.'''

# Standard Library Imports
import csv
import json
from pathlib import Path
import tempfile
import unittest

from tools.preview_exports import previewExports
from ulb_scraper.store import Store


class PreviewExportTests(unittest.TestCase):
    def testPreviewClonesCheckpointAndPhotosWithoutModifyingSource(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = Store(home / 'state.sqlite3')
            jobId = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], home / 'original')[0]
            originalFolder = Path(store.job(jobId)['folder'])
            (originalFolder / 'photos').mkdir(parents=True)
            (originalFolder / 'photos' / 'sample.jpg').write_bytes(b'photo fixture')
            (originalFolder / 'properties.xlsx').write_bytes(b'existing workbook')
            with store.connect() as connection:
                connection.execute("DELETE FROM settings WHERE key='propertyParserVersion'")
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                    VALUES (?, 'restricted', ?, ?, 'error', 'error')''',
                    (jobId, json.dumps({'PID': 'SAMPLE01', 'IsDataVerified2023': True}),
                     json.dumps({'Detail': {'FetchStatus': 'error'}})))
                connection.execute("INSERT INTO photos VALUES (?, 'restricted', 'https://example.test/photo.jpg', 'photos/sample.jpg', 'ok', '')", (jobId,))
            results = previewExports(store.path, home / 'preview')
            self.assertFalse((originalFolder / '.ulb-checkpoint.sqlite3').exists())
            self.assertFalse((home / 'original' / '.ulb-dataset.json').exists())
            self.assertEqual(len(results), 1)
            with results[0].with_suffix('.csv').open(encoding='utf-8-sig') as fileObj:
                row = next(csv.DictReader(fileObj))
            self.assertNotIn('Property Availability', row)
            self.assertNotIn('Processing Status', row)
            self.assertEqual(row['Data Fetched At (IST)'], '-')
            self.assertEqual((results[0].parent / row['Photo 1']).read_bytes(), b'photo fixture')
            self.assertEqual((originalFolder / 'properties.xlsx').read_bytes(), b'existing workbook')
            with store.connect() as connection:
                self.assertEqual(connection.execute('SELECT detailState FROM properties').fetchone()[0], 'error')
                self.assertIsNone(connection.execute("SELECT value FROM settings WHERE key='propertyParserVersion'").fetchone())
            with self.assertRaises(FileExistsError):
                previewExports(store.path, home / 'preview')


if __name__ == '__main__':
    unittest.main()
