'''Dynamic property schemas, published-value privacy, and checkpoint upgrades.'''

# Standard Library Imports
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

# Third Party Imports
from openpyxl import load_workbook

from scraper import AuthenticationError, collectTables
from ulb_scraper.engine import Engine
from ulb_scraper.exporter import exportJob, iterExportRows
from ulb_scraper.portal import PortalClient
from ulb_scraper.property_data import (inferPlotNumber, metadataColumn, normalizeProperty,
                                     orderedColumns, parsePropertyPage,
                                     publicViewRestricted, responseSchema,
                                     restrictedPage)
from ulb_scraper.store import Store


FIXTUREDIR = Path(__file__).resolve().parents[1] / 'refrence'


def samplePage(ownerCount=2, floorCount=3, category='Commercial', subtype='Shop'):
    pairs = ''.join(f'<div><span class="font-weight-bold en">{label}:</span>'
                    f'<span class="hi">Translation</span><span class="det">{value}</span></div>'
                    for label, value in [('Municipality', 'Sample town'), ('PID', 'SAMPLE01'),
                                         ('Property Category', category), ('Property Type', 'Building'),
                                         ('Property SubType', subtype), ('Plot No. / House No', 'H-3'),
                                         ('Address', 'H-3, Sample colony'), ('Plot Area', '100 SqYard'),
                                         ('Mobile Number linked with the PID', '00XXXXX000'),
                                         ('Ownership Type', 'Partnership'), ('New Property Field', 'New value')])
    owners = ''.join(f'<tr><td>{index}</td><td>Owner {index}</td><td>Female</td><td>Guardian {index}</td>'
                     f'<td>Father</td><td>00XXXXX{index:03d}</td><td>Pending</td><td>1/{ownerCount}</td></tr>'
                     for index in range(1, ownerCount + 1))
    floors = ''.join(f'<tr><td>{index}</td><td>Floor {index}</td><td>Rented</td><td>100 Sq.Ft</td>'
                     f'<td>No Rebate</td><td>Tenant {index}</td><td>00XXXXX{index:03d}</td></tr>'
                     for index in range(1, floorCount + 1))
    ownerDetailJson = json.dumps([{'OwnerName': f'Owner {index}', 'MobileNo': f'90000000{index:02d}',
                                    'AadharNo': '000000000000'} for index in range(1, ownerCount + 1)])
    return (pairs + '<span class="font-weight-bold en">Authorized Status: <b>Authorized</b></span>'
            '<input type="hidden" id="MobileNo" value="9000000000">'
            f'<script>var ownerDetail={ownerDetailJson};</script>'
            '<table><thead><tr><th>Sr.No</th><th>Owner Name</th><th>Gender</th><th>Father/Husband Name</th>'
            '<th>Relation</th><th>Mobile No</th><th>Authenticated Status</th><th>Bhag</th></tr></thead>'
            f'<tbody>{owners}</tbody></table><table><thead><tr><th colspan="7">Construction Details</th></tr>'
            '<tr><th rowspan="2">Sr.No</th><th rowspan="2">FLOOR</th><th rowspan="2">Occupancy Type</th>'
            '<th rowspan="2">COVERED AREA</th><th rowspan="2">REBATE</th><th colspan="2">Occupier Details</th></tr>'
            '<tr><th>Occupier Name</th><th>Occupier Mobile</th></tr></thead>'
            f'<tbody>{floors}</tbody></table>')


