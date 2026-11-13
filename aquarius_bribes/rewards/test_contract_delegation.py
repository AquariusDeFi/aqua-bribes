import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from stellar_sdk import Asset, Keypair, StrKey

from aquarius_bribes.bribes.models import MarketKey
from aquarius_bribes.rewards.contract_delegation import source_snapshot, validate_source
from aquarius_bribes.rewards.models import ContractDelegationSnapshot, VoteSnapshot
from aquarius_bribes.rewards.tasks import LOAD_VOTES_TASK_ACTIVE_KEY, task_load_votes
from aquarius_bribes.rewards.votes_loader import VotesLoader

AT = datetime(2026, 11, 13, 12, 7, 4, tzinfo=timezone.utc)
A, B, C = [Keypair.from_raw_ed25519_seed(bytes([n]) * 32).public_key for n in (41, 42, 43)]
ISSUER = Keypair.from_raw_ed25519_seed(b'\x44' * 32).public_key
PROXY = StrKey.encode_contract(b'\x01' * 32)
LOCKER = StrKey.encode_contract(b'\x02' * 32)
PASSPHRASE = 'Test SDF Network ; September 2015'
OWN, INCOMING = Asset('upvoteICE', ISSUER), Asset('dICE', ISSUER)


def envelope(donors=None):
    payload = {
        'capability': 'aquarius.ice.vote_source_snapshot.v1', 'day': AT.date().isoformat(),
        'effective_timestamp': int(AT.timestamp()),
        'ledger': {'sequence': 100, 'hash': 'a' * 64, 'closed_at': int(AT.timestamp())},
        'configuration': {'network_id': hashlib.sha256(PASSPHRASE.encode()).hexdigest(),
                          'locker_contract_id': LOCKER, 'contracts': {'Upvote': PROXY}},
        'projection': {'complete': True, 'fresh': True, 'observed_ledger': 100},
        'weight_denominator': 156 * 604800,
        'delegates': {B: donors if donors is not None else [{'donor': A, 'weight': '1'}]},
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'id': identity, 'payload': payload}


@override_settings(  # noqa: S106 - ephemeral local fixture token.
    CONTRACT_DELEGATION_UPVOTE_PROXY=PROXY, CONTRACT_DELEGATION_LOCKER=LOCKER,
    STELLAR_PASSPHRASE=PASSPHRASE, DELEGATABLE_ASSETS=[(OWN, INCOMING)],
    VOTING_TRACKER_URL='https://voting-tracker-testnet.example',
    CONTRACT_DELEGATION_SNAPSHOT_URL='https://distributor-testnet.example',
    CONTRACT_DELEGATION_SNAPSHOT_TOKEN='ephemeral-local-test-token')
