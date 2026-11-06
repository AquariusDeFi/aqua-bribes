from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
import logging
from typing import List

import sentry_sdk
from django.conf import settings
from django.db import models
from django.utils import timezone

from billiard.exceptions import SoftTimeLimitExceeded
from stellar_sdk.exceptions import BaseHorizonError, NotFoundError
from stellar_sdk.exceptions import ConnectionError as StellarConnectionError
from stellar_sdk.transaction_builder import TransactionBuilder, TransactionEnvelope

from aquarius_bribes.bribes.utils import get_horizon
from aquarius_bribes.rewards.models import Payout

logger = logging.getLogger(__name__)


class BaseRewardPayer(object):
    payout_class = None

    def __init__(self, bribe, payer_wallet, reward_asset, reward_amount, stop_at=None):
        self.bribe = bribe
        self.asset = reward_asset
        self.payer_wallet = payer_wallet
        self.server = get_horizon()
        self.time_before_check_timeouted_transactions = 5
        self.stop_at = stop_at
        self.reward_amount = reward_amount
        # Vote IDs that hit a build_failure in this run. Marked retryable across
        # runs by _clean_rewards, but skipped within the current pay_reward loop
        # so a persistent horizon outage can't infinitely re-enqueue the same page.
        self._build_failure_vote_ids: set = set()

    def _clean_rewards(self, rewards):
        raise NotImplementedError()

    def _get_reward_page(self, rewards):
        qs = self._clean_rewards(rewards)
        if self._build_failure_vote_ids:
            qs = qs.exclude(id__in=self._build_failure_vote_ids)
        return list(qs[:100])

    def _get_memo(self):
        raise NotImplementedError()

    def _get_builder(self):
        server_account = self.server.load_account(self.payer_wallet.public_key)
        base_fee = settings.BASE_FEE

        memo = self._get_memo()

        builder = TransactionBuilder(
            source_account=server_account,
            network_passphrase=settings.STELLAR_PASSPHRASE,
            base_fee=base_fee,
        ).add_text_memo(memo)
        return builder

    def _get_payout_instance(self, reward, total_votes):
        raise NotImplementedError()

    def _generate_payouts(self, rewards_page: List, total_votes):
        payouts = []
        for reward in rewards_page:
            payouts.append(
                self._get_payout_instance(reward, total_votes),
            )
        return payouts

    def _append_payment_op(self, builder, payout):
        raise NotImplementedError()

    def _build_transaction(self, payouts) -> TransactionEnvelope:
        builder = self._get_builder()

        for payout in payouts:
            self._append_payment_op(builder, payout)

        return builder.build()

    def _process_page(self, rewards_page: List, total_votes):
        payouts = self._generate_payouts(rewards_page, total_votes)
        transaction_envelope = None

        try:
            # workaround to avoid fails because of unstable horizon
            transaction_envelope = self._build_transaction(payouts)
        except Exception as build_exc:  # noqa: BLE001 — intentional broad catch; we persist + log, do not silently swallow
            bribe_id = getattr(self.bribe, 'id', None)
            page_size = len(payouts)
            stop_at = self.stop_at
            logger.exception(
                'Failed to build payout transaction',
                extra={'bribe_id': bribe_id, 'page_size': page_size, 'stop_at': stop_at},
            )
            sentry_sdk.capture_exception(build_exc)

            message = 'build_failure: {}: {}'.format(
                type(build_exc).__name__, str(build_exc)[:200],
            )
            for payout in payouts:
                payout.stellar_transaction_id = ''
                payout.status = self.payout_class.STATUS_FAILED
                payout.message = message
            self.payout_class.objects.bulk_create(payouts)
            self._build_failure_vote_ids.update(
                p.vote_snapshot_id for p in payouts
            )
            return

        transaction_envelope.sign(self.payer_wallet.secret)

        try:
            response = self.server.submit_transaction(transaction_envelope)

            if response.get('successful', False):
                for payout in payouts:
                    payout.stellar_transaction_id = response['hash']
                self.payout_class.objects.bulk_create(payouts)
            else:
                for payout in payouts:
                    payout.stellar_transaction_id = response.get('hash', '') or transaction_envelope.hash_hex()
                    payout.status = self.payout_class.STATUS_FAILED
                    payout.message = 'unknown_response_no_successful_field'
                self.payout_class.objects.bulk_create(payouts)
                # In-run skip: the response lacked 'successful: true'. That is
                # retryable across runs (next hourly run re-checks the hash
                # via _clean_failed_payouts), but must NOT be retried inside
                # the current pay_reward while-loop — otherwise the same page
                # is re-enqueued every iteration until stop_at (or forever
                # when stop_at is None, as in tests).
                self._build_failure_vote_ids.update(
                    p.vote_snapshot_id for p in payouts
                )
        except SoftTimeLimitExceeded:
            for payout in payouts:
                payout.stellar_transaction_id = transaction_envelope.hash_hex()
                payout.status = self.payout_class.STATUS_FAILED
                payout.message = 'timeout'
            self.payout_class.objects.bulk_create(payouts)
        except StellarConnectionError:
            # Network layer failed mid-submit — the tx may or may not have
            # reached Horizon. Persist with the envelope hash and `timeout`
            # message so the next run re-checks it (same treatment as
            # SoftTimeLimitExceeded).
            for payout in payouts:
                payout.stellar_transaction_id = transaction_envelope.hash_hex()
                payout.status = self.payout_class.STATUS_FAILED
                payout.message = 'timeout'
            self.payout_class.objects.bulk_create(payouts)
        except BaseHorizonError as submit_exc:
            if getattr(submit_exc, 'status', None) in [504, 522]:
                for payout in payouts:
                    payout.stellar_transaction_id = transaction_envelope.hash_hex()
                    payout.status = self.payout_class.STATUS_FAILED
                    payout.message = 'timeout'
                self.payout_class.objects.bulk_create(payouts)
            else:
                failed_payouts = []

                if submit_exc.extras:
                    operation_fail_reasons = submit_exc.extras.get('result_codes', {}).get('operations', [])
                else:
                    operation_fail_reasons = None
                if not operation_fail_reasons:
                    if submit_exc.extras:
                        # Horizon returned a JSON body with result_codes but
                        # no transaction-level code. Unknown submit outcome —
                        # treat as `timeout` (5-min grace then re-verify).
                        # This preserves the "Horizon definitely has the tx"
                        # semantics of `unknown_response_no_successful_field`
                        # only for the response.get('successful') else-branch
                        # below, where the response body WAS received.
                        operation_fail_reasons = submit_exc.extras.get('result_codes', {}).get(
                            'transaction', 'timeout',
                        )
                    else:
                        # Horizon returned a non-JSON error body (e.g. 500 HTML
                        # page). We have no response body — the tx may or may
                        # not have reached Horizon at all. Treat as `timeout`
                        # so `_clean_failed_payouts` keeps the 5-min grace
                        # before re-checking: without the grace, a transient
                        # NotFoundError on the very next run would delete the
                        # row and let a fresh submit double-pay when the
                        # original tx eventually propagates.
                        operation_fail_reasons = 'timeout'

                    operation_fail_reasons = [operation_fail_reasons] * len(payouts)

                for index, code in enumerate(operation_fail_reasons):
                    if code != 'op_success':
                        payouts[index].message = code
                        payouts[index].status = self.payout_class.STATUS_FAILED
                        failed_payouts.append(payouts[index])
                    payouts[index].stellar_transaction_id = transaction_envelope.hash_hex()

                if operation_fail_reasons:
                    self.payout_class.objects.bulk_create(failed_payouts)
                    # In-run skip for any FAILED payout — `tx_bad_*` codes are
                    # retryable across runs via `_clean_rewards`, and without
                    # this set the while-loop in `pay_reward` would re-enqueue
                    # the same page forever when stop_at is None. Adding
                    # non-retryable codes (`timeout`, most op_*) too is a safe
                    # no-op — they're already excluded by `failed_by_unkown_reason`.
                    self._build_failure_vote_ids.update(
                        p.vote_snapshot_id for p in failed_payouts
                    )
        except Exception as unknown_exc:
            # Unexpected exception after the envelope was signed. We don't
            # know whether submit_transaction reached Horizon — raw str(exc)
            # is neither in the re-verification list nor in the retryable
            # set, so persisting it silently drops the voter from all future
            # runs. Capture to Sentry for observability and persist with the
            # envelope hash + `timeout` so `_clean_failed_payouts` keeps the
            # 5-min grace before re-checking the hash on Horizon. Using the
            # `timeout` message (instead of `unknown_response_no_successful_field`)
            # is critical: without the grace, a transient NotFoundError on
            # the very next run would delete the row and let a fresh submit
            # double-pay when the original tx eventually propagates.
            logger.exception(
                'Unexpected error submitting payout transaction',
                extra={'bribe_id': getattr(self.bribe, 'id', None)},
            )
            sentry_sdk.capture_exception(unknown_exc)
            for payout in payouts:
                payout.stellar_transaction_id = transaction_envelope.hash_hex()
                payout.status = self.payout_class.STATUS_FAILED
                payout.message = 'timeout'
            self.payout_class.objects.bulk_create(payouts)

    def _clean_failed_payouts(self, rewards):
        # Any FAILED Payout whose stellar_transaction_id was set from an
        # actual submit attempt (timeout or unverified-response) must have
        # its hash re-checked on Horizon before the row is allowed to be
        # retried. Without this check, making 'unknown_response_no_successful_field'
        # retryable in _clean_rewards would double-pay voters when Horizon
        # landed the tx but returned a malformed response.
        #
        # `timeout` keeps a 5-min grace period so the next run doesn't
        # race Horizon's own indexing lag (a submit can return
        # SoftTimeLimitExceeded mid-flight while the tx is still landing).
        # `unknown_response_no_successful_field` has no such lag — the
        # response body was received, we just can't tell if the tx landed.
        # Gating it behind the 5-min window would let an operator's manual
        # re-run during incident response re-enqueue and double-pay the
        # voter before the re-check ever fires. Always re-verify.
        timeout_cutoff = (
            timezone.now()
            - timedelta(minutes=self.time_before_check_timeouted_transactions)
        )
        # STATUS_FAILED guard is load-bearing: once a previous run upgraded
        # a row FAILED→SUCCESS but left the message intact (`'timeout'` /
        # `'unknown_response_no_successful_field'`), every subsequent run
        # would otherwise re-check the same hash. A transient NotFoundError
        # would then delete the SUCCESS row on the `.delete()` branches
        # below, re-enqueue the voter via `_clean_rewards`, and double-pay
        # on the next submit when the original tx lands later. Same status
        # filter on the update/delete operations below for defense in depth.
        uncertain_transactions = rewards.filter(
            payout__status=self.payout_class.STATUS_FAILED,
            payout__stellar_transaction_id__gt='',
        ).filter(
            models.Q(payout__message='unknown_response_no_successful_field')
            | (
                models.Q(payout__message='timeout')
                & models.Q(payout__created_at__lte=timeout_cutoff)
            )
        ).values_list(
            'payout__stellar_transaction_id', flat=True
        ).distinct()

        for tx_hash in uncertain_transactions:
            try:
                tx_data = self.server.transactions().transaction(tx_hash).call()

                if tx_data.get('successful', False):
                    self.payout_class.objects.filter(
                        stellar_transaction_id=tx_hash,
                        status=self.payout_class.STATUS_FAILED,
                    ).update(
                        status=self.payout_class.STATUS_SUCCESS,
                        message='reverified_after_timeout',
                    )
                else:
                    self.payout_class.objects.filter(
                        stellar_transaction_id=tx_hash,
                        status=self.payout_class.STATUS_FAILED,
                    ).delete()
            except NotFoundError:
                self.payout_class.objects.filter(
                    stellar_transaction_id=tx_hash,
                    status=self.payout_class.STATUS_FAILED,
                ).delete()

    def _exclude_small_votes(self, votes, total_votes):
        min_votes_value = Decimal(Decimal("0.0000001") * total_votes / self.reward_amount).quantize(
            Decimal('0.0000001'), rounding=ROUND_UP,
        )
        return votes.filter(votes_value__gte=min_votes_value)

    def pay_reward(self, votes, total_votes=None):
        self._clean_failed_payouts(votes)

        if total_votes is None:
            total_votes = votes.aggregate(
                total_votes=models.Sum('votes_value'),
            )['total_votes']
            votes = self._exclude_small_votes(votes, total_votes)

        page = self._get_reward_page(votes)
        while page:
            if self.stop_at and timezone.now() > self.stop_at:
                return

            self._process_page(page, total_votes)
            page = self._get_reward_page(votes)


