'''Bounded parallel property pipelines with a shared authentication and rate gate.'''

# Standard Library Imports
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid
from urllib import parse

from scraper import (AuthenticationError, BASEURL, buildRectWkt,
                     encryptPortalValue, PortalHttpError, PortalResponseError, propertyIdentity, slugify)
from ulb_scraper import datasets
from ulb_scraper.elapsed import ElapsedTracker
from ulb_scraper.exporter import exportJob
from ulb_scraper.geometry import containsProperty, readBoundary, seedBounds, splitBounds
from ulb_scraper.municipality_export import exportMunicipality
from ulb_scraper.portal import PortalClient, propertyPhotoUrls
from ulb_scraper.pid_verification import municipalityKey, PidReference, queueProperty, recoveryJob
from ulb_scraper.property_data import publicViewRestricted
from ulb_scraper.traffic import TrafficController


class WorkSuspended(InterruptedError):
    '''An unfinished unit of work retained after pause, expiry, or shutdown.'''


class DatasetCatalogError(ValueError):
    '''An imported colony needs catalog review before requests can advance.'''


class AuthenticationChallenge(AuthenticationError):
    '''A worker login response requiring session confirmation after its request slot is released.'''

    def __init__(self, epoch, stage, identity):
        super().__init__('Worker access needs session or marker-token verification')
        self.epoch = epoch
        self.stage = stage
        self.identity = identity


