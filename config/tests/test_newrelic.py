import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import TestCase, mock

from config.newrelic import initialize_new_relic


class InitializeNewRelicTests(TestCase):
    def test_disabled_agent_is_not_imported(self):
        with mock.patch.dict(sys.modules, {'newrelic': None}):
            agent = initialize_new_relic(False, Path('/missing/newrelic.ini'), environ={})

        self.assertIsNone(agent)

    def test_missing_or_placeholder_license_does_not_import_agent(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / 'newrelic.ini'

            for license_key in ('', 'undefined', '<change me>'):
                with self.subTest(license_key=license_key):
                    config_path.write_text(
                        '[newrelic]\nlicense_key = {0}\n'.format(license_key),
                        encoding='utf-8',
                    )
                    with mock.patch.dict(sys.modules, {'newrelic': None}):
                        agent = initialize_new_relic(True, config_path, environ={})

                    self.assertIsNone(agent)

    def test_enabled_agent_with_license_is_initialized(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / 'newrelic.ini'
            config_path.write_text('[newrelic]\nlicense_key = test-key\n', encoding='utf-8')
            agent_module = ModuleType('newrelic.agent')
            agent_module.initialize = mock.Mock()
            newrelic_module = ModuleType('newrelic')
            newrelic_module.agent = agent_module

            with mock.patch.dict(
                sys.modules,
                {'newrelic': newrelic_module, 'newrelic.agent': agent_module},
            ):
                agent = initialize_new_relic(True, config_path, environ={})

        self.assertIs(agent, agent_module)
        agent_module.initialize.assert_called_once_with(str(config_path))
