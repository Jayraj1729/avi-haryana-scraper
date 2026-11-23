'''On-demand municipality workbooks from queued and retained colony datasets.'''

# Standard Library Imports
from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import quote

# Third Party Imports
from openpyxl import load_workbook, Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from scraper import normalizeCellValue, slugify
from ulb_scraper import datasets
from ulb_scraper.exporter import createSheet, formatFetchedAt, iterExportRows
from ulb_scraper.property_data import compactColumns, compactExportRow, usable


SHEETROWS = 1048575
EXPORTNOTE = 'All saved properties collected so far are included. Export again to include future updates.'


def legacyRows(folder):
    '''Stream saved CSV or Excel records when a colony has no checkpoint.'''
    logging.debug('Reading retained colony export')
    csvPath = folder / 'properties.csv'
    workbook = None
    stream = None
    try:
        if csvPath.exists():
            stream = csvPath.open(newline='', encoding='utf-8-sig')
            tables = [iter(csv.reader(stream))]
        else:
            workbook = load_workbook(folder / 'properties.xlsx', read_only=True)
            tables = [sheet.iter_rows(values_only=True) for sheet in workbook.worksheets]
        for table in tables:
            headers = [str(value or '').strip() for value in next(table, [])]
            if 'PID' not in headers or '' in headers or len(headers) != len(set(headers)):
                raise ValueError(f'Invalid property headers in {folder}')
            for values in table:
                if not any(value is not None and str(value).strip() for value in values):
                    continue
                if len(values) != len(headers):
                    raise ValueError(f'Invalid property row in {folder}')
                row = dict(zip(headers, values))
                combined = not any(re.match(r'^(Owner|Floor) \d+: .*Gender$', label) for label in row)
                row = compactExportRow(row, peopleCombined=combined)
                oldTimestamp = row.pop('Data Fetched At (UTC)', None)
                row['Data Fetched At (IST)'] = formatFetchedAt(row.get('Data Fetched At (IST)', oldTimestamp))
                yield row, True
    finally:
        if stream:
            stream.close()
        if workbook:
            workbook.close()


def checkpointRows(connection, job, folder):
    '''Read a consistent saved colony snapshot without migrating its database.'''
    logging.debug('Reading municipality export checkpoint')
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA trusted_schema=OFF')
    connection.execute('BEGIN')
    if job is None:
        jobs = connection.execute('SELECT * FROM jobs').fetchall()
        if len(jobs) != 1:
            raise ValueError(f'Expected one colony checkpoint in {folder}')
        job = dict(jobs[0])
    if (str(job['municipalityId']) != folder.parent.name.rsplit('_', 1)[-1] or
            str(job['colonyId']) != folder.name.rsplit('_', 1)[-1]):
        raise ValueError(f'Checkpoint IDs do not match the colony folder: {folder}')
    job = dict(job, folder=str(folder))
    for row in iterExportRows(connection, job['id'], job):
        # Retained baselines remain valid when a later refresh fails. A new
        # failed pipeline should not displace a successful duplicate elsewhere.
        state = connection.execute('''SELECT processingState FROM properties
            WHERE jobId=? AND pid=? AND NOT EXISTS (
                SELECT 1 FROM baselines WHERE jobId=? AND pid=?) LIMIT 1''',
            (job['id'], row.get('PID'), job['id'], row.get('PID'))).fetchone()
        yield row, not state or state['processingState'] != 'error'


def collectFolders(jobs, municipalityId):
    '''Include saved sibling colonies even when they are absent from the queue.'''
    logging.info('Finding saved municipality colony folders')
    roots = {Path(job['folder']).resolve().parent.parent for job in jobs}
    folders = {Path(job['folder']).resolve() for job in jobs}
    for root in roots:
        if not root.exists():
            continue
        for municipality in root.iterdir():
            if not municipality.is_dir() or municipality.is_symlink() or not re.fullmatch(
                    rf'.+_{re.escape(str(municipalityId))}', municipality.name):
                continue
            for colony in municipality.iterdir():
                if colony.is_dir() and not colony.is_symlink() and re.fullmatch(r'.+_\d+', colony.name) and any(
                        (colony / name).exists() for name in (datasets.CHECKPOINT, 'properties.csv', 'properties.xlsx')):
                    folders.add(colony.resolve())
    return sorted(folders)


