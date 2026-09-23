import re
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.db import models

from stellar_sdk import Asset

# Stellar text memos are limited to 28 bytes. Labels are sanitized to ASCII
# alphanumerics so that "characters" and "bytes" are the same thing: a token
# `symbol()` is chosen by whoever deployed the token and may be multi-byte
# (eight emoji are 32 bytes), which would make the payout memo unbuildable.
MEMO_MAX_BYTES = 28
MEMO_PREFIX = 'Bribe: '
CONTRACT_LABEL_MAX_LEN = 8
CLASSIC_LABEL_MAX_LEN = 4
NON_ALPHANUMERIC_RE = re.compile(r'[^A-Za-z0-9]')


class SorobanTokenSymbol(models.Model):
    """Resolved SEP-41 token symbols: each contract is queried on-chain once,
    afterwards the symbol is always served from this table."""

    contract = models.CharField(max_length=56, primary_key=True)
    symbol = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return '{} ({})'.format(self.symbol, self.contract[:4])


class MarketKey(models.Model):
    market_key = models.CharField(max_length=56, primary_key=True)

    raw_asset1 = models.CharField(max_length=255, default='', blank=True)
    raw_asset2 = models.CharField(max_length=255, default='', blank=True)

    # Token symbols for soroban (contract) assets, resolved from the AMM API
    # by load_market_key_details. Used for human-readable payout memos.
    asset1_label = models.CharField(max_length=64, default='', blank=True)
    asset2_label = models.CharField(max_length=64, default='', blank=True)

    def __str__(self):
        return self.market_key

    @staticmethod
    def _is_contract(raw_asset):
        # Soroban (non-SAC) tokens come from marketkeys-tracker as a bare contract id
        return raw_asset.startswith('C') and ':' not in raw_asset and len(raw_asset) == 56

    def get_asset_object(self, raw_asset):
        if raw_asset:
            if raw_asset == 'native':
                return Asset.native()
            if self._is_contract(raw_asset):
                # Soroban tokens have no classic Asset representation
                return None
            code, issuer = raw_asset.split(':')
            return Asset(code=code, issuer=issuer)

    @property
    def asset1(self):
        if self.raw_asset1:
            return self.get_asset_object(self.raw_asset1)

    @property
    def asset2(self):
        if self.raw_asset2:
            return self.get_asset_object(self.raw_asset2)

    @staticmethod
    def _sanitize_label(value: str, max_len: int) -> str:
        """Reduce a label to at most `max_len` ASCII alphanumerics.

        Token symbols come straight off the contract, so they may contain any
        unicode; after sanitizing, one character is always one byte, which is
        what keeps the memo inside its byte budget.
        """
        return NON_ALPHANUMERIC_RE.sub('', value or '')[:max_len]

    def _asset_label(self, raw_asset, stored_label):
        if self._is_contract(raw_asset):
            # Prefer the resolved token symbol. A symbol that sanitizes to
            # nothing (emoji-only, say) falls back to the contract prefix, so
            # the label is never empty.
            return (
                self._sanitize_label(stored_label, CONTRACT_LABEL_MAX_LEN)
                or raw_asset[:CLASSIC_LABEL_MAX_LEN]
            )

        return self._sanitize_label(self.get_asset_object(raw_asset).code, CLASSIC_LABEL_MAX_LEN)

    @property
    def short_value(self):
        if self.raw_asset1 and self.raw_asset2:
            return '{}/{}'.format(
                self._asset_label(self.raw_asset1, self.asset1_label),
                self._asset_label(self.raw_asset2, self.asset2_label),
            )
        return '{}...{}'.format(self.market_key[:4], self.market_key[-4:])

    @property
    def payout_memo(self) -> str:
        """Text memo for payout transactions, guaranteed to be buildable.

        Labels are already ASCII-bounded, so the prefix + pair never exceeds the
        limit; the final clamp is a hard guarantee that no market can produce a
        transaction that cannot be built (which would be retried forever).
        """
        memo = '{}{}'.format(MEMO_PREFIX, self.short_value)
        return memo.encode('ascii', 'ignore')[:MEMO_MAX_BYTES].decode('ascii')


class BribeIngestionCursor(models.Model):
    account = models.CharField(max_length=56, primary_key=True)
    paging_token = models.CharField(max_length=255, blank=True, default='')


