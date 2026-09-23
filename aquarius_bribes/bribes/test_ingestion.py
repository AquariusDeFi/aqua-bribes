from copy import deepcopy
from unittest.mock import MagicMock, call, patch

from django.db import IntegrityError
from django.test import TestCase, override_settings

from aquarius_bribes.bribes.loader import BribesLoader
from aquarius_bribes.bribes.models import Bribe, BribeIngestionCursor, MarketKey

COLLECTOR = 'GCM375EAU2Y6E2LTDPSDNGZ4SLXHR3T2GYK7J6XOOUGXZAOYUMDOWTW5'
SPONSOR = 'GASMJGGEFR6SSKEYWNDK23BYDYETI53HHFJ5WRHUE2N5CDOBKXAY7FO2'
MARKET_KEY = 'GDR6DKRJY25OCF25LV3FK7BCEW4BMR2CBFJD457P42AKGPY3C2IXJAXZ'
REWARD_ISSUER = 'GDN2OYI5BE5EADJH6JCDITZPDZ35G5XJS6LMI3UIQGKCQZFXEVROI5AM'


@override_settings(
    REWARD_ASSET_CODE='AQUA',
    REWARD_ASSET_ISSUER=REWARD_ISSUER,
    AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS=SPONSOR,
)
class BribesLoaderIngestionTests(TestCase):
    def setUp(self):
        horizon_patcher = patch('aquarius_bribes.bribes.loader.get_horizon')
        self.mock_get_horizon = horizon_patcher.start()
        self.addCleanup(horizon_patcher.stop)

    def _loader(self):
        loader = BribesLoader(COLLECTOR, 'unused signer')
        loader._get_asset_equivalent = MagicMock(return_value='1.0000000')
        loader.logger.warning = MagicMock()
        return loader

    def _record(self, record_id, paging_token):
        return {
            'amount': '10.0000000',
            'asset': 'native',
            'claimants': [
                {
                    'destination': COLLECTOR,
                    'predicate': {
                        'not': {
                            'abs_before': '2026-07-27T12:00:00Z',
                        },
                    },
                },
                {
                    'destination': MARKET_KEY,
                    'predicate': {
                        'not': {
                            'unconditional': True,
                        },
                    },
                },
            ],
            'id': record_id,
            'last_modified_time': '2026-07-19T12:00:00Z',
            'paging_token': paging_token,
            'sponsor': SPONSOR,
        }

    def _skippable_record(self, record_id, paging_token):
        record = self._record(record_id, paging_token)
        record['claimants'] = record['claimants'][:1]
        return record

    def test_mixed_page_commits_bribe_and_raw_invalid_tail_cursor(self):
        loader = self._loader()
        valid = self._record('balance-101', '101')
        invalid_tail = self._skippable_record('balance-102', '102')
        loader._get_page = MagicMock(side_effect=[[valid, invalid_tail], []])

        loader.load_bribes()

        self.assertEqual(list(Bribe.objects.values_list('claimable_balance_id', flat=True)), ['balance-101'])
        self.assertEqual(BribeIngestionCursor.objects.get(account=COLLECTOR).paging_token, '102')
        loader.logger.warning.assert_called_once()

    def test_all_invalid_page_still_advances_raw_tail_cursor(self):
        loader = self._loader()
        invalid = self._skippable_record('balance-202', '202')
        loader._get_page = MagicMock(side_effect=[[invalid], []])

        loader.load_bribes()

        self.assertFalse(Bribe.objects.exists())
        self.assertFalse(MarketKey.objects.exists())
        self.assertEqual(BribeIngestionCursor.objects.get(account=COLLECTOR).paging_token, '202')

    def test_each_fetched_record_is_processed_once(self):
        loader = self._loader()
        first = self._record('balance-301', '301')
        second = self._skippable_record('balance-302', '302')
        loader._get_page = MagicMock(side_effect=[[first, second], []])

        with patch.object(loader, 'process_bribe', wraps=loader.process_bribe) as process_bribe:
            loader.load_bribes()

        self.assertEqual(process_bribe.call_args_list, [call(first), call(second)])

    def test_invalid_last_modified_time_blocks_page_before_enrichment(self):
        invalid_timestamps = {
            'missing': object(),
            'null': None,
            'non-string': 1721390400,
            'malformed': 'not-a-timestamp',
            'overflow': '999999999999999999999999999999999999-01-01T00:00:00Z',
        }

        for label, value in invalid_timestamps.items():
            with self.subTest(label=label):
                loader = self._loader()
                record = deepcopy(self._record('balance-{0}'.format(label), '400'))
                if label == 'missing':
                    record.pop('last_modified_time')
                else:
                    record['last_modified_time'] = value
                loader._get_page = MagicMock(side_effect=[[record], []])

                with self.assertRaises(ValueError):
                    loader.load_bribes()

                loader._get_asset_equivalent.assert_not_called()
                self.assertFalse(Bribe.objects.exists())
                self.assertFalse(MarketKey.objects.exists())
                self.assertFalse(BribeIngestionCursor.objects.filter(account=COLLECTOR).exists())

            Bribe.objects.all().delete()
            MarketKey.objects.all().delete()
            BribeIngestionCursor.objects.all().delete()

    def test_wrong_claimant_count_does_not_hide_invalid_transport_metadata(self):
        invalid_records = {}

        missing_id = self._skippable_record('balance-missing-id', '410')
        missing_id.pop('id')
        invalid_records['missing id'] = missing_id

        blank_id = self._skippable_record('balance-blank-id', '411')
        blank_id['id'] = ''
        invalid_records['blank id'] = blank_id

        missing_paging_token = self._skippable_record('balance-missing-token', '412')
        missing_paging_token.pop('paging_token')
        invalid_records['missing paging token'] = missing_paging_token

        malformed_timestamp = self._skippable_record('balance-bad-time', '413')
        malformed_timestamp['last_modified_time'] = 'not-a-timestamp'
        invalid_records['malformed timestamp'] = malformed_timestamp

        non_list_claimants = self._record('balance-bad-claimants', '414')
        non_list_claimants['claimants'] = {'not': 'a list'}
        invalid_records['non-list claimants'] = non_list_claimants

        for label, record in invalid_records.items():
            with self.subTest(label=label):
                loader = self._loader()
                loader._get_page = MagicMock(side_effect=[[record], []])

                with self.assertRaises(ValueError):
                    loader.load_bribes()

                loader.logger.warning.assert_not_called()
                self.assertFalse(Bribe.objects.exists())
                self.assertFalse(MarketKey.objects.exists())
                self.assertFalse(BribeIngestionCursor.objects.exists())

    def test_horizon_failure_does_not_advance_existing_cursor(self):
        cursor = BribeIngestionCursor.objects.create(account=COLLECTOR, paging_token='500')  # noqa: S106
        loader = self._loader()
        loader._get_page = MagicMock(side_effect=RuntimeError('Horizon unavailable'))

        with self.assertRaisesRegex(RuntimeError, 'Horizon unavailable'):
            loader.load_bribes()

        cursor.refresh_from_db()
        self.assertEqual(cursor.paging_token, '500')
        self.assertFalse(Bribe.objects.exists())
        self.assertFalse(MarketKey.objects.exists())
        loader._get_page.assert_called_once_with(cursor='500')

    def test_database_failure_rolls_back_market_key_bribe_and_cursor(self):
        cursor = BribeIngestionCursor.objects.create(account=COLLECTOR, paging_token='600')  # noqa: S106
        loader = self._loader()
        record = self._record('balance-601', '601')
        loader._get_page = MagicMock(side_effect=[[record], []])

        with patch.object(Bribe.objects, 'bulk_create', side_effect=IntegrityError('write failed')):
            with self.assertRaisesRegex(IntegrityError, 'write failed'):
                loader.load_bribes()

        cursor.refresh_from_db()
        self.assertEqual(cursor.paging_token, '600')
        self.assertFalse(Bribe.objects.exists())
        self.assertFalse(MarketKey.objects.exists())

    def test_replaying_durable_bribe_does_not_create_duplicate(self):
        record = self._record('balance-701', '701')
        first_loader = self._loader()
        first_loader._get_page = MagicMock(side_effect=[[record], []])
        first_loader.load_bribes()
        BribeIngestionCursor.objects.get(account=COLLECTOR).delete()

        replay_loader = self._loader()
        replay_loader._get_page = MagicMock(side_effect=[[record], []])
        with patch.object(replay_loader, 'process_bribe', wraps=replay_loader.process_bribe) as process_bribe:
            replay_loader.load_bribes()

        process_bribe.assert_called_once_with(record)
        self.assertEqual(Bribe.objects.filter(claimable_balance_id='balance-701').count(), 1)
        self.assertEqual(MarketKey.objects.filter(market_key=MARKET_KEY).count(), 1)
        self.assertEqual(BribeIngestionCursor.objects.get(account=COLLECTOR).paging_token, '701')

    def test_stale_worker_cannot_regress_cursor(self):
        cursor = BribeIngestionCursor.objects.create(  # noqa: S106
            account=COLLECTOR,
            paging_token='newer-durable-cursor',
        )
        loader = self._loader()
        stale_bribe = loader.process_bribe(self._record('balance-stale', 'stale-page-tail'))

        persisted = loader._persist_page(
            [stale_bribe],
            requested_cursor='stale-request-cursor',
            page_cursor='stale-page-tail',
        )

        self.assertFalse(persisted)
        cursor.refresh_from_db()
        self.assertEqual(cursor.paging_token, 'newer-durable-cursor')
        self.assertFalse(Bribe.objects.exists())
        self.assertFalse(MarketKey.objects.exists())

    def test_stale_worker_does_not_log_skipped_record(self):
        loader = self._loader()
        invalid = self._skippable_record('balance-stale', 'stale-page-tail')
        loader.load_last_event_id = MagicMock(side_effect=['stale-request-cursor', 'newer-durable-cursor'])
        loader._get_page = MagicMock(side_effect=[[invalid], []])
        loader._persist_page = MagicMock(return_value=False)

        loader.load_bribes()

        loader.logger.warning.assert_not_called()
        loader._get_page.assert_has_calls([
            call(cursor='stale-request-cursor'),
            call(cursor='newer-durable-cursor'),
        ])
