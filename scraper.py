#!/usr/bin/env python3

'''
CLI prototype for scraping ULB Haryana property map data.
'''

# Standard Library Imports
import argparse
import asyncio
from base64 import b64encode
import csv
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.cookiejar
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import logging
import math
from pathlib import Path
import re
import shutil
import sys
import time
from urllib import error, parse, request

BASEURL = 'https://property.ulbharyana.gov.in'
SESSIONFILE = Path('data/session_state.json')
COOKIEFILE = Path('data/http_cookies.lwp')
DATADIR = Path('data')
LOGDIR = Path('logs')
OUTPUTDIR = Path('output')
DEFAULTVIEWPORTLAT = 0.0006688
DEFAULTVIEWPORTLON = 0.0009401
DEFAULTOVERLAP = 0.5
DEFAULTPAGE_SIZE = 500
DEFAULTEMPTYRINGS = 2
DEFAULTBUFFERMETERS = 1000
AESKEY = b'5480808080808091'
IMPORTANTCOLUMNS = [
    'pkPropertyId',
    'PID',
    'PIDNDC',
    'Detail.OldPID',
    'McName',
    'fkULBId',
    'ColonyName',
    'fkColonySurveyId',
    'OwnerName',
    'Detail.OwnerNames',
    'Detail.MobileNoFull',
    'MobileNo',
    'Detail.MobileNumberLinkedWithThePID',
    'Address1',
    'Detail.Address',
    'PropertyCategory',
    'Detail.PropertyCategory',
    'PropertyType',
    'Detail.PropertyType',
    'PropertySubType',
    'Detail.PropertySubType',
    'PlotNo',
    'Detail.PlotNoHouseNo',
    'PlotSize',
    'Detail.PlotArea',
    'Unit',
    'Lat',
    'Long',
    'KhasaraNo',
    'AuthorityArea',
    'IsAuthorised',
    'Detail.OwnershipType',
    'Detail.OldOwnerOccupierName',
    'Detail.HaveAnyCourtCaseStayLegalProceeding',
    'ImageViewLink',
    'Token',
    'Detail.FetchStatus',
    'Detail.FetchError',
    'TotalRecord',
    'PageNo',
]
REFERENCECOLUMNS = [
    'IsIntegratedReset',
    'IsIntegratedVerified',
]
LOWSIGNALPREFIXES = (
    'DueDetails.',
    'Detail.Tables',
)


class PortalResponseError(RuntimeError):
    '''
    Raised when the portal returns an unexpected response shape.
    '''


class AuthenticationError(PortalResponseError):
    '''The portal requires a renewed authenticated session.'''


class PortalHttpError(PortalResponseError):
    '''An HTTP failure retaining its status and retry delay.'''

    def __init__(self, status, retryAfter=0):
        super().__init__(f'Portal HTTP {status}')
        self.status = status
        self.retryAfter = retryAfter


