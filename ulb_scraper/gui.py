'''Native desktop controls for authenticated scraping and persistent job groups.'''

# Standard Library Imports
from collections import deque
from datetime import datetime
import logging
from pathlib import Path
import sys
import time

# Third Party Imports
from PySide6.QtCore import QEvent, QObject, QRect, QRegularExpression, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                              QDialog, QDialogButtonBox, QFileDialog,
                              QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
                              QLineEdit, QListView, QMainWindow, QMenu, QMessageBox, QPushButton,
                              QProgressBar, QSizePolicy, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox, QStylePainter,
                              QSystemTrayIcon, QToolButton, QToolTip,
                              QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QWidgetAction)

from ulb_scraper.elapsed import formatElapsed
from ulb_scraper.engine import Engine
from ulb_scraper.pid_dialog import PidResultsDialog


STATUSLABELS = {
    'queued':        'Queued',
    'preparing':     'Preparing',
    'scanning':      'Scanning map',
    'scraping':      'Scraping',
    'details':       'Fetching details',
    'photos':        'Downloading photos',
    'paused':        'Paused',
    'waiting_login': 'Login required',
    'waiting_service': 'Waiting for portal',
    'retrying':      'Retrying',
    'error':         'Needs attention',
    'complete':      'Complete',
    'complete_warnings': 'Complete with warnings',
}
STYLE = '''
QWidget { font-family: 'Segoe UI', 'Arial'; font-size: 13px; color: #253137; }
QMainWindow, QDialog { background: #f5f7f8; }
QLabel#title { font-size: 24px; font-weight: 700; color: #172c28; }
QLabel#subtitle { color: #647176; }
QLabel#metricValue { font-size: 13px; font-weight: 600; color: #087962; }
QLabel#session { font-weight: 600; }
QWidget#selection { background: #ffffff; border-top: 1px solid #dae1e3; border-bottom: 1px solid #dae1e3; }
QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #c9d3d6; border-radius: 4px; padding: 8px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus { border-color: #148570; }
QLineEdit:disabled, QComboBox:disabled { color: #8b9599; background: #f0f3f4; }
QPushButton { border: 1px solid #c9d3d6; border-radius: 4px; padding: 9px 14px; background: #ffffff; }
QPushButton:hover { background: #edf4f2; border-color: #5f978a; }
QPushButton:disabled { color: #9ca6a9; border-color: #dce2e4; background: #f1f4f5; }
QPushButton#primary { background: #087962; border-color: #087962; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #05654f; }
QPushButton#primary:disabled { background: #96b7ae; border-color: #96b7ae; }
QToolButton { border: none; border-radius: 3px; padding: 5px; }
QToolButton:hover { background: #dfece7; }
QToolButton#pidList { border: 1px solid #c9d3d6; border-radius: 4px; padding: 9px 30px 9px 14px; background: #ffffff; }
QToolButton#pidList:hover { background: #edf4f2; border-color: #5f978a; }
QToolButton#pidList:disabled { color: #9ca6a9; border-color: #dce2e4; background: #f1f4f5; }
QToolButton#pidList::menu-button { subcontrol-origin: border; subcontrol-position: top right; width: 22px; border: none; border-left: 1px solid #c9d3d6; background: transparent; }
QToolButton#pidList::menu-button:disabled { border-left-color: #dce2e4; }
QTreeWidget { background: white; border: 1px solid #dae1e3; outline: 0; alternate-background-color: #fafcfc; }
QTreeWidget::item { height: 46px; border-bottom: 1px solid #eef1f2; }
QTreeWidget::item:selected { background: #e1f0eb; color: #172c28; }
QHeaderView::section { background: #edf2f3; color: #506166; font-weight: 600; border: none; border-bottom: 1px solid #d6dfe1; padding: 12px 8px; }
QLabel#notice { color: #53666c; }
'''


class Bridge(QObject):
    event = Signal(str, object)


class SearchCheckCombo(QComboBox):
    '''Searchable catalog with persistent checks and an explicit ALL selection.'''

    selectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.updating = False
        self.popup = QFrame(self, Qt.WindowType.Popup)
        layout = QVBoxLayout(self.popup)
        self.searchInput = QLineEdit()
        self.searchInput.setPlaceholderText('Type to search...')
        layout.addWidget(self.searchInput)
        self.options = QListView()
        self.options.setModel(self.model())
        self.options.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.options.viewport().installEventFilter(self)
        self.options.installEventFilter(self)
        layout.addWidget(self.options)
        self.selectionCount = QLabel()
        layout.addWidget(self.selectionCount)
        self.searchInput.installEventFilter(self)
        self.searchInput.textChanged.connect(self.filterOptions)
        self.currentIndexChanged.connect(self.selectSingle)

    def addItem(self, text, userData=None):
        '''Append a checkable option. Inputs: <text>, [userData]. Output: None.'''
        logging.debug('Adding checkable catalog option')
        super().addItem(text, userData)
        item = self.model().item(self.count() - 1)
        if userData:
            item.setCheckable(True)
            item.setCheckState(Qt.CheckState.Unchecked)

    def selectedValues(self):
        '''Read selected active identities. Inputs: None. Output: list.'''
        logging.debug('Reading checked catalog options')
        selected = [self.itemData(index) for index in range(self.count())
                    if self.model().item(index).isEnabled() and
                    self.model().item(index).checkState() == Qt.CheckState.Checked]
        return ['ALL'] if 'ALL' in selected else selected

    def setSelectedValues(self, values):
        '''Restore checks without treating typing as selection. Inputs: <values>. Output: None.'''
        logging.debug('Restoring catalog checks')
        self.updating = True
        try:
            for index in range(self.count()):
                item = self.model().item(index)
                if not self.itemData(index):
                    continue
                checked = bool(self.itemData(index)) and item.isEnabled() and (
                    'ALL' in values or self.itemData(index) in values)
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            first = next((index for index in range(self.count()) if self.itemData(index) in values), 0)
            self.setCurrentIndex(first)
        finally:
            self.updating = False
        self.selectionCount.setText(self.selectionSummary())
        self.update()

    def selectionSummary(self):
        '''Count active catalog entries, excluding ALL and group headings.'''
        logging.debug('Counting catalog selections')
        total = 0
        selected = 0
        for index in range(self.count()):
            item = self.model().item(index)
            if not self.itemData(index) or self.itemData(index) == 'ALL' or not item.isEnabled():
                continue
            total += 1
            selected += item.checkState() == Qt.CheckState.Checked
        return f'{selected} selected / {total}'

    def selectSingle(self, index):
        '''Support a direct single selection. Inputs: <index>. Output: None.'''
        logging.debug('Selecting a single catalog entry')
        if self.updating:
            return
        self.setSelectedValues([self.itemData(index)] if index >= 0 else [])
        self.selectionChanged.emit()

    def toggleOption(self, index):
        '''Toggle one checkbox, keeping ALL and children consistent. Inputs: <index>. Output: None.'''
        logging.debug('Toggling catalog checkbox')
        item = self.model().item(index)
        if not item or not item.isEnabled() or not self.itemData(index):
            return
        checked = item.checkState() != Qt.CheckState.Checked
        if self.itemData(index) == 'ALL':
            self.setSelectedValues(['ALL'] if checked else [])
        else:
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            allIndex = self.findData('ALL')
            if allIndex >= 0:
                self.model().item(allIndex).setCheckState(Qt.CheckState.Unchecked)
            self.setSelectedValues(self.selectedValues())
        self.selectionChanged.emit()

    def filterOptions(self, query):
        '''Filter labels without clearing checks. Inputs: <query>. Output: None.'''
        logging.debug('Filtering catalog options')
        for index in range(self.count()):
            self.options.setRowHidden(index, query.casefold() not in self.itemText(index).casefold())
        self.selectionCount.setText(self.selectionSummary())

    def showPopup(self):
        '''Open a searchable checklist. Inputs: None. Output: None.'''
        logging.debug('Opening catalog checklist')
        self.searchInput.clear()
        self.selectionCount.setText(self.selectionSummary())
        self.popup.resize(max(380, self.width()), 360)
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        screen = self.screen().availableGeometry()
        self.popup.move(min(self.popup.x(), screen.right() - self.popup.width()),
                        min(self.popup.y(), screen.bottom() - self.popup.height()))
        self.popup.show()
        self.searchInput.setFocus()

    def hidePopup(self):
        '''Close the checklist. Inputs: None. Output: None.'''
        self.popup.hide()

    def eventFilter(self, watched, event):
        '''Keep the checklist open during mouse and keyboard selection.'''
        if watched == self.searchInput and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return):
            index = next((index for index in range(self.count()) if not self.options.isRowHidden(index)
                          and self.itemData(index) and self.model().item(index).isEnabled()), None)
            if index is not None:
                self.options.setCurrentIndex(self.model().index(index, 0))
                self.options.setFocus()
                if event.key() == Qt.Key.Key_Return:
                    self.toggleOption(index)
            return True
        if watched == self.options.viewport() and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                index = self.options.indexAt(event.position().toPoint())
                if index.isValid():
                    self.toggleOption(index.row())
            return True
        if watched == self.options and event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            self.toggleOption(self.options.currentIndex().row())
            return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        '''Preserve checks when scrolling past a closed selector.'''
        event.ignore()

    def keyPressEvent(self, event):
        '''Open search from the keyboard without replacing checked entries.'''
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Space, Qt.Key.Key_Return) or event.text().isprintable() and event.text():
            self.showPopup()
            if event.text().strip() and event.key() != Qt.Key.Key_Return:
                self.searchInput.setText(event.text())
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        '''Display a compact selection summary in the closed control.'''
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        if self.selectedValues():
            option.currentText = self.selectionSummary()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class OtpDialog(QDialog):
    '''Small OTP verification dialog; request work stays on the engine thread.'''

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('Verify mobile number')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(370)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(QLabel('OTP verification'))
        self.mobileLabel = QLabel('Sent to ' + window.phone.text())
        layout.addWidget(self.mobileLabel)
        self.otp = QLineEdit()
        self.otp.setPlaceholderText('Enter OTP')
        self.otp.setMaxLength(8)
        self.otp.setValidator(QRegularExpressionValidator(QRegularExpression('[0-9]{0,8}'), self))
        layout.addWidget(self.otp)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        self.message.setStyleSheet('color: #a43c3c')
        layout.addWidget(self.message)
        row = QHBoxLayout()
        self.resend = QPushButton('Resend OTP')
        self.resend.clicked.connect(window.generateOtp)
        self.verify = QPushButton('Verify')
        self.verify.setObjectName('primary')
        self.verify.clicked.connect(self.verifyOtp)
        self.otp.returnPressed.connect(self.verifyOtp)
        row.addWidget(self.resend)
        row.addStretch()
        row.addWidget(self.verify)
        layout.addLayout(row)
        self.otp.setFocus()

    def verifyOtp(self):
        '''Submit OTP for background verification. Inputs: None. Output: None.'''
        logging.info('Submitting OTP verification')
        if not self.verify.isEnabled():
            return
        if len(self.otp.text()) < 4:
            self.message.setText('Enter the OTP received on your mobile.')
            return
        self.verify.setEnabled(False)
        self.resend.setEnabled(False)
        self.message.setText('Verifying...')
        self.window.engine.submit('verifyOtp', otp=self.otp.text())