class RewardPayer(BaseRewardPayer):
    payout_class = Payout

    def _clean_rewards(self, rewards):
        qs = rewards

        # build_failure: transient error during envelope construction (Horizon timeout,
        # account load flake, etc.). Must stay retryable so the next hourly run can recover.
        # unknown_response_no_successful_field: also retryable — covers transient Horizon
        # responses that lacked `successful: true` (e.g., HTML error page or upstream bug).
        retryable_failure = models.Q(message__in=[
            'tx_bad_auth', 'tx_bad_seq', 'tx_insufficient_balance', 'tx_insufficient_fee',
            'unknown_response_no_successful_field',
        ]) | models.Q(message__startswith='build_failure:')
        failed_by_unkown_reason = self.payout_class.objects.filter(
            bribe=self.bribe, vote_snapshot__in=qs,
        ).exclude(
            retryable_failure, status=self.payout_class.STATUS_FAILED,
        ).values_list('vote_snapshot_id').distinct()
        qs = qs.exclude(id__in=failed_by_unkown_reason)

        already_payed = rewards.filter(
            payout__status=self.payout_class.STATUS_SUCCESS, payout__bribe=self.bribe,
        ).values_list('id', flat=True)
        qs = qs.exclude(id__in=already_payed)

        return qs

    def _get_memo(self):
        return self.bribe.market_key.payout_memo

    def _get_payout_instance(self, vote, total_votes):
        reward_amount = (self.reward_amount * vote.votes_value / total_votes)
        reward_amount = reward_amount.quantize(Decimal('0.0000000'), rounding=ROUND_DOWN)
        return self.payout_class(
            vote_snapshot=vote,
            bribe=self.bribe,
            asset_code=self.asset.code,
            asset_issuer=self.asset.issuer or '',
            reward_amount=reward_amount,
            status=self.payout_class.STATUS_SUCCESS,
        )

    def _append_payment_op(self, builder, payout):
        builder.append_payment_op(
            destination=payout.vote_snapshot.voting_account,
            asset=self.bribe.asset,
            source=self.payer_wallet.public_key,
            amount=payout.reward_amount,
        )
