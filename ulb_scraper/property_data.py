'''Label-driven property parsing and a single human-readable export schema.'''

# Standard Library Imports
from collections import defaultdict
import hashlib
from html import unescape
import json
import logging
import re
from urllib.parse import urlsplit

# Third Party Imports
from bs4 import BeautifulSoup


PARSERVERSION = 2
EXPORTVERSION = 9
IMPORTANT = [
    'PID', 'State', 'Municipality', 'Colony', 'Old PID', 'Owner Names',
    'Old Owner / Occupier Name', 'Ownership Type',
    'Have Any Court Case, Stay / Legal Proceeding',
    'Property Category', 'Property Type', 'Property Subtype',
    'Plot / House Number', 'Plot Area', 'Plot Area Unit', 'Address',
    'Mobile Number Linked With PID', 'Mobile Numbers',
    'Authorized Status', 'Property Availability',
]
METADATA = [
    'Update Review',
    'Owner Count', 'Construction Row Count', 'Integrated Count',
    'Integrated Count CMCDMC', 'Claim Count', 'Availability Note', 'Data Notes',
    'Property Record ID', 'Linked Property Record ID', 'Municipality ID',
    'Municipality Code', 'Colony ID', 'District ID', 'Portal Public-View Restriction',
    'Photo Source URLs', 'Plot / House Number Source',
    'Photo Download Status', 'Processing Status', 'Processing Error',
]
PRIVATEKEYS = ('aadhar', 'aadhaar', 'familyid', 'familymemberid', 'pantan',
               'pannumber', 'passport', 'dateofbirth', 'email', 'verificationtoken')
OWNERLABELS = {
    'srno': 'Serial Number', 'ownername': 'Name', 'gender': 'Gender',
    'fatherhusbandname': 'Father / Husband Name', 'guardianname': 'Father / Husband Name',
    'relation': 'Relation', 'relationtype': 'Relation', 'mobileno': 'Mobile',
    'authenticatedstatus': 'Authenticated Status', 'bhag': 'Share',
}
FLOORLABELS = {
    'srno': 'Serial Number', 'floor': 'Floor', 'occupancytype': 'Occupancy Type',
    'coveredarea': 'Covered Area', 'coveredareaunit': 'Covered Area Unit', 'rebate': 'Rebate',
    'occupiername': 'Occupier Name', 'occupiergender': 'Occupier Gender',
    'occupierguardianname': 'Occupier Guardian Name',
    'occupierrelationtype': 'Occupier Relation Type',
    'occupiermobile': 'Occupier Mobile',
}
EXTRALABELS = {
    'pkPropertyId': 'Property Record ID', 'fkDistrictId': 'District ID',
    'fkULBId': 'Municipality ID', 'fkColonySurveyId': 'Colony ID',
    'ULBCode': 'Municipality Code', 'Lat': 'Latitude', 'Long': 'Longitude',
    'KhasaraNo': 'Khasra Number', 'IsAuthorised': 'Authorized Status',
    'LinkedpkPropertyId': 'Linked Property Record ID',
    'IsDataVerified2023': 'Portal Public-View Restriction',
}


def textValue(value):
    '''Normalize a scraped scalar. Inputs: <value>. Output: stripped text.'''
    return re.sub(r'\s+', ' ', unescape(str(value if value is not None else ''))).strip()


def labelKey(label):
    '''Compare labels independent of casing/punctuation. Inputs: <label>. Output: key.'''
    return re.sub(r'[^a-z0-9]', '', textValue(label).lower())


def usable(value):
    '''Distinguish placeholders from actual values. Inputs: <value>. Output: boolean.'''
    return textValue(value).lower() not in ('', '-', '--', 'null', 'none', 'undefined', 'n/a', 'na')


def joinValues(values):
    '''Join distinct values without losing their order. Inputs: <values>. Output: text.'''
    return ', '.join(dict.fromkeys(textValue(value) for value in values if usable(value)))


def privateLabel(label):
    '''Identify personal identifiers excluded from outputs. Inputs: <label>. Output: boolean.'''
    key = labelKey(label)
    return any(part in key for part in PRIVATEKEYS)



def publicViewRestricted(record):
    '''Match the portal map's public-view rule. Inputs: <record>. Output: boolean.'''
    # The recorded map template exposes the detail link only for
    # !IsDataVerified2023. This counterintuitive flag is not a verification score.
    value = record.get('IsDataVerified2023')
    return value is True or str(value).strip().lower() in ('true', '1')


