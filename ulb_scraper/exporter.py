'''Streaming CSV/XLSX snapshots with relative photo hyperlinks.'''

# Standard Library Imports
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
import shutil
from pathlib import Path
from urllib.parse import quote

# Third Party Imports
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from scraper import isZeroLike, normalizeCellValue
from ulb_scraper import datasets
from ulb_scraper.property_data import compactColumns, compactExportRow, normalizeProperty


def formatFetchedAt(value):
    '''Format a recorded instant in IST. Inputs: <value>. Output: ISO timestamp or -.'''
    logging.debug('Formatting property fetch time in IST')
    if not value or str(value).strip() == '-':
        return '-'
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            raise ValueError('Fetch time has no timezone')
    except (TypeError, ValueError):
        logging.warning('Invalid property fetch timestamp; leaving exported time empty')
        return '-'
    return timestamp.astimezone(timezone(timedelta(hours=5, minutes=30), 'IST')).isoformat(timespec='seconds')


def iterExportRows(connection, jobId, job=None, mobileReport=False):
    '''
    Stream merged property rows without loading the full colony into memory.

    Inputs:    <connection>  -  SQLite snapshot connection
               <jobId>       -  Colony job ID

    Output:    Iterator of flat property dictionaries
    '''
    logging.debug('Streaming checkpoint export rows')
    job = job or dict(connection.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone())
    if job['mode'] == 'update':
        for baseline in connection.execute("""SELECT * FROM baselines WHERE jobId=?
            AND (?=0 OR (mobileTarget=1 AND review='updated')) ORDER BY pid""", (jobId, int(mobileReport))):
            row = compactExportRow(json.loads(baseline['row']), peopleCombined=True)
            oldTimestamp = row.pop('Data Fetched At (UTC)', None)
            row['Data Fetched At (IST)'] = formatFetchedAt(row.get('Data Fetched At (IST)', oldTimestamp))
            row['Data Last Checked At (IST)'] = formatFetchedAt(baseline['lastChecked'])
            row['Data Last Changed At (IST)'] = formatFetchedAt(baseline['lastChanged'])
            row['Update Review'] = datasets.REVIEWLABELS[baseline['review']]
            yield row
    if mobileReport:
        return
    for record in connection.execute("""SELECT * FROM properties WHERE jobId=? AND
        (processingState!='pending' OR retainedForExport=1) AND
        (?!='update' OR NOT EXISTS (SELECT 1 FROM baselines WHERE baselines.jobId=properties.jobId
        AND baselines.pid=properties.pid)) ORDER BY identity""", (jobId, job['mode'])):
        row = propertyExportRow(connection, record, job)
        if job['mode'] == 'update':
            row['Update Review'] = 'Update failed; no previous data' if record['processingState'] == 'error' else 'New property'
        yield row


def propertyExportRow(connection, record, job):
    '''Normalize one checkpoint with its photo links. Inputs: <connection>, <record>, <job>. Output: row.'''
    logging.debug('Preparing resolved property row')
    flatRow = normalizeProperty(json.loads(record['raw']), json.loads(record['detail'] or '{}'), job)
    sources = []
    paths = []
    statuses = []
    for photo in connection.execute('SELECT * FROM photos WHERE jobId=? AND identity=? ORDER BY url',
                                    (record['jobId'], record['identity'])):
        sources.append(photo['url'])
        statuses.append(photo['state'])
        if photo['path']:
            paths.append(photo['path'])
    flatRow['Photo Source URLs'] = ', '.join(sources)
    flatRow['Photo Download Status'] = ', '.join(sorted(set(statuses))) or '-'
    flatRow['Photos'] = ', '.join(paths)
    for photoIndex, path in enumerate(paths, start=1):
        flatRow[f'Photo {photoIndex}'] = path
    flatRow['Property Availability'] = flatRow['Property Availability'] or record['detailState']
    flatRow['Processing Status'] = record['processingState']
    flatRow['Processing Error'] = record['processingError'] or '-'
    flatRow['Data Fetched At (IST)'] = formatFetchedAt(record['fetchedAt'])
    return compactExportRow(flatRow)


