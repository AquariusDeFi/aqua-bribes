import logging
import random
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from stellar_sdk import Asset

from aquarius_bribes.bribes.models import AggregatedByAssetBribe
from aquarius_bribes.rewards.claim_loader import ClaimLoader
from aquarius_bribes.rewards.contract_delegation import source_snapshot, validate_source
from aquarius_bribes.rewards.eligibility import get_payable_votes
from aquarius_bribes.rewards.models import ClaimableBalance, ContractDelegationSnapshot, VoteSnapshot
from aquarius_bribes.rewards.reward_payer import RewardPayer
from aquarius_bribes.rewards.trustees_loader import TrusteesLoader
from aquarius_bribes.rewards.utils import SecuredWallet
from aquarius_bribes.rewards.votes_loader import VotesLoader
from aquarius_bribes.taskapp import app as celery_app

logger = logging.getLogger(__name__)

DEFAULT_REWARD_PERIOD = timedelta(hours=24)
PAYREWARD_TIME_LIMIT = timedelta(minutes=55)
LOAD_VOTES_TASK_ACTIVE_KEY = 'LOAD_VOTES_TASK_ACTIVE_KEY'
LOAD_TRUSTORS_TASK_ACTIVE_KEY = 'LOAD_TRUSTORS_TASK_ACTIVE_KEY'
PAY_REWARDS_TASK_ACTIVE_KEY = 'PAY_REWARDS_TASK_ACTIVE_KEY'

LOAD_VOTES_TASK_TTL = 60 * 60 * 2
LOAD_TRUSTORS_TASK_TTL = 60 * 60 * 10
PAY_REWARDS_TASK_TTL = int(PAYREWARD_TIME_LIMIT.total_seconds()) + 60 * 5


@celery_app.task(ignore_result=True)
def task_make_claims_snapshot():
    snapshot_time = timezone.now()
    snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0, hour=0)

    for asset, delegated_asset in settings.DELEGATABLE_ASSETS:
        ClaimableBalance.objects.filter(asset_code=asset.code, asset_issuer=asset.issuer).filter(
            loaded_at__gte=snapshot_time,
            loaded_at__lt=snapshot_time + timedelta(days=1),
        ).delete()
        loader = ClaimLoader(asset)
        loader.make_claim_spanshot()

        ClaimableBalance.objects.filter(asset_code=delegated_asset.code, asset_issuer=delegated_asset.issuer).filter(
            loaded_at__gte=snapshot_time,
            loaded_at__lt=snapshot_time + timedelta(days=1),
        ).delete()
        loader = ClaimLoader(delegated_asset)
        loader.make_claim_spanshot()


@celery_app.task(ignore_result=True, soft_time_limit=60 * 20, time_limit=60 * 30)
def task_run_load_votes():
    hour = random.randint(0, 22)
    task_load_votes.apply_async(countdown=int(hour * timedelta(hours=1).total_seconds()))

    task_make_trustees_snapshot.delay()


@celery_app.task(ignore_result=True, soft_time_limit=60 * 60 * 1, time_limit=60 * (60 * 1 + 5))
def task_load_votes(snapshot_time=None):
    cache.set(LOAD_VOTES_TASK_ACTIVE_KEY, True, LOAD_VOTES_TASK_TTL)
    try:
        if getattr(settings, 'CONTRACT_DELEGATION_SNAPSHOTS_ENABLED', False):
            now = snapshot_time or timezone.now()
            if not AggregatedByAssetBribe.objects.filter(start_at__lte=now, stop_at__gt=now).exists():
                return
            source = source_snapshot(snapshot_time)
            at = validate_source(source.source_id, source.payload)
            markets = sorted(AggregatedByAssetBribe.objects.filter(
                start_at__lte=at, stop_at__gt=at,
            ).values_list('market_key', flat=True).distinct())
            with transaction.atomic():
                source = ContractDelegationSnapshot.objects.select_for_update().get(pk=source.pk)
                if source.votes_loaded:
                    if source.loaded_markets != markets:
                        raise ValueError('Active markets changed after the immutable daily snapshot')
                    return
                if VoteSnapshot.objects.filter(snapshot_time=at.date()).exists():
                    raise ValueError('Existing daily vote rows cannot be mixed with contract attribution')
                for market_key in markets:
                    VotesLoader(market_key, at, source_snapshot=source).load_votes()
                source.votes_loaded = True
                source.loaded_markets = markets
                source.save(update_fields=['votes_loaded', 'loaded_markets'])
            return
        if snapshot_time is None:
            snapshot_time = timezone.now().replace(minute=0, second=0, microsecond=0)
        task_make_claims_snapshot()
        markets = AggregatedByAssetBribe.objects.filter(
            start_at__lte=snapshot_time, stop_at__gt=snapshot_time,
        ).values_list('market_key', flat=True).distinct()
        for market_key in markets:
            VotesLoader(market_key, snapshot_time).load_votes()
    finally:
        cache.set(LOAD_VOTES_TASK_ACTIVE_KEY, False, None)