def nodeText(node, visibleOnly=False):
    '''Read English text without controls. Inputs: <node>, [visibleOnly]. Output: text.'''
    values = []
    for string in node.strings:
        parents = [string.parent, *string.parent.parents]
        if any(parent.name in ('script', 'style', 'input', 'select', 'button', 'textarea') or
               'hi' in parent.get('class', []) for parent in parents if parent.name):
            continue
        if visibleOnly and any(
                parent.has_attr('hidden') or
                {'d-none', 'hidden'}.intersection(parent.get('class', [])) or
                re.search(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b',
                          parent.get('style', ''), re.IGNORECASE) or
                (parent.name == 'label' and (parent.get('for') or parent.find(['input', 'select', 'textarea'])))
                for parent in parents if parent.name):
            continue
        values.append(str(string))
    return textValue(' '.join(values))


def authenticationStatus(value):
    '''Keep status text, not certification instructions. Inputs: <value>. Output: text.'''
    instruction = (r'\b(?:I\s+)?Certify\s+that\s+Family\s+id\s*'
                   r'\(Parivar\s+Pehchan\s+Patra\)\s+and\s+'
                   r'(?:Aadhar|Aadhaar|Adhar)\s+Number\s+Not\s+Available\.?')
    return textValue(re.sub(instruction, '', textValue(value), flags=re.IGNORECASE)).strip(' ,;')


def workflowField(label):
    '''Identify wizard step labels, not property data. Inputs: <label>. Output: boolean.'''
    return bool(re.fullmatch(r'step\d+', labelKey(label)))


def metadataColumn(label):
    '''Identify tracking/provenance and portal-internal fields. Inputs: <label>. Output: boolean.'''
    key = labelKey(label.rsplit(':', 1)[-1])
    return (label in METADATA or key.startswith(('integratedcount', 'claimcount')) or
            key.startswith(('fk', 'pk')) and key.endswith('id'))


def tableGrid(table):
    '''Expand row/column spans before matching table values to headers. Inputs: <table>. Output: headers, rows.'''
    grid = []
    occupied = {}
    headerCount = 0
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    for rowIndex, row in enumerate(rows):
        cells = row.find_all(['th', 'td'], recursive=False)
        if not cells:
            continue
        isHeader = bool(row.find_parent('thead')) or all(cell.name == 'th' for cell in cells)
        if isHeader and len(grid) == headerCount:
            headerCount += 1
        column = 0
        for cell in cells:
            while (rowIndex, column) in occupied:
                column += 1
            isStatus = not isHeader and any(labelKey(occupied.get((headerIndex, column), '')) == 'authenticatedstatus'
                                           for headerIndex in range(headerCount))
            value = nodeText(cell, visibleOnly=isStatus)
            rowSpan = min(2000, max(1, int(cell.get('rowspan', 1))))
            colSpan = min(1000, max(1, int(cell.get('colspan', 1))))
            for rowOffset in range(rowSpan):
                for columnOffset in range(colSpan):
                    occupied[rowIndex + rowOffset, column + columnOffset] = value
                    if len(occupied) > 100000:
                        raise ValueError('Property table exceeds the safe cell limit')
            column += colSpan
        width = max((columnIndex + 1 for position, columnIndex in occupied if position == rowIndex), default=0)
        grid.append([occupied.get((rowIndex, columnIndex), '') for columnIndex in range(width)])
    if not grid:
        return [], []
    if not headerCount:
        headerCount = 1
    width = max(map(len, grid))
    headers = []
    counts = defaultdict(int)
    for columnIndex in range(width):
        candidates = [row[columnIndex] for row in grid[:headerCount]
                      if columnIndex < len(row) and row[columnIndex]]
        header = candidates[-1] if candidates else f'Column {columnIndex + 1}'
        counts[header] += 1
        headers.append(header if counts[header] == 1 else f'{header} {counts[header]}')
    records = [dict(zip(headers, row + [''] * (width - len(row)))) for row in grid[headerCount:] if any(row)]
    return headers, records


def publicRow(row, labels):
    '''Preserve table columns with stable names. Inputs: <row>, <labels>. Output: dict.'''
    result = {}
    for label, value in row.items():
        key = labelKey(label)
        if privateLabel(label) or workflowField(label) or key.startswith('update'):
            continue
        name = labels.get(key, textValue(label))
        result[name] = (authenticationStatus(value) if key == 'authenticatedstatus' else
                        textValue(value) if 'mobile' in key or 'phone' in key else textValue(value))
    return result


