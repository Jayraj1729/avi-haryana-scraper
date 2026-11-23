'''Compact export presentation and durable fetch-time regression coverage.'''

# Standard Library Imports
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

# Third Party Imports
from openpyxl import load_workbook

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
        self.assertEqual(row['Mobile Number Linked With PID'], '9000000001')
        self.assertEqual(row['Owner 2: Mobile'], '9000000002')
        self.assertEqual(row['Owner 10: Mobile'], '')
        self.assertEqual(row['Floor 1: Occupier Mobile'], '9000000003')
        self.assertEqual(row['Floor 2: Occupier Mobile'], '')
        self.assertEqual(row['Mobile Numbers'], '9000000001, 9000000002, 9000000003, 9000000004')
        row['Data Fetched At (IST)'] = '-'
        row['Data Notes'] = 'metadata'
        columns = compactColumns(set(row), set(row))
        self.assertEqual(columns[-3:], ['Mobile Numbers', 'Data Fetched At (IST)', 'Data Notes'])

    def testInvalidMobilesAreRemovedFromEveryContactColumn(self):
        source = {'Mobile Number Linked With PID': '123456789',
                  'Owner 1: Mobile': '+91 90000-00001; 919000000002; 9000000003',
                  'Floor 1: Occupier Mobile': '9000000001, 00XXXXX001, 12345',
                  'Extra Phone': '9000000004',
                  'Mobile Numbers': '9000000003, 123456789012, +44 9000000005, -, NA'}
        row = compactExportRow(source)
        self.assertEqual(row['Mobile Number Linked With PID'], '')
        self.assertEqual(row['Owner 1: Mobile'], '9000000001, 9000000002, 9000000003')
        self.assertEqual(row['Floor 1: Occupier Mobile'], '')
        self.assertEqual(row['Mobile Numbers'], '9000000001, 9000000002, 9000000003, 9000000004')
        self.assertEqual(source['Mobile Number Linked With PID'], '123456789')
        self.assertEqual(compactExportRow(row, peopleCombined=True), row)
        # A ten-digit number starting with 91 is already a local number.
        self.assertEqual(compactExportRow({'Mobile Numbers': '9100000001'})['Mobile Numbers'], '9100000001')

    def testPlotAreaAndUnitAreSeparateForNewAndLegacyRows(self):
        for source, expected in [({'Plot Area': '2203.190 SqYard'}, ('2203.190', 'SqYard')),
                                 ({'Plot Area': '1,200.00 Sq. Ft.', 'Unit': 'SqYard'}, ('1,200.00', 'Sq. Ft.')),
                                 ({'Plot Area': '100', 'Unit': 'SqYard'}, ('100', 'SqYard')),
                                 ({'Plot Area': '-'}, ('', ''))]:
            with self.subTest(source=source):
                row = compactExportRow(source)
                self.assertEqual((row['Plot Area'], row['Plot Area Unit']), expected)
                self.assertNotIn('Unit', row)
                columns = compactColumns(set(row), set(row))
                self.assertEqual(columns[columns.index('Plot Area') + 1], 'Plot Area Unit')
        row = normalizeProperty({'PlotSize': '100.000', 'Unit': 'SqYard'},
                                {'Detail': {'Fields': {'Plot Area': '2203.190 SqMeter'}}})
        self.assertEqual((row['Plot Area'], row['Plot Area Unit']), ('2203.190', 'SqMeter'))

    def testRepeatedDigitMobilesAreRejectedAfterRemovingCountryCode(self):
        for digit in '0123456789':
            for prefix in ('', '91', '+91 '):
                with self.subTest(digit=digit, prefix=prefix):
                    value = prefix + digit * 10
                    row = compactExportRow({'Mobile Number Linked With PID': value,
                                            'Owner 1: Mobile': value, 'Floor 1: Occupier Mobile': value,
                                            'Mobile Numbers': value})
                    for label in ('Mobile Number Linked With PID', 'Owner 1: Mobile',
                                  'Floor 1: Occupier Mobile', 'Mobile Numbers'):
                        self.assertEqual(row[label], '')
        row = compactExportRow({'Mobile Numbers': '9999999999, 9999999998'})
        self.assertEqual(row['Mobile Numbers'], '9999999998')

    def testStateAndEveryFloorAreaUnitAreOrderedAndPreservePrecision(self):
        source = {'PID': 'A', 'State': 'Old value', 'Municipality': 'Town',
                  'Floor 12: Covered Area': '100.500 Sq.Ft',
                  'Floor 3: Covered Area': '2203.190 SqYard',
                  'Floor 3: Covered Area Unit': 'SqMeter',
                  'Floor 3: Carpet Area': '90.000', 'Floor 3: Carpet Area Unit': 'Sq.Ft'}
        row = compactExportRow(source)
        self.assertEqual(row['State'], 'Haryana')
        for label, area, unit in [('Floor 12: Covered Area', '100.500', 'Sq.Ft'),
                                  ('Floor 3: Covered Area', '2203.190', 'SqYard'),
                                  ('Floor 3: Carpet Area', '90.000', 'Sq.Ft')]:
            self.assertEqual(row[label], area)
            self.assertEqual(row[label + ' Unit'], unit)
            columns = compactColumns(set(row), set(row))
            self.assertEqual(columns[columns.index(label) + 1], label + ' Unit')
        self.assertEqual(columns[:3], ['PID', 'State', 'Municipality'])
        self.assertEqual(compactExportRow(row, peopleCombined=True), row)
        self.assertEqual(source['Floor 12: Covered Area'], '100.500 Sq.Ft')

    def testWorkbookPublishesSeparateAreaAndValidatedPhones(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = Store(home / 'state.sqlite3')
            jobId = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], home / 'output')[0]
            store.initializeTiles(jobId, 'unused', [(76, 30, 77, 31)])
            store.saveTile(jobId, store.pendingTile(jobId), [
                {'PID': 'A', 'pkPropertyId': 'A', 'PlotSize': '2203.190',
                 'Unit': 'SqYard', 'MobileNo': '+91 90000 00001'},
                {'PID': 'B', 'pkPropertyId': 'B', 'MobileNo': '123456789'}], lambda raw: [])
            with store.connect() as connection:
                identities = [record['identity'] for record in connection.execute('SELECT identity FROM properties')]
            for identity in identities:
                store.saveDetail(jobId, identity, {'Detail': {'FetchStatus': 'ok',
                    'ConstructionRows': [{'Covered Area': '100.500 Sq.Ft'}]}}, [])
                store.finishProperty(jobId, identity)
            workbookPath = exportJob(store, store.job(jobId))
            workbook = load_workbook(workbookPath, read_only=True)
            try:
                records = iter(workbook.active.values)
                columns = next(records)
                rows = {row['PID']: row for values in records if (row := dict(zip(columns, values)))}
                self.assertEqual(columns[:4], ('PID', 'State', 'Municipality', 'Colony'))
                self.assertEqual(rows['A']['State'], 'Haryana')
                self.assertEqual(columns[columns.index('Plot Area') + 1], 'Plot Area Unit')
                self.assertEqual(rows['A']['Plot Area'], '2203.190')
                self.assertEqual(rows['A']['Plot Area Unit'], 'SqYard')
                self.assertEqual(rows['A']['Mobile Numbers'], '9000000001')
                self.assertEqual(rows['B']['Mobile Numbers'], '-')
                self.assertEqual(rows['A']['Floor 1: Covered Area'], '100.500')
                self.assertEqual(rows['A']['Floor 1: Covered Area Unit'], 'Sq.Ft')
                self.assertEqual(columns[columns.index('Floor 1: Covered Area') + 1], 'Floor 1: Covered Area Unit')
            finally:
                workbook.close()

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
