'''Compact export presentation and durable fetch-time regression coverage.'''

# Standard Library Imports
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from ulb_scraper.exporter import exportJob, formatFetchedAt, iterExportRows
from ulb_scraper.property_data import compactColumns, compactExportRow, normalizeProperty, personDescription
from ulb_scraper.store import Store


class CompactExportTests(unittest.TestCase):
    def testRelationshipsHavePrefixesAndMissingValuesAreOmitted(self):
        for gender, relation, abbreviation in [('Female', 'Husband', 'W/O'),
                                               ('Male', 'Wife', 'H/O'),
                                               ('Male', 'Father', 'S/O'),
                                               ('Female', 'Father', 'D/O'),
                                               ('Female', 'Mother', 'D/O'),
                                               ('', 'Guardian', 'C/O')]:
            with self.subTest(gender=gender, relation=relation):
                self.assertEqual(personDescription('Person', gender, 'Guardian', relation, '1/2'),
                                 f"{('Mrs. ' if abbreviation == 'W/O' else 'Mr. ' if abbreviation in ('S/O', 'H/O') else 'Ms. ' if gender == 'Female' else '')}Person {abbreviation} Guardian (1/2)")
        self.assertEqual(personDescription('Person', 'Female', share=''),
                         'Ms. Person')
        self.assertEqual(personDescription('Person', 'Male', relation='Father', share=''), 'Mr. Person')
        self.assertEqual(personDescription('Person', '', 'Guardian', 'Father'), 'Person R/O Guardian')
        self.assertEqual(personDescription('', share=''), '')
        self.assertTrue(personDescription('', share='1/2').endswith('(1/2)'))

    def testAllOwnersAndFloorsAreCompactedAndBackendDataIsPreserved(self):
        source = {'Owner Names': 'Redundant name', 'Owner 12: Name': 'Person',
                  'Owner 12: Gender': 'Female', 'Owner 12: Father / Husband Name': 'Partner',
                  'Owner 12: Relation': 'Husband', 'Owner 12: Share': '1/2',
                  'Owner 12: GST Number': 'excluded', 'Owner 12: Authenticated Status': 'Pending',
                  'Floor 15: Occupier Name': 'Tenant', 'Floor 15: Occupier Gender': 'Male',
                  'Floor 15: Occupier Guardian Name': 'Parent', 'Floor 15: Occupier Relation Type': 'Father',
                  'Floor 15: Rebate': 'No', 'Floor 15: Covered Area': '100',
                  'Processing Status': 'error', 'Processing Error': 'backend error',
                  'Due Details: Ptax Net Dues': '50',
                  'OutStanding Dues Details(In Rupees) 3, Row 1: Total Dues': '10',
                  'Property Record ID': '123', 'Property Card No./Membership No': 'deleted',
                  'Unfamiliar Useful Field': 'retained'}
        row = compactExportRow(source)
        self.assertEqual(row['Owner 12: Name'], 'Mrs. Person W/O Partner (1/2)')
        self.assertEqual(row['Floor 15: Occupier Name'], 'Mr. Tenant S/O Parent')
        self.assertEqual(row['Floor 15: Covered Area'], '100')
        self.assertEqual(row['Unfamiliar Useful Field'], 'retained')
        for deleted in ('Owner Names', 'Owner 12: Share', 'Owner 12: Gender', 'Owner 12: GST Number',
                        'Owner 12: Authenticated Status', 'Floor 15: Occupier Guardian Name',
                        'Floor 15: Rebate', 'Processing Status', 'Processing Error',
                        'Due Details: Ptax Net Dues', 'Property Record ID', 'Property Card No./Membership No'):
            self.assertNotIn(deleted, row)
            self.assertIn(deleted, source)
        self.assertFalse(any(label.startswith('OutStanding') for label in row))

    def testMapOnlyOwnerFallbackDoesNotInventSeparateOwners(self):
        row = compactExportRow({'Owner Names': 'FIRST, SECOND', 'Property Availability': 'Not available for public view'})
        self.assertTrue(row['Owner 1: Name'].startswith('FIRST, SECOND'))
        self.assertNotIn('Owner 2: Name', row)
        self.assertNotIn('Owner Names', row)

    def testMobileDeduplicationFollowsExportOrderAndKeepsUnion(self):
        source = {'Mobile Number Linked With PID': '+91 90000 00001',
                  'Owner 10: Mobile': '9000000002', 'Owner 2: Mobile': '9000000001, 9000000002',
                  'Floor 1: Occupier Mobile': '9000000002;9000000003',
                  'Floor 2: Occupier Mobile': '9000000003',
                  'Mobile Numbers': '9000000001, 9000000002, 9000000003, 9000000004'}
        row = compactExportRow(source)
        self.assertEqual(row['Mobile Number Linked With PID'], '+91 90000 00001')
        self.assertEqual(row['Owner 2: Mobile'], '9000000002')
        self.assertEqual(row['Owner 10: Mobile'], '')
        self.assertEqual(row['Floor 1: Occupier Mobile'], '9000000003')
        self.assertEqual(row['Floor 2: Occupier Mobile'], '')
        self.assertEqual(row['Mobile Numbers'], '+91 90000 00001, 9000000002, 9000000003, 9000000004')
        row['Data Fetched At (IST)'] = '-'
        row['Data Notes'] = 'metadata'
        columns = compactColumns(set(row), set(row))
        self.assertEqual(columns[-3:], ['Mobile Numbers', 'Data Fetched At (IST)', 'Data Notes'])

    def testFetchTimePersistsAndExportDoesNotAdvanceIt(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = Store(home / 'state.sqlite3')
            jobId = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], home / 'output')[0]
            store.initializeTiles(jobId, 'unused', [(76, 30, 77, 31)])
            store.saveTile(jobId, store.pendingTile(jobId), [{'pkPropertyId': 'record', 'OwnerName': 'Map Owner'}], lambda raw: [])
            with store.connect() as connection:
                firstFetch = connection.execute('SELECT fetchedAt FROM properties').fetchone()[0]
                self.assertIsNotNone(datetime.fromisoformat(firstFetch).tzinfo)
                connection.execute("UPDATE properties SET fetchedAt='2000-01-01T00:00:00+00:00'")
            store.saveDetail(jobId, 'record', {'Detail': {'FetchStatus': 'ok'}}, [])
            store.finishProperty(jobId, 'record')
            with store.connect() as connection:
                timestamp = next(iterExportRows(connection, jobId))['Data Fetched At (IST)']
                self.assertNotEqual(timestamp, '2000-01-01T05:30:00+05:30')
            exportJob(store, store.job(jobId))
            with store.connect() as connection:
                savedTimestamp = connection.execute('SELECT fetchedAt FROM properties').fetchone()[0]
                self.assertEqual(datetime.fromisoformat(savedTimestamp), datetime.fromisoformat(timestamp))
                self.assertTrue(timestamp.endswith('+05:30'))
                connection.execute('''INSERT INTO properties(jobId,identity,raw,processingState)
                    VALUES (?, 'legacy', ?, 'complete')''', (jobId, json.dumps({'PID': 'OLD'})))
                rows = list(iterExportRows(connection, jobId))
                self.assertEqual(next(row for row in rows if row['PID'] == 'OLD')['Data Fetched At (IST)'], '-')

    def testMobileAliasesAreConsumedEvenWhenHiddenFullNumberWins(self):
        aliases = ['Mobile Number linked with the PID', 'Mobile Number Linked With PID', 'Mobile No']
        parsed = {'Detail': {'MobileNoFull': '9000000001',
                             'MobileNumberLinkedWithThePID': '00XXXXX001',
                             'Fields': {alias: '00XXXXX001' for alias in aliases}}}
        normalized = normalizeProperty({'MobileNo': '9000000002'}, parsed)
        self.assertEqual(normalized['Mobile Number Linked With PID'], '9000000001')
        for label in aliases:
            if label != 'Mobile Number Linked With PID':
                self.assertNotIn(label, normalized)
        row = compactExportRow(normalized)
        self.assertIn('9000000002', row['Mobile Numbers'])
        self.assertEqual([label for label in row if 'linked' in label.lower() and 'mobile' in label.lower()],
                         ['Mobile Number Linked With PID'])

    def testLegacyAliasesDisappearWithoutLosingDistinctNumbers(self):
        row = compactExportRow({'Mobile Number Linked With PID': '', 'Mobile No': '9000000001',
                                'Mobile Number Linked With The PID': '9000000002'})
        self.assertEqual(row['Mobile Number Linked With PID'], '9000000001')
        self.assertEqual(row['Mobile Numbers'], '9000000001, 9000000002')
        self.assertNotIn('Mobile No', row)
        self.assertNotIn('Mobile Number Linked With The PID', row)

    def testIstConversionPreservesInstantAndDoesNotInventUnknownTimes(self):
        self.assertEqual(formatFetchedAt('2026-11-02T21:00:00+00:00'), '2026-11-03T02:30:00+05:30')
        self.assertEqual(formatFetchedAt('2026-11-03T02:30:00+05:30'), '2026-11-03T02:30:00+05:30')
        self.assertEqual(formatFetchedAt(None), '-')
        with self.assertLogs(level='WARNING'):
            self.assertEqual(formatFetchedAt('2026-11-03T02:30:00'), '-')
