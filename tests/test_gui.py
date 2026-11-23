'''Offscreen desktop tests for dependent selections and group actions.'''

# Standard Library Imports
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# Third Party Imports
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QMessageBox, QSpinBox

from ulb_scraper.gui import MainWindow
from ulb_scraper.store import Store


class CatalogClient:
    '''Deterministic HTTP catalog for exercising actual Add controls.'''

    def requestJson(self, method, url, **kwargs):
        if 'GetULBList' in url:
            return [{'Value': '60', 'Text': 'PANCHKULA'}, {'Value': '61', 'Text': 'OTHER'}]
        parent = parse_qs(urlsplit(url).query)['fkULBId'][0]
        return ([{'Value': '1', 'Text': 'Sector 6'}, {'Value': '2', 'Text': 'Sector 7'}]
                if parent == '60' else [{'Value': '3', 'Text': 'Other colony'}])


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = Store(self.home / 'state.sqlite3')
        self.store.updateCatalog('municipality', '', [{'Value': '60', 'Text': 'PANCHKULA'}, {'Value': '61', 'Text': 'OTHER'}])
        self.store.updateCatalog('colony', '60', [{'Value': '1', 'Text': 'Sector 6'}, {'Value': '2', 'Text': 'Sector 7'}])
        self.store.updateCatalog('colony', '61', [{'Value': '3', 'Text': 'Other colony'}])
        self.window = MainWindow(self.store, self.home, startWorker=False)
        self.window.handleEvent('session', 'valid')

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.application.processEvents()
        self.temporary.cleanup()

    def addColony(self, colonyId='1'):
        return self.store.addJobs([({'id': '60', 'name': 'PANCHKULA'},
                                   {'id': colonyId, 'name': 'Sector ' + colonyId})], self.home / 'exports')[0]

    def drainCommands(self):
        while True:
            try:
                action, payload = self.window.engine.commands.get_nowait()
            except queue.Empty:
                return
            self.window.engine.handleCommand(action, payload)
            self.application.processEvents()

    def testLongOperationsShowProgressAndKeepQueueLockedUntilFinished(self):
        self.window.request('inspectDataset', folder=str(self.home))
        self.assertFalse(self.window.operationPanel.isHidden())
        self.assertEqual(self.window.operationProgress.maximum(), 0)
        self.window.handleEvent('operationProgress', {'current': 25, 'total': 100, 'message': 'Loading colonies'})
        self.assertEqual(self.window.operationProgress.value(), 25)
        self.window.request('importDataset', inventory={})
        self.window.handleEvent('actionFinished', 'inspectDataset')
        self.assertFalse(self.window.tree.isEnabled())
        self.window.handleEvent('actionFinished', 'importDataset')
        self.assertTrue(self.window.operationPanel.isHidden())
        self.assertTrue(self.window.tree.isEnabled())

    def testMunicipalityExportButtonQueuesOnlyAfterConfirmation(self):
        self.addColony()
        self.window.refreshJobs()
        parent = self.window.groupItems['60']
        widget = self.window.tree.itemWidget(parent, 0)
        self.assertIsNotNone(widget)
        self.assertEqual(self.window.groupExportLabels['60'].text(), 'Last exported: Never')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel), \
                patch.object(self.window, 'request') as request:
            self.window.exportMunicipality('60')
        request.assert_not_called()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Ok):
            self.window.exportMunicipality('60')
        self.assertIn('exportMunicipality', self.window.busyActions)
        self.assertFalse(self.window.tree.isEnabled())
        self.assertFalse(self.window.operationPanel.isHidden())
        action, payload = self.window.engine.commands.get_nowait()
        self.assertEqual(action, 'exportMunicipality')
        self.assertEqual(payload['municipalityId'], '60')
        self.assertTrue(str(payload['destinationRoot']).endswith('exports'))
        self.window.handleEvent('actionFinished', 'exportMunicipality')
        self.assertTrue(self.window.tree.isEnabled())

    def testMunicipalityExportCompletionShowsFilenameCountAndLastExportTime(self):
        self.addColony()
        self.window.refreshJobs()
        result = {'municipalityId': '60', 'municipalityName': 'PANCHKULA', 'rows': 1234,
                  'filename': 'panchkula_timestamp.xlsx', 'path': str(self.home / 'Master' / 'panchkula_timestamp.xlsx'),
                  'exportedAt': '2026-11-23T15:30:00+05:30'}
        with patch.object(QMessageBox, 'exec', return_value=0) as show:
            self.window.handleEvent('municipalityExported', result)
        show.assert_called_once()
        self.assertIn('23 Nov 2026, 15:30 IST', self.window.groupExportLabels['60'].text())
        self.assertEqual(self.window.operationProgress.value(), 100)

    def testUpdateOptionsPassMissingMobileChoiceToValidation(self):
        def chooseMobileOnly(dialog):
            dialog.findChild(QCheckBox).setChecked(True)
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, 'exec', chooseMobileOnly), \
                patch('ulb_scraper.gui.QFileDialog.getExistingDirectory', return_value=str(self.home)), \
                patch.object(self.window, 'request') as request:
            self.window.chooseUpdateFolder()
        request.assert_called_once_with('inspectDataset', folder=str(self.home), mobileOnly=True)

    def testRemovalDoesNotPauseLargeQueueOnTheGuiThread(self):
        identity = self.addColony()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.store, 'control') as control, \
                patch.object(self.window, 'request') as request:
            self.window.deleteJobs([identity])
        control.assert_not_called()
        request.assert_called_once_with('delete', jobIds=[identity])

    def testUpdateConfirmationCancelResumeAndRestart(self):
        inventory = {'root': str(self.home), 'entries': [
            {'municipalityName': 'Town', 'colonyName': 'Colony', 'rowCount': 2,
             'portable': {'mode': 'update', 'status': 'paused'}}]}
        for choice, restart in [(QMessageBox.StandardButton.Cancel, None),
                                (QMessageBox.StandardButton.Yes, False),
                                (QMessageBox.StandardButton.No, True)]:
            with patch.object(QMessageBox, 'exec', return_value=choice), \
                    patch.object(self.window, 'request') as request:
                self.window.handleEvent('datasetInspected', inventory)
                if restart is None:
                    request.assert_not_called()
                else:
                    request.assert_called_once_with('importDataset', inventory=inventory, restart=restart)
        self.assertFalse(self.store.jobs())

    def testUpdateImportLocksQueueControlsAndRecoversOnFailure(self):
        identity = self.addColony()
        self.window.refreshJobs()
        self.window.handleEvent('session', 'expired')
        self.assertTrue(self.window.updateButton.isEnabled())
        self.window.request('importDataset', inventory={})
        self.assertFalse(self.window.tree.isEnabled())
        self.assertFalse(self.window.updateButton.isEnabled())
        self.window.dispatchAction(identity, False, 'pause')
        self.assertTrue(self.store.job(identity)['desired'])
        with patch.object(QMessageBox, 'warning') as warning:
            self.window.handleEvent('actionFailed', {'action': 'importDataset', 'message': 'Invalid fixture'})
            warning.assert_called_once()
        self.window.handleEvent('actionFinished', 'importDataset')
        self.assertTrue(self.window.tree.isEnabled())
        self.assertTrue(self.window.updateButton.isEnabled())
        self.assertTrue(self.window.municipality.isEnabled())

    def testUpdateAndMixedQueuesShowOperationAndPreservePause(self):
        identity = self.addColony()
        with self.store.connect() as connection:
            connection.execute("UPDATE jobs SET mode='update',status='scraping',uniqueCount=2 WHERE id=?", (identity,))
        self.window.refreshJobs()
        self.assertEqual(self.window.queueLabel.text(), 'Update queue')
        self.assertEqual(self.window.jobItems[identity].text(1), 'Updating: 0/2')
        self.addColony('2')
        self.window.refreshJobs()
        self.assertEqual(self.window.queueLabel.text(), 'Scraping / Update queue')
        self.window.dispatchAction(identity, False, 'pause')
        self.assertEqual(self.window.jobItems[identity].text(1), 'Paused')

    def testSuccessfulUpdateImportSetsFolderForNewColonyAdditions(self):
        root = str(self.home / 'imported-dataset')
        self.window.handleEvent('datasetImported', root)
        self.assertEqual(self.window.folder.text(), root)
        self.assertEqual(self.store.setting('outputFolder'), root)

    def testTrafficShowsEffectiveConcurrencyAndCooldownWithoutGreenFlapping(self):
        self.window.handleEvent('session', 'unavailable')
        self.window.handleEvent('traffic', {'active': 0, 'limit': 2, 'configured': 20,
                                          'intervalMs': 500, 'cooldown': 10, 'recovering': True, 'colonies': 0})
        self.assertFalse(self.window.validSession)
        self.assertEqual(self.window.sessionLabel.text(), 'Portal cooldown: 10s')
        self.assertEqual(self.window.metrics['active'].text(), 'Active: 0/2')
        self.assertIn('configured ceiling 20', self.window.metrics['active'].toolTip())

    def testSessionIndicatorIsRedUntilVerifiedAndRedAfterFailures(self):
        window = MainWindow(self.store, self.home, startWorker=False)
        try:
            self.assertFalse(window.validSession)
            self.assertIn('#c33e42', window.sessionLed.styleSheet())
            self.assertFalse(window.addButton.isEnabled())
            window.handleEvent('session', 'valid')
            self.assertIn('#168852', window.sessionLed.styleSheet())
            window.handleEvent('session', 'unavailable')
            self.assertFalse(window.validSession)
            self.assertIn('#c33e42', window.sessionLed.styleSheet())
        finally:
            window.close()
            window.deleteLater()

    def testAddButtonAndColonyThenMunicipalityDeleteKeepCorrectScope(self):
        self.window.engine.client = CatalogClient()
        self.window.folder.setText(str(self.home / 'exports'))
        self.window.show()
        for municipalityId, colonyId in [('60', '1'), ('60', '2'), ('61', '3')]:
            self.window.municipality.setCurrentIndex(self.window.municipality.findData(municipalityId))
            self.drainCommands()
            self.window.colony.setCurrentIndex(self.window.colony.findData(colonyId))
            self.application.processEvents()
            self.assertTrue(self.window.addButton.isEnabled())
            QTest.mouseClick(self.window.addButton, Qt.MouseButton.LeftButton)
            self.drainCommands()
        self.assertEqual(len(self.store.jobs()), 3)
        self.assertEqual(self.window.groupItems['60'].childCount(), 2)
        self.window.dispatchAction('60', True, 'pause')
        jobs = self.store.jobs()
        self.assertFalse(any(job['desired'] for job in jobs if job['municipalityId'] == '60'))
        self.assertTrue(all(job['desired'] for job in jobs if job['municipalityId'] == '61'))
        childId = next(job['id'] for job in jobs if job['colonyId'] == '1')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.dispatchAction(childId, False, 'delete')
            self.drainCommands()
            self.assertIsNone(self.store.job(childId))
            self.assertEqual(self.window.groupItems['60'].childCount(), 1)
            self.window.dispatchAction('60', True, 'delete')
            self.drainCommands()
        self.assertEqual([(job['municipalityId'], job['colonyId']) for job in self.store.jobs()], [('61', '3')])
        self.assertNotIn('60', self.window.groupItems)

    def testSettingsExposeOnlyPacingAndParallelRequests(self):
        def saveSettings():
            dialog = self.window.findChildren(QDialog)[0]
            spins = dialog.findChildren(QSpinBox)
            self.assertEqual(len(spins), 2)
            parallel = dialog.findChild(QSpinBox, 'parallelRequests')
            self.assertEqual(parallel.maximum(), 100)
            parallel.setValue(3)
            dialog.accept()

        QTimer.singleShot(0, saveSettings)
        self.window.showSettings()
        self.assertEqual(self.store.setting('parallelRequests'), 3)

    def testAllMunicipalitiesForceAllColonies(self):
        self.window.municipality.setCurrentIndex(self.window.municipality.findData('ALL'))
        self.assertEqual(self.window.colony.currentData(), 'ALL')
        self.assertFalse(self.window.colony.isEnabled())
        self.assertTrue(self.window.addButton.isEnabled())

    def testDependentColoniesDoNotLeakAcrossMunicipalities(self):
        self.window.municipality.setCurrentIndex(self.window.municipality.findData('60'))
        self.assertGreater(self.window.colony.findData('1'), 0)
        self.window.municipality.setCurrentIndex(self.window.municipality.findData('61'))
        self.assertEqual(self.window.colony.findData('1'), -1)
        self.assertGreater(self.window.colony.findData('3'), 0)

    def testRetiredOptionsAreDisabled(self):
        self.store.updateCatalog('municipality', '', [{'Value': '61', 'Text': 'OTHER'}])
        self.window.loadMunicipalities()
        index = self.window.municipality.findData('60')
        self.assertFalse(self.window.municipality.model().item(index).isEnabled())

    def testParentActionsIncludeNewlyAddedChildren(self):
        first = self.addColony('1')
        self.window.refreshJobs()
        parent = self.window.groupItems['60']
        second = self.addColony('2')
        self.window.refreshJobs()
        self.window.dispatchAction('60', True, 'pause')
        self.assertFalse(self.store.job(first)['desired'])
        self.assertFalse(self.store.job(second)['desired'])
        self.window.dispatchAction('60', True, 'start')
        self.assertTrue(self.store.job(first)['desired'])
        self.assertTrue(self.store.job(second)['desired'])

    def testPaintedPauseControlAcceptsAnActualMouseClick(self):
        identity = self.addColony()
        self.window.refreshJobs()
        self.window.show()
        self.application.processEvents()
        item = self.window.jobItems[identity]
        rect = self.window.tree.visualItemRect(item)
        position = QPoint(self.window.tree.columnViewportPosition(4) + 48, rect.center().y())
        QTest.mouseClick(self.window.tree.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.assertFalse(self.store.job(identity)['desired'])

    def testAllCancellationDoesNotResetJobs(self):
        identity = self.addColony()
        self.window.refreshJobs()
        self.window.municipality.setCurrentIndex(self.window.municipality.findData('ALL'))
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Cancel):
            self.window.addSelection()
        self.assertTrue(self.store.job(identity)['desired'])

    def testSessionExpiryKeepsResumeIntent(self):
        identity = self.addColony()
        self.window.refreshJobs()
        self.window.handleEvent('session', 'expired')
        self.assertFalse(self.window.validSession)
        self.assertTrue(self.store.job(identity)['desired'])
        self.assertEqual(self.window.jobItems[identity].text(1), 'Login required')

    def testCompactTableRetainsReadableNamesAndActions(self):
        self.addColony()
        self.window.refreshJobs()
        self.window.resize(980, 680)
        self.window.show()
        self.application.processEvents()
        self.window.fitColumns()
        self.assertGreaterEqual(self.window.tree.columnWidth(0), 225)
        self.assertEqual(self.window.tree.columnWidth(4), 138)
        self.assertEqual(self.window.tree.columnCount(), 5)


    def testSearchAndMouseChecksKeepSelectionAcrossFiltering(self):
        self.window.show()
        combo = self.window.municipality
        combo.showPopup()
        self.application.processEvents()
        combo.searchInput.setText('panch')
        otherIndex = combo.findData('61')
        selectedIndex = combo.findData('60')
        self.assertTrue(combo.options.isRowHidden(otherIndex))
        rect = combo.options.visualRect(combo.model().index(selectedIndex, 0))
        QTest.mouseClick(combo.options.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(combo.selectedValues(), ['60'])
        self.assertTrue(combo.popup.isVisible())
        combo.searchInput.setText('other')
        rect = combo.options.visualRect(combo.model().index(otherIndex, 0))
        QTest.mouseClick(combo.options.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(set(combo.selectedValues()), {'60', '61'})
        combo.hidePopup()
        self.window.loadMunicipalities()
        self.assertEqual(set(combo.selectedValues()), {'60', '61'})

    def testSelectionCountsExcludeHeadingsAndInactiveEntriesAndSurviveSearch(self):
        combo = self.window.municipality
        combo.addItem('Unavailable', 'inactive')
        combo.model().item(combo.findData('inactive')).setEnabled(False)
        combo.addItem('Group heading')
        combo.setSelectedValues(['60'])
        self.assertEqual(combo.selectionSummary(), '1 selected / 2')
        combo.searchInput.setText('panch')
        self.assertEqual(combo.selectionCount.text(), '1 selected / 2')
        combo.setSelectedValues(['ALL'])
        self.assertEqual(combo.selectionCount.text(), '2 selected / 2')
        combo.setSelectedValues([])
        self.assertEqual(combo.selectionCount.text(), '0 selected / 2')

    def testGroupedCheckedColoniesAddExactPairsAndStayIdempotent(self):
        self.window.engine.client = CatalogClient()
        self.window.folder.setText(str(self.home / 'exports'))
        self.window.municipality.toggleOption(self.window.municipality.findData('60'))
        self.window.municipality.toggleOption(self.window.municipality.findData('61'))
        self.assertGreater(self.window.colony.findData('60:1'), 0)
        self.assertGreater(self.window.colony.findData('61:3'), 0)
        self.window.colony.toggleOption(self.window.colony.findData('60:2'))
        self.window.colony.toggleOption(self.window.colony.findData('61:3'))
        self.drainCommands()
        self.assertEqual(self.window.selectedColonies(), {'60': ['2'], '61': ['3']})
        self.window.addSelection()
        self.drainCommands()
        self.assertEqual({(job['municipalityId'], job['colonyId']) for job in self.store.jobs()}, {('60', '2'), ('61', '3')})
        self.assertEqual(self.window.municipality.selectedValues(), [])
        self.assertEqual(self.window.colony.selectedValues(), [])
        self.assertEqual(self.store.setting('selectedMunicipalities'), [])
        self.assertTrue(self.window.municipality.isEnabled())
        self.assertFalse(self.window.colony.isEnabled())
        self.window.municipality.toggleOption(self.window.municipality.findData('60'))
        self.window.municipality.toggleOption(self.window.municipality.findData('61'))
        self.window.colony.toggleOption(self.window.colony.findData('60:2'))
        self.window.colony.toggleOption(self.window.colony.findData('61:3'))
        for value in ('60:2', '61:3'):
            item = self.window.colony.model().item(self.window.colony.findData(value))
            self.assertEqual(item.background().color().name(), '#e1f0e6')
        self.window.municipality.toggleOption(self.window.municipality.findData('61'))
        self.assertEqual(self.window.colony.selectedValues(), ['2'])
        self.window.addSelection()
        self.drainCommands()
        self.assertEqual(len(self.store.jobs()), 2)
        self.assertEqual(self.window.municipality.selectedValues(), [])

    def testAddFailureRetainsChecksForRetry(self):
        self.window.engine.client = CatalogClient()
        self.window.municipality.toggleOption(self.window.municipality.findData('60'))
        self.window.colony.toggleOption(self.window.colony.findData('1'))
        self.drainCommands()
        self.window.addSelection()
        self.assertFalse(self.window.municipality.isEnabled())
        self.assertFalse(self.window.colony.isEnabled())
        with patch.object(self.window.engine, 'addSelections', side_effect=ValueError('Fixture failure')):
            with self.assertLogs(level='ERROR'):
                self.drainCommands()
        self.assertEqual(self.window.municipality.selectedValues(), ['60'])
        self.assertEqual(self.window.colony.selectedValues(), ['1'])
        self.assertTrue(self.window.municipality.isEnabled())
        self.assertTrue(self.window.colony.isEnabled())

    def testProcessingStatusMovesWhileMapCoverageWaits(self):
        jobId = self.addColony()
        with self.store.connect() as connection:
            connection.execute("""UPDATE jobs SET status='scraping', uniqueCount=36,
                completedCount=10, coverage=0.5625, tileDone=9, tileTotal=16 WHERE id=?""", (jobId,))
        self.window.refreshJobs()
        item = self.window.jobItems[jobId]
        self.assertEqual(item.text(1), 'Processing: 10/36')
        coverage = item.text(2)
        with self.store.connect() as connection:
            connection.execute('UPDATE jobs SET completedCount=11 WHERE id=?', (jobId,))
        self.window.refreshJobs()
        self.assertEqual(item.text(1), 'Processing: 11/36')
        self.assertEqual(item.text(2), coverage)
        self.assertIn('details and photos', item.toolTip(1))

    def testAllChecksActiveOptionsAndUncheckingOneClearsAllFlag(self):
        combo = self.window.municipality
        combo.toggleOption(combo.findData('ALL'))
        self.assertEqual(combo.selectedValues(), ['ALL'])
        for value in ('60', '61'):
            self.assertEqual(combo.model().item(combo.findData(value)).checkState(), Qt.CheckState.Checked)
        combo.toggleOption(combo.findData('61'))
        self.assertEqual(combo.selectedValues(), ['60'])
        self.window.colony.toggleOption(self.window.colony.findData('ALL'))
        self.assertEqual(self.window.colony.selectedValues(), ['ALL'])
        self.window.colony.toggleOption(self.window.colony.findData('2'))
        self.assertEqual(self.window.colony.selectedValues(), ['1'])

    def testAddingMunicipalityPreservesPreviouslyCheckedColonyChildren(self):
        self.window.municipality.toggleOption(self.window.municipality.findData('60'))
        self.window.colony.toggleOption(self.window.colony.findData('ALL'))
        self.window.municipality.toggleOption(self.window.municipality.findData('61'))
        self.assertEqual(self.window.selectedColonies(), {'60': ['1', '2']})
        self.assertNotIn('ALL', self.window.colony.selectedValues())

    def testKeyboardSearchSelectsWithoutClearingExistingChecks(self):
        self.window.show()
        combo = self.window.municipality
        combo.toggleOption(combo.findData('60'))
        combo.showPopup()
        combo.searchInput.setText('other')
        QTest.keyClick(combo.searchInput, Qt.Key.Key_Return)
        self.assertEqual(set(combo.selectedValues()), {'60', '61'})
        combo.hidePopup()

    def testSpeedUsesNewProgressAndIgnoresHistoricalCountsAndDeletion(self):
        self.addColony()
        self.window.refreshJobs()
        jobs = self.store.jobs()
        with patch('ulb_scraper.gui.time.monotonic', return_value=100):
            self.window.speedSamples.clear()
            self.window.updateSpeed(jobs)
        jobs[0]['completedCount'] += 5
        with patch('ulb_scraper.gui.time.monotonic', return_value=110):
            self.window.updateSpeed(jobs)
        self.assertEqual(self.window.metrics['speed'].text(), 'Scraping speed: 30.0/min')
        self.window.updateSpeed([])
        self.assertEqual(self.window.metrics['speed'].text(), 'Scraping speed: 0.0/min')
        self.assertIn('Total Jobs', self.window.metrics['jobs'].text())
        self.assertIn('Total properties', self.window.metrics['properties'].text())


if __name__ == '__main__':
    unittest.main()