def _extractOwnerMobiles(soup):
    '''Extract full owner mobile numbers from the ownerDetail JavaScript variable.'''
    for script in soup.find_all('script'):
        if script.get('src'):
            continue
        text = script.get_text()
        match = re.search(r'var\s+ownerDetail\s*=\s*(\[.*?\])\s*;', text, re.DOTALL)
        if not match:
            continue
        try:
            entries = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        mobiles = [textValue(entry.get('MobileNo', '')) if isinstance(entry, dict) else ''
                   for entry in entries]
        if any(mobiles):
            return mobiles
    return []


def parsePropertyPage(html):
    '''
    Parse scalar fields and arbitrarily many owners/construction rows.

    Inputs:    <html>  -  Property response HTML

    Output:    Detail envelope retained for checkpoint compatibility
    '''
    logging.debug('Parsing property labels and repeatable tables')
    soup = BeautifulSoup(html, 'html.parser')
    fields = {}
    for valueNode in soup.select('.det, dd, [data-property-value]'):
        labelNode = valueNode.find_previous_sibling(['span', 'label', 'b', 'strong', 'dt'])
        while labelNode and ('hi' in labelNode.get('class', []) or not nodeText(labelNode)):
            labelNode = labelNode.find_previous_sibling(['span', 'label', 'b', 'strong', 'dt'])
        if not labelNode or 'det' in labelNode.get('class', []):
            continue
        label = nodeText(labelNode).rstrip(':').strip()
        if label and not privateLabel(label) and not workflowField(label):
            value = nodeText(valueNode)
            if 'mobile' in labelKey(label) or 'phone' in labelKey(label):
                value = textValue(value)
            fields[label] = joinValues([fields.get(label), value])
    for labelNode in soup.select('.font-weight-bold.en, label, dt'):
        inline = nodeText(labelNode)
        if ':' not in inline:
            continue
        label, value = inline.split(':', 1)
        if usable(value) and not privateLabel(label) and not workflowField(label) and '?' not in label:
            fields.setdefault(label.strip(), textValue(value) if 'mobile' in labelKey(label) else value.strip())
    owners = []
    floors = []
    otherTables = []
    for table in soup.find_all('table'):
        headers, rows = tableGrid(table)
        keys = {labelKey(header) for header in headers}
        if 'ownername' in keys:
            owners.extend(publicRow(row, OWNERLABELS) for row in rows)
        elif 'floor' in keys:
            floors.extend(publicRow(row, FLOORLABELS) for row in rows)
        elif rows:
            heading = table.find_previous(['h3', 'h4', 'h5'])
            title = nodeText(heading) if heading else f'Additional Table {len(otherTables) + 1}'
            otherTables.append({'Title': title, 'Rows': [publicRow(row, {}) for row in rows]})
    detail = {''.join(part[:1].upper() + part[1:] for part in re.findall(r'[A-Za-z0-9]+', label)): value
              for label, value in fields.items()}
    identity = soup.find('input', id='AsPerRecord_pkPropertyId')
    if identity:
        detail['AsPerRecordPkPropertyId'] = textValue(identity.get('value'))
    mobileInput = soup.find('input', id='MobileNo')
    if mobileInput and mobileInput.get('value', '').strip():
        detail['MobileNoFull'] = textValue(mobileInput['value'])
    ownerMobiles = _extractOwnerMobiles(soup)
    if ownerMobiles:
        detail['OwnerMobileNos'] = ownerMobiles
    detail.update({
        'Fields': fields,
        'OwnerRows': owners,
        'ConstructionRows': floors,
        'OtherTables': otherTables,
        'OwnerNames': joinValues(row.get('Name') for row in owners),
        'ImageLinks': list(dict.fromkeys(image.get('src') for image in soup.find_all('img') if image.get('src'))),
        'ParseVersion': PARSERVERSION,
    })
    return {'Detail': detail}


def restrictedPage(html):
    '''Recognize a public-view restriction without bypassing it. Inputs: <html>. Output: boolean.'''
    soup = BeautifulSoup(html, 'html.parser')
    # The captured denial redirects to the signed-in home page and emits this
    # literal popup call. Shared JavaScript message templates are not evidence.
    signedIn = any(urlsplit(link['href']).path.lower() == '/account/logoff'
                   for link in soup.find_all('a', href=True))
    notice = r'''^\s*showNotification\(\s*(['"])Error\1\s*,\s*(['"])Not Authori[sz]e(?:d)? to view the details\.\2\s*\)\s*;?\s*$'''
    if signedIn and any(re.search(notice, script.get_text(), re.IGNORECASE | re.MULTILINE)
                        for script in soup.find_all('script') if not script.get('src')):
        return True
    for node in soup.select('script, style, .hi, [hidden], [style*="display:none"], [style*="display: none"]'):
        node.decompose()
    text = nodeText(soup)
    pattern = r'(?:not\s+(?:available|accessible|permitted|allowed)|unavailable)\s+(?:for|to|in)\s+(?:the\s+)?public(?:\s+view(?:ing)?)?'
    return bool(re.search(pattern, text, re.IGNORECASE))