def photoLinks(row, folder, destination):
    '''Retarget photo links to existing files without copying or downloading them.'''
    logging.debug('Retargeting municipality photo links')
    for label, value in list(row.items()):
        if usable(value) and (label == 'Photos' or re.fullmatch(r'Photo \d+', label)):
            links = [os.path.relpath(datasets.photoPath(folder, path), destination).replace(os.sep, '/')
                     for path in str(value).split(', ')]
            row[label] = ', '.join(links)


def publishWorkbook(temporary, destination, marker, metadata):
    '''Replace the previous managed workbook, restoring it if publication fails.'''
    logging.info('Publishing municipality workbook')
    previous = None
    if marker.exists():
        old = json.loads(marker.read_text(encoding='utf-8'))
        filename = old.get('filename', '')
        if filename != Path(filename).name or not filename.endswith('.xlsx'):
            raise ValueError('Invalid previous municipality export filename')
        previous = marker.parent / filename
    backup = marker.with_suffix('.previous')
    if backup.exists():
        raise ValueError('A previous export recovery file exists; retain it and check the Master folder before exporting again.')
    markerTemporary = marker.with_suffix('.part')
    markerTemporary.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    movedPrevious = False
    published = False
    try:
        if destination.exists():
            raise FileExistsError(f'Export filename already exists: {destination.name}')
        if previous and previous.exists():
            previous.replace(backup)
            movedPrevious = True
        temporary.replace(destination)
        published = True
        markerTemporary.replace(marker)
    except Exception:
        if published:
            destination.unlink()
        if movedPrevious:
            backup.replace(previous)
        raise
    finally:
        markerTemporary.unlink(missing_ok=True)
    if movedPrevious:
        backup.unlink()