def exportJob(store, job, mobileReport=False):
    '''
    Atomically publish bounded-memory CSV and XLSX snapshots of a job.

    Inputs:    <store>  -  Checkpoint store
               <job>    -  Job with an output folder

    Output:    Path to properties.xlsx
    '''
    logging.info(f'Exporting {job["municipalityName"]} / {job["colonyName"]}')
    folder = Path(job['folder'])
    if mobileReport:
        folder = Path(job['mobileReportRoot']) / folder.parent.name / folder.name
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM baselines WHERE jobId=? AND mobileTarget=1 AND review='updated' LIMIT 1",
                                      (job['id'],)).fetchone():
                return None
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'photos').mkdir(exist_ok=True)
    csvPath = folder / 'properties.csv'
    xlsxPath = folder / 'properties.xlsx'
    temporaryCsv = folder / 'properties.csv.part'
    temporaryXlsx = folder / 'properties.xlsx.part'
    workbook = None
    try:
        with store.connect() as connection:
            connection.execute('BEGIN')
            version = connection.execute('SELECT dataVersion FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            available = {'PID', 'Municipality', 'Colony', 'Owner 1: Name', 'Mobile Numbers', 'Data Fetched At (IST)'}
            nonEmpty = set()
            for row in iterExportRows(connection, job['id'], job, mobileReport=mobileReport):
                available.update(row)
                nonEmpty.update(key for key, value in row.items() if not isZeroLike(value))
            columns = compactColumns(available, nonEmpty)
            workbook = Workbook(write_only=True)
            sheetNumber = 0
            rowCount = 0
            sheet = None
            with temporaryCsv.open('w', newline='', encoding='utf-8-sig') as fileObj:
                writer = csv.DictWriter(fileObj, fieldnames=columns)
                writer.writeheader()
                for row in iterExportRows(connection, job['id'], job, mobileReport=mobileReport):
                    if sheet is None or rowCount >= 1048575:
                        sheetNumber += 1
                        sheet = createSheet(workbook, columns, sheetNumber)
                        rowCount = 0
                    if mobileReport:
                        for label, value in row.items():
                            if label.startswith('Photo ') and label[6:].isdigit() and value and value != '-':
                                original = datasets.photoPath(Path(job['folder']), value)
                                destination = datasets.photoPath(folder, value)
                                if original.is_file() and not destination.exists():
                                    destination.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(original, destination)
                    values = {column: normalizeCellValue(row.get(column)) for column in columns}
                    writer.writerow(values)
                    cells = []
                    for column in columns:
                        value = values[column]
                        if isinstance(value, str):
                            value = ''.join(character for character in value
                                            if ord(character) >= 32 or character in '\t\n\r')[:32767]
                        cell = WriteOnlyCell(sheet, value=value)
                        if isinstance(value, str):
                            cell.data_type = 's'
                        if (column.startswith('Photo ') and column[6:].isdigit() or column == 'Photos') and value != '-':
                            link = str(value).split(', ')[0]
                            cell.hyperlink = quote(link, safe='/._-')
                            cell.font = Font(color='007A65', underline='single')
                        cells.append(cell)
                    sheet.append(cells)
                    rowCount += 1
                if sheet is None:
                    sheet = createSheet(workbook, columns, 1)
                sheet.auto_filter.ref = f'A1:{get_column_letter(len(columns))}{rowCount + 1}'
            workbook.save(temporaryXlsx)
        # Offline previews override the job folder and must never touch the real dataset.
        if not mobileReport and Path(job['folder']).resolve() == Path(store.job(job['id'])['folder']).resolve():
            store.persistDataset(job['id'])
        temporaryXlsx.replace(xlsxPath)
        temporaryCsv.replace(csvPath)
        if not mobileReport:
            if job.get('updateScope') == 'missing_mobile':
                exportJob(store, job, mobileReport=True)
            store.exportResult(job['id'], version)
        logging.info(f'Excel snapshot saved: {xlsxPath}')
        return xlsxPath
    except Exception:
        if workbook is not None:
            for sheet in workbook.worksheets:
                try:
                    if not sheet.closed:
                        sheet.close()
                    # Write-only XML files are otherwise retained when save fails.
                    if sheet._writer and Path(sheet._writer.out).exists():
                        sheet._writer.cleanup()
                except Exception:
                    logging.debug('Could not clean an interrupted worksheet', exc_info=True)
            workbook.close()
        temporaryCsv.unlink(missing_ok=True)
        temporaryXlsx.unlink(missing_ok=True)
        raise


def createSheet(workbook, columns, sheetNumber):
    '''Create one formatted worksheet. Inputs: <workbook>, <columns>, <sheetNumber>. Output: worksheet.'''
    logging.debug('Creating export worksheet')
    sheet = workbook.create_sheet('Properties' if sheetNumber == 1 else f'Properties {sheetNumber}')
    sheet.freeze_panes = 'A2'
    for columnIndex, name in enumerate(columns, start=1):
        width = min(45, max(16, len(name) + 2))
        if name.startswith('Owner ') and name.endswith(': Name') or name.startswith('Floor ') and name.endswith(': Occupier Name'):
            width = 65
        sheet.column_dimensions[get_column_letter(columnIndex)].width = width
    cells = []
    for name in columns:
        cell = WriteOnlyCell(sheet, value=name)
        cell.font = Font(color='FFFFFF', bold=True)
        cell.fill = PatternFill('solid', fgColor='116657')
        cells.append(cell)
    sheet.append(cells)
    return sheet
