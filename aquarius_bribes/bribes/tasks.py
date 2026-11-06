import logging
import requests

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from stellar_sdk import Asset
from stellar_sdk.exceptions import BaseHorizonError

from aquarius_bribes.bribes.bribe_processor import BribeProcessor
from aquarius_bribes.bribes.exceptions import NoPathForConversionError
from aquarius_bribes.bribes.loader import BribesLoader
from aquarius_bribes.bribes.models import AggregatedByAssetBribe, Bribe, MarketKey, SorobanTokenSymbol
from aquarius_bribes.taskapp import app as celery_app
from aquarius_bribes.utils.soroban import get_token_symbol


logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True, soft_time_limit=60 * 30, time_limit=60 * 35)
def task_load_bribes():
    loader = BribesLoader(settings.BRIBE_WALLET_ADDRESS, settings.BRIBE_WALLET_SIGNER)
    loader.load_bribes()


# Bare soroban contract id, as marketkeys-tracker reports it (C + 55 base32).
CONTRACT_ID_REGEX = r'^C[A-Z2-7]{55}$'


def _get_contract_label(raw_asset: str) -> str:
    """Return the token symbol for a contract asset, resolving it on-chain
    only once per contract (afterwards it is served from the database)."""
    if not MarketKey._is_contract(raw_asset):
        return ''

    stored = SorobanTokenSymbol.objects.filter(contract=raw_asset).first()
    if stored is not None:
        return stored.symbol

    try:
        symbol = get_token_symbol(raw_asset)[:64]
    except Exception as e:
        logger.info('Failed to resolve token symbol for {}: {}'.format(raw_asset, e))
        return ''

    SorobanTokenSymbol.objects.get_or_create(contract=raw_asset, defaults={'symbol': symbol})
    return symbol


@celery_app.task(ignore_result=True, soft_time_limit=60 * 30, time_limit=60 * 35)
def load_market_key_details():
    # Also revisit markets whose contract asset has no label yet: a single RPC
    # failure while resolving the symbol used to leave the label empty forever,
    # because the row was saved with raw_asset1 set and never looked at again.
    missing_label = (
        models.Q(raw_asset1__regex=CONTRACT_ID_REGEX, asset1_label='')
        | models.Q(raw_asset2__regex=CONTRACT_ID_REGEX, asset2_label='')
    )

    queryset = MarketKey.objects.filter(
        models.Q(raw_asset1='') | missing_label,
        bribes__status__in=[Bribe.STATUS_PENDING, Bribe.STATUS_ACTIVE],
    ).distinct()

    for market_key in queryset:
        try:
            url = "{}/api/market-keys/?account_id={}".format(
                settings.MARKETKEYS_TRACKER_URL.rstrip('/'), market_key.market_key,
            )
            response = requests.get(url)
            key_info = response.json()['results'][0]

            if key_info['asset1'] and key_info['asset2']:
                market_key.raw_asset1 = key_info['asset1']
                market_key.raw_asset2 = key_info['asset2']
                market_key.asset1_label = _get_contract_label(key_info['asset1'])
                market_key.asset2_label = _get_contract_label(key_info['asset2'])
                market_key.save(
                    update_fields=['raw_asset1', 'raw_asset2', 'asset1_label', 'asset2_label'],
                )
        except Exception as e:
            logger.info(str(e))


@celery_app.task(ignore_result=True, soft_time_limit=60 * 30, time_limit=60 * 35)
def task_claim_bribes():
    ready_to_claim = Bribe.objects.filter(unlock_time__lte=timezone.now(), status=Bribe.STATUS_PENDING)

    aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)
    bribe_processor = BribeProcessor(settings.BRIBE_WALLET_ADDRESS, settings.BRIBE_WALLET_SIGNER, aqua)

    while ready_to_claim.count() > 0:
        for bribe in ready_to_claim:
            try:
                bribe_processor.claim_and_convert(bribe)
                bribe.update_active_period(timezone.now())
                bribe.status = Bribe.STATUS_ACTIVE
                bribe.save()
            except NoPathForConversionError:
                bribe.status = Bribe.STATUS_NO_PATH_FOR_CONVERSION
                bribe.save()
            except BaseHorizonError as submit_exc:
                if submit_exc.extras:
                    result_codes = submit_exc.extras.get('result_codes', {}) or {}
                    transaction_fail_reason = result_codes.get('transaction', 'no_reason')
                else:
                    transaction_fail_reason = submit_exc.message
                safe_fail_reasons = [
                    'tx_bad_seq',
                    'tx_bad_auth',
                ]
                if (
                    getattr(submit_exc, 'status', None) not in (504, 522, 502)
                    and transaction_fail_reason not in safe_fail_reasons
                ):
                    message = bribe.message or ''
                    message += '\n' + str(submit_exc)
                    bribe.message = message
                    bribe.status = Bribe.STATUS_FAILED_CLAIM
                    bribe.save()
            except Exception as e:
                message = bribe.message or ''
                message += '\n' + str(e)
                bribe.message = message
                bribe.status = Bribe.STATUS_FAILED_CLAIM
                bribe.save()

        ready_to_claim = Bribe.objects.filter(unlock_time__lte=timezone.now(), status=Bribe.STATUS_PENDING)


