import environ

# Build paths inside the project like this: root(...)
env = environ.Env()

root = environ.Path(__file__) - 3
apps_root = root.path('aquarius_bribes')

BASE_DIR = root()


# Base configurations
# --------------------------------------------------------------------------

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'


# Application definition
# --------------------------------------------------------------------------

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.sitemaps',
]

THIRD_PARTY_APPS = [
    'drf_secure_token',
    'rest_framework',
    'corsheaders',
    'django_filters',
    'constance',
]

LOCAL_APPS = [
    'aquarius_bribes.mailing',
    'aquarius_bribes.taskapp',
    'aquarius_bribes.bribes',
    'aquarius_bribes.rewards',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# Middleware configurations
# --------------------------------------------------------------------------

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.contrib.sites.middleware.CurrentSiteMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]


# Template configurations
# --------------------------------------------------------------------------

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            root('aquarius_bribes', 'templates'),
        ],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'aquarius_bribes.context_processors.google_analytics',
            ],
            'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ],
        },
    },
]


# Fixture configurations
# --------------------------------------------------------------------------

FIXTURE_DIRS = [
    root('aquarius_bribes', 'fixtures'),
]


# Password validation
# https://docs.djangoproject.com/en/1.9/ref/settings/#auth-password-validators
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/1.9/topics/i18n/
# --------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True

SITE_ID = 1


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.9/howto/static-files/
# --------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = root('static')

STATICFILES_FINDERS = (
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
    'django.contrib.staticfiles.finders.FileSystemFinder',
)

STATICFILES_DIRS = [
    root('aquarius_bribes', 'assets'),
]

MEDIA_URL = '/media/'
MEDIA_ROOT = root('media')


CELERY_ENABLED = env.bool('CELERY_ENABLED', default=True)
if CELERY_ENABLED:
    # Celery configuration
    # --------------------------------------------------------------------------

    CELERY_ACCEPT_CONTENT = ['json']
    CELERY_TASK_SERIALIZER = 'json'
    CELERY_TASK_IGNORE_RESULT = True


# Django mailing configuration
# --------------------------------------------------------------------------

if CELERY_ENABLED:
    TEMPLATED_EMAIL_BACKEND = 'aquarius_bribes.mailing.backends.AsyncTemplateBackend'
    MAILING_USE_CELERY = True

TEMPLATED_EMAIL_TEMPLATE_DIR = 'email'
TEMPLATED_EMAIL_FILE_EXTENSION = 'html'

MAILING_USE_CELERY = True


# Rest framework configuration
# http://www.django-rest-framework.org/api-guide/settings/
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    'PAGE_SIZE': 10,
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'drf_secure_token.authentication.SecureTokenAuthentication',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}


# Horizon configuration
# --------------------------------------------------------------------------

STELLAR_PASSPHRASE = NotImplemented
HORIZON_URL = NotImplemented
SOROBAN_RPC_URL = NotImplemented
BASE_FEE = 10 ** 5 * 2

MARKETKEYS_TRACKER_URL = NotImplemented


# Bribe configuration
# --------------------------------------------------------------------------
from decimal import Decimal

BRIBE_WALLET_ADDRESS = NotImplemented
BRIBE_WALLET_SIGNER = NotImplemented

REWARD_ASSET_CODE = NotImplemented
REWARD_ASSET_ISSUER = NotImplemented

AMM_PROTOCOL_BRIBES_ADMIN_ADDRESS = NotImplemented

CONSTANCE_CONFIG = {
    'CONVERTATION_AMOUNT': (Decimal('100000'), 'Amount in aqua needed for bribe accept', Decimal),
}
CONSTANCE_BACKEND = 'constance.backends.database.DatabaseBackend'

CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://aqua\.network$",
    r"^https://[a-zA-Z0-9.-]+\.aqua\.network$",
    r"^https://[a-zA-Z0-9.-]+\.netlify\.app$",
    r"^http://localhost:[0-9.-]+$",
]

HORIZON_REQUEST_USER_AGENT = 'Aqua-Bribes-Backend'
DEFAULT_GET_TIMEOUT = 60

REWARD_SERVER_AUTHORIZATION_TOKEN = NotImplemented

DELEGATE_MARKER = NotImplemented
DELEGATABLE_ASSETS = NotImplemented

