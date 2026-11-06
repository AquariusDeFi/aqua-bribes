import importlib
import os
import sys
from contextlib import ExitStack
from unittest import TestCase, mock


class WSGIStartupTests(TestCase):
    def test_new_relic_is_initialized_before_django_application(self):
        calls = []
        wrapped_application = object()
        agent = mock.Mock()

        def wrap_application(application):
            calls.append('wrapper')
            return wrapped_application

        agent.WSGIApplicationWrapper.side_effect = wrap_application

        def initialize(*args, **kwargs):
            calls.append('newrelic')
            return agent

        def get_application():
            calls.append('django')
            return object()

        sys.modules.pop('config.wsgi', None)
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {'NEWRELIC_DJANGO_ACTIVE': 'true'},
                ),
            )
            stack.enter_context(
                mock.patch(
                    'config.newrelic.initialize_new_relic',
                    side_effect=initialize,
                ),
            )
            stack.enter_context(
                mock.patch(
                    'django.core.wsgi.get_wsgi_application',
                    side_effect=get_application,
                ),
            )
            try:
                module = importlib.import_module('config.wsgi')
            finally:
                sys.modules.pop('config.wsgi', None)

        self.assertEqual(calls, ['newrelic', 'django', 'wrapper'])
        self.assertIs(module.application, wrapped_application)
