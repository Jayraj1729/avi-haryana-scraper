'''Congestion, bounded colony admission and recovery without losing pending data.'''

# Standard Library Imports
from contextlib import closing
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest

from scraper import AuthenticationError, PortalHttpError
from tools.analyze_performance import analyzeLogs
from tools.benchmark_portal import ProbeGate, ProbeStopped
from ulb_scraper import datasets
from ulb_scraper.engine import AuthenticationChallenge, Engine, WorkSuspended
from ulb_scraper.store import Store
from ulb_scraper.traffic import TrafficController


class Client:
    def __init__(self, **kwargs):
        self.checks = 0

    def checkSession(self):
        self.checks += 1


class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, *args):
        self.calls.append(args)
        return Future()


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.traffic = TrafficController(lambda: self.now)

    def testStartsWithFourAndHonorsSmallerCeiling(self):
        self.assertEqual(self.traffic.ceiling(20), 4)
        self.assertEqual(self.traffic.ceiling(2), 2)
        self.assertEqual(self.traffic.ceiling(1), 1)

    def testRampRequiresHealthyRequestsAndTime(self):
        for request in range(100):
            self.traffic.success(0.5, 20)
        self.assertEqual(self.traffic.limit, 4)
        self.now += 60
        self.traffic.success(0.5, 20)
        self.assertEqual(self.traffic.limit, 5)
        self.traffic.success(0.5, 20)
        self.assertEqual(self.traffic.limit, 5)

    def testCongestionHalvesLimitAndIncreasesCooldown(self):
        self.assertEqual(self.traffic.congested(), 5)
        self.assertEqual(self.traffic.limit, 2)
        self.assertEqual(self.traffic.congested(), 10)
        self.assertEqual(self.traffic.limit, 1)
        for incident in range(10):
            self.traffic.congested()
        self.assertEqual(self.traffic.congested(), 300)

    def testIsolatedServerErrorsDoNotStopOtherJobs(self):
        for request in range(5):
            self.assertFalse(self.traffic.failure(PortalHttpError(500), 'one'))
        self.assertTrue(self.traffic.failure(PortalHttpError(500), 'two'))
        self.now += 16
        self.assertFalse(self.traffic.failure(PortalHttpError(500), 'three'))
        self.assertFalse(self.traffic.failure(PortalHttpError(503), 'three'))
        self.assertTrue(self.traffic.failure(PortalHttpError(429), 'one'))
        self.assertTrue(self.traffic.failure(PortalHttpError(503, 90), 'one'))

    def testSustainedSlowRequestsReduceConcurrency(self):
        self.now += 10
        for request in range(3):
            self.traffic.success(10, 20)
        self.assertEqual(self.traffic.limit, 2)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.store.setSetting('parallelRequests', 20)
        self.store.setSetting('requestDelayMs', 0)
        self.events = []
        self.engine = Engine(self.store, self.home, lambda *event: self.events.append(event), Client)
        self.engine.authenticated = True
        self.jobs = self.store.addJobs([({'id': '1', 'name': 'Town'}, {'id': str(index), 'name': f'Colony {index}'})
                                        for index in range(8)], self.home / 'output')

    def tearDown(self):
        self.temporary.cleanup()

    def failRequest(self, exception, jobId=None, probe=False):
        def request():
            raise exception

        self.engine.local.jobId = jobId
        self.engine.local.probe = probe
        self.engine.nextRequest = 0
        try:
            with self.assertRaises(type(exception)):
                self.engine.network(request)
        finally:
            self.engine.local.jobId = None
            self.engine.local.probe = False

    def pendingMarker(self):
        jobId = self.jobs[0]
        self.store.initializeTiles(jobId, 'POLYGON ((0 0,1 0,1 1,0 1,0 0))', [(0, 0, 1, 1)])
        self.store.saveTile(jobId, self.store.pendingTile(jobId),
                            [{'pkPropertyId': 1, 'PID': 'P1', 'Token': 'stale', 'Lat': 0.5, 'Long': 0.5}], lambda raw: [])
        return self.store.pendingProperty(jobId)

    def testQueueGrowthDoesNotDisplaceActiveBatch(self):
        first = self.engine.selectJobs(4)
        self.assertEqual([job['id'] for job in first], self.jobs[:4])
        self.store.addJobs([({'id': '2', 'name': 'Other'}, {'id': str(index), 'name': f'Colony {index}'})
                            for index in range(100)], self.home / 'output')
        self.assertEqual([job['id'] for job in self.engine.selectJobs(4)], self.jobs[:4])
        self.store.control([self.jobs[0]], 'pause')
        self.assertEqual(len(self.engine.selectJobs(4)), 4)
        self.assertNotIn(self.jobs[0], self.engine.leasedJobs)

    def testIsolated500KeepsPortalAvailableAndCoalescesLoginChecks(self):
        exception = PortalHttpError(500)
        self.failRequest(exception, self.jobs[0])
        self.assertFalse(self.engine.servicePaused)
        self.engine.handleFailure(self.store.job(self.jobs[0]), exception)
        for jobId in self.jobs[1:]:
            self.engine.handleFailure(self.store.job(jobId), PortalHttpError(500))
        self.assertEqual(self.engine.client.checks, 1)
        self.assertNotIn(('session', 'unavailable'), self.events)

    def testOutageKeepsPendingPropertyAndRetryAllowance(self):
        jobId = self.jobs[0]
        self.store.initializeTiles(jobId, 'POLYGON ((0 0,1 0,1 1,0 1,0 0))', [(0, 0, 1, 1)])
        self.store.saveTile(jobId, self.store.pendingTile(jobId), [{'pkPropertyId': 1, 'PID': 'P1', 'Token': 'test'}], lambda raw: [])
        exception = PortalHttpError(429, 90)
        self.failRequest(exception, jobId)
        self.engine.handleFailure(self.store.job(jobId), exception)
        record = self.store.pendingProperty(jobId)
        self.assertEqual(record['attempts'], 0)
        self.assertEqual(record['detailState'], 'pending')
        self.assertGreaterEqual(self.engine.retryUntil, time.monotonic() + 89)
        self.assertEqual(self.engine.traffic.limit, 2)
        self.assertEqual(self.engine.client.checks, 0)

    def testRetryAfterAlsoDefersAmbiguousFailureLoginCheck(self):
        exception = PortalHttpError(500, 90)
        self.failRequest(exception, self.jobs[0])
        self.engine.handleFailure(self.store.job(self.jobs[0]), exception)
        self.assertEqual(self.engine.client.checks, 0)
        self.engine.retryUntil = 0
        self.engine.nextRequest = 0
        self.engine.scheduleWork(Executor())
        self.assertEqual(self.engine.client.checks, 1)
        self.assertTrue(self.engine.servicePaused)

    def testOneRecoveryProbeAndLoginCheckCannotTurnIndicatorGreen(self):
        self.engine.pauseService(TimeoutError('fixture timeout'))
        executor = Executor()
        self.engine.scheduleWork(executor)
        self.assertFalse(executor.calls)
        self.engine.retryUntil = 0
        self.engine.nextRequest = 0
        self.engine.network(self.engine.client.checkSession)
        self.assertTrue(self.engine.servicePaused)
        self.assertNotIn(('session', 'valid'), self.events)
        self.engine.scheduleWork(executor)
        self.engine.scheduleWork(executor)
        self.assertEqual(len(executor.calls), 1)
        self.assertTrue(executor.calls[0][-1])

    def testRecoveryNeedsSuccessfulWorkAndBlockedNormalWorkersDoNotSpendRetries(self):
        self.engine.pauseService(TimeoutError('fixture timeout'))
        self.engine.local.jobId = self.jobs[0]
        with self.assertRaises(WorkSuspended):
            self.engine.network(lambda: None)
        self.engine.local.probe = True
        self.engine.traffic.minimumDelay = 0
        self.engine.nextRequest = 0
        self.engine.network(lambda: None)
        self.assertTrue(self.engine.servicePaused)
        self.engine.network(lambda: None)
        self.assertFalse(self.engine.servicePaused)
        self.assertIn(('session', 'valid'), self.events)

    def testFailedProbeGivesAnotherColonyAChance(self):
        self.engine.pauseService(TimeoutError('fixture timeout'))
        self.failRequest(TimeoutError('fixture timeout'), self.jobs[0], probe=True)
        self.engine.retryUntil = 0
        executor = Executor()
        self.engine.scheduleWork(executor)
        self.assertNotEqual(executor.calls[0][1]['id'], self.jobs[0])

    def testRepeatedTransportFailuresPauseOnlyAffectedColonyWithoutDroppingRows(self):
        for attempt in range(3):
            self.failRequest(TimeoutError('fixture timeout'), self.jobs[0], probe=True)
        self.assertFalse(self.store.job(self.jobs[0])['desired'])
        self.assertEqual(self.store.job(self.jobs[0])['status'], 'error')
        self.assertTrue(self.store.job(self.jobs[1])['desired'])

    def testRejectedWorkerTokenDoesNotExpireSessionBeforeProtectedPageCheck(self):
        self.pendingMarker()
        self.engine.local.jobId = self.jobs[0]
        self.engine.local.recordIdentity = '1'

        def fetchDetails():
            raise AuthenticationError('stale token')

        with self.assertRaises(AuthenticationChallenge) as result:
            self.engine.network(fetchDetails)
        self.engine.local.jobId = None
        self.assertTrue(self.engine.authenticated)
        self.engine.handleAuthenticationChallenge(self.jobs[0], result.exception)
        self.assertTrue(self.engine.authenticated)
        self.assertEqual(self.store.pendingProperty(self.jobs[0])['refreshToken'], 1)
        self.assertNotIn(('session', 'expired'), self.events)

    def testActuallyExpiredProtectedPageStillStopsAllWorkersAndKeepsManualPause(self):
        self.store.control([self.jobs[1]], 'pause')

        def checkSession():
            raise AuthenticationError('expired')

        self.engine.client.checkSession = checkSession
        self.engine.handleAuthenticationChallenge(self.jobs[0], AuthenticationChallenge(0, 'fetchDetails', '1'))
        self.assertFalse(self.engine.authenticated)
        self.assertEqual(self.store.job(self.jobs[0])['status'], 'waiting_login')
        self.assertEqual(self.store.job(self.jobs[1])['status'], 'paused')

    def testTokenRefreshIsBoundedAndSurvivesPortableQueueReconstruction(self):
        self.pendingMarker()
        self.assertTrue(self.store.requestTokenRefresh(self.jobs[0], '1'))
        self.assertTrue(self.store.requestTokenRefresh(self.jobs[0], '1'))
        self.assertFalse(self.store.requestTokenRefresh(self.jobs[0], '1'))
        self.store.deleteJobs([self.jobs[0]])
        inventory = datasets.inspectDataset(self.home / 'output')
        restoredId = self.store.importDataset(inventory, restoreOnly=True)[0]
        self.assertEqual(self.store.pendingProperty(restoredId)['refreshToken'], 1)
        self.assertEqual(self.store.pendingProperty(restoredId)['tokenRefreshes'], 2)

    def testMarkerRefreshKeepsCountsAccurateAndLegacyRetainedValuesAvailable(self):
        marker = self.pendingMarker()
        self.store.saveDetail(self.jobs[0], '1', {'Detail': {'Address': 'Retained address'}}, ['https://example.test/photo.jpg'])
        self.store.savePhoto(self.jobs[0], '1', 'https://example.test/photo.jpg', 'photos/old.jpg')
        raw = json.loads(marker['raw'])
        raw['Token'] = 'fresh'
        self.store.saveRefreshedMarker(self.jobs[0], '1', raw, [])
        self.assertEqual(self.store.job(self.jobs[0])['detailCount'], 0)
        self.assertEqual(self.store.job(self.jobs[0])['photoCount'], 0)
        self.assertEqual(json.loads(self.store.pendingProperty(self.jobs[0])['detail'])['Detail']['Address'], 'Retained address')

    def testBackendSchemaUpgradeRebuildsPortableCopyWithoutResettingPause(self):
        self.pendingMarker()
        self.store.control([self.jobs[0]], 'pause')
        self.store.persistDataset(self.jobs[0])
        checkpoint = Path(self.store.job(self.jobs[0])['folder']) / datasets.CHECKPOINT
        with self.store.connect() as connection:
            connection.execute('ALTER TABLE properties DROP COLUMN refreshToken')
            connection.execute('ALTER TABLE properties DROP COLUMN tokenRefreshes')
        with closing(sqlite3.connect(checkpoint)) as connection:
            with connection:
                connection.execute('ALTER TABLE properties DROP COLUMN refreshToken')
                connection.execute('ALTER TABLE properties DROP COLUMN tokenRefreshes')
        self.store = Store(self.store.path)
        self.assertEqual(self.store.job(self.jobs[0])['status'], 'paused')
        self.assertEqual(self.store.job(self.jobs[0])['portableReady'], 0)
        self.store.persistDataset(self.jobs[0])
        with closing(sqlite3.connect(checkpoint)) as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(properties)')}
            self.assertIn('refreshToken', columns)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM properties').fetchone()[0], 1)


