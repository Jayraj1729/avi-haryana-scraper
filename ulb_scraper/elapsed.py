'''A shared monotonic clock for persisted colony and municipality durations.'''

# Standard Library Imports
import logging
import time


def formatElapsed(seconds):
    '''Format accumulated seconds without wrapping durations at midnight.'''
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


class ElapsedTracker:
    '''Count admitted work, including retries, without counting queued/offline time.'''

    def __init__(self, store, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.previous = clock()
        self.lastSaved = self.previous
        self.running = set()
        with store.connect() as connection:
            self.colonies = dict(connection.execute('SELECT jobId,seconds FROM jobElapsed'))
            self.municipalities = dict(connection.execute('SELECT municipalityId,seconds FROM municipalityElapsed'))
        self.parents = {}

    def tick(self, jobs, authenticated=True, admitted=(), save=False):
        '''Account once per shared tick; persist every ten seconds and at transitions.'''
        now = self.clock()
        duration = max(0, now - self.previous)
        current = {job['id']: job for job in jobs}
        for municipalityId in set(self.parents.values()):
            if not any(jobId in current and parent == municipalityId for jobId, parent in self.parents.items()):
                self.municipalities.pop(municipalityId, None)
        activeParents = set()
        for jobId in self.running:
            if jobId in current:
                self.colonies[jobId] = self.colonies.get(jobId, 0) + duration
                activeParents.add(self.parents[jobId])
        for municipalityId in activeParents:
            self.municipalities[municipalityId] = self.municipalities.get(municipalityId, 0) + duration
        running = {jobId for jobId in self.running | set(admitted)
                   if jobId in current and current[jobId]['desired'] and authenticated}
        changed = running != self.running
        self.running = running
        self.parents = {jobId: job['municipalityId'] for jobId, job in current.items()}
        self.colonies = {jobId: seconds for jobId, seconds in self.colonies.items() if jobId in current}
        parents = set(self.parents.values())
        self.municipalities = {identity: seconds for identity, seconds in self.municipalities.items() if identity in parents}
        self.previous = now
        if save or changed or now - self.lastSaved >= 10:
            logging.debug('Saving elapsed processing durations')
            with self.store.connect() as connection:
                connection.executemany('INSERT OR REPLACE INTO jobElapsed VALUES (?,?)', self.colonies.items())
                connection.executemany('INSERT OR REPLACE INTO municipalityElapsed VALUES (?,?)', self.municipalities.items())
            self.lastSaved = now
        return {'colonies': dict(self.colonies), 'municipalities': dict(self.municipalities)}
