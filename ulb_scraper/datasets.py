'''Read-only dataset validation and portable, incremental colony checkpoints.'''

# Standard Library Imports
from contextlib import closing
import csv
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from zipfile import BadZipFile

# Third Party Imports
from openpyxl import load_workbook

from ulb_scraper.property_data import compactExportRow, usable


CHECKPOINT = '.ulb-checkpoint.sqlite3'
MANIFEST = '.ulb-dataset.json'
TABLES = ('jobs', 'properties', 'photos', 'tiles', 'baselines', 'datasetHistory', 'settings')
REVIEWLABELS = {'not_selected': 'Not selected for missing-mobile update', 'pending': 'Not checked in this update', 'updated': 'Updated',
                'unchanged': 'Unchanged', 'new': 'New property',
                'error': 'Update failed; previous data retained',
                'unavailable': 'Public details unavailable; previous data retained',
                'not_found': 'Not found in completed scan; retained for review'}


def photoPath(folder, value):
    '''Validate a portable photo path. Inputs: <folder>, <value>. Output: resolved path.'''
    logging.debug('Validating dataset photo path')
    value = str(value).replace('\\', '/')
    if Path(value).is_absolute() or re.match(r'^[A-Za-z]:', value):
        raise ValueError('Photo links must be relative to the colony folder')
    path = (folder / value).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError('A photo link escapes its colony folder')
    return path


def contentHash(row, folder):
    '''Hash property values and image content, excluding volatile metadata. Inputs: <row>, <folder>. Output: digest.'''
    logging.debug('Hashing dataset property content')
    values = {label: str(value) for label, value in row.items() if usable(value) and
              label not in ('Photos', 'Data Fetched At (IST)', 'Data Fetched At (UTC)',
                            'Data Last Checked At (IST)', 'Data Last Changed At (IST)', 'Update Review') and
              not re.fullmatch(r'Photo \d+', label)}
    photographs = []
    for label, value in row.items():
        if re.fullmatch(r'Photo \d+', label) and usable(value):
            path = photoPath(Path(folder), value)
            digest = hashlib.sha256()
            if path.is_file():
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                photographs.append(digest.hexdigest())
            else:
                photographs.append('missing')
    values['Photograph Content'] = sorted(photographs)
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validatePhotoLinks(row, folder):
    '''Check relative photo links in an imported row. Inputs: <row>, <folder>. Output: None.'''
    logging.debug('Validating imported row photo links')
    for label, value in row.items():
        if (label == 'Photos' or re.fullmatch(r'Photo \d+', label)) and usable(value):
            for relative in str(value).split(', '):
                photoPath(folder, relative)


def readRows(path):
    '''Read and validate a CSV/XLSX baseline without changing it. Inputs: <path>. Output: PID-to-row mapping.'''
    logging.info(f'Reading dataset baseline {path.name}')
    workbook = None
    stream = None
    result = {}
    try:
        if path.suffix == '.csv':
            stream = path.open(newline='', encoding='utf-8-sig')
            tables = [iter(csv.reader(stream))]
        else:
            workbook = load_workbook(path, read_only=True, data_only=False)
            tables = [sheet.iter_rows(values_only=True) for sheet in workbook.worksheets]
        for table in tables:
            headers = [str(value).strip() if value is not None else '' for value in next(table, [])]
            if not headers or 'PID' not in headers or len(set(headers)) != len(headers) or '' in headers:
                raise ValueError(f'{path.name} needs unique, nonempty headers including PID')
            for cells in table:
                if not any(value is not None and str(value).strip() for value in cells):
                    continue
                if len(cells) != len(headers):
                    raise ValueError(f'{path.name} contains a row with the wrong number of cells')
                row = {label: str(value) if value is not None and value != '' else '-'
                       for label, value in zip(headers, cells)}
                pid = row['PID'].strip()
                if not usable(pid) or pid in result:
                    raise ValueError(f'{path.name} contains missing or duplicate PIDs')
                validatePhotoLinks(row, path.parent)
                result[pid] = row
        return result
    finally:
        if stream:
            stream.close()
        if workbook:
            workbook.close()


