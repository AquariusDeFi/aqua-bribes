import logging

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone

from dateutil.parser import parse as date_parse
from stellar_sdk import Asset

from aquarius_bribes.bribes.models import Bribe, MarketKey
from aquarius_bribes.bribes.utils import get_horizon


class BribesLoader(object):
    def __init__(self, account, signer, last_id_cache_timeout: int = 60 * 60 * 12):
        self.account = account
        self.signer = signer
        self.horizon = get_horizon()
        self.last_id_cache_key = None
        self.last_id_cache_timeout = last_id_cache_timeout
        self.logger = logging.getLogger('BribesLoader')

    def load_last_event_id(self) -> str:
        paging_token = cache.get(self.last_id_cache_key, None)

        if paging_token:
            return paging_token

        last_saved_bribe = Bribe.objects.order_by('-created_at').first()

        if last_saved_bribe and last_saved_bribe.paging_token:
            return last_saved_bribe.paging_token

    def save_last_event_id(self, last_id: str):
        cache.set(self.last_id_cache_key, last_id, self.last_id_cache_timeout)

    def _get_page(self, page_limit: int = 200):
        builder = self.horizon.claimable_balances().for_claimant(
            self.account,
        ).limit(page_limit).order(
            desc=False,
        )

        last_id = self.load_last_event_id()
        if last_id:
            builder = builder.cursor(last_id)

        return builder.call()['_embedded']['records']

    def _is_market_key_predicate_correct(self, predicate: dict):
        return predicate == {
            'not': {
                'unconditional': True,
            },
        }

    def _parse_bribe_predicate(self, predicate: dict):
        return predicate.get('not', {}).get('abs_before', None)

    def _get_asset_equivalent(self, amount, asset, to_asset):
        if asset == to_asset:
            return amount

        paths = self.horizon.strict_send_paths(
            source_amount=str(amount), destination=[to_asset], source_asset=asset
        ).call().get("_embedded", {}).get("records", [])

        if len(paths) == 0:
            return 0
        else:
            return paths[0]['destination_amount']

    def _get_is_amm_protocol(self, sponsor: str) -> bool:
        # Check if the bribe is an AMM protocol bribe. they are created by separated accounts (per tokens set)
        # but claimable balance reserve is sponsored by the aquarius protocol fees admin, so compare to it
        return sponsor == settings.AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS

    def parse(self, bribe):
        amount = bribe['amount']
        sponsor = bribe['sponsor']
        claimants = bribe['claimants']
        claimable_balance_id = bribe['id']
        paging_token = bribe['paging_token']

        asset = bribe['asset']
        if asset == 'native':
            asset = Asset.native()
        else:
            asset = asset.split(':')
            asset = Asset(code=asset[0], issuer=asset[1])

        balance_created_at = bribe['last_modified_time']
        if len(claimants) != 2:
            self.logger.error('Invalid claimants %s', bribe['id'])
            return None

        bribe_collector_claim, market_key_claim = sorted(
            claimants, key=lambda cl: cl['destination'] == self.account, reverse=True,
        )

        status = Bribe.STATUS_PENDING
        messages = []

        if bribe_collector_claim['destination'] != self.account:
            messages.append('Invalid predicate: no bribe account')

        if not self._is_market_key_predicate_correct(market_key_claim['predicate']):
            messages.append('Invalid predicate: market key predicate incorrect')

        unlock_time = self._parse_bribe_predicate(bribe_collector_claim['predicate'])
        if not unlock_time:
            messages.append('Invalid predicate: bribe account predicate incorrect time')

        if balance_created_at is not None:
            try:
                balance_created_at = date_parse(balance_created_at)
            except ValueError:
                balance_created_at = None
                messages.append('Invalid predicate: invalid time format')
        else:
            balance_created_at = timezone.now()

        if unlock_time:
            try:
                unlock_time = date_parse(unlock_time)
            except ValueError:
                unlock_time = None
                messages.append('Invalid predicate: invalid unlock time format')

        if len(messages) > 0 and unlock_time:
            status = Bribe.STATUS_PENDING_RETURN
        elif len(messages) > 0:
            status = Bribe.STATUS_INVALID

        market_key, _ = MarketKey.objects.get_or_create(market_key=market_key_claim['destination'])
        aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)

        bribe = Bribe(
            asset_code=asset.code,
            asset_issuer=asset.issuer or '',
            sponsor=sponsor,
            market_key=market_key,
            amount=amount,
            claimable_balance_id=claimable_balance_id,
            paging_token=paging_token,
            created_at=balance_created_at,
            unlock_time=unlock_time,
            status=status,
            message='\n'.join(messages),
            aqua_total_reward_amount_equivalent=self._get_asset_equivalent(amount, asset, aqua),
            is_amm_protocol=self._get_is_amm_protocol(sponsor),
        )

        bribe.update_active_period()
        return bribe

    def process_bribe(self, bribe):
        bribe_instance = self.parse(bribe)
        return bribe_instance

    def save_all_items(self, items):
        try:
            Bribe.objects.bulk_create(items, batch_size=5000)
        except IntegrityError:
            for item in items:
                try:
                    item.save()
                except IntegrityError:
                    pass

    def load_bribes(self):
        bribes = self._get_page()

        while bribes:
            parsed_bribes = []
            for bribe in bribes:
                bribe_instance = self.process_bribe(bribe)
                if bribe_instance:
                    parsed_bribes.append(
                        self.process_bribe(bribe)
                    )

            self.save_all_items(parsed_bribes)
            self.save_last_event_id(parsed_bribes[-1].paging_token)

            bribes = self._get_page()
