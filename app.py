#!/usr/bin/env python3
'''Launch the native ULB Haryana scraper application.'''

# Standard Library Imports
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

# Third Party Imports
from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from ulb_scraper.gui import MainWindow
from ulb_scraper.store import Store


def applicationHome():
    '''
    Resolve a persistent, writable application data directory.

    Inputs:    None

    Output:    Path to the application home
    '''
    logging.debug('Resolving application home')
    if getattr(sys, 'frozen', False):
        if sys.platform == 'win32':
            return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ULBPropertyScraper'
        return Path.home() / '.ulb-property-scraper'
    return Path(__file__).resolve().parent / 'data' / 'application'


def configureLogging(home):
    '''Configure bounded desktop logs. Inputs: <home>. Output: None.'''
    logFolder = home / 'logs'
    logFolder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logFolder / 'application.log', maxBytes=5 * 1024 * 1024,
                                 backupCount=10, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(threadName)s %(funcName)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.info('Starting ULB Property Scraper')


def main():
    '''
    Start the desktop app, enforcing one worker per checkpoint database.

    Inputs:    None; optional --data-dir and --smoke-test CLI arguments

    Output:    Process exit code
    '''
    parser = argparse.ArgumentParser(description='ULB Property Scraper desktop application')
    parser.add_argument('--data-dir', type=Path, default=applicationHome())
    parser.add_argument('--smoke-test', action='store_true', help='Open the interface offline and exit')
    args = parser.parse_args()
    home = args.data_dir.resolve()
    home.mkdir(parents=True, exist_ok=True)
    configureLogging(home)
    application = QApplication(sys.argv[:1])
    application.setApplicationName('ULB Property Scraper')
    application.setOrganizationName('ULBPropertyScraper')
    lock = QLockFile(str(home / 'application.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None, 'Already running', 'Another scraper is using this application data folder.')
        return 1
    store = Store(home / 'state.sqlite3')
    window = MainWindow(store, home, startWorker=not args.smoke_test)
    window.show()
    if args.smoke_test:
        QTimer.singleShot(400, window.close)
    exitCode = application.exec()
    lock.unlock()
    return exitCode


if __name__ == '__main__':
    sys.exit(main())