def responseSchema(html):
    '''Return structural diagnostics without response values or tokens. Inputs: <html>. Output: dictionary.'''
    soup = BeautifulSoup(html, 'html.parser')
    return {
        'sha256': hashlib.sha256(html.encode()).hexdigest(),
        'bytes': len(html.encode()),
        'title': nodeText(soup.title) if soup.title else '',
        'labels': [nodeText(node).split(':', 1)[0] for node in soup.select('.font-weight-bold.en, dt')],
        'tableHeaders': [[nodeText(cell) for cell in table.find_all('th')] for table in soup.find_all('table')],
        'publicViewRestricted': restrictedPage(html),
    }


def inferPlotNumber(address):
    '''Conservatively infer a plot identifier only when explicitly absent. Inputs: <address>. Output: text.'''
    component = textValue(address).split(',', 1)[0].strip()
    if re.match(r'^(?:sector|ward|phase|block|near|village|street|road|pin)\b', component, re.IGNORECASE):
        return ''
    prefix = r'(?:h\s*\.?\s*(?:no\.?|number)|house\s*(?:no\.?|number)?|plot\s*(?:no\.?|number)?)\s*[:#.-]?\s*'
    identifier = r'[A-Za-z]{0,4}\s*-?\s*\d+[A-Za-z]*(?:[/-][A-Za-z0-9]+)*'
    prefixed = re.match(prefix + f'({identifier})' + r'(?=\s|$)', component, re.IGNORECASE)
    if prefixed:
        return re.sub(r'\s+', '', prefixed.group(1))
    if re.fullmatch(identifier, component):
        return re.sub(r'\s+', '', component)
    return ''


def legacyRows(tables, kind):
    '''Recover visible tables from version-one checkpoints. Inputs: <tables>, <kind>. Output: rows.'''
    records = []
    for table in tables:
        for index, row in enumerate(table):
            headers = [re.split(r'[^\x00-\x7f]', value, maxsplit=1)[0].strip() for value in row]
            keys = [labelKey(value) for value in headers]
            if kind == 'owner' and 'ownername' in keys:
                records.extend(publicRow(dict(zip(headers, values)), OWNERLABELS) for values in table[index + 1:])
                break
            if kind == 'floor' and 'floor' in keys:
                if index + 1 < len(table) and any('occupiername' == labelKey(value) for value in table[index + 1]):
                    headers = headers[:-1] + [re.split(r'[^\x00-\x7f]', value, maxsplit=1)[0].strip() for value in table[index + 1]]
                    index += 1
                records.extend(publicRow(dict(zip(headers, values)), FLOORLABELS) for values in table[index + 1:])
                break
    return records


def humanLabel(label):
    '''Format unknown source labels without generating opaque IDs. Inputs: <label>. Output: text.'''
    if label in EXTRALABELS:
        return EXTRALABELS[label]
    value = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', str(label)).replace('_', ' ')
    return textValue(value).strip('.: ')


def addExtraFields(row, label, value):
    '''Flatten new source structures without exposing identity/contact values. Inputs: <row>, <label>, <value>.'''
    if privateLabel(label) or workflowField(label):
        return
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, dict):
        for childLabel, childValue in value.items():
            addExtraFields(row, f'{label}: {humanLabel(childLabel)}', childValue)
    elif isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            combined = joinValues(value)
            row[label] = textValue(combined) if 'mobile' in labelKey(label) or 'phone' in labelKey(label) else combined
        else:
            for index, item in enumerate(value, start=1):
                addExtraFields(row, f'{label} {index}', item)
    else:
        row[label] = textValue(value) if 'mobile' in labelKey(label) or 'phone' in labelKey(label) else value


