from django.conf import settings
from django.db import models

from stellar_sdk import Asset
from stellar_sdk import Claimant as SDKClaimant
from stellar_sdk import ClaimPredicate
from stellar_sdk.xdr import ClaimPredicate as XDRClaimPredicate


class OldClaimableBalance(models.Model):
    claimable_balance_id = models.CharField(max_length=96, primary_key=True)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    amount = models.DecimalField(max_digits=20, decimal_places=7, default=0)
    sponsor = models.CharField(max_length=56)

    owner = models.CharField(max_length=56)

    paging_token = models.CharField(max_length=32, blank=True)
    last_modified_time = models.DateTimeField(null=True)
    last_modified_ledger = models.PositiveIntegerField()

    loaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "ClaimableBalance: {}...{}".format(self.claimable_balance_id[:6], self.claimable_balance_id[-6:])

    @property
    def asset(self):
        return Asset(code=self.asset_code, issuer=self.asset_issuer)

    @property
    def balance_claimants(self):
        result = []
        for claimant in self.claimants.all():
            result.append(
                SDKClaimant(
                    destination=claimant.destination,
                    predicate=claimant.predicate,
                )
            )
        return result


class OldClaimant(models.Model):
    destination = models.CharField(max_length=56)

    raw_predicate = models.TextField()

    old_claimable_balance = models.CharField(max_length=255)

    def __str__(self):
        return "Claimant {}...{} for {}...{}".format(
            self.destination[:6], self.destination[-6:], self.claimable_balance_id[:6], self.claimable_balance_id[-6:],
        )

    @property
    def predicate(self):
        return ClaimPredicate.from_xdr_object(XDRClaimPredicate.from_xdr(self.raw_predicate))


class ClaimableBalance(models.Model):
    claimable_balance_id = models.CharField(max_length=96)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    amount = models.DecimalField(max_digits=20, decimal_places=7, default=0)
    sponsor = models.CharField(max_length=56, db_index=True)

    owner = models.CharField(max_length=56, db_index=True)

    paging_token = models.CharField(max_length=32, blank=True)
    last_modified_time = models.DateTimeField(null=True)
    last_modified_ledger = models.PositiveIntegerField()

    loaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "ClaimableBalance: {}...{}".format(self.claimable_balance_id[:6], self.claimable_balance_id[-6:])

    @property
    def asset(self):
        return Asset(code=self.asset_code, issuer=self.asset_issuer)

    @property
    def balance_claimants(self):
        result = []
        for claimant in self.claimants.all():
            result.append(
                SDKClaimant(
                    destination=claimant.destination,
                    predicate=claimant.predicate,
                )
            )
        return result


class Claimant(models.Model):
    destination = models.CharField(max_length=56, db_index=True)

    raw_predicate = models.TextField()

    claimable_balance = models.ForeignKey('ClaimableBalance', on_delete=models.CASCADE, related_name='claimants')

    def __str__(self):
        return "Claimant {}...{} for {}...{}".format(
            self.destination[:6], self.destination[-6:],
            self.claimable_balance.claimable_balance_id[:6],
            self.claimable_balance.claimable_balance_id[-6:],
        )

    @property
    def predicate(self):
        return ClaimPredicate.from_xdr_object(XDRClaimPredicate.from_xdr(self.raw_predicate))


class VoteSnapshot(models.Model):
    market_key = models.ForeignKey('bribes.MarketKey', null=True, on_delete=models.PROTECT)

    votes_value = models.DecimalField(max_digits=20, decimal_places=7)
    voting_account = models.CharField(max_length=56, db_index=True)

    is_delegated = models.BooleanField(default=False)
    has_delegation = models.BooleanField(default=False)
    delegate_owner = models.CharField(max_length=56, db_index=True, default=None, blank=True, null=True)

    snapshot_time = models.DateField(db_index=True)

    def __str__(self):
        return 'VoteSnapshot: {}..{} ({})'.format(
            self.voting_account[:4], self.voting_account[-4:], self.snapshot_time,
        )

    class Meta:
        unique_together = (
            'snapshot_time', 'market_key', 'voting_account',
            'is_delegated', 'has_delegation', 'delegate_owner',
        )


class Payout(models.Model):
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_SUCCESS, 'success'),
        (STATUS_FAILED, 'failed'),
    )

    bribe = models.ForeignKey('bribes.AggregatedByAssetBribe', on_delete=models.PROTECT)

    vote_snapshot = models.ForeignKey(VoteSnapshot, on_delete=models.PROTECT)

    stellar_transaction_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        choices=STATUS_CHOICES, default=STATUS_SUCCESS, max_length=30, db_index=True,
    )
    message = models.TextField(blank=True, db_index=True)

    reward_amount = models.DecimalField(max_digits=20, decimal_places=7, null=True)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return 'Payout {0} for {1}'.format(
            self.reward_amount,
            self.vote_snapshot.voting_account,
        )


class AssetHolderBalanceSnapshot(models.Model):
    account = models.CharField(max_length=255, db_index=True)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    balance = models.DecimalField(max_digits=20, decimal_places=7)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['asset_code', 'asset_issuer', 'created_at'],
                name='ahbs_asset_date_idx',
            ),
        ]

    def __str__(self):
        return '{0}..{1} at {2}: {3} {4}'.format(
            self.account[:8], self.account[-8:], self.created_at.date(), self.balance, self.asset.code,
        )


class ContractDelegationSnapshot(models.Model):
    """Retained source input and completion checkpoint for one daily attribution run."""
    day = models.DateField(primary_key=True)
    source_id = models.CharField(max_length=64, unique=True)
    payload = models.JSONField()
    votes_loaded = models.BooleanField(default=False)
    loaded_markets = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
