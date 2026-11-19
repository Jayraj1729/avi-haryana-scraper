'''OTP challenges, authenticated catalog APIs, and property photo downloads.'''

# Standard Library Imports
import copy
import hashlib
import json
import logging
from pathlib import Path
import re
from urllib import error, parse, request

from scraper import (AuthenticationError, BASEURL, collectInputsAndImages,
                     encryptPortalValue, isLoginHtml, parsePropertyDetailHtml,
                     PortalHttpError, PortalResponseError, PortalSession,
                     retryAfterSeconds)
from ulb_scraper.property_data import labelKey, publicViewRestricted, responseSchema, restrictedPage


def propertyPhotoUrls(propertyInfo, detail=None):
    '''
    Collect property photographs while excluding site logos and avatars.

    Inputs:    <propertyInfo>  -  Map API record
               [detail]        -  Parsed property details

    Output:    Deduplicated list of absolute HTTP image URLs
    '''
    logging.debug('Collecting property photographs')
    links = propertyInfo.get('ImageViewLink') or []
    if isinstance(links, str):
        links = re.findall(r'https?://[^<>"\r\n]+?(?=,\s*https?://|$)', links) or [links]
    links = list(links) + (detail or {}).get('Detail', {}).get('ImageLinks', [])
    urls = []
    for link in links:
        url = parse.urljoin(BASEURL, str(link).strip())
        parts = parse.urlsplit(url)
        if parts.scheme not in ('https', 'http') or '/content/' in parts.path.lower():
            continue
        if url not in urls:
            urls.append(url)
    return urls


