import sentry_sdk
from kombu import Exchange, Queue  # NOQA
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

from config.settings.base import *  # noqa: F403
from config.settings.utils import parse_delegatable_asset_config

environ.Env.read_env()


DEBUG = False

ADMINS = env.json('ADMINS')

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')

SECRET_KEY = env('SECRET_KEY')


# Database
# https://docs.djangoproject.com/en/1.9/ref/settings/#databases
# --------------------------------------------------------------------------

DATABASES = {
    'default': env.db(),
}

# Cache
# --------------------------------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient"
        },
        "KEY_PREFIX": "aquarius",
    },
}


# Template
# --------------------------------------------------------------------------

TEMPLATES[0]['OPTIONS']['loaders'] = [
    ('django.template.loaders.cached.Loader', [
        'django.template.loaders.filesystem.Loader',
        'django.template.loaders.app_directories.Loader',
    ]),
]


# --------------------------------------------------------------------------

USE_COMPRESSOR = env.bool('USE_COMPRESSOR')
USE_CLOUDFRONT = env.bool('USE_CLOUDFRONT')
USE_HTTPS = env.bool('USE_HTTPS')
if USE_HTTPS:
    LETSENCRYPT_DIR = env('LETSENCRYPT_DIR', default='/opt/letsencrypt/')


# Storage configurations
# --------------------------------------------------------------------------

USE_AWS = env('AWS_STORAGE_BUCKET_NAME', default=False)

STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'

if USE_AWS:
    AWS_STORAGE_BUCKET_NAME = env('AWS_STORAGE_BUCKET_NAME')
    AWS_ACCESS_KEY_ID = env('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = env('AWS_SECRET_ACCESS_KEY')
    AWS_AUTO_CREATE_BUCKET = True


    AWS_QUERYSTRING_AUTH = False
    AWS_S3_SECURE_URLS = USE_HTTPS


    if USE_CLOUDFRONT:
        AWS_S3_CUSTOM_DOMAIN = env('AWS_S3_CUSTOM_DOMAIN')
    else:
        AWS_S3_CUSTOM_DOMAIN = '{0}.s3.amazonaws.com'.format(AWS_STORAGE_BUCKET_NAME)

    STATIC_URL = 'http{0}://{1}/static/'.format('s' if USE_HTTPS else '', AWS_S3_CUSTOM_DOMAIN)
    MEDIA_URL = 'http{0}://{1}/media/'.format('s' if USE_HTTPS else '', AWS_S3_CUSTOM_DOMAIN)

    DEFAULT_FILE_STORAGE = 'config.settings.s3utils.MediaRootS3BotoStorage'
    STATICFILES_STORAGE = 'config.settings.s3utils.StaticRootS3BotoStorage'


# Compressor & Cloudfront settings
# --------------------------------------------------------------------------

if USE_CLOUDFRONT or USE_COMPRESSOR:
    AWS_HEADERS = {'Cache-Control': str('public, max-age=604800')}

if USE_COMPRESSOR:
    INSTALLED_APPS += ('compressor',)
    STATICFILES_FINDERS += ('compressor.finders.CompressorFinder',)

    # See: http://django_compressor.readthedocs.org/en/latest/settings/#django.conf.settings.COMPRESS_ENABLED
    COMPRESS_ENABLED = True

    COMPRESS_STORAGE = STATICFILES_STORAGE

    # See: http://django-compressor.readthedocs.org/en/latest/settings/#django.conf.settings.COMPRESS_CSS_HASHING_METHOD
    COMPRESS_CSS_HASHING_METHOD = 'content'

    COMPRESS_CSS_FILTERS = (
        'config.settings.abs_compress.CustomCssAbsoluteFilter',
        'compressor.filters.cssmin.CSSMinFilter',
    )

    COMPRESS_OFFLINE = True
    COMPRESS_OUTPUT_DIR = 'cache'
    COMPRESS_CACHE_BACKEND = 'locmem'


# Email settings
# --------------------------------------------------------------------------

EMAIL_CONFIG = env.email()
vars().update(EMAIL_CONFIG)

SERVER_EMAIL_SIGNATURE = env('SERVER_EMAIL_SIGNATURE', default='aquarius_bribes'.capitalize())
DEFAULT_FROM_EMAIL = SERVER_EMAIL = SERVER_EMAIL_SIGNATURE + ' <{0}>'.format(env('SERVER_EMAIL'))


# Google analytics settings
# --------------------------------------------------------------------------

GOOGLE_ANALYTICS_PROPERTY_ID = env('GA_PROPERTY_ID', default='')
GA_ENABLED = bool(GOOGLE_ANALYTICS_PROPERTY_ID)


if CELERY_ENABLED:
    # Celery configurations
    # http://docs.celeryproject.org/en/latest/configuration.html
    # --------------------------------------------------------------------------

    CELERY_BROKER_URL = env('CELERY_BROKER_URL')

    CELERY_TASK_DEFAULT_QUEUE = 'aquarius_bribes-celery-queue'
    CELERY_TASK_DEFAULT_EXCHANGE = 'aquarius_bribes-exchange'
    CELERY_TASK_DEFAULT_ROUTING_KEY = 'celery.aquarius_bribes'
    CELERY_TASK_QUEUES = (
        Queue(
            CELERY_TASK_DEFAULT_QUEUE,
            Exchange(CELERY_TASK_DEFAULT_EXCHANGE),
            routing_key=CELERY_TASK_DEFAULT_ROUTING_KEY,
        ),
    )


# New Relic configurations
# --------------------------------------------------------------------------

# Enable/disable run newrelic python agent with django application.
NEWRELIC_DJANGO_ACTIVE = env.bool('NEWRELIC_DJANGO_ACTIVE', default=False)

# Sentry config
# -------------

SENTRY_DSN = env('SENTRY_DSN', default='')
SENTRY_ENABLED = True if SENTRY_DSN else False

if SENTRY_ENABLED:
    sentry_sdk.init(
        SENTRY_DSN,
        traces_sample_rate=0.2,
        integrations=[DjangoIntegration(), CeleryIntegration()],
    )


# Horizon configuration
# --------------------------------------------------------------------------

STELLAR_PASSPHRASE = 'Public Global Stellar Network ; September 2015'
HORIZON_URL = env('HORIZON_URL', default='https://horizon.stellar.org')
SOROBAN_RPC_URL = env('SOROBAN_RPC_URL', default='https://mainnet.sorobanrpc.com')

MARKETKEYS_TRACKER_URL = env('MARKETKEYS_TRACKER_URL', default='https://marketkeys-tracker.aqua.network')

# Bribe configuration
# --------------------------------------------------------------------------
from decimal import Decimal

BRIBE_WALLET_ADDRESS = env('BRIBE_WALLET_ADDRESS')
BRIBE_WALLET_SIGNER = env('BRIBE_WALLET_SIGNER')

REWARD_ASSET_CODE = env('REWARD_ASSET_CODE')
REWARD_ASSET_ISSUER = env('REWARD_ASSET_ISSUER')

REWARD_SERVER_AUTHORIZATION_TOKEN = env('REWARD_SERVER_AUTHORIZATION_TOKEN')

DELEGATE_MARKER = env('DELEGATE_MARKER')
DELEGATABLE_ASSETS = [
    parse_delegatable_asset_config(config)
    for config in env.list('DELEGATABLE_ASSETS')
]

AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS = env('AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS')