class ContractAttributionTests(TestCase):
    def setUp(self):
        self.market = MarketKey.objects.create(market_key=Keypair.from_raw_ed25519_seed(b'\x45' * 32).public_key)

    def snapshot(self, donors=None):
        value = envelope(donors)
        return ContractDelegationSnapshot.objects.create(day=AT.date(), source_id=value['id'], payload=value['payload'])

    def load(self, snapshot, amount='100', incoming='100'):
        loader = VotesLoader(self.market.pk, AT, source_snapshot=snapshot)
        vote = {'voting_account': B, 'votes_value': amount, 'asset_votes': [
            {'asset': '{0}:{1}'.format(INCOMING.code, ISSUER), 'votes_value': incoming},
            {'asset': '{0}:{1}'.format(OWN.code, ISSUER), 'votes_value': str(Decimal(amount) - Decimal(incoming))},
        ]}
        with (
            patch.object(loader, '_get_page', side_effect=[[vote], []]),
            patch.object(loader, 'has_delegated_votes', side_effect=AssertionError('Legacy markers used')),
        ):
            loader.load_votes()

    def test_contract_only_source_gets_reward_weight_without_legacy_marker(self):
        self.load(self.snapshot())
        rows = VoteSnapshot.objects.filter(has_delegation=False)
        self.assertEqual(list(rows.values_list('voting_account', 'votes_value')), [(A, Decimal('100'))])

    def test_stale_legacy_marker_cannot_supply_a_different_owner(self):
        self.load(self.snapshot())
        self.assertFalse(VoteSnapshot.objects.filter(voting_account=C).exists())

    def test_mixed_own_and_incoming_votes_keep_own_share(self):
        self.load(self.snapshot([{'donor': A, 'weight': '1'}, {'donor': C, 'weight': '3'}]), '150', '100')
        rows = dict(VoteSnapshot.objects.filter(has_delegation=False).values_list('voting_account', 'votes_value'))
        self.assertEqual(rows, {A: Decimal('25'), C: Decimal('75'), B: Decimal('50')})

    def test_absent_historical_donors_blocks_all_rows(self):
        with self.assertRaisesRegex(ValueError, 'donor'):
            self.load(self.snapshot([]))
        self.assertFalse(VoteSnapshot.objects.exists())

    def test_rounding_uses_exact_integer_weights(self):
        self.load(self.snapshot([{'donor': A, 'weight': '1'}, {'donor': C, 'weight': '2'}]), '0.0000002', '0.0000002')
        values = dict(VoteSnapshot.objects.filter(has_delegation=False).values_list('voting_account', 'votes_value'))
        self.assertEqual(values, {A: Decimal('0'), C: Decimal('0.0000001')})

    def test_wrong_timestamp_or_tampered_source_is_rejected(self):
        record = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            VotesLoader(self.market.pk, AT.replace(minute=0), source_snapshot=record)
        record.payload['delegates'][B] = [{'donor': C, 'weight': '1'}]
        with self.assertRaisesRegex(ValueError, 'identity'):
            VotesLoader(self.market.pk, AT, source_snapshot=record)

    def test_retry_reuses_saved_source_without_network_or_new_donors(self):
        record = self.snapshot()
        with patch(
            'aquarius_bribes.rewards.contract_delegation.requests.request',
            side_effect=AssertionError('network'),
        ):
            self.assertEqual(source_snapshot(AT).source_id, record.source_id)

    def test_failed_upstream_snapshot_does_not_create_local_record(self):
        with patch(
            'aquarius_bribes.rewards.contract_delegation.requests.request',
            side_effect=ValueError('unavailable'),
        ):
            with self.assertRaises(ValueError):
                source_snapshot(AT)
        self.assertFalse(ContractDelegationSnapshot.objects.exists())

    def response(self, rows, **changes):
        contents = {'as_of': int(AT.timestamp()), 'network_id': hashlib.sha256(PASSPHRASE.encode()).hexdigest(),
                    'market_key': self.market.pk, 'results': rows}
        identity = hashlib.sha256(json.dumps(contents, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        body = dict(contents, count=len(rows), next=None, view_id=identity)
        body.update(changes)
        return SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    def test_last_page_does_not_request_nonexistent_next_page(self):
        row = {'voting_account': B, 'votes_value': '100.0000000',
               'asset_votes': [{'asset': '{0}:{1}'.format(INCOMING.code, ISSUER), 'votes_value': '100.0000000'}]}
        loader = VotesLoader(self.market.pk, AT, source_snapshot=self.snapshot())
        with patch('aquarius_bribes.rewards.votes_loader.requests.get', return_value=self.response([row])) as request:
            loader.load_votes()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(VoteSnapshot.objects.get(is_delegated=True).voting_account, A)

    def test_network_mismatch_or_incomplete_view_cannot_save_votes(self):
        row = {'voting_account': B, 'votes_value': '100.0000000',
               'asset_votes': [{'asset': '{0}:{1}'.format(INCOMING.code, ISSUER), 'votes_value': '100.0000000'}]}
        snapshot = self.snapshot()
        for changes in ({'network_id': 'b' * 64}, {'count': 2}, {'view_id': 'b' * 64}):
            with self.subTest(changes=changes):
                loader = VotesLoader(self.market.pk, AT, source_snapshot=snapshot)
                with patch(
                    'aquarius_bribes.rewards.votes_loader.requests.get',
                    return_value=self.response([row], **changes),
                ):
                    with self.assertRaises(ValueError):
                        loader.load_votes()
        self.assertFalse(VoteSnapshot.objects.exists())

    @override_settings(CONTRACT_DELEGATION_SNAPSHOTS_ENABLED=True)
    def test_failure_in_second_market_rolls_back_all_votes_and_releases_active_flag(self):
        record = self.snapshot()
        other = MarketKey.objects.create(market_key=Keypair.from_raw_ed25519_seed(b'\x46' * 32).public_key)
        markets = sorted([self.market.pk, other.pk])

        def loader(market, *args, **kwargs):
            def load():
                if market == markets[1]:
                    raise ValueError('Missing donor input')
                VoteSnapshot.objects.create(snapshot_time=AT.date(), market_key_id=market,
                                            voting_account=A, votes_value=1)
            return SimpleNamespace(load_votes=load)

        with (
            patch('aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects.filter') as active,
            patch('aquarius_bribes.rewards.tasks.VotesLoader', side_effect=loader),
            patch('aquarius_bribes.rewards.tasks.cache') as cache,
        ):
            active.return_value.exists.return_value = True
            active.return_value.values_list.return_value.distinct.return_value = markets
            with self.assertRaisesRegex(ValueError, 'donor'):
                task_load_votes(AT)
            cache.set.assert_called_with(LOAD_VOTES_TASK_ACTIVE_KEY, False, None)
        record.refresh_from_db()
        self.assertFalse(record.votes_loaded)
        self.assertFalse(VoteSnapshot.objects.exists())

    @override_settings(CONTRACT_DELEGATION_SNAPSHOTS_ENABLED=True)
    def test_completed_daily_task_is_not_loaded_twice(self):
        record = self.snapshot()
        record.votes_loaded, record.loaded_markets = True, [self.market.pk]
        record.save(update_fields=['votes_loaded', 'loaded_markets'])
        with (
            patch('aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects.filter') as active,
            patch('aquarius_bribes.rewards.tasks.VotesLoader') as loader,
            patch('aquarius_bribes.rewards.tasks.cache'),
        ):
            active.return_value.exists.return_value = True
            active.return_value.values_list.return_value.distinct.return_value = [self.market.pk]
            task_load_votes(AT)
            loader.assert_not_called()

    def test_textual_false_is_not_a_completeness_attestation(self):
        value = envelope()
        value['payload']['projection']['complete'] = 'false'
        encoded = json.dumps(value['payload'], sort_keys=True, separators=(',', ':')).encode()
        identity = hashlib.sha256(encoded).hexdigest()
        with self.assertRaises(ValueError):
            validate_source(identity, value['payload'])
