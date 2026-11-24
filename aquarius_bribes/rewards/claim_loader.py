from typing import Dict, List

from django.utils import timezone

from billiard.exceptions import SoftTimeLimitExceeded
from stellar_sdk import Asset as SDKAsset
from stellar_sdk import ClaimPredicate, RequestsClient

from aquarius_bribes.bribes.utils import get_horizon
from aquarius_bribes.rewards.models import ClaimableBalance, Claimant


class ClaimLoader(object):
    def __init__(self, asset: SDKAsset, account: str = None):
        self.asset = asset
        self.account = account
        # Retry the current snapshot page across brief Horizon outages.
        self.horizon = get_horizon(client=RequestsClient(num_retries=7, backoff_factor=1))

    def _get_page(self, page_limit: int = 200, cursor=None) -> List[Dict]:
        builder = self.horizon.claimable_balances()

        if self.account is not None:
            builder = builder.for_claimant(
                self.account,
            )

        builder = builder.for_asset(
            self.asset,
        ).limit(page_limit).order(
            desc=False,
        )

        if cursor:
            builder = builder.cursor(cursor)

        return builder.call()['_embedded']['records']

    def make_claim_spanshot(self):
        claims = self._get_page()

        processed_claims = []
        while claims:
            try:
                for claim in claims:
                    processed_claims.append(self._process_claim(claim))

                cursor = claims[-1]['paging_token']

                claims = self._get_page(cursor=cursor)
            except SoftTimeLimitExceeded:
                pass

    def _build_predicate(self, raw_predicate):
        if 'and' in raw_predicate:
            return ClaimPredicate.predicate_and(
                self._build_predicate(raw_predicate['and'][0]),
                self._build_predicate(raw_predicate['and'][1]),
            )
        elif 'or' in raw_predicate:
            return ClaimPredicate.predicate_or(
                self._build_predicate(raw_predicate['or'][0]),
                self._build_predicate(raw_predicate['or'][1]),
            )
        elif 'not' in raw_predicate:
            return ClaimPredicate.predicate_not(
                self._build_predicate(raw_predicate['not']),
            )
        elif 'abs_before_epoch' in raw_predicate:
            return ClaimPredicate.predicate_before_absolute_time(
                abs_before=int(raw_predicate['abs_before_epoch']),
            )
        elif 'rel_before' in raw_predicate:
            return ClaimPredicate.predicate_before_relative_time(
                rel_before=int(raw_predicate['rel_before']),
            )
        elif 'unconditional' in raw_predicate:
            return ClaimPredicate.predicate_unconditional()

        raise Exception('Invalid predicate {0}'.format(raw_predicate))

    def _process_claim(self, claim: Dict) -> ClaimableBalance:
        owner = None
        for claimant in claim['claimants']:
            if claimant['predicate'].get('not', {}).get('unconditional', False) is not True:
                owner = claimant['destination']
                break

        date = timezone.now()
        date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        instance = ClaimableBalance.objects.filter(claimable_balance_id=claim['id'], loaded_at__gt=date).first()

        if not instance:
            instance = ClaimableBalance.objects.create(
                claimable_balance_id=claim['id'],
                asset_code=self.asset.code,
                asset_issuer=self.asset.issuer,
                amount=claim['amount'],
                sponsor=claim['sponsor'],
                paging_token='',
                last_modified_time=claim['last_modified_time'],
                last_modified_ledger=claim['last_modified_ledger'],
                owner=owner,
            )
        else:
            return instance

        for claimant in claim['claimants']:
            Claimant.objects.create(
                destination=claimant['destination'],
                raw_predicate=self._build_predicate(claimant['predicate']).to_xdr_object().to_xdr(),
                claimable_balance=instance,
            )
        return instance
