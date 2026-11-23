'''Certificate trust configuration shared by login, workers and photographs.'''

# Standard Library Imports
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib import request

# Third Party Imports
import certifi

from scraper import createPortalSslContext, makeCookie
from ulb_scraper.portal import PortalClient


class HttpsTrustTests(unittest.TestCase):
    def testBundledRootsAndVerification(self):
        context = createPortalSslContext()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        bundlePath = Path(certifi.where())
        self.assertTrue(bundlePath.is_file())
        # Parse the actual shipped bundle, so a missing/broken CA file fails
        # both the source suite and the packaged offline launch.
        bundleContext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bundleContext.load_verify_locations(cafile=str(bundlePath))
        self.assertGreater(len(bundleContext.get_ca_certs()), 50)

    def testWindowsUsesNativeValidationAndAdditionalRoots(self):
        with patch('scraper.sys.platform', 'win32'), patch(
                'truststore.SSLContext') as nativeContext:
            context = createPortalSslContext()
        nativeContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile=certifi.where())

    def testOtherPlatformsKeepDefaultSystemTrust(self):
        with patch('scraper.sys.platform', 'linux'), patch(
                'scraper.ssl.create_default_context') as defaultContext:
            context = createPortalSslContext()
        defaultContext.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile=certifi.where())

    def testWorkerClonesUseVerifiedTransportAndKeepCookiesInMemory(self):
        with tempfile.TemporaryDirectory() as folder:
            cookiePath = Path(folder) / 'cookies.lwp'
            client = PortalClient(cookieFile=cookiePath, fresh=True, persist=False)
            client.cookieJar.set_cookie(makeCookie(
                'testSession', 'offline-test', 'property.ulbharyana.gov.in'))
            clone = client.clone()
            contexts = []
            for worker in (client, clone):
                handlers = [handler for handler in worker.opener.handlers
                            if isinstance(handler, request.HTTPSHandler)]
                self.assertEqual(len(handlers), 1)
                context = handlers[0]._context
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                contexts.append(context)
            self.assertIsNot(contexts[0], contexts[1])
            self.assertIsNot(client.cookieJar, clone.cookieJar)
            self.assertEqual([cookie.value for cookie in clone.cookieJar], ['offline-test'])
            clone.save()
            self.assertFalse(cookiePath.exists())


if __name__ == '__main__':
    unittest.main()
