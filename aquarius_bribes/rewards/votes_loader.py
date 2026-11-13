import hashlib
import json
import re
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import IntegrityError, models

import requests
import sentry_sdk
from stellar_sdk import StrKey

from aquarius_bribes.rewards.contract_delegation import validate_source
from aquarius_bribes.rewards.models import ClaimableBalance, VoteSnapshot
from aquarius_bribes.rewards.utils import _get_not_unconditinal_predicate
from aquarius_bribes.utils.assets import get_asset_string


class VotesLoader(object):
    def __init__(self, market_key, snapshot_time, base_url=None, source_snapshot=None):
        self.market_key = market_key
        self.snapshot_time = snapshot_time
        self.source_snapshot = source_snapshot
        self._contract_has_next = None
        self._contract_view = None
        self._contract_count = None
        self._contract_rows = []
        if getattr(settings, 'CONTRACT_DELEGATION_SNAPSHOTS_ENABLED', False) and source_snapshot is None:
            raise ValueError('Contract mode requires an immutable source snapshot')
        if source_snapshot is not None:
            at = validate_source(source_snapshot.source_id, source_snapshot.payload)
            if at != snapshot_time:
                raise ValueError('Vote and delegation snapshot timestamp mismatch')
            base_url = base_url or settings.VOTING_TRACKER_URL
            if not base_url.startswith('https://'):
                raise ValueError('Contract mode requires an explicit HTTPS voting-tracker URL')
        self.base_url = base_url or settings.VOTING_TRACKER_URL

    def _get_page(self, page, page_limit: int = 200):
        response = requests.get(
            f'{self.base_url}/api/market-keys/{self.market_key}/votes/'
            f'?limit={page_limit}&timestamp={int(self.snapshot_time.timestamp())}&page={page}'
            + ('&include_assets=1' if self.source_snapshot is not None else ''),
            timeout=(5, 20),
        )
        if self.source_snapshot is not None:
            response.raise_for_status()
            body = response.json()
            network_id = hashlib.sha256(settings.STELLAR_PASSPHRASE.encode()).hexdigest()
            if (body.get('as_of') != int(self.snapshot_time.timestamp()) or body.get('network_id') != network_id
                    or not isinstance(body.get('results'), list)
                    or type(body.get('count')) is not int or not 0 <= body['count'] <= 10000
                    or not re.fullmatch('[0-9a-f]{64}', str(body.get('view_id')))):
                raise ValueError('Voting source did not confirm the pinned time, network and complete view')
            if self._contract_view is None:
                self._contract_view, self._contract_count = body['view_id'], body['count']
            if (body['view_id'], body['count']) != (self._contract_view, self._contract_count):
                raise ValueError('Voting source changed between pages')
            self._contract_rows.extend(body['results'])
            if len(self._contract_rows) > self._contract_count:
                raise ValueError('Voting source returned more rows than its declared population')
            self._contract_has_next = body.get('next') is not None
            if self._contract_has_next and not body['results']:
                raise ValueError('Empty intermediate vote page')
            if not self._contract_has_next:
                contents = {'as_of': body['as_of'], 'network_id': network_id,
                            'market_key': str(self.market_key), 'results': self._contract_rows}
                encoded = json.dumps(contents, sort_keys=True, separators=(',', ':')).encode()
                actual = hashlib.sha256(encoded).hexdigest()
                if len(self._contract_rows) != self._contract_count or actual != self._contract_view:
                    raise ValueError('Voting source view identity or complete denominator mismatch')
            return body['results']
        return response.json().get('results', [])

    def process_vote(self, vote):
        return VoteSnapshot(
            snapshot_time=self.snapshot_time,
            votes_value=vote['votes_value'],
            voting_account=vote['voting_account'],
            market_key_id=self.market_key,
        )

    def _get_delegated_asset_filter(self):
        asset_filter = models.Q()
        for _, delegated_asset in settings.DELEGATABLE_ASSETS:
            asset_filter |= models.Q(
                asset_code=delegated_asset.code,
                asset_issuer=delegated_asset.issuer,
            )
        return asset_filter

    def _get_delegatable_asset_filter(self):
        asset_filter = models.Q()
        for asset, _ in settings.DELEGATABLE_ASSETS:
            asset_filter |= models.Q(
                asset_code=asset.code,
                asset_issuer=asset.issuer,
            )
        return asset_filter

    def has_delegated_votes(self, voting_account):
        asset_filter = self._get_delegated_asset_filter()

        date = self.snapshot_time.replace(hour=0)
        return ClaimableBalance.objects.filter(
            loaded_at__gte=date, loaded_at__lt=date + timedelta(days=1),
        ).filter(owner=voting_account).filter(
            asset_filter,
        ).filter(claimants__destination=self.market_key).exists()

    def amount_delegated_votes(self, voting_account):
        asset_filter = self._get_delegated_asset_filter()

        date = self.snapshot_time.replace(hour=0)
        return ClaimableBalance.objects.filter(
            loaded_at__gte=date, loaded_at__lt=date + timedelta(days=1),
        ).filter(owner=voting_account).filter(
            asset_filter,
        ).filter(claimants__destination=self.market_key).aggregate(
            total_votes=models.Sum('amount'),
        )['total_votes'] or Decimal('0')

    def process_delegated_vote(self, voting_account, votes_value):
        votes = []
        votes_value = Decimal(votes_value)

        asset_filter = self._get_delegatable_asset_filter()

        date = self.snapshot_time.replace(hour=0)
        delegated_votes = ClaimableBalance.objects.filter(
            loaded_at__gte=date, loaded_at__lt=date + timedelta(days=1),
        ).filter(
            claimants__raw_predicate=_get_not_unconditinal_predicate(),
            claimants__destination=voting_account
        ).filter(
            asset_filter,
        ).filter(claimants__destination=settings.DELEGATE_MARKER)
        total_delegated_votes = delegated_votes.aggregate(total_votes=models.Sum('amount'))['total_votes']

        if total_delegated_votes is None:
            total_delegated_votes = Decimal(0)

        delegated_votes_amount = self.amount_delegated_votes(voting_account)

        votes.append(
            VoteSnapshot(
                snapshot_time=self.snapshot_time,
                votes_value=votes_value,
                voting_account=voting_account,
                market_key_id=self.market_key,
                is_delegated=False,
                has_delegation=True,
            )
        )

        if votes_value > delegated_votes_amount:
            votes.append(
                VoteSnapshot(
                    snapshot_time=self.snapshot_time,
                    votes_value=votes_value - delegated_votes_amount,
                    voting_account=voting_account,
                    market_key_id=self.market_key,
                    is_delegated=False,
                    has_delegation=False,
                )
            )
            votes_value = delegated_votes_amount

        if total_delegated_votes > 0:
            for delegated_vote in delegated_votes:
                votes.append(
                    VoteSnapshot(
                        snapshot_time=self.snapshot_time,
                        votes_value=Decimal(
                            votes_value * delegated_vote.amount / total_delegated_votes,
                        ).quantize(
                            Decimal('0.0000001'), rounding=ROUND_DOWN,
                        ),
                        delegate_owner=voting_account,
                        voting_account=delegated_vote.owner,
                        market_key_id=self.market_key,
                        is_delegated=True,
                    )
                )

        return votes

    def process_contract_vote(self, vote):
        account = vote['voting_account']
        if not StrKey.is_valid_ed25519_public_key(account):
            raise ValueError('Invalid Classic voting account')
        components = vote.get('asset_votes')
        if not isinstance(components, list) or not components:
            raise ValueError('Per-asset vote components are required')
        amounts = {}
        for component in components:
            asset = component['asset']
            value = Decimal(component['votes_value'])
            if not value.is_finite() or value < 0 or value * 10000000 != (value * 10000000).to_integral_value():
                raise ValueError('Invalid vote amount')
            if asset in amounts:
                raise ValueError('Duplicate vote asset component')
            amounts[asset] = value
        total = sum(amounts.values(), Decimal(0))
        if total != Decimal(vote['votes_value']):
            raise ValueError('Vote components differ from the aggregate')
        delegated_assets = {get_asset_string(asset) for _, asset in settings.DELEGATABLE_ASSETS}
        incoming = sum((value for asset, value in amounts.items() if asset in delegated_assets), Decimal(0))
        if not incoming:
            return [self.process_vote(vote)]
        donors = self.source_snapshot.payload['delegates'].get(account, [])
        if not donors:
            raise ValueError('No historical donor weights exist for delegated votes')
        denominator = sum(int(donor['weight']) for donor in donors)
        rows = [VoteSnapshot(snapshot_time=self.snapshot_time, votes_value=total, voting_account=account,
                             market_key_id=self.market_key, has_delegation=True)]
        if total > incoming:
            rows.append(VoteSnapshot(snapshot_time=self.snapshot_time, votes_value=total - incoming,
                                     voting_account=account, market_key_id=self.market_key))
        units = int(incoming * 10000000)
        for donor in donors:
            allocated = units * int(donor['weight']) // denominator
            rows.append(VoteSnapshot(snapshot_time=self.snapshot_time, votes_value=Decimal(allocated).scaleb(-7),
                                     voting_account=donor['donor'], delegate_owner=account,
                                     market_key_id=self.market_key, is_delegated=True))
        return rows

    def aggregate_items(self, processed) -> list:
        # Aggregate items by unique keys
        # in case of multiple inherited vote snapshots to the same market key from delegate
        aggregated = {}
        for item in processed:
            key = (
                item.voting_account,
                item.market_key_id,
                item.is_delegated,
                item.has_delegation,
                item.delegate_owner,
            )
            if key not in aggregated:
                aggregated[key] = item
            else:
                aggregated_item = aggregated[key]
                aggregated_item.votes_value += item.votes_value

        return list(aggregated.values())

    def save_all_items(self, processed):
        if self.source_snapshot is not None:
            VoteSnapshot.objects.bulk_create(processed, batch_size=5000)
            return
        try:
            VoteSnapshot.objects.bulk_create(processed, batch_size=5000)
        except IntegrityError:
            for item in processed:
                try:
                    item.save()
                except IntegrityError as err:
                    sentry_sdk.capture_exception(err)

    def load_votes(self):
        page = 1
        votes = self._get_page(page)
        parsed_votes = []

        while votes:
            for vote in votes:
                # if vote['asset'] in delegated_assets:
                if self.source_snapshot is not None:
                    parsed_votes += self.process_contract_vote(vote)
                elif self.has_delegated_votes(vote['voting_account']):
                    parsed_votes += self.process_delegated_vote(
                        vote['voting_account'],
                        vote['votes_value'],
                    )
                else:
                    parsed_votes.append(
                        self.process_vote(vote)
                    )

            if self.source_snapshot is not None and self._contract_has_next is False:
                break
            page += 1
            votes = self._get_page(page)

        aggregated_votes = self.aggregate_items(parsed_votes)
        self.save_all_items(aggregated_votes)