def inspectDataset(root, progress=None):
    '''
    Validate a dataset root and collect its colony inventory, without writes.

    Inputs:    <root>  -  Existing municipality/colony output root

    Output:    Inventory with file fingerprints for confirmation-time revalidation
    '''
    logging.info('Validating selected update dataset')
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Choose an existing dataset root')
    manifest = root / MANIFEST
    datasetId = ''
    if manifest.exists():
        if manifest.is_symlink():
            raise ValueError('Dataset manifests cannot be symbolic links')
        document = json.loads(manifest.read_text(encoding='utf-8'))
        if document.get('version') != 1:
            raise ValueError('Unsupported dataset manifest version')
        datasetId = uuid.UUID(document['datasetId']).hex
    entries = []
    errors = []
    seen = set()
    municipalities = sorted(root.iterdir())
    for municipality in municipalities:
        if progress:
            progress(0, 0, f'Validating {municipality.name}')
        if (not municipality.is_dir() or municipality.name.startswith(('.', '_')) or
                municipality.name.casefold() in ('master', 'exported') or
                municipality.name.casefold().startswith(('updated-mobilenos-', 'mobile-updates-'))):
            continue
        for colony in sorted(municipality.iterdir()):
            if not colony.is_dir() or colony.name.startswith(('.', '_')):
                continue
            files = [colony / name for name in ('properties.csv', 'properties.xlsx', CHECKPOINT)
                     if (colony / name).exists()]
            if not files:
                continue
            try:
                if progress:
                    progress(0, 0, f'Validating {municipality.name} / {colony.name}')
                if municipality.is_symlink() or colony.is_symlink() or any(path.is_symlink() for path in files):
                    raise ValueError('Dataset folders and record files cannot be symbolic links')
                parentMatch = re.fullmatch(r'(.+)_(\d+)', municipality.name)
                childMatch = re.fullmatch(r'(.+)_(\d+)', colony.name)
                if not parentMatch or not childMatch:
                    raise ValueError('Expected municipality_name_ID/colony_name_ID folders')
                pair = (parentMatch[2], childMatch[2])
                if pair in seen:
                    raise ValueError('Multiple folders identify the same municipality/colony')
                csvRows = readRows(colony / 'properties.csv') if (colony / 'properties.csv').exists() else None
                xlsxRows = readRows(colony / 'properties.xlsx') if (colony / 'properties.xlsx').exists() else None
                if csvRows is not None and xlsxRows is not None and csvRows != xlsxRows:
                    raise ValueError('CSV and Excel snapshots disagree; choose a consistent pair or retain only the intended baseline')
                rows = csvRows if csvRows is not None else xlsxRows
                checkpoint = colony / CHECKPOINT
                portable = None
                if checkpoint.exists():
                    with closing(sqlite3.connect(checkpoint.as_uri() + '?mode=ro', uri=True)) as connection:
                        connection.row_factory = sqlite3.Row
                        connection.execute('PRAGMA trusted_schema=OFF')
                        jobs = connection.execute('SELECT * FROM jobs').fetchall()
                        if len(jobs) != 1 or (jobs[0]['municipalityId'], jobs[0]['colonyId']) != pair:
                            raise ValueError('Portable checkpoint does not match the colony folder IDs')
                        portable = dict(jobs[0])
                        if not datasetId:
                            datasetId = uuid.UUID(portable['datasetId']).hex
                        if datasetId and portable['datasetId'] != datasetId:
                            raise ValueError('Checkpoint belongs to a different dataset manifest')
                        for photograph in connection.execute('SELECT path FROM photos WHERE path IS NOT NULL'):
                            photoPath(colony, photograph['path'])
                        for table in ('baselines', 'datasetHistory'):
                            for baseline in connection.execute(f'SELECT pid,row FROM {table}'):
                                row = json.loads(baseline['row'])
                                if not isinstance(row, dict) or row.get('PID') != baseline['pid']:
                                    raise ValueError('Checkpoint baseline contains an inconsistent PID')
                                validatePhotoLinks(row, colony)
                if rows is None and not portable:
                    raise ValueError('No readable baseline or portable checkpoint')
                rows = rows or {}
                municipalityNames = {row.get('Municipality', '') for row in rows.values() if usable(row.get('Municipality'))}
                colonyNames = {row.get('Colony', '') for row in rows.values() if usable(row.get('Colony'))}
                if len(municipalityNames) > 1 or len(colonyNames) > 1:
                    raise ValueError('Baseline rows belong to multiple municipalities or colonies')
                seen.add(pair)
                entries.append({'municipalityId': pair[0], 'colonyId': pair[1], 'folder': str(colony),
                                'municipalityName': next(iter(municipalityNames), parentMatch[1].replace('_', ' ')),
                                'colonyName': next(iter(colonyNames), childMatch[1].replace('_', ' ')),
                                'rowCount': len(rows), 'portable': portable,
                                'files': {path.name: fileDigest(path) for path in files}})
            except (BadZipFile, ValueError, KeyError, OSError, sqlite3.Error) as exception:
                errors.append(f'{municipality.name}/{colony.name}: {exception}')
    if errors:
        raise ValueError('Dataset validation failed:\n' + '\n'.join(errors[:20]))
    if not entries:
        raise ValueError('No colony datasets found under the selected root')
    return {'root': str(root), 'datasetId': datasetId, 'entries': entries,
            'manifestFingerprint': fileDigest(manifest) if manifest.exists() else None}


