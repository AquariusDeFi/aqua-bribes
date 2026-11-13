"""Immutable contract donor inputs. Unavailable history never falls back to Classic markers."""
import hashlib
import json
import re
from datetime import datetime
from datetime import timezone as datetime_timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

import requests
from stellar_sdk import StrKey

from aquarius_bribes.rewards.models import ContractDelegationSnapshot

CAPABILITY = 'aquarius.ice.vote_source_snapshot.v1'
HORIZON = 156 * 604800


def validate_source(identity, payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != identity:
        raise ValueError('Source snapshot identity mismatch')
    if (not StrKey.is_valid_contract(settings.CONTRACT_DELEGATION_LOCKER)
            or not StrKey.is_valid_contract(settings.CONTRACT_DELEGATION_UPVOTE_PROXY)):
        raise ValueError('Valid expected Locker and Upvote contract identities are required')
    config = payload['configuration']
    expected_network = hashlib.sha256(settings.STELLAR_PASSPHRASE.encode()).hexdigest()
    at = payload['effective_timestamp']
    if (payload.get('capability') != CAPABILITY or type(at) is not int
            or payload['day'] != datetime.fromtimestamp(at, datetime_timezone.utc).date().isoformat()
            or config.get('network_id') != expected_network
            or config.get('locker_contract_id') != settings.CONTRACT_DELEGATION_LOCKER
            or config.get('contracts', {}).get('Upvote') != settings.CONTRACT_DELEGATION_UPVOTE_PROXY
            or payload['projection']['complete'] is not True or payload['projection']['fresh'] is not True
            or payload['projection']['observed_ledger'] != payload['ledger']['sequence']
            or payload['ledger']['closed_at'] != at
            or not re.fullmatch('[0-9a-f]{64}', payload['ledger']['hash'])
            or payload.get('weight_denominator') != HORIZON):
        raise ValueError('Source snapshot network, contracts, timestamp or completeness mismatch')
    if not isinstance(payload.get('delegates'), dict):
        raise ValueError('Complete donor population is required')
    for delegate, donors in payload['delegates'].items():
        if not StrKey.is_valid_ed25519_public_key(delegate) or not isinstance(donors, list):
            raise ValueError('Invalid delegate or donor population')
        seen = set()
        for donor in donors:
            owner, weight = donor['donor'], donor['weight']
            if (not StrKey.is_valid_ed25519_public_key(owner) or owner in seen or owner == delegate
                    or not isinstance(weight, str) or not re.fullmatch('[1-9][0-9]{0,79}', weight)):
                raise ValueError('Invalid or duplicate donor weight')
            seen.add(owner)
    return datetime.fromtimestamp(at, datetime_timezone.utc)


def source_snapshot(snapshot_time=None):
    day = (snapshot_time or timezone.now()).astimezone(datetime_timezone.utc).date()
    existing = ContractDelegationSnapshot.objects.filter(day=day).first()
    if existing is None:
        base_url = settings.CONTRACT_DELEGATION_SNAPSHOT_URL
        if urlsplit(base_url).scheme != 'https' or not settings.CONTRACT_DELEGATION_SNAPSHOT_TOKEN:
            raise ValueError('An HTTPS snapshot service and its dedicated token are required')
        # Explicit historical requests can only read a retained snapshot. Current
        # creation is idempotent by UTC day/configuration at the producer.
        response = requests.request('GET' if snapshot_time else 'POST',
                                    base_url.rstrip('/') + '/api/proxy-vote-snapshots/' + day.isoformat() + '/',
                                    headers={'Authorization': 'Bearer ' + settings.CONTRACT_DELEGATION_SNAPSHOT_TOKEN},
                                    timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Immutable delegation snapshot is unavailable')
        value = response.json()
        at = validate_source(value['id'], value['payload'])
        if at.date() != day:
            raise ValueError('Source snapshot UTC day mismatch')
        existing, _ = ContractDelegationSnapshot.objects.get_or_create(day=day, defaults={
            'source_id': value['id'], 'payload': value['payload'],
        })
    at = validate_source(existing.source_id, existing.payload)
    if existing.day != at.date():
        raise ValueError('Stored source snapshot UTC day mismatch')
    if snapshot_time is not None and snapshot_time != at:
        raise ValueError('Requested timestamp does not match the immutable snapshot timestamp')
    return existing
