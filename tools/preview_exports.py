'''Build normalized previews from a read-only checkpoint backup, without scraping.'''

# Standard Library Imports
import argparse
from contextlib import closing
import logging
from pathlib import Path
import shutil
import sqlite3
import tempfile

from scraper import slugify
from ulb_scraper.exporter import exportJob
from ulb_scraper.store import Store


def previewExports(source, destination):
    '''
    Test checkpoint upgrades and exports on an isolated database copy.

    Inputs:    <source>       -  Existing SQLite checkpoint path
               <destination>  -  New or empty directory for preview workbooks

    Output:    List of generated workbook paths; source files remain unchanged
    '''
    logging.info('Preparing read-only checkpoint preview')
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Checkpoint does not exist: {source}')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f'Preview destination must be new or empty: {destination}')
    results = []
    with tempfile.TemporaryDirectory(prefix='ulb-preview-') as temporary:
        backupPath = Path(temporary) / 'state.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(backupPath)) as backup:
                original.backup(backup)
        store = Store(backupPath)
        for job in store.jobs():
            folder = (destination /
                      f'{slugify(job["municipalityName"])}_{job["municipalityId"]}' /
                      f'{slugify(job["colonyName"])}_{job["colonyId"]}')
            originalFolder = Path(job['folder']).resolve()
            if folder.is_relative_to(originalFolder) or originalFolder.is_relative_to(folder):
                raise ValueError('Preview output must be separate from original colony folders')
            job['folder'] = str(folder)
            photoFolder = originalFolder / 'photos'
            if photoFolder.exists():
                shutil.copytree(photoFolder, folder / 'photos', dirs_exist_ok=True)
            results.append(exportJob(store, job))
            logging.info(f'Preview ready: {results[-1]}')
    return results


def main():
    '''Build offline previews. Inputs: CLI --source, --output. Output: None.'''
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.info('Starting normalized export preview')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('data/application/state.sqlite3'))
    parser.add_argument('--output', type=Path, default=Path('output/normalized-preview'))
    args = parser.parse_args()
    previewExports(args.source, args.output)


if __name__ == '__main__':
    main()
