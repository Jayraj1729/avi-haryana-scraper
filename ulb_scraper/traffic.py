'''Conservative concurrency adaptation and portal outage recovery state.'''

# Standard Library Imports
from collections import deque
import logging
import time


class TrafficController:
    '''Keep the configured concurrency as a ceiling, with a gradual healthy ramp.'''

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.limit = 4
        self.minimumDelay = 0
        self.healthyCount = 0
        self.lastAdjustment = clock()
        self.serverErrors = deque()
        self.slowCount = 0
        self.incidents = 0
        self.recoverySuccesses = 0

    def ceiling(self, configured):
        '''Apply a user's upper bound. Inputs: <configured>. Output: effective limit.'''
        self.limit = max(1, min(self.limit, int(configured), 100))
        return self.limit

    def failure(self, exception, jobId=None):
        '''Classify isolated server errors versus shared failures. Inputs: <exception>, [jobId]. Output: bool.'''
        logging.debug('Classifying portal failure')
        status = getattr(exception, 'status', None)
        if status in (500, 502, 503, 504) and not getattr(exception, 'retryAfter', 0):
            now = self.clock()
            self.serverErrors.append((now, jobId))
            while self.serverErrors and self.serverErrors[0][0] < now - 15:
                self.serverErrors.popleft()
            return len(self.serverErrors) >= 3 and len({job for unusedTime, job in self.serverErrors}) >= 2
        return status in (408, 429, 500, 502, 503, 504) or status is None

    def congested(self):
        '''Reduce load after congestion without changing saved user settings. Inputs: None. Output: cooldown seconds.'''
        logging.info('Reducing effective portal concurrency')
        self.limit = max(1, self.limit // 2)
        self.minimumDelay = min(4, max(0.5, self.minimumDelay * 2))
        self.healthyCount = 0
        self.lastAdjustment = self.clock()
        self.recoverySuccesses = 0
        self.incidents += 1
        return min(300, 5 * 2 ** min(self.incidents - 1, 6))

    def success(self, elapsed, configured, recovering=False):
        '''Adapt using successful work requests, never login checks. Inputs: <elapsed>, <configured>, [recovering]. Output: None.'''
        logging.debug('Recording successful portal work')
        now = self.clock()
        self.slowCount = self.slowCount + 1 if elapsed >= 8 else 0
        if self.slowCount >= 3 and now - self.lastAdjustment >= 10:
            self.limit = max(1, self.limit // 2)
            self.healthyCount = 0
            self.lastAdjustment = now
            self.slowCount = 0
        if recovering:
            self.recoverySuccesses += 1
            return
        self.healthyCount += 1
        if self.healthyCount >= 40 and now - self.lastAdjustment >= 60:
            self.limit = min(max(1, int(configured)), 100, self.limit + 1)
            self.minimumDelay = max(0, self.minimumDelay * 0.8 - 0.05)
            self.lastAdjustment = now
            self.healthyCount = 0
            self.incidents = 0
            self.serverErrors.clear()
