'''On-demand reference PID comparisons and targeted property recovery.'''

# Standard Library Imports
from contextlib import closing
import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sqlite3

# Third Party Imports
from openpyxl import load_workbook

from scraper import propertyIdentity, slugify
from ulb_scraper import datasets
from ulb_scraper.municipality_export import collectFolders, legacyRows


VERIFICATIONNOTE = ('These PIDs were not found in the saved data. Some may belong to colonies '
                    'that have not been added, or where scraping is still in progress.')


def municipalityKey(value):
    '''Match names conservatively without removing meaningful punctuation.'''
    return ' '.join(str(value or '').split()).casefold()


def pidText(value):
    '''Preserve textual IDs and avoid turning integral numeric cells into decimals.'''
    if isinstance(value, bool):
        raise ValueError('PID cells must contain property IDs')
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError('PID cells cannot contain decimal numbers')
        value = int(value)
    return str(value).strip().upper() if value is not None else ''


def legacyPids(folder):
    '''Stream only saved PID cells; verification does not format property exports.'''
    logging.debug('Reading retained PID column')
    csvPath = folder / 'properties.csv'
    if csvPath.is_symlink() or (folder / 'properties.xlsx').is_symlink():
        raise ValueError('Saved property files cannot be symbolic links')
    if csvPath.exists():
        with csvPath.open(newline='', encoding='utf-8-sig') as stream:
            rows = csv.reader(stream)
            headers = next(rows, [])
            if headers.count('PID') != 1:
                raise ValueError(f'Expected one PID column in {csvPath}')
            column = headers.index('PID')
            for row in rows:
                if any(row):
                    if len(row) <= column or not row[column].strip():
                        raise ValueError(f'A property row has no PID in {csvPath}')
                    yield pidText(row[column])
    else:
        workbook = load_workbook(folder / 'properties.xlsx', read_only=True, data_only=False)
        try:
            for sheet in workbook:
                rows = sheet.iter_rows(values_only=True)
                headers = next(rows, ())
                if headers.count('PID') != 1:
                    raise ValueError(f'Expected one PID column in {folder.name}')
                column = headers.index('PID')
                for row in rows:
                    if any(value is not None for value in row):
                        if len(row) <= column or row[column] is None:
                            raise ValueError(f'A property row has no PID in {folder.name}')
                        yield pidText(row[column])
        finally:
            workbook.close()


def checkedPids(values, progress, current, total):
    '''Check cancellation during large colony scans without one UI event per PID.'''
    for count, pid in enumerate(values, start=1):
        if count % 1000 == 0:
            progress(current, total, f'Verifying saved PIDs: {count:,} records in this colony...')
        yield pidText(pid)