def normalizeProperty(raw, parsed=None, job=None):
    '''
    Reconcile map/page values into one public-facing property row.

    Inputs:    <raw>     -  Map response record
               [parsed]  -  Current or legacy detail checkpoint
               [job]     -  Municipality/colony fallback names

    Output:    Flat row with human-readable, dynamically extensible columns
    '''
    logging.debug('Normalizing property export row')
    detail = (parsed or {}).get('Detail', {})
    job = job or {}
    fields = detail.get('Fields', {})
    page = {labelKey(label): value for label, value in detail.items() if not isinstance(value, (dict, list))}
    page.update({labelKey(label): value for label, value in fields.items()})
    mapping = {labelKey(label): value for label, value in raw.items()}
    usedPage = set()
    usedMap = set()
    notes = []

    def choose(labels, mapLabels=(), fallback='', name=''):
        keys = [labelKey(label) for label in labels]
        mapKeys = [labelKey(label) for label in mapLabels]
        usedPage.update(keys)
        usedMap.update(mapKeys)
        pageValue = next((page[key] for key in keys if usable(page.get(key))), '')
        mapValue = next((mapping[key] for key in mapKeys if usable(mapping.get(key))), '')
        if name and usable(pageValue) and usable(mapValue) and labelKey(pageValue) != labelKey(mapValue):
            notes.append(f'{name}: page={pageValue}; map={mapValue}')
        return pageValue if usable(pageValue) else mapValue if usable(mapValue) else fallback

    pidMobile = choose(['Mobile Number linked with the PID', 'Mobile Number linked with PID', 'Mobile No'],
                       ['MobileNo', 'Mobile Number linked with the PID', 'Mobile Number linked with PID'])
    row = {
        'PID': choose(['PID'], ['PID', 'PIDNDC']),
        'State': 'Haryana',
        'Municipality': choose(['Municipality', 'McName'], ['McName'], job.get('municipalityName', '')),
        'Colony': choose(['Colony Name'], ['ColonyName'], job.get('colonyName', '')),
        'Old PID': choose(['Old PID'], ['OldPID']),
        'Old Owner / Occupier Name': choose(['Old Owner / Occupier Name'], ['OwnerNameNDC']),
        'Ownership Type': choose(['Ownership Type'], ['OwnershipType']),
        'Have Any Court Case, Stay / Legal Proceeding': choose(['Have Any Court Case, Stay/Legal Proceeding'], ['HaveAnyCourtCaseStayLegalProceeding']),
        'Property Category': choose(['Property Category'], ['PropertyCategory'], name='Property Category'),
        'Property Type': choose(['Property Type'], ['PropertyType'], name='Property Type'),
        'Property Subtype': choose(['Property SubType'], ['PropertySubType'], name='Property Subtype'),
        'Address': choose(['Address'], ['Address1', 'Address']),
        'Plot / House Number': choose(['Plot No. / House No', 'Plot Number', 'House Number'], ['PlotNo', 'HouseNo']),
        'Plot Area': choose(['Plot Area'], ['PlotSize']),
        'Plot Area Unit': choose(['Plot Area Unit', 'Unit'], ['Unit']),
        'Mobile Number Linked With PID': textValue(detail.get('MobileNoFull') or pidMobile),
        'Authorized Status': choose(['Authorized Status', 'Authorised Status'], ['IsAuthorised']),
    }
    if isinstance(row['Authorized Status'], bool):
        row['Authorized Status'] = 'Authorized' if row['Authorized Status'] else 'Unauthorized'
    if not usable(row['Plot / House Number']):
        row['Plot / House Number'] = inferPlotNumber(row['Address'])
        row['Plot / House Number Source'] = 'Address (inferred)' if row['Plot / House Number'] else ''
    else:
        row['Plot / House Number Source'] = 'Explicit field'
    row['Plot Area'], row['Plot Area Unit'] = splitPlotArea(row['Plot Area'], row['Plot Area Unit'])
    owners = detail.get('OwnerRows', []) if detail.get('ParseVersion', 1) >= 2 else legacyRows(detail.get('Tables', []), 'owner')
    if not owners:
        owners = [publicRow({key: value for key, value in owner.items()
                             if labelKey(key) in ('ownername', 'gender', 'guardianname', 'relationtype', 'bhag')}, OWNERLABELS)
                  for owner in detail.get('OwnerRows', [])]
    floors = detail.get('ConstructionRows', []) or legacyRows(detail.get('Tables', []), 'floor')
    row['Owner Names'] = joinValues(owner.get('Name') for owner in owners) or choose(['Owner Names'], ['OwnerName'])
    row['Owner Count'] = len(owners) if owners else ''
    row['Construction Row Count'] = len(floors) if floors else ''
    ownerMobiles = detail.get('OwnerMobileNos', [])
    mobiles = [row['Mobile Number Linked With PID'], textValue(raw.get('MobileNo', ''))]
    for prefix, records in [('Owner', owners), ('Floor', floors)]:
        for index, record in enumerate(records, start=1):
            for label, value in record.items():
                if privateLabel(label) or workflowField(label):
                    continue
                label = re.sub(r'\s*\(Masked\)$', '', label, flags=re.IGNORECASE)
                if labelKey(label) == 'authenticatedstatus':
                    value = authenticationStatus(value)
                if 'mobile' in labelKey(label) or 'phone' in labelKey(label):
                    if prefix == 'Owner' and index - 1 < len(ownerMobiles) and usable(ownerMobiles[index - 1]):
                        value = ownerMobiles[index - 1]
                    else:
                        value = textValue(value)
                    mobiles.append(value)
                row[f'{prefix} {index}: {label}'] = value
    row['Mobile Numbers'] = joinValues(number.strip() for group in mobiles
                                      for number in re.split(r'[,;|]', str(group)))
    row['Property Availability'] = 'Not available for public view' if detail.get('FetchStatus') == 'unavailable' or publicViewRestricted(raw) else detail.get('FetchStatus', '')
    row['Availability Note'] = detail.get('FetchError', '') if detail.get('FetchStatus') == 'unavailable' else ''
    row['Data Notes'] = '; '.join(notes)
    ignored = {'ownernames', 'ownerrows', 'ownermobilenos', 'ownerguardiannames', 'mobilenofull',
               'inputvalues', 'imagelinks', 'tables', 'fields', 'constructionrows', 'othertables',
               'parseversion', 'fetchstatus', 'fetcherror', 'sourceurl', 'asperrecordpkpropertyid'}
    for label, value in {**detail, **fields}.items():
        key = labelKey(label)
        if key in usedPage or key in ignored or privateLabel(label) or workflowField(label) or isinstance(value, (dict, list)):
            continue
        row[humanLabel(label)] = textValue(value) if 'mobile' in key else value
    for tableIndex, table in enumerate(detail.get('OtherTables', []), start=1):
        for rowIndex, values in enumerate(table['Rows'], start=1):
            for label, value in values.items():
                if not privateLabel(label) and not workflowField(label):
                    row[f'{table["Title"]} {tableIndex}, Row {rowIndex}: {label}'] = value
    for label, value in raw.items():
        key = labelKey(label)
        if key in usedMap or key in ('token', 'imageviewlink', 'ownername', 'pidndc', 'pageno', 'totalrecord') or privateLabel(label):
            continue
        addExtraFields(row, humanLabel(label), value)
    return row


