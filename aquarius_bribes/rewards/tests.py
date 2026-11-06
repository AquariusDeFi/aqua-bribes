import io
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import models
from django.test import TestCase, override_settings
from django.utils import timezone

import requests
from constance import config
from stellar_sdk import Asset, Claimant, ClaimPredicate, Keypair, Server, TransactionBuilder
from stellar_sdk.exceptions import BaseHorizonError

from aquarius_bribes.bribes.models import AggregatedByAssetBribe, Bribe, MarketKey
from aquarius_bribes.bribes.tasks import task_aggregate_bribes, load_market_key_details
from aquarius_bribes.rewards.eligibility import get_payable_votes
from aquarius_bribes.rewards.models import AssetHolderBalanceSnapshot, ClaimableBalance, Payout, VoteSnapshot
from aquarius_bribes.rewards.reward_payer import RewardPayer
from aquarius_bribes.rewards.tasks import (
    task_load_votes,
    task_make_claims_snapshot,
    task_make_trustees_snapshot,
    task_pay_rewards,
)
from aquarius_bribes.rewards.utils import SecuredWallet
from aquarius_bribes.rewards.votes_loader import VotesLoader
from aquarius_bribes.utils.assets import get_asset_string

random_asset_issuer = Keypair.random()
bribe_wallet = Keypair.random()

DELEGATABLE_ASSETS = [
    (Asset('upvote', random_asset_issuer.public_key), Asset('delegatedUp', random_asset_issuer.public_key))
]

