'''Transactional job state, catalog history, and property checkpoints.'''

# Standard Library Imports
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from scraper import propertyIdentity, slugify
from ulb_scraper import datasets
from ulb_scraper.exporter import formatFetchedAt, iterExportRows, propertyExportRow
from ulb_scraper.property_data import compactExportRow, EXPORTVERSION, PARSERVERSION, publicViewRestricted, usable


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog (
    kind TEXT NOT NULL, parent TEXT NOT NULL, id TEXT NOT NULL,
    name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(kind, parent, id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, municipalityId TEXT NOT NULL, municipalityName TEXT NOT NULL,
    colonyId TEXT NOT NULL, colonyName TEXT NOT NULL, folder TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', desired INTEGER NOT NULL DEFAULT 1,
    boundary TEXT, uniqueCount INTEGER NOT NULL DEFAULT 0,
    detailCount INTEGER NOT NULL DEFAULT 0, photoCount INTEGER NOT NULL DEFAULT 0,
    completedCount INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, retryAt REAL NOT NULL DEFAULT 0,
    tileTotal INTEGER NOT NULL DEFAULT 0, tileDone INTEGER NOT NULL DEFAULT 0,
    coverage REAL NOT NULL DEFAULT 0,
    dataVersion INTEGER NOT NULL DEFAULT 0, exportVersion INTEGER NOT NULL DEFAULT -1,
    lastExport REAL NOT NULL DEFAULT 0, exportError TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    UNIQUE(municipalityId, colonyId)
);
CREATE TABLE IF NOT EXISTS tiles (
    id INTEGER PRIMARY KEY, jobId TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    bounds TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
    page INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
    lastFingerprint TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, retryAt REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL,
    UNIQUE(jobId, bounds)
);
CREATE INDEX IF NOT EXISTS pendingTiles ON tiles(jobId, state, id);
CREATE TABLE IF NOT EXISTS properties (
    jobId TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    identity TEXT NOT NULL, raw TEXT NOT NULL, detail TEXT, fetchedAt TEXT,
    detailState TEXT NOT NULL DEFAULT 'pending',
    retainedForExport INTEGER NOT NULL DEFAULT 0,
    processingState TEXT NOT NULL DEFAULT 'pending', processingError TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, retryAt REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(jobId, identity)
);
CREATE INDEX IF NOT EXISTS pendingDetails ON properties(jobId, detailState);
CREATE TABLE IF NOT EXISTS photos (
    jobId TEXT NOT NULL, identity TEXT NOT NULL, url TEXT NOT NULL,
    path TEXT, state TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(jobId, identity, url),
    FOREIGN KEY(jobId, identity) REFERENCES properties(jobId, identity) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS pendingPhotos ON photos(jobId, state);
CREATE TABLE IF NOT EXISTS baselines (
    jobId TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    pid TEXT NOT NULL, row TEXT NOT NULL, contentHash TEXT NOT NULL DEFAULT '',
    lastChecked TEXT, lastChanged TEXT, seen INTEGER NOT NULL DEFAULT 0,
    review TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(jobId,pid)
);
CREATE TABLE IF NOT EXISTS datasetHistory (
    jobId TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    pid TEXT NOT NULL, row TEXT NOT NULL, PRIMARY KEY(jobId,pid)
);
CREATE TABLE IF NOT EXISTS datasetChanges (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, jobId TEXT NOT NULL,
    kind TEXT NOT NULL, recordKey TEXT NOT NULL,
    UNIQUE(jobId,kind,recordKey)
);
'''


class Store:
    '''SQLite store shared by the UI, colony workers, and workbook writer.'''

    def __init__(self, path):
        self.settingCache = {}
        self.settingLock = threading.RLock()
        self.datasetLocks = {}
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute("CREATE INDEX IF NOT EXISTS unfinishedProperties ON properties(jobId) WHERE processingState='pending'")
            for table, kind, key in [('properties', 'property', 'identity'), ('photos', 'property', 'identity'),
                                     ('tiles', 'tile', 'id'), ('baselines', 'baseline', 'pid')]:
                for operation in ('INSERT', 'UPDATE', 'DELETE'):
                    record = 'OLD' if operation == 'DELETE' else 'NEW'
                    connection.execute(f'DROP TRIGGER IF EXISTS dataset_{table}_{operation}')
                    connection.execute(f'''CREATE TRIGGER dataset_{table}_{operation}
                        AFTER {operation} ON {table}
                        WHEN EXISTS (SELECT 1 FROM jobs WHERE id={record}.jobId) BEGIN
                        DELETE FROM datasetChanges WHERE jobId={record}.jobId
                            AND kind='{kind}' AND recordKey={record}.{key};
                        INSERT INTO datasetChanges(jobId,kind,recordKey)
                        VALUES ({record}.jobId,'{kind}',{record}.{key}); END''')

    def _migrate(self, connection):
        '''Add pipeline checkpoints to earlier databases without discarding data.'''
        additions = {
            'baselines':  {'mobileTarget': 'INTEGER NOT NULL DEFAULT 0'},
            'jobs':       {'updateScope': "TEXT NOT NULL DEFAULT 'all'",
                           'mobileReportRoot': "TEXT NOT NULL DEFAULT ''",
                           'completedCount': 'INTEGER NOT NULL DEFAULT 0',
                           'mode': "TEXT NOT NULL DEFAULT 'scrape'", 'generation': 'INTEGER NOT NULL DEFAULT 0',
                           'datasetId': "TEXT NOT NULL DEFAULT ''", 'portableReady': 'INTEGER NOT NULL DEFAULT 0',
                           'baselineCount': 'INTEGER NOT NULL DEFAULT 0', 'catalogChecked': 'INTEGER NOT NULL DEFAULT 0',
                           'attempts': 'INTEGER NOT NULL DEFAULT 0', 'retryAt': 'REAL NOT NULL DEFAULT 0'},
            'tiles':      {'attempts': 'INTEGER NOT NULL DEFAULT 0', 'retryAt': 'REAL NOT NULL DEFAULT 0',
                           'error': "TEXT NOT NULL DEFAULT ''"},
            'properties': {'processingState': "TEXT NOT NULL DEFAULT 'pending'",
                           'pid': "TEXT NOT NULL DEFAULT ''",
                           'refreshToken': 'INTEGER NOT NULL DEFAULT 0',
                           'tokenRefreshes': 'INTEGER NOT NULL DEFAULT 0',
                           'fetchedAt': 'TEXT',
                           'processingError': "TEXT NOT NULL DEFAULT ''",
                           'retainedForExport': 'INTEGER NOT NULL DEFAULT 0',
                           'attempts': 'INTEGER NOT NULL DEFAULT 0', 'retryAt': 'REAL NOT NULL DEFAULT 0'},
            'photos':     {'error': "TEXT NOT NULL DEFAULT ''"},
        }
        legacy = False
        migrateProperties = False
        for table, columns in additions.items():
            existing = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            for column, declaration in columns.items():
                if column not in existing:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
                    legacy = True
                    if table == 'properties' and column == 'processingState':
                        migrateProperties = True
        connection.execute('CREATE INDEX IF NOT EXISTS pendingProperties ON properties(jobId,processingState)')
        connection.execute('CREATE INDEX IF NOT EXISTS retainedExports ON properties(jobId) WHERE retainedForExport=1')
        connection.execute('CREATE INDEX IF NOT EXISTS propertyPids ON properties(jobId,pid)')
        if legacy:
            logging.info('Migrating existing checkpoints to property pipelines')
            connection.execute('UPDATE jobs SET portableReady=0')
        if migrateProperties:
            connection.execute('''UPDATE properties SET processingState=CASE
                WHEN detailState='error' OR EXISTS (SELECT 1 FROM photos
                    WHERE photos.jobId=properties.jobId AND photos.identity=properties.identity AND state='error')
                THEN 'error' ELSE 'complete' END
                WHERE detailState!='pending' AND NOT EXISTS (
                    SELECT 1 FROM photos WHERE photos.jobId=properties.jobId
                    AND photos.identity=properties.identity AND state='pending')''')
            connection.execute('''UPDATE jobs SET completedCount=(SELECT COUNT(*) FROM properties
                WHERE properties.jobId=jobs.id AND processingState IN ('complete','error')),
                dataVersion=dataVersion+1''')
        version = connection.execute("SELECT value FROM settings WHERE key='exportSchemaVersion'").fetchone()
        if not version or json.loads(version['value']) < EXPORTVERSION:
            connection.execute('UPDATE jobs SET dataVersion=dataVersion+1')
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('exportSchemaVersion',?)", (json.dumps(EXPORTVERSION),))
        version = connection.execute("SELECT value FROM settings WHERE key='propertyParserVersion'").fetchone()
        if not version or json.loads(version['value']) < PARSERVERSION:
            self._upgradeProperties(connection)
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('propertyParserVersion',?)", (json.dumps(PARSERVERSION),))

    def _upgradeProperties(self, connection):
        '''Reclassify verified restrictions and schedule legacy enrichment without losing map work.'''
        affected = set()
        for record in connection.execute('SELECT * FROM properties'):
            raw = json.loads(record['raw'])
            detail = json.loads(record['detail'] or '{}').get('Detail', {})
            if publicViewRestricted(raw):
                payload = {'Detail': {'FetchStatus': 'unavailable', 'FetchError': 'Not available for public view',
                                      'ParseVersion': PARSERVERSION}}
                connection.execute('''UPDATE properties SET detail=?,detailState='unavailable',processingState='pending',
                    attempts=0,retryAt=0,processingError='' WHERE jobId=? AND identity=?''',
                    (json.dumps(payload), record['jobId'], record['identity']))
                photos = connection.execute('SELECT state,error FROM photos WHERE jobId=? AND identity=?',
                                            (record['jobId'], record['identity'])).fetchall()
                if not any(photo['state'] == 'pending' for photo in photos):
                    failed = any(photo['state'] == 'error' for photo in photos)
                    connection.execute('UPDATE properties SET processingState=?,processingError=? WHERE jobId=? AND identity=?',
                                       ('error' if failed else 'complete', ', '.join(photo['error'] for photo in photos if photo['error']),
                                        record['jobId'], record['identity']))
                affected.add(record['jobId'])
            elif raw.get('Token') and detail.get('ParseVersion') == 1 and record['detailState'] == 'ok':
                # Keep the old parsed values available for Excel while the new
                # parser refreshes fields that the earlier parser never saved.
                connection.execute('''UPDATE properties SET detailState='pending',processingState='pending',retainedForExport=1,
                    attempts=0,retryAt=0 WHERE jobId=? AND identity=?''', (record['jobId'], record['identity']))
                affected.add(record['jobId'])
        for jobId in affected:
            connection.execute('''UPDATE jobs SET dataVersion=dataVersion+1,
                completedCount=(SELECT COUNT(*) FROM properties WHERE jobId=? AND processingState!='pending'),
                detailCount=(SELECT COUNT(*) FROM properties WHERE jobId=? AND detailState!='pending') WHERE id=?''',
                               (jobId, jobId, jobId))
            pending = connection.execute("SELECT 1 FROM properties WHERE jobId=? AND processingState='pending' LIMIT 1", (jobId,)).fetchone()
            if pending:
                connection.execute('''UPDATE jobs SET status=CASE WHEN desired=1 THEN 'queued' ELSE 'paused' END,
                    error='' WHERE id=?''', (jobId,))
            else:
                failures = connection.execute('''SELECT (SELECT COUNT(*) FROM properties WHERE jobId=? AND processingState='error') +
                    (SELECT COUNT(*) FROM tiles WHERE jobId=? AND state='failed')''', (jobId, jobId)).fetchone()[0]
                connection.execute('''UPDATE jobs SET status=?,error=? WHERE id=? AND status IN ('complete','complete_warnings')''',
                                   ('complete_warnings' if failures else 'complete',
                                    f'{failures} records or tiles need retry' if failures else '', jobId))

    @contextmanager
    def connect(self):
        '''
        Open an independent connection with automatic commit or rollback.

        Inputs:    None

        Output:    Context manager yielding a SQLite connection
        '''
        logging.debug('Opening checkpoint connection')
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def setting(self, key, default=None):
        '''Read a persisted preference. Inputs: <key>, [default]. Output: value.'''
        logging.debug(f'Reading preference {key}')
        cachedKeys = ('parallelRequests', 'requestDelayMs')
        with self.settingLock:
            cached = self.settingCache.get(key)
            if key in cachedKeys and cached and time.monotonic() - cached[0] < 1:
                return cached[1]
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        value = json.loads(row['value']) if row else default
        if key in cachedKeys:
            with self.settingLock:
                self.settingCache[key] = (time.monotonic(), value)
        return value

    def setSetting(self, key, value):
        '''Save a preference. Inputs: <key>, <value>. Output: None.'''
        logging.debug(f'Saving preference {key}')
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                               (key, json.dumps(value)))
        with self.settingLock:
            self.settingCache[key] = (time.monotonic(), value)

    def updateCatalog(self, kind, parent, entries):
        '''Replace current options and retain retired ones. Inputs: <kind>, <parent>, <entries>.'''
        logging.debug(f'Updating {kind} catalog for {parent}')
        with self.connect() as connection:
            connection.execute('UPDATE catalog SET active=0 WHERE kind=? AND parent=?', (kind, parent))
            for entry in entries:
                identity = str(entry['Value'])
                name = str(entry['Text']).strip()
                if not identity or identity in ('0', '-1') or not name:
                    continue
                connection.execute('INSERT OR REPLACE INTO catalog VALUES (?,?,?,?,1)',
                                   (kind, parent, identity, name))

    def catalog(self, kind, parent=''):
        '''Read options including retired entries. Inputs: <kind>, [parent]. Output: list of dictionaries.'''
        logging.debug(f'Reading {kind} catalog')
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM catalog WHERE kind=? AND parent=? ORDER BY name COLLATE NOCASE',
                (kind, str(parent)))]

    def addJobs(self, pairs, rootFolder, replaceMunicipality=None, replaceAll=False, replaceMunicipalities=()):
        '''
        Atomically replace scoped jobs or add new pairs without duplicate jobs.

        Inputs:    <pairs>                -  Municipality/colony dictionaries
                   <rootFolder>           -  Export root selected by the user
                   [replaceMunicipality]  -  Municipality whose jobs are reset
                   [replaceAll]           -  Reset all jobs before insertion
                   [replaceMunicipalities] - Additional municipalities to reset

        Output:    List of newly created job IDs
        '''
        logging.info(f'Adding {len(pairs)} colony jobs')
        identifiers = []
        with self.connect() as connection:
            if replaceAll:
                connection.execute('DELETE FROM jobs')
            elif replaceMunicipality:
                connection.execute('DELETE FROM jobs WHERE municipalityId=?', (replaceMunicipality,))
            if not replaceAll:
                connection.executemany('DELETE FROM jobs WHERE municipalityId=?',
                                       [(identity,) for identity in replaceMunicipalities])
            for municipality, colony in pairs:
                identity = uuid.uuid4().hex
                folder = (Path(rootFolder).resolve() /
                          f'{slugify(municipality["name"])}_{municipality["id"]}' /
                          f'{slugify(colony["name"])}_{colony["id"]}')
                cursor = connection.execute('''INSERT OR IGNORE INTO jobs
                    (id, municipalityId, municipalityName, colonyId, colonyName, folder, created)
                    VALUES (?,?,?,?,?,?,?)''',
                    (identity, municipality['id'], municipality['name'], colony['id'],
                     colony['name'], str(folder), time.time()))
                if cursor.rowcount:
                    identifiers.append(identity)
            connection.execute('DELETE FROM datasetChanges WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=datasetChanges.jobId)')
        return identifiers

    def jobs(self):
        '''Read jobs and live scan coverage. Inputs: None. Output: list of dictionaries.'''
        logging.debug('Reading live job summaries')
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM jobs ORDER BY created')
            return [dict(row) for row in rows]

    def job(self, jobId):
        '''Read one job. Inputs: <jobId>. Output: dictionary or None.'''
        logging.debug('Reading job checkpoint')
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone()
            return dict(row) if row else None

    def control(self, jobIds, action):
        '''
        Change desired state without losing pending work.

        Inputs:    <jobIds>  -  Jobs to update
                   <action>  -  start or pause

        Output:    None
        '''
        logging.info(f'{action.capitalize()} requested for {len(jobIds)} jobs')
        with self.connect() as connection:
            for jobId in jobIds:
                if action == 'start':
                    connection.execute('''UPDATE jobs SET desired=1, status='queued', error=''
                        WHERE id=? AND status != 'complete' ''', (jobId,))
                    connection.execute('UPDATE jobs SET attempts=0,retryAt=0 WHERE id=?', (jobId,))
                    connection.execute('''UPDATE properties SET processingState='pending', processingError='',
                        attempts=0,retryAt=0 WHERE jobId=? AND processingState='error' ''', (jobId,))
                    connection.execute("UPDATE properties SET detailState='pending', detail=NULL WHERE jobId=? AND detailState='error'", (jobId,))
                    connection.execute("UPDATE photos SET state='pending', error='' WHERE jobId=? AND state='error'", (jobId,))
                    connection.execute("UPDATE tiles SET state='pending', attempts=0, retryAt=0, error='' WHERE jobId=? AND state='failed'", (jobId,))
                    connection.execute('''UPDATE jobs SET completedCount=(SELECT COUNT(*) FROM properties
                        WHERE properties.jobId=jobs.id AND processingState IN ('complete','error')) WHERE id=?''', (jobId,))
                elif action == 'pause':
                    connection.execute('''UPDATE jobs SET desired=0, status='paused'
                        WHERE id=? AND status != 'complete' ''', (jobId,))
                else:
                    raise ValueError(f'Unknown control action: {action}')

    def deleteJobs(self, jobIds, progress=None):
        '''Remove checkpoints while retaining exports. Inputs: <jobIds>. Output: None.'''
        logging.info(f'Removing {len(jobIds)} jobs')
        for index, jobId in enumerate(jobIds):
            if progress:
                progress(index, len(jobIds), 'Saving portable checkpoints before removal')
            self.persistDataset(jobId)
        if progress:
            progress(0, 0, 'Removing saved queue entries')
        with self.connect() as connection:
            connection.executemany('DELETE FROM jobs WHERE id=?', [(jobId,) for jobId in jobIds])
            connection.executemany('DELETE FROM datasetChanges WHERE jobId=?', [(jobId,) for jobId in jobIds])

    def persistDataset(self, jobId):
        '''Retain committed colony work independently of the queue. Inputs: <jobId>. Output: None.'''
        datasets.writeCheckpoint(self, jobId)

    def importDataset(self, inventory, restart=False, catalogChecked=False, restoreOnly=False,
                      mobileOnly=False, reportRoot='', progress=None):
        '''
        Recreate update jobs from validated legacy or portable colony folders.

        Inputs:    <inventory>       -  Validated dataset root and colony inventory
                   [restart]         -  Start a new generation instead of resuming
                   [catalogChecked]  -  Current portal IDs have been verified
                   [restoreOnly]     -  Ordinary re-add retains the previous job mode

        Output:    List of affected queue IDs
        '''
        logging.info('Importing confirmed update dataset')
        identifiers = []
        with self.connect() as connection:
            for index, entry in enumerate(inventory['entries']):
                if progress:
                    progress(index, len(inventory['entries']), f"Loading {entry['municipalityName']} / {entry['colonyName']}")
                existing = connection.execute('SELECT * FROM jobs WHERE municipalityId=? AND colonyId=?',
                                              (entry['municipalityId'], entry['colonyId'])).fetchone()
                if existing and Path(existing['folder']).resolve() != Path(entry['folder']).resolve():
                    raise ValueError('A selected colony is queued in another output folder; remove that queue entry before importing this location')
                if existing and existing['mode'] == 'update' and existing['status'] not in ('complete', 'complete_warnings') and not restart:
                    if (existing['updateScope'] == 'missing_mobile') != mobileOnly:
                        raise ValueError('Resume the existing update with its original scope, or choose Start new update')
                    identifiers.append(existing['id'])
                    continue
                jobId = existing['id'] if existing else uuid.uuid4().hex
                if not existing:
                    portable = entry['portable']
                    if portable:
                        job = dict(portable, id=jobId, folder=entry['folder'], portableReady=0,
                                   desired=1, status='queued', catalogChecked=int(catalogChecked))
                        if portable.get('updateScope') == 'missing_mobile':
                            reportName = portable.get('mobileReportRoot', '').replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
                            if not reportName.casefold().startswith(('updated-mobilenos-', 'mobile-updates-')):
                                raise ValueError('Missing-mobile checkpoint has an invalid report folder')
                            job['mobileReportRoot'] = str(Path(entry['folder']).parent.parent / reportName)
                        if restoreOnly:
                            job['desired'] = portable['desired']
                            job['status'] = portable['status']
                        datasets.upsert(connection, 'jobs', job)
                        with closing(sqlite3.connect((Path(entry['folder']) / datasets.CHECKPOINT).as_uri() + '?mode=ro', uri=True)) as original:
                            original.row_factory = sqlite3.Row
                            for table in ('properties', 'photos', 'tiles', 'baselines', 'datasetHistory'):
                                for record in original.execute(f'SELECT * FROM {table} WHERE jobId=?', (portable['id'],)):
                                    row = dict(record, jobId=jobId)
                                    if table == 'tiles':
                                        row.pop('id')
                                    datasets.upsert(connection, table, row)
                        if restoreOnly or portable['mode'] == 'update' and portable['status'] not in ('complete', 'complete_warnings') and not restart:
                            if not restoreOnly and (portable.get('updateScope', 'all') == 'missing_mobile') != mobileOnly:
                                raise ValueError('Resume the existing update with its original scope, or choose Start new update')
                            identifiers.append(jobId)
                            continue
                    else:
                        connection.execute('''INSERT INTO jobs(id,municipalityId,municipalityName,colonyId,colonyName,
                            folder,created,datasetId) VALUES (?,?,?,?,?,?,?,?)''',
                            (jobId, entry['municipalityId'], entry['municipalityName'], entry['colonyId'],
                             entry['colonyName'], entry['folder'], time.time(), inventory['datasetId']))
                        for row in datasets.importedRows(entry):
                            checked = row.get('Data Last Checked At (IST)', row.get('Data Fetched At (IST)', row.get('Data Fetched At (UTC)')))
                            changed = row.get('Data Last Changed At (IST)')
                            connection.execute('''INSERT INTO baselines(jobId,pid,row,contentHash,lastChecked,lastChanged)
                                VALUES (?,?,?,?,?,?)''', (jobId, row['PID'], json.dumps(row),
                                datasets.contentHash(row, entry['folder']), checked, changed))
                self._prepareUpdate(connection, jobId, catalogChecked, mobileOnly, reportRoot)
                identifiers.append(jobId)
        return identifiers

    def _prepareUpdate(self, connection, jobId, catalogChecked, mobileOnly=False, reportRoot=''):
        '''Retain the latest baseline, then begin a fresh map generation.'''
        job = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone())
        if job['mode'] != 'update':
            for row in iterExportRows(connection, jobId, job):
                if not row.get('PID') or row['PID'] == '-':
                    raise ValueError('Existing checkpoint contains a property without PID; review before updating')
                datasets.upsert(connection, 'baselines', {'jobId': jobId, 'pid': row['PID'],
                    'row': json.dumps(row), 'contentHash': datasets.contentHash(row, job['folder']),
                    'lastChecked': row.get('Data Fetched At (IST)'), 'lastChanged': None,
                    'seen': 0, 'review': 'pending'})
        connection.execute('UPDATE baselines SET mobileTarget=0 WHERE jobId=?', (jobId,))
        if mobileOnly:
            targets = [(jobId, baseline['pid']) for baseline in connection.execute(
                'SELECT pid,row FROM baselines WHERE jobId=?', (jobId,))
                if not usable(compactExportRow(json.loads(baseline['row']), peopleCombined=True).get('Mobile Numbers'))]
            connection.executemany('UPDATE baselines SET mobileTarget=1 WHERE jobId=? AND pid=?', targets)
        connection.execute('UPDATE jobs SET updateScope=?,mobileReportRoot=? WHERE id=?',
                           ('missing_mobile' if mobileOnly else 'all', reportRoot if mobileOnly else '', jobId))
        connection.execute('DELETE FROM datasetHistory WHERE jobId=?', (jobId,))
        connection.execute('INSERT INTO datasetHistory SELECT jobId,pid,row FROM baselines WHERE jobId=?', (jobId,))
        connection.execute("UPDATE baselines SET seen=0,review='pending' WHERE jobId=?", (jobId,))
        if mobileOnly:
            connection.execute("UPDATE baselines SET review='not_selected' WHERE jobId=? AND mobileTarget=0", (jobId,))
        connection.execute('DELETE FROM properties WHERE jobId=?', (jobId,))
        connection.execute('DELETE FROM tiles WHERE jobId=?', (jobId,))
        connection.execute('''UPDATE jobs SET mode='update',generation=generation+1,portableReady=0,
            status='queued',desired=1,boundary=NULL,uniqueCount=0,completedCount=0,detailCount=0,photoCount=0,
            attempts=0,retryAt=0,tileTotal=0,tileDone=0,coverage=0,error='',exportError='',
            dataVersion=dataVersion+1,catalogChecked=?,
            baselineCount=(SELECT COUNT(*) FROM baselines WHERE jobId=?) WHERE id=?''',
                           (int(catalogChecked), jobId, jobId))
        if mobileOnly and not connection.execute(
                'SELECT 1 FROM baselines WHERE jobId=? AND mobileTarget=1 LIMIT 1', (jobId,)).fetchone():
            connection.execute("UPDATE jobs SET status='complete',desired=0,coverage=1 WHERE id=?", (jobId,))
    def finishUpdate(self, jobId):
        '''Retain unseen records and flag absence only after a successful scan. Inputs: <jobId>. Output: None.'''
        logging.debug('Resolving update generation review states')
        with self.connect() as connection:
            failed = connection.execute("SELECT 1 FROM tiles WHERE jobId=? AND state!='done' AND state!='split' LIMIT 1", (jobId,)).fetchone()
            if not failed:
                connection.execute("""UPDATE baselines SET review='not_found' WHERE jobId=? AND seen=0
                    AND ((SELECT updateScope FROM jobs WHERE id=?)!='missing_mobile' OR mobileTarget=1)""", (jobId, jobId))
            connection.execute('UPDATE jobs SET dataVersion=dataVersion+1 WHERE id=?', (jobId,))

    def setStatus(self, jobId, status, error='', desired=None):
        '''Update worker status. Inputs: <jobId>, <status>, [error], [desired]. Output: None.'''
        logging.debug(f'Job status: {status}')
        with self.connect() as connection:
            if desired is None:
                connection.execute('UPDATE jobs SET status=?, error=? WHERE id=? AND desired=1',
                                   (status, error, jobId))
            else:
                connection.execute('UPDATE jobs SET status=?, error=?, desired=? WHERE id=?',
                                   (status, error, desired, jobId))

    def waitForLogin(self):
        '''Suspend all desired jobs after session expiry. Inputs: None. Output: None.'''
        logging.info('Suspending active jobs for login')
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status='waiting_login' WHERE desired=1")

    def initializeTiles(self, jobId, wkt, bounds):
        '''Commit geometry and initial queue together. Inputs: <jobId>, <wkt>, <bounds>.'''
        logging.debug('Initializing colony scan checkpoint')
        bounds = list(bounds)
        if not bounds:
            raise ValueError('Colony boundary produced no scan tiles')
        with self.connect() as connection:
            connection.execute('UPDATE jobs SET boundary=?, tileTotal=?, attempts=0, retryAt=0 WHERE id=?',
                               (wkt, len(bounds), jobId))
            connection.executemany('INSERT OR IGNORE INTO tiles (jobId,bounds,weight) VALUES (?,?,?)',
                                   [(jobId, json.dumps(tile), 1 / len(bounds)) for tile in bounds])

    def pendingTile(self, jobId):
        '''Read the next unfinished tile. Inputs: <jobId>. Output: dictionary or None.'''
        logging.debug('Reading pending map tile')
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tiles WHERE jobId=? AND state='pending' ORDER BY id LIMIT 1",
                                     (jobId,)).fetchone()
            return dict(row) if row else None

    def saveTile(self, jobId, tile, properties, photoUrls, children=None, nextPage=None, fingerprint=''):
        '''
        Commit properties, photos, and tile advancement as one transaction.

        Inputs:    <jobId>       -  Owning job ID
                   <tile>        -  Pending tile checkpoint
                   <properties>  -  Accepted marker records
                   <photoUrls>   -  Function extracting property photo URLs
                   [children]    -  Dense tile subdivisions
                   [nextPage]    -  Next page for a minimal dense tile
                   [fingerprint] -  Response identities for pagination validation

        Output:    None
        '''
        logging.debug(f'Committing map tile {tile["id"]}')
        newCount = 0
        with self.connect() as connection:
            current = connection.execute('SELECT state, page FROM tiles WHERE id=?', (tile['id'],)).fetchone()
            if not current or current['state'] != 'pending' or current['page'] != tile['page']:
                return
            job = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone())
            for propertyInfo in properties:
                identity = propertyIdentity(propertyInfo)
                pid = str(propertyInfo.get('PID') or propertyInfo.get('PIDNDC') or '')
                if not identity or identity == 'None':
                    raise ValueError('Property record has no stable identity')
                mode = job['mode']
                if job['updateScope'] == 'missing_mobile' and not connection.execute(
                        'SELECT 1 FROM baselines WHERE jobId=? AND pid=? AND mobileTarget=1', (jobId, pid)).fetchone():
                    continue
                if mode == 'update':
                    if not pid:
                        raise ValueError('Update map record has no PID; tile retained for review')
                    duplicate = connection.execute('SELECT identity FROM properties WHERE jobId=? AND pid=?', (jobId, pid)).fetchone()
                    if duplicate and duplicate['identity'] != identity:
                        raise ValueError('Map returned multiple internal records for the same PID; tile retained for review')
                    connection.execute('UPDATE baselines SET seen=1 WHERE jobId=? AND pid=?', (jobId, pid))
                cursor = connection.execute('INSERT OR IGNORE INTO properties (jobId,identity,pid,raw,fetchedAt) VALUES (?,?,?,?,?)',
                                            (jobId, identity, pid, json.dumps(propertyInfo), datetime.now(timezone.utc).isoformat(timespec='seconds')))
                newCount += cursor.rowcount
                for url in ([] if job['updateScope'] == 'missing_mobile' else photoUrls(propertyInfo)):
                    connection.execute('INSERT OR IGNORE INTO photos (jobId,identity,url) VALUES (?,?,?)',
                                       (jobId, identity, url))
            if children:
                connection.execute("UPDATE tiles SET state='split' WHERE id=?", (tile['id'],))
                connection.executemany('INSERT OR IGNORE INTO tiles (jobId,bounds,depth,weight) VALUES (?,?,?,?)',
                    [(jobId, json.dumps(child), tile['depth'] + 1, tile['weight'] / len(children)) for child in children])
                connection.execute('UPDATE jobs SET tileTotal=tileTotal+? WHERE id=?', (len(children) - 1, jobId))
            elif nextPage:
                connection.execute('UPDATE tiles SET page=?, lastFingerprint=? WHERE id=?',
                                   (nextPage, fingerprint, tile['id']))
            else:
                connection.execute("UPDATE tiles SET state='done' WHERE id=?", (tile['id'],))
                connection.execute('UPDATE jobs SET tileDone=tileDone+1, coverage=coverage+? WHERE id=?',
                                   (tile['weight'], jobId))
            connection.execute("UPDATE tiles SET attempts=0,retryAt=0,error='' WHERE id=?", (tile['id'],))
            connection.execute('UPDATE jobs SET uniqueCount=uniqueCount+?, dataVersion=dataVersion+? WHERE id=?',
                               (newCount, newCount, jobId))

    def pendingDetail(self, jobId):
        '''Read a property awaiting details. Inputs: <jobId>. Output: dictionary or None.'''
        logging.debug('Reading pending property details')
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM properties WHERE jobId=? AND detailState='pending' LIMIT 1",
                                     (jobId,)).fetchone()
            return dict(row) if row else None

    def retainedExportJobs(self):
        '''Read jobs retaining earlier exports during refresh. Inputs: None. Output: set of job IDs.'''
        logging.debug('Reading jobs with retained export rows')
        with self.connect() as connection:
            return {row['jobId'] for row in connection.execute('SELECT DISTINCT jobId FROM properties WHERE retainedForExport=1')}

    def saveDetail(self, jobId, identity, detail, photoUrls, state='ok'):
        '''Commit parsed details and discovered photos. Inputs: <jobId>, <identity>, <detail>, <photoUrls>, [state].'''
        logging.debug('Committing property details')
        with self.connect() as connection:
            cursor = connection.execute("""UPDATE properties SET detail=?, detailState=?,
                fetchedAt=CASE WHEN ?='ok' THEN ? ELSE fetchedAt END, attempts=0, retryAt=0
                WHERE jobId=? AND identity=? AND detailState='pending'""",
                (json.dumps(detail), state, state, datetime.now(timezone.utc).isoformat(timespec='seconds'), jobId, identity))
            for url in photoUrls:
                connection.execute('INSERT OR IGNORE INTO photos (jobId,identity,url) VALUES (?,?,?)',
                                   (jobId, identity, url))
            connection.execute('UPDATE jobs SET detailCount=detailCount+?, dataVersion=dataVersion+? WHERE id=?',
                               (cursor.rowcount, cursor.rowcount, jobId))

    def pendingPhoto(self, jobId):
        '''Read the next photo to download. Inputs: <jobId>. Output: dictionary or None.'''
        logging.debug('Reading pending photo')
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM photos WHERE jobId=? AND state='pending' LIMIT 1", (jobId,)).fetchone()
            return dict(row) if row else None

    def pendingProperty(self, jobId):
        '''Read the next unfinished property pipeline. Inputs: <jobId>. Output: dictionary or None.'''
        logging.debug('Reading pending property pipeline')
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM properties WHERE jobId=? AND processingState='pending' ORDER BY rowid LIMIT 1", (jobId,)).fetchone()
            return dict(row) if row else None

    def requestTokenRefresh(self, jobId, identity):
        '''Bound retries for stale marker access. Inputs: <jobId>, <identity>. Output: bool.'''
        logging.info('Queuing a property token refresh')
        with self.connect() as connection:
            cursor = connection.execute('''UPDATE properties SET refreshToken=1,tokenRefreshes=tokenRefreshes+1,retryAt=0
                WHERE jobId=? AND identity=? AND processingState='pending' AND tokenRefreshes<2''', (jobId, identity))
            return bool(cursor.rowcount)

    def saveRefreshedMarker(self, jobId, identity, marker, photoUrls):
        '''Replace rejected marker credentials and restart its detail/photo stages. Inputs: <jobId>, <identity>, <marker>, <photoUrls>. Output: None.'''
        logging.info('Saving a refreshed property marker')
        with self.connect() as connection:
            record = connection.execute("SELECT * FROM properties WHERE jobId=? AND identity=? AND processingState='pending'",
                                        (jobId, identity)).fetchone()
            if not record:
                return
            removedPhotos = 0
            if not record['retainedForExport']:
                removedPhotos = connection.execute("SELECT COUNT(*) FROM photos WHERE jobId=? AND identity=? AND state IN ('ok','missing')",
                                                   (jobId, identity)).fetchone()[0]
                connection.execute('DELETE FROM photos WHERE jobId=? AND identity=?', (jobId, identity))
            connection.execute('''UPDATE properties SET raw=?,detailState='pending',refreshToken=0,fetchedAt=?,
                retryAt=0,processingError='' WHERE jobId=? AND identity=? AND processingState='pending' ''',
                               (json.dumps(marker), datetime.now(timezone.utc).isoformat(timespec='seconds'), jobId, identity))
            connection.executemany('INSERT OR IGNORE INTO photos(jobId,identity,url) VALUES (?,?,?)',
                                   [(jobId, identity, url) for url in photoUrls])
            connection.execute('''UPDATE jobs SET dataVersion=dataVersion+1,detailCount=MAX(0,detailCount-?),
                photoCount=MAX(0,photoCount-?) WHERE id=?''',
                               (int(record['detailState'] != 'pending'), removedPhotos, jobId))

    def propertyPhotos(self, jobId, identity):
        '''Read one property's photo checkpoints. Inputs: <jobId>, <identity>. Output: list.'''
        logging.debug('Reading property photo checkpoints')
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM photos WHERE jobId=? AND identity=? ORDER BY url', (jobId, identity))]

    def finishProperty(self, jobId, identity):
        '''Commit completion only when details and all photos are resolved. Inputs: <jobId>, <identity>.'''
        logging.debug('Completing property pipeline')
        with self.connect() as connection:
            record = connection.execute('SELECT * FROM properties WHERE jobId=? AND identity=?', (jobId, identity)).fetchone()
            if not record or record['processingState'] != 'pending' or record['detailState'] == 'pending':
                return False
            photos = connection.execute('SELECT state,error FROM photos WHERE jobId=? AND identity=?', (jobId, identity)).fetchall()
            if any(photo['state'] == 'pending' for photo in photos):
                return False
            failed = record['detailState'] == 'error' or any(photo['state'] == 'error' for photo in photos)
            detailError = json.loads(record['detail'] or '{}').get('Detail', {}).get('FetchError', '')
            message = ', '.join(item for item in [detailError, *[photo['error'] for photo in photos]] if item) if failed else ''
            connection.execute('''UPDATE properties SET processingState=?, processingError=?, retainedForExport=0,attempts=0,retryAt=0
                WHERE jobId=? AND identity=?''', ('error' if failed else 'complete', message, jobId, identity))
            connection.execute('UPDATE jobs SET completedCount=completedCount+1,dataVersion=dataVersion+1 WHERE id=?', (jobId,))
            job = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (jobId,)).fetchone())
            current = connection.execute('SELECT * FROM properties WHERE jobId=? AND identity=?', (jobId, identity)).fetchone()
            row = propertyExportRow(connection, current, job)
            pid = row['PID']
            if pid and pid != '-':
                baseline = connection.execute('SELECT * FROM baselines WHERE jobId=? AND pid=?', (jobId, str(pid))).fetchone()
                if failed:
                    if baseline:
                        connection.execute("UPDATE baselines SET seen=1,review='error' WHERE jobId=? AND pid=?", (jobId, str(pid)))
                elif baseline and job['updateScope'] == 'missing_mobile':
                    previous = json.loads(baseline['row'])
                    found = usable(row.get('Mobile Numbers'))
                    if found:
                        for label, value in row.items():
                            if 'mobile' in label.lower() or 'phone' in label.lower():
                                if usable(value):
                                    previous[label] = value
                        previous['Data Fetched At (IST)'] = row['Data Fetched At (IST)']
                    connection.execute('''UPDATE baselines SET row=?,seen=1,review=?,lastChecked=?,
                        lastChanged=CASE WHEN ? THEN ? ELSE lastChanged END,
                        contentHash=CASE WHEN ? THEN ? ELSE contentHash END WHERE jobId=? AND pid=?''',
                        (json.dumps(previous), 'updated' if found else 'unchanged', current['fetchedAt'],
                         found, current['fetchedAt'], found,
                         datasets.contentHash(previous, job['folder']) if found else '', jobId, str(pid)))
                elif baseline and job['mode'] == 'update' and record['detailState'] == 'unavailable':
                    connection.execute("UPDATE baselines SET seen=1,review='unavailable',lastChecked=? WHERE jobId=? AND pid=?",
                                       (current['fetchedAt'], jobId, str(pid)))
                else:
                    digest = datasets.contentHash(row, job['folder'])
                    changed = not baseline or digest != baseline['contentHash']
                    changedAt = current['fetchedAt'] if changed else baseline['lastChanged']
                    review = 'new' if not baseline else 'updated' if changed else 'unchanged'
                    datasets.upsert(connection, 'baselines', {'jobId': jobId, 'pid': str(pid), 'row': json.dumps(row),
                        'contentHash': digest, 'lastChecked': current['fetchedAt'], 'lastChanged': changedAt,
                        'seen': 1, 'review': review})
            return True

    def propertyRetry(self, jobId, identity, message, delay):
        '''Persist attempts and retry timing for the current property. Inputs: <jobId>, <identity>, <message>, <delay>.'''
        logging.debug('Checkpointing property retry')
        with self.connect() as connection:
            connection.execute('''UPDATE properties SET attempts=attempts+1,retryAt=?,processingError=?
                WHERE jobId=? AND identity=?''', (time.time() + delay, message, jobId, identity))

    def photoError(self, jobId, identity, url, message):
        '''Retain a permanently failed photo for explicit retry. Inputs: <jobId>, <identity>, <url>, <message>.'''
        logging.warning('Recording a photo that needs retry')
        with self.connect() as connection:
            connection.execute("UPDATE photos SET state='error',error=? WHERE jobId=? AND identity=? AND url=?",
                               (message, jobId, identity, url))
            connection.execute('UPDATE properties SET attempts=0,retryAt=0 WHERE jobId=? AND identity=?',
                               (jobId, identity))
            connection.execute('UPDATE jobs SET dataVersion=dataVersion+1 WHERE id=?', (jobId,))

    def tileRetry(self, jobId, tileId, message, delay, terminal=False):
        '''Retain failed map coverage for resume and explicit retry. Inputs: <jobId>, <tileId>, <message>, <delay>, [terminal].'''
        logging.warning('Checkpointing map request failure')
        with self.connect() as connection:
            connection.execute('UPDATE tiles SET attempts=attempts+1,retryAt=?,error=?,state=? WHERE id=? AND jobId=?',
                               (time.time() + delay, message, 'failed' if terminal else 'pending', tileId, jobId))

    def jobRetry(self, jobId, message, delay):
        '''Persist preparation backoff before map tiles exist. Inputs: <jobId>, <message>, <delay>.'''
        logging.debug('Checkpointing colony preparation retry')
        with self.connect() as connection:
            connection.execute('UPDATE jobs SET attempts=attempts+1,retryAt=?,error=? WHERE id=?',
                               (time.time() + delay, message, jobId))

    def problemCount(self, jobId):
        '''Count unresolved map or property failures. Inputs: <jobId>. Output: integer.'''
        logging.debug('Counting job warnings')
        with self.connect() as connection:
            return connection.execute('''SELECT (SELECT COUNT(*) FROM tiles WHERE jobId=? AND state='failed') +
                (SELECT COUNT(*) FROM properties WHERE jobId=? AND processingState='error') +
                (SELECT COUNT(*) FROM baselines WHERE jobId=? AND review='not_found')''', (jobId, jobId, jobId)).fetchone()[0]

    def savePhoto(self, jobId, identity, url, path):
        '''Commit a completed photo. Inputs: <jobId>, <identity>, <url>, <path>. Output: None.'''
        logging.debug('Committing downloaded photo')
        with self.connect() as connection:
            cursor = connection.execute("UPDATE photos SET path=?, state=? WHERE jobId=? AND identity=? AND url=? AND state='pending'",
                                        (path, 'ok' if path else 'missing', jobId, identity, url))
            connection.execute('UPDATE jobs SET photoCount=photoCount+?, dataVersion=dataVersion+? WHERE id=?',
                               (cursor.rowcount if path else 0, cursor.rowcount, jobId))
            connection.execute('UPDATE properties SET attempts=0,retryAt=0 WHERE jobId=? AND identity=?', (jobId, identity))

    def exportResult(self, jobId, version, error=''):
        '''Record an export outcome. Inputs: <jobId>, <version>, [error]. Output: None.'''
        logging.debug('Recording export outcome')
        with self.connect() as connection:
            if error:
                connection.execute('UPDATE jobs SET exportError=? WHERE id=?', (error, jobId))
            else:
                connection.execute('UPDATE jobs SET exportVersion=?, lastExport=?, exportError=? WHERE id=?',
                                   (version, time.time(), '', jobId))