def exportMunicipality(store, jobs, destinationRoot, progress=None):
    '''
    Build one deduplicated workbook from every saved colony in a municipality.

    Inputs:    <store>            -  Application store; selected jobs are paused
               <jobs>             -  Queued jobs belonging to one municipality
               <destinationRoot>  -  Root in which Master is created
               [progress]         -  Progress callback

    Output:    Saved workbook path, row count, municipality and IST timestamp
    '''
    logging.info('Exporting saved municipality properties')
    municipalityId = str(jobs[0]['municipalityId'])
    municipalityName = jobs[0]['municipalityName']
    destination = Path(destinationRoot).resolve() / 'Master'
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    filename = f'{slugify(municipalityName)}_{stamp:%Y%m%d_%H%M%S_%f}.xlsx'
    workbookPath = destination / filename
    marker = destination / f'.municipality-{municipalityId}.json'
    folders = collectFolders(jobs, municipalityId)
    queued = {Path(job['folder']).resolve(): job for job in jobs}
    report = progress or (lambda *args: None)
    workbook = Workbook(write_only=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.municipality-', dir=destination) as temporary:
            temporary = Path(temporary)
            with closing(sqlite3.connect(temporary / 'records.sqlite3')) as records:
                records.execute('CREATE TABLE records (pid TEXT PRIMARY KEY,row TEXT,success INTEGER,stamp REAL,colonies TEXT)')
                available = {'PID', 'State', 'Municipality', 'Colony', 'Mobile Numbers', 'Data Fetched At (IST)'}
                nonEmpty = set()
                for index, folder in enumerate(folders):
                    report(index, len(folders) * 2, f'Reading saved colonies: {index + 1} / {len(folders)}')
                    def rowProgress(count):
                        report(index, len(folders) * 2, f'Reading {folder.name}: {count:,} properties')
                    if folder.is_symlink() or any((folder / name).is_symlink() for name in (
                            datasets.CHECKPOINT, 'properties.csv', 'properties.xlsx')):
                        raise ValueError('Colony record files cannot be symbolic links')
                    if folder in queued:
                        source = store.connect()
                        job = queued[folder]
                    elif (folder / datasets.CHECKPOINT).exists():
                        source = closing(sqlite3.connect((folder / datasets.CHECKPOINT).as_uri() + '?mode=ro', uri=True))
                        job = None
                    else:
                        source = None
                    if source:
                        with source as connection:
                            rows = checkpointRows(connection, job, folder)
                            mergeRows(records, rows, folder, destination, available, nonEmpty, rowProgress)
                    else:
                        mergeRows(records, legacyRows(folder), folder, destination, available, nonEmpty, rowProgress)
                    records.commit()
                rowCount = records.execute('SELECT COUNT(*) FROM records').fetchone()[0]
                columns = compactColumns(available, nonEmpty)
                sheet = None
                sheetRows = 0
                sheetNumber = 0
                for index, (serialized, colonies) in enumerate(records.execute('SELECT row,colonies FROM records ORDER BY pid')):
                    if sheet is None or sheetRows >= SHEETROWS:
                        if sheet is not None:
                            sheet.auto_filter.ref = f'A1:{get_column_letter(len(columns))}{sheetRows + 1}'
                        sheetNumber += 1
                        sheet = createSheet(workbook, columns, sheetNumber)
                        sheetRows = 0
                    row = json.loads(serialized)
                    row['Colony'] = ', '.join(json.loads(colonies))
                    appendRow(sheet, columns, row)
                    sheetRows += 1
                    if index % 250 == 0:
                        report(rowCount + index, max(1, rowCount * 2), f'Writing Excel: {index:,} / {rowCount:,} properties')
                if sheet is None:
                    sheet = createSheet(workbook, columns, 1)
                sheet.auto_filter.ref = f'A1:{get_column_letter(len(columns))}{sheetRows + 1}'
                result = {'municipalityId': municipalityId, 'municipalityName': municipalityName,
                          'path': str(workbookPath), 'filename': filename, 'rows': rowCount,
                          'exportedAt': stamp.isoformat(timespec='seconds'), 'colonies': len(folders)}
                summary = workbook.create_sheet('Export details')
                for key, value in [('Municipality', municipalityName), ('Exported at (IST)', result['exportedAt']),
                                   ('Total properties', rowCount), ('Colony datasets', len(folders)), ('Note', EXPORTNOTE)]:
                    summary.append([key, value])
                summary.column_dimensions['A'].width = 24
                summary.column_dimensions['B'].width = 100
                report(0, 0, 'Saving municipality workbook...')
                temporaryWorkbook = temporary / 'workbook.xlsx'
                workbook.save(temporaryWorkbook)
                publishWorkbook(temporaryWorkbook, workbookPath, marker, result)
                report(1, 1, 'Municipality export complete')
                return result
    finally:
        # Release write-only sheet XML even when a source/read/save fails.
        for sheet in workbook.worksheets:
            try:
                if not sheet.closed:
                    sheet.close()
                if sheet._writer and Path(sheet._writer.out).exists():
                    sheet._writer.cleanup()
            except Exception:
                logging.debug('Could not clean interrupted municipality sheet', exc_info=True)
        workbook.close()


def mergeRows(records, rows, folder, destination, available, nonEmpty, progress=None):
    '''Keep the latest successful row per PID and all colony memberships on disk.'''
    logging.debug('Merging municipality property records')
    for index, (row, success) in enumerate(rows):
        if progress and index % 250 == 0:
            progress(index)
        pid = str(row.get('PID') or '').strip()
        if not usable(pid):
            raise ValueError(f'Missing property PID in {folder}')
        row = dict(row)
        photoLinks(row, folder, destination)
        colony = str(row['Colony']) if usable(row.get('Colony')) else folder.name.rsplit('_', 1)[0].replace('_', ' ')
        try:
            timestamp = datetime.fromisoformat(row.get('Data Fetched At (IST)', ''))
            stamp = timestamp.timestamp() if timestamp.tzinfo else 0
        except (ValueError, TypeError):
            stamp = 0
        previous = records.execute('SELECT row,success,stamp,colonies FROM records WHERE pid=?', (pid,)).fetchone()
        colonies = list(dict.fromkeys((json.loads(previous[3]) if previous else []) + [colony]))
        if previous and (int(success), stamp) <= (previous[1], previous[2]):
            records.execute('UPDATE records SET colonies=? WHERE pid=?', (json.dumps(colonies), pid))
            continue
        available.update(row)
        nonEmpty.update(key for key, value in row.items() if usable(value))
        records.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)',
                        (pid, json.dumps(row), int(success), stamp, json.dumps(colonies)))


def appendRow(sheet, columns, row):
    '''Write property cells with photo hyperlinks relative to the Master folder.'''
    cells = []
    for column in columns:
        value = normalizeCellValue(row.get(column))
        if isinstance(value, str):
            value = ''.join(character for character in value if ord(character) >= 32 or character in '\t\n\r')[:32767]
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = 's'
        if (column == 'Photos' or re.fullmatch(r'Photo \d+', column)) and usable(value):
            cell.hyperlink = quote(str(value).split(', ')[0], safe='/._-')
            cell.font = Font(color='007A65', underline='single')
        cells.append(cell)
    sheet.append(cells)
