from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from stellar_sdk import Asset
from stellar_sdk.exceptions import BadResponseError

from aquarius_bribes.bribes.utils import get_horizon
from aquarius_bribes.rewards.claim_loader import ClaimLoader


class ClaimPageRetryTests(SimpleTestCase):
    @contextmanager
    def horizon(self, failures):
        cursors = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler dispatches by method name
                cursors.append(parse_qs(urlsplit(self.path).query)['cursor'][0])
                failed = len(cursors) <= failures
                self.send_response(503 if failed else 200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                body = b'{"status":503}' if failed else b'{"_embedded":{"records":[{"paging_token":"next"}]}}'
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                with self.settings(HORIZON_URL=f'http://127.0.0.1:{server.server_port}'):
                    with patch('urllib3.util.retry.time.sleep'):
                        yield cursors
            finally:
                server.shutdown()
                thread.join()

    def test_recovers_same_cursor_after_four_unavailable_responses(self):
        with self.horizon(failures=4) as cursors:
            records = ClaimLoader(Asset.native())._get_page(cursor='previous')
        self.assertEqual(records, [{'paging_token': 'next'}])
        self.assertEqual(cursors, ['previous'] * 5)

    def test_persistent_unavailability_exhausts_the_page_retry_budget(self):
        with self.horizon(failures=10) as cursors, self.assertRaises(BadResponseError):
            ClaimLoader(Asset.native())._get_page(cursor='previous')
        self.assertEqual(len(cursors), 8)

    def test_other_horizon_clients_keep_the_sdk_retry_budget(self):
        with self.horizon(failures=4) as cursors, self.assertRaises(BadResponseError):
            get_horizon().claimable_balances().cursor('previous').call()
        self.assertEqual(len(cursors), 4)