class DiagnosticTests(unittest.TestCase):
    def testLogAnalysisCountsRequestsOnceAndIncludesIdleTime(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'application.log'
            path.write_text('2026-11-03 12:00:00,000 WARNING colony_1 pauseService Portal unavailable: Portal HTTP 500\n'
                           '2026-11-03 12:00:01,000 INFO colony_1 network Request stage=requestJson elapsed=1.000s wait=0.000s outcome=PortalHttpError\n'
                           '2026-11-03 12:00:02,000 INFO colony_1 advance property=private saved with details and photos\n')
            result = analyzeLogs([path], datetime(2026, 11, 3, 12), datetime(2026, 11, 3, 12, 2))
            self.assertEqual(result['httpErrors'], {'500': 1})
            self.assertEqual(result['propertiesPerMinute'], 0.5)
            self.assertNotIn('private', str(result))

    def testProbeEnforcesRequestBudgetAndStopsOnFirstError(self):
        gate = ProbeGate(0, maximum=1)
        gate.call(lambda: None)
        with self.assertRaises(ProbeStopped):
            gate.call(lambda: self.fail('Beyond probe budget'))
        gate = ProbeGate(0)
        with self.assertRaises(PortalHttpError):
            gate.call(lambda: (_ for unused in ()).throw(PortalHttpError(429, 10)))
        with self.assertRaises(ProbeStopped):
            gate.call(lambda: self.fail('Continued after a portal error'))
