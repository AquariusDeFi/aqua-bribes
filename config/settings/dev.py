from config.settings.base import *  # noqa: F403

DEBUG = True
TEMPLATES[0]['OPTIONS']['debug'] = DEBUG

SECRET_KEY = env('SECRET_KEY', default='test_key')

ALLOWED_HOSTS = ['*']
INTERNAL_IPS = ['127.0.0.1']

ADMINS = (
    ('Dev Email', env('DEV_ADMIN_EMAIL', default='admin@localhost')),
)
MANAGERS = ADMINS


# Database
# https://docs.djangoproject.com/en/1.9/ref/settings/#databases
# --------------------------------------------------------------------------

DATABASES = {
    'default': env.db(default='postgres://localhost/aquarius_bribes'),
}


# Email settings
# --------------------------------------------------------------------------

DEFAULT_FROM_EMAIL = 'noreply@example.com'
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

if CELERY_ENABLED:
    MAILING_USE_CELERY = False


# Debug toolbar installation
# --------------------------------------------------------------------------

INSTALLED_APPS += (
    'debug_toolbar',
)

MIDDLEWARE += [
    'debug_toolbar.middleware.DebugToolbarMiddleware',
]
INTERNAL_IPS = ('127.0.0.1',)


if CELERY_ENABLED:
    # Celery configurations
    # http://docs.celeryproject.org/en/latest/configuration.html
    # --------------------------------------------------------------------------

    CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='amqp://guest@localhost//')

    CELERY_TASK_ALWAYS_EAGER = True


# New Relic configurations
# --------------------------------------------------------------------------

NEWRELIC_DJANGO_ACTIVE = False

# Sentry config
# -------------

SENTRY_ENABLED = False


# Horizon configuration
# --------------------------------------------------------------------------

STELLAR_PASSPHRASE = 'Test SDF Network ; September 2015'
HORIZON_URL = 'https://horizon-testnet.stellar.org'
SOROBAN_RPC_URL = 'https://soroban-testnet.stellar.org'

MARKETKEYS_TRACKER_URL = env('MARKETKEYS_TRACKER_URL', default='https://marketkeys-tracker-testnet.aqua.network')


# Bribe configuration
# --------------------------------------------------------------------------

BRIBE_WALLET_ADDRESS = 'GCM375EAU2Y6E2LTDPSDNGZ4SLXHR3T2GYK7J6XOOUGXZAOYUMDOWTW5'
BRIBE_WALLET_SIGNER = 'SD67FI2JJOTOGEMIJGDZYO4ZPFCQAYNGSKPVSPKA6UKDECASQ2JBP2Y6'

REWARD_ASSET_CODE = 'ZZZ'
REWARD_ASSET_ISSUER = 'GASMJGGEFR6SSKEYWNDK23BYDYETI53HHFJ5WRHUE2N5CDOBKXAY7FO2'

REWARD_SERVER_AUTHORIZATION_TOKEN = 'test token'

DELEGATE_MARKER = 'GDR6DKRJY25OCF25LV3FK7BCEW4BMR2CBFJD457P42AKGPY3C2IXJAXZ'
DELEGATABLE_ASSETS = 'upvoteICE:GDN2OYI5BE5EADJH6JCDITZPDZ35G5XJS6LMI3UIQGKCQZFXEVROI5AM;dUpvoteICE:GDN2OYI5BE5EADJH6JCDITZPDZ35G5XJS6LMI3UIQGKCQZFXEVROI5AM'

AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS = 'GDEM5HH6I22MN5TWYCHRTTIOY7D5UCMLAWOKMIXINHBONFLKVBJJI7HO'
