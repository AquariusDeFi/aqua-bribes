import logging

from django.conf import settings
from django.db import transaction

from dateutil.parser import parse as date_parse
from stellar_sdk import Asset

from aquarius_bribes.bribes.models import Bribe, BribeIngestionCursor, MarketKey
from aquarius_bribes.bribes.utils import get_horizon


class BribesLoader(object):
    def __init__(self, account, signer, last_id_cache_timeout: int = 60 * 60 * 12):
        self.account = account
        self.signer = signer
        self.horizon = get_horizon()
        self.logger = logging.getLogger('BribesLoader')

    def load_last_event_id(self) -> str:
        paging_token = BribeIngestionCursor.objects.filter(
            account=self.account,
        ).values_list('paging_token', flat=True).first()
        return paging_token or None

    def _get_page(self, page_limit: int = 200, cursor=None):
        builder = self.horizon.claimable_balances().for_claimant(
            self.account,
        ).limit(page_limit).order(
            desc=False,
        )

        if cursor:
            builder = builder.cursor(cursor)

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

    def _parse_transport_metadata(self, bribe):
        claimants = bribe.get('claimants')
        if not isinstance(claimants, list):
            raise ValueError('Invalid claimable balance claimants')

        claimable_balance_id = bribe.get('id')
        if not isinstance(claimable_balance_id, str) or not claimable_balance_id:
            raise ValueError('Invalid claimable balance id')

        paging_token = bribe.get('paging_token')
        if not isinstance(paging_token, str) or not paging_token:
            raise ValueError('Invalid claimable balance paging_token')

        balance_created_at = bribe.get('last_modified_time')
        if not isinstance(balance_created_at, str) or not balance_created_at:
            raise ValueError('Invalid claimable balance last_modified_time')
        try:
            balance_created_at = date_parse(balance_created_at)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError('Invalid claimable balance last_modified_time') from exc

        return claimants, claimable_balance_id, paging_token, balance_created_at

    def parse(self, bribe):
        claimants, claimable_balance_id, paging_token, balance_created_at = (
            self._parse_transport_metadata(bribe)
        )
        if len(claimants) != 2:
            return None

        amount = bribe['amount']
        sponsor = bribe['sponsor']

        asset = bribe['asset']
        if asset == 'native':
            asset = Asset.native()
        else:
            asset = asset.split(':')
            asset = Asset(code=asset[0], issuer=asset[1])

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

        aqua = Asset(code=settings.REWARD_ASSET_CODE, issuer=settings.REWARD_ASSET_ISSUER)

        bribe = Bribe(
            asset_code=asset.code,
            asset_issuer=asset.issuer or '',
            sponsor=sponsor,
            market_key_id=market_key_claim['destination'],
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
        existing_ids = set(Bribe.objects.filter(
            claimable_balance_id__in=[item.claimable_balance_id for item in items],
        ).values_list('claimable_balance_id', flat=True))
        new_items = []
        for item in items:
            if item.claimable_balance_id not in existing_ids:
                existing_ids.add(item.claimable_balance_id)
                new_items.append(item)

        MarketKey.objects.bulk_create(
            [MarketKey(market_key=item.market_key_id) for item in new_items],
            ignore_conflicts=True,
        )
        Bribe.objects.bulk_create(new_items, batch_size=5000)

    @transaction.atomic
    def _persist_page(self, items, requested_cursor, page_cursor):
        cursor, _ = BribeIngestionCursor.objects.select_for_update().get_or_create(
            account=self.account,
            defaults={'paging_token': requested_cursor or ''},
        )
        if (cursor.paging_token or None) != requested_cursor:
            return False

        self.save_all_items(items)
        cursor.paging_token = page_cursor
        cursor.save(update_fields=['paging_token'])
        return True

    def load_bribes(self):
        requested_cursor = self.load_last_event_id()
        bribes = self._get_page(cursor=requested_cursor)

        while bribes:
            parsed_bribes = []
            skipped_bribes = []
            for bribe in bribes:
                bribe_instance = self.process_bribe(bribe)
                if bribe_instance:
                    parsed_bribes.append(bribe_instance)
                else:
                    skipped_bribes.append(
                        (bribe['id'], len(bribe['claimants'])),
                    )

            page_cursor = bribes[-1].get('paging_token')
            if not isinstance(page_cursor, str) or not page_cursor or page_cursor == requested_cursor:
                raise ValueError('Invalid claimable balances page cursor')

            if self._persist_page(parsed_bribes, requested_cursor, page_cursor):
                for bribe_id, claimant_count in skipped_bribes:
                    self.logger.warning(
                        'Skipping claimable balance %s: expected 2 claimants, got %s',
                        bribe_id,
                        claimant_count,
                    )
                requested_cursor = page_cursor
            else:
                requested_cursor = self.load_last_event_id()
            bribes = self._get_page(cursor=requested_cursor)
