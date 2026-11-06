from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from aquarius_bribes.bribes.loader import BribesLoader
from aquarius_bribes.bribes.models import BribeIngestionCursor

ACCOUNT = 'GCM375EAU2Y6E2LTDPSDNGZ4SLXHR3T2GYK7J6XOOUGXZAOYUMDOWTW5'


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL-specific concurrency test')
class BribesLoaderConcurrencyTests(TransactionTestCase):
    def test_only_one_worker_claims_initial_cursor(self):
        barrier = Barrier(2)

        def persist(page_cursor):
            close_old_connections()
            connection.ensure_connection()
            connection_id = id(connection.connection)
            loader = object.__new__(BribesLoader)
            loader.account = ACCOUNT
            try:
                barrier.wait(timeout=10)
                result = loader._persist_page([], None, page_cursor)
                return page_cursor, result, connection_id
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(persist, 'page-a'),
                executor.submit(persist, 'page-b'),
            ]
            outcomes = [future.result(timeout=20) for future in futures]

        self.assertEqual(sorted(result for _, result, _ in outcomes), [False, True])
        self.assertEqual(len({connection_id for _, _, connection_id in outcomes}), 2)
        winner = next(page_cursor for page_cursor, result, _ in outcomes if result)
        cursor = BribeIngestionCursor.objects.get(account=ACCOUNT)
        self.assertEqual(cursor.paging_token, winner)