@celery_app.task(ignore_result=True, soft_time_limit=60 * 60 * 8, time_limit=60 * (60 * 8 + 5))
def task_make_trustees_snapshot(snapshot_time=None):
    cache.set(LOAD_TRUSTORS_TASK_ACTIVE_KEY, True, LOAD_TRUSTORS_TASK_TTL)

    if snapshot_time is None:
        snapshot_time = timezone.now()

    markets_with_active_bribes = AggregatedByAssetBribe.objects.filter(
        start_at__lte=snapshot_time, stop_at__gt=snapshot_time,
    )

    assets = set()
    for bribe in markets_with_active_bribes:
        assets.add((bribe.asset_code, bribe.asset_issuer))

    for asset_data in assets:
        if not (asset_data[0] == Asset.native().code and asset_data[1] == ''):
            asset = Asset(code=asset_data[0], issuer=asset_data[1])

            loader = TrusteesLoader(asset)
            loader.save_last_event_id(None)
            loader.make_balances_spanshot()

    cache.set(LOAD_TRUSTORS_TASK_ACTIVE_KEY, False, None)


@celery_app.task(
    ignore_result=True, soft_time_limit=PAYREWARD_TIME_LIMIT.total_seconds(),
    time_limit=PAYREWARD_TIME_LIMIT.total_seconds() + 60 * 3,
)
def task_pay_rewards(snapshot_time=None, reward_period=DEFAULT_REWARD_PERIOD):
    # PAY_REWARDS_TASK_ACTIVE_KEY is in the blocking list AND acquired via
    # cache.add() with an owner token: a concurrent task_pay_rewards must
    # fail to acquire. Without the self-exclusion two workers both pass
    # the guard, both compute the payable set, both submit to Horizon,
    # and the voter is double-credited.
    if any(cache.get(key, False) for key in (
        LOAD_VOTES_TASK_ACTIVE_KEY,
        LOAD_TRUSTORS_TASK_ACTIVE_KEY,
        PAY_REWARDS_TASK_ACTIVE_KEY,
    )):
        return

    owner_token = uuid.uuid4().hex
    if not cache.add(PAY_REWARDS_TASK_ACTIVE_KEY, owner_token, PAY_REWARDS_TASK_TTL):
        # Another worker acquired between our blocking-list check and the
        # cache.add — back out without touching the key it owns.
        logger.warning(
            'task_pay_rewards: another run holds PAY_REWARDS_TASK_ACTIVE_KEY; aborting',
        )
        return

    try:
        stop_at = timezone.now() + PAYREWARD_TIME_LIMIT
        asset_holder_cache = {}

        if snapshot_time is None:
            snapshot_time = timezone.now()
            snapshot_time = snapshot_time.replace(minute=0, second=0, microsecond=0)

        reward_wallet = SecuredWallet(
            public_key=settings.BRIBE_WALLET_ADDRESS,
            secret=settings.BRIBE_WALLET_SIGNER,
        )

        active_bribes = AggregatedByAssetBribe.objects.filter(
            start_at__lte=snapshot_time, stop_at__gt=snapshot_time,
        )

        for bribe in active_bribes:
            reward_amount = bribe.daily_amount * Decimal(reward_period.total_seconds() / (24 * 3600))
            votes, total_votes = get_payable_votes(
                bribe,
                snapshot_time.date(),
                reward_amount=reward_amount,
                asset_holder_cache=asset_holder_cache,
            )

            if votes.count() > 0:
                reward_payer = RewardPayer(bribe, reward_wallet, bribe.asset, reward_amount, stop_at=stop_at)
                reward_payer.pay_reward(votes, total_votes=total_votes)
    finally:
        # Only release the lock if we still own it — otherwise a stale
        # finally from a timed-out run could clear a key freshly acquired
        # by the next worker.
        if cache.get(PAY_REWARDS_TASK_ACTIVE_KEY) == owner_token:
            cache.delete(PAY_REWARDS_TASK_ACTIVE_KEY)