class PropertyDataTests(unittest.TestCase):
    def testRepeatableOwnersAndFloorsHaveNoFixedTwelveRowLimit(self):
        detail = parsePropertyPage(samplePage(ownerCount=15, floorCount=17))
        row = normalizeProperty({'PropertyCategory': 'Residential'}, detail)
        self.assertEqual(row['Owner Count'], 15)
        self.assertEqual(row['Construction Row Count'], 17)
        self.assertEqual(row['Owner 15: Name'], 'Owner 15')
        self.assertEqual(row['Floor 17: Occupier Name'], 'Tenant 17')
        self.assertEqual(row['Floor 1: Covered Area'], '100 Sq.Ft')
        self.assertEqual(row['Property Category'], 'Commercial')
        self.assertEqual(row['Property Subtype'], 'Shop')
        self.assertIn('page=Commercial; map=Residential', row['Data Notes'])
        self.assertEqual(row['Authorized Status'], 'Authorized')
        self.assertEqual(row['New Property Field'], 'New value')
        self.assertFalse(any(label.startswith('Detail.') for label in row))

    def testPDFExampleShapesCanUseDifferentOwnerAndFloorCounts(self):
        for owners, floors in [(2, 2), (2, 3), (1, 3)]:
            with self.subTest(owners=owners, floors=floors):
                row = normalizeProperty({}, parsePropertyPage(samplePage(owners, floors)))
                self.assertEqual(row['Owner Count'], owners)
                self.assertEqual(row['Construction Row Count'], floors)

    def testFullMobilesExtractedFromHiddenInputAndOwnerDetailScript(self):
        detail = parsePropertyPage(samplePage())
        self.assertEqual(detail['Detail']['MobileNoFull'], '9000000000')
        self.assertEqual(detail['Detail']['OwnerMobileNos'], ['9000000001', '9000000002'])
        row = normalizeProperty({'MobileNo': '0000000000'}, detail)
        self.assertEqual(row['Mobile Number Linked With PID'], '9000000000')
        self.assertEqual(row['Owner 1: Mobile'], '9000000001')
        self.assertEqual(row['Owner 2: Mobile'], '9000000002')
        self.assertIn('Floor 1: Occupier Mobile', row)
        self.assertEqual(row['Floor 1: Occupier Mobile'], '00XXXXX001')
        self.assertFalse(any('(masked)' in label.lower() for label in row))
        self.assertIn('9000000000', row['Mobile Numbers'])
        self.assertIn('9000000001', row['Mobile Numbers'])
        legacy = {'Detail': {'ParseVersion': 2,
                              'OwnerRows': [{'Name': 'Owner', 'Mobile (Masked)': '00XXXXX000'}],
                              'ConstructionRows': [{'Floor': 'Ground', 'Occupier Mobile (Masked)': '00XXXXX001'}]}}
        row = normalizeProperty({}, legacy)
        self.assertEqual(row['Owner 1: Mobile'], '00XXXXX000')
        self.assertEqual(row['Floor 1: Occupier Mobile'], '00XXXXX001')
        self.assertFalse(any('(masked)' in label.lower() for label in row))

    def testAuthenticationStatusExcludesHiddenTextAndFormInstructions(self):
        instruction = 'Certify that Family id (Parivar Pehchan Patra) and Aadhar Number Not Available.'
        statuses = [
            ('Aadhar', 'Aadhar'), ('Pending', 'Pending'), ('Family ID', 'Family ID'),
            (f'<span>Aadhar</span><div style="DISPLAY : none !important">{instruction}</div>', 'Aadhar'),
            ('<span>Pending</span><span class="d-none">Aadhar</span>', 'Pending'),
            ('<span>Aadhar</span><span style="visibility: hidden">Pending</span>', 'Aadhar'),
            (f'<span>Pending</span><label><input type="checkbox">{instruction}</label>', 'Pending'),
            (f'<span>{instruction}</span>', ''),
            (f'<span>Aadhar {instruction}</span>', 'Aadhar'),
        ]
        for markup, expected in statuses:
            with self.subTest(markup=markup):
                html = samplePage(ownerCount=1).replace('<td>Pending</td>', f'<td>{markup}</td>')
                detail = parsePropertyPage(html)
                self.assertEqual(detail['Detail']['OwnerRows'][0]['Authenticated Status'], expected)
                row = normalizeProperty({}, detail)
                self.assertEqual(row['Owner 1: Authenticated Status'], expected)
        legacy = {'Detail': {'ParseVersion': 2, 'OwnerRows': [{'Authenticated Status': instruction}]}}
        self.assertEqual(normalizeProperty({}, legacy)['Owner 1: Authenticated Status'], '')

    def testWizardStepsAreExcludedFromFreshAndExistingCheckpoints(self):
        controls = ''.join(f'<div><span class="font-weight-bold en">{label}:</span><span class="det">2</span></div>'
                           for label in ['Step1', 'Step2', 'Step 1', 'Step 2'])
        detail = parsePropertyPage(samplePage() + controls)
        self.assertFalse(any(label.lower().startswith('step') for label in detail['Detail']['Fields']))
        detail['Detail']['Fields']['Step 1'] = '2'
        detail['Detail']['Step1'] = '2'
        row = normalizeProperty({'Step2': 3, 'Step 2': 3, 'Step Rate': 'real value'}, detail)
        for label in ['Step1', 'Step2', 'Step 1', 'Step 2']:
            self.assertNotIn(label, row)
        self.assertEqual(row['Step Rate'], 'real value')

    def testFullMobilesExtractedButIdentityNumbersStillExcluded(self):
        detail = parsePropertyPage(samplePage())
        serialized = json.dumps(detail)
        self.assertNotIn('AadharNo', serialized)
        self.assertNotIn('InputValues', detail['Detail'])
        self.assertEqual(detail['Detail']['MobileNoFull'], '9000000000')
        self.assertEqual(detail['Detail']['OwnerMobileNos'], ['9000000001', '9000000002'])
        row = normalizeProperty({'MobileNo': '0000000000', 'AadharNo': '000000000000'}, detail)
        self.assertNotIn('AadharNo', json.dumps(row))
        self.assertIn('9000000000', row['Mobile Numbers'])
        self.assertIn('9000000001', row['Mobile Numbers'])

    def testAddressFallbackAndExplicitPlotPriority(self):
        for address, expected in [('H -3, Colony', 'H-3'), ('J -7, Colony', 'J-7'),
                                  ('32, Colony', '32'), ('C16p, Colony', 'C16p'),
                                  ('H NO C-5, Colony', 'C-5'), ('G -8, Colony', 'G-8')]:
            with self.subTest(address=address):
                row = normalizeProperty({'Address1': address}, {'Detail': {'Address': '-'}})
                self.assertEqual(row['Address'], address)
                self.assertEqual(row['Plot / House Number'], expected)
                self.assertEqual(row['Plot / House Number Source'], 'Address (inferred)')
                explicit = normalizeProperty({'Address1': address, 'PlotNo': '99'})
                self.assertEqual(explicit['Plot / House Number'], '99')
        row = normalizeProperty({'Address1': '-'}, {'Detail': {'Address': 'C-8, Colony', 'PlotNoHouseNo': '55'}})
        self.assertEqual(row['Address'], 'C-8, Colony')
        self.assertEqual(row['Plot / House Number'], '55')
        self.assertFalse(inferPlotNumber('Sector 6, Main road'))
        self.assertFalse(inferPlotNumber('Near school, Main road'))
        self.assertFalse(inferPlotNumber('A very long address without an identifier'))

    def testContactUnionRemovesDuplicates(self):
        row = normalizeProperty({'MobileNo': '00XXXXX001, 00XXXXX002'},
                                parsePropertyPage(samplePage(2, 2)))
        values = row['Mobile Numbers'].split(', ')
        self.assertEqual(len(values), len(set(values)))
        self.assertIn('9000000000', values)
        self.assertIn('9000000001', values)
        self.assertIn('9000000002', values)
        row = normalizeProperty({'MobileNo': '33XXXXX333'}, parsePropertyPage(samplePage()))
        self.assertIn('33XXXXX333', row['Mobile Numbers'])

    def testEmptyTableFieldsAreRetainedInTheDynamicSchema(self):
        html = samplePage().replace('<td>Female</td>', '<td></td>').replace('<td>No Rebate</td>', '<td></td>')
        row = normalizeProperty({}, parsePropertyPage(html))
        self.assertIn('Owner 1: Gender', row)
        self.assertFalse(row['Owner 1: Gender'])
        self.assertIn('Floor 1: Rebate', row)
        self.assertFalse(row['Floor 1: Rebate'])

    def testUnfamiliarNestedFieldsStayStructuredAndPrivateValuesAreExcluded(self):
        row = normalizeProperty({'Additional': [{'New Value': 'first'}, {'New Value': 'second'}],
                                 'ExtrasJson': '{"Amount":3,"AadharNo":"000000000000","MobileNo":"0000000000"}',
                                 'Phones': ['0000000000', '1111111111']})
        self.assertEqual(row['Additional 1: New Value'], 'first')
        self.assertEqual(row['Additional 2: New Value'], 'second')
        self.assertEqual(row['Extras Json: Amount'], 3)
        self.assertIn('0000000000', row['Extras Json: Mobile No'])
        self.assertIn('0000000000', row['Phones'])
        self.assertNotIn('Aadhar', json.dumps(row))

    def testColumnOrderGroupsNumericallyAndLeavesEmptyExtrasAtEnd(self):
        available = {'PID', 'Municipality', 'Colony', 'Empty Extra', 'Useful Extra',
                     'Owner 10: Name', 'Owner 2: Name', 'Owner 1: Gender', 'Owner 1: Name', 'Floor 1: Floor'}
        columns = orderedColumns(available, {'Useful Extra'})
        self.assertEqual(columns[:3], ['PID', 'Municipality', 'Colony'])
        self.assertLess(columns.index('Owner 1: Name'), columns.index('Owner 1: Gender'))
        self.assertLess(columns.index('Owner 2: Name'), columns.index('Owner 10: Name'))
        self.assertLess(columns.index('Owner 10: Name'), columns.index('Floor 1: Floor'))
        self.assertEqual(columns[-1], 'Empty Extra')

    def testMetadataIsAlwaysAfterAllPropertyFieldsIncludingEmptyOnes(self):
        propertyFields = {'PID', 'Municipality', 'Colony', 'Owner 1: Name',
                          'Floor 1: Floor', 'Address', 'Empty Extra', 'Photos', 'Photo 1'}
        trackingFields = {'Processing Status', 'Processing Error', 'Integrated Count',
                          'Integrated Count CMCDMC', 'Photo Download Status', 'Plot / House Number Source',
                          'Data Notes', 'Owner Count', 'Construction Row Count', 'Property Record ID',
                          'fk Property Sub Type Id', 'Due Details: pk Due Id'}
        columns = orderedColumns(propertyFields | trackingFields | {'Step1', 'Step 2'},
                                 (propertyFields | trackingFields) - {'Empty Extra'})
        self.assertTrue(all(metadataColumn(label) for label in trackingFields))
        self.assertLess(max(columns.index(label) for label in propertyFields),
                        min(columns.index(label) for label in trackingFields))
        self.assertNotIn('Step1', columns)
        self.assertNotIn('Step 2', columns)
        self.assertEqual(columns[:3], ['PID', 'Municipality', 'Colony'])

    def testSessionExpiredDocumentRequiresRenewalRatherThanParserRetries(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = PortalClient(cookieFile=Path(temporary) / 'cookies')
            with patch.object(client, 'requestText', return_value='<html><title>Session Expired</title></html>'):
                with self.assertRaises(AuthenticationError):
                    client.fetchDetails({'Token': 'sample'})
            self.assertFalse((Path(temporary) / 'diagnostics').exists())

    def testPublicRestrictionMatchesRecordedPortalFlagNotTokenPresence(self):
        self.assertTrue(publicViewRestricted({'IsDataVerified2023': True, 'Token': 'present'}))
        self.assertTrue(publicViewRestricted({'IsDataVerified2023': 'true'}))
        self.assertFalse(publicViewRestricted({'IsDataVerified2023': False}))
        self.assertFalse(publicViewRestricted({'IsDataVerified2023': 'false'}))
        self.assertFalse(publicViewRestricted({}))
        with tempfile.TemporaryDirectory() as temporary:
            client = PortalClient(cookieFile=Path(temporary) / 'cookies')
            with patch.object(client, 'requestText', side_effect=AssertionError('Must not request a restricted page')):
                detail = client.fetchDetails({'IsDataVerified2023': True, 'Token': 'present'})
            self.assertEqual(detail['Detail']['FetchStatus'], 'unavailable')

    def testRestrictionAcrossTagsAndHiddenTemplateIsIgnored(self):
        self.assertTrue(restrictedPage('<main>Property is NOT <b>available</b> for&nbsp;public view.</main>'))
        self.assertFalse(restrictedPage('<script>var message="Not available for public view";</script>'))
        self.assertFalse(restrictedPage('<div hidden>Not available for public view</div>'))

    def testAuthorizationPopupRequiresSignedInShellAndExactEmittedCall(self):
        notice = "showNotification('Error', 'Not Authorise to view the details.')"
        shell = '<a href="/Account/LogOff">Sign Out</a>'
        self.assertFalse(restrictedPage(f'<script>{notice}</script>'))
        self.assertFalse(restrictedPage(shell + '<script>var message="Not Authorise to view the details.";</script>'))
        self.assertTrue(restrictedPage(shell + f'<script>\n{notice}\n</script>'))
        with tempfile.TemporaryDirectory() as temporary:
            client = PortalClient(cookieFile=Path(temporary) / 'cookies')
            html = shell + f'<script>\n{notice}\n</script><input id="txtUser"><input id="txtOTP">'
            with patch.object(client, 'requestText', return_value=html):
                with self.assertRaises(AuthenticationError):
                    client.fetchDetails({'Token': 'sample'})

    @unittest.skipUnless((FIXTUREDIR / 'not_available_for_public_full.har').exists(), 'Recorded restriction HAR unavailable')
    def testRecordedAuthorizationRedirectIsUnavailableNotAParserOrLoginFailure(self):
        har = json.loads((FIXTUREDIR / 'not_available_for_public_full.har').read_text())
        entries = har['log']['entries']
        detailRequest = next(entry for entry in entries if urlsplit(entry['request']['url']).path == '/RegisterProperty/NewApplication')
        self.assertEqual(detailRequest['response']['status'], 302)
        self.assertEqual(urlsplit(detailRequest['response']['redirectURL']).path, '/Home/Index')
        home = next(entry for entry in entries if urlsplit(entry['request']['url']).path == '/Home/Index')
        html = home['response']['content']['text']
        self.assertTrue(restrictedPage(html))
        with tempfile.TemporaryDirectory() as temporary:
            client = PortalClient(cookieFile=Path(temporary) / 'cookies')
            with patch.object(client, 'requestText', return_value=html) as request:
                detail = client.fetchDetails({'Token': 'sample'})
            request.assert_called_once()
            self.assertEqual(detail['Detail']['FetchStatus'], 'unavailable')

    def testBooleanAuthorizationFallbackUsesReadableValues(self):
        self.assertEqual(normalizeProperty({'IsAuthorised': True})['Authorized Status'], 'Authorized')
        self.assertEqual(normalizeProperty({'IsAuthorised': False})['Authorized Status'], 'Unauthorized')

    def testDiagnosticsContainStructureNotFieldValuesOrSessionTokens(self):
        schema = responseSchema(samplePage())
        text = json.dumps(schema)
        self.assertNotIn('Owner 1', text)
        self.assertNotIn('0000000000', text)
        self.assertIn('Owner Name', text)

    def testGenericLabelledErrorPageIsNotAcceptedAsAProperty(self):
        html = '<span class="font-weight-bold en">Error:</span><span class="det">Temporarily unavailable</span>'
        with tempfile.TemporaryDirectory() as temporary:
            client = PortalClient(cookieFile=Path(temporary) / 'cookies')
            with patch.object(client, 'requestText', return_value=html):
                with self.assertRaisesRegex(RuntimeError, 'unrecognized format'):
                    client.fetchDetails({'PID': 'SAMPLE01', 'Token': 'sample'})
            diagnostic = Path(temporary) / 'diagnostics' / 'SAMPLE01_schema.json'
            self.assertTrue(diagnostic.exists())
            self.assertNotIn('Temporarily unavailable', diagnostic.read_text())

    @unittest.skipUnless((FIXTUREDIR / '1F7CL8R0.html').exists(), 'Recorded multi-owner HTML unavailable')
    def testRecordedMultiOwnerPageMatchesPDFStructure(self):
        row = normalizeProperty({}, parsePropertyPage((FIXTUREDIR / '1F7CL8R0.html').read_text()))
        self.assertEqual(row['PID'], '1F7CL8R0')
        self.assertEqual(row['Owner Count'], 2)
        self.assertEqual(row['Construction Row Count'], 2)
        self.assertEqual(row['Owner 1: Share'], '6/12')
        self.assertEqual(row['Owner 2: Share'], '6/12')
        self.assertEqual(row['Owner 1: Authenticated Status'], 'Aadhar')
        self.assertEqual(row['Authorized Status'], 'Authorized')
        self.assertEqual(row['Plot / House Number'], '8')
        self.assertEqual(row['Property Category'], 'Residential')
        self.assertEqual(row['Property Type'], 'House')
        self.assertEqual(row['Property Subtype'], 'Independent House')
        self.assertTrue(row['Floor 1: Covered Area'])
        self.assertTrue(row['Floor 2: Covered Area'])
        self.assertEqual(row['Owner 1: Mobile'], '9431121931')
        self.assertEqual(row['Owner 2: Mobile'], '9263185555')
        self.assertEqual(row['Mobile Number Linked With PID'], '9431121931')
        self.assertFalse(any('aadhar' in label.lower() or 'aadhaar' in label.lower() for label in row))

    @unittest.skipUnless((FIXTUREDIR / 'jaigopal.html').exists(), 'Recorded HTML unavailable')
    def testExistingHTMLAndVersionOneTableCheckpoints(self):
        html = (FIXTUREDIR / 'jaigopal.html').read_text()
        new = normalizeProperty({}, parsePropertyPage(html))
        old = normalizeProperty({}, {'Detail': {'ParseVersion': 1, 'Tables': collectTables(html)}})
        for key in ['Owner 1: Name', 'Floor 1: Floor', 'Floor 1: Occupier Name']:
            self.assertEqual(new[key], old[key])
        self.assertTrue(new['Owner 1: Mobile'])
        self.assertNotIn('X', new['Owner 1: Mobile'])
        self.assertIn('X', old['Owner 1: Mobile'])
        self.assertEqual(new['Authorized Status'], 'Authorized')

    def testMergedExportHasOneAddressPlotAndCategoryAndExpandingRows(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'state.sqlite3')
            identity = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], temporary)[0]
            with store.connect() as connection:
                for index, ownerCount in enumerate([1, 12], start=1):
                    detail = parsePropertyPage(samplePage(ownerCount, 3))
                    connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                        VALUES (?,?,?,?,'ok','complete')''', (identity, str(index), json.dumps({'PID': f'PID{index}', 'Address1': 'Fallback', 'PropertyCategory': 'Commercial'}), json.dumps(detail)))
            path = exportJob(store, store.job(identity))
            with path.with_suffix('.csv').open(encoding='utf-8-sig') as fileObj:
                rows = list(csv.DictReader(fileObj))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['Owner 12: Name'], '-')
            self.assertEqual(rows[1]['Owner 12: Name'], 'Ms. Owner 12 D/O Guardian 12 (1/12)')
            headers = list(rows[0])
            self.assertEqual(headers.count('Address'), 1)
            self.assertNotIn('Address1', headers)
            self.assertNotIn('PropertyCategory', headers)
            self.assertFalse(any(name.startswith('Detail.') for name in headers))
            workbook = load_workbook(path, read_only=True)
            self.assertEqual(workbook.active.max_row, None)
            self.assertEqual(sum(1 for unused in workbook.active.rows), 3)
            workbook.close()


class PropertyUpgradeTests(unittest.TestCase):
    def testExportSchemaUpgradeDoesNotRefetchOrChangeCompletedJobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.sqlite3'
            store = Store(path)
            identity = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], temporary)[0]
            detail = parsePropertyPage(samplePage())
            with store.connect() as connection:
                connection.execute("UPDATE settings SET value='2' WHERE key='exportSchemaVersion'")
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                    VALUES (?, 'record', ?, ?, 'ok', 'complete')''',
                    (identity, json.dumps({'PID': 'SAMPLE01', 'Token': 'sample'}), json.dumps(detail)))
                connection.execute('''UPDATE jobs SET status='complete',desired=0,uniqueCount=1,completedCount=1,
                    detailCount=1,dataVersion=5,exportVersion=5 WHERE id=?''', (identity,))
            store = Store(path)
            job = store.job(identity)
            self.assertEqual(job['status'], 'complete')
            self.assertFalse(job['desired'])
            self.assertEqual(job['completedCount'], 1)
            self.assertEqual(job['detailCount'], 1)
            self.assertEqual(job['dataVersion'], 6)
            self.assertIsNone(store.pendingProperty(identity))
            self.assertFalse(store.retainedExportJobs())
            engine = Engine(store, Path(temporary), lambda *event: None)
            engine.flushExports(force=True)
            with (Path(job['folder']) / 'properties.csv').open(encoding='utf-8-sig') as fileObj:
                rows = list(csv.DictReader(fileObj))
            self.assertEqual(len(rows), 1)
            self.assertIn('Owner 1: Mobile', rows[0])
            self.assertEqual(store.job(identity)['exportVersion'], 6)
            self.assertEqual(Store(path).job(identity)['dataVersion'], 6)

    def testRetainedExportRowsSurviveDetailRefreshAndPendingPhotos(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.sqlite3'
            store = Store(path)
            identity = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], temporary)[0]
            store.control([identity], 'pause')
            with store.connect() as connection:
                connection.execute("DELETE FROM settings WHERE key='propertyParserVersion'")
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                    VALUES (?, 'legacy', ?, ?, 'ok', 'complete')''',
                    (identity, json.dumps({'Token': 'exists', 'PID': 'OLDPID'}),
                     json.dumps({'Detail': {'ParseVersion': 1, 'Address': 'C-8, Colony'}})))
                connection.execute("INSERT INTO properties(jobId,identity,raw) VALUES (?, 'new', '{}')", (identity,))
            store = Store(path)
            self.assertFalse(store.job(identity)['completedCount'])
            self.assertIn(identity, store.retainedExportJobs())
            engine = Engine(store, Path(temporary), lambda *event: None)
            engine.flushExports(force=True)
            csvPath = Path(store.job(identity)['folder']) / 'properties.csv'
            with csvPath.open(encoding='utf-8-sig') as fileObj:
                rows = list(csv.DictReader(fileObj))
            self.assertEqual(len(rows), 1)
            self.assertNotIn('Processing Status', rows[0])
            self.assertEqual(rows[0]['Address'], 'C-8, Colony')
            store.saveDetail(identity, 'legacy', parsePropertyPage(samplePage()), ['https://example.test/photo.jpg'])
            with store.connect() as connection:
                rows = list(iterExportRows(connection, identity))
                self.assertEqual(len(rows), 1)
                self.assertNotIn('Processing Status', rows[0])
                self.assertIn('Owner 1: Name', rows[0])
            self.assertFalse(store.finishProperty(identity, 'legacy'))
            store.savePhoto(identity, 'legacy', 'https://example.test/photo.jpg', 'photos/legacy.jpg')
            self.assertTrue(store.finishProperty(identity, 'legacy'))
            with store.connect() as connection:
                record = connection.execute("SELECT retainedForExport FROM properties WHERE identity='legacy'").fetchone()
                self.assertFalse(record['retainedForExport'])

    def testFreshDiscoveredRowsDoNotTriggerEmptyWorkbook(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'state.sqlite3')
            identity = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], temporary)[0]
            with store.connect() as connection:
                connection.execute("INSERT INTO properties(jobId,identity,raw) VALUES (?, 'new', '{}')", (identity,))
            self.assertNotIn(identity, store.retainedExportJobs())
            engine = Engine(store, Path(temporary), lambda *event: None)
            engine.flushExports(force=True)
            self.assertFalse((Path(store.job(identity)['folder']) / 'properties.xlsx').exists())

    def testUpgradeReclassifiesRestrictionsAndRefreshesLegacyPagesWithoutLosingMapWork(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.sqlite3'
            store = Store(path)
            identity = store.addJobs([({'id': '1', 'name': 'Town'}, {'id': '2', 'name': 'Colony'})], temporary)[0]
            store.control([identity], 'pause')
            with store.connect() as connection:
                connection.execute("DELETE FROM settings WHERE key='propertyParserVersion'")
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                    VALUES (?, 'restricted', ?, ?, 'error', 'error')''',
                    (identity, json.dumps({'IsDataVerified2023': True, 'Token': 'exists'}),
                     json.dumps({'Detail': {'FetchStatus': 'error'}})))
                connection.execute('''INSERT INTO properties(jobId,identity,raw,detail,detailState,processingState)
                    VALUES (?, 'legacy', ?, ?, 'ok', 'complete')''',
                    (identity, json.dumps({'Token': 'exists', 'PID': 'OLDPID'}),
                     json.dumps({'Detail': {'ParseVersion': 1, 'Address': 'C-8, Colony'}})))
            store = Store(path)
            self.assertFalse(store.job(identity)['desired'])
            self.assertEqual(store.job(identity)['status'], 'paused')
            self.assertEqual(store.pendingProperty(identity)['identity'], 'legacy')
            self.assertEqual(store.job(identity)['completedCount'], 1)
            with store.connect() as connection:
                rows = list(iterExportRows(connection, identity))
                self.assertEqual(len(rows), 2)
                restricted = connection.execute("SELECT * FROM properties WHERE identity='restricted'").fetchone()
                self.assertEqual(restricted['detailState'], 'unavailable')
                self.assertEqual(restricted['processingState'], 'complete')
            # Reopening must not schedule the same migration a second time.
            self.assertEqual(Store(path).job(identity)['completedCount'], 1)


if __name__ == '__main__':
    unittest.main()