def revalidateDataset(inventory, progress=None):
    '''
    Confirm previously validated files are unchanged without parsing them again.

    Inputs:    <inventory>  -  Original validated folder inventory
               [progress]   -  Operation progress callback

    Output:    None; raises ValueError if the source changed
    '''
    logging.info('Checking dataset fingerprints before import')
    root = Path(inventory['root'])
    changed = 'Dataset changed after validation. Pause its active jobs and select the folder again.'
    folders = set()
    for municipality in root.iterdir():
        if (not municipality.is_dir() or municipality.name.startswith(('.', '_')) or
                municipality.name.casefold() in ('master', 'exported') or
                municipality.name.casefold().startswith(('updated-mobilenos-', 'mobile-updates-'))):
            continue
        for colony in municipality.iterdir():
            if colony.is_dir() and not colony.name.startswith(('.', '_')) and any(
                    (colony / name).exists() for name in ('properties.csv', 'properties.xlsx', CHECKPOINT)):
                if municipality.is_symlink() or colony.is_symlink():
                    raise ValueError(changed)
                folders.add(str(colony.resolve()))
    if folders != {entry['folder'] for entry in inventory['entries']}:
        raise ValueError(changed)
    manifest = root / MANIFEST
    if (manifest.is_symlink() or
            (fileDigest(manifest) if manifest.exists() else None) != inventory.get('manifestFingerprint')):
        raise ValueError(changed)
    if manifest.exists():
        if manifest.is_symlink():
            raise ValueError(changed)
        document = json.loads(manifest.read_text(encoding='utf-8'))
        if document.get('version') != 1 or uuid.UUID(document['datasetId']).hex != inventory['datasetId']:
            raise ValueError(changed)
    for index, entry in enumerate(inventory['entries']):
        if progress:
            progress(index, len(inventory['entries']), 'Checking source files have not changed')
        folder = Path(entry['folder'])
        files = [folder / name for name in ('properties.csv', 'properties.xlsx', CHECKPOINT)
                 if (folder / name).exists()]
        if any(path.is_symlink() for path in files) or {path.name: fileDigest(path) for path in files} != entry['files']:
            raise ValueError(changed)


def fileDigest(path):
    '''Fingerprint an import file. Inputs: <path>. Output: SHA-256 digest.'''
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def upsert(connection, table, row):
    '''Copy a checkpoint row without replacing its children. Inputs: <connection>, <table>, <row>.'''
    columns = list(row)
    names = ','.join(f'"{column}"' for column in columns)
    updates = ','.join(f'"{column}"=excluded."{column}"' for column in columns)
    connection.execute(f'INSERT INTO {table} ({names}) VALUES ({",".join("?" for column in columns)}) '
                       f'ON CONFLICT DO UPDATE SET {updates}', tuple(row[column] for column in columns))


