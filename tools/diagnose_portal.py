'''Read-only portal diagnostics using existing cookies and saved failing jobs.'''

# Standard Library Imports
import json
import logging
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlencode

from scraper import (AuthenticationError, BASEURL, buildRectWkt,
                     encryptPortalValue, isLoginHtml, parsePropertyDetailHtml)
from ulb_scraper.portal import PortalClient


def main():
    '''Check session, an unparsed detail page, and one failing tile per municipality.'''
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    home = Path('data/application')
    client = PortalClient(cookieFile=home / 'cookies.lwp', persist=False)
    connection = sqlite3.connect(f'file:{home / "state.sqlite3"}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        try:
            client.checkSession()
        except AuthenticationError:
            logging.warning('Saved session expired. Verify a new OTP in the app before live diagnostics.')
            return
        logging.info('Authenticated page check passed')
        sessionHtml = client.requestText('GET', '/SearchProperty/MyProperties', referer=BASEURL)
        sessionTitle = re.search(r'<title>(.*?)</title>', sessionHtml, re.DOTALL | re.IGNORECASE)
        logging.info(f'Authenticated page title={sessionTitle.group(1).strip() if sessionTitle else "none"} '
                     f'loginForm={isLoginHtml(sessionHtml)}')
        job = connection.execute("SELECT * FROM jobs WHERE colonyName='Jalauli'").fetchone()
        if job:
            row = connection.execute("SELECT raw FROM properties WHERE jobId=? AND detailState='pending' LIMIT 1", (job['id'],)).fetchone()
            if row:
                raw = json.loads(row['raw'])
                html = client.requestText('GET', '/RegisterProperty/NewApplication?' + urlencode({'token': raw['Token']}), referer=BASEURL)
                diagnosticFolder = Path('data/diagnostics')
                diagnosticFolder.mkdir(parents=True, exist_ok=True)
                (diagnosticFolder / 'jalauli_unparsed.html').write_text(html, encoding='utf-8')
                title = re.search(r'<title>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
                logging.info(f'Detail bytes={len(html)} loginForm={isLoginHtml(html)} '
                             f'title={title.group(1).strip() if title else "none"} '
                             f'parsedKeys={list(parsePropertyDetailHtml(html)["Detail"])}')
        for name in ('Lal Dora Ramra Gate', 'Sector 6'):
            job = connection.execute('SELECT * FROM jobs WHERE colonyName=?', (name,)).fetchone()
            if not job:
                continue
            tile = connection.execute("SELECT * FROM tiles WHERE jobId=? AND state='pending' ORDER BY id LIMIT 1", (job['id'],)).fetchone()
            if not tile:
                continue
            data = {'pageNo': '1', 'pageSize': '500', 'efkUlbId': encryptPortalValue(job['municipalityId']),
                    'PID': '', 'OwnerName': '', 'MobileNo': '', 'fkColonySurveyId': job['colonyId'],
                    'fkPropertyCategoryId': '', 'wkt': buildRectWkt(*json.loads(tile['bounds'])),
                    'searchType': '3', 'PIDNDC': '', 'PlotNo': ''}
            for method in ('POST', 'GET'):
                try:
                    url = '/SearchProperty/getSearchPropertiesData'
                    rows = client.requestJson(method, url if method == 'POST' else url + '?' + urlencode(data),
                                              data=data if method == 'POST' else None, referer=BASEURL)
                    logging.info(f'{name} {method}: response type={type(rows).__name__}, rows={len(rows)}')
                except Exception as exception:
                    logging.info(f'{name} {method}: {type(exception).__name__}: {exception}')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