class PidReference:
    '''Store large imported lists and verification results on disk, outside exports.'''

    def __init__(self, home):
        self.path = Path(home) / 'pid-reference.sqlite3'

    def connect(self):
        '''Open the dedicated reference database, never the scraping checkpoint.'''
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def importWorkbook(self, path, progress):
        '''Replace the reference list transactionally after validating workbook cells.'''
        logging.info('Importing reference PID workbook')
        with closing(self.connect()) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS reference (name TEXT, pid TEXT, PRIMARY KEY(name,pid));
                CREATE TABLE IF NOT EXISTS referenceCounts (name TEXT PRIMARY KEY, count INTEGER);
                CREATE TABLE IF NOT EXISTS results (municipalityId TEXT, pid TEXT, state TEXT,
                    message TEXT DEFAULT '', PRIMARY KEY(municipalityId,pid));
                CREATE TABLE IF NOT EXISTS checks (municipalityId TEXT PRIMARY KEY, metadata TEXT);
            ''')
            connection.execute('DELETE FROM reference')
            connection.execute('DELETE FROM referenceCounts')
            connection.execute('DELETE FROM results')
            connection.execute('DELETE FROM checks')
            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                sheet = workbook.worksheets[0]
                rows = sheet.iter_rows()
                headers = next(rows, ())
                names = []
                for cell in headers:
                    if cell.data_type == 'f':
                        raise ValueError('Municipality headers cannot contain formulas')
                    names.append(municipalityKey(cell.value))
                if not any(names):
                    raise ValueError('The first row must contain municipality names')
                for rowNumber, cells in enumerate(rows, start=2):
                    entries = []
                    for index, cell in enumerate(cells):
                        if cell.value is None or not str(cell.value).strip():
                            continue
                        if index >= len(names) or not names[index]:
                            raise ValueError(f'A PID in row {rowNumber} has no municipality header')
                        if cell.data_type == 'f':
                            raise ValueError(f'PID formulas are not supported (row {rowNumber})')
                        pid = pidText(cell.value)
                        if pid in ('-', 'NA', 'N/A') or not re.fullmatch(r'[A-Z0-9-]+', pid):
                            raise ValueError(f'Invalid PID in row {rowNumber}')
                        entries.append((names[index], pid))
                    connection.executemany('INSERT OR IGNORE INTO reference VALUES (?,?)', entries)
                    if rowNumber % 1000 == 0:
                        progress(rowNumber, max(rowNumber, sheet.max_row or rowNumber), 'Reading reference PID list...')
                if not connection.execute('SELECT 1 FROM reference LIMIT 1').fetchone():
                    raise ValueError('The workbook contains no PIDs')
                connection.execute('INSERT INTO referenceCounts SELECT name,COUNT(*) FROM reference GROUP BY name')
            finally:
                workbook.close()
        return self.summary()

    def summary(self, catalog=()):
        '''Return counts only; resolve names again when the portal catalog changes.'''
        if not self.path.exists():
            return []
        matches = {}
        for entry in catalog:
            if entry.get('active', True):
                matches.setdefault(municipalityKey(entry['name']), []).append(entry)
        with closing(self.connect()) as connection:
            rows = connection.execute('SELECT name,count FROM referenceCounts ORDER BY name').fetchall()
        return [dict(row, municipalityId=matches[row['name']][0]['id']
                     if len(matches.get(row['name'], [])) == 1 else None) for row in rows]

    def page(self, municipalityId, offset=0, limit=100):
        '''Read one bounded page for the optional More details view.'''
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute('''SELECT pid,state,message FROM results
                WHERE municipalityId=? AND state!='found' ORDER BY pid LIMIT ? OFFSET ?''',
                (municipalityId, limit, offset))]

    def verify(self, store, municipalityId, name, roots, progress, preserveErrors=False):
        '''Compare indexed checkpoint PIDs and retained legacy files without portal calls.'''
        logging.info('Verifying saved municipality PIDs')
        jobs = [job for job in store.jobs() if job['municipalityId'] == municipalityId]
        folders = collectFolders(jobs, municipalityId, roots)
        queued = {Path(job['folder']).resolve(): job for job in jobs}
        with closing(self.connect()) as comparison, comparison:
            comparison.execute("CREATE TEMP TABLE previousErrors AS SELECT pid,message FROM results WHERE municipalityId=? AND state='failed'", (municipalityId,))
            comparison.execute('DELETE FROM results WHERE municipalityId=?', (municipalityId,))
            comparison.execute('''INSERT INTO results(municipalityId,pid,state)
                SELECT ?,pid,'missing' FROM reference WHERE name=?''', (municipalityId, municipalityKey(name)))
            if not comparison.execute('SELECT 1 FROM results WHERE municipalityId=? LIMIT 1', (municipalityId,)).fetchone():
                raise ValueError('No matching PID reference list was found for this municipality')
            for index, folder in enumerate(folders):
                progress(index, len(folders), f'Verifying saved colony {index + 1:,} / {len(folders):,}...')
                job = queued.get(folder)
                checkpoint = folder / datasets.CHECKPOINT
                if folder.is_symlink() or checkpoint.is_symlink():
                    raise ValueError('Dataset checkpoints cannot be symbolic links')
                if job or checkpoint.exists():
                    source = store.connect() if job else closing(sqlite3.connect(checkpoint.as_uri() + '?mode=ro', uri=True))
                    with source as connection:
                        connection.row_factory = sqlite3.Row
                        connection.execute('PRAGMA trusted_schema=OFF')
                        connection.execute('BEGIN')
                        if not job:
                            saved = connection.execute('SELECT * FROM jobs').fetchall()
                            if len(saved) != 1 or str(saved[0]['municipalityId']) != municipalityId or str(saved[0]['colonyId']) != folder.name.rsplit('_', 1)[-1]:
                                raise ValueError(f'Checkpoint IDs do not match {folder.name}')
                            job = dict(saved[0])
                        for records, state in ((connection.execute("SELECT pid FROM properties WHERE jobId=? AND processingState!='complete'", (job['id'],)), 'pending'),
                                               (connection.execute("SELECT pid FROM properties WHERE jobId=? AND processingState='complete' UNION SELECT pid FROM baselines WHERE jobId=?", (job['id'], job['id'])), 'found')):
                            comparison.executemany("UPDATE results SET state=? WHERE municipalityId=? AND pid=? AND state!='found'",
                                                   ((state, municipalityId, pid) for pid in checkedPids(
                                                       (record['pid'] for record in records), progress, index, len(folders))))
                else:
                    comparison.executemany("UPDATE results SET state='found' WHERE municipalityId=? AND pid=?",
                                           ((municipalityId, pid) for pid in checkedPids(legacyPids(folder), progress, index, len(folders))))
            counts = dict(comparison.execute('SELECT state,COUNT(*) FROM results WHERE municipalityId=? GROUP BY state', (municipalityId,)))
            if preserveErrors:
                comparison.execute("""UPDATE results SET state='failed',message=(SELECT message FROM previousErrors WHERE previousErrors.pid=results.pid)
                    WHERE municipalityId=? AND state!='found' AND pid IN (SELECT pid FROM previousErrors)""", (municipalityId,))
            result = {'municipalityId': municipalityId, 'municipalityName': name, 'roots': list(roots),
                      'found': counts.get('found', 0), 'missing': counts.get('missing', 0), 'pending': counts.get('pending', 0),
                      'checkedAt': datetime.now(timezone.utc).isoformat(), 'colonies': len(folders)}
            comparison.execute('INSERT OR REPLACE INTO checks VALUES (?,?)', (municipalityId, json.dumps(result)))
        progress(1, 1, 'PID verification complete')
        return result

    def result(self, municipalityId):
        '''Read the latest explicit verification context, not a live count.'''
        with closing(self.connect()) as connection:
            row = connection.execute('SELECT metadata FROM checks WHERE municipalityId=?', (municipalityId,)).fetchone()
            if not row:
                raise ValueError('Verify this municipality before fetching missing PIDs')
            return json.loads(row[0])

    def mark(self, municipalityId, pid, state, message=''):
        '''Keep unresolved lookup failures available for review and retry.'''
        with closing(self.connect()) as connection, connection:
            connection.execute('UPDATE results SET state=?,message=? WHERE municipalityId=? AND pid=?',
                               (state, message, municipalityId, pid))


def recoveryJob(store, municipality, colony, roots):
    '''Restore the correct retained colony or create a paused targeted-recovery job.'''
    logging.info('Preparing a colony for targeted PID recovery')
    for job in store.jobs():
        if job['municipalityId'] == municipality['id'] and job['colonyId'] == colony['id']:
            return job
    # Search retained folders before creating one, including renamed display names.
    matches = []
    for root in roots:
        for parent in Path(root).glob(f'*_{municipality["id"]}'):
            matches.extend(folder for folder in parent.glob(f'*_{colony["id"]}') if folder.is_dir())
    if len(set(matches)) > 1:
        raise ValueError('This colony exists in multiple output roots; resolve its destination before fetching')
    folder = matches[0].resolve() if matches else (Path(roots[0]).resolve() /
        f'{slugify(municipality["name"])}_{municipality["id"]}' / f'{slugify(colony["name"])}_{colony["id"]}')
    checkpoint = folder / datasets.CHECKPOINT
    if checkpoint.exists():
        with closing(sqlite3.connect(checkpoint.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            saved = connection.execute('SELECT * FROM jobs').fetchall()
            if len(saved) != 1 or str(saved[0]['municipalityId']) != municipality['id'] or str(saved[0]['colonyId']) != colony['id']:
                raise ValueError('Retained checkpoint does not match the resolved property colony')
            portable = dict(saved[0])
        entry = dict(portable, portable=portable, folder=str(folder))
        jobId = store.importDataset({'datasetId': portable['datasetId'], 'entries': [entry]}, restoreOnly=True)[0]
    else:
        jobId = store.addJobs([(municipality, colony)], roots[0])[0]
        with store.connect() as connection:
            connection.execute('UPDATE jobs SET folder=? WHERE id=?', (str(folder), jobId))
            if (folder / 'properties.csv').exists() or (folder / 'properties.xlsx').exists():
                connection.execute("UPDATE jobs SET mode='update' WHERE id=?", (jobId,))
                for row, unused in legacyRows(folder):
                    connection.execute('INSERT INTO baselines(jobId,pid,row,review) VALUES (?,?,?,?)',
                                       (jobId, str(row['PID']), json.dumps(row), 'unchanged'))
                connection.execute('UPDATE jobs SET baselineCount=(SELECT COUNT(*) FROM baselines WHERE jobId=?) WHERE id=?', (jobId, jobId))
    store.control([jobId], 'pause')
    return store.job(jobId)


def queueProperty(store, job, raw):
    '''Insert a recovery target once, preserving completed work and map coverage.'''
    logging.debug('Checkpointing a targeted PID recovery')
    pid = str(raw['PID']).strip()
    with store.connect() as connection:
        existing = connection.execute('SELECT * FROM properties WHERE jobId=? AND upper(pid)=?', (job['id'], pid.upper())).fetchone()
        baseline = connection.execute('SELECT 1 FROM baselines WHERE jobId=? AND upper(pid)=?', (job['id'], pid.upper())).fetchone()
        if baseline or existing and existing['processingState'] == 'complete':
            return None
        identity = existing['identity'] if existing else propertyIdentity(raw)
        if existing:
            connection.execute("UPDATE properties SET raw=?,detailState='pending',processingState='pending',detail=NULL,attempts=0,retryAt=0,processingError='',refreshToken=0 WHERE jobId=? AND identity=?",
                               (json.dumps(raw), job['id'], identity))
            connection.execute("UPDATE photos SET state='pending',error='' WHERE jobId=? AND identity=? AND state='error'", (job['id'], identity))
        else:
            connection.execute('INSERT INTO properties(jobId,identity,pid,raw,fetchedAt) VALUES (?,?,?,?,?)',
                               (job['id'], identity, pid, json.dumps(raw), datetime.now(timezone.utc).isoformat(timespec='seconds')))
        connection.execute('''UPDATE jobs SET uniqueCount=(SELECT COUNT(*) FROM properties WHERE jobId=?),
            completedCount=(SELECT COUNT(*) FROM properties WHERE jobId=? AND processingState!='pending'),
            dataVersion=dataVersion+1 WHERE id=?''', (job['id'], job['id'], job['id']))
        return identity