def writeCheckpoint(store, jobId):
    '''
    Commit only changed records to a portable colony checkpoint.

    Inputs:    <store>  -  Authoritative application store
               <jobId>  -  Colony job whose checkpoint is being retained

    Output:    None; latest committed bounded work unit is portable
    '''
    logging.debug('Persisting portable dataset checkpoint')
    with store.settingLock:
        lock = store.datasetLocks.setdefault(jobId, threading.RLock())
    with lock:
        job = store.job(jobId)
        if not job:
            return
        folder = Path(job['folder']).resolve()
        root = folder.parent.parent
        folder.mkdir(parents=True, exist_ok=True)
        with store.settingLock:
            manifest = root / MANIFEST
            if manifest.is_symlink():
                raise ValueError('Dataset manifest cannot be a symbolic link')
            if manifest.exists():
                document = json.loads(manifest.read_text(encoding='utf-8'))
                datasetId = uuid.UUID(document['datasetId']).hex
                if document.get('version') != 1 or job['datasetId'] and job['datasetId'] != datasetId:
                    raise ValueError('Output root belongs to another dataset')
            else:
                datasetId = job['datasetId'] or uuid.uuid4().hex
                temporary = manifest.with_suffix('.json.part')
                temporary.write_text(json.dumps({'version': 1, 'datasetId': datasetId}), encoding='utf-8')
                temporary.replace(manifest)
            if not job['datasetId']:
                with store.connect() as connection:
                    connection.execute('UPDATE jobs SET datasetId=? WHERE id=?', (datasetId, jobId))
        path = folder / CHECKPOINT
        if path.is_symlink():
            raise ValueError('Portable checkpoint cannot be a symbolic link')
        with store.connect() as source:
            source.execute('BEGIN')
            job = dict(source.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone())
            changes = source.execute('SELECT * FROM datasetChanges WHERE jobId=? ORDER BY sequence', (jobId,)).fetchall()
            highWater = max((row['sequence'] for row in changes), default=0)
            full = not job['portableReady'] or not path.exists()
            destination = path.with_name(path.name + '.part') if full else path
            job['portableReady'] = 1
            if not full and not changes:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as saved:
                    saved.row_factory = sqlite3.Row
                    portable = saved.execute('SELECT * FROM jobs').fetchone()
                    if portable and dict(portable) == job:
                        return
            if full:
                destination.unlink(missing_ok=True)
            try:
                with closing(sqlite3.connect(destination)) as target:
                    target.execute('PRAGMA foreign_keys=ON')
                    with target:
                        if full:
                            for table in TABLES:
                                target.execute(source.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()[0])
                        upsert(target, 'jobs', job)
                        if full:
                            for table in ('properties', 'photos', 'tiles', 'baselines', 'datasetHistory'):
                                for row in source.execute(f'SELECT * FROM {table} WHERE jobId=?', (jobId,)):
                                    upsert(target, table, dict(row))
                            for row in source.execute("SELECT * FROM settings WHERE key IN ('propertyParserVersion','exportSchemaVersion')"):
                                upsert(target, 'settings', dict(row))
                        else:
                            for change in changes:
                                kind, key = change['kind'], change['recordKey']
                                table, column = {'property': ('properties', 'identity'), 'tile': ('tiles', 'id'),
                                                 'baseline': ('baselines', 'pid')}[kind]
                                row = source.execute(f'SELECT * FROM {table} WHERE jobId=? AND {column}=?', (jobId, key)).fetchone()
                                if row:
                                    upsert(target, table, dict(row))
                                else:
                                    target.execute(f'DELETE FROM {table} WHERE jobId=? AND {column}=?', (jobId, key))
                                if kind == 'property':
                                    target.execute('DELETE FROM photos WHERE jobId=? AND identity=?', (jobId, key))
                                    for photo in source.execute('SELECT * FROM photos WHERE jobId=? AND identity=?', (jobId, key)):
                                        upsert(target, 'photos', dict(photo))
                if full:
                    destination.replace(path)
            except Exception:
                if full:
                    destination.unlink(missing_ok=True)
                raise
        with store.connect() as connection:
            connection.execute('DELETE FROM datasetChanges WHERE jobId=? AND sequence<=?', (jobId, highWater))
            connection.execute('UPDATE jobs SET portableReady=1 WHERE id=?', (jobId,))


def importedRows(entry):
    '''Read a validated legacy baseline. Inputs: <entry>. Output: iterator of cleaned rows.'''
    folder = Path(entry['folder'])
    path = folder / 'properties.csv'
    if not path.exists():
        path = folder / 'properties.xlsx'
    for row in readRows(path).values():
        combined = not any(re.match(r'^(Owner|Floor) \d+: .*Gender$', label) for label in row)
        yield compactExportRow(row, peopleCombined=combined)