def isLoginHtml(html):
    '''Identify the OTP login form without matching shared page scripts.'''
    title = re.search(r'<title\b[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if title and re.fullmatch(r'\s*(?:(?:Citizen\s+)?Login|Session\s+Expired)\s*', unescape(title.group(1)), re.IGNORECASE):
        return True
    if re.search(r'(?:id|name)=[\"\']txtUser[\"\']', html, re.IGNORECASE) and re.search(
            r'(?:id|name)=[\"\']txtOTP[\"\']', html, re.IGNORECASE):
        return True
    return bool(re.search(
        r'<form\b[^>]*action=[\"\'][^\"\']*/Account/(?:Login|CheckOTP)|'
        r'<input\b[^>]*(?:id|name)=[\"\'](?:userName|phoneNumber|loginUsing)[\"\']',
        html, re.IGNORECASE)) and not re.search(
            r'(?:id|name)=[\"\']AsPerRecord_pkPropertyId[\"\']', html)


def retryAfterSeconds(value):
    '''Parse Retry-After as seconds or an HTTP date.'''
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            timestamp = parsedate_to_datetime(value)
            return max(0, (timestamp - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def setupLogging(command):
    '''
    Configure console and file logging for one scraper command.
    '''
    LOGDIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    logPath = LOGDIR / f'{timestamp}_{slugify(command or "scraper")}.log'

    rootLogger = logging.getLogger()
    rootLogger.handlers.clear()
    rootLogger.setLevel(logging.DEBUG)

    fileHandler = logging.FileHandler(logPath, encoding='utf-8')
    fileHandler.setLevel(logging.DEBUG)
    fileHandler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(funcName)s:%(lineno)d %(message)s'))

    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setLevel(logging.INFO)
    consoleHandler.setFormatter(logging.Formatter('%(message)s'))

    rootLogger.addHandler(fileHandler)
    rootLogger.addHandler(consoleHandler)
    logging.info(f'Log file: {logPath}')
    return logPath


def requireCryptoCipher():
    '''
    Import AES only when a command needs it.
    '''
    try:
        from Crypto.Cipher import AES
    except ImportError as error:
        raise RuntimeError(
            'pycryptodome is not installed. Run: pip install -r requirements.txt'
        ) from error
    return AES


def requireWorkbook():
    '''
    Import openpyxl only when exporting Excel.
    '''
    try:
        from openpyxl import Workbook
    except ImportError as error:
        raise RuntimeError('openpyxl is not installed. Run: pip install -r requirements.txt') from error
    return Workbook


def encryptPortalValue(value):
    '''
    Encrypt a portal value using the same AES-CBC helper as the website.
    '''
    AES = requireCryptoCipher()
    rawValue = str(value).encode('utf-8')
    padLength = 16 - (len(rawValue) % 16)
    rawValue += bytes([padLength]) * padLength
    cipher = AES.new(AESKEY, AES.MODE_CBC, AESKEY)
    return b64encode(cipher.encrypt(rawValue)).decode('ascii')


def makeCookie(name, value, domain):
    '''
    Create a cookiejar Cookie object.
    '''
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=False,
        path='/',
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={'HttpOnly': None},
        rfc2109=False,
    )


class PortalSession:
    '''
    Lightweight HTTP client for the ULB Haryana portal.
    '''

    def __init__(self, fresh=False, cookieFile=None, persist=True):
        self.cookieFile = Path(cookieFile or COOKIEFILE)
        self.persist = persist
        self.cookieJar = http.cookiejar.LWPCookieJar(str(self.cookieFile))
        if fresh:
            pass
        elif self.cookieFile.exists():
            self.cookieJar.load(ignore_discard=True, ignore_expires=True)
        elif cookieFile is None and SESSIONFILE.exists():
            self._importStorageStateCookies()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.cookieJar))

    def _importStorageStateCookies(self):
        '''
        Import cookies saved by an older browser-session login.
        '''
        state = loadJson(SESSIONFILE, {})
        for cookieInfo in state.get('cookies', []):
            if not cookieInfo.get('domain', '').endswith('property.ulbharyana.gov.in'):
                continue
            self.cookieJar.set_cookie(makeCookie(
                cookieInfo['name'], cookieInfo['value'], cookieInfo['domain']))
        self.save()

    def save(self):
        '''
        Persist cookies.
        '''
        if not self.persist:
            return
        self.cookieFile.parent.mkdir(parents=True, exist_ok=True)
        self.cookieJar.save(ignore_discard=True, ignore_expires=True)

    def _headers(self, referer=None, ajax=False, contentType=None):
        '''
        Build standard request headers.
        '''
        headers = {
            'Accept': 'application/json, text/javascript, */*; q=0.01' if ajax else '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        }
        if referer:
            headers['Referer'] = referer
        if ajax:
            headers['X-Requested-With'] = 'XMLHttpRequest'
            headers['Origin'] = BASEURL
        if contentType:
            headers['Content-Type'] = contentType
        return headers

    def requestText(self, method, url, data=None, referer=None, ajax=False):
        '''
        Make a request and return decoded text.
        '''
        fullUrl = url if url.startswith('http') else f'{BASEURL}{url}'
        body = None
        contentType = None
        if data is not None:
            body = parse.urlencode(data).encode('utf-8')
            contentType = 'application/x-www-form-urlencoded; charset=UTF-8'
        req = request.Request(
            fullUrl,
            data=body,
            headers=self._headers(referer=referer, ajax=ajax, contentType=contentType),
            method=method,
        )
        safeUrl = parse.urlsplit(fullUrl).path
        logging.debug(f'HTTP {method} {safeUrl} ajax={ajax}')
        if data is not None:
            safeData = dict(data)
            for field in ('efkUlbId', 'phoneNumber', 'userName', 'otp',
                          'MobileNo', '__RequestVerificationToken'):
                if field in safeData:
                    safeData[field] = '<redacted>'
            logging.debug(f'HTTP payload for {safeUrl}: {safeData}')
        try:
            with self.opener.open(req, timeout=45) as response:
                text = response.read().decode('utf-8', 'replace')
                responsePath = parse.urlsplit(response.geturl()).path.lower()
                if responsePath.startswith('/account/login') and not safeUrl.lower().startswith('/account/'):
                    raise AuthenticationError('Session expired; please verify a new OTP.')
                logging.debug(
                    f'HTTP {method} {safeUrl} status={response.getcode()} bytes={len(text)}')
        except error.HTTPError as httpError:
            httpError.close()
            if httpError.code == 401:
                raise AuthenticationError('Session expired; please verify a new OTP.') from httpError
            retryAfter = retryAfterSeconds(httpError.headers.get('Retry-After', '0'))
            raise PortalHttpError(httpError.code, retryAfter) from httpError
        except error.URLError as urlError:
            logging.debug(f'Could not reach {safeUrl}: {urlError.reason}')
            raise RuntimeError(f'Could not reach {safeUrl}: {urlError.reason}') from urlError
        self.save()
        return text

    def requestJson(self, method, url, data=None, referer=None, ajax=True):
        '''
        Make a request and parse JSON.
        '''
        text = self.requestText(method, url, data=data, referer=referer, ajax=ajax)
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            if isLoginHtml(text):
                raise AuthenticationError('Session expired; please verify a new OTP.') from error
            safeUrl = parse.urlsplit(url).path
            logging.error(f'Expected JSON from {safeUrl}, got non-JSON content')
            raise PortalResponseError(
                f'Expected JSON from {safeUrl}, got non-JSON response.'
            ) from error

    def hasValidSession(self):
        '''
        Return True if saved cookies can reach an authenticated page.
        '''
        try:
            text = self.requestText(
                'GET',
                '/SearchProperty/MyProperties',
                referer=BASEURL,
                ajax=False,
            )
        except Exception:
            return False
        return 'MyProperties' in text or 'getSearchPropertiesDataMyProperties' in text


@dataclass(frozen=True)
class Tile:
    '''
    Grid tile request metadata.
    '''
    gridX: int
    gridY: int
    emptyBudget: int


def slugify(value):
    '''
    Convert a human readable value into a filename-safe slug.
    '''
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', value.strip().lower())
    return slug.strip('_') or 'run'


def loadJson(path, defaultValue):
    '''
    Load a JSON file if present.
    '''
    if not path.exists():
        return defaultValue
    with path.open('r', encoding='utf-8') as fileObj:
        return json.load(fileObj)


def saveJson(path, data):
    '''
    Write JSON to disk.
    '''
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fileObj:
        json.dump(data, fileObj, indent=2, sort_keys=True)


def appendJsonl(path, data):
    '''
    Append one JSON object as a JSONL row.
    '''
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fileObj:
        fileObj.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
        fileObj.write('\n')


def saveQueue(queuePath, queue):
    '''
    Persist pending tile queue.
    '''
    saveJson(queuePath, [
        {'gridX': queuedTile.gridX, 'gridY': queuedTile.gridY, 'emptyBudget': queuedTile.emptyBudget}
        for queuedTile in queue
    ])


def readJsonl(path):
    '''
    Read all valid JSONL rows.
    '''
    if not path.exists():
        return []
    rows = []
    with path.open('r', encoding='utf-8') as fileObj:
        for line in fileObj:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parsePolygonWkt(wkt):
    '''
    Parse a simple POLYGON WKT into [(lon, lat), ...].
    '''
    match = re.search(r'POLYGON\s*\(\((.*)\)\)', wkt, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError(f'Unsupported WKT: {wkt[:80]}')
    points = []
    for token in match.group(1).split(','):
        parts = token.strip().split()
        if len(parts) < 2:
            continue
        points.append((float(parts[0]), float(parts[1])))
    if points and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        raise ValueError('Polygon has fewer than 3 points')
    return points


def polygonBounds(points):
    '''
    Return (minLon, minLat, maxLon, maxLat).
    '''
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return min(lons), min(lats), max(lons), max(lats)


def metersToLatDegrees(meters):
    '''
    Convert meters into approximate latitude degrees.
    '''
    return meters / 111320.0


def metersToLonDegrees(meters, lat):
    '''
    Convert meters into approximate longitude degrees at a latitude.
    '''
    return meters / (111320.0 * max(math.cos(math.radians(lat)), 0.01))


def pointInPolygon(lon, lat, polygon):
    '''
    Ray-casting point in polygon test.
    '''
    inside = False
    count = len(polygon)
    prevLon, prevLat = polygon[-1]
    for currLon, currLat in polygon:
        crosses = ((currLat > lat) != (prevLat > lat))
        if crosses:
            intersectLon = (prevLon - currLon) * (lat - currLat) / (prevLat - currLat) + currLon
            if lon < intersectLon:
                inside = not inside
        prevLon, prevLat = currLon, currLat
    return inside


def orientation(pointA, pointB, pointC):
    '''
    Return orientation sign for three points.
    '''
    value = ((pointB[1] - pointA[1]) * (pointC[0] - pointB[0]) -
             (pointB[0] - pointA[0]) * (pointC[1] - pointB[1]))
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else 2


def onSegment(pointA, pointB, pointC):
    '''
    Check whether pointB lies on pointA-pointC.
    '''
    return (min(pointA[0], pointC[0]) <= pointB[0] <= max(pointA[0], pointC[0]) and
            min(pointA[1], pointC[1]) <= pointB[1] <= max(pointA[1], pointC[1]))


def segmentsIntersect(pointA, pointB, pointC, pointD):
    '''
    Check whether two line segments intersect.
    '''
    orient1 = orientation(pointA, pointB, pointC)
    orient2 = orientation(pointA, pointB, pointD)
    orient3 = orientation(pointC, pointD, pointA)
    orient4 = orientation(pointC, pointD, pointB)

    if orient1 != orient2 and orient3 != orient4:
        return True
    if not orient1 and onSegment(pointA, pointC, pointB):
        return True
    if not orient2 and onSegment(pointA, pointD, pointB):
        return True
    if not orient3 and onSegment(pointC, pointA, pointD):
        return True
    if not orient4 and onSegment(pointC, pointB, pointD):
        return True
    return False


def rectIntersectsPolygon(west, south, east, north, polygon):
    '''
    Return True when a rectangle intersects a polygon.
    '''
    rectCorners = [(west, south), (east, south), (east, north), (west, north)]
    for lon, lat in rectCorners:
        if pointInPolygon(lon, lat, polygon):
            return True
    for lon, lat in polygon:
        if west <= lon <= east and south <= lat <= north:
            return True

    rectEdges = list(zip(rectCorners, rectCorners[1:] + rectCorners[:1]))
    polygonEdges = list(zip(polygon, polygon[1:] + polygon[:1]))
    for rectStart, rectEnd in rectEdges:
        for polygonStart, polygonEnd in polygonEdges:
            if segmentsIntersect(rectStart, rectEnd, polygonStart, polygonEnd):
                return True
    return False


def buildRectWkt(west, south, east, north):
    '''
    Build a rectangle WKT matching the portal map request style.
    '''
    return (f'POLYGON(({east} {north}, {west} {north},'
            f'{west} {south}, {east} {south}, {east} {north}))')


def flattenDict(data, prefix=''):
    '''
    Flatten nested dictionaries for CSV/XLSX export.
    '''
    flatData = {}
    for key, value in data.items():
        flatKey = f'{prefix}{key}' if not prefix else f'{prefix}.{key}'
        if isinstance(value, dict):
            flatData.update(flattenDict(value, flatKey))
        elif isinstance(value, list):
            if all(not isinstance(item, (dict, list)) for item in value):
                flatData[flatKey] = joinUniqueValues(value)
            else:
                flatData[flatKey] = json.dumps(value, ensure_ascii=False)
        else:
            flatData[flatKey] = value
    return flatData


class TextCollector(HTMLParser):
    '''
    Collect visible-ish text from an HTML fragment.
    '''

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        '''
        Capture text chunks.
        '''
        if data:
            self.parts.append(data)

    def getText(self):
        '''
        Return normalized text.
        '''
        return cleanText(' '.join(self.parts))


class InputAndImageCollector(HTMLParser):
    '''
    Extract input attributes and image links from a detail page.
    '''

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        '''
        Capture selected start tags.
        '''
        attrMap = dict(attrs)
        if tag.lower() == 'input':
            self.inputs.append(attrMap)
        elif tag.lower() == 'img' and attrMap.get('src'):
            self.images.append(attrMap['src'])


class TableCollector(HTMLParser):
    '''
    Extract table cell text while preserving row boundaries.
    '''

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.tableDepth = 0
        self.currentRows = None
        self.currentRow = None
        self.currentCell = None
        self.cellDepth = 0

    def handle_starttag(self, tag, attrs):
        '''
        Track table, row, and cell starts.
        '''
        tag = tag.lower()
        if tag == 'table':
            if not self.tableDepth:
                self.currentRows = []
            self.tableDepth += 1
        elif tag == 'tr' and self.tableDepth:
            self.currentRow = []
        elif tag in ('td', 'th') and self.tableDepth and self.currentRow is not None:
            self.currentCell = []
            self.cellDepth += 1

    def handle_endtag(self, tag):
        '''
        Track table, row, and cell ends.
        '''
        tag = tag.lower()
        if tag in ('td', 'th') and self.tableDepth and self.currentCell is not None:
            self.currentRow.append(cleanText(' '.join(self.currentCell)))
            self.currentCell = None
            self.cellDepth = max(self.cellDepth - 1, 0)
        elif tag == 'tr' and self.tableDepth and self.currentRow is not None:
            if any(cell for cell in self.currentRow):
                self.currentRows.append(self.currentRow)
            self.currentRow = None
        elif tag == 'table' and self.tableDepth:
            self.tableDepth -= 1
            if not self.tableDepth and self.currentRows:
                self.tables.append(self.currentRows)
                self.currentRows = None

    def handle_data(self, data):
        '''
        Capture text inside table cells.
        '''
        if self.currentCell is not None and data:
            self.currentCell.append(data)


def cleanText(value):
    '''
    Normalize whitespace in scraped text.
    '''
    if value is None:
        return ''
    text = unescape(str(value))
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def joinUniqueValues(values):
    '''
    Join non-empty values into one comma-separated cell.
    '''
    uniqueValues = []
    seenValues = set()
    for value in values:
        text = cleanText(value)
        if not text or text in seenValues:
            continue
        uniqueValues.append(text)
        seenValues.add(text)
    return ', '.join(uniqueValues)


def htmlToText(html):
    '''
    Convert an HTML fragment to normalized text.
    '''
    parser = TextCollector()
    parser.feed(html)
    return parser.getText()


def detailKey(label):
    '''
    Convert a detail-page label into a stable export key.
    '''
    key = cleanText(label).rstrip(':')
    key = key.replace('/', ' ')
    key = re.sub(r'[^a-zA-Z0-9]+', ' ', key).strip()
    if not key:
        return ''
    return ''.join(part[:1].upper() + part[1:] for part in key.split())


def collectInputsAndImages(html):
    '''
    Return input attributes and image links from HTML.
    '''
    parser = InputAndImageCollector()
    parser.feed(html)
    return parser.inputs, parser.images


def collectTables(html):
    '''
    Return all tables from a detail page.
    '''
    parser = TableCollector()
    parser.feed(html)
    return parser.tables


def extractDetailPairs(html):
    '''
    Extract label/value pairs rendered with span.det.
    '''
    detailInfo = {}
    pattern = re.compile(
        r'<span[^>]*class="[^"]*font-weight-bold[^"]*\ben\b[^"]*"[^>]*>'
        r'(.*?)</span>.*?<span[^>]*class="[^"]*\bdet\b[^"]*"[^>]*>(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    )
    for labelHtml, valueHtml in pattern.findall(html):
        key = detailKey(htmlToText(labelHtml))
        value = htmlToText(valueHtml)
        if not key or not value:
            continue
        if key in detailInfo:
            detailInfo[key] = joinUniqueValues([detailInfo[key], value])
        else:
            detailInfo[key] = value
    return detailInfo


def extractOwnerDetails(html):
    '''
    Extract displayed owner rows without collecting hidden personal values.
    '''
    from ulb_scraper.property_data import parsePropertyPage
    return parsePropertyPage(html)['Detail']['OwnerRows']


def parsePropertyDetailHtml(html):
    '''
    Parse a RegisterProperty/NewApplication page into export-friendly fields.
    '''
    from ulb_scraper.property_data import parsePropertyPage
    return parsePropertyPage(html)


def normalizeCellValue(value):
    '''
    Convert empty values to dash for export.
    '''
    if value is None or value == '':
        return '-'
    return value


def buildRunDir(municipalityName, colonyName):
    '''
    Build the checkpoint directory path for a municipality/colony run.
    '''
    return DATADIR / f'{slugify(municipalityName)}__{slugify(colonyName)}'


async def loginWithOtp(phoneNumber):
    '''
    Login with mobile OTP and save HTTP cookies.
    '''
    portalSession = PortalSession(fresh=True)
    logging.info('Opening login page...')
    homeHtml = portalSession.requestText('GET', '/', referer=BASEURL)
    tokenMatch = re.search(
        r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"', homeHtml)
    if not tokenMatch:
        tokenMatch = re.search(
            r'type="hidden"\s+value="([^"]+)"\s+name="__RequestVerificationToken"', homeHtml)
    if not tokenMatch:
        raise RuntimeError('Could not find __RequestVerificationToken on login page')
    verificationToken = unescape(tokenMatch.group(1))

    otpRequestData = {
        'userName': encryptPortalValue(phoneNumber),
        'userType': 'Citizen',
        'oTPVerificationType': 'Tx3MZR/MXi84+8I/P/PXeA==',
        'loginUsing': 'Mobile',
        'fkulbId': '',
        '__RequestVerificationToken': verificationToken,
    }
    otpResponse = portalSession.requestJson(
        'POST', '/Account/IsUserExist', data=otpRequestData, referer=BASEURL, ajax=True)
    if otpResponse.get('Type') != 'Success':
        raise RuntimeError(f'OTP request failed: {otpResponse}')

    otp = input('Enter OTP received on phone: ').strip()
    verifyData = {
        'phoneNumber': phoneNumber,
        'otp': otp,
        'userName': phoneNumber,
        'otpType': '58qi6Nxt6OtVinoAj/78gg==',
        'userType': 'Citizen',
        'ReturnUrl': '',
        'loginUsing': 'Mobile',
    }
    logging.info('Verifying OTP...')
    verifyResponse = portalSession.requestJson(
        'POST', '/Account/CheckOTP', data=verifyData, referer=BASEURL, ajax=True)
    if not verifyResponse.get('UrlString'):
        raise RuntimeError(f'OTP verification failed: {verifyResponse}')

    portalSession.requestText('GET', verifyResponse['UrlString'], referer=BASEURL)
    portalSession.save()
    logging.info(f'Session cookies saved to {COOKIEFILE}')


def tileBounds(tile, originLon, originLat, stepLon, stepLat, viewportLon, viewportLat):
    '''
    Return rectangle bounds for a tile.
    '''
    centerLon = originLon + tile.gridX * stepLon
    centerLat = originLat + tile.gridY * stepLat
    west = centerLon - viewportLon / 2
    east = centerLon + viewportLon / 2
    south = centerLat - viewportLat / 2
    north = centerLat + viewportLat / 2
    return west, south, east, north


def buildSeedTiles(polygon, viewportLon, viewportLat, stepLon, stepLat, bufferMeters):
    '''
    Build initial polygon-intersecting tiles and scan bounds.
    '''
    minLon, minLat, maxLon, maxLat = polygonBounds(polygon)
    midLat = (minLat + maxLat) / 2
    bufferLat = metersToLatDegrees(bufferMeters)
    bufferLon = metersToLonDegrees(bufferMeters, midLat)

    originLon = minLon - viewportLon
    originLat = minLat - viewportLat
    minX = math.floor((minLon - originLon - viewportLon) / stepLon)
    maxX = math.ceil((maxLon - originLon + viewportLon) / stepLon)
    minY = math.floor((minLat - originLat - viewportLat) / stepLat)
    maxY = math.ceil((maxLat - originLat + viewportLat) / stepLat)

    seeds = []
    for gridX in range(minX, maxX + 1):
        for gridY in range(minY, maxY + 1):
            tile = Tile(gridX, gridY, 0)
            west, south, east, north = tileBounds(
                tile, originLon, originLat, stepLon, stepLat, viewportLon, viewportLat)
            if rectIntersectsPolygon(west, south, east, north, polygon):
                seeds.append(tile)

    scanBounds = {
        'minLon': minLon - bufferLon,
        'maxLon': maxLon + bufferLon,
        'minLat': minLat - bufferLat,
        'maxLat': maxLat + bufferLat,
    }
    return seeds, originLon, originLat, scanBounds


def insideScanBounds(tile, originLon, originLat, stepLon, stepLat, scanBounds):
    '''
    Check whether tile center is inside configured scan bounds.
    '''
    centerLon = originLon + tile.gridX * stepLon
    centerLat = originLat + tile.gridY * stepLat
    return (scanBounds['minLon'] <= centerLon <= scanBounds['maxLon'] and
            scanBounds['minLat'] <= centerLat <= scanBounds['maxLat'])


def tileKey(tile):
    '''
    Stable tile key.
    '''
    return f'{tile.gridX}:{tile.gridY}'


def queueNeighbors(queue, queuedKeys, tile, budget, originLon, originLat, stepLon, stepLat, scanBounds):
    '''
    Add neighbor tiles to queue.
    '''
    for deltaX in (-1, 0, 1):
        for deltaY in (-1, 0, 1):
            if not deltaX and not deltaY:
                continue
            nextTile = Tile(tile.gridX + deltaX, tile.gridY + deltaY, budget)
            nextKey = tileKey(nextTile)
            if nextKey in queuedKeys:
                continue
            if not insideScanBounds(nextTile, originLon, originLat, stepLon, stepLat, scanBounds):
                continue
            queue.append(nextTile)
            queuedKeys.add(nextKey)


def propertyIdentity(propertyInfo):
    '''
    Return stable property identity.
    '''
    return str(propertyInfo.get('pkPropertyId') or propertyInfo.get('PID') or propertyInfo.get('PIDNDC'))


def normalizeName(value):
    '''
    Normalize a portal name for loose comparisons.
    '''
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def propertyInPolygon(propertyInfo, polygon):
    '''
    Return True when a property coordinate is inside the target polygon.
    '''
    try:
        lon = float(propertyInfo.get('Long'))
        lat = float(propertyInfo.get('Lat'))
    except (TypeError, ValueError):
        return False
    return pointInPolygon(lon, lat, polygon)


def shouldKeepProperty(propertyInfo, polygon, colonyName, filterMode):
    '''
    Return True if a property belongs in the current scrape output.
    '''
    if filterMode == 'all':
        return True
    if filterMode == 'colony':
        return normalizeName(propertyInfo.get('ColonyName')) == normalizeName(colonyName)
    return propertyInPolygon(propertyInfo, polygon)


def detailUrlForProperty(propertyInfo):
    '''
    Return detail-page URL path for a property.
    '''
    token = propertyInfo.get('Token')
    if not token:
        return None
    query = parse.urlencode({'token': token})
    return f'/RegisterProperty/NewApplication?{query}'


def loadDetailRows(runDir):
    '''
    Load existing detail checkpoint rows by property identity.
    '''
    detailRows = {}
    for detailInfo in readJsonl(runDir / 'details.jsonl'):
        identity = detailInfo.get('propertyIdentity')
        if identity:
            detailRows[identity] = detailInfo
    return detailRows


def saveDetailHtml(runDir, identity, html):
    '''
    Save raw detail HTML for debugging parser coverage.
    '''
    htmlDir = runDir / 'details_html'
    htmlDir.mkdir(parents=True, exist_ok=True)
    htmlPath = htmlDir / f'{slugify(identity)}.html'
    with htmlPath.open('w', encoding='utf-8') as fileObj:
        fileObj.write(html)


def fetchDetailForProperty(portalSession, runDir, propertyInfo, saveHtml=False):
    '''
    Fetch and parse one property detail page.
    '''
    identity = propertyIdentity(propertyInfo)
    detailUrl = detailUrlForProperty(propertyInfo)
    if not detailUrl:
        return {
            'propertyIdentity': identity,
            'status':           'no-token',
            'detail':           {'Detail': {'FetchStatus': 'no-token'}},
        }

    html = portalSession.requestText(
        'GET',
        detailUrl,
        referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap',
        ajax=False,
    )
    if 'Account/Login' in html or 'Login' in html[:1000]:
        raise RuntimeError('Detail page returned login content; session may be expired')
    if saveHtml:
        saveDetailHtml(runDir, identity, html)

    detailData = parsePropertyDetailHtml(html)
    detailData['Detail']['FetchStatus'] = 'ok'
    detailData['Detail']['SourceUrl'] = f'{BASEURL}{detailUrl}'
    return {
        'propertyIdentity': identity,
        'status':           'ok',
        'detail':           detailData,
    }


def fetchMissingDetails(portalSession, runDir, args):
    '''
    Fetch detail pages for properties that do not have detail checkpoints.
    '''
    properties = readJsonl(runDir / 'properties.jsonl')
    existingDetails = loadDetailRows(runDir)
    detailsPath = runDir / 'details.jsonl'
    completedStatuses = {'ok', 'no-token'}
    pendingProperties = [
        propertyInfo for propertyInfo in properties
        if existingDetails.get(propertyIdentity(propertyInfo), {}).get('status') not in completedStatuses
    ]
    if not pendingProperties:
        return

    logging.info(f'Fetching detail pages for {len(pendingProperties)} properties')
    for index, propertyInfo in enumerate(pendingProperties, start=1):
        identity = propertyIdentity(propertyInfo)
        try:
            detailInfo = fetchDetailForProperty(
                portalSession, runDir, propertyInfo, saveHtml=args.save_detail_html)
        except Exception as error:
            detailInfo = {
                'propertyIdentity': identity,
                'status':           'error',
                'detail':           {
                    'Detail': {
                        'FetchStatus': 'error',
                        'FetchError':  str(error),
                    },
                },
            }
            logging.error(f'ERROR detail {identity}: {error}')

        appendJsonl(detailsPath, detailInfo)
        logging.info(
            f'detail={identity} status={detailInfo["status"]} '
            f'done={index}/{len(pendingProperties)}')
        if args.detail_delay_ms:
            time.sleep(args.detail_delay_ms / 1000)


def mergeRowsWithDetails(runDir, rows):
    '''
    Merge raw map rows with parsed detail-page checkpoints.
    '''
    detailRows = loadDetailRows(runDir)
    mergedRows = []
    for row in rows:
        mergedRow = dict(row)
        detailInfo = detailRows.get(propertyIdentity(row), {})
        detailData = detailInfo.get('detail')
        if detailData:
            mergedRow.update(detailData)
        mergedRows.append(mergedRow)
    return mergedRows


def isZeroLike(value):
    '''
    Return True for empty or zero-ish values.
    '''
    if value is None:
        return True
    text = cleanText(value)
    if text in ('', '-'):
        return True
    try:
        return float(text) == 0
    except ValueError:
        return False


def isLowSignalColumn(column, flatRows):
    '''
    Return True when a column should be pushed to the end of the export.
    '''
    if column.startswith(LOWSIGNALPREFIXES):
        return True
    values = [row.get(column) for row in flatRows]
    return values and all(isZeroLike(value) for value in values)


def orderExportColumns(flatRows):
    '''
    Return export columns with important columns first and noisy columns last.
    '''
    availableColumns = {key for row in flatRows for key in row.keys()}
    availableColumns.update(REFERENCECOLUMNS)

    importantColumns = [column for column in IMPORTANTCOLUMNS if column in availableColumns]
    referenceColumns = [
        column for column in REFERENCECOLUMNS
        if column in availableColumns and column not in importantColumns
    ]
    remainingColumns = sorted(
        column for column in availableColumns
        if column not in importantColumns and column not in referenceColumns
    )
    usefulColumns = [
        column for column in remainingColumns
        if not isLowSignalColumn(column, flatRows)
    ]
    lowSignalColumns = [
        column for column in remainingColumns
        if isLowSignalColumn(column, flatRows)
    ]
    return importantColumns + referenceColumns + usefulColumns + lowSignalColumns


async def scrapeColony(args):
    '''
    Scrape one municipality/colony using adaptive map grid scan.
    '''
    municipalityName = args.municipality
    colonyName = args.colony
    runDir = buildRunDir(municipalityName, colonyName)
    if args.reset and runDir.exists():
        shutil.rmtree(runDir)

    runDir.mkdir(parents=True, exist_ok=True)
    propertiesPath = runDir / 'properties.jsonl'
    tilesPath = runDir / 'tiles.jsonl'
    queuePath = runDir / 'queue.json'
    metaPath = runDir / 'meta.json'

    if not COOKIEFILE.exists() and not SESSIONFILE.exists():
        if not args.phone:
            raise RuntimeError('No saved cookies found. Run login first or pass --phone.')
        await loginWithOtp(args.phone)

    portalSession = PortalSession()
    if not portalSession.hasValidSession():
        if args.phone:
            logging.info('Saved session is expired; logging in again.')
            await loginWithOtp(args.phone)
            portalSession = PortalSession()
        else:
            raise RuntimeError('Saved session is expired. Run: python scraper.py login --phone YOUR_MOBILE_NUMBER')

    if args.ulb_id:
        ulbInfo = {'Text': municipalityName, 'Value': str(args.ulb_id)}
    else:
        ulbList = portalSession.requestJson('GET', '/Widget/GetULBListPropertyWise', referer=BASEURL)
        targetName = municipalityName.strip().lower()
        matchingUlbs = [ulb for ulb in ulbList if ulb['Text'].strip().lower() == targetName]
        if len(matchingUlbs) != 1:
            raise RuntimeError(f'Municipality not found uniquely: {municipalityName}')
        ulbInfo = matchingUlbs[0]

    if args.colony_id:
        colonyInfo = {'Text': colonyName, 'Value': str(args.colony_id)}
    else:
        query = parse.urlencode({'fkULBId': ulbInfo['Value']})
        colonyList = portalSession.requestJson(
            'GET', f'/Widget/GetULBColonySurvey?{query}', referer=BASEURL)
        targetName = colonyName.strip().lower()
        matchingColonies = [colony for colony in colonyList if colony['Text'].strip().lower() == targetName]
        if len(matchingColonies) != 1:
            raise RuntimeError(f'Colony not found uniquely: {colonyName}')
        colonyInfo = matchingColonies[0]

    ulbId = str(ulbInfo['Value'])
    colonyId = str(colonyInfo['Value'])
    encryptedId = encryptPortalValue(ulbId)

    logging.info(
        f'Using municipality {ulbInfo["Text"]} ({ulbId}), '
        f'colony {colonyInfo["Text"]} ({colonyId})')

    query = parse.urlencode({'fkColonySurveyId': colonyId})
    boundaryInfo = portalSession.requestJson(
        'GET', f'/Widget/GetColonyBoundry?{query}', referer=f'{BASEURL}/Home/Searchproperty')
    polygon = parsePolygonWkt(boundaryInfo['Wkt'])

    viewportLon = args.viewport_lon
    viewportLat = args.viewport_lat
    stepLon = viewportLon * (1 - args.overlap)
    stepLat = viewportLat * (1 - args.overlap)
    seeds, originLon, originLat, scanBounds = buildSeedTiles(
        polygon, viewportLon, viewportLat, stepLon, stepLat, args.buffer_meters)

    metaData = {
        'municipalityName': ulbInfo['Text'],
        'municipalityId': ulbId,
        'colonyName': colonyInfo['Text'],
        'colonyId': colonyId,
        'boundary': boundaryInfo,
        'viewportLon': viewportLon,
        'viewportLat': viewportLat,
        'stepLon': stepLon,
        'stepLat': stepLat,
        'overlap': args.overlap,
        'bufferMeters': args.buffer_meters,
        'originLon': originLon,
        'originLat': originLat,
        'scanBounds': scanBounds,
        'startedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    saveJson(metaPath, metaData)

    existingProperties = readJsonl(propertiesPath)
    seenProperties = {propertyIdentity(propertyInfo) for propertyInfo in existingProperties}
    visitedTiles = {
        row['tileKey'] for row in readJsonl(tilesPath)
        if row.get('status') == 'ok'
    }

    pendingRows = loadJson(queuePath, None)
    if pendingRows is None:
        queue = deque(seeds)
    else:
        queue = deque(Tile(row['gridX'], row['gridY'], row['emptyBudget']) for row in pendingRows)

    queuedKeys = {tileKey(tile) for tile in queue}
    logging.info(
        f'Seed tiles: {len(seeds)}, pending tiles: {len(queue)}, '
        f'existing properties: {len(seenProperties)}')

    processedCount = 0
    while queue:
        tile = queue.popleft()
        currentKey = tileKey(tile)
        queuedKeys.discard(currentKey)
        if currentKey in visitedTiles:
            continue

        west, south, east, north = tileBounds(
            tile, originLon, originLat, stepLon, stepLat, viewportLon, viewportLat)
        wkt = buildRectWkt(west, south, east, north)

        requestData = {
            'pageNo': '1',
            'pageSize': str(args.page_size),
            'efkUlbId': encryptedId,
            'PID': '',
            'OwnerName': '',
            'MobileNo': '',
            'fkColonySurveyId': colonyId,
            'fkPropertyCategoryId': '',
            'wkt': wkt,
            'searchType': '3',
            'PIDNDC': '',
            'PlotNo': '',
        }

        try:
            responseRows = portalSession.requestJson(
                'POST',
                '/SearchProperty/getSearchPropertiesData',
                data=requestData,
                referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap',
                ajax=True,
            )
            status = 'ok'
        except Exception as error:
            logging.error(f'ERROR tile {currentKey}: {error}')
            queue.appendleft(tile)
            queuedKeys.add(currentKey)
            saveQueue(queuePath, queue)
            raise RuntimeError(
                f'Tile {currentKey} failed and was not marked complete. '
                'Run login again if the log shows an HTML/login response, then resume '
                'without --reset. For the current bad run, use --reset to start fresh.'
            ) from error

        acceptedRows = [
            propertyInfo for propertyInfo in responseRows
            if shouldKeepProperty(propertyInfo, polygon, colonyInfo['Text'], args.filter_mode)
        ]
        rejectedCount = len(responseRows) - len(acceptedRows)
        if rejectedCount:
            logging.debug(
                f'tile={currentKey} rejected {rejectedCount} out-of-scope rows '
                f'with filter={args.filter_mode}')

        newCount = 0
        for propertyInfo in acceptedRows:
            identity = propertyIdentity(propertyInfo)
            if not identity or identity in seenProperties:
                continue
            seenProperties.add(identity)
            appendJsonl(propertiesPath, propertyInfo)
            newCount += 1

        tileInfo = {
            'tileKey': currentKey,
            'gridX': tile.gridX,
            'gridY': tile.gridY,
            'emptyBudget': tile.emptyBudget,
            'status': status,
            'responseCount': len(responseRows),
            'acceptedCount': len(acceptedRows),
            'rejectedCount': rejectedCount,
            'newCount': newCount,
            'west': west,
            'south': south,
            'east': east,
            'north': north,
            'wkt': wkt,
        }
        appendJsonl(tilesPath, tileInfo)
        visitedTiles.add(currentKey)

        if len(responseRows) >= args.page_size:
            logging.warning(
                f'WARNING tile {currentKey} hit page size cap '
                f'{args.page_size}; reduce viewport size here later.')

        if acceptedRows:
            queueNeighbors(queue, queuedKeys, tile, args.empty_rings,
                           originLon, originLat, stepLon, stepLat, scanBounds)
        elif tile.emptyBudget > 0:
            queueNeighbors(queue, queuedKeys, tile, tile.emptyBudget - 1,
                           originLon, originLat, stepLon, stepLat, scanBounds)

        saveQueue(queuePath, queue)

        processedCount += 1
        logging.info(
            f'tile={currentKey} rows={len(responseRows)} accepted={len(acceptedRows)} '
            f'new={newCount} unique={len(seenProperties)} pending={len(queue)}')
        if args.max_tiles and processedCount >= args.max_tiles:
            logging.info(f'Stopping after --max-tiles {args.max_tiles}')
            break

        if args.delay_ms:
            time.sleep(args.delay_ms / 1000)

    if args.fetch_details:
        fetchMissingDetails(portalSession, runDir, args)

    exportRows(runDir, municipalityName, colonyName)
    logging.info(f'Done. Unique properties: {len(readJsonl(propertiesPath))}. Run data: {runDir}')


def exportRows(runDir, municipalityName, colonyName):
    '''
    Export deduped property JSONL into CSV and XLSX.
    '''
    rows = readJsonl(runDir / 'properties.jsonl')
    if not rows:
        return

    Workbook = requireWorkbook()
    mergedRows = mergeRowsWithDetails(runDir, rows)
    flatRows = [flattenDict(row) for row in mergedRows]
    columns = orderExportColumns(flatRows)
    outputSlug = f'{slugify(municipalityName)}__{slugify(colonyName)}'
    OUTPUTDIR.mkdir(parents=True, exist_ok=True)
    csvPath = OUTPUTDIR / f'{outputSlug}.csv'
    xlsxPath = OUTPUTDIR / f'{outputSlug}.xlsx'

    with csvPath.open('w', newline='', encoding='utf-8-sig') as fileObj:
        writer = csv.DictWriter(fileObj, fieldnames=columns)
        writer.writeheader()
        for row in flatRows:
            writer.writerow({column: normalizeCellValue(row.get(column)) for column in columns})

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('properties')
    sheet.freeze_panes = 'A2'
    sheet.append(columns)
    for row in flatRows:
        sheet.append([normalizeCellValue(row.get(column)) for column in columns])
    workbook.save(xlsxPath)
    logging.info(f'Exported {csvPath} and {xlsxPath}')


def buildParser():
    '''
    Build CLI argument parser.
    '''
    parser = argparse.ArgumentParser(description='ULB Haryana property scraper prototype')
    subparsers = parser.add_subparsers(dest='command', required=True)

    loginParser = subparsers.add_parser('login', help='Login with mobile OTP and save session')
    loginParser.add_argument('--phone', required=True, help='Mobile number for OTP login')

    exportParser = subparsers.add_parser('export', help='Export an existing demo checkpoint without scraping')
    exportParser.add_argument('--municipality', default='PANCHKULA')
    exportParser.add_argument('--colony', default='Sector 6')

    scrapeParser = subparsers.add_parser('scrape-demo', help='Scrape Panchkula Sector 6 demo')
    scrapeParser.add_argument('--phone', help='Mobile number; used only when no saved session exists')
    scrapeParser.add_argument('--municipality', default='PANCHKULA')
    scrapeParser.add_argument('--colony', default='Sector 6')
    scrapeParser.add_argument('--ulb-id', type=int, default=60)
    scrapeParser.add_argument('--colony-id', type=int, default=18949)
    scrapeParser.add_argument('--reset', action='store_true', help='Delete existing checkpoint for this run')
    scrapeParser.add_argument('--viewport-lon', type=float, default=DEFAULTVIEWPORTLON)
    scrapeParser.add_argument('--viewport-lat', type=float, default=DEFAULTVIEWPORTLAT)
    scrapeParser.add_argument('--overlap', type=float, default=DEFAULTOVERLAP)
    scrapeParser.add_argument('--empty-rings', type=int, default=DEFAULTEMPTYRINGS)
    scrapeParser.add_argument('--buffer-meters', type=int, default=DEFAULTBUFFERMETERS)
    scrapeParser.add_argument(
        '--filter-mode',
        choices=('polygon', 'colony', 'all'),
        default='polygon',
        help='Which marker rows to keep and use for frontier expansion',
    )
    scrapeParser.add_argument('--page-size', type=int, default=DEFAULTPAGE_SIZE)
    scrapeParser.add_argument('--delay-ms', type=int, default=750)
    scrapeParser.add_argument(
        '--fetch-details',
        action='store_true',
        help='Fetch and parse detail pages using each property Token',
    )
    scrapeParser.add_argument(
        '--detail-delay-ms',
        type=int,
        default=500,
        help='Delay between detail-page requests',
    )
    scrapeParser.add_argument(
        '--save-detail-html',
        action='store_true',
        help='Save raw detail HTML under the run checkpoint directory',
    )
    scrapeParser.add_argument('--max-tiles', type=int, default=0, help='Optional debug limit')

    return parser


async def asyncMain():
    '''
    Async CLI entry point.
    '''
    parser = buildParser()
    args = parser.parse_args()
    setupLogging(args.command)
    safeArgs = dict(vars(args))
    if 'phone' in safeArgs:
        safeArgs['phone'] = '<redacted>'
    logging.debug(f'CLI args: {safeArgs}')
    if args.command == 'login':
        await loginWithOtp(args.phone)
    elif args.command == 'scrape-demo':
        await scrapeColony(args)
    elif args.command == 'export':
        runDir = buildRunDir(args.municipality, args.colony)
        if not (runDir / 'properties.jsonl').exists():
            raise RuntimeError(f'No property checkpoint found at {runDir}')
        exportRows(runDir, args.municipality, args.colony)


def main():
    '''
    CLI entry point.
    '''
    try:
        asyncio.run(asyncMain())
    except KeyboardInterrupt:
        logging.info('Interrupted. Resume by running the same scrape command without --reset.')
    except Exception as error:
        logging.debug('Unhandled exception', exc_info=True)
        logging.error(f'ERROR: {error}')
        sys.exit(1)


if __name__ == '__main__':
    main()
