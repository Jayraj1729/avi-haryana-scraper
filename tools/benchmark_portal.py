'''Bounded, read-only portal pipeline probes; production checkpoints and cookies remain unchanged.'''

# Standard Library Imports
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from scraper import BASEURL, buildRectWkt, encryptPortalValue
from tools.analyze_performance import percentile
from ulb_scraper.geometry import containsProperty, readBoundary
from ulb_scraper.portal import PortalClient, propertyPhotoUrls
from ulb_scraper.property_data import publicViewRestricted


class ProbeStopped(InterruptedError):
    '''The time/request budget was reached or another probe request failed.'''


class ProbeGate:
    '''One bounded request budget and start interval shared by all probe threads.'''

    def __init__(self, interval, seconds=90, maximum=60):
        self.interval = interval
        self.deadline = time.monotonic() + seconds
        self.maximum = maximum
        self.condition = threading.Condition()
        self.nextStart = 0
        self.stopped = False
        self.requests = 0
        self.durations = []
        self.errors = []

    def call(self, operation, *args, **kwargs):
        '''Pace and measure a single request. Inputs: <operation>, [args], [kwargs]. Output: response.'''
        with self.condition:
            while True:
                if self.stopped or time.monotonic() >= self.deadline or self.requests >= self.maximum:
                    raise ProbeStopped('Probe budget reached')
                delay = self.nextStart - time.monotonic()
                if delay <= 0:
                    self.requests += 1
                    self.nextStart = time.monotonic() + self.interval
                    break
                self.condition.wait(min(delay, 0.1))
        started = time.monotonic()
        try:
            return operation(*args, **kwargs)
        except Exception as exception:
            with self.condition:
                self.errors.append({'type': type(exception).__name__, 'status': getattr(exception, 'status', None),
                                    'retryAfter': getattr(exception, 'retryAfter', None)})
                self.stopped = True
                self.condition.notify_all()
            raise
        finally:
            with self.condition:
                self.durations.append(time.monotonic() - started)


def sampleMap(client, state, colonyId):
    '''Fetch fresh tokens near saved in-scope coordinates. Inputs: <client>, <state>, <colonyId>. Output: records.'''
    logging.info('Fetching one small current map sample')
    with closing(sqlite3.connect(state.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        job = connection.execute('SELECT * FROM jobs WHERE colonyId=? AND boundary IS NOT NULL', (colonyId,)).fetchone()
        if not job:
            raise ValueError('The selected colony needs saved geometry and at least one marker')
        record = connection.execute('SELECT raw FROM properties WHERE jobId=? ORDER BY rowid LIMIT 1', (job['id'],)).fetchone()
        raw = json.loads(record['raw'])
    latitude, longitude = float(raw['Lat']), float(raw['Long'])
    bounds = (longitude - 0.0005, latitude - 0.0005, longitude + 0.0005, latitude + 0.0005)
    data = {'pageNo': '1', 'pageSize': '100', 'efkUlbId': encryptPortalValue(job['municipalityId']),
            'PID': '', 'OwnerName': '', 'MobileNo': '', 'fkColonySurveyId': job['colonyId'],
            'fkPropertyCategoryId': '', 'wkt': buildRectWkt(*bounds), 'searchType': '3', 'PIDNDC': '', 'PlotNo': ''}
    rows = client.requestJson('POST', '/SearchProperty/getSearchPropertiesData', data=data,
                              referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap')
    geometry = readBoundary(job['boundary'])
    if not isinstance(rows, list):
        raise ValueError('Unexpected map response; probe stopped')
    unique = {}
    for row in rows:
        if containsProperty(geometry, row):
            unique[str(row.get('pkPropertyId') or row.get('PID'))] = row
    if not unique:
        raise ValueError('No in-scope properties in the probe viewport')
    return list(unique.values())


def probeLevel(template, rows, folder, concurrency, interval):
    '''Measure complete detail/photo pipelines in temporary storage. Inputs: <template>, <rows>, <folder>, <concurrency>, <interval>. Output: metrics.'''
    logging.info(f'Probing {concurrency} parallel pipelines, at least {round(interval * 1000)} ms between request starts')
    gate = ProbeGate(interval)
    local = threading.local()
    started = time.monotonic()

    def pipeline(raw):
        if gate.stopped:
            return False
        if not hasattr(local, 'client'):
            local.client = template.clone()
        client = local.client
        try:
            detail = (client.fetchDetails(raw) if publicViewRestricted(raw) or not raw.get('Token')
                      else gate.call(client.fetchDetails, raw))
            for url in propertyPhotoUrls(raw, detail):
                gate.call(client.downloadPhoto, url, folder, str(raw.get('pkPropertyId') or raw.get('PID')))
            return True
        except ProbeStopped:
            return False
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=concurrency) as workers:
        completed = sum(workers.map(pipeline, rows))
    elapsed = time.monotonic() - started
    return {'parallel': concurrency, 'intervalMs': round(interval * 1000), 'sampleProperties': len(rows),
            'completedProperties': completed, 'elapsedSeconds': round(elapsed, 3),
            'propertiesPerMinute': round(completed * 60 / elapsed, 2), 'requests': gate.requests,
            'requestP50Seconds': percentile(gate.durations, 0.5), 'requestP95Seconds': percentile(gate.durations, 0.95),
            'errors': gate.errors, 'budgetReached': completed < len(rows) and not gate.errors,
            'photoBytes': sum(path.stat().st_size for path in folder.rglob('*') if path.is_file())}


def main():
    '''Run a short fixed probe, never a load test. Inputs: CLI paths and bounded sample/pacing. Output: JSON.'''
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path('data/application'))
    parser.add_argument('--colony-id', default='18949')
    parser.add_argument('--samples', type=int, choices=range(1, 21), default=12)
    parser.add_argument('--interval-ms', type=int, choices=range(250, 10001), default=650)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Choose a new report file')
    report = {'levels': [], 'note': 'Short fixed-sample probes include details and photos, but exclude ongoing map scans, '
              'exports, and checkpoint writes. They do not establish a sustainable site limit. '
              'Each level is bounded to 90 seconds of request starts and 60 requests; stop at the first failure.'}
    with tempfile.TemporaryDirectory(prefix='ulb-probe-') as temporary:
        client = PortalClient(cookieFile=args.home / 'cookies.lwp', persist=False)
        # Parser diagnostics and downloaded images belong exclusively to temporary storage.
        client.cookieFile = Path(temporary) / 'cookies.lwp'
        try:
            client.checkSession()
            rows = sampleMap(client, args.home / 'state.sqlite3', args.colony_id)[:args.samples]
            report['sampleProperties'] = len(rows)
            report['restrictedProperties'] = sum(publicViewRestricted(row) for row in rows)
            for concurrency in (1, 2, 4):
                folder = Path(temporary) / str(concurrency)
                folder.mkdir()
                level = probeLevel(client, rows, folder, concurrency, args.interval_ms / 1000)
                report['levels'].append(level)
                logging.info(json.dumps(level))
                if level['errors'] or level['budgetReached'] or (level['requestP95Seconds'] or 0) >= 8:
                    break
                time.sleep(2)
        except Exception as exception:
            report['stopped'] = {'type': type(exception).__name__, 'status': getattr(exception, 'status', None)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    logging.info(f'Probe report: {args.output}')


if __name__ == '__main__':
    main()