class Engine(threading.Thread):
    '''Coordinator for parallel colony workers and one automatic workbook writer.'''

    def __init__(self, store, home, emit, clientFactory=PortalClient):
        super().__init__(name='portal-worker', daemon=False)
        self.store = store
        self.home = Path(home)
        self.emit = emit
        self.clientFactory = clientFactory
        self.commands = queue.Queue()
        self.stopEvent = threading.Event()
        self.client = clientFactory(cookieFile=self.home / 'cookies.lwp')
        self.challenge = None
        self.authenticated = False
        self.lastRequest = 0
        self.lastSessionCheck = 0
        self.retryUntil = 0
        self.failures = {}
        self.geometryCache = {}
        self.lastJob = None
        self.exportAttempts = {}
        self.condition = threading.Condition()
        self.activeRequests = 0
        self.nextRequest = 0
        self.sessionEpoch = 0
        self.local = threading.local()
        self.inFlight = {}
        self.exportFuture = None
        self.exportJobId = None
        self.exportDuration = {}
        self.exportPending = set()
        self.servicePaused = False
        self.traffic = TrafficController()
        self.leasedJobs = []
        self.lastFailureSessionCheck = 0
        self.failureSessionCheckPending = False
        self.lastTrafficReport = 0
        self.failedProbeJobs = set()
        self.transportFailures = {}
        self.lastChallengeVerified = 0
        self.forceExport = set()
        self.publishedCounts = {}
        self.retainedExports = store.retainedExportJobs()
        self.municipalityExport = None
        self.elapsed = ElapsedTracker(store)
        self.pidReference = PidReference(home)
        self.pidCancel = threading.Event()

    def submit(self, action, **payload):
        '''Queue a worker operation. Inputs: <action>, [payload]. Output: None.'''
        logging.debug(f'Queuing worker action {action}')
        self.commands.put((action, payload))

    def stop(self):
        '''Request checkpointed shutdown. Inputs: None. Output: None.'''
        logging.info('Requesting application shutdown')
        self.stopEvent.set()
        with self.condition:
            self.condition.notify_all()

    def network(self, operation, *args, **kwargs):
        '''
        Pace all portal requests using the same interruptible throttle.

        Inputs:    <operation>  -  HTTP client method
                   [args]       -  Method arguments
                   [kwargs]     -  Method keyword arguments

        Output:    The HTTP method's result
        '''
        jobId = getattr(self.local, 'jobId', None)
        probe = getattr(self.local, 'probe', False)
        queuedAt = time.monotonic()
        with self.condition:
            epoch = self.sessionEpoch
            while True:
                if self.stopEvent.is_set():
                    raise WorkSuspended('Application is closing')
                if jobId:
                    job = self.store.job(jobId)
                    if (not job or (not job['desired'] and not getattr(self.local, 'pidRecovery', False)) or not self.authenticated or (self.servicePaused and not probe) or
                            getattr(self.local, 'epoch', self.sessionEpoch) != self.sessionEpoch):
                        raise WorkSuspended('Scraping is paused')
                maximum = self.traffic.ceiling(self.store.setting('parallelRequests', 2))
                if self.servicePaused:
                    maximum = 1
                delay = self.nextRequest - time.monotonic()
                if self.activeRequests < maximum and delay <= 0:
                    self.activeRequests += 1
                    self.nextRequest = time.monotonic() + max(self.store.setting('requestDelayMs', 650) / 1000,
                                                             self.traffic.minimumDelay)
                    break
                self.condition.wait(min(0.1, max(0.01, delay)))
        startedAt = time.monotonic()
        outcome = 'ok'
        httpStatus = '-'
        try:
            result = operation(*args, **kwargs)
            if jobId:
                with self.condition:
                    if epoch != self.sessionEpoch or not self.authenticated:
                        raise WorkSuspended('Session changed during the request')
                    self.transportFailures.pop((jobId, getattr(operation, '__name__', 'request')), None)
                    if not self.servicePaused or probe:
                        self.traffic.success(time.monotonic() - startedAt,
                                             self.store.setting('parallelRequests', 2), self.servicePaused and probe)
                    if self.servicePaused and probe and self.traffic.recoverySuccesses >= 2:
                        self.servicePaused = False
                        self.failedProbeJobs.clear()
                        self.traffic.serverErrors.clear()
                        self.condition.notify_all()
                        self.emit('session', 'valid')
                        self.emit('notice', 'Portal work requests recovered. Resuming at reduced concurrency.')
            return result
        except AuthenticationError:
            outcome = 'authentication'
            with self.condition:
                if epoch != self.sessionEpoch:
                    raise WorkSuspended('Response belongs to an earlier session')
                if jobId:
                    outcome = 'authentication_challenge'
                    raise AuthenticationChallenge(epoch, getattr(operation, '__name__', 'request'),
                                                  getattr(self.local, 'recordIdentity', None))
                self.expireSession()
            raise
        except (PortalHttpError, OSError, RuntimeError) as exception:
            outcome = type(exception).__name__
            httpStatus = getattr(exception, 'status', '-')
            with self.condition:
                if jobId and epoch != self.sessionEpoch:
                    raise WorkSuspended('Response belongs to an earlier session') from exception
                if not isinstance(exception, (WorkSuspended, PortalResponseError)) or isinstance(exception, PortalHttpError):
                    if self.authenticated:
                        shared = self.traffic.failure(exception, jobId)
                        if shared or self.servicePaused and probe:
                            self.pauseService(exception)
                        exception.sharedFailure = shared
                        exception.workProbe = probe
                        if probe and jobId:
                            self.failedProbeJobs.add(jobId)
                        if jobId and not isinstance(exception, PortalResponseError):
                            failureKey = (jobId, getattr(operation, '__name__', 'request'))
                            count = self.transportFailures.get(failureKey, 0) + 1
                            self.transportFailures[failureKey] = count
                            if count >= 3:
                                self.store.setStatus(jobId, 'error', 'Repeated transport failures; pending work retained', desired=0)
                                self.emit('notice', 'One colony was paused after repeated transport failures. '
                                          'Its pending work is retained; other colonies can continue.')
            raise
        except Exception as exception:
            outcome = type(exception).__name__
            raise
        finally:
            stage = getattr(operation, '__name__', type(operation).__name__)
            endpoint = next((parse.urlsplit(value).path for value in args if isinstance(value, str) and
                             value.startswith(('/', 'http'))), '-')
            logging.info(f'Request stage={stage} elapsed={time.monotonic() - startedAt:.3f}s '
                         f'wait={startedAt - queuedAt:.3f}s outcome={outcome} endpoint={endpoint} '
                         f'status={httpStatus} job={jobId or "control"} '
                         f'active={self.activeRequests} limit={self.traffic.limit}')
            with self.condition:
                self.activeRequests -= 1
                self.condition.notify_all()

    def workerClient(self):
        '''Get an independent client for the current authenticated epoch. Inputs: None. Output: client.'''
        logging.debug('Selecting worker HTTP client')
        with self.condition:
            if getattr(self.local, 'epoch', -1) != self.sessionEpoch:
                template = getattr(self, 'workerTemplate', self.client)
                self.local.client = template.clone() if hasattr(template, 'clone') else template
                self.local.epoch = self.sessionEpoch
            return self.local.client

    def run(self):
        '''
        Restore session, process commands, and fairly advance active jobs.

        Inputs:    None

        Output:    None; events are emitted to the GUI bridge
        '''
        logging.info('Starting parallel scraper coordinator')
        self.store.waitForLogin()
        with ThreadPoolExecutor(max_workers=100, thread_name_prefix='colony') as workers, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='excel') as writer, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='municipality-export') as masterWriter:
            self.writer = writer
            self.masterWriter = masterWriter
            try:
                self.restoreSession()
                while not self.stopEvent.is_set():
                    try:
                        self.collectWork()
                        self.collectMunicipalityExport()
                        try:
                            action, payload = self.commands.get_nowait()
                        except queue.Empty:
                            action = None
                        if action:
                            self.handleCommand(action, payload)
                        self.flushExports()
                        if self.authenticated and not self.servicePaused:
                            if time.monotonic() - self.lastSessionCheck > 300:
                                self.network(self.client.checkSession)
                                self.lastSessionCheck = time.monotonic()
                        if self.authenticated:
                            self.scheduleWork(workers)
                        self.reportTraffic()
                        self.stopEvent.wait(0.05)
                    except AuthenticationError:
                        self.expireSession()
                    except WorkSuspended:
                        if self.stopEvent.is_set():
                            break
                    except Exception as exception:
                        logging.exception('Coordinator operation failed')
                        self.emit('error', str(exception))
                        self.stopEvent.wait(0.5)
            except WorkSuspended:
                pass
            finally:
                self.elapsed.tick(self.store.jobs(), False, save=True)
                workers.shutdown(wait=True, cancel_futures=True)
                self.collectWork()
                self.collectMunicipalityExport(wait=True)
                if self.exportFuture:
                    self.exportFuture.result()
                    self.exportFuture = None
                self.flushExports(force=True)
                for job in self.store.jobs():
                    try:
                        self.store.persistDataset(job['id'])
                    except Exception:
                        logging.exception('Portable checkpoint could not be saved during shutdown')
                logging.info('Worker stopped with checkpoints retained')
                self.emit('stopped', None)

    def jobReady(self, job):
        '''Respect persisted backoff without occupying worker slots. Inputs: <job>. Output: boolean.'''
        logging.debug('Checking job retry timing')
        record = self.store.pendingProperty(job['id']) or self.store.pendingTile(job['id'])
        return (record or job)['retryAt'] <= time.time()

    def selectJobs(self, maximum, avoid=()):
        '''Keep a bounded colony batch progressing. Inputs: <maximum>, [avoid]. Output: jobs.'''
        logging.debug('Selecting bounded active colony batch')
        jobs = {job['id']: job for job in self.store.jobs() if job['desired'] and job['id'] not in avoid}
        selected = []
        for jobId in self.leasedJobs:
            if jobId in jobs and (jobId in self.inFlight or self.jobReady(jobs[jobId])):
                selected.append(jobs[jobId])
                if len(selected) >= maximum:
                    break
        candidates = sorted(jobs.values(), key=lambda job: (job['uniqueCount'] <= job['completedCount'], job['created']))
        identities = {job['id'] for job in selected}
        for job in candidates:
            if len(selected) >= maximum:
                break
            if job['id'] not in identities and self.jobReady(job):
                selected.append(job)
                identities.add(job['id'])
        self.leasedJobs = [job['id'] for job in selected]
        return selected

    def scheduleWork(self, workers):
        '''Admit a bounded batch, or a single work probe after cooldown. Inputs: <workers>. Output: None.'''
        logging.debug('Scheduling portal work')
        with self.condition:
            recovering = self.servicePaused
            maximum = self.traffic.ceiling(self.store.setting('parallelRequests', 2))
            if recovering:
                if self.inFlight or self.activeRequests or time.monotonic() < self.retryUntil:
                    return
                maximum = 1
        if recovering and self.failureSessionCheckPending:
            self.checkFailureSession()
            if not self.authenticated or time.monotonic() < self.retryUntil:
                return
        jobs = self.selectJobs(maximum, self.failedProbeJobs if recovering else ())
        if recovering and not jobs:
            self.failedProbeJobs.clear()
            jobs = self.selectJobs(maximum)
        for job in jobs:
            if len(self.inFlight) >= maximum:
                break
            if job['id'] not in self.inFlight:
                self.elapsed.tick(self.store.jobs(), self.authenticated, admitted=[job['id']])
                self.inFlight[job['id']] = workers.submit(self.runUnit, job, recovering)

    def reportTraffic(self):
        '''Publish a small operational status without property values. Inputs: None. Output: None.'''
        now = time.monotonic()
        if now - self.lastTrafficReport < 1:
            return
        self.lastTrafficReport = now
        self.emit('elapsed', self.elapsed.tick(self.store.jobs(), self.authenticated))
        with self.condition:
            payload = {'active': self.activeRequests, 'limit': self.traffic.limit,
                       'configured': self.store.setting('parallelRequests', 2),
                       'intervalMs': round(max(self.traffic.minimumDelay * 1000,
                                               self.store.setting('requestDelayMs', 650))),
                       'cooldown': max(0, round(self.retryUntil - now)) if self.servicePaused else 0,
                       'recovering': self.servicePaused, 'colonies': len(self.inFlight)}
        self.emit('traffic', payload)
        logging.info('Traffic ' + ' '.join(f'{key}={value}' for key, value in payload.items()))

    def runUnit(self, job, probe=False):
        '''Run bounded work under the session gate. Inputs: <job>, [probe]. Output: None.'''
        logging.debug('Starting colony work unit')
        self.local.jobId = job['id']
        self.local.probe = probe
        self.local.recordIdentity = None
        try:
            self.advance(job)
        finally:
            self.local.jobId = None
            self.local.probe = False
            self.local.recordIdentity = None
            try:
                self.store.persistDataset(job['id'])
            except Exception as exception:
                logging.exception('Portable checkpoint write failed; application checkpoint retained')
                self.store.setStatus(job['id'], 'error', f'Portable checkpoint: {exception}', desired=0)
                self.emit('notice', f'{job["colonyName"]}: portable checkpoint could not be saved. Local progress is retained.')

    def collectWork(self):
        '''Collect completed work without blocking UI commands. Inputs: None. Output: None.'''
        logging.debug('Collecting colony futures')
        for jobId, future in list(self.inFlight.items()):
            if not future.done():
                continue
            del self.inFlight[jobId]
            try:
                future.result()
            except AuthenticationChallenge as exception:
                self.handleAuthenticationChallenge(jobId, exception)
            except (WorkSuspended, AuthenticationError, CancelledError):
                pass
            except DatasetCatalogError as exception:
                self.store.setStatus(jobId, 'error', str(exception), desired=0)
                self.emit('notice', str(exception))
            except Exception as exception:
                job = self.store.job(jobId)
                if job:
                    self.handleFailure(job, exception)

    def handleAuthenticationChallenge(self, jobId, challenge):
        '''Verify login before expiring all workers; refresh rejected saved tokens. Inputs: <jobId>, <challenge>. Output: None.'''
        logging.info('Confirming worker authentication response')
        if challenge.epoch != self.sessionEpoch or not self.authenticated:
            return
        try:
            if time.monotonic() - self.lastChallengeVerified >= 5:
                self.network(self.client.checkSession)
                self.lastChallengeVerified = time.monotonic()
                self.lastSessionCheck = self.lastChallengeVerified
        except (AuthenticationError, WorkSuspended):
            return
        except Exception:
            logging.warning('Worker authentication confirmation deferred until portal recovery')
            return
        job = self.store.job(jobId)
        if not job or not job['desired']:
            return
        if (challenge.identity and challenge.stage in ('fetchDetails', 'downloadPhoto') and
                self.store.requestTokenRefresh(jobId, challenge.identity)):
            self.store.setStatus(jobId, 'retrying', 'Login is valid; refreshing a rejected property token')
            self.emit('notice', 'Login is still valid. Refreshing a saved property token without resetting the queue.')
            return
        self.handleFailure(job, PortalResponseError('Login is valid but this portal record still rejects access'))

    def pauseService(self, exception):
        '''Back off shared outages, then recover through one work probe. Inputs: <exception>. Output: None.'''
        logging.warning(f'Portal unavailable: {exception}')
        with self.condition:
            incident = not self.servicePaused or getattr(self.local, 'probe', False)
            delay = self.traffic.congested() if incident else max(0, self.retryUntil - time.monotonic())
            self.servicePaused = True
            self.retryUntil = max(self.retryUntil, time.monotonic() + max(delay, getattr(exception, 'retryAfter', 0)))
            self.condition.notify_all()
        if incident:
            self.emit('session', 'unavailable')

    def restoreSession(self):
        '''Check saved cookies without sending an OTP. Inputs: None. Output: None.'''
        logging.info('Checking saved portal session')
        self.emit('session', 'checking')
        try:
            self.network(self.client.checkSession)
        except AuthenticationError:
            self.expireSession()
        except InterruptedError:
            raise
        except Exception:
            logging.warning('Saved session could not be checked; login or check connection')
            with self.condition:
                self.authenticated = False
                self.sessionEpoch += 1
                self.condition.notify_all()
            self.store.waitForLogin()
            self.emit('session', 'unverified')
        else:
            with self.condition:
                self.authenticated = True
                self.servicePaused = False
                self.sessionEpoch += 1
                self.workerTemplate = self.client.clone() if hasattr(self.client, 'clone') else self.client
                self.condition.notify_all()
            self.lastSessionCheck = time.monotonic()
            self.emit('session', 'valid')
            try:
                self.refreshMunicipalities()
                selectedMunicipalities = self.store.setting('selectedMunicipalities', []) or [
                    self.store.setting('selectedMunicipality', '')]
                for municipalityId in selectedMunicipalities:
                    if municipalityId and municipalityId != 'ALL':
                        self.refreshColonies(municipalityId)
            except AuthenticationError:
                self.expireSession()
            except Exception as exception:
                self.emit('notice', f'Catalog refresh failed: {exception}')

    def expireSession(self):
        '''Suspend desired jobs and alert the UI. Inputs: None. Output: None.'''
        logging.warning('Portal session expired; waiting for OTP login')
        with self.condition:
            self.authenticated = False
            self.servicePaused = False
            self.sessionEpoch += 1
            self.condition.notify_all()
        self.store.waitForLogin()
        self.emit('session', 'expired')
        self.forceExport.update(job['id'] for job in self.store.jobs())

    def handleCommand(self, action, payload):
        '''Process an action on the HTTP owning thread. Inputs: <action>, <payload>. Output: None.'''
        logging.info(f'Processing application action {action}')
        operationStarted = time.monotonic()
        try:
            if self.municipalityExport and action in ('add', 'delete', 'importDataset'):
                raise ValueError('Wait for the municipality export to finish before changing dataset folders or jobs')
            if action == 'generateOtp':
                challenge = self.clientFactory(cookieFile=self.home / 'cookies.lwp', fresh=True, persist=False)
                self.network(challenge.generateOtp, payload['phone'])
                self.challenge = challenge
                self.emit('otpSent', None)
            elif action == 'verifyOtp':
                if not self.challenge:
                    raise ValueError('Generate an OTP first')
                self.network(self.challenge.verifyOtp, payload['otp'])
                with self.condition:
                    self.client = self.challenge
                    self.challenge = None
                    self.authenticated = True
                    self.servicePaused = False
                    self.sessionEpoch += 1
                    self.workerTemplate = self.client.clone() if hasattr(self.client, 'clone') else self.client
                    self.condition.notify_all()
                self.lastSessionCheck = time.monotonic()
                self.retryUntil = 0
                self.failures.clear()
                self.emit('session', 'valid')
                self.emit('otpVerified', None)
                self.refreshMunicipalities()
            elif action == 'refreshMunicipalities':
                self.refreshMunicipalities()
            elif action == 'refreshColonies':
                self.refreshColonies(payload['municipalityId'])
            elif action == 'checkSession':
                self.restoreSession()
            elif action == 'add':
                if 'selections' in payload:
                    self.addSelections(**payload)
                else:
                    self.addSelection(**payload)
                self.emit('addSucceeded', None)
            elif action == 'inspectDataset':
                inventory = datasets.inspectDataset(payload['folder'], self.operationProgress)
                queued = {(job['municipalityId'], job['colonyId']): job for job in self.store.jobs()}
                for entry in inventory['entries']:
                    existing = queued.get((entry['municipalityId'], entry['colonyId']))
                    if existing and Path(existing['folder']).resolve() != Path(entry['folder']).resolve():
                        raise ValueError('A colony is already queued in another output folder. Remove that queue entry first; its data will be retained.')
                if payload.get('mobileOnly'):
                    inventory['mobileOnly'] = True
                self.emit('datasetInspected', inventory)
            elif action == 'importDataset':
                self.importDataset(**payload)
            elif action == 'importPidList':
                self.pidReference.importWorkbook(payload['path'], self.operationProgress)
                self.emit('pidListChanged', None)
            elif action in ('verifyPids', 'fetchMissingPids'):
                self.runPidOperation(action, **payload)
            elif action == 'exportMunicipality':
                self.beginMunicipalityExport(**payload)
            elif action == 'delete':
                self.operationProgress(0, 0, 'Stopping selected jobs safely')
                selectedIds = set(payload['jobIds'])
                selected = [job for job in self.store.jobs() if job['id'] in selectedIds]
                self.store.control([job['id'] for job in selected
                                    if job['status'] not in ('complete', 'complete_warnings')], 'pause')
                self.waitForJobs(payload['jobIds'])
                self.waitForWriter()
                dirty = [job['id'] for job in self.store.jobs() if job['id'] in selectedIds
                         and job['completedCount']
                         and (job['dataVersion'] != job['exportVersion'] or job['exportError'])]
                for index, jobId in enumerate(dirty):
                    self.operationProgress(index, len(dirty), 'Saving changed exports before removal')
                    self.exportSelected([jobId])
                self.store.deleteJobs(payload['jobIds'], self.operationProgress)
                self.emit('jobsChanged', None)
            else:
                raise ValueError(f'Unknown worker operation {action}')
        except WorkSuspended:
            if action in ('verifyPids', 'fetchMissingPids') and not self.stopEvent.is_set():
                self.emit('notice', 'PID operation stopped. Saved progress is retained; all jobs remain paused.')
            else:
                raise
        except InterruptedError:
            raise
        except AuthenticationError:
            self.expireSession()
            self.emit('actionFailed', {'action': action, 'message': 'Please renew login and try again.'})
        except Exception as exception:
            logging.exception(f'Application action {action} failed')
            self.emit('actionFailed', {'action': action, 'message': str(exception)})
        finally:
            logging.info(f'Finished application action {action} in {time.monotonic() - operationStarted:.2f}s')
            if action != 'exportMunicipality' or self.municipalityExport is None:
                self.emit('actionFinished', action)

    def beginMunicipalityExport(self, municipalityId, destinationRoot=None):
        '''Pause the selected municipality before its background export begins.'''
        logging.info('Preparing municipality master export')
        if self.municipalityExport:
            raise ValueError('A municipality export is already in progress')
        jobs = [job for job in self.store.jobs() if job['municipalityId'] == municipalityId]
        if not jobs:
            raise ValueError('No queued colonies were found for this municipality')
        roots = {str(Path(job['folder']).resolve().parent.parent) for job in jobs}
        if destinationRoot is None:
            if len(roots) != 1:
                raise ValueError('Choose an output root for the Master folder')
            destinationRoot = next(iter(roots))
        self.store.control([job['id'] for job in jobs
                            if job['status'] not in ('complete', 'complete_warnings')], 'pause')
        self.municipalityExport = {'jobs': jobs, 'root': destinationRoot, 'future': None}
        self.operationProgress(0, 0, 'Pausing municipality colonies and saving current work...')
        self.emit('jobsChanged', None)

    def collectMunicipalityExport(self, wait=False):
        '''Start after scoped requests stop, then publish background completion.'''
        pending = self.municipalityExport
        if not pending:
            return
        if any(job['id'] in self.inFlight for job in pending['jobs']):
            return
        finished = False
        try:
            if pending['future'] is None:
                pending['future'] = self.masterWriter.submit(
                    exportMunicipality, self.store, pending['jobs'], pending['root'], self.operationProgress)
            if not wait and not pending['future'].done():
                return
            finished = True
            result = pending['future'].result()
            history = self.store.setting('municipalityExports', {})
            history[result['municipalityId']] = result
            self.store.setSetting('municipalityExports', history)
            self.emit('municipalityExported', result)
        except Exception as exception:
            finished = True
            logging.exception('Municipality master export failed')
            self.emit('actionFailed', {'action': 'exportMunicipality',
                'message': f'Export could not finish: {exception}. Close any open municipality workbook and try again. '
                           'The municipality stays paused.'})
        finally:
            if finished:
                self.municipalityExport = None
                self.emit('actionFinished', 'exportMunicipality')

    def operationProgress(self, current, total, message):
        '''Report long operations without flooding the UI. Inputs: <current>, <total>, <message>.'''
        now = time.monotonic()
        if now - getattr(self, 'lastOperationProgress', 0) >= 0.1 or (total > 0 and current == total):
            self.lastOperationProgress = now
            self.emit('operationProgress', {'current': current, 'total': total, 'message': message})

    def runPidOperation(self, action, municipalityId, root=None):
        '''Pause all work for an explicit local comparison or targeted recovery.'''
        logging.info('Starting on-demand PID operation')
        if self.municipalityExport:
            raise ValueError('Wait for the municipality export to finish')
        self.pidCancel.clear()
        catalog = [entry for entry in self.store.catalog('municipality')
                   if entry['id'] == municipalityId and entry['active']]
        if len(catalog) != 1:
            raise ValueError('Match this municipality to the portal catalog first')
        municipality = catalog[0]
        jobs = self.store.jobs()
        self.elapsed.tick(jobs, self.authenticated, save=True)
        self.store.control([job['id'] for job in jobs if job['desired']], 'pause')
        self.elapsed.tick(self.store.jobs(), self.authenticated, save=True)
        self.emit('jobsChanged', None)
        self.operationProgress(0, 0, 'Pausing all jobs and waiting for current saves...')
        self.waitForJobs([job['id'] for job in jobs])
        self.waitForWriter()
        selectedRoot = str(Path(root).resolve()) if root else None
        jobRoots = {str(Path(job['folder']).resolve().parent.parent) for job in jobs
                    if job['municipalityId'] == municipalityId}
        roots = ([selectedRoot] if selectedRoot else []) + sorted(jobRoots - {selectedRoot})
        if not roots:
            raise ValueError('Choose the dataset output root to verify')
        result = self.pidReference.verify(self.store, municipalityId, municipality['name'], roots, self.pidProgress)
        if action == 'fetchMissingPids':
            if not self.authenticated:
                raise AuthenticationError('Verify your login before fetching missing PIDs')
            result = self.recoverPids(municipality, result)
        self.emit('pidVerified', result)

    def pidProgress(self, current, total, message):
        '''Allow cancellation between bounded verification and recovery steps.'''
        if self.stopEvent.is_set() or self.pidCancel.is_set():
            raise WorkSuspended('PID operation stopped; saved progress is retained')
        self.operationProgress(current, total, message)

    def pidRequest(self, operation, *args, **kwargs):
        '''Use the shared request gate and bounded retries for recovery requests.'''
        for attempt in range(3):
            self.pidProgress(0, 0, 'Fetching missing properties...')
            try:
                return self.network(operation, *args, **kwargs)
            except (AuthenticationError, WorkSuspended):
                raise
            except (PortalHttpError, PortalResponseError, OSError, RuntimeError) as exception:
                if attempt == 2:
                    raise
                delay = max(2 ** (attempt + 1), getattr(exception, 'retryAfter', 0),
                            self.retryUntil - time.monotonic() if self.servicePaused else 0)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self.pidProgress(0, 0, 'Portal request failed; waiting before retry...')
                    self.stopEvent.wait(min(.2, max(0, deadline - time.monotonic())))

    def recoverPids(self, municipality, verification):
        '''Resolve and save missing properties, then refresh existing Master workbooks.'''
        logging.info('Fetching verified missing properties')
        municipalityId = municipality['id']
        client = self.client
        colonies = self.pidRequest(client.requestJson, 'GET', '/Widget/GetULBColonySurvey?' +
                                   parse.urlencode({'fkULBId': municipalityId}), referer=BASEURL)
        self.validateCatalog(colonies)
        touched = set()
        failures = 0
        fetched = 0
        cancelled = False
        lastPid = ''
        total = verification['missing'] + verification['pending']
        try:
            while True:
                with closing(self.pidReference.connect()) as reference:
                    targets = reference.execute("SELECT pid FROM results WHERE municipalityId=? AND state!='found' AND pid>? ORDER BY pid LIMIT 100",
                                                (municipalityId, lastPid)).fetchall()
                if not targets:
                    break
                for target in targets:
                    pid = target['pid']
                    lastPid = pid
                    self.pidProgress(fetched + failures, total, f'Fetching missing properties: {fetched:,} saved; {failures:,} need review')
                    try:
                        raw = self.pidRequest(client.lookupPid, municipalityId, pid)
                        colonyId = str(raw.get('fkColonySurveyId') or '')
                        matches = [entry for entry in colonies if
                                   (colonyId and str(entry['Value']) == colonyId) or
                                   (not colonyId and municipalityKey(entry['Text']) == municipalityKey(raw.get('ColonyName')))]
                        if len(matches) != 1 or str(matches[0]['Value']) in ('', '0', '-1'):
                            raise ValueError('The property colony could not be matched uniquely; no destination was guessed')
                        colony = {'id': str(matches[0]['Value']), 'name': matches[0]['Text']}
                        job = recoveryJob(self.store, municipality, colony, verification['roots'])
                        touched.add(job['id'])
                        identity = queueProperty(self.store, job, raw)
                        if identity:
                            parsed = self.pidRequest(client.fetchDetails, raw)
                            self.store.saveDetail(job['id'], identity, parsed, propertyPhotoUrls(raw, parsed),
                                                  state=parsed['Detail']['FetchStatus'])
                            for photo in self.store.propertyPhotos(job['id'], identity):
                                if photo['state'] == 'pending':
                                    path = self.pidRequest(client.downloadPhoto, photo['url'], job['folder'], identity)
                                    self.store.savePhoto(job['id'], identity, photo['url'], path)
                            if not self.store.finishProperty(job['id'], identity):
                                raise ValueError('Property processing is still incomplete')
                            self.store.persistDataset(job['id'])
                        fetched += 1
                        self.pidReference.mark(municipalityId, pid, 'found')
                    except (AuthenticationError, WorkSuspended):
                        raise
                    except Exception as exception:
                        logging.exception('A missing property could not be recovered')
                        failures += 1
                        self.pidReference.mark(municipalityId, pid, 'failed', str(exception))
                        if self.servicePaused:
                            raise WorkSuspended('Portal is unavailable; retry the remaining PIDs later')
        except WorkSuspended:
            cancelled = True
        finally:
            # Checkpoints are authoritative even if a locked workbook cannot be replaced.
            exportErrors = []
            for jobId in touched:
                try:
                    exportJob(self.store, self.store.job(jobId))
                except Exception as exception:
                    exportErrors.append(str(exception))
                    self.store.exportResult(jobId, self.store.job(jobId)['dataVersion'], str(exception))
            if touched and not exportErrors:
                jobs = [job for job in self.store.jobs() if job['municipalityId'] == municipalityId]
                history = self.store.setting('municipalityExports', {})
                masterRoots = set(verification['roots'])
                if municipalityId in history:
                    masterRoots.add(str(Path(history[municipalityId]['path']).parent.parent))
                for destination in masterRoots:
                    marker = Path(destination) / 'Master' / f'.municipality-{municipalityId}.json'
                    if not marker.exists():
                        continue
                    try:
                        exported = exportMunicipality(self.store, jobs, destination, self.operationProgress,
                                                      sourceRoots=verification['roots'])
                        history[municipalityId] = exported
                        self.store.setSetting('municipalityExports', history)
                        self.emit('pidMasterUpdated', exported)
                    except Exception as exception:
                        exportErrors.append(str(exception))
            self.emit('jobsChanged', None)
        result = self.pidReference.verify(self.store, municipalityId, municipality['name'], verification['roots'],
                                          self.operationProgress, preserveErrors=True)
        result.update(fetched=fetched, failures=failures, cancelled=cancelled, exportErrors=exportErrors)
        return result

    def refreshMunicipalities(self):
        '''Update municipality options from the portal. Inputs: None. Output: None.'''
        logging.info('Refreshing municipality catalog')
        entries = self.network(self.client.requestJson, 'GET', '/Widget/GetULBListPropertyWise', referer=BASEURL)
        self.validateCatalog(entries)
        self.store.updateCatalog('municipality', '', entries)
        self.emit('catalog', {'kind': 'municipality', 'parent': ''})

    def refreshColonies(self, municipalityId):
        '''Update one municipality's colony options. Inputs: <municipalityId>. Output: None.'''
        logging.info(f'Refreshing colonies for municipality {municipalityId}')
        url = '/Widget/GetULBColonySurvey?' + parse.urlencode({'fkULBId': municipalityId})
        entries = self.network(self.client.requestJson, 'GET', url, referer=BASEURL)
        self.validateCatalog(entries)
        self.store.updateCatalog('colony', str(municipalityId), entries)
        self.emit('catalog', {'kind': 'colony', 'parent': str(municipalityId)})

    def validateCatalog(self, entries):
        '''Reject malformed option responses before retiring cached options. Inputs: <entries>. Output: None.'''
        logging.debug('Validating catalog response')
        if not isinstance(entries, list) or any(not isinstance(item, dict) or
                'Text' not in item or 'Value' not in item for item in entries):
            raise PortalResponseError('Portal returned an unexpected catalog format')

    def addSelection(self, municipalityId, colonyId, folder):
        '''
        Refresh and expand an explicit selection or ALL before atomic insertion.

        Inputs:    <municipalityId>  -  Municipality ID or ALL
                   <colonyId>        -  Colony ID or ALL
                   <folder>          -  Export root

        Output:    None; job change events emitted after success
        '''
        logging.info('Expanding scraping selection')
        self.refreshMunicipalities()
        municipalities = [item for item in self.store.catalog('municipality') if item['active']
                          and (municipalityId == 'ALL' or item['id'] == municipalityId)]
        if not municipalities:
            raise ValueError('Selected municipality is no longer available')
        pairs = []
        for municipalityIndex, municipality in enumerate(municipalities, start=1):
            self.emit('notice', f'Loading colonies: {municipality["name"]} '
                               f'({municipalityIndex}/{len(municipalities)})')
            self.refreshColonies(municipality['id'])
            colonies = [item for item in self.store.catalog('colony', municipality['id']) if item['active']
                       and (municipalityId == 'ALL' or colonyId == 'ALL' or item['id'] == colonyId)]
            if not colonies and colonyId != 'ALL' and municipalityId != 'ALL':
                raise ValueError('Selected colony is no longer available')
            pairs.extend((municipality, colony) for colony in colonies)
        if not pairs:
            raise ValueError('No active colonies were returned by the portal')
        replaceAll = municipalityId == 'ALL'
        replaceMunicipality = municipalityId if colonyId == 'ALL' and not replaceAll else None
        if replaceAll or replaceMunicipality:
            existing = [job for job in self.store.jobs() if replaceAll or job['municipalityId'] == replaceMunicipality]
            self.store.control([job['id'] for job in existing], 'pause')
            self.waitForJobs([job['id'] for job in existing])
            self.waitForWriter()
            self.exportSelected([job['id'] for job in existing if job['completedCount']])
            self.archiveOutputs(existing)
        identifiers = self.addQueuedPairs(pairs, folder, replaceMunicipality, replaceAll)
        self.emit('jobsChanged', None)
        self.emit('notice', f'{len(identifiers)} colony jobs added' if identifiers else 'That colony is already in the queue')

    def addSelections(self, selections, folder, globalAll=False):
        '''
        Validate grouped checked selections before adding or replacing any jobs.

        Inputs:    <selections>  -  Municipality IDs mapped to colony IDs or ALL
                   <folder>      -  Output root
                   [globalAll]   -  Explicitly confirmed global ALL replacement

        Output:    None; emits queue and progress events
        '''
        logging.info('Adding checked municipality and colony selections')
        self.refreshMunicipalities()
        municipalities = {entry['id']: entry for entry in self.store.catalog('municipality') if entry['active']}
        if globalAll:
            selections = {identity: ['ALL'] for identity in municipalities}
        if not selections or any(identity not in municipalities for identity in selections):
            raise ValueError('A selected municipality is no longer available')
        pairs = []
        replacements = set()
        for municipalityId, selected in selections.items():
            if not selected:
                raise ValueError('Select at least one colony for each municipality')
            self.refreshColonies(municipalityId)
            colonies = {entry['id']: entry for entry in self.store.catalog('colony', municipalityId) if entry['active']}
            if 'ALL' in selected:
                replacements.add(municipalityId)
                chosen = list(colonies)
            else:
                chosen = list(dict.fromkeys(selected))
                if any(identity not in colonies for identity in chosen):
                    raise ValueError('A selected colony is no longer available')
            pairs.extend((municipalities[municipalityId], colonies[identity]) for identity in chosen)
        if not pairs:
            raise ValueError('No active colonies were returned by the portal')
        existing = [job for job in self.store.jobs() if globalAll or job['municipalityId'] in replacements]
        if existing:
            identifiers = [job['id'] for job in existing]
            self.store.control(identifiers, 'pause')
            self.waitForJobs(identifiers)
            self.waitForWriter()
            self.exportSelected([job['id'] for job in existing if job['completedCount']])
            self.archiveOutputs(existing)
        identifiers = self.addQueuedPairs(pairs, folder, replaceAll=globalAll, replaceMunicipalities=replacements)
        self.emit('jobsChanged', None)
        self.emit('notice', f'{len(identifiers)} colony jobs added' if identifiers else 'Selected colonies are already in the queue')

    def addQueuedPairs(self, pairs, folder, replaceMunicipality=None, replaceAll=False, replaceMunicipalities=()):
        '''Restore portable re-adds and reject silent output replacement. Inputs: <pairs>, <folder>, [replacement scope]. Output: IDs.'''
        logging.info('Checking selected dataset locations before adding jobs')
        replacements = set(replaceMunicipalities) | ({replaceMunicipality} if replaceMunicipality else set())
        restores = []
        archives = []
        inventories = {}
        rootFolder = Path(folder).resolve()
        existing = {(job['municipalityId'], job['colonyId']): job for job in self.store.jobs()}
        for municipality, colony in pairs:
            destination = rootFolder / f'{slugify(municipality["name"])}_{municipality["id"]}' / f'{slugify(colony["name"])}_{colony["id"]}'
            job = existing.get((municipality['id'], colony['id']))
            replacing = replaceAll or municipality['id'] in replacements
            if job and not replacing:
                if Path(job['folder']).resolve().parent.parent != rootFolder:
                    raise ValueError('This colony is already queued in another folder. Remove its queue entry before changing location.')
                continue
            candidates = [child for parent in rootFolder.glob(f'*_{municipality["id"]}') if parent.is_dir()
                          for child in parent.glob(f'*_{colony["id"]}') if child.is_dir()]
            if len(candidates) > 1:
                raise ValueError('Multiple folders identify this municipality/colony. Use Update data to review the dataset.')
            if candidates:
                destination = candidates[0]
            if replacing:
                if destination.exists():
                    archives.append(dict(municipalityName=municipality['name'], colonyName=colony['name'],
                                         id=uuid.uuid4().hex, folder=str(destination)))
            elif (destination / datasets.CHECKPOINT).exists():
                root = str(destination.parent.parent)
                inventory = inventories.setdefault(root, None)
                if inventory is None:
                    inventory = datasets.inspectDataset(root)
                    inventories[root] = inventory
                restores.extend(entry for entry in inventory['entries'] if Path(entry['folder']) == destination)
            elif any((destination / name).exists() for name in ('properties.csv', 'properties.xlsx')):
                raise ValueError('Existing property files found. Use Update data to refresh this folder safely.')
        self.archiveOutputs(archives)
        identifiers = []
        for root, inventory in inventories.items():
            selected = [entry for entry in restores if Path(entry['folder']).parent.parent == Path(root)]
            identifiers.extend(self.store.importDataset(dict(inventory, entries=selected),
                                                       catalogChecked=self.authenticated, restoreOnly=True))
        identifiers.extend(self.store.addJobs(pairs, folder, replaceMunicipality, replaceAll, replaceMunicipalities))
        return identifiers

    def importDataset(self, inventory, restart=False, mobileOnly=False):
        '''Revalidate confirmation and begin or resume scoped update jobs. Inputs: <inventory>, [restart]. Output: None.'''
        logging.info('Starting confirmed folder update')
        mobileOnly = mobileOnly or inventory.get('mobileOnly', False)
        datasets.revalidateDataset(inventory, self.operationProgress)
        current = {key: value for key, value in inventory.items() if key != 'mobileOnly'}
        if self.authenticated:
            self.refreshMunicipalities()
            active = {entry['id'] for entry in self.store.catalog('municipality') if entry['active']}
            coloniesByMunicipality = {}
            for entry in current['entries']:
                if entry['municipalityId'] not in active:
                    raise ValueError('An imported municipality is no longer in the portal catalog; its data has not been changed')
                if entry['municipalityId'] not in coloniesByMunicipality:
                    self.operationProgress(len(coloniesByMunicipality), 0, 'Checking municipality and colony lists')
                    self.refreshColonies(entry['municipalityId'])
                    coloniesByMunicipality[entry['municipalityId']] = {colony['id'] for colony in
                        self.store.catalog('colony', entry['municipalityId']) if colony['active']}
                if entry['colonyId'] not in coloniesByMunicipality[entry['municipalityId']]:
                    raise ValueError('An imported colony is no longer in the portal catalog; its data has not been changed')
        entriesByPair = {(entry['municipalityId'], entry['colonyId']): entry for entry in current['entries']}
        affected = [job for job in self.store.jobs() if (job['municipalityId'], job['colonyId']) in entriesByPair]
        for job in affected:
            entry = entriesByPair[(job['municipalityId'], job['colonyId'])]
            if Path(job['folder']).resolve() != Path(entry['folder']).resolve():
                raise ValueError('A selected colony is queued in another output folder; remove that queue entry first')
        draining = [job['id'] for job in affected if restart or job['mode'] != 'update'
                    or job['status'] in ('complete', 'complete_warnings')]
        drainingIds = set(draining)
        self.store.control([job['id'] for job in affected if job['id'] in drainingIds and
                            job['status'] not in ('complete', 'complete_warnings')], 'pause')
        self.waitForJobs(draining)
        self.waitForWriter()
        for jobId in draining:
            self.store.persistDataset(jobId)
        reportRoot = ''
        if mobileOnly:
            reportRoot = str(Path(current['root']) / datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('Updated-mobileNos-%Y%m%d-%H%M%S-%f'))
        identifiers = self.store.importDataset(current, restart=restart, catalogChecked=self.authenticated,
            mobileOnly=mobileOnly, reportRoot=reportRoot, progress=self.operationProgress)
        self.retainedExports.update(identifiers)
        for index, jobId in enumerate(identifiers):
            self.operationProgress(index, len(identifiers), 'Saving imported colony checkpoints')
            self.geometryCache.pop(jobId, None)
            self.store.persistDataset(jobId)
        self.emit('jobsChanged', None)
        self.emit('datasetImported', current['root'])
        self.emit('notice', f'{len(identifiers)} update jobs prepared. Existing rows are retained while current data is fetched.')

    def waitForJobs(self, identifiers):
        '''Drain affected workers before deleting or replacing their checkpoints. Inputs: <identifiers>.'''
        logging.debug('Waiting for scoped workers to stop')
        while any(jobId in self.inFlight for jobId in identifiers):
            self.collectWork()
            if self.stopEvent.wait(0.02):
                raise WorkSuspended('Application is closing')

    def waitForWriter(self):
        '''Finish the current automatic workbook update before folder mutations. Inputs: None.'''
        logging.debug('Waiting for workbook writer')
        if self.exportFuture:
            self.exportFuture.result()
            self.exportFuture = None

    def archiveOutputs(self, jobs):
        '''Retain previous exports before a confirmed fresh run. Inputs: <jobs>. Output: None.'''
        logging.info(f'Archiving outputs for {len(jobs)} reset jobs')
        stamp = time.strftime('%Y%m%d_%H%M%S')
        for job in jobs:
            if self.store.job(job['id']):
                self.store.persistDataset(job['id'])
            folder = Path(job['folder'])
            if folder.exists():
                destination = folder.parent.parent / '_archive' / f'{stamp}_{job["id"][:8]}' / folder.parent.name / folder.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(destination))

    def advance(self, job):
        '''
        Perform one checkpointed unit of work, then yield to other jobs.

        Inputs:    <job>  -  Current desired colony job

        Output:    None
        '''
        logging.debug(f'Advancing colony {job["colonyId"]}')
        jobId = job['id']
        client = self.workerClient()
        if job['mode'] == 'update' and not job['catalogChecked']:
            municipalities = self.network(client.requestJson, 'GET', '/Widget/GetULBListPropertyWise', referer=BASEURL)
            self.validateCatalog(municipalities)
            colonies = self.network(client.requestJson, 'GET', '/Widget/GetULBColonySurvey?' + parse.urlencode({'fkULBId': job['municipalityId']}), referer=BASEURL)
            self.validateCatalog(colonies)
            if (job['municipalityId'] not in {str(entry['Value']) for entry in municipalities} or
                    job['colonyId'] not in {str(entry['Value']) for entry in colonies}):
                raise DatasetCatalogError('Imported municipality or colony is unavailable; previous data is retained for review')
            with self.store.connect() as connection:
                connection.execute('UPDATE jobs SET catalogChecked=1 WHERE id=?', (jobId,))
            return
        if not job['boundary']:
            self.store.setStatus(jobId, 'preparing')
            url = '/Widget/GetColonyBoundry?' + parse.urlencode({'fkColonySurveyId': job['colonyId']})
            boundary = self.network(client.requestJson, 'GET', url, referer=f'{BASEURL}/Home/Searchproperty')
            geometry = readBoundary(boundary['Wkt'])
            self.store.initializeTiles(jobId, boundary['Wkt'], seedBounds(geometry))
            self.geometryCache[jobId] = geometry
            return
        record = self.store.pendingProperty(jobId)
        if record:
            self.local.recordIdentity = record['identity']
            if record['retryAt'] > time.time():
                return
            self.store.setStatus(jobId, 'scraping')
            if record['refreshToken']:
                self.refreshPropertyToken(job, record)
                return
            raw = json.loads(record['raw'])
            if record['detailState'] == 'pending':
                parsed = (client.fetchDetails(raw) if publicViewRestricted(raw) or not raw.get('Token')
                          else self.network(client.fetchDetails, raw))
                self.store.saveDetail(jobId, record['identity'], parsed,
                                      [] if job['updateScope'] == 'missing_mobile' else propertyPhotoUrls(raw, parsed),
                                      state=parsed['Detail']['FetchStatus'])
            for photo in self.store.propertyPhotos(jobId, record['identity']):
                if photo['state'] == 'pending':
                    photoIdentity = f'{record["identity"]}_g{job["generation"]}' if job['mode'] == 'update' else record['identity']
                    path = self.network(client.downloadPhoto, photo['url'], job['folder'], photoIdentity)
                    self.store.savePhoto(jobId, record['identity'], photo['url'], path)
            self.store.finishProperty(jobId, record['identity'])
            logging.info(f'{job["municipalityName"]}/{job["colonyName"]} property={record["identity"]} saved with details and photos')
            return
        geometry = self.geometryCache.get(jobId)
        if geometry is None:
            geometry = readBoundary(job['boundary'])
            self.geometryCache[jobId] = geometry
        tile = self.store.pendingTile(jobId)
        if tile:
            if tile['retryAt'] > time.time():
                return
            self.store.setStatus(jobId, 'scraping')
            self.scanTile(job, tile, geometry)
            return
        if job['mode'] == 'update':
            self.store.finishUpdate(jobId)
        problems = self.store.problemCount(jobId)
        self.store.setStatus(jobId, 'complete_warnings' if problems else 'complete',
                             f'{problems} records or tiles need retry' if problems else '', desired=0)
        self.forceExport.add(jobId)
        self.emit('notice', f'{job["municipalityName"]} / {job["colonyName"]} completed')

    def refreshPropertyToken(self, job, record):
        '''Fetch a current marker for a rejected token without resetting map coverage. Inputs: <job>, <record>. Output: None.'''
        logging.info('Refreshing rejected property marker token')
        raw = json.loads(record['raw'])
        latitude, longitude = float(raw['Lat']), float(raw['Long'])
        bounds = (longitude - 0.0005, latitude - 0.0005, longitude + 0.0005, latitude + 0.0005)
        data = {'pageNo': '1', 'pageSize': '500', 'efkUlbId': encryptPortalValue(job['municipalityId']),
                'PID': '', 'OwnerName': '', 'MobileNo': '', 'fkColonySurveyId': job['colonyId'],
                'fkPropertyCategoryId': '', 'wkt': buildRectWkt(*bounds), 'searchType': '3', 'PIDNDC': '', 'PlotNo': ''}
        rows = self.network(self.workerClient().requestJson, 'POST', '/SearchProperty/getSearchPropertiesData',
                            data=data, referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap')
        if not isinstance(rows, list):
            raise PortalResponseError('Unexpected token-refresh map response')
        geometry = readBoundary(job['boundary'])
        marker = next((row for row in rows if isinstance(row, dict) and
                       str(propertyIdentity(row)) == record['identity'] and containsProperty(geometry, row)), None)
        if not marker:
            raise PortalResponseError('The rejected property marker was not found in the current colony map; previous data retained')
        self.store.saveRefreshedMarker(job['id'], record['identity'], marker, propertyPhotoUrls(marker))

    def scanTile(self, job, tile, geometry):
        '''
        Fetch one tile, filter coordinates, and split or paginate capped results.

        Inputs:    <job>       -  Colony job
                   <tile>      -  Pending tile
                   <geometry>  -  Selected polygon

        Output:    None
        '''
        logging.debug(f'Scanning tile {tile["id"]}, page {tile["page"]}')
        bounds = json.loads(tile['bounds'])
        pageSize = 500
        data = {
            'pageNo':               str(tile['page']),
            'pageSize':             str(pageSize),
            'efkUlbId':             encryptPortalValue(job['municipalityId']),
            'PID':                 '',
            'OwnerName':           '',
            'MobileNo':            '',
            'fkColonySurveyId':    job['colonyId'],
            'fkPropertyCategoryId': '',
            'wkt':                 buildRectWkt(*bounds),
            'searchType':          '3',
            'PIDNDC':              '',
            'PlotNo':              '',
        }
        rows = self.network(self.workerClient().requestJson, 'POST', '/SearchProperty/getSearchPropertiesData',
                            data=data, referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PortalResponseError('Unexpected map response; tile retained for retry')
        accepted = [row for row in rows if containsProperty(geometry, row)]
        total = max([int(row.get('TotalRecord') or 0) for row in rows] or [0])
        capped = len(rows) >= pageSize or total > tile['page'] * pageSize
        children = splitBounds(bounds, geometry) if capped and tile['depth'] < 8 else None
        nextPage = tile['page'] + 1 if capped and not children else None
        if nextPage and nextPage > 10000:
            raise PortalResponseError('Dense tile exceeded safe pagination bound; manual review required')
        fingerprint = hashlib.sha256(json.dumps(sorted(str(row.get('pkPropertyId') or row.get('PID') or '')
            for row in rows)).encode()).hexdigest()
        if tile['page'] > 1 and rows and fingerprint == tile['lastFingerprint']:
            raise PortalResponseError('Map pagination repeated the previous page; coverage needs review')
        self.store.saveTile(job['id'], tile, accepted, propertyPhotoUrls, children, nextPage, fingerprint)
        logging.info(f'{job["municipalityName"]}/{job["colonyName"]} tile={tile["id"]} rows={len(rows)} accepted={len(accepted)}')

    def handleFailure(self, job, exception):
        '''Schedule bounded retries while preserving pending work. Inputs: <job>, <exception>. Output: None.'''
        logging.warning(f'{job["municipalityName"]}/{job["colonyName"]}: {type(exception).__name__}: {exception}')
        if isinstance(exception, PortalHttpError) and exception.status in (500, 302):
            self.failureSessionCheckPending = True
            self.checkFailureSession()
            if not self.authenticated:
                return
        if getattr(exception, 'sharedFailure', False) or isinstance(exception, PortalHttpError) and exception.status == 429:
            self.store.setStatus(job['id'], 'retrying', str(exception))
            return
        jobId = job['id']
        record = self.store.pendingProperty(jobId)
        tile = self.store.pendingTile(jobId) if not record else None
        attempts = (record or tile or job)['attempts'] + 1
        delay = max(min(120, 2 ** attempts), getattr(exception, 'retryAfter', 0))
        message = str(exception)
        if record:
            if attempts < 3:
                self.store.propertyRetry(jobId, record['identity'], message, delay)
                self.store.setStatus(jobId, 'retrying', message)
                return
            if record['detailState'] == 'pending':
                self.store.saveDetail(jobId, record['identity'], {'Detail': {
                    'FetchStatus': 'error', 'FetchError': message}}, [], state='error')
            else:
                pending = next((photo for photo in self.store.propertyPhotos(jobId, record['identity'])
                                if photo['state'] == 'pending'), None)
                if pending:
                    self.store.photoError(jobId, record['identity'], pending['url'], message)
            self.store.finishProperty(jobId, record['identity'])
            self.emit('notice', f'{job["colonyName"]}: one property needs retry; other records will continue.')
        elif tile:
            self.store.tileRetry(jobId, tile['id'], message, delay, terminal=attempts >= 5)
            self.store.setStatus(jobId, 'retrying', message)
        else:
            current = self.store.job(jobId)
            if current and current['desired']:
                self.store.jobRetry(jobId, message, delay)
                if attempts >= 5:
                    self.store.setStatus(jobId, 'error', message, desired=0)
                else:
                    self.store.setStatus(jobId, 'retrying', message)

    def checkFailureSession(self):
        '''Coalesce ambiguous server-error login checks outside cooldown. Inputs: None. Output: None.'''
        logging.debug('Checking whether a failure session check is due')
        now = time.monotonic()
        if now - self.lastFailureSessionCheck < 60 or self.servicePaused and now < self.retryUntil:
            return
        self.lastFailureSessionCheck = now
        self.failureSessionCheckPending = False
        try:
            self.network(self.client.checkSession)
            self.lastSessionCheck = time.monotonic()
        except (AuthenticationError, WorkSuspended):
            return
        except Exception:
            logging.warning('Session recheck could not complete')

    def exportSelected(self, jobIds):
        '''Publish selected snapshots without scraping. Inputs: <jobIds>. Output: None.'''
        logging.info(f'Export requested for {len(jobIds)} jobs')
        for jobId in jobIds:
            job = self.store.job(jobId)
            if not job:
                continue
            try:
                exportJob(self.store, job)
            except Exception as exception:
                logging.exception('Excel export failed')
                self.store.exportResult(jobId, job['dataVersion'], str(exception))
                self.emit('notice', f'Export failed for {job["colonyName"]}: {exception}')
            finally:
                self.exportAttempts[jobId] = time.monotonic()
                self.publishedCounts[jobId] = job['completedCount']

    def writeAutomatically(self, jobId):
        '''Publish one automatic Excel batch off the coordinator thread. Inputs: <jobId>. Output: elapsed seconds.'''
        logging.debug('Publishing automatic workbook batch')
        started = time.monotonic()
        self.exportSelected([jobId])
        return time.monotonic() - started

    def flushExports(self, force=False):
        '''Automatically save small completed-property batches and paused jobs. Inputs: [force]. Output: None.'''
        logging.debug('Checking pending Excel snapshots')
        if self.exportFuture and self.exportFuture.done():
            self.exportDuration[self.exportJobId] = self.exportFuture.result()
            self.exportFuture = None
            self.exportJobId = None
        if self.exportFuture:
            if not force:
                return
            self.waitForWriter()
        for job in self.store.jobs():
            dirty = job['dataVersion'] != job['exportVersion']
            hasData = bool(job['completedCount'] or job['baselineCount'] or job['id'] in self.retainedExports or
                           job['status'] in ('complete', 'complete_warnings'))
            duration = self.exportDuration.get(job['id'], 0)
            automaticDelay = max(5, duration * 10)
            due = time.time() - job['lastExport'] >= automaticDelay
            changedCount = job['completedCount'] - self.publishedCounts.get(job['id'], 0)
            inactive = not job['desired'] or not self.authenticated or self.servicePaused
            requested = job['id'] in self.forceExport
            cooldown = 60 if job['exportError'] else automaticDelay
            attempted = time.monotonic() - self.exportAttempts.get(job['id'], -cooldown) >= cooldown
            if hasData and dirty and (force or (job['id'] not in self.inFlight and requested) or
                    (attempted and ((inactive and job['id'] not in self.inFlight) or due or changedCount >= 25))):
                self.forceExport.discard(job['id'])
                if force or not hasattr(self, 'writer'):
                    self.exportSelected([job['id']])
                else:
                    self.exportJobId = job['id']
                    self.exportFuture = self.writer.submit(self.writeAutomatically, job['id'])
                    break