@override_settings(
    REWARD_ASSET_CODE='ZZZ', REWARD_ASSET_ISSUER=random_asset_issuer.public_key,
    BRIBE_WALLET_ADDRESS=bribe_wallet.public_key, BRIBE_WALLET_SIGNER=bribe_wallet.secret,
    DELEGATABLE_ASSETS=DELEGATABLE_ASSETS,
)
class BribesTests(TestCase):
    def _create_account(self, account):
        # Fund new wallet
        response = requests.get('https://friendbot.stellar.org/?addr={}'.format(account))
        return response

    def _get_builder(self, source):
        account_info = self._load_or_create_account(source.public_key)
        base_fee = settings.BASE_FEE
        builder = TransactionBuilder(
            source_account=account_info,
            network_passphrase=settings.STELLAR_PASSPHRASE,
            base_fee=base_fee,
        )
        return builder

    def _trust_asset(self, account, asset, builder=None):
        if builder is None:
            builder = self._get_builder(account)

        builder.append_change_trust_op(
            asset=asset, source=account.public_key,
        )

        return builder

    def _payment(self, source, destination, asset, amount, builder=None):
        if builder is None:
            builder = self._get_builder(source)

        builder.append_payment_op(
            destination=destination.public_key,
            asset=asset,
            source=source.public_key,
            amount=Decimal(amount),
        )

        return builder

    def _create_wallet(self, source, destination, amount, builder=None):
        if builder is None:
            builder = self._get_builder(source)

        builder.append_create_account_op(
            destination=destination.public_key,
            source=source.public_key,
            starting_balance=Decimal(amount),
        )

        return builder

    def _send_claim(self, source, claimants, asset, amount, builder=None):
        if builder is None:
            builder = self._get_builder(source)

        builder.append_create_claimable_balance_op(
            claimants=claimants,
            asset=asset,
            source=source.public_key,
            amount=Decimal(amount),
        )

        return builder

    def _prepare_orderbook(self, amount, price):
        builder = self._get_builder(random_asset_issuer)

        builder.append_manage_buy_offer_op(
            selling_code=settings.REWARD_ASSET_CODE,
            selling_issuer=settings.REWARD_ASSET_ISSUER,
            buying_code=self.asset_xxx.code,
            buying_issuer=self.asset_xxx.issuer,
            amount=amount,
            price=price,
        )
        transaction_envelope = builder.build()
        transaction_envelope.sign(random_asset_issuer.secret)
        return self.server.submit_transaction(transaction_envelope)

    def _load_or_create_account(self, account):
        try:
            account_info = self.server.load_account(account)
        except Exception:
            self._create_account(account)
            account_info = self._load_or_create_account(account)

        return account_info

    def setUp(self):
        self.server = Server(settings.HORIZON_URL)
        self.bribe_wallet = bribe_wallet
        self.account_1 = Keypair.random()
        self.account_2 = Keypair.random()
        self.default_market_key = MarketKey(market_key=Keypair.random().public_key)
        self.default_market_key.save()
        self.asset_xxx_issuer = Keypair.random()
        self.reward_asset = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)

        self.delegation_asset = settings.DELEGATABLE_ASSETS[0][1]
        self.delegated_asset = settings.DELEGATABLE_ASSETS[0][0]

        self.asset_xxx = Asset(code='XXX', issuer=self.asset_xxx_issuer.public_key)
        self._load_or_create_account(self.asset_xxx_issuer.public_key)
        self._load_or_create_account(random_asset_issuer.public_key)

        self._load_or_create_account(self.bribe_wallet.public_key)
        self._load_or_create_account(self.account_1.public_key)
        self._load_or_create_account(self.account_2.public_key)

        builder = self._get_builder(self.asset_xxx_issuer)
        builder = self._trust_asset(self.account_1, self.asset_xxx, builder=builder)
        builder = self._trust_asset(self.account_2, self.asset_xxx, builder=builder)
        builder = self._trust_asset(self.bribe_wallet, self.reward_asset, builder=builder)
        builder = self._trust_asset(self.bribe_wallet, self.asset_xxx, builder=builder)
        builder = self._trust_asset(random_asset_issuer, self.asset_xxx, builder=builder)
        builder = self._payment(self.asset_xxx_issuer, self.account_1, self.asset_xxx, amount=1000, builder=builder)
        builder = self._payment(self.asset_xxx_issuer, self.account_2, self.asset_xxx, amount=1000, builder=builder)
        builder = self._payment(
            self.asset_xxx_issuer, self.bribe_wallet, self.asset_xxx, amount=100000000, builder=builder,
        )
        builder = self._payment(
            random_asset_issuer, self.bribe_wallet, self.reward_asset, amount=100000000, builder=builder,
        )
        transaction_envelope = builder.build()
        transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope.sign(self.account_2.secret)
        transaction_envelope.sign(self.bribe_wallet.secret)
        transaction_envelope.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

    def test_votes_loader(self):
        market_key = MarketKey(market_key='GBPF7NLFCYGZNHU6HS64ZGTE4YCRLAWTLFGOMFTHQ3WSUUFIGOSQFPJT')
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        loader = VotesLoader(market_key, snapshot_time)
        loader.load_votes()

        response = requests.get(
            'https://voting-tracker.aqua.network/api/market-keys/{}/votes/?timestamp={}'.format(
                market_key, snapshot_time.strftime("%s"),
            )
        )

        self.assertEqual(VoteSnapshot.objects.count(), response.json()['count'])

    def test_votes_loader_aggregates_duplicate_delegated_votes(self):
        market_key = MarketKey(market_key=Keypair.random().public_key)
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        delegate = Keypair.random().public_key
        delegator_1 = Keypair.random().public_key
        delegator_2 = Keypair.random().public_key

        not_unconditional = ClaimPredicate.predicate_not(
            ClaimPredicate.predicate_unconditional()
        ).to_xdr_object().to_xdr()
        unconditional = ClaimPredicate.predicate_unconditional().to_xdr_object().to_xdr()

        def create_claimable_balance(owner, asset, amount, claimants):
            balance = ClaimableBalance.objects.create(
                claimable_balance_id=Keypair.random().public_key,
                asset_code=asset.code,
                asset_issuer=asset.issuer,
                amount=Decimal(amount),
                sponsor=owner,
                paging_token='',
                last_modified_time=timezone.now(),
                last_modified_ledger=1,
                owner=owner,
            )
            for destination, raw_predicate in claimants:
                balance.claimants.create(
                    destination=destination,
                    raw_predicate=raw_predicate,
                )
            return balance

        # delegate has 300 tokens in total
        create_claimable_balance(
            owner=delegate,
            asset=self.delegation_asset,
            amount='300',
            claimants=[(market_key.market_key, unconditional)],
        )

        # each delegator has two inherited claimable balances because of extra tokens lock
        create_claimable_balance(
            owner=delegator_1,
            asset=self.delegated_asset,
            amount='120',
            claimants=[
                (delegate, not_unconditional),
                (settings.DELEGATE_MARKER, not_unconditional),
            ],
        )
        create_claimable_balance(
            owner=delegator_1,
            asset=self.delegated_asset,
            amount='60',
            claimants=[
                (delegate, not_unconditional),
                (settings.DELEGATE_MARKER, not_unconditional),
            ],
        )
        create_claimable_balance(
            owner=delegator_2,
            asset=self.delegated_asset,
            amount='80',
            claimants=[
                (delegate, not_unconditional),
                (settings.DELEGATE_MARKER, not_unconditional),
            ],
        )
        create_claimable_balance(
            owner=delegator_2,
            asset=self.delegated_asset,
            amount='40',
            claimants=[
                (delegate, not_unconditional),
                (settings.DELEGATE_MARKER, not_unconditional),
            ],
        )

        loader = VotesLoader(market_key.market_key, snapshot_time)

        # all votes come from delegate
        def vote_loading_mock(self, page, page_limit=200):
            if page and page > 1:
                return []
            return [
                {"votes_value": '300', "voting_account": delegate},
            ]

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            loader.load_votes()

        snapshots = VoteSnapshot.objects.filter(
            snapshot_time=snapshot_time.date(),
            market_key=market_key,
        )
        self.assertEqual(snapshots.count(), 3)

        delegate_snapshot = snapshots.get(
            voting_account=delegate,
            is_delegated=False,
            has_delegation=True,
            delegate_owner=None,
        )
        self.assertEqual(delegate_snapshot.votes_value, Decimal('300'))

        delegator_1_snapshot = snapshots.get(
            voting_account=delegator_1,
            is_delegated=True,
            has_delegation=False,
            delegate_owner=delegate,
        )
        delegator_2_snapshot = snapshots.get(
            voting_account=delegator_2,
            is_delegated=True,
            has_delegation=False,
            delegate_owner=delegate,
        )
        # delegated votes are aggregated correctly
        self.assertEqual(delegator_1_snapshot.votes_value, Decimal('180'))
        self.assertEqual(delegator_2_snapshot.votes_value, Decimal('120'))

    def test_reward_payer(self):
        market_key = MarketKey(market_key='GBPF7NLFCYGZNHU6HS64ZGTE4YCRLAWTLFGOMFTHQ3WSUUFIGOSQFPJT')
        market_key.save()

        load_market_key_details()

        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.asset_xxx, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                self.asset_xxx_issuer, voting_account, self.asset_xxx, amount=1000, builder=builder,
            )

        transaction_envelope = builder.build()

        for i in range(10):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope.sign(self.asset_xxx_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        VoteSnapshot.objects.bulk_create(votes)

        start_at = timezone.now()
        stop_at = timezone.now()

        bribe = Bribe(
            market_key=market_key,
            amount_for_bribes=100000,
            amount_aqua=config.CONVERTATION_AMOUNT,
            asset_code=self.asset_xxx.code,
            asset_issuer=self.asset_xxx.issuer,
            status=Bribe.STATUS_ACTIVE,
            amount=100000,
            created_at=timezone.now(),
            updated_at=timezone.now(),
            start_at=start_at,
            stop_at=stop_at,
        )
        bribe.save()
        task_aggregate_bribes(start_at, stop_at)
        bribe = AggregatedByAssetBribe.objects.first()

        reward_wallet = SecuredWallet(
            public_key=settings.BRIBE_WALLET_ADDRESS,
            secret=settings.BRIBE_WALLET_SIGNER,
        )

        reward_period = timedelta(hours=1)
        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))
        reward_payer = RewardPayer(bribe, reward_wallet, self.reward_asset, reward_amount)
        reward_payer.pay_reward(VoteSnapshot.objects.all())

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)

    def test_reward_payer_with_native_asset(self):
        market_key = MarketKey(market_key='GBPF7NLFCYGZNHU6HS64ZGTE4YCRLAWTLFGOMFTHQ3WSUUFIGOSQFPJT')
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.asset_xxx, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                self.asset_xxx_issuer, voting_account, self.asset_xxx, amount=1000, builder=builder,
            )

        transaction_envelope = builder.build()

        for i in range(10):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope.sign(self.asset_xxx_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        VoteSnapshot.objects.bulk_create(votes)

        start_at = timezone.now()
        stop_at = timezone.now()

        bribe = Bribe(
            market_key=market_key,
            amount_for_bribes=100000,
            amount_aqua=config.CONVERTATION_AMOUNT,
            asset_code=Asset.native().code,
            asset_issuer=Asset.native().issuer or '',
            status=Bribe.STATUS_ACTIVE,
            amount=100000,
            created_at=timezone.now(),
            updated_at=timezone.now(),
            start_at=start_at,
            stop_at=stop_at,
        )
        bribe.save()
        task_aggregate_bribes(start_at, stop_at)
        bribe = AggregatedByAssetBribe.objects.first()

        reward_wallet = SecuredWallet(
            public_key=settings.BRIBE_WALLET_ADDRESS,
            secret=settings.BRIBE_WALLET_SIGNER,
        )

        reward_period = timedelta(hours=1)
        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))
        reward_payer = RewardPayer(bribe, reward_wallet, self.reward_asset, reward_amount)
        reward_payer.pay_reward(VoteSnapshot.objects.all())

        Payout.objects.values_list('stellar_transaction_id', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)

    def test_reward_payer_delegated(self):
        market_key = MarketKey(market_key=Keypair.random().public_key)
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        start_at = timezone.now()
        bribe = AggregatedByAssetBribe(
            market_key_id=market_key,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe.save()
        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        claims_before = ClaimableBalance.objects.count()

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.asset_xxx, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                self.asset_xxx_issuer, voting_account, self.asset_xxx, amount=1000, builder=builder,
            )

        delegation_voting_account = Keypair.random()
        accounts.append(delegation_voting_account)
        builder = self._create_wallet(self.account_1, delegation_voting_account, 5, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegation_asset, builder=builder)
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegation_asset, amount=1000, builder=builder,
        )
        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        builder = self._trust_asset(delegation_voting_account, self.reward_asset, builder=builder)
        builder = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=300, builder=builder)

        votes.append(
            VoteSnapshot(
                votes_value=300,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key,
                is_delegated=False,
            )
        )

        for i in range(3):
            voting_account = Keypair.random()
            accounts.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder,
            )
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        transaction_envelope = builder.build()

        for i in range(len(accounts)):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        task_make_claims_snapshot()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (3 + 1))  # 3 delegations + 1 delegated vote

        task_make_trustees_snapshot()
        self.assertEqual(AssetHolderBalanceSnapshot.objects.count() - holders_before, 13 + 1)  # 13 votes + bribe wallet

        def vote_loading_mock(self, page):
            if page and page > 1:
                return []
            else:
                return list(map(lambda x: {
                    "votes_value": x.votes_value,
                    "voting_account": x.voting_account
                }, votes))

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            task_load_votes()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (3 + 1))  # 3 delegations + 1 delegated vote
        self.assertEqual(VoteSnapshot.objects.count(), 14)
        self.assertEqual(VoteSnapshot.objects.filter(is_delegated=True).count(), 3)

        reward_period = timedelta(hours=1)
        task_pay_rewards(reward_period=reward_period)

        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), 13)
        print(reward_amount, Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'])
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] - reward_amount < 0.01, True)
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] <= reward_amount, True)

    def test_reward_payer_delegated_with_own_votes(self):
        market_key = MarketKey(market_key='GBPF7NLFCYGZNHU6HS64ZGTE4YCRLAWTLFGOMFTHQ3WSUUFIGOSQFPJT')
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        start_at = timezone.now()
        bribe = AggregatedByAssetBribe(
            market_key_id=market_key,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe.save()

        task_make_claims_snapshot()
        claims_before = ClaimableBalance.objects.count()
        ClaimableBalance.objects.all().delete()
        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=1000, builder=builder,
            )

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=market_key.market_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
            ]
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        delegation_voting_account = Keypair.random()
        accounts.append(delegation_voting_account)
        builder = self._create_wallet(self.account_1, delegation_voting_account, 5, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegation_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegated_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.reward_asset, builder=builder)
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegated_asset, amount=1000, builder=builder,
        )
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegation_asset, amount=1000, builder=builder,
        )
        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        builder = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=300, builder=builder)

        votes.append(
            VoteSnapshot(
                votes_value=500,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key,
                is_delegated=False,
            )
        )

        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        builder = self._send_claim(delegation_voting_account, claimants, self.delegated_asset, amount=200, builder=builder)

        for i in range(3):
            voting_account = Keypair.random()
            accounts.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder,
            )
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        transaction_envelope = builder.build()

        for i in range(len(accounts)):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        # transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        task_make_claims_snapshot()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1))  # 10 votes + 3 delegations + 1 delegated vote

        task_make_trustees_snapshot()
        self.assertEqual(AssetHolderBalanceSnapshot.objects.count() - holders_before, 13 + 1)  # 13 votes + bribe wallet

        def vote_loading_mock(self, page):
            if page and page > 1:
                return []
            else:
                return list(map(lambda x: {
                    "votes_value": x.votes_value,
                    "voting_account": x.voting_account
                }, votes))

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            task_load_votes()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1))  # 10 votes + 3 delegations + 1 delegated vote
        self.assertEqual(VoteSnapshot.objects.count(), 15)
        self.assertEqual(VoteSnapshot.objects.filter(is_delegated=True).count(), 3)
        self.assertEqual(VoteSnapshot.objects.filter(has_delegation=True).count(), 1)

        reward_period = timedelta(hours=1)
        task_pay_rewards(reward_period=reward_period)

        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), 14)
        print(reward_amount, Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'])
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] - reward_amount < 0.01, True)
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] <= reward_amount, True)

    def test_reward_payer_delegated_with_not_delegated(self):
        market_key = MarketKey(market_key=Keypair.random().public_key)
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)
        ClaimableBalance.objects.all().delete()

        start_at = timezone.now()
        bribe = AggregatedByAssetBribe(
            market_key_id=market_key,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe.save()

        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        builder = self._get_builder(self.account_1)

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.asset_xxx, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                self.asset_xxx_issuer, voting_account, self.asset_xxx, amount=1000, builder=builder,
            )

        delegation_voting_account = Keypair.random()
        accounts.append(delegation_voting_account)
        builder = self._create_wallet(self.account_1, delegation_voting_account, 5, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegation_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.reward_asset, builder=builder)
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegation_asset, amount=1000, builder=builder,
        )
        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        builder = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=300, builder=builder)

        votes.append(
            VoteSnapshot(
                votes_value=600,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key,
                is_delegated=False,
            )
        )

        for i in range(3):
            voting_account = Keypair.random()
            accounts.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder,
            )
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        transaction_envelope = builder.build()

        for i in range(len(accounts)):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        task_make_claims_snapshot()

        self.assertEqual(ClaimableBalance.objects.filter(claimants__destination=market_key.market_key).count(), 1)
        self.assertEqual(ClaimableBalance.objects.filter(
                asset_code=self.delegated_asset.code,
                asset_issuer=self.delegated_asset.issuer,
            ).filter(claimants__destination=delegation_voting_account.public_key).count(), 3
        )

        task_make_trustees_snapshot()
        self.assertEqual(AssetHolderBalanceSnapshot.objects.count() - holders_before, 14)

        def vote_loading_mock(self, page):
            if page and page > 1:
                return []
            else:
                return list(map(lambda x: {
                    "votes_value": x.votes_value,
                    "voting_account": x.voting_account
                }, votes))

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            task_load_votes()

        self.assertEqual(ClaimableBalance.objects.filter(claimants__destination=market_key.market_key).count(), 1)
        self.assertEqual(ClaimableBalance.objects.filter(
                asset_code=self.delegated_asset.code,
                asset_issuer=self.delegated_asset.issuer,
            ).filter(claimants__destination=delegation_voting_account.public_key).count(), 3
        )
        self.assertEqual(VoteSnapshot.objects.count(), 15)
        self.assertEqual(VoteSnapshot.objects.filter(is_delegated=True).count(), 3)

        reward_period = timedelta(hours=1)
        task_pay_rewards(reward_period=reward_period)

        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), 14)
        print(reward_amount, Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'])
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] - reward_amount < 0.01, True)
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] <= reward_amount, True)

    def test_reward_payer_delegated_with_own_votes_2(self):
        market_key = MarketKey(market_key=Keypair.random().public_key)
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        start_at = timezone.now()
        bribe = AggregatedByAssetBribe(
            market_key_id=market_key,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe.save()

        task_make_claims_snapshot()
        claims_before = ClaimableBalance.objects.count()
        ClaimableBalance.objects.all().delete()
        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=1000, builder=builder,
            )

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=market_key.market_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
            ]
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        delegation_voting_account = Keypair.random()
        print(delegation_voting_account.public_key)
        accounts.append(delegation_voting_account)
        builder = self._create_wallet(self.account_1, delegation_voting_account, 5, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegation_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegated_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.reward_asset, builder=builder)
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegated_asset, amount=1000, builder=builder,
        )
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegation_asset, amount=1000, builder=builder,
        )
        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_delegated_votes = 30
        builder = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=delegation_voting_account_delegated_votes, builder=builder)

        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_own_votes = 200
        builder = self._send_claim(delegation_voting_account, claimants, self.delegated_asset, amount=delegation_voting_account_own_votes, builder=builder)

        votes.append(
            VoteSnapshot(
                votes_value=delegation_voting_account_delegated_votes + delegation_voting_account_own_votes,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key,
                is_delegated=False,
            )
        )

        for i in range(3):
            voting_account = Keypair.random()
            accounts.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder,
            )
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        transaction_envelope = builder.build()

        for i in range(len(accounts)):
            transaction_envelope.sign(accounts[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        # transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)

        task_make_claims_snapshot()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1))  # 10 votes + 3 delegations + 1 delegated vote

        task_make_trustees_snapshot()
        self.assertEqual(AssetHolderBalanceSnapshot.objects.count() - holders_before, 13 + 1)  # 13 votes + bribe wallet

        def vote_loading_mock(self, page):
            if page and page > 1:
                return []
            else:
                return list(map(lambda x: {
                    "votes_value": x.votes_value,
                    "voting_account": x.voting_account
                }, votes))

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            task_load_votes()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1))  # 10 votes + 3 delegations + 1 delegated vote
        self.assertEqual(VoteSnapshot.objects.count(), 15)
        self.assertEqual(VoteSnapshot.objects.filter(is_delegated=True).count(), 3)
        self.assertEqual(VoteSnapshot.objects.filter(has_delegation=True).count(), 1)

        reward_period = timedelta(hours=1)
        task_pay_rewards(reward_period=reward_period)

        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), 14)
        print(reward_amount, Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'])
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] - reward_amount < 0.01, True)
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] <= reward_amount, True)

    def test_reward_payer_delegated_with_own_votes_two_markets(self):
        market_key = MarketKey(market_key=Keypair.random().public_key)
        market_key.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        builder = self._get_builder(self.account_1)

        start_at = timezone.now()
        bribe = AggregatedByAssetBribe(
            market_key_id=market_key,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe.save()

        task_make_claims_snapshot()
        claims_before = ClaimableBalance.objects.count()
        ClaimableBalance.objects.all().delete()
        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        votes = []
        accounts = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts.append(voting_account)
            votes.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key,
                )
            )
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=1000, builder=builder,
            )

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=market_key.market_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
            ]
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        delegation_voting_account = Keypair.random()
        print(delegation_voting_account.public_key)
        accounts.append(delegation_voting_account)
        builder = self._create_wallet(self.account_1, delegation_voting_account, 10, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegation_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.delegated_asset, builder=builder)
        builder = self._trust_asset(delegation_voting_account, self.reward_asset, builder=builder)
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegated_asset, amount=1000, builder=builder,
        )
        builder = self._payment(
            random_asset_issuer, delegation_voting_account, self.delegation_asset, amount=1000, builder=builder,
        )
        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_delegated_votes = 30
        builder = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=delegation_voting_account_delegated_votes, builder=builder)

        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_own_votes = 200
        builder = self._send_claim(delegation_voting_account, claimants, self.delegated_asset, amount=delegation_voting_account_own_votes, builder=builder)

        votes.append(
            VoteSnapshot(
                votes_value=delegation_voting_account_delegated_votes + delegation_voting_account_own_votes,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key,
                is_delegated=False,
            )
        )

        for i in range(3):
            voting_account = Keypair.random()
            accounts.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder = self._create_wallet(self.account_1, voting_account, 5, builder=builder)
            builder = self._trust_asset(voting_account, self.delegated_asset, builder=builder)
            builder = self._trust_asset(voting_account, self.reward_asset, builder=builder)
            builder = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder,
            )
            builder = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder)

        builder_2 = self._get_builder(self.account_2)
        market_key_2 = MarketKey(market_key=Keypair.random().public_key)
        market_key_2.save()
        snapshot_time = timezone.now()
        snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        start_at = timezone.now()
        bribe_2 = AggregatedByAssetBribe(
            market_key_id=market_key_2,
            asset_code=self.reward_asset.code,
            asset_issuer=self.reward_asset.issuer,
            start_at=start_at - timedelta(days=1),
            stop_at=start_at + timedelta(days=1),
            total_reward_amount=100000,
        )
        bribe_2.save()

        task_make_claims_snapshot()
        claims_before = ClaimableBalance.objects.count()
        ClaimableBalance.objects.all().delete()
        task_make_trustees_snapshot()
        holders_before = AssetHolderBalanceSnapshot.objects.count()
        AssetHolderBalanceSnapshot.objects.all().delete()

        votes_2 = []
        accounts_2 = []
        for i in range(10):
            voting_account = Keypair.random()
            accounts_2.append(voting_account)
            votes_2.append(
                VoteSnapshot(
                    votes_value=100,
                    voting_account=voting_account.public_key,
                    snapshot_time=snapshot_time,
                    market_key=market_key_2,
                )
            )
            builder_2 = self._create_wallet(self.account_2, voting_account, 5, builder=builder_2)
            builder_2 = self._trust_asset(voting_account, self.delegated_asset, builder=builder_2)
            builder_2 = self._trust_asset(voting_account, self.reward_asset, builder=builder_2)
            builder_2 = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=1000, builder=builder_2,
            )

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=market_key_2.market_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
            ]
            builder_2 = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder_2)

        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key_2.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_delegated_votes_2 = 50
        builder_2 = self._send_claim(delegation_voting_account, claimants, self.delegation_asset, amount=delegation_voting_account_delegated_votes_2, builder=builder_2)

        claim_after = timezone.now() + timedelta(seconds=1)
        claim_after_timestamp = int(claim_after.strftime("%s"))
        claimants = [
            Claimant(
                destination=delegation_voting_account.public_key,
                predicate=ClaimPredicate.predicate_not(
                    ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                ),
            ),
            Claimant(
                destination=market_key_2.market_key,
                predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
            ),
        ]
        delegation_voting_account_own_votes_2 = 170
        builder_2 = self._send_claim(delegation_voting_account, claimants, self.delegated_asset, amount=delegation_voting_account_own_votes_2, builder=builder_2)

        votes_2.append(
            VoteSnapshot(
                votes_value=delegation_voting_account_delegated_votes_2 + delegation_voting_account_own_votes_2,
                voting_account=delegation_voting_account.public_key,
                snapshot_time=snapshot_time,
                market_key=market_key_2,
                is_delegated=False,
            )
        )

        for i in range(3):
            voting_account = Keypair.random()
            accounts_2.append(voting_account)

            claim_after = timezone.now() + timedelta(seconds=1)
            claim_after_timestamp = int(claim_after.strftime("%s"))
            claimants = [
                Claimant(
                    destination=voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(
                        ClaimPredicate.predicate_before_absolute_time(claim_after_timestamp)
                    ),
                ),
                Claimant(
                    destination=delegation_voting_account.public_key,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                ),
                Claimant(
                    destination=settings.DELEGATE_MARKER,
                    predicate=ClaimPredicate.predicate_not(ClaimPredicate.predicate_unconditional()),
                )
            ]
            builder_2 = self._create_wallet(self.account_2, voting_account, 5, builder=builder_2)
            builder_2 = self._trust_asset(voting_account, self.delegated_asset, builder=builder_2)
            builder_2 = self._trust_asset(voting_account, self.reward_asset, builder=builder_2)
            builder_2 = self._payment(
                random_asset_issuer, voting_account, self.delegated_asset, amount=100, builder=builder_2,
            )
            builder_2 = self._send_claim(voting_account, claimants, self.delegated_asset, amount=100, builder=builder_2)

        transaction_envelope = builder.build()
        transaction_envelope_2 = builder_2.build()

        for i in range(len(accounts)):
            transaction_envelope.sign(accounts[i].secret)
        for i in range(len(accounts_2)):
            transaction_envelope_2.sign(accounts_2[i].secret)

        transaction_envelope.sign(self.account_1.secret)
        transaction_envelope_2.sign(self.account_2.secret)
        transaction_envelope_2.sign(delegation_voting_account.secret)
        # transaction_envelope.sign(self.asset_xxx_issuer.secret)
        transaction_envelope.sign(random_asset_issuer.secret)
        transaction_envelope_2.sign(random_asset_issuer.secret)

        response = self.server.submit_transaction(transaction_envelope)
        self.assertEqual(response['successful'], True)
        response = self.server.submit_transaction(transaction_envelope_2)
        self.assertEqual(response['successful'], True)

        task_make_claims_snapshot()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1) * 2)  # 10 votes + 3 delegations + 1 delegated vote

        task_make_trustees_snapshot()
        self.assertEqual(AssetHolderBalanceSnapshot.objects.count() - holders_before, 26 + 1)  # 13 votes + bribe wallet

        def vote_loading_mock(self, page):
            if page and page > 1:
                return []
            else:
                if self.market_key == market_key.market_key:
                    return list(map(lambda x: {
                        "votes_value": x.votes_value,
                        "voting_account": x.voting_account
                    }, votes))
                elif self.market_key == market_key_2.market_key:
                    return list(map(lambda x: {
                        "votes_value": x.votes_value,
                        "voting_account": x.voting_account
                    }, votes_2))

        with mock.patch.object(VotesLoader, '_get_page', new=vote_loading_mock):
            task_load_votes()

        self.assertEqual(claims_before, ClaimableBalance.objects.count() - (10 + 3 + 1 + 1) * 2)  # 10 votes + 3 delegations + 1 delegated vote
        self.assertEqual(VoteSnapshot.objects.count(), 36)  # 20 true votes + 2 votes from delegate owner + 2 delegations + 6 * 2 (6 delegators and 2 markets)
        self.assertEqual(VoteSnapshot.objects.filter(is_delegated=True).count(), 12)  # 6 * 2 (6 delegators and 2 markets)
        self.assertEqual(VoteSnapshot.objects.filter(has_delegation=True).count(), 2)

        reward_period = timedelta(hours=1)
        task_pay_rewards(reward_period=reward_period)

        reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))
        reward_amount_2 = bribe_2.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))

        Payout.objects.values_list('status', flat=True).distinct()
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().count(), 1)
        self.assertEqual(Payout.objects.values_list('status', flat=True).distinct().first(), Payout.STATUS_SUCCESS)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), 34)
        self.assertEqual(Payout.objects.filter(status=Payout.STATUS_SUCCESS).count(), VoteSnapshot.objects.exclude(has_delegation=True).count())
        self.assertEqual(
            Payout.objects.filter(
                status=Payout.STATUS_SUCCESS,
                vote_snapshot__voting_account=delegation_voting_account.public_key,
                vote_snapshot__market_key=market_key_2.market_key,
            ).first().reward_amount - reward_amount_2 * (delegation_voting_account_own_votes_2) / sum(map(lambda x: x.votes_value, votes_2)) < 0.01, True
        )
        self.assertEqual(
            sum(
                map(
                    lambda x: x.reward_amount,
                    Payout.objects.filter(
                        status=Payout.STATUS_SUCCESS,
                        vote_snapshot__voting_account__in=list(map(lambda x: x.public_key, accounts_2[-3:])),
                        vote_snapshot__market_key=market_key_2.market_key,
                    ),
                ),
            ) - reward_amount_2 * (delegation_voting_account_delegated_votes_2) / sum(map(lambda x: x.votes_value, votes_2)) < 0.01, True,
        )
        print(reward_amount + reward_amount_2, Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'])
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] - reward_amount - reward_amount_2 < 0.01, True)
        self.assertEqual(Payout.objects.aggregate(total=models.Sum('reward_amount'))['total'] <= reward_amount + reward_amount_2, True)