@celery_app.task(ignore_result=True, soft_time_limit=60 * 15, time_limit=60 * 15)
def task_update_bribe_aqua_equivalent():
    now = timezone.now()
    aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)
    loader = BribesLoader(settings.BRIBE_WALLET_ADDRESS, settings.BRIBE_WALLET_SIGNER)

    for bribe in AggregatedByAssetBribe.objects.filter(stop_at__gt=now):
        bribe.aqua_total_reward_amount_equivalent = loader._get_asset_equivalent(
            bribe.total_reward_amount, bribe.asset, aqua,
        )
        bribe.save()


@celery_app.task(ignore_result=True, soft_time_limit=60 * 7, time_limit=60 * 10)
def task_update_pending_bribe_aqua_equivalent():
    aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)
    loader = BribesLoader(settings.BRIBE_WALLET_ADDRESS, settings.BRIBE_WALLET_SIGNER)

    for bribe in Bribe.objects.filter(status=Bribe.STATUS_PENDING).order_by('-updated_at'):
        bribe.aqua_total_reward_amount_equivalent = loader._get_asset_equivalent(bribe.amount, bribe.asset, aqua)
        bribe.save()


@celery_app.task(ignore_result=True, soft_time_limit=60 * 7, time_limit=60 * 10)
def task_update_pending_bribe_period():
    now = timezone.now()

    Bribe.objects.filter(status=Bribe.STATUS_PENDING).filter(start_at__lt=now).update(
        start_at=models.F('start_at') + timedelta(days=7),
        stop_at=models.F('stop_at') + timedelta(days=7),
    )


@celery_app.task(ignore_result=True, soft_time_limit=60 * 7, time_limit=60 * 10)
def task_stop_active_bribes():
    Bribe.objects.filter(status=Bribe.STATUS_ACTIVE).filter(stop_at__lte=timezone.now()).update(
        status=Bribe.STATUS_FINISHED,
    )


@celery_app.task(ignore_result=True, soft_time_limit=60 * 30, time_limit=60 * 35)
def task_aggregate_bribes(start_at=None, stop_at=None):
    if start_at is None:
        time = timezone.now()
        start_at = time + timedelta(days=8 - time.isoweekday())
        start_at = start_at.replace(hour=0, minute=0, second=0, microsecond=0)
    if stop_at is None:
        stop_at = start_at + Bribe.DEFAULT_DURATION

    aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)
    active_bribes = Bribe.objects.filter(status=Bribe.STATUS_ACTIVE, start_at=start_at, stop_at=stop_at)

    aggregated_by_asset = active_bribes.exclude(asset_code=aqua.code, asset_issuer=aqua.issuer).values(
        "market_key", "asset_code", "asset_issuer", "start_at", "stop_at",
    ).annotate(
        total_reward_amount=models.Sum('amount_for_bribes')
    )

    aggregated_bribes = []
    for bribe in aggregated_by_asset:
        aggregated_bribes.append(
            AggregatedByAssetBribe(
                market_key_id=bribe['market_key'],
                asset_code=bribe['asset_code'],
                asset_issuer=bribe['asset_issuer'],
                start_at=bribe['start_at'],
                stop_at=bribe['stop_at'],
                total_reward_amount=bribe['total_reward_amount'],
            )
        )

    aqua_bribes = active_bribes.filter(asset_code=aqua.code, asset_issuer=aqua.issuer).values(
        "market_key",
    ).annotate(
        total_reward_amount=models.Sum('amount_for_bribes')
    )
    aqua_bribes = dict(aqua_bribes.values_list("market_key", "total_reward_amount"))

    aggregated_aqua_by_market = active_bribes.values(
        "market_key", "start_at", "stop_at",
    ).annotate(
        total_reward_amount=models.Sum('amount_aqua')
    )
    for bribe in aggregated_aqua_by_market:
        aggregated_bribes.append(
            AggregatedByAssetBribe(
                market_key_id=bribe['market_key'],
                asset_code=aqua.code,
                asset_issuer=aqua.issuer or '',
                start_at=bribe['start_at'],
                stop_at=bribe['stop_at'],
                total_reward_amount=bribe['total_reward_amount'] + aqua_bribes.get(bribe['market_key'], 0),
            )
        )

    AggregatedByAssetBribe.objects.bulk_create(aggregated_bribes)


@celery_app.task(ignore_result=True, soft_time_limit=60 * 30, time_limit=60 * 35)
def task_return_bribes():
    ready_to_return = Bribe.objects.filter(status=Bribe.STATUS_NO_PATH_FOR_CONVERSION)
    aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)
    bribe_processor = BribeProcessor(settings.BRIBE_WALLET_ADDRESS, settings.BRIBE_WALLET_SIGNER, aqua)

    for bribe in ready_to_return:
        try:
            response = bribe_processor.claim_and_return(bribe)
            bribe.refund_tx_hash = response['hash']
            bribe.status = Bribe.STATUS_RETURNED
            bribe.save()
        except Exception as e:
            message = bribe.message or ''
            message += '\n' + str(e)
            bribe.message = message
            bribe.status = Bribe.STATUS_FAILED_RETURN
            bribe.save()