class MainWindow(QMainWindow):
    '''Persistent municipality tree with live counts and scoped controls.'''

    def __init__(self, store, home, startWorker=True):
        super().__init__()
        self.store = store
        self.home = Path(home)
        self.bridge = Bridge()
        self.bridge.event.connect(self.handleEvent)
        self.engine = Engine(store, home, self.bridge.event.emit)
        self.otpDialog = None
        self.validSession = False
        self.sessionState = 'checking'
        self.trafficState = {}
        self.busyActions = set()
        self.closing = False
        self.jobItems = {}
        self.groupItems = {}
        self.groupExportLabels = {}
        self.groupVerifyButtons = {}
        self.elapsedValues = {'colonies': {}, 'municipalities': {}}
        self.pidSummaries = []
        self.municipalityExports = self.store.setting('municipalityExports', {})
        self.jobsById = {}
        self.speedSamples = deque()
        self.previousCompleted = {}
        self.sessionCompleted = 0
        self.setWindowTitle('ULB Property Scraper')
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.resize(1280, 820)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(STYLE)
        self.buildWindow()
        self.loadMunicipalities()
        self.refreshPidList()
        self.elapsedValues = {'colonies': dict(self.engine.elapsed.colonies), 'municipalities': dict(self.engine.elapsed.municipalities)}
        self.refreshJobs()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refreshJobs)
        self.timer.start(700)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        if startWorker:
            self.engine.start()

    def buildWindow(self):
        '''Build the primary desktop workspace. Inputs: None. Output: None.'''
        logging.debug('Building desktop workspace')
        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(26, 22, 26, 16)
        layout.setSpacing(18)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel('ULB Property Scraper')
        title.setObjectName('title')
        titles.addWidget(title)
        subtitle = QLabel('Haryana / Property records')
        subtitle.setObjectName('subtitle')
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.sessionLabel = QLabel('Session: checking')
        self.sessionLabel.setObjectName('session')
        self.sessionLed = QLabel()
        self.sessionLed.setFixedSize(12, 12)
        self.sessionLed.setStyleSheet('background: #c33e42; border-radius: 6px;')
        header.addWidget(self.sessionLed)
        header.addWidget(self.sessionLabel)
        settings = self.iconButton(QStyle.StandardPixmap.SP_FileDialogDetailedView, 'Settings', self.showSettings)
        header.addWidget(settings)
        layout.addLayout(header)

        login = QHBoxLayout()
        login.addWidget(QLabel('Mobile number'))
        self.phone = QLineEdit(self.store.setting('phone', ''))
        self.phone.setPlaceholderText('10-digit mobile number')
        self.phone.setMaxLength(10)
        self.phone.setValidator(QRegularExpressionValidator(QRegularExpression('[0-9]{0,10}'), self))
        self.phone.setFixedWidth(220)
        login.addWidget(self.phone)
        self.otpButton = QPushButton('Generate OTP')
        self.otpButton.clicked.connect(self.generateOtp)
        login.addWidget(self.otpButton)
        check = self.iconButton(QStyle.StandardPixmap.SP_BrowserReload, 'Check saved session',
                                lambda: self.request('checkSession'))
        login.addWidget(check)
        login.addStretch()
        self.updateButton = QPushButton('Update data')
        self.updateButton.setToolTip('Validate an existing output folder and update its municipality/colony datasets')
        self.updateButton.clicked.connect(self.chooseUpdateFolder)
        login.addWidget(self.updateButton)
        self.pidButton = QToolButton()
        self.pidButton.setObjectName('pidList')
        self.pidButton.setText('Upload PID list')
        self.pidButton.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.pidButton.clicked.connect(self.choosePidList)
        self.pidMenu = QMenu(self.pidButton)
        self.pidButton.setMenu(self.pidMenu)
        login.addWidget(self.pidButton)
        logs = QPushButton('Logs')
        logs.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        logs.clicked.connect(lambda: self.openFolder(self.home / 'logs'))
        login.addWidget(logs)
        layout.addLayout(login)
        self.updateButton.ensurePolished()
        self.pidButton.setFixedHeight(self.updateButton.sizeHint().height())

        selection = QWidget()
        selection.setObjectName('selection')
        selectionLayout = QHBoxLayout(selection)
        selectionLayout.setContentsMargins(16, 16, 16, 16)
        selectionLayout.setSpacing(14)
        self.municipality = SearchCheckCombo()
        self.municipality.setMinimumWidth(180)
        self.municipality.setMaxVisibleItems(20)
        self.municipality.selectionChanged.connect(self.municipalityChanged)
        self.colony = SearchCheckCombo()
        self.colony.setMinimumWidth(180)
        self.colony.setMaxVisibleItems(20)
        self.colony.selectionChanged.connect(self.updateAddButton)
        selectionLayout.addLayout(self.labeled('Municipality', self.municipality), 2)
        selectionLayout.addLayout(self.labeled('Colony', self.colony), 2)
        defaultFolder = (Path.home() / 'Documents' / 'ULB Property Records' if getattr(sys, 'frozen', False)
                         else Path(__file__).resolve().parents[1] / 'output')
        self.folder = QLineEdit(self.store.setting('outputFolder', str(defaultFolder)))
        self.folder.setReadOnly(True)
        self.folder.setMinimumWidth(190)
        folderRow = QHBoxLayout()
        folderRow.setSpacing(4)
        folderRow.addWidget(self.folder)
        folderRow.addWidget(self.iconButton(QStyle.StandardPixmap.SP_DirOpenIcon, 'Choose output folder', self.chooseFolder))
        folderColumn = QVBoxLayout()
        folderColumn.addWidget(QLabel('Save to folder'))
        folderColumn.addLayout(folderRow)
        selectionLayout.addLayout(folderColumn, 3)
        self.addButton = QPushButton('Add')
        self.addButton.setObjectName('primary')
        self.addButton.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        self.addButton.clicked.connect(self.addSelection)
        selectionLayout.addWidget(self.addButton, 0, Qt.AlignmentFlag.AlignBottom)
        refresh = self.iconButton(QStyle.StandardPixmap.SP_BrowserReload, 'Refresh municipality and colony options', self.refreshCatalog)
        selectionLayout.addWidget(refresh, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(selection)

        toolbar = QHBoxLayout()
        self.queueLabel = QLabel('Scraping queue')
        self.queueLabel.setFont(QFont('Arial', 14, QFont.Weight.DemiBold))
        toolbar.addWidget(self.queueLabel)
        toolbar.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Filter municipality or colony')
        self.search.setMaximumWidth(280)
        self.search.textChanged.connect(self.filterJobs)
        toolbar.addWidget(self.search)
        self.statusFilter = QComboBox()
        for label, value in [('All statuses', 'all'), ('Active', 'active'), ('Paused', 'paused'),
                             ('Login required', 'waiting_login'), ('Needs attention', 'error'), ('Complete', 'complete')]:
            self.statusFilter.addItem(label, value)
        self.statusFilter.currentIndexChanged.connect(self.filterJobs)
        toolbar.addWidget(self.statusFilter)
        toolbar.addWidget(self.iconButton(QStyle.StandardPixmap.SP_MediaPlay, 'Resume all jobs',
                                          lambda: self.controlJobs(list(self.jobsById), 'start')))
        toolbar.addWidget(self.iconButton(QStyle.StandardPixmap.SP_MediaPause, 'Pause all jobs',
                                          lambda: self.controlJobs(list(self.jobsById), 'pause')))
        layout.addLayout(toolbar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(['Municipality / Colony', 'Time elapsed', 'Status', 'Map coverage', 'Properties', 'Actions'])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(False)
        self.tree.setIndentation(22)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.showJobMenu)
        widths = [510, 105, 210, 135, 105, 138]
        for column, width in enumerate(widths):
            self.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.tree.setColumnWidth(column, width)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.tree.setItemDelegateForColumn(5, ActionDelegate(self))
        self.tree.header().setSectionsClickable(True)
        self.tree.header().sectionClicked.connect(self.sortJobs)
        self.sortColumn = 0
        self.sortOrder = Qt.SortOrder.AscendingOrder
        layout.addWidget(self.tree, 1)
        self.emptyLabel = QLabel('No colony jobs')
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setStyleSheet('color: #77868c; padding: 24px')
        layout.addWidget(self.emptyLabel)
        metrics = QHBoxLayout()
        metrics.setSpacing(20)
        self.metrics = {}
        for name, label in [('jobs', 'Total Jobs'), ('active', 'Active'), ('properties', 'Total properties'), ('speed', 'Scraping speed')]:
            value = QLabel(f'{label}: 0')
            value.setObjectName('metricValue')
            metrics.addWidget(value)
            self.metrics[name] = value
        metrics.addStretch()
        layout.addLayout(metrics)
        self.operationPanel = QWidget()
        operationLayout = QHBoxLayout(self.operationPanel)
        operationLayout.setContentsMargins(0, 0, 0, 0)
        self.operationLabel = QLabel('Working...')
        self.operationLabel.setWordWrap(True)
        self.operationProgress = QProgressBar()
        self.operationProgress.setRange(0, 0)
        self.operationProgress.setMaximumWidth(220)
        operationLayout.addWidget(self.operationLabel, 1)
        operationLayout.addWidget(self.operationProgress)
        layout.addWidget(self.operationPanel)
        self.cancelPidButton = QPushButton('Stop PID operation')
        self.cancelPidButton.clicked.connect(self.engine.pidCancel.set)
        operationLayout.addWidget(self.cancelPidButton)
        self.cancelPidButton.hide()
        self.operationPanel.hide()
        self.dataFolderNote = QLabel('Keep this output folder unchanged. To work with the data, copy the full folder and use the copy.')
        self.dataFolderNote.setWordWrap(True)
        self.dataFolderNote.setStyleSheet('color: #b42318; font-weight: bold;')
        layout.addWidget(self.dataFolderNote)
        self.notice = QLabel('Checkpoints retained automatically')
        self.notice.setObjectName('notice')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.updateAddButton()

    def labeled(self, label, widget):
        '''Build a labeled input. Inputs: <label>, <widget>. Output: layout.'''
        logging.debug('Creating input label')
        column = QVBoxLayout()
        column.addWidget(QLabel(label))
        column.addWidget(widget)
        return column

    def iconButton(self, icon, tooltip, action):
        '''Build a fixed-size icon control. Inputs: <icon>, <tooltip>, <action>. Output: button.'''
        logging.debug('Creating icon action')
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(30, 30)
        button.clicked.connect(action)
        return button

    def request(self, action, **payload):
        '''Send a UI request once while it is pending. Inputs: <action>, [payload]. Output: None.'''
        logging.info(f'UI requested {action}')
        if self.closing or action in self.busyActions:
            return
        self.busyActions.add(action)
        self.engine.submit(action, **payload)
        if action in ('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids'):
            self.tree.setEnabled(False)
            self.municipality.setEnabled(False)
            self.colony.setEnabled(False)
        if action in ('inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids'):
            self.operationLabel.setText({'inspectDataset': 'Validating dataset folders...',
                'importDataset': 'Preparing update jobs...', 'delete': 'Removing jobs safely...',
                'exportMunicipality': 'Preparing municipality export...',
                'importPidList': 'Reading reference PID workbook...',
                'verifyPids': 'Pausing all jobs before verification...',
                'fetchMissingPids': 'Preparing missing PID recovery...'}[action])
            self.operationProgress.setRange(0, 0)
            self.operationPanel.show()
            self.cancelPidButton.setVisible(action in ('verifyPids', 'fetchMissingPids'))
        self.updateAddButton()

    def exportMunicipality(self, municipalityId):
        '''Confirm a paused municipality snapshot and queue its master export.'''
        logging.info('Requesting municipality master export')
        if self.closing or self.busyActions:
            return
        jobs = [job for job in self.jobsById.values() if job['municipalityId'] == municipalityId]
        if not jobs:
            return
        name = jobs[0]['municipalityName']
        if QMessageBox.question(self, 'Export municipality',
                f'Pause {name} colonies and export all saved properties collected so far? '
                'Saved colonies removed from the queue will also be included. '
                'Export again to include future updates. You can resume scraping after the export.',
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok) != QMessageBox.StandardButton.Ok:
            return
        roots = {str(Path(job['folder']).resolve().parent.parent) for job in jobs}
        root = next(iter(roots))
        if len(roots) > 1:
            root = QFileDialog.getExistingDirectory(self, 'Choose output root for the Master folder', root)
            if not root:
                return
        self.request('exportMunicipality', municipalityId=municipalityId, destinationRoot=root)

    def choosePidList(self):
        '''Import municipality columns from a reference workbook without starting work.'''
        logging.info('Choosing a reference PID workbook')
        if self.busyActions or self.closing:
            return
        path, unused = QFileDialog.getOpenFileName(self, 'Upload municipality PID list', '', 'Excel workbook (*.xlsx)')
        if path:
            self.request('importPidList', path=path)

    def refreshPidList(self):
        '''Show municipality counts and mark unknown names in red.'''
        logging.debug('Refreshing imported PID municipality menu')
        self.pidSummaries = self.engine.pidReference.summary(self.store.catalog('municipality'))
        self.pidMenu.clear()
        upload = self.pidMenu.addAction('Upload / replace PID list...')
        upload.triggered.connect(self.choosePidList)
        self.pidMenu.addSeparator()
        for entry in self.pidSummaries:
            label = f'{entry["name"].upper()} ({entry["count"]:,} PIDs)'
            if entry['municipalityId'] is None:
                unknown = QWidgetAction(self.pidMenu)
                text = QLabel(label + ' — unknown or ambiguous municipality')
                text.setStyleSheet('color: #b42318; padding: 8px;')
                unknown.setDefaultWidget(text)
                self.pidMenu.addAction(unknown)
            else:
                action = self.pidMenu.addAction(label)
                action.triggered.connect(lambda checked=False, identity=entry['municipalityId']: self.verifyPids(identity))
        if not self.pidSummaries:
            empty = self.pidMenu.addAction('No PID list uploaded')
            empty.setEnabled(False)
        for identity, button in self.groupVerifyButtons.items():
            button.setEnabled(any(entry['municipalityId'] == identity for entry in self.pidSummaries))

    def verifyPids(self, municipalityId):
        '''Confirm an on-demand comparison, including saved colonies absent from the queue.'''
        logging.info('Requesting PID verification')
        if self.busyActions or self.closing:
            return
        if QMessageBox.question(self, 'Verify saved data',
                'Pause all jobs and verify this municipality against all saved colony folders? '
                'No properties will be fetched during verification. Jobs will stay paused until you resume them.',
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
            return
        self.request('verifyPids', municipalityId=municipalityId, root=self.folder.text())

    def fetchMissingPids(self, result):
        '''Confirm targeted recovery and refresh existing municipality Master exports.'''
        logging.info('Confirming missing PID recovery')
        if not self.validSession or self.closing:
            QMessageBox.information(self, 'Login required', 'Verify your login before fetching missing PIDs.')
            return False
        if QMessageBox.question(self, 'Fetch missing PIDs',
                'Fetch missing and incomplete properties, including their details and photos? '
                'Their colony datasets will be updated, and an existing Master workbook will be rebuilt. '
                'This may include colonies that were not added to the queue. All jobs will remain paused.',
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
            return False
        self.request('fetchMissingPids', municipalityId=result['municipalityId'], root=result['roots'][0])
        return True

    def municipalityExportComplete(self, result):
        '''Show the saved workbook, row count and an Open folder shortcut.'''
        logging.info('Municipality workbook saved')
        self.municipalityExports[result['municipalityId']] = result
        self.refreshJobs()
        self.operationProgress.setRange(0, 100)
        self.operationProgress.setValue(100)
        box = QMessageBox(self)
        box.setWindowTitle('Municipality export complete')
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f'File saved under Master:\n{result["filename"]}\n\n'
                    f'Total property rows: {result["rows"]:,}')
        box.setInformativeText('All saved properties collected so far are included. '
            'Export again to include future updates. You can now resume scraping for this municipality.')
        box.setDetailedText(result['path'])
        box.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
        openButton = box.addButton('Open folder', QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if box.clickedButton() == openButton:
            self.openFolder(Path(result['path']).parent)

    def addMunicipalityExportControl(self, parent, municipalityId, name):
        '''Place an export button and last-export time beside the municipality.'''
        logging.debug('Adding municipality export control')
        widget = QWidget()
        widget.setObjectName('municipalityExport')
        widget.setStyleSheet('QWidget#municipalityExport { background: #edf5f1; }')
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(10)
        label = QLabel(name)
        label.setStyleSheet('font-weight: bold;')
        label.setMaximumWidth(155)
        label.setToolTip(name)
        layout.addWidget(label)
        button = QPushButton('Export Excel')
        button.setStyleSheet('padding: 3px 6px; font-size: 11px;')
        button.setToolTip('Pause this municipality and export all saved colony data to Master')
        button.clicked.connect(lambda checked=False: self.exportMunicipality(municipalityId))
        layout.addWidget(button)
        verify = QPushButton('Verify data')
        verify.setStyleSheet('padding: 3px 6px; font-size: 11px;')
        verify.clicked.connect(lambda checked=False: self.verifyPids(municipalityId))
        verify.setEnabled(any(entry['municipalityId'] == municipalityId for entry in self.pidSummaries))
        verify.setToolTip('Pause all jobs and compare the uploaded PID list with all saved colonies')
        self.groupVerifyButtons[municipalityId] = verify
        layout.addWidget(verify)
        lastExport = QLabel()
        lastExport.setMinimumWidth(100)
        lastExport.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lastExport.setStyleSheet('font-size: 10px; color: #53666c;')
        layout.addWidget(lastExport, 1)
        parent.setSizeHint(0, QSize(0, 46))
        self.tree.setItemWidget(parent, 0, widget)
        self.groupExportLabels[municipalityId] = lastExport

    def generateOtp(self):
        '''Validate phone and generate an OTP. Inputs: None. Output: None.'''
        logging.info('Requesting mobile login')
        if len(self.phone.text()) != 10 or self.phone.text()[0] not in '6789':
            QMessageBox.warning(self, 'Mobile number', 'Enter a valid ten-digit mobile number.')
            return
        self.store.setSetting('phone', self.phone.text())
        self.otpButton.setEnabled(False)
        self.phone.setEnabled(False)
        self.request('generateOtp', phone=self.phone.text())

    def loadMunicipalities(self):
        '''Populate cached municipality options and disabled retired entries. Inputs: None. Output: None.'''
        logging.debug('Loading municipality options')
        selected = self.municipality.selectedValues() or self.store.setting('selectedMunicipalities', [])
        if not selected:
            selected = [self.store.setting('selectedMunicipality', '')]
        self.municipality.blockSignals(True)
        self.municipality.clear()
        self.municipality.addItem('Select municipality', '')
        self.municipality.addItem('ALL municipalities', 'ALL')
        for entry in self.store.catalog('municipality'):
            self.appendOption(self.municipality, entry)
        self.municipality.setSelectedValues(selected)
        self.municipality.blockSignals(False)
        self.loadColonies()
        self.tintCatalog()

    def appendOption(self, combo, entry):
        '''Append a catalog option with retirement status. Inputs: <combo>, <entry>. Output: None.'''
        logging.debug('Appending catalog option')
        label = entry['name'] if entry['active'] else entry['name'] + ' (unavailable)'
        combo.addItem(label, entry['id'])
        if not entry['active']:
            item = combo.model().item(combo.count() - 1)
            item.setEnabled(False)
            item.setForeground(QColor('#929b9f'))

    def municipalityChanged(self):
        '''Change colony options with the parent municipality. Inputs: None. Output: None.'''
        logging.info('Municipality selection changed')
        selected = self.municipality.selectedValues()
        self.store.setSetting('selectedMunicipalities', selected)
        self.store.setSetting('selectedMunicipality', selected[0] if len(selected) == 1 else '')
        self.loadColonies()
        if self.validSession and selected != ['ALL']:
            for municipalityId in self.selectedMunicipalities():
                self.engine.submit('refreshColonies', municipalityId=municipalityId)

    def selectedMunicipalities(self):
        '''Expand checked municipality identities. Inputs: None. Output: list.'''
        logging.debug('Reading selected municipalities')
        selected = self.municipality.selectedValues()
        return ([entry['id'] for entry in self.store.catalog('municipality') if entry['active']]
                if 'ALL' in selected else selected)

    def selectedColonies(self):
        '''Group checked colonies by municipality. Inputs: None. Output: dictionary.'''
        logging.debug('Grouping selected colonies')
        municipalities = self.selectedMunicipalities()
        selected = self.colony.selectedValues()
        if 'ALL' in selected:
            return {municipalityId: ['ALL'] for municipalityId in municipalities}
        if len(municipalities) == 1:
            return {municipalities[0]: selected} if selected else {}
        grouped = {}
        for value in selected:
            municipalityId, colonyId = value.split(':', 1)
            grouped.setdefault(municipalityId, []).append(colonyId)
        return grouped

    def loadColonies(self):
        '''Load colonies for only the currently selected municipality. Inputs: None. Output: None.'''
        logging.debug('Loading dependent colony options')
        municipalities = self.selectedMunicipalities()
        selected = self.colony.selectedValues()
        previous = getattr(self, 'colonyParents', [])
        # Convert single-parent values to stable compound identities before rebuilding.
        stable = (selected if len(previous) != 1 or selected == ['ALL'] else
                  [f'{previous[0]}:{value}' for value in selected])
        if selected == ['ALL'] and previous != municipalities:
            stable = [f'{parent}:{entry["id"]}' for parent in previous
                      for entry in self.store.catalog('colony', parent) if entry['active']]
        self.colonyParents = municipalities
        self.colony.blockSignals(True)
        self.colony.clear()
        self.colony.setEnabled(bool(municipalities) and self.municipality.selectedValues() != ['ALL']
                               and not self.busyActions.intersection(('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')))
        self.colony.addItem('Select colonies', '')
        if municipalities:
            self.colony.addItem('ALL colonies', 'ALL')
        municipalityNames = {entry['id']: entry['name'] for entry in self.store.catalog('municipality')}
        for municipalityId in municipalities:
            if len(municipalities) > 1:
                self.colony.addItem(municipalityNames[municipalityId], '')
                heading = self.colony.model().item(self.colony.count() - 1)
                heading.setEnabled(False)
                heading.setFont(QFont('Arial', 12, QFont.Weight.Bold))
            for entry in self.store.catalog('colony', municipalityId):
                option = dict(entry)
                if len(municipalities) > 1:
                    option['id'] = f'{municipalityId}:{entry["id"]}'
                    option['name'] = f'{municipalityNames[municipalityId]} / {entry["name"]}'
                self.appendOption(self.colony, option)
        if self.municipality.selectedValues() == ['ALL']:
            restored = ['ALL']
        elif len(municipalities) == 1:
            prefix = municipalities[0] + ':'
            restored = [value[len(prefix):] for value in stable if value.startswith(prefix)]
            if stable == ['ALL'] and previous == municipalities:
                restored = ['ALL']
        else:
            restored = stable if stable != ['ALL'] or previous == municipalities else []
        self.colony.setSelectedValues(restored)
        self.colony.blockSignals(False)
        self.updateAddButton()
        self.tintCatalog()

    def tintCatalog(self):
        '''Tint already queued municipalities and exact colony pairs. Inputs: None. Output: None.'''
        logging.debug('Marking queued catalog entries')
        jobs = self.store.jobs()
        pairs = {(job['municipalityId'], job['colonyId']) for job in jobs}
        parents = {parent for parent, colony in pairs}
        for index in range(self.municipality.count()):
            item = self.municipality.model().item(index)
            municipalityId = self.municipality.itemData(index)
            item.setBackground(QColor('#e1f0e6') if municipalityId in parents else QColor('transparent'))
            item.setToolTip(f'{sum(job["municipalityId"] == municipalityId for job in jobs)} colony jobs already queued'
                            if municipalityId in parents else '')
        municipalities = self.selectedMunicipalities()
        for index in range(self.colony.count()):
            value = self.colony.itemData(index)
            pair = tuple(value.split(':', 1)) if value and ':' in value else (municipalities[0], value) if len(municipalities) == 1 else None
            self.colony.model().item(index).setBackground(QColor('#e1f0e6') if pair in pairs else QColor('transparent'))
            self.colony.model().item(index).setToolTip('Already in the queue' if pair in pairs else '')

    def updateAddButton(self):
        '''Enable Add for a valid, current, authenticated selection. Inputs: None. Output: None.'''
        logging.debug('Updating Add availability')
        if not hasattr(self, 'addButton'):
            return
        queueBusy = bool(self.busyActions.intersection(('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')))
        self.addButton.setEnabled(bool(self.validSession and self.selectedColonies() and not queueBusy))
        self.addButton.setText('Adding...' if 'add' in self.busyActions else 'Add')
        self.updateButton.setEnabled(not queueBusy and 'inspectDataset' not in self.busyActions)
        self.pidButton.setEnabled(not queueBusy)

    def chooseFolder(self):
        '''Persist the folder for subsequent jobs. Inputs: None. Output: None.'''
        logging.info('Choosing export folder')
        folder = QFileDialog.getExistingDirectory(self, 'Save property records to', self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.store.setSetting('outputFolder', folder)

    def chooseUpdateFolder(self):
        '''Validate an existing dataset on the worker before confirmation. Inputs: None. Output: None.'''
        logging.info('Selecting an existing dataset for update')
        options = QDialog(self)
        options.setWindowTitle('Update data')
        layout = QVBoxLayout(options)
        mobileOnly = QCheckBox('Update only properties with no mobile numbers')
        mobileOnly.setToolTip('Check existing records whose Mobile Numbers value is empty or -. Other records are kept unchanged.')
        layout.addWidget(mobileOnly)
        mobileNote = QLabel('New numbers will be saved in your original dataset. A timestamped Updated-mobileNos folder will contain only records where numbers were found.')
        mobileNote.setWordWrap(True)
        mobileNote.hide()
        mobileOnly.toggled.connect(mobileNote.setVisible)
        layout.addWidget(mobileNote)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(options.accept)
        buttons.rejected.connect(options.reject)
        layout.addWidget(buttons)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        self.mobileOnlyUpdate = mobileOnly.isChecked()
        folder = QFileDialog.getExistingDirectory(self, 'Update existing property data', self.folder.text())
        if folder:
            self.request('inspectDataset', folder=folder, mobileOnly=self.mobileOnlyUpdate)
            self.notice.setText('Validating dataset folders, record identities and CSV/Excel consistency...')

    def confirmDatasetUpdate(self, inventory):
        '''Explain update behavior and select resume/new generation. Inputs: <inventory>. Output: None.'''
        logging.info('Confirming validated dataset update')
        self.operationPanel.hide()
        entries = inventory['entries']
        unfinished = any(entry['portable'] and entry['portable']['mode'] == 'update' and
                         entry['portable']['status'] not in ('complete', 'complete_warnings') for entry in entries)
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Update existing data')
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(f'Validated {len(entries)} colony dataset(s).')
        dialog.setInformativeText('The app will fetch current map data, details and photos. Previous rows stay available '
            'until successfully checked; new properties are added and records not found in a completed scan are retained for review. '
            'Updates can take as long as a fresh run and can be paused or resumed. Previous baseline values and photos are retained. '
            'A valid portal login is needed to fetch data.' +
            ('\n\nAn unfinished update was found. Resume it, or start a new scan from zero coverage?' if unfinished else ''))
        if inventory.get('mobileOnly'):
            dialog.setText('Update only records with no mobile numbers?')
            dialog.setInformativeText(
                'Only existing properties whose Mobile Numbers value is empty or - will be checked. '
                'Newly found numbers will be saved in the original dataset. Other property fields and photos are preserved. '
                'A timestamped Updated-mobileNos folder under this root will contain only records where numbers were newly found, '
                'using the same municipality/colony folders. These report folders are ignored during future imports. '
                'Map searches are still needed to find current property links. You can pause and resume this update.' +
                ('\n\nResume the unfinished update, or start a new one?' if unfinished else ''))
        dialog.setDetailedText('\n'.join(f'{entry["municipalityName"]} / {entry["colonyName"]}: {entry["rowCount"]} exported rows'
                                         for entry in entries))
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel |
                                  (QMessageBox.StandardButton.No if unfinished else QMessageBox.StandardButton.NoButton))
        dialog.button(QMessageBox.StandardButton.Yes).setText('Resume update' if unfinished else 'Start update')
        if unfinished:
            dialog.button(QMessageBox.StandardButton.No).setText('Start new update')
        dialog.setDefaultButton(QMessageBox.StandardButton.Yes if unfinished else QMessageBox.StandardButton.Cancel)
        choice = dialog.exec()
        if choice in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No):
            self.request('importDataset', inventory=inventory, restart=choice == QMessageBox.StandardButton.No)

    def addSelection(self):
        '''Confirm ALL replacement and enqueue the selection. Inputs: None. Output: None.'''
        logging.info('Adding selected municipality and colony')
        selections = self.selectedColonies()
        globalAll = self.municipality.selectedValues() == ['ALL']
        if not selections:
            return
        if globalAll or any(values == ['ALL'] for values in selections.values()):
            scope = 'all municipalities' if globalAll else ', '.join(
                entry['name'] for entry in self.store.catalog('municipality') if entry['id'] in selections)
            answer = QMessageBox.warning(self, 'Start a fresh ALL run',
                f'This will reset all added colony jobs for {scope} and start fresh. '
                'Existing output folders will be moved into _archive. Continue?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.store.setSetting('outputFolder', self.folder.text())
        self.request('add', selections=selections, globalAll=globalAll, folder=self.folder.text())
        self.notice.setText('Refreshing options and adding colony jobs...')

    def refreshCatalog(self):
        '''Refresh the selected parent and child options. Inputs: None. Output: None.'''
        logging.info('Refreshing dropdown options')
        self.request('refreshMunicipalities')
        for municipalityId in self.selectedMunicipalities():
            self.engine.submit('refreshColonies', municipalityId=municipalityId)

    def clearAddedSelection(self):
        '''Clear checks only after a successful addition. Inputs: None. Output: None.'''
        logging.info('Clearing successfully added selections')
        self.municipality.setSelectedValues([])
        self.municipality.searchInput.clear()
        self.colony.searchInput.clear()
        self.municipalityChanged()

    def controlJobs(self, identifiers, action):
        '''Apply parent, child, or global start/pause controls. Inputs: <identifiers>, <action>. Output: None.'''
        logging.info(f'Applying {action} control')
        if self.closing or self.busyActions.intersection(('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')):
            return
        self.store.control(identifiers, action)
        self.refreshJobs()
        self.notice.setText('Pause requested; Excel snapshots will be saved.' if action == 'pause'
                            else ('Jobs queued.' if self.validSession else 'Jobs queued; verify an OTP to resume.'))

    def deleteJobs(self, identifiers):
        '''Confirm removal of scoped jobs, retaining saved outputs. Inputs: <identifiers>. Output: None.'''
        logging.info('Requesting job deletion')
        if QMessageBox.question(self, 'Remove jobs',
            f'Remove {len(identifiers)} colony job(s) from the queue? '
            'Saved records, photos and portable dataset checkpoints will be retained.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self.request('delete', jobIds=identifiers)

    def groupIds(self, municipalityId):
        '''Resolve current children for a parent action. Inputs: <municipalityId>. Output: list of IDs.'''
        logging.debug('Resolving municipality action scope')
        return [job['id'] for job in self.jobsById.values() if job['municipalityId'] == municipalityId]

    def dispatchAction(self, identity, isGroup, action):
        '''Resolve current job membership when a row action is activated. Inputs: <identity>, <isGroup>, <action>.'''
        logging.info(f'Activating row action {action}')
        if self.closing or self.busyActions.intersection(('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')):
            return
        identifiers = self.groupIds(identity) if isGroup else [identity]
        if action in ('start', 'pause'):
            self.controlJobs(identifiers, action)
        elif action == 'folder':
            self.openJobFolder(identifiers, isGroup)
        elif action == 'delete':
            self.deleteJobs(identifiers)

    def showJobMenu(self, position):
        '''Expose row actions through an accessible native menu. Inputs: <position>. Output: None.'''
        logging.debug('Opening job action menu')
        item = self.tree.itemAt(position) or self.tree.currentItem()
        if item is None:
            return
        identity = item.data(0, Qt.ItemDataRole.UserRole)
        isGroup = item.data(0, Qt.ItemDataRole.UserRole + 1)
        identifiers = self.groupIds(identity) if isGroup else [identity]
        jobs = [self.jobsById[jobId] for jobId in identifiers if jobId in self.jobsById]
        menu = QMenu(self)
        delegate = self.tree.itemDelegateForColumn(5)
        for action, label, icon in delegate.actions:
            entry = menu.addAction(self.style().standardIcon(icon), label)
            entry.setEnabled(delegate.enabled(action, jobs))
            entry.triggered.connect(lambda checked=False, command=action:
                                    self.dispatchAction(identity, isGroup, command))
        if isGroup:
            entry = menu.addAction('Export municipality Excel')
            entry.triggered.connect(lambda checked=False: self.exportMunicipality(identity))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def refreshJobs(self):
        '''Update counters and rows without rebuilding the tree. Inputs: None. Output: None.'''
        logging.debug('Refreshing live job counts')
        jobs = self.store.jobs()
        queueSignature = tuple(job['id'] for job in jobs)
        if queueSignature != getattr(self, 'queueSignature', None):
            self.queueSignature = queueSignature
            self.tintCatalog()
        previousJobs = self.jobsById
        self.jobsById = {job['id']: job for job in jobs}
        modes = {job['mode'] for job in jobs}
        self.queueLabel.setText('Update queue' if modes == {'update'} else
                                'Scraping / Update queue' if len(modes) > 1 else 'Scraping queue')
        for identity in list(self.jobItems):
            if identity not in self.jobsById:
                item = self.jobItems.pop(identity)
                item.parent().removeChild(item)
        grouped = {}
        for job in jobs:
            grouped.setdefault(job['municipalityId'], []).append(job)
            parent = self.groupItems.get(job['municipalityId'])
            if parent is None:
                parent = JobItem(self.tree)
                parent.setText(0, job['municipalityName'])
                parent.setFont(0, QFont('Arial', 12, QFont.Weight.DemiBold))
                parent.setBackground(0, QColor('#edf5f1'))
                parent.setData(0, Qt.ItemDataRole.UserRole, job['municipalityId'])
                parent.setData(0, Qt.ItemDataRole.UserRole + 1, True)
                self.groupItems[job['municipalityId']] = parent
                self.addMunicipalityExportControl(parent, job['municipalityId'], job['municipalityName'])
                parent.setExpanded(len(self.groupItems) == 1)
            item = self.jobItems.get(job['id'])
            if item is None:
                item = JobItem(parent)
                self.jobItems[job['id']] = item
                item.setData(0, Qt.ItemDataRole.UserRole, job['id'])
                item.setData(0, Qt.ItemDataRole.UserRole + 1, False)
            if previousJobs.get(job['id']) != job or getattr(self, 'renderedSession', None) != self.sessionState:
                self.updateJobItem(item, job)
        self.renderedSession = self.sessionState
        for municipalityId in list(self.groupItems):
            if municipalityId not in grouped:
                parent = self.groupItems.pop(municipalityId)
                self.groupExportLabels.pop(municipalityId, None)
                self.groupVerifyButtons.pop(municipalityId, None)
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(parent))
            else:
                self.updateGroupItem(self.groupItems[municipalityId], grouped[municipalityId])
        self.metrics['jobs'].setText(f'Total Jobs: {len(jobs):,}')
        active = self.trafficState.get('colonies', 0)
        limit = self.trafficState.get('limit', min(4, self.store.setting('parallelRequests', 2)))
        self.metrics['active'].setText(f'Active: {active:,}/{limit}')
        self.metrics['active'].setToolTip(
            f'{sum(bool(job["desired"]) for job in jobs):,} jobs enabled; {active} colony work units running. '
            f'Effective concurrency {limit}; configured ceiling {self.trafficState.get("configured", limit)}. '
            f'{self.trafficState.get("active", 0)} HTTP requests in flight; '
            f'effective request interval {self.trafficState.get("intervalMs", self.store.setting("requestDelayMs", 650))} ms.')
        self.metrics['properties'].setText(f'Total properties: {sum(job["uniqueCount"] for job in jobs):,}')
        self.metrics['properties'].setToolTip('Sum of discovered properties across jobs; overlapping colonies may contain the same property.')
        self.updateSpeed(jobs)
        self.emptyLabel.setVisible(not jobs)
        self.filterJobs()
        if self.tree.header().isSortIndicatorShown():
            self.tree.sortItems(self.sortColumn, self.sortOrder)

    def updateSpeed(self, jobs):
        '''Show recently resolved property pipelines per minute. Inputs: <jobs>. Output: None.'''
        logging.debug('Updating scraping speed')
        now = time.monotonic()
        current = {job['id']: job['completedCount'] for job in jobs}
        self.sessionCompleted += sum(max(0, count - self.previousCompleted.get(identity, count))
                                     for identity, count in current.items())
        self.previousCompleted = current
        active = self.validSession and any(job['desired'] for job in jobs)
        if not active:
            self.speedSamples.clear()
        self.speedSamples.append((now, self.sessionCompleted))
        while len(self.speedSamples) > 1 and self.speedSamples[1][0] <= now - 60:
            self.speedSamples.popleft()
        elapsed = now - self.speedSamples[0][0]
        speed = (self.sessionCompleted - self.speedSamples[0][1]) * 60 / elapsed if elapsed > 0 and active else 0
        self.metrics['speed'].setText(f'Scraping speed: {speed:.1f}/min')
        self.metrics['speed'].setToolTip('Resolved property pipelines per minute over the last minute, including terminal failures. Details and photos are included.')

    def updateJobItem(self, item, job):
        '''Render one colony's current checkpoint. Inputs: <item>, <job>. Output: None.'''
        logging.debug('Rendering colony row')
        status = job['status']
        if job['desired'] and not self.validSession:
            status = 'waiting_service' if self.sessionState == 'unavailable' else 'waiting_login'
        coverage = min(100, max(0, job['coverage'] * 100))
        statusText = STATUSLABELS.get(status, status)
        if status == 'scraping':
            operation = ('Updating mobiles' if job.get('updateScope') == 'missing_mobile' else 'Updating') if job['mode'] == 'update' else 'Processing'
            statusText = (f'{operation}: {job["completedCount"]:,}/{job["uniqueCount"]:,}'
                          if job['uniqueCount'] > job['completedCount'] else 'Scanning map')
        if job['mode'] == 'update':
            statusText = {'queued': 'Update queued', 'preparing': 'Preparing update',
                          'complete': 'Update complete', 'complete_warnings': 'Updated with warnings'}.get(status, statusText)
            if status == 'scraping' and job['uniqueCount'] == job['completedCount']:
                statusText = 'Updating map'
        values = [job['colonyName'], formatElapsed(self.elapsedValues['colonies'].get(job['id'], 0)), statusText,
                  f'{coverage:.1f}%' if job['tileTotal'] else '-', f'{job["uniqueCount"]:,}']
        for column, value in enumerate(values):
            item.setText(column, value)
        for column, value in [(3, coverage), (4, job['uniqueCount'])]:
            item.setData(column, Qt.ItemDataRole.UserRole, value)
        item.setData(1, Qt.ItemDataRole.UserRole, self.elapsedValues['colonies'].get(job['id'], 0))
        item.setToolTip(1, 'Processing time includes retries. Queued time, manual pauses and time with the app closed are excluded.')
        colors = {'complete': '#087962', 'error': '#b34343', 'waiting_login': '#a56b16', 'retrying': '#a56b16'}
        item.setForeground(2, QColor(colors.get(status, '#53666c')))
        if job['exportError']:
            item.setText(2, 'Excel save pending')
            item.setForeground(2, QColor('#b34343'))
        item.setToolTip(2, job['exportError'] or job['error'] or (
            'Processing details and photos for discovered properties; map coverage advances after the next map tile.'
            if status == 'scraping' and job['uniqueCount'] > job['completedCount'] else ''))
        item.setToolTip(3, f'Map tiles: {job["tileDone"]:,} / {job["tileTotal"]:,}. '
                          'Coverage stays fixed while discovered properties finish their details and photos.')
        item.setToolTip(4, f'{job["completedCount"]:,} fully processed / {job["uniqueCount"]:,} discovered' +
                          (f'; {job["baselineCount"]:,} previous records retained during update' if job['mode'] == 'update' else ''))
        item.setToolTip(5, job['folder'])

    def updateGroupItem(self, parent, jobs):
        '''Aggregate a municipality's jobs and scoped controls. Inputs: <parent>, <jobs>. Output: None.'''
        logging.debug('Rendering municipality totals')
        municipalityId = jobs[0]['municipalityId']
        exported = self.municipalityExports.get(municipalityId)
        lastExport = 'Never'
        if exported:
            lastExport = datetime.fromisoformat(exported['exportedAt']).strftime('%d %b %Y, %H:%M IST')
        label = self.groupExportLabels[municipalityId]
        text = f'Last exported: {lastExport}'
        if label.text() != text:
            label.setText(text)
            label.setToolTip(f'{text}\n{exported["path"]}' if exported else 'Export all saved properties collected so far')
        seconds = self.elapsedValues['municipalities'].get(municipalityId, 0)
        parent.setText(1, formatElapsed(seconds))
        parent.setData(1, Qt.ItemDataRole.UserRole, seconds)
        parent.setToolTip(1, 'Elapsed processing time; overlapping colony activity is counted once.')
        active = sum(bool(job['desired']) for job in jobs)
        complete = sum(job['status'] in ('complete', 'complete_warnings') for job in jobs)
        coverage = sum(job['coverage'] for job in jobs) / len(jobs) * 100
        parent.setText(2, f'{complete} / {len(jobs)} complete')
        parent.setText(3, f'{min(100, coverage):.1f}%')
        parent.setText(4, f'{sum(job["uniqueCount"] for job in jobs):,}')
        parent.setData(3, Qt.ItemDataRole.UserRole, coverage)
        for column, key in [(4, 'uniqueCount')]:
            parent.setData(column, Qt.ItemDataRole.UserRole, sum(job[key] for job in jobs))

    def filterJobs(self):
        '''Apply text and status filters while retaining parent hierarchy. Inputs: None. Output: None.'''
        logging.debug('Filtering job tree')
        search = self.search.text().casefold()
        status = self.statusFilter.currentData()
        for jobId, item in self.jobItems.items():
            job = self.jobsById[jobId]
            matchesText = search in (job['municipalityName'] + ' ' + job['colonyName']).casefold()
            currentStatus = ('waiting_service' if self.sessionState == 'unavailable' else 'waiting_login') if job['desired'] and not self.validSession else job['status']
            matchesStatus = status == 'all' or currentStatus == status or (
                status == 'active' and job['desired'] and self.validSession) or (
                status == 'error' and currentStatus == 'complete_warnings') or (
                status == 'complete' and currentStatus == 'complete_warnings')
            item.setHidden(not (matchesText and matchesStatus))
        for parent in self.groupItems.values():
            parent.setHidden(all(parent.child(index).isHidden() for index in range(parent.childCount())))

    def sortJobs(self, column):
        '''Sort both municipality groups and their child rows. Inputs: <column>. Output: None.'''
        logging.debug('Sorting job tree')
        if column == 4:
            return
        self.sortOrder = (Qt.SortOrder.DescendingOrder if self.sortColumn == column and
                          self.sortOrder == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder)
        self.sortColumn = column
        self.tree.sortItems(column, self.sortOrder)
        self.tree.header().setSortIndicator(column, self.sortOrder)
        self.tree.header().setSortIndicatorShown(True)

    def openJobFolder(self, jobIds, isGroup):
        '''Open selected exports with the operating system. Inputs: <jobIds>, <isGroup>. Output: None.'''
        logging.info('Opening output folder')
        folders = {Path(self.jobsById[jobId]['folder']).parent if isGroup else
                   Path(self.jobsById[jobId]['folder']) for jobId in jobIds if jobId in self.jobsById}
        if len(folders) > 1:
            self.notice.setText('This municipality has jobs in multiple output folders; opening each folder.')
        for folder in sorted(folders):
            self.openFolder(folder)

    def openFolder(self, folder):
        '''Open a local folder. Inputs: <folder>. Output: None.'''
        logging.info('Opening local folder')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def showSettings(self):
        '''Edit request pacing and concurrency. Inputs: None. Output: None.'''
        logging.info('Opening application settings')
        dialog = QDialog(self)
        dialog.setWindowTitle('Settings')
        layout = QFormLayout(dialog)
        delay = QSpinBox()
        delay.setRange(250, 10000)
        delay.setSingleStep(50)
        delay.setSuffix(' ms')
        delay.setValue(self.store.setting('requestDelayMs', 650))
        parallel = QSpinBox()
        parallel.setObjectName('parallelRequests')
        parallel.setRange(1, 100)
        parallel.setValue(self.store.setting('parallelRequests', 2))
        parallel.setToolTip('Upper bound. Starts with at most 4 active colonies, reduces on congestion, and increases gradually after healthy work.')
        delay.setToolTip('Minimum interval between request starts across all colonies. Congestion can increase the effective interval temporarily.')
        layout.addRow('Request interval', delay)
        layout.addRow('Parallel requests ceiling', parallel)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.setSetting('requestDelayMs', delay.value())
            self.store.setSetting('parallelRequests', parallel.value())

    def handleEvent(self, event, payload):
        '''Render worker events on the GUI thread. Inputs: <event>, <payload>. Output: None.'''
        logging.debug(f'Handling worker event {event}')
        if self.closing and event != 'stopped':
            return
        if event == 'elapsed':
            self.elapsedValues = payload
            for jobId, item in self.jobItems.items():
                seconds = payload['colonies'].get(jobId, 0)
                item.setText(1, formatElapsed(seconds))
                item.setData(1, Qt.ItemDataRole.UserRole, seconds)
            for identity, parent in self.groupItems.items():
                seconds = payload['municipalities'].get(identity, 0)
                parent.setText(1, formatElapsed(seconds))
                parent.setData(1, Qt.ItemDataRole.UserRole, seconds)
        elif event == 'pidListChanged':
            self.refreshPidList()
        elif event == 'pidVerified':
            self.busyActions.discard('verifyPids')
            self.busyActions.discard('fetchMissingPids')
            self.refreshJobs()
            PidResultsDialog(self, payload).exec()
        elif event == 'pidMasterUpdated':
            self.municipalityExports[payload['municipalityId']] = payload
            self.refreshJobs()
        elif event == 'operationProgress':
            self.operationLabel.setText(payload['message'])
            total = payload['total']
            self.operationProgress.setRange(0, total if total > 0 else 0)
            self.operationProgress.setValue(payload['current'] if total > 0 else 0)
            if self.busyActions.intersection(('inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')):
                self.operationPanel.show()
        elif event == 'traffic':
            self.trafficState = payload
            if payload['recovering'] and self.sessionState == 'unavailable':
                self.sessionLabel.setText(f'Portal cooldown: {payload["cooldown"]}s' if payload['cooldown'] else
                                         'Checking portal work requests')
            self.refreshJobs()
        elif event == 'session':
            self.validSession = payload == 'valid'
            self.sessionState = payload
            text = {'valid': 'Ready to scrape', 'checking': 'Checking session',
                    'expired': 'Login required', 'unverified': 'Login / connection required',
                    'unavailable': 'Portal unavailable; retrying'}.get(payload, payload)
            self.sessionLabel.setText(text)
            color = '#168852' if self.validSession else '#c33e42'
            self.sessionLed.setStyleSheet(f'background: {color}; border-radius: 6px;')
            self.sessionLed.setToolTip('Authenticated session verified; no login needed' if self.validSession else text)
            self.updateAddButton()
            self.refreshJobs()
            if payload == 'expired':
                self.notice.setText('Session expired. Active jobs are checkpointed and will resume after OTP verification.')
                self.tray.showMessage('Login required', 'Scraping paused. Verify a new OTP to continue.')
        elif event == 'otpSent':
            if self.otpDialog is None:
                self.otpDialog = OtpDialog(self)
                self.otpDialog.finished.connect(self.otpClosed)
            self.otpDialog.message.setText('')
            self.otpDialog.verify.setEnabled(True)
            self.otpDialog.resend.setEnabled(True)
            self.otpDialog.otp.clear()
            self.otpDialog.show()
            self.otpDialog.raise_()
        elif event == 'otpVerified':
            if self.otpDialog:
                self.otpDialog.accept()
            self.notice.setText('Login verified. Queued jobs will resume.')
            for municipalityId in self.selectedMunicipalities():
                self.engine.submit('refreshColonies', municipalityId=municipalityId)
        elif event == 'catalog':
            if payload['kind'] == 'municipality':
                self.loadMunicipalities()
                self.refreshPidList()
            elif payload['parent'] in self.selectedMunicipalities():
                self.loadColonies()
        elif event == 'jobsChanged':
            self.refreshJobs()
        elif event == 'addSucceeded':
            self.clearAddedSelection()
        elif event == 'datasetInspected':
            self.confirmDatasetUpdate(payload)
        elif event == 'datasetImported':
            self.folder.setText(payload)
            self.store.setSetting('outputFolder', payload)
        elif event == 'municipalityExported':
            self.municipalityExportComplete(payload)
        elif event in ('notice', 'error'):
            self.notice.setText(payload)
        elif event == 'actionFailed':
            self.notice.setText(payload['message'])
            if payload['action'] in ('inspectDataset', 'importDataset', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids'):
                QMessageBox.warning(self, 'Municipality export' if payload['action'] == 'exportMunicipality' else 'Dataset update', payload['message'])
            if payload['action'] == 'verifyOtp' and self.otpDialog:
                self.otpDialog.message.setText(payload['message'])
                self.otpDialog.verify.setEnabled(True)
                self.otpDialog.resend.setEnabled(True)
        elif event == 'actionFinished':
            self.busyActions.discard(payload)
            if payload == 'generateOtp':
                self.otpButton.setEnabled(True)
                self.phone.setEnabled(True)
            if payload in ('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids') and not self.busyActions.intersection(
                    ('add', 'inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')):
                self.tree.setEnabled(True)
                self.municipality.setEnabled(True)
                self.colony.setEnabled(bool(self.selectedMunicipalities()) and
                                       self.municipality.selectedValues() != ['ALL'])
            if not self.busyActions.intersection(('inspectDataset', 'importDataset', 'delete', 'exportMunicipality', 'importPidList', 'verifyPids', 'fetchMissingPids')):
                self.operationPanel.hide()
            self.updateAddButton()
        elif event == 'stopped' and self.closing:
            self.timer.stop()
            QTimer.singleShot(0, self.finishClose)

    def otpClosed(self):
        '''Forget the visible OTP after the dialog closes. Inputs: None. Output: None.'''
        logging.debug('Closing OTP dialog')
        if self.otpDialog:
            self.otpDialog.otp.clear()
            self.otpDialog.deleteLater()
            self.otpDialog = None

    def closeEvent(self, event):
        '''Wait asynchronously for checkpoint/export completion. Inputs: <event>. Output: None.'''
        logging.info('Closing desktop application')
        if not self.engine.is_alive():
            self.timer.stop()
            self.municipality.hidePopup()
            self.colony.hidePopup()
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        self.closing = True
        self.centralWidget().setEnabled(False)
        self.notice.setText('Finishing the current request and saving Excel snapshots...')
        self.engine.stop()

    def finishClose(self):
        '''Exit after the worker has completed shutdown. Inputs: None. Output: None.'''
        logging.info('Application checkpoints saved; exiting')
        if self.engine.is_alive():
            QTimer.singleShot(50, self.finishClose)
        else:
            self.close()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tree'):
            QTimer.singleShot(0, self.fitColumns)

    def fitColumns(self):
        '''Keep names and actions readable in compact windows. Inputs: None. Output: None.'''
        logging.debug('Fitting table columns')
        otherWidth = sum(self.tree.columnWidth(column) for column in range(1, 6)
                         if not self.tree.isColumnHidden(column))
        self.tree.setColumnWidth(0, max(510, self.tree.viewport().width() - otherWidth - 2))


class JobItem(QTreeWidgetItem):
    '''Use numeric values when sorting counts and coverage columns.'''

    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        if column in (1, 3, 4):
            return (self.data(column, Qt.ItemDataRole.UserRole) or 0) < (other.data(column, Qt.ItemDataRole.UserRole) or 0)
        return self.text(column).casefold() < other.text(column).casefold()


class ActionDelegate(QStyledItemDelegate):
    '''Draw row controls without allocating widgets for every queued colony.'''

    actions = [
        ('start', 'Start / resume', QStyle.StandardPixmap.SP_MediaPlay),
        ('pause', 'Pause and save Excel', QStyle.StandardPixmap.SP_MediaPause),
        ('folder', 'Open output folder', QStyle.StandardPixmap.SP_DirOpenIcon),
        ('delete', 'Remove job(s)', QStyle.StandardPixmap.SP_TrashIcon),
    ]

    def __init__(self, window):
        super().__init__(window.tree)
        self.window = window

    def scope(self, index):
        '''Resolve a row's current identity and scope. Inputs: <index>. Output: identity, group, jobs.'''
        logging.debug('Resolving row action scope')
        identity = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        isGroup = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole + 1)
        ids = self.window.groupIds(identity) if isGroup else [identity]
        return identity, isGroup, [self.window.jobsById[jobId] for jobId in ids if jobId in self.window.jobsById]

    def enabled(self, action, jobs):
        '''Compute whether a scoped action is available. Inputs: <action>, <jobs>. Output: boolean.'''
        logging.debug('Checking row control availability')
        if not jobs or self.window.closing or 'add' in self.window.busyActions:
            return False
        if action == 'start':
            return any(not job['desired'] and job['status'] != 'complete' for job in jobs)
        if action == 'pause':
            return any(job['desired'] for job in jobs)
        return True

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        unusedIdentity, unusedGroup, jobs = self.scope(index)
        for actionIndex, (action, unusedTooltip, icon) in enumerate(self.actions):
            rect = QRect(option.rect.left() + 5 + actionIndex * 32,
                         option.rect.center().y() - 11, 22, 22)
            standardIcon = self.window.style().standardIcon(icon)
            mode = standardIcon.Mode.Normal if self.enabled(action, jobs) else standardIcon.Mode.Disabled
            standardIcon.paint(painter, rect, Qt.AlignmentFlag.AlignCenter, mode)

    def editorEvent(self, event, model, option, index):
        if event.type() != QEvent.Type.MouseButtonRelease or event.button() != Qt.MouseButton.LeftButton:
            return False
        actionIndex = int((event.position().x() - option.rect.left() - 2) // 32)
        if not 0 <= actionIndex < len(self.actions):
            return False
        identity, isGroup, jobs = self.scope(index)
        action = self.actions[actionIndex][0]
        if self.enabled(action, jobs):
            self.window.dispatchAction(identity, isGroup, action)
        return True

    def helpEvent(self, event, view, option, index):
        actionIndex = int((event.pos().x() - option.rect.left() - 2) // 32)
        if 0 <= actionIndex < len(self.actions):
            QToolTip.showText(event.globalPos(), self.actions[actionIndex][1], view)
            return True
        return False