class RewardPayerResilienceTests(TestCase):
    def tearDown(self):
        from django.core.cache import cache as _cache
        from aquarius_bribes.rewards.tasks import (
            LOAD_VOTES_TASK_ACTIVE_KEY,
            LOAD_TRUSTORS_TASK_ACTIVE_KEY,
            PAY_REWARDS_TASK_ACTIVE_KEY,
        )

        for k in (
            LOAD_VOTES_TASK_ACTIVE_KEY,
            LOAD_TRUSTORS_TASK_ACTIVE_KEY,
            PAY_REWARDS_TASK_ACTIVE_KEY,
        ):
            _cache.delete(k)

    def _make_market(self, key=None, asset1=None, asset2=None):
        from stellar_sdk import Keypair

        market = MarketKey.objects.create(
            market_key=key or Keypair.random().public_key,
        )
        if asset1 is not None or asset2 is not None:
            if asset1 is not None:
                market.raw_asset1 = asset1
            if asset2 is not None:
                market.raw_asset2 = asset2
            market.save(update_fields=["raw_asset1", "raw_asset2"])
        return market

    def _make_bribe(
        self,
        market,
        asset_code="native",
        asset_issuer="",
        start=None,
        stop=None,
        total=Decimal("700"),
    ):
        start = start or timezone.now() - timedelta(days=1)
        stop = stop or start + timedelta(days=7)
        return AggregatedByAssetBribe.objects.create(
            market_key=market,
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            start_at=start,
            stop_at=stop,
            total_reward_amount=total,
        )

    def _make_day_bribe(self, market, snapshot_date, **kwargs):
        # _make_bribe with start/stop defaulted to the [D-1, D+2] window
        # spanning snapshot_date — the shape used by every reconcile test
        # that needs a bribe active on a specific UTC day.
        kwargs.setdefault(
            "start", self._at(snapshot_date, 0) - timedelta(days=1),
        )
        kwargs.setdefault(
            "stop", self._at(snapshot_date, 0) + timedelta(days=2),
        )
        return self._make_bribe(market, **kwargs)

    def _make_payout(
        self,
        bribe,
        vote,
        tx_hash,
        amount,
        status=Payout.STATUS_SUCCESS,
        asset_code=None,
        asset_issuer="",
    ):
        # Defaults to a SUCCESS Payout on the native asset; callers
        # override asset_code/asset_issuer for non-native bribes and
        # pass status=Payout.STATUS_FAILED for the drift paths.
        return Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            stellar_transaction_id=tx_hash,
            status=status,
            reward_amount=Decimal(amount)
            if not isinstance(amount, Decimal)
            else amount,
            asset_code=asset_code or Asset.native().code,
            asset_issuer=asset_issuer,
        )

    def _make_vote(
        self, market, account, snapshot_date, votes_value, has_delegation=False
    ):
        return VoteSnapshot.objects.create(
            market_key=market,
            voting_account=account,
            votes_value=Decimal(votes_value),
            snapshot_time=snapshot_date,
            has_delegation=has_delegation,
        )

    def _make_holder(self, account, asset_code, asset_issuer, created_at):
        snap = AssetHolderBalanceSnapshot.objects.create(
            account=account,
            asset_code=asset_code,
            asset_issuer=asset_issuer,
            balance=Decimal("1"),
        )
        AssetHolderBalanceSnapshot.objects.filter(pk=snap.pk).update(
            created_at=created_at
        )
        return snap

    def _at(self, snapshot_date, hour):
        return timezone.make_aware(datetime.combine(snapshot_date, time(hour=hour)))

    def _make_dust_votes(self, market, snapshot_date):
        regular_votes = [
            self._make_vote(
                market, Keypair.random().public_key, snapshot_date, "250000"
            )
            for _ in range(4)
        ]
        dust_vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "1"
        )
        return regular_votes, dust_vote

    def _old_payable_vote_ids(self, bribe, snapshot_date, reward_amount):
        votes = VoteSnapshot.objects.filter(
            snapshot_time=snapshot_date,
            market_key=bribe.market_key,
        )

        if bribe.asset.type != Asset.native().type:
            votes = votes.filter(
                voting_account__in=AssetHolderBalanceSnapshot.objects.filter(
                    created_at__gte=snapshot_date,
                    created_at__lt=snapshot_date + timedelta(days=1),
                    asset_code=bribe.asset_code,
                    asset_issuer=bribe.asset_issuer,
                ).values_list("account"),
            )

        votes = votes.exclude(has_delegation=True)

        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]
        if reward_amount is not None and total_votes and total_votes > 0:
            min_votes_value = Decimal(
                Decimal("0.0000001") * total_votes / Decimal(reward_amount),
            ).quantize(Decimal("0.0000001"), rounding=ROUND_UP)
            votes = votes.filter(votes_value__gte=min_votes_value)

        return set(votes.values_list("id", flat=True))

    def test_eligibility_parity_with_task_pay_rewards(self):
        snapshot_date = timezone.now().date()
        reward_amount = Decimal("0.0001000")

        native_market = self._make_market()
        native_bribe = self._make_bribe(
            native_market,
            asset_code=Asset.native().code,
            asset_issuer="",
            total=Decimal("0.0007000"),
        )
        self._make_vote(
            native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        self._make_vote(
            native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        self._make_vote(
            native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        self._make_vote(native_market, Keypair.random().public_key, snapshot_date, "1")
        self._make_vote(
            native_market,
            Keypair.random().public_key,
            snapshot_date,
            "250000",
            has_delegation=True,
        )

        expected_native_ids = self._old_payable_vote_ids(
            native_bribe, snapshot_date, reward_amount
        )
        native_votes, _ = get_payable_votes(
            native_bribe,
            snapshot_date,
            reward_amount=reward_amount,
        )
        self.assertSetEqual(
            expected_native_ids, set(native_votes.values_list("id", flat=True))
        )

        issuer = Keypair.random().public_key
        non_native_market = self._make_market()
        non_native_bribe = self._make_bribe(
            non_native_market,
            asset_code="AQUA",
            asset_issuer=issuer,
            total=Decimal("0.0007000"),
        )

        valid_vote_1 = self._make_vote(
            non_native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        valid_vote_2 = self._make_vote(
            non_native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        late_trustline_vote = self._make_vote(
            non_native_market, Keypair.random().public_key, snapshot_date, "250000"
        )
        dust_vote = self._make_vote(
            non_native_market, Keypair.random().public_key, snapshot_date, "1"
        )
        delegated_vote = self._make_vote(
            non_native_market,
            Keypair.random().public_key,
            snapshot_date,
            "250000",
            has_delegation=True,
        )

        for vote in [valid_vote_1, valid_vote_2, dust_vote, delegated_vote]:
            self._make_holder(
                vote.voting_account, "AQUA", issuer, self._at(snapshot_date, 12)
            )
        self._make_holder(
            late_trustline_vote.voting_account,
            "AQUA",
            issuer,
            self._at(snapshot_date + timedelta(days=1), 1),
        )

        expected_non_native_ids = self._old_payable_vote_ids(
            non_native_bribe, snapshot_date, reward_amount
        )
        non_native_votes, _ = get_payable_votes(
            non_native_bribe,
            snapshot_date,
            reward_amount=reward_amount,
        )
        self.assertSetEqual(
            expected_non_native_ids, set(non_native_votes.values_list("id", flat=True))
        )

    def test_eligibility_non_native_requires_trustline(self):
        snapshot_date = timezone.now().date()
        market = self._make_market()
        issuer = Keypair.random().public_key
        bribe = self._make_bribe(
            market,
            asset_code="AQUA",
            asset_issuer=issuer,
        )
        valid_vote_1 = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        valid_vote_2 = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "200"
        )
        outside_window_vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "300"
        )

        self._make_holder(
            valid_vote_1.voting_account, "AQUA", issuer, self._at(snapshot_date, 1)
        )
        self._make_holder(
            valid_vote_2.voting_account, "AQUA", issuer, self._at(snapshot_date, 23)
        )
        self._make_holder(
            outside_window_vote.voting_account,
            "AQUA",
            issuer,
            self._at(snapshot_date + timedelta(days=1), 1),
        )

        returned_votes, total_votes = get_payable_votes(bribe, snapshot_date)

        self.assertSetEqual(
            set(returned_votes.values_list("id", flat=True)),
            {valid_vote_1.id, valid_vote_2.id},
        )
        self.assertEqual(
            total_votes, valid_vote_1.votes_value + valid_vote_2.votes_value
        )
        self.assertNotIn(
            outside_window_vote.id, returned_votes.values_list("id", flat=True)
        )

    def test_eligibility_dust_filter_with_reward_amount(self):
        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market,
            asset_code=Asset.native().code,
            asset_issuer="",
            total=Decimal("0.0007000"),
        )
        regular_votes, dust_vote = self._make_dust_votes(market, snapshot_date)

        returned_votes, _ = get_payable_votes(
            bribe,
            snapshot_date,
            reward_amount=bribe.daily_amount,
        )

        self.assertEqual(returned_votes.count(), len(regular_votes))
        self.assertNotIn(dust_vote.id, returned_votes.values_list("id", flat=True))

    def _make_rewardpayer_with_mocked_server(self, bribe, mock_server):
        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        return payer

    def test_process_page_build_failure_logs_sentry_and_persists_failed_payouts(self):
        import aquarius_bribes.rewards.reward_payer as rp_module

        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        for _ in range(3):
            self._make_vote(
                market, Keypair.random().public_key, snapshot_date, "100000"
            )

        mock_server = mock.MagicMock()
        mock_server.load_account = mock.MagicMock(
            side_effect=RuntimeError("horizon boom")
        )
        payer = self._make_rewardpayer_with_mocked_server(bribe, mock_server)

        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        with mock.patch.object(rp_module, "sentry_sdk") as mock_sentry:
            payer.pay_reward(votes, total_votes=total_votes)

        mock_sentry.capture_exception.assert_called()
        call_arg = mock_sentry.capture_exception.call_args[0][0]
        self.assertIsInstance(call_arg, RuntimeError)
        self.assertIn("horizon boom", str(call_arg))

        payouts = Payout.objects.filter(bribe=bribe)
        self.assertEqual(payouts.count(), 3)
        for payout in payouts:
            self.assertEqual(payout.status, Payout.STATUS_FAILED)
            self.assertEqual(payout.stellar_transaction_id, "")
            self.assertTrue(payout.message.startswith("build_failure: RuntimeError:"))
            self.assertIn("horizon boom", payout.message)

    def test_process_page_build_failure_allows_next_bribe_to_run(self):
        import aquarius_bribes.rewards.reward_payer as rp_module

        snapshot_date = timezone.now().date()

        # Bribe 1 — build will fail
        market1 = self._make_market()
        bribe1 = self._make_bribe(
            market1, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(market1, Keypair.random().public_key, snapshot_date, "100000")

        # Bribe 2 — build will succeed
        market2 = self._make_market()
        bribe2 = self._make_bribe(
            market2, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(market2, Keypair.random().public_key, snapshot_date, "200000")

        # Mock envelope for bribe2
        mock_envelope2 = mock.MagicMock()
        mock_envelope2.sign = mock.MagicMock()
        mock_envelope2.hash_hex = mock.MagicMock(return_value="hexhex2")

        # Payer for bribe2 — submit_transaction returns success
        mock_server2 = mock.MagicMock()
        mock_server2.submit_transaction = mock.MagicMock(
            return_value={"successful": True, "hash": "aabbcc"}
        )

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer1 = RewardPayer(bribe1, wallet, bribe1.asset, Decimal("100"), stop_at=None)
        payer2 = RewardPayer(bribe2, wallet, bribe2.asset, Decimal("100"), stop_at=None)
        payer2.server = mock_server2

        votes1 = VoteSnapshot.objects.filter(
            market_key=market1, snapshot_time=snapshot_date
        )
        total1 = votes1.aggregate(total=models.Sum("votes_value"))["total"]
        votes2 = VoteSnapshot.objects.filter(
            market_key=market2, snapshot_time=snapshot_date
        )
        total2 = votes2.aggregate(total=models.Sum("votes_value"))["total"]

        with mock.patch.object(rp_module, "sentry_sdk"):
            with mock.patch.object(
                payer1, "_build_transaction", side_effect=RuntimeError("bribe1 fails")
            ):
                payer1.pay_reward(votes1, total_votes=total1)

        with mock.patch.object(
            payer2, "_build_transaction", return_value=mock_envelope2
        ):
            payer2.pay_reward(votes2, total_votes=total2)

        bribe2_payouts = Payout.objects.filter(bribe=bribe2)
        self.assertEqual(bribe2_payouts.count(), 1)
        for payout in bribe2_payouts:
            self.assertEqual(payout.status, Payout.STATUS_SUCCESS)
            self.assertEqual(payout.stellar_transaction_id, "aabbcc")

    def test_process_page_unknown_response_treated_as_failure(self):
        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        for _ in range(2):
            self._make_vote(
                market, Keypair.random().public_key, snapshot_date, "100000"
            )

        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="hexhex")

        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(
            return_value={"hash": "deadbeef"}
        )

        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server

        with mock.patch.object(payer, "_build_transaction", return_value=mock_envelope):
            payer.pay_reward(votes, total_votes=total_votes)

        payouts = Payout.objects.filter(bribe=bribe)
        self.assertEqual(payouts.count(), 2)
        for payout in payouts:
            self.assertEqual(payout.status, Payout.STATUS_FAILED)
            self.assertEqual(payout.message, "unknown_response_no_successful_field")

    def test_cache_flag_ttl_applied(self):
        from aquarius_bribes.rewards.tasks import (
            LOAD_TRUSTORS_TASK_ACTIVE_KEY,
            LOAD_TRUSTORS_TASK_TTL,
            LOAD_VOTES_TASK_ACTIVE_KEY,
            LOAD_VOTES_TASK_TTL,
        )

        with mock.patch("aquarius_bribes.rewards.tasks.cache") as mock_cache:
            with mock.patch("aquarius_bribes.rewards.tasks.task_make_claims_snapshot"):
                with mock.patch("aquarius_bribes.rewards.tasks.VotesLoader"):
                    with mock.patch(
                        "aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects"
                    ) as mock_objects:
                        mock_objects.filter.return_value.values_list.return_value.distinct.return_value = []
                        task_load_votes()

        mock_cache.set.assert_any_call(
            LOAD_VOTES_TASK_ACTIVE_KEY, True, LOAD_VOTES_TASK_TTL
        )

        with mock.patch("aquarius_bribes.rewards.tasks.cache") as mock_cache:
            with mock.patch("aquarius_bribes.rewards.tasks.TrusteesLoader"):
                with mock.patch(
                    "aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects"
                ) as mock_objects:
                    mock_objects.filter.return_value = []
                    task_make_trustees_snapshot()

        mock_cache.set.assert_any_call(
            LOAD_TRUSTORS_TASK_ACTIVE_KEY, True, LOAD_TRUSTORS_TASK_TTL
        )

    def test_pay_rewards_sets_and_clears_active_flag(self):
        from django.core.cache import cache
        from aquarius_bribes.rewards.tasks import PAY_REWARDS_TASK_ACTIVE_KEY

        def assert_flag_set(*args, **kwargs):
            self.assertTrue(cache.get(PAY_REWARDS_TASK_ACTIVE_KEY, False))
            return []

        with mock.patch("aquarius_bribes.rewards.tasks.SecuredWallet"):
            with mock.patch(
                "aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects.filter",
                side_effect=assert_flag_set,
            ):
                task_pay_rewards()

        self.assertFalse(cache.get(PAY_REWARDS_TASK_ACTIVE_KEY, False))

        with mock.patch("aquarius_bribes.rewards.tasks.SecuredWallet"):
            with mock.patch(
                "aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects.filter",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError):
                    task_pay_rewards()

        self.assertFalse(cache.get(PAY_REWARDS_TASK_ACTIVE_KEY, False))

    def test_pay_rewards_refuses_when_own_key_already_set(self):
        # Self-exclusion: if another task_pay_rewards run already owns
        # PAY_REWARDS_TASK_ACTIVE_KEY, a second invocation must return
        # without computing the payable set — otherwise two workers
        # double-submit payouts to the same voter.
        from django.core.cache import cache
        from aquarius_bribes.rewards.tasks import PAY_REWARDS_TASK_ACTIVE_KEY

        cache.set(PAY_REWARDS_TASK_ACTIVE_KEY, "other-owner", 60)
        try:
            with mock.patch(
                "aquarius_bribes.rewards.tasks.AggregatedByAssetBribe.objects.filter"
            ) as mock_filter:
                task_pay_rewards()

            self.assertFalse(mock_filter.called)
            # The other owner's token must remain — a refused run never
            # clears a key it does not own.
            self.assertEqual(
                cache.get(PAY_REWARDS_TASK_ACTIVE_KEY), "other-owner"
            )
        finally:
            cache.delete(PAY_REWARDS_TASK_ACTIVE_KEY)

    @override_settings(
        PAYOUT_COMPLETENESS_ALERT_ENABLED=True,
        PAYOUT_COMPLETENESS_THRESHOLD_PCT=5,
    )

    def test_pay_reward_build_failure_exits_without_infinite_loop(self):
        # Within a single pay_reward run, a persistent build_failure must not
        # re-enqueue the same page forever even though _clean_rewards now
        # treats build_failure: payouts as retryable across runs.
        market = self._make_market()
        bribe = self._make_bribe(market)
        snapshot_date = timezone.now().date()
        for _ in range(3):
            self._make_vote(
                market, Keypair.random().public_key, snapshot_date, "100"
            )
        wallet = SecuredWallet(
            public_key=Keypair.random().public_key, secret=None
        )
        asset = Asset.native()
        payer = RewardPayer(bribe, wallet, asset, bribe.total_reward_amount)
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )

        with mock.patch.object(
            payer, "_build_transaction",
            side_effect=RuntimeError("horizon upstream slow"),
        ) as mock_build:
            payer.pay_reward(votes)

        self.assertEqual(mock_build.call_count, 1)
        self.assertEqual(
            Payout.objects.filter(
                bribe=bribe, status=Payout.STATUS_FAILED,
                message__startswith="build_failure:",
            ).count(),
            3,
        )

    def test_clean_rewards_retries_build_failure_payouts(self):
        market = self._make_market()
        bribe = self._make_bribe(market)
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="",
            status=Payout.STATUS_FAILED,
            message="build_failure: TimeoutError: horizon upstream slow",
        )
        wallet = SecuredWallet(
            public_key=Keypair.random().public_key, secret=None
        )
        asset = (
            Asset.native()
            if bribe.asset_code == "native"
            else Asset(bribe.asset_code, bribe.asset_issuer)
        )
        payer = RewardPayer(bribe, wallet, asset, bribe.total_reward_amount)
        remaining = payer._clean_rewards(
            VoteSnapshot.objects.filter(
                market_key=market, snapshot_time=snapshot_date
            )
        )
        self.assertIn(vote, list(remaining))

    def test_clean_rewards_still_excludes_terminal_failed_payouts(self):
        # Regression guard: op_no_trust remains terminal and the build_failure
        # carve-out must not re-open it.
        market = self._make_market()
        bribe = self._make_bribe(market)
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="deadbeef",
            status=Payout.STATUS_FAILED,
            message="op_no_trust",
        )
        wallet = SecuredWallet(
            public_key=Keypair.random().public_key, secret=None
        )
        asset = (
            Asset.native()
            if bribe.asset_code == "native"
            else Asset(bribe.asset_code, bribe.asset_issuer)
        )
        payer = RewardPayer(bribe, wallet, asset, bribe.total_reward_amount)
        remaining = payer._clean_rewards(
            VoteSnapshot.objects.filter(
                market_key=market, snapshot_time=snapshot_date
            )
        )
        self.assertNotIn(vote, list(remaining))

    def test_task_pay_rewards_reuses_asset_holder_cache(self):
        # X4: get_payable_votes must be called with the same asset_holder_cache
        # dict across bribes — verifiable by checking the cache grows across calls
        # rather than starting fresh each time.
        from aquarius_bribes.rewards.eligibility import get_payable_votes as real_gpv

        snapshot_date = timezone.now().date()
        market1 = self._make_market()
        market2 = self._make_market()
        issuer = Keypair.random().public_key
        asset_code = "AQUA"

        for market in (market1, market2):
            AggregatedByAssetBribe.objects.create(
                market_key=market,
                asset_code=asset_code,
                asset_issuer=issuer,
                start_at=timezone.now() - timedelta(hours=1),
                stop_at=timezone.now() + timedelta(hours=1),
                total_reward_amount=Decimal("700"),
            )
            self._make_vote(market, Keypair.random().public_key, snapshot_date, "1000")
            self._make_holder(
                Keypair.random().public_key, asset_code, issuer, self._at(snapshot_date, 12)
            )

        calls = []

        def spy_gpv(bribe, snap_date, reward_amount=None, asset_holder_cache=None):
            calls.append(asset_holder_cache)
            return real_gpv(bribe, snap_date, reward_amount=reward_amount, asset_holder_cache=asset_holder_cache)

        with mock.patch("aquarius_bribes.rewards.tasks.get_payable_votes", side_effect=spy_gpv):
            with mock.patch("aquarius_bribes.rewards.tasks.SecuredWallet"):
                with mock.patch("aquarius_bribes.rewards.tasks.RewardPayer"):
                    task_pay_rewards()

        # All calls must have received the same cache dict object (not None).
        self.assertGreaterEqual(len(calls), 2)
        self.assertIsNotNone(calls[0])
        # Verify they're all the same dict instance — cache is shared.
        first_cache = calls[0]
        for c in calls[1:]:
            self.assertIs(c, first_cache)

    def test_unknown_response_failed_payouts_remain_held(self):
        # A missing confirmation must not expose a submitted vote to another
        # payment. Reconciliation must establish the outcome first.
        market = self._make_market()
        bribe = self._make_bribe(market, asset_code=Asset.native().code, asset_issuer="")
        snapshot_date = timezone.now().date()
        vote = self._make_vote(market, Keypair.random().public_key, snapshot_date, "100")
        Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="deadbeef",
            status=Payout.STATUS_FAILED,
            message="unknown_response_no_successful_field",
        )
        wallet = SecuredWallet(public_key=Keypair.random().public_key, secret=None)
        asset = Asset.native()
        payer = RewardPayer(bribe, wallet, asset, bribe.total_reward_amount)
        remaining = payer._clean_rewards(
            VoteSnapshot.objects.filter(market_key=market, snapshot_time=snapshot_date)
        )
        self.assertNotIn(vote, list(remaining))

    def test_clean_failed_payouts_rechecks_unknown_response_under_5_minutes(self):
        # X1 (2026-04-24 audit): bypass the 5-min timeout grace for
        # `unknown_response_no_successful_field` so an operator's manual
        # re-run during incident response re-verifies the hash before
        # _clean_rewards exposes the voter to a fresh submit (which would
        # double-pay if the original tx actually landed).
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        payout = Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="landedhash",
            status=Payout.STATUS_FAILED,
            message="unknown_response_no_successful_field",
        )
        # Payout is fresh (<5 min old); the old code path would skip it.
        tx_call = mock.MagicMock()
        tx_call.call = mock.MagicMock(return_value={"successful": True})
        transactions_builder = mock.MagicMock()
        transactions_builder.transaction = mock.MagicMock(return_value=tx_call)
        mock_server = mock.MagicMock()
        mock_server.transactions = mock.MagicMock(return_value=transactions_builder)

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, bribe.total_reward_amount)
        payer.server = mock_server

        payer._clean_failed_payouts(
            VoteSnapshot.objects.filter(market_key=market, snapshot_time=snapshot_date)
        )

        transactions_builder.transaction.assert_called_once_with("landedhash")
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.STATUS_SUCCESS)

    def test_clean_failed_payouts_keeps_timeout_5_minute_grace(self):
        # X1 (2026-04-24 audit) regression: `timeout` retains the 5-min
        # grace so the re-check doesn't race Horizon's own indexing lag
        # on a tx that is still mid-landing.
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        payout = Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="pendinghash",
            status=Payout.STATUS_FAILED,
            message="timeout",
        )
        mock_server = mock.MagicMock()
        mock_server.transactions = mock.MagicMock()

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, bribe.total_reward_amount)
        payer.server = mock_server

        payer._clean_failed_payouts(
            VoteSnapshot.objects.filter(market_key=market, snapshot_time=snapshot_date)
        )

        mock_server.transactions.assert_not_called()
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.STATUS_FAILED)
        self.assertEqual(payout.message, "timeout")

    def test_process_page_connection_error_persists_retryable_timeout(self):
        # X3 (2026-04-24 audit): `stellar_sdk.exceptions.ConnectionError` is
        # a sibling of BaseHorizonError, not a subclass — prior to this
        # fix it fell through to the generic `except Exception` branch and
        # wrote `message=str(exc)`, which is neither re-checked by
        # _clean_failed_payouts nor in the _clean_rewards retryable set, so
        # the voter was silently dropped from every future run.
        from stellar_sdk.exceptions import ConnectionError as StellarConnectionError

        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100000"
        )
        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="envhash")
        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(
            side_effect=StellarConnectionError("connection reset")
        )
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        with mock.patch.object(payer, "_build_transaction", return_value=mock_envelope):
            payer.pay_reward(votes, total_votes=total_votes)

        payouts = list(Payout.objects.filter(bribe=bribe))
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0].status, Payout.STATUS_FAILED)
        self.assertEqual(payouts[0].message, "timeout")
        self.assertEqual(payouts[0].stellar_transaction_id, "envhash")

    def test_process_page_horizon_error_empty_extras_is_retryable(self):
        # X3 (2026-04-24 audit): BaseHorizonError with extras=None (Horizon
        # returned a non-JSON body — typically a 500 HTML page) used to
        # persist `message='no_reason'`, which is neither re-checked nor in
        # the retryable set. Must become `unknown_response_no_successful_field`
        # so the next run re-verifies the hash on Horizon.
        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100000"
        )
        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="hexhex")
        exc = BaseHorizonError.__new__(BaseHorizonError)
        exc.extras = None
        exc.status = 500
        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(side_effect=exc)
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        with mock.patch.object(payer, "_build_transaction", return_value=mock_envelope):
            payer.pay_reward(votes, total_votes=total_votes)

        payouts = list(Payout.objects.filter(bribe=bribe))
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0].status, Payout.STATUS_FAILED)
        # X4 fix: `timeout` (not `unknown_response_no_successful_field`) so
        # `_clean_failed_payouts` keeps the 5-min grace before re-checking.
        # Without the grace, a transient NotFoundError on the very next run
        # would `.delete()` this row and let a fresh submit double-pay once
        # the original tx propagates.
        self.assertEqual(payouts[0].message, "timeout")
        self.assertEqual(payouts[0].stellar_transaction_id, "hexhex")

    def test_process_page_unexpected_exception_persists_recheckable(self):
        # X3 (2026-04-24 audit): anything reaching the generic `except
        # Exception` branch after envelope signing must persist a message
        # that triggers Horizon re-verification — otherwise a single
        # unexpected error type removes the voter from all future payouts.
        import aquarius_bribes.rewards.reward_payer as rp_module

        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100000"
        )
        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="unexp-hash")
        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(
            side_effect=RuntimeError("surprise")
        )
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        with mock.patch.object(rp_module, "sentry_sdk") as mock_sentry:
            with mock.patch.object(
                payer, "_build_transaction", return_value=mock_envelope
            ):
                payer.pay_reward(votes, total_votes=total_votes)

        mock_sentry.capture_exception.assert_called()
        payouts = list(Payout.objects.filter(bribe=bribe))
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0].status, Payout.STATUS_FAILED)
        # X4 fix: `timeout` not `unknown_response_no_successful_field` — the
        # generic Exception branch has no guarantee the tx reached Horizon,
        # so the 5-min grace is required before `_clean_failed_payouts`
        # re-checks (otherwise transient NotFoundError deletes the row and
        # enables a double-pay when the original tx propagates late).
        self.assertEqual(payouts[0].message, "timeout")
        self.assertEqual(payouts[0].stellar_transaction_id, "unexp-hash")

    def test_horizon_error_empty_extras_fresh_row_survives_hourly_recheck(self):
        # X4 (2026-04-24 audit): a Payout persisted via the BaseHorizonError
        # empty-extras path must NOT be deleted by `_clean_failed_payouts`
        # on a re-run inside the 5-min grace window, even if Horizon returns
        # NotFoundError for its hash. Without the grace the row would be
        # deleted, `_clean_rewards` would re-enqueue the voter, and a fresh
        # submit could double-pay once the original tx propagates.
        from stellar_sdk.exceptions import NotFoundError

        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100000"
        )
        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="pending-hash")
        exc = BaseHorizonError.__new__(BaseHorizonError)
        exc.extras = None
        exc.status = 500
        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(side_effect=exc)
        # Horizon would 404 the hash on immediate re-check (tx hasn't
        # propagated yet). The 5-min grace must prevent the re-check from
        # firing.
        tx_call = mock.MagicMock()
        tx_call.call = mock.MagicMock(side_effect=NotFoundError(mock.MagicMock()))
        transactions_builder = mock.MagicMock()
        transactions_builder.transaction = mock.MagicMock(return_value=tx_call)
        mock_server.transactions = mock.MagicMock(return_value=transactions_builder)
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        with mock.patch.object(payer, "_build_transaction", return_value=mock_envelope):
            payer.pay_reward(votes, total_votes=total_votes)

        # Fresh run persisted one FAILED Payout with message='timeout'.
        self.assertEqual(Payout.objects.filter(bribe=bribe).count(), 1)
        persisted = Payout.objects.get(bribe=bribe)
        self.assertEqual(persisted.message, "timeout")

        # Operator re-runs pay_reward ~1 min later (before the 5-min grace).
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        payer2 = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer2.server = mock_server
        payer2.pay_reward(votes, total_votes=total_votes)

        # Row must still exist: 5-min grace kept `_clean_failed_payouts`
        # away from the hash; _clean_rewards excluded the voter because
        # 'timeout' is NOT in the retryable set. No second submit → no
        # double-pay.
        self.assertEqual(Payout.objects.filter(bribe=bribe).count(), 1)
        self.assertTrue(Payout.objects.filter(pk=persisted.pk).exists())

    def test_unexpected_exception_fresh_row_survives_hourly_recheck(self):
        # X4 (2026-04-24 audit): same guarantee as above for the generic
        # `except Exception` branch — persisted message='timeout' must not
        # be deleted by a <5-min re-run that sees NotFoundError.
        import aquarius_bribes.rewards.reward_payer as rp_module
        from stellar_sdk.exceptions import NotFoundError

        snapshot_date = timezone.now().date()
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100000"
        )
        mock_envelope = mock.MagicMock()
        mock_envelope.sign = mock.MagicMock()
        mock_envelope.hash_hex = mock.MagicMock(return_value="surprise-hash")
        mock_server = mock.MagicMock()
        mock_server.submit_transaction = mock.MagicMock(
            side_effect=RuntimeError("surprise")
        )
        tx_call = mock.MagicMock()
        tx_call.call = mock.MagicMock(side_effect=NotFoundError(mock.MagicMock()))
        transactions_builder = mock.MagicMock()
        transactions_builder.transaction = mock.MagicMock(return_value=tx_call)
        mock_server.transactions = mock.MagicMock(return_value=transactions_builder)
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        total_votes = votes.aggregate(total=models.Sum("votes_value"))["total"]

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer.server = mock_server
        with mock.patch.object(rp_module, "sentry_sdk"):
            with mock.patch.object(payer, "_build_transaction", return_value=mock_envelope):
                payer.pay_reward(votes, total_votes=total_votes)

        persisted = Payout.objects.get(bribe=bribe)
        self.assertEqual(persisted.message, "timeout")

        payer2 = RewardPayer(bribe, wallet, bribe.asset, Decimal("100"), stop_at=None)
        payer2.server = mock_server
        votes = VoteSnapshot.objects.filter(
            market_key=market, snapshot_time=snapshot_date
        )
        with mock.patch.object(rp_module, "sentry_sdk"):
            payer2.pay_reward(votes, total_votes=total_votes)

        self.assertEqual(Payout.objects.filter(bribe=bribe).count(), 1)
        self.assertTrue(Payout.objects.filter(pk=persisted.pk).exists())

    def test_clean_failed_payouts_skips_already_success_timeout_row(self):
        # X4 iter-3 (2026-04-24 audit): a previous run upgraded a FAILED +
        # message='timeout' row to SUCCESS but left the message intact. A
        # later run must NOT re-check the hash (and therefore cannot be
        # tricked into deleting the SUCCESS row on a transient NotFoundError
        # response). The STATUS_FAILED filter guard in _clean_failed_payouts
        # is load-bearing for that guarantee.
        from stellar_sdk.exceptions import NotFoundError

        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        payout = Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="landed-hash",
            status=Payout.STATUS_SUCCESS,
            message="timeout",  # legacy shape from a pre-guard upgrade
        )
        Payout.objects.filter(pk=payout.pk).update(
            created_at=timezone.now() - timedelta(minutes=15),
        )
        # Horizon would 404 this hash on re-fetch (transient node-switch or
        # indexing gap). If the re-check ran it would delete the SUCCESS
        # row, re-enqueue the voter, and enable a double-pay.
        tx_call = mock.MagicMock()
        tx_call.call = mock.MagicMock(side_effect=NotFoundError(mock.MagicMock()))
        transactions_builder = mock.MagicMock()
        transactions_builder.transaction = mock.MagicMock(return_value=tx_call)
        mock_server = mock.MagicMock()
        mock_server.transactions = mock.MagicMock(return_value=transactions_builder)

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, bribe.total_reward_amount)
        payer.server = mock_server

        payer._clean_failed_payouts(
            VoteSnapshot.objects.filter(market_key=market, snapshot_time=snapshot_date)
        )

        # Row must still exist as SUCCESS — the STATUS_FAILED guard kept
        # `uncertain_transactions` from picking it up, and the .delete() /
        # .update() calls also filter by status so they never touch it.
        transactions_builder.transaction.assert_not_called()
        self.assertTrue(Payout.objects.filter(pk=payout.pk).exists())
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.STATUS_SUCCESS)

    def test_clean_failed_payouts_upgrade_stamps_message_reverified(self):
        # X4 iter-3 (2026-04-24 audit): on successful re-verification, the
        # FAILED-with-timeout row is upgraded to SUCCESS with
        # message='reverified_after_timeout'. Future runs then see a
        # non-`timeout`/non-`unknown_response_*` message and would not
        # re-check the hash even if someone later loosens the status guard.
        market = self._make_market()
        bribe = self._make_bribe(
            market, asset_code=Asset.native().code, asset_issuer=""
        )
        snapshot_date = timezone.now().date()
        vote = self._make_vote(
            market, Keypair.random().public_key, snapshot_date, "100"
        )
        payout = Payout.objects.create(
            bribe=bribe,
            vote_snapshot=vote,
            asset_code=bribe.asset_code,
            asset_issuer=bribe.asset_issuer or "",
            reward_amount=Decimal("1.0000000"),
            stellar_transaction_id="landed-hash-2",
            status=Payout.STATUS_FAILED,
            message="timeout",
        )
        Payout.objects.filter(pk=payout.pk).update(
            created_at=timezone.now() - timedelta(minutes=15),
        )
        tx_call = mock.MagicMock()
        tx_call.call = mock.MagicMock(return_value={"successful": True})
        transactions_builder = mock.MagicMock()
        transactions_builder.transaction = mock.MagicMock(return_value=tx_call)
        mock_server = mock.MagicMock()
        mock_server.transactions = mock.MagicMock(return_value=transactions_builder)

        wallet = SecuredWallet(public_key="G" + "A" * 55, secret="S" + "A" * 55)
        payer = RewardPayer(bribe, wallet, bribe.asset, bribe.total_reward_amount)
        payer.server = mock_server

        payer._clean_failed_payouts(
            VoteSnapshot.objects.filter(market_key=market, snapshot_time=snapshot_date)
        )

        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.STATUS_SUCCESS)
        self.assertEqual(payout.message, "reverified_after_timeout")

