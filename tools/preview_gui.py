'''Render offline desktop previews without modifying actual application state.'''

# Standard Library Imports
import os
from pathlib import Path
import tempfile
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# Third Party Imports
from PySide6.QtWidgets import QApplication

from ulb_scraper.gui import MainWindow
from ulb_scraper.store import Store


def main():
    '''Generate desktop and compact screenshots with synthetic UI fixtures.'''
    application = QApplication([])
    destination = Path(__file__).resolve().parents[1] / 'output' / 'previews'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        store = Store(home / 'state.sqlite3')
        store.updateCatalog('municipality', '', [{'Text': 'PANCHKULA', 'Value': '60'},
                                                {'Text': 'AMBALA', 'Value': '44'}])
        store.updateCatalog('colony', '60', [{'Text': 'Sector 6', 'Value': '18949'},
                                           {'Text': 'Sector 7', 'Value': '18950'}])
        for index, (municipalityId, municipalityName, colonyId, colonyName, status) in enumerate([
                ('60', 'PANCHKULA', '18949', 'Sector 6', 'scraping'),
                ('60', 'PANCHKULA', '18950', 'Sector 7', 'paused'),
                ('60', 'PANCHKULA', '18951', 'Sector 8', 'complete'),
                ('44', 'AMBALA', '1', 'Model Town', 'queued'),
                ('44', 'AMBALA', '2', 'New Colony', 'queued')]):
            identity = store.addJobs([({'id': municipalityId, 'name': municipalityName},
                                      {'id': colonyId, 'name': colonyName})], home / 'exports')[0]
            with store.connect() as connection:
                connection.execute('''UPDATE jobs SET status=?, desired=?, uniqueCount=?, detailCount=?,
                    photoCount=?, coverage=?, tileDone=?, tileTotal=250, lastExport=? WHERE id=?''',
                    (status, int(status not in ('paused', 'complete')), 834 - index * 130,
                     251 if status != 'complete' else 834 - index * 130, 216,
                     0.64 if status != 'complete' else 1, 160, time.time(), identity))
        window = MainWindow(store, home, startWorker=False)
        window.handleEvent('session', 'valid')
        window.municipality.setCurrentIndex(window.municipality.findData('60'))
        window.colony.setCurrentIndex(window.colony.findData('18949'))
        window.notice.setText('PANCHKULA / Sector 6: checkpoint saved')
        for parent in window.groupItems.values():
            parent.setExpanded(True)
        window.show()
        for name, width, height in [('desktop', 1280, 820), ('compact', 980, 680)]:
            window.resize(width, height)
            application.processEvents()
            window.grab().save(str(destination / f'{name}.png'))
        window.close()


if __name__ == '__main__':
    main()
