import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from stellar_sdk import Account, Asset, Keypair, TransactionEnvelope
from stellar_sdk.client.response import Response
from stellar_sdk.exceptions import BadResponseError, NotFoundError

from aquarius_bribes.bribes.models import AggregatedByAssetBribe, MarketKey
from aquarius_bribes.rewards.models import Payout, VoteSnapshot
from aquarius_bribes.rewards.reward_payer import RewardPayer


class UnknownSubmissionTests(TestCase):
    def setUp(self):
        self.wallet = Keypair.random()
        market = MarketKey.objects.create(
            market_key=Keypair.random().public_key, raw_asset1='native', raw_asset2='native',
        )
        self.bribe = AggregatedByAssetBribe.objects.create(
            market_key=market, asset_code=Asset.native().code, asset_issuer='',
            start_at=timezone.now() - timedelta(days=1), stop_at=timezone.now() + timedelta(days=1),
            total_reward_amount=Decimal('100'),
        )
        VoteSnapshot.objects.create(
            market_key=market, votes_value=Decimal('100'), voting_account=Keypair.random().public_key,
            snapshot_time=timezone.now().date(),
        )
        self.votes = VoteSnapshot.objects.filter(market_key=market)
        self.horizon = mock.Mock()
        self.submitted_hashes = []
        self.horizon.load_account.side_effect = lambda account: Account(account, 123 + len(self.submitted_hashes))
        self.horizon.submit_transaction.side_effect = self.submit_with_lost_response
        signing = mock.patch.object(TransactionEnvelope, 'sign', return_value=None)
        signing.start()
        self.addCleanup(signing.stop)

    def payer(self):
        with mock.patch('aquarius_bribes.rewards.reward_payer.get_horizon', return_value=self.horizon):
            return RewardPayer(self.bribe, self.wallet, Asset.native(), Decimal('100'))

    def submit_with_lost_response(self, envelope):
        self.submitted_hashes.append(envelope.hash_hex())
        raise BadResponseError(Response(502, '<html>Bad gateway</html>', {}, 'https://offline.invalid/transactions'))

    def assert_unknown_lookup_does_not_repay(self, response=None, error=None):
        self.payer().pay_reward(self.votes, total_votes=Decimal('100'))
        lookup = self.horizon.transactions.return_value.transaction.return_value.call
        lookup.return_value = response
        lookup.side_effect = error
        later = timezone.now() + timedelta(minutes=6)
        with mock.patch('aquarius_bribes.rewards.reward_payer.timezone.now', return_value=later):
            if isinstance(error, BadResponseError):
                with self.assertRaises(BadResponseError):
                    self.payer().pay_reward(self.votes, total_votes=Decimal('100'))
            else:
                self.payer().pay_reward(self.votes, total_votes=Decimal('100'))
            self.assertEqual(len(self.submitted_hashes), 1)
            self.assertEqual(
                list(Payout.objects.values_list('status', 'stellar_transaction_id')),
                [(Payout.STATUS_FAILED, self.submitted_hashes[0])],
            )

            lookup.side_effect = None
            lookup.return_value = {'successful': True, 'hash': self.submitted_hashes[0]}
            self.payer().pay_reward(self.votes, total_votes=Decimal('100'))

        self.assertEqual(len(self.submitted_hashes), 1)
        self.horizon.transactions.return_value.transaction.assert_called_with(self.submitted_hashes[0])
        self.assertEqual(
            list(Payout.objects.values_list('status', 'stellar_transaction_id')),
            [(Payout.STATUS_SUCCESS, self.submitted_hashes[0])],
        )

    def test_404_then_success_does_not_repay(self):
        error = NotFoundError(Response(404, '', {}, 'https://offline.invalid/transactions/hash'))
        self.assert_unknown_lookup_does_not_repay(error=error)

    def test_502_then_success_does_not_repay(self):
        error = BadResponseError(Response(
            502, '<html>Bad gateway</html>', {}, 'https://offline.invalid/transactions/hash',
        ))
        self.assert_unknown_lookup_does_not_repay(error=error)

    def test_incomplete_lookup_then_success_does_not_repay(self):
        self.assert_unknown_lookup_does_not_repay(response={})

    def test_server_error_result_codes_do_not_allow_retry(self):
        def submit_with_server_error(envelope):
            self.submitted_hashes.append(envelope.hash_hex())
            body = json.dumps({'extras': {'result_codes': {'transaction': 'tx_bad_seq'}}})
            raise BadResponseError(Response(503, body, {}, 'https://offline.invalid/transactions'))

        self.horizon.submit_transaction.side_effect = submit_with_server_error
        self.assert_unknown_lookup_does_not_repay(response={})

    def test_malformed_submit_and_404_do_not_repay(self):
        self.horizon.submit_transaction.side_effect = self.submit_with_malformed_response
        error = NotFoundError(Response(404, '', {}, 'https://offline.invalid/transactions/hash'))
        self.assert_unknown_lookup_does_not_repay(error=error)

    def test_malformed_submit_and_incomplete_lookup_do_not_repay(self):
        self.horizon.submit_transaction.side_effect = self.submit_with_malformed_response
        self.assert_unknown_lookup_does_not_repay(response={})

    def submit_with_malformed_response(self, envelope):
        self.submitted_hashes.append(envelope.hash_hex())
        return {}

    def test_confirmed_failed_transaction_can_be_retried(self):
        self.payer().pay_reward(self.votes, total_votes=Decimal('100'))
        self.horizon.transactions.return_value.transaction.return_value.call.return_value = {'successful': False}

        def submit_success(envelope):
            self.submitted_hashes.append(envelope.hash_hex())
            return {'successful': True, 'hash': envelope.hash_hex()}

        self.horizon.submit_transaction.side_effect = submit_success
        later = timezone.now() + timedelta(minutes=6)
        with mock.patch('aquarius_bribes.rewards.reward_payer.timezone.now', return_value=later):
            self.payer().pay_reward(self.votes, total_votes=Decimal('100'))

        self.assertEqual(len(self.submitted_hashes), 2)
        self.assertEqual(
            list(Payout.objects.values_list('status', 'stellar_transaction_id')),
            [(Payout.STATUS_SUCCESS, self.submitted_hashes[1])],
        )