class Bribe(models.Model):
    DEFAULT_DURATION = timedelta(days=7)

    STATUS_PENDING = 0
    STATUS_INVALID = 1
    STATUS_ACTIVE = 2
    STATUS_RETURNED = 3
    STATUS_PENDING_RETURN = 4
    STATUS_FAILED_CLAIM = 5
    STATUS_NO_PATH_FOR_CONVERSION = 6
    STATUS_FAILED_RETURN = 7
    STATUS_FINISHED = 8

    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending unlock time'),
        (STATUS_INVALID, 'Invalid bribe'),
        (STATUS_ACTIVE, 'Active bribe'),
        (STATUS_RETURNED, 'Returned'),
        (STATUS_PENDING_RETURN, 'Pending unlock time to return'),
        (STATUS_FAILED_CLAIM, 'Failed claim'),
        (STATUS_NO_PATH_FOR_CONVERSION, 'Conversion failed'),
        (STATUS_FAILED_RETURN, 'Failed return'),
        (STATUS_FINISHED, 'Finished'),
    )

    status = models.IntegerField(choices=STATUS_CHOICES)
    message = models.TextField()

    market_key = models.ForeignKey(MarketKey, related_name='bribes', null=True, on_delete=models.PROTECT)

    sponsor = models.CharField(max_length=56)
    amount = models.DecimalField(max_digits=20, decimal_places=7)

    is_amm_protocol = models.BooleanField(default=False, help_text="AMM Protocol bribes are created using a "
                                                                   "fraction of collected protocol fees")

    aqua_total_reward_amount_equivalent = models.DecimalField(max_digits=20, decimal_places=7, null=True)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    amount_for_bribes = models.DecimalField(max_digits=20, decimal_places=7, null=True)
    amount_aqua = models.DecimalField(max_digits=20, decimal_places=7, null=True)
    convertation_tx_hash = models.CharField(max_length=255, null=True, default=None)
    refund_tx_hash = models.CharField(max_length=255, null=True, default=None)

    claimable_balance_id = models.CharField(max_length=255, unique=True)
    paging_token = models.CharField(max_length=255)

    unlock_time = models.DateTimeField(null=True)

    start_at = models.DateTimeField(null=True)
    stop_at = models.DateTimeField(null=True)

    created_at = models.DateTimeField(null=True)

    loaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Bribe: {0}...{1} {2}...{3}".format(
            self.market_key_id[:4], self.market_key_id[-4:],
            self.claimable_balance_id[:4], self.claimable_balance_id[-4:],
        )

    def update_active_period(self, time=None, duration=DEFAULT_DURATION):
        if time is None:
            time = self.unlock_time

        if time is None:
            return

        start_at = time + timedelta(days=8 - time.isoweekday())
        self.start_at = start_at.replace(hour=0, minute=0, second=0, microsecond=0)
        self.stop_at = self.start_at + duration

    @property
    def short_asset(self):
        asset = self.asset_code
        if self.asset_issuer:
            asset += ':{}...{}'.format(self.asset_issuer[:4], self.asset_issuer[-4:])
        return asset

    @property
    def asset(self):
        if self.asset_code == Asset.native().code and self.asset_issuer == '':
            return Asset.native()
        else:
            return Asset(code=self.asset_code, issuer=self.asset_issuer)

    @property
    def daily_bribe_amount(self):
        return Decimal(self.amount_for_bribes / self.DEFAULT_DURATION.days).quantize(
            Decimal('0.0000001'), rounding=ROUND_DOWN,
        )

    @property
    def daily_aqua_amount(self):
        return Decimal(self.amount_aqua / self.DEFAULT_DURATION.days).quantize(
            Decimal('0.0000001'), rounding=ROUND_DOWN,
        )


class AggregatedByAssetBribe(models.Model):
    market_key = models.ForeignKey(MarketKey, related_name='aggregated_bribes', null=True, on_delete=models.PROTECT)

    asset_code = models.CharField(max_length=12)
    asset_issuer = models.CharField(max_length=56)

    start_at = models.DateTimeField(null=True)
    stop_at = models.DateTimeField(null=True)

    total_reward_amount = models.DecimalField(max_digits=20, decimal_places=7, null=True)

    aqua_total_reward_amount_equivalent = models.DecimalField(max_digits=20, decimal_places=7, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        if self.asset.type == Asset.native().type:
            return self.asset.code
        return '{} ({}...{})'.format(
            self.asset.code, self.asset_issuer[:4], self.asset_issuer[-4:],
        )

    @property
    def daily_amount(self):
        return Decimal(self.total_reward_amount / Bribe.DEFAULT_DURATION.days).quantize(
            Decimal('0.0000001'), rounding=ROUND_DOWN,
        )

    @property
    def daily_aqua_equivalent(self):
        if not self.aqua_total_reward_amount_equivalent:
            return Decimal(0)

        return Decimal(self.aqua_total_reward_amount_equivalent / Bribe.DEFAULT_DURATION.days).quantize(
            Decimal('0.0000001'), rounding=ROUND_DOWN,
        )

    @property
    def asset(self):
        if self.asset_code == Asset.native().code and self.asset_issuer == '':
            return Asset.native()
        else:
            return Asset(code=self.asset_code, issuer=self.asset_issuer)
