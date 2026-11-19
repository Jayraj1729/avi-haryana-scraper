'''Summarize request latency and completed property throughput without reading record values.'''

# Standard Library Imports
import argparse
from collections import Counter
from datetime import datetime
import json
import logging
from pathlib import Path
import re


REQUEST = re.compile(r'Request stage=(\w+) elapsed=([\d.]+)s wait=([\d.]+)s outcome=(\w+)')


def percentile(values, fraction):
    '''Return a nearest-rank percentile. Inputs: <values>, <fraction>. Output: float or None.'''
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, int(len(ordered) * fraction + 0.999999) - 1)], 3)


def analyzeLogs(paths, start, end):
    '''
    Measure a fixed wall-clock window from application logs.

    Inputs:    <paths>  -  Application log files, including rotated files
               <start>  -  Inclusive local timestamp, matching the app log timezone
               <end>    -  Exclusive local timestamp

    Output:    Aggregate report without PIDs, URLs, tokens or contact values
    '''
    logging.info('Analyzing application performance logs')
    if end <= start:
        raise ValueError('The end of the measurement must be after its start')
    requests = Counter()
    statuses = Counter()
    latencies = []
    waits = []
    properties = 0
    unavailable = 0
    expired = 0
    threadStatuses = {}
    for path in sorted(paths, key=lambda item: item.stat().st_mtime):
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    timestamp = datetime.strptime(line[:23], '%Y-%m-%d %H:%M:%S,%f')
                except ValueError:
                    continue
                if not start <= timestamp < end:
                    continue
                thread = line.split(maxsplit=4)[3]
                if 'Portal unavailable:' in line:
                    unavailable += 1
                    status = re.search(r'Portal HTTP (\d+)', line)
                    if status:
                        threadStatuses[thread] = status[1]
                expired += 'Portal session expired;' in line
                properties += ' saved with details and photos' in line
                match = REQUEST.search(line)
                if not match:
                    continue
                stage, elapsed, wait, outcome = match.groups()
                requests[f'{stage}:{outcome}'] += 1
                if outcome == 'ok':
                    latencies.append(float(elapsed))
                waits.append(float(wait))
                oldStatus = threadStatuses.pop(thread, None)
                status = re.search(r'\bstatus=(\d+)\b', line)
                if status:
                    statuses[status[1]] += 1
                elif oldStatus and outcome == 'PortalHttpError':
                    statuses[oldStatus] += 1
    seconds = (end - start).total_seconds()
    count = sum(requests.values())
    return {'start': start.isoformat(), 'end': end.isoformat(), 'wallSeconds': seconds,
            'completedProperties': properties, 'propertiesPerMinute': round(properties * 60 / seconds, 2),
            'requests': count, 'requestsPerProperty': round(count / properties, 2) if properties else None,
            'stages': dict(sorted(requests.items())), 'httpErrors': dict(statuses),
            'successfulRequestP50Seconds': percentile(latencies, 0.5),
            'successfulRequestP95Seconds': percentile(latencies, 0.95),
            'throttleWaitP95Seconds': percentile(waits, 0.95),
            'unavailableEvents': unavailable, 'sessionExpiryEvents': expired,
            'note': 'Wall-clock rate includes idle time and pauses in the selected window. '
                    'Completed properties are successful pipeline log events, not unique PIDs across update generations.'}


def main():
    '''Analyze a bounded log window. Inputs: CLI --since, --until, [--logs], [--output]. Output: JSON report.'''
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs', type=Path, default=Path('data/application/logs'))
    parser.add_argument('--since', type=datetime.fromisoformat, required=True)
    parser.add_argument('--until', type=datetime.fromisoformat, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = analyzeLogs(list(args.logs.glob('application.log*')), args.since, args.until)
    document = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(document + '\n')
    else:
        logging.info(document)


if __name__ == '__main__':
    main()
