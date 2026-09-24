import json
import os
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('CODEX_HOME', None)
        language = patch("bridge.system_language", return_value="en")
        language.start()
        self.addCleanup(language.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        (self.project / 'bridge.py').write_text('# test bridge\n')
        self.runtime = self.home / 'runtime'
        self.runtime.mkdir()
        for name in ['node', 'cua-repl.mjs', 'sky.app', 'codex', 'node_repl']:
            (self.runtime / name).touch()
        self.definition = self.home / '.codex/plugins/cache/openai-bundled/unified-computer-use/1/.mcp.json'
        self.definition.parent.mkdir(parents=True)
        self.definition.write_text(json.dumps({'mcpServers': {'cua_repl': {
            'command': str(self.runtime / 'node'), 'args': [str(self.runtime / 'cua-repl.mjs')],
            'env': {'SKY_CUA_SERVICE_PATH': str(self.runtime / 'sky.app'),
                    'CODEX_CLI_PATH': str(self.runtime / 'codex'),
                    'CUA_REPL_NODE_REPL_PATH': str(self.runtime / 'node_repl'),
                    'BROWSER_USE_AVAILABLE_BACKENDS': 'chrome',
                    'NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME': 'browser instructions',
                    'PRESERVED_VALUE': 'do not print this'}}}}))
        self.config = self.home / '.claude.json'

    def run_install(self, **kwargs):
        return install.configure(self.home, project_dir=self.project, **kwargs)

    def read(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def test_discovery_and_missing_runtime(self):
        self.assertEqual(install.discover(self.home)[0], self.definition)
        (self.runtime / 'node').unlink()
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse(self.config.exists())

    def test_preserves_config_permissions_and_backs_up(self):
        original = {'mcpServers': {'other': {'command': 'other'}}, 'secret': 'preserve', 'projects': {'x': {}}}
        self.config.write_text(json.dumps(original))
        if os.name != 'nt':
            self.config.chmod(0o640)
        plan = self.run_install()
        result = self.read()
        self.assertEqual(result['secret'], original['secret'])
        self.assertEqual(result['projects'], original['projects'])
        self.assertEqual(result['mcpServers']['other'], original['mcpServers']['other'])
        self.assertEqual(json.loads(Path(plan['backup']).read_text(encoding='utf-8')), original)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        env = result['mcpServers']['cua_repl']['env']
        self.assertEqual(env['CUA_BRIDGE_AUTO_APPROVE_APPS'], '0')
        self.assertEqual(env['PRESERVED_VALUE'], 'do not print this')
        self.assertFalse(any('BROWSER' in k or 'CHROME' in k for k in env))
        self.assertEqual(json.loads(env['NODE_REPL_TRUSTED_SERVICES']), {'sky': '@oai/sky/service'})
        self.assertEqual(env['CUA_REPL_ENABLED_SURFACES'], 'computer')
        self.assertEqual(install.managed_path(self.home).read_text(), '# test bridge\n')

    def test_dry_run_writes_nothing_and_prints_no_env(self):
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        plan = self.run_install(dry_run=True, auto_approve_apps=True)
        after = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.home / '.claude').exists())
        self.assertNotIn('do not print this', json.dumps(plan))

    def test_auto_approve_is_explicit_and_upgrade_resets_default(self):
        self.run_install(auto_approve_apps=True)
        self.assertEqual(self.read()['mcpServers']['cua_repl']['env']['CUA_BRIDGE_AUTO_APPROVE_APPS'], '1')
        self.run_install()
        self.assertEqual(self.read()['mcpServers']['cua_repl']['env']['CUA_BRIDGE_AUTO_APPROVE_APPS'], '0')

    def test_unmanaged_server_not_overwritten_or_removed(self):
        self.config.write_text(json.dumps({'mcpServers': {'cua_repl': {'command': 'unknown'}}}))
        original = self.config.read_bytes()
        for uninstall in [False, True]:
            with self.assertRaises(ValueError):
                self.run_install(uninstall=uninstall)
            self.assertEqual(self.config.read_bytes(), original)

    def test_legacy_install_can_upgrade(self):
        old = str(self.home / '.claude/mcp-servers/cua-repl-bridge/bridge.py')
        self.config.write_text(json.dumps({'mcpServers': {'cua_repl': {
            'command': sys.executable, 'args': ['-B', old, str(self.runtime/'node'), str(self.runtime/'cua-repl.mjs')]}}}))
        self.run_install()
        self.assertEqual(self.read()['mcpServers']['cua_repl']['args'][1], str(install.managed_path(self.home)))

    def test_uninstall_only_removes_managed_entry(self):
        self.config.write_text(json.dumps({'mcpServers': {'other': {'command': 'keep'}}, 'keep': 42}))
        self.run_install()
        self.run_install(uninstall=True)
        self.assertEqual(self.read(), {'mcpServers': {'other': {'command': 'keep'}}, 'keep': 42})
        self.assertTrue(install.managed_path(self.home).exists())
        self.assertEqual(self.run_install(uninstall=True)['action'], 'nothing to uninstall')


    def test_windows_manifest_without_mac_helper_preserves_native_pipe(self):
        definition = json.loads(self.definition.read_text(encoding='utf-8'))
        runtime = definition['mcpServers']['cua_repl']
        runtime['env'].pop('SKY_CUA_SERVICE_PATH')
        runtime['env']['SKY_CUA_NATIVE_PIPE'] = '1'
        runtime['env']['SKY_CUA_NATIVE_PIPE_DIRECTORY'] = str(self.home / 'native-pipe')
        runtime['args'].insert(0, '--no-warnings')
        self.definition.write_text(json.dumps(definition), encoding='utf-8')
        with patch('install.sys.platform', 'win32'):
            self.run_install()
            installed = self.read()['mcpServers']['cua_repl']
            self.assertEqual(installed['args'][3:], runtime['args'])
            for key in ('SKY_CUA_NATIVE_PIPE', 'SKY_CUA_NATIVE_PIPE_DIRECTORY'):
                self.assertEqual(installed['env'][key], runtime['env'][key])
            self.run_install(uninstall=True)
        self.assertNotIn('cua_repl', self.read()['mcpServers'])

    def test_mac_manifest_requires_helper(self):
        definition = json.loads(self.definition.read_text(encoding='utf-8'))
        definition['mcpServers']['cua_repl']['env'].pop('SKY_CUA_SERVICE_PATH')
        self.definition.write_text(json.dumps(definition), encoding='utf-8')
        with patch('install.sys.platform', 'darwin'), self.assertRaises(install.RuntimeNotFoundError):
            install.discover(self.home)

    def test_python_exe_owned_and_other_commands_rejected(self):
        server = {'command': str(self.home / 'python.exe'),
                  'args': ['-B', str(install.managed_path(self.home)), 'node', 'runtime']}
        self.assertTrue(install.owned(server, self.home))
        server['command'] = str(self.home / 'Python3.14.EXE')
        self.assertTrue(install.owned(server, self.home))
        server['command'] = str(self.home / 'unrelated.exe')
        self.assertFalse(install.owned(server, self.home))

    def test_codex_home_override(self):
        alternate = self.home / 'alternate-codex'
        (self.home / '.codex').rename(alternate)
        with patch.dict(os.environ, {'CODEX_HOME': str(alternate)}):
            source, _ = install.discover(self.home)
        self.assertEqual(source, alternate / self.definition.relative_to(self.home / '.codex'))

    def test_unicode_configuration_roundtrip(self):
        original = {'language': '简体中文', 'projects': {'工作项目': {'name': '日本語'}}}
        self.config.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
        self.run_install()
        self.assertEqual(self.read()['projects'], original['projects'])
        self.run_install(uninstall=True)
        self.assertEqual(self.read(), {**original, 'mcpServers': {}})

    def test_windows_cli_supported_and_missing_runtime_explained(self):
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['install.py']), patch('install.sys.platform', 'win32'), patch('install.Path.home', return_value=self.home), redirect_stderr(errors), redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(), 0)
        (self.runtime / 'node').unlink()
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['install.py']), patch('install.sys.platform', 'win32'), patch('install.Path.home', return_value=self.home), redirect_stderr(errors):
            self.assertEqual(install.main(), 1)
        self.assertIn('No usable Codex computer-use runtime', errors.getvalue())

    def test_cli_localization_preserves_machine_json_and_hides_config_errors(self):
        cases = [('en', 'Show the plan', 'Plan prepared.', 'Installation failed'),
                 ('zh-Hans', '显示执行计划', '执行计划已生成。', '安装失败'),
                 ('zh-Hant', '顯示執行計畫', '執行計畫已產生。', '安裝失敗'),
                 ('ja', '実行計画を表示', '実行計画を作成しました。', 'インストールに失敗')]
        for language, help_text, success, error_text in cases:
            with self.subTest(language=language), patch('bridge.system_language', return_value=language):
                output = io.StringIO()
                with patch.object(sys, 'argv', ['install.py', '--help']), redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    install.main()
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(help_text, output.getvalue())
                output, errors = io.StringIO(), io.StringIO()
                plan = {'action': 'install', 'dry_run': True}
                with patch.object(sys, 'argv', ['install.py', '--dry-run']), patch('install.sys.platform', 'darwin'), patch('install.configure', return_value=plan), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(install.main(), 0)
                self.assertEqual(json.loads(output.getvalue()), plan)
                self.assertIn(success, errors.getvalue())
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, 'argv', ['install.py']), patch('install.sys.platform', 'darwin'), patch('install.configure', side_effect=ValueError('secret configuration')), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(install.main(), 1)
                self.assertIn(error_text, errors.getvalue())
                self.assertNotIn('secret configuration', errors.getvalue())
                self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