def orderedColumns(available, nonEmpty):
    '''Put property fields first and tracking metadata last. Inputs: <available>, <nonEmpty>. Output: list.'''
    def natural(label):
        return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r'(\d+)', label))

    def repeatedOrder(label):
        match = re.match(r'^(Owner|Floor) (\d+): (.+)', label)
        prefix, index, field = match.groups()
        labels = list(dict.fromkeys((OWNERLABELS if prefix == 'Owner' else FLOORLABELS).values()))
        baseField = field[:-5] if field.endswith(' Unit') else field
        return (0 if prefix == 'Owner' else 1, int(index),
                labels.index(baseField) if baseField in labels else len(labels),
                natural(baseField), int(field.endswith(' Unit')))

    available = {label for label in available if not workflowField(label)}
    metadata = {label for label in available if metadataColumn(label)}
    available = available.difference(metadata)
    primary = [label for label in IMPORTANT if label in available]
    repeated = sorted((label for label in available if re.match(r'^(?:Owner|Floor) \d+:', label)), key=repeatedOrder)
    remainder = sorted(available.difference(primary, repeated), key=natural)
    metadataOrder = [label for label in METADATA if label in metadata]
    metadataOrder += sorted(metadata.difference(metadataOrder), key=natural)
    return (primary + repeated + [label for label in remainder if label in nonEmpty] +
            [label for label in remainder if label not in nonEmpty] + metadataOrder)


