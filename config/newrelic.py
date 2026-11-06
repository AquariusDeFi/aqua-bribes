import os
from configparser import ConfigParser, Error
from pathlib import Path

MISSING_LICENSE_KEYS = frozenset({
    '',
    '<change me>',
    'none',
    'null',
    'undefined',
})


def _read_license_key(config_path, environ):
    if 'NEW_RELIC_LICENSE_KEY' in environ:
        return environ['NEW_RELIC_LICENSE_KEY']

    parser = ConfigParser(interpolation=None)
    try:
        with config_path.open(encoding='utf-8') as config_file:
            parser.read_file(config_file)
        return parser.get('newrelic', 'license_key', fallback='')
    except (Error, OSError):
        return ''


def initialize_new_relic(active, config_path, environ=None):
    """Initialize New Relic only when it is enabled and has a license key."""
    if not active:
        return None

    environ = os.environ if environ is None else environ
    config_path = Path(config_path)
    license_key = _read_license_key(config_path, environ).strip()
    if license_key.lower() in MISSING_LICENSE_KEYS:
        return None

    import newrelic.agent

    config_file = str(config_path) if config_path.is_file() else None
    newrelic.agent.initialize(config_file)
    return newrelic.agent