class PortalClient(PortalSession):
    '''HTTP-only client with one cookie jar and opener per worker thread.'''

    def clone(self):
        '''Copy authenticated cookies into an independent worker client. Inputs: None. Output: client.'''
        logging.debug('Creating independent HTTP worker client')
        client = PortalClient(cookieFile=self.cookieFile, fresh=True, persist=False)
        for cookie in self.cookieJar:
            client.cookieJar.set_cookie(copy.copy(cookie))
        return client

    def generateOtp(self, phone):
        '''
        Generate an OTP using the login page's anti-forgery token.

        Inputs:    <phone>  -  Ten-digit mobile number

        Output:    None; challenge cookies remain on this client
        '''
        logging.info('Generating login OTP')
        if not re.fullmatch(r'[6-9][0-9]{9}', phone):
            raise ValueError('Enter a valid ten-digit mobile number')
        html = self.requestText('GET', '/', referer=BASEURL)
        inputs, unusedImages = collectInputsAndImages(html)
        token = next((item.get('value') for item in inputs
                      if item.get('name') == '__RequestVerificationToken'), '')
        if not token:
            raise PortalResponseError('Login page is missing its verification token')
        response = self.requestJson('POST', '/Account/IsUserExist', data={
            'userName':                 encryptPortalValue(phone),
            'userType':                 'Citizen',
            'oTPVerificationType':      'Tx3MZR/MXi84+8I/P/PXeA==',
            'loginUsing':               'Mobile',
            'fkulbId':                  '',
            '__RequestVerificationToken': token,
        }, referer=BASEURL)
        if response.get('Type') != 'Success':
            raise PortalResponseError('OTP could not be sent. Check the mobile number and try again.')
        self.phone = phone

    def verifyOtp(self, otp):
        '''
        Verify the challenge and follow the server's authenticated redirect.

        Inputs:    <otp>  -  OTP received on the mobile number

        Output:    None; raises on invalid credentials or unverified session
        '''
        logging.info('Verifying login OTP')
        if not re.fullmatch(r'[0-9]{4,8}', otp):
            raise ValueError('Enter the numeric OTP')
        response = self.requestJson('POST', '/Account/CheckOTP', data={
            'phoneNumber': self.phone,
            'otp':         otp,
            'userName':    self.phone,
            'otpType':     '58qi6Nxt6OtVinoAj/78gg==',
            'userType':    'Citizen',
            'ReturnUrl':   '',
            'loginUsing':  'Mobile',
        }, referer=BASEURL)
        redirect = response.get('UrlString')
        if not redirect:
            raise PortalResponseError('OTP was rejected or expired. Please try again.')
        destination = parse.urljoin(BASEURL, redirect)
        if parse.urlsplit(destination).netloc != parse.urlsplit(BASEURL).netloc:
            raise PortalResponseError('Unexpected login redirect host')
        self.requestText('GET', destination, referer=BASEURL)
        self.checkSession()
        self.persist = True
        self.save()

    def checkSession(self):
        '''
        Verify access to the authenticated properties page.

        Inputs:    None

        Output:    None; AuthenticationError for an expired session
        '''
        logging.debug('Checking authenticated session')
        html = self.requestText('GET', '/SearchProperty/MyProperties', referer=BASEURL)
        if isLoginHtml(html) or 'getSearchPropertiesDataMyProperties' not in html:
            raise AuthenticationError('Session expired; please verify a new OTP.')

    def fetchDetails(self, propertyInfo):
        '''
        Fetch full property details, retaining public-view restrictions.

        Inputs:    <propertyInfo>  -  Map record with an optional token

        Output:    Parsed detail dictionary
        '''
        logging.debug('Fetching property detail page')
        if publicViewRestricted(propertyInfo):
            return {'Detail': {'FetchStatus': 'unavailable', 'FetchError': 'Not available for public view'}}
        token = propertyInfo.get('Token')
        if not token:
            return {'Detail': {'FetchStatus': 'unavailable', 'FetchError': 'No public detail token'}}
        url = '/RegisterProperty/NewApplication?' + parse.urlencode({'token': token})
        html = self.requestText('GET', url, referer=f'{BASEURL}/SearchProperty/SearchPropertyOnMap')
        if isLoginHtml(html):
            raise AuthenticationError('Session expired; please verify a new OTP.')
        if restrictedPage(html):
            return {'Detail': {'FetchStatus': 'unavailable', 'FetchError': 'Not available for public view'}}
        detail = parsePropertyDetailHtml(html)
        fields = detail['Detail']
        recognizedLabels = {'pid', 'municipality', 'propertycategory', 'propertytype',
                            'propertysubtype', 'address', 'plotnohouseno', 'plotarea',
                            'ownershiptype', 'colonyname'}
        recognizedFields = any(labelKey(label) in recognizedLabels and value
                               for label, value in fields.get('Fields', {}).items())
        if not any(fields.get(key) for key in ('AsPerRecordPkPropertyId', 'PID', 'OwnerNames',
                                                'Municipality', 'PropertyCategory', 'Address')) and not (
                recognizedFields or fields.get('OwnerRows') or fields.get('ConstructionRows')):
            schema = responseSchema(html)
            identity = re.sub(r'[^a-zA-Z0-9_-]', '_', str(propertyInfo.get('PID') or propertyInfo.get('pkPropertyId') or 'unknown'))
            destination = self.cookieFile.parent / 'diagnostics' / f'{identity}_schema.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(schema, indent=2), encoding='utf-8')
            logging.warning(f'Unrecognized property response; structural diagnostics: {destination}')
            raise PortalResponseError('Property detail page has an unrecognized format')
        fields['FetchStatus'] = 'ok'
        fields['SourceUrl'] = BASEURL + url
        return detail

    def downloadPhoto(self, url, folder, identity):
        '''
        Download a bounded image and atomically save it under a stable filename.

        Inputs:    <url>       -  Image source URL
                   <folder>    -  Colony output folder
                   <identity>  -  Property ID used in the filename

        Output:    Relative path to the image; None for a removed image
        '''
        logging.debug('Downloading property photograph')
        req = request.Request(parse.quote(url, safe=':/?=&%+'), headers=self._headers(referer=BASEURL))
        try:
            with self.opener.open(req, timeout=45) as response:
                contentType = response.headers.get_content_type()
                if not contentType.startswith('image/'):
                    responsePath = parse.urlsplit(response.geturl()).path.lower()
                    responseHtml = response.read(256 * 1024).decode('utf-8', errors='replace')
                    if '/account/login' in responsePath or isLoginHtml(responseHtml):
                        raise AuthenticationError('Photo download requires renewed login')
                    raise PortalResponseError('Photo URL returned non-image content')
                content = response.read(25 * 1024 * 1024 + 1)
        except error.HTTPError as httpError:
            httpError.close()
            if httpError.code in (404, 410):
                return None
            if httpError.code == 401:
                raise AuthenticationError('Photo download requires renewed login') from httpError
            retryAfter = retryAfterSeconds(httpError.headers.get('Retry-After', '0'))
            raise PortalHttpError(httpError.code, retryAfter) from httpError
        if not content or len(content) > 25 * 1024 * 1024:
            raise PortalResponseError('Photo is empty or exceeds the 25 MB limit')
        extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                     'image/gif': '.gif', 'image/bmp': '.bmp', 'image/tiff': '.tif'}.get(contentType, '.img')
        safeIdentity = re.sub(r'[^a-zA-Z0-9_-]', '_', identity)
        fileName = safeIdentity + '_' + hashlib.sha256(url.encode()).hexdigest()[:16] + extension
        relativePath = Path('photos') / fileName
        destination = Path(folder) / relativePath
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.part')
        temporary.write_bytes(content)
        temporary.replace(destination)
        return relativePath.as_posix()
