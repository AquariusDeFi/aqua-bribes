"""
WSGI config for test_project project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/2.2/howto/deployment/wsgi/
"""

import os
from pathlib import Path

import environ

from config.newrelic import initialize_new_relic

newrelic_active = environ.Env().bool('NEWRELIC_DJANGO_ACTIVE', default=False)
newrelic_config = Path(__file__).resolve().parent.parent / 'conf' / 'newrelic.ini'
newrelic_agent = initialize_new_relic(
    newrelic_active,
    newrelic_config,
    environ=os.environ,
)

from django.core.wsgi import get_wsgi_application  # noqa I001

application = get_wsgi_application()

if newrelic_agent is not None:
    application = newrelic_agent.WSGIApplicationWrapper(application)