def personDescription(name, gender='', guardian='', relation='', share=None):
    '''
    Combine a person's relationship fields without guessing missing values.

    Inputs:    <name>      -  Display name
               [gender]    -  Recorded gender
               [guardian]  -  Recorded guardian or spouse
               [relation]  -  Relationship to that person
               [share]     -  Ownership share; None omits the share

    Output:    Readable name, relationship and optional share
    '''
    logging.debug('Combining person description')
    genderKey = labelKey(gender)
    relationKey = labelKey(relation)
    childRelation = {'female': 'D/O', 'f': 'D/O', 'male': 'S/O', 'm': 'S/O'}.get(genderKey)
    relationship = {'husband': 'W/O', 'wife': 'H/O', 'father': childRelation,
                    'mother': childRelation, 'parent': childRelation,
                    'guardian': 'C/O', 'wo': 'W/O', 'ho': 'H/O',
                    'so': 'S/O', 'do': 'D/O', 'co': 'C/O', 'ro': 'R/O'}.get(relationKey)
    honorific = ('Mrs.' if relationship == 'W/O' else
                 'Mr.' if relationship in ('H/O', 'S/O') else
                 'Ms.' if relationship == 'D/O' else
                 {'female': 'Ms.', 'f': 'Ms.', 'male': 'Mr.', 'm': 'Mr.'}.get(genderKey, ''))
    description = textValue(name) if usable(name) else ''
    if description and honorific and not re.match(r'^(?:Mr|Mrs|Ms)\.?\s', description, re.I):
        description = f'{honorific} {description}'
    if usable(guardian):
        description = f'{description} {relationship or "R/O"} {textValue(guardian)}'.strip()
    if share is not None and usable(share):
        description = f'{description} ({textValue(share)})'.strip()
    return description


def cleanCombinedPerson(value):
    '''Upgrade older combined names without inventing missing gender.'''
    logging.debug('Cleaning previously combined person description')
    value = textValue(value) if usable(value) else ''
    fallback = re.fullmatch(r'(.*?) \[Gender: (.*?); Relation: (.*?); Guardian: (.*?)\](?: \((.*?)\))?', value)
    if fallback:
        name, gender, relation, guardian, share = fallback.groups()
        return personDescription(name, gender, guardian, relation, share)
    shareMatch = re.search(r' \(([^()]*)\)$', value)
    share = shareMatch[1] if shareMatch else None
    person = value[:shareMatch.start()] if shareMatch else value
    relationship = re.fullmatch(r'(.*?) (W/O|H/O|S/O|D/O|C/O|R/O) (.*)', person, re.I)
    if relationship:
        return personDescription(relationship[1], guardian=relationship[3], relation=relationship[2], share=share)
    return personDescription(person, share=share)


def splitPlotArea(value, unit=''):
    '''Separate a plot measurement from its unit without changing precision.'''
    logging.debug('Separating plot area and unit')
    area = textValue(value) if usable(value) else ''
    unit = textValue(unit) if usable(unit) else ''
    match = re.fullmatch(r'([+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+))\s*([^\d]*)', area)
    if match:
        return match[1], match[2].strip() or unit
    return area, unit


def mobileIdentity(value):
    '''Return ten digits, removing an Indian country code; reject unusable numbers.'''
    value = textValue(value)
    if not re.fullmatch(r'\+?[0-9\s().-]+', value):
        return ''
    digits = re.sub(r'[^0-9]', '', value)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    return digits if len(digits) == 10 and len(set(digits)) > 1 else ''


