'''Compact PID verification results with optional, paginated details.'''

# Standard Library Imports
import logging

# Third Party Imports
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                              QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ulb_scraper.pid_verification import VERIFICATIONNOTE


class PidResultsDialog(QDialog):
    '''Keep large PID lists off screen until the user requests details.'''

    def __init__(self, window, result):
        super().__init__(window)
        self.setWindowTitle('PID verification — ' + result['municipalityName'])
        self.resize(650, 280)
        self.offset = 0
        self.result = result
        self.reference = window.engine.pidReference
        layout = QVBoxLayout(self)
        self.counts = QLabel(f'Found: {result["found"]:,}    Missing: {result["missing"]:,}    '
                             f'Incomplete: {result["pending"]:,}')
        self.counts.setStyleSheet('font-weight: bold;')
        layout.addWidget(self.counts)
        note = QLabel(VERIFICATIONNOTE + '\nAll jobs remain paused. Resume them when you are ready.')
        note.setWordWrap(True)
        layout.addWidget(note)
        if 'fetched' in result:
            message = f'Properties saved: {result["fetched"]:,}. Lookups needing review: {result["failures"]:,}.'
            if result.get('cancelled'):
                message += ' Fetching stopped; saved progress is retained.'
            if result.get('exportErrors'):
                message += '\nSaved data is retained. Close open Excel files and export again: ' + '; '.join(result['exportErrors'])
            outcome = QLabel(message)
            outcome.setWordWrap(True)
            layout.addWidget(outcome)
        self.more = QPushButton('More details')
        self.more.setCheckable(True)
        layout.addWidget(self.more)
        self.details = QWidget()
        detailsLayout = QVBoxLayout(self.details)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(['PID', 'Result', 'Details'])
        detailsLayout.addWidget(self.table)
        pages = QHBoxLayout()
        self.previous = QPushButton('Previous')
        self.next = QPushButton('Next')
        self.pageLabel = QLabel()
        pages.addWidget(self.previous)
        pages.addWidget(self.pageLabel)
        pages.addWidget(self.next)
        detailsLayout.addLayout(pages)
        layout.addWidget(self.details)
        self.details.hide()
        self.more.toggled.connect(self.showDetails)
        self.previous.clicked.connect(lambda: self.changePage(-100))
        self.next.clicked.connect(lambda: self.changePage(100))
        buttons = QHBoxLayout()
        fetch = QPushButton('Fetch missing PIDs')
        fetch.setEnabled(bool(result['missing'] + result['pending']) and window.validSession)
        fetch.setToolTip('Fetch missing and incomplete properties, including details and photos')
        fetch.clicked.connect(lambda: self.fetch(window))
        buttons.addWidget(fetch)
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def showDetails(self, visible):
        '''Load at most one page when expanded.'''
        self.details.setVisible(visible)
        if visible:
            self.changePage(0)
            self.resize(700, 550)

    def changePage(self, step):
        '''Read only the requested page from the disk-backed comparison.'''
        self.offset = max(0, self.offset + step)
        rows = self.reference.page(self.result['municipalityId'], self.offset)
        self.table.clear()
        for row in rows:
            state = {'pending': 'Incomplete', 'missing': 'Not found locally', 'failed': 'Needs review'}.get(row['state'], row['state'])
            QTreeWidgetItem(self.table, [row['pid'], state, row['message']])
        self.previous.setEnabled(self.offset > 0)
        total = self.result['missing'] + self.result['pending']
        self.next.setEnabled(self.offset + len(rows) < total)
        self.pageLabel.setText(f'{self.offset + 1 if rows else 0:,}–{self.offset + len(rows):,} of {total:,}')

    def fetch(self, window):
        '''Close the report only after the user confirms targeted recovery.'''
        logging.info('Requesting missing PID recovery from verification results')
        if window.fetchMissingPids(self.result):
            self.accept()