def compactExportRow(source, peopleCombined=False):
    '''
    Project normalized checkpoint data into the compact user export schema.

    Inputs:    <source>  -  Normalized property row, including pipeline fields
               [peopleCombined] - Existing compact name fields need no reformatting

    Output:    New row with combined people and deduplicated contact columns
    '''
    logging.debug('Preparing compact export row')
    row = dict(source)
    row['State'] = 'Haryana'
    oldUnit = row.pop('Unit', '')
    plotUnit = row.get('Plot Area Unit') if usable(row.get('Plot Area Unit')) else oldUnit
    if 'Plot Area' in row:
        row['Plot Area'], row['Plot Area Unit'] = splitPlotArea(row['Plot Area'], plotUnit)
    elif usable(plotUnit):
        row['Plot Area Unit'] = textValue(plotUnit)
    for label in list(row):
        if re.fullmatch(r'Floor \d+: .*\bArea', label):
            row[label], row[label + ' Unit'] = splitPlotArea(row[label], row.get(label + ' Unit', ''))
    pidMobileAliases = {'mobilenumberlinkedwithpid', 'mobilenumberlinkedwiththepid', 'mobileno'}
    aliasNumbers = []
    for label in list(row):
        if labelKey(label) in pidMobileAliases and label != 'Mobile Number Linked With PID':
            if usable(row[label]):
                aliasNumbers.append(row[label])
                if not usable(row.get('Mobile Number Linked With PID')):
                    row['Mobile Number Linked With PID'] = row[label]
            del row[label]
    ownerIndices = sorted({int(match.group(1)) for label in row
                           if (match := re.match(r'^Owner (\d+):', label))})
    if not any(usable(row.get(f'Owner {index}: Name')) for index in ownerIndices) and usable(row.get('Owner Names')):
        row['Owner 1: Name'] = row['Owner Names']
        ownerIndices = sorted(set(ownerIndices + [1]))
    for index in ([] if peopleCombined else ownerIndices):
        prefix = f'Owner {index}: '
        row[prefix + 'Name'] = personDescription(
            row.get(prefix + 'Name'), row.get(prefix + 'Gender'),
            row.get(prefix + 'Father / Husband Name'), row.get(prefix + 'Relation'),
            share=row.get(prefix + 'Share', ''))
    floorIndices = sorted({int(match.group(1)) for label in row
                           if (match := re.match(r'^Floor (\d+):', label))})
    for index in ([] if peopleCombined else floorIndices):
        prefix = f'Floor {index}: Occupier '
        row[prefix + 'Name'] = personDescription(
            row.get(prefix + 'Name'), row.get(prefix + 'Gender'),
            row.get(prefix + 'Guardian Name'), row.get(prefix + 'Relation Type'))
    if peopleCombined:
        for label in row:
            if re.fullmatch(r'Owner \d+: Name|Floor \d+: Occupier Name', label):
                row[label] = cleanCombinedPerson(row[label])
    deleted = {
        'Owner Names', 'Authorized Status', 'Owner Count', 'Construction Row Count',
        'Property Availability', 'Processing Status', 'Processing Error', 'Availability Note',
        'fk Property Sub Type Id', 'Integrated Count', 'Linked Property Record ID',
        'Municipality ID', 'Plot / House Number Source', 'Portal Public-View Restriction',
        'Property Record ID', 'Claim Count', 'Colony ID', 'District ID',
        'Integrated Count CMCDMC', 'Locations Data Json', 'Municipality Code',
        'Possession File', 'Property Card No Membership No', 'Property Card No./Membership No',
        'Registered Society Name',
    }
    deletedKeys = {labelKey(label) for label in deleted}
    ownerFields = {'serialnumber', 'gender', 'fatherhusbandname', 'relation',
                   'authenticatedstatus', 'share', 'gstnumber', 'nodalofficername'}
    floorFields = {'serialnumber', 'rebate', 'occupiergender', 'occupierguardianname', 'occupierrelationtype'}
    for label in list(row):
        repeated = re.match(r'^(Owner|Floor) \d+: (.+)$', label)
        if (labelKey(label) in deletedKeys or privateLabel(label) or workflowField(label) or
                label.startswith(('Due Details:', 'OutStanding Dues Details')) or
                repeated and labelKey(repeated.group(2)) in (ownerFields if repeated.group(1) == 'Owner' else floorFields)):
            del row[label]
    contactColumns = ['Mobile Number Linked With PID']
    contactColumns += [f'Owner {index}: Mobile' for index in ownerIndices]
    contactColumns += [f'Floor {index}: Occupier Mobile' for index in floorIndices]
    contactColumns += sorted(label for label in row if label not in contactColumns and
                             label != 'Mobile Numbers' and ('mobile' in labelKey(label) or 'phone' in labelKey(label)))
    allNumbers = {}
    for column in contactColumns:
        if column not in row:
            continue
        unique = []
        for number in re.split(r'[,;|]', textValue(row[column])):
            number = number.strip()
            key = mobileIdentity(number)
            if key and key not in allNumbers:
                allNumbers[key] = key
                unique.append(key)
        row[column] = ', '.join(unique)
    remainingNumbers = [source.get('Mobile Numbers', ''), *aliasNumbers]
    for number in re.split(r'[,;|]', joinValues(remainingNumbers)):
        number = number.strip()
        key = mobileIdentity(number)
        if key:
            allNumbers.setdefault(key, key)
    row['Mobile Numbers'] = ', '.join(allNumbers.values())
    return row


def compactColumns(available, nonEmpty):
    '''Order usable fields, contact summary, timestamp, then metadata. Inputs: <available>, <nonEmpty>. Output: list.'''
    columns = orderedColumns(available, nonEmpty)
    tail = ['Mobile Numbers', 'Data Fetched At (IST)', 'Data Last Checked At (IST)', 'Data Last Changed At (IST)']
    data = [label for label in columns if label not in tail and not metadataColumn(label)]
    metadata = [label for label in columns if label not in tail and metadataColumn(label)]
    return data + [label for label in tail if label in available] + metadata
