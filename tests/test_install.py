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
        for key in ('CODEX_HOME', 'APPDATA', 'LOCALAPPDATA', 'CLAUDE_USER_DATA_DIR'):
            os.environ.pop(key, None)
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
        self.legacy_backup({})
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

    def legacy_backup(self, config):
        path = self.home / '.claude/backups/claude.json.before-cua-repl-20260924-103502'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding='utf-8')
        return path

    def desktop_file(self, data=None):
        path = install.desktop_config_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data or {'preferences': {'chicagoEnabled': False, 'other': 7}, 'keep': 8}), encoding='utf-8')
        return path

    def test_uninstall_restores_baseline_and_enables_desktop_preserving_changes(self):
        self.run_install()
        state_path = install.managed_path(self.home).parent / 'install-state.json'
        first_state = state_path.read_bytes()
        self.run_install(auto_approve_apps=True)
        self.assertEqual(first_state, state_path.read_bytes())
        current = self.read()
        current['later'] = 'keep me'
        current['mcpServers']['other'] = {'command': 'keep'}
        self.config.write_text(json.dumps(current), encoding='utf-8')
        desktop = self.desktop_file()
        plan = self.run_install(uninstall=True)
        self.assertEqual(self.read(), {'later': 'keep me', 'mcpServers': {'other': {'command': 'keep'}}})
        self.assertEqual(json.loads(desktop.read_text()), {'preferences': {'chicagoEnabled': True, 'other': 7}, 'keep': 8})
        self.assertEqual(plan['desktop_computer_use'], 'enabled')
        self.assertFalse(json.loads(state_path.read_text())['active'])
        self.assertTrue(install.managed_path(self.home).exists())

    def test_legacy_original_entry_restored_and_unknown_baseline_refused(self):
        old = {'command': sys.executable, 'args': ['-B', str(self.home / '.claude/mcp-servers/cua-repl-bridge/bridge.py'), 'node', 'runtime']}
        self.config.write_text(json.dumps({'mcpServers': {'cua_repl': old}}))
        original = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.run_install(uninstall=True)
        self.assertEqual(original, self.config.read_bytes())
        baseline = {'command': 'original-runtime', 'args': ['original']}
        self.legacy_backup({'mcpServers': {'cua_repl': baseline}})
        self.run_install()
        self.run_install(uninstall=True)
        self.assertEqual(self.read()['mcpServers']['cua_repl'], baseline)
        self.assertEqual(self.run_install(uninstall=True)['action'], 'nothing to uninstall')

    def test_uninstall_dry_run_missing_desktop_and_malformed_desktop(self):
        self.run_install()
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        plan = self.run_install(uninstall=True, dry_run=True)
        self.assertIn('missing', plan['desktop_computer_use'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        desktop = self.desktop_file()
        desktop.write_text('{bad json')
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.run_install(uninstall=True)
        self.assertEqual(self.config.read_bytes(), before)

    def test_malformed_state_refused(self):
        self.run_install()
        state = install.managed_path(self.home).parent / 'install-state.json'
        state.write_text('{}')
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.run_install(uninstall=True)
        self.assertEqual(before, self.config.read_bytes())

    def test_state_symlink_refused(self):
        self.run_install()
        state = install.managed_path(self.home).parent / 'install-state.json'
        target = state.with_name('actual-state.json')
        state.rename(target)
        try:
            state.symlink_to(target)
        except OSError:
            # Exercise the refusal even when Windows denies creating real symlinks.
            original_is_symlink = Path.is_symlink
            with patch.object(Path, 'is_symlink', lambda path: path == state or original_is_symlink(path)):
                with self.assertRaises(ValueError):
                    self.run_install(uninstall=True)
        else:
            with self.assertRaises(ValueError):
                self.run_install(uninstall=True)
        self.assertTrue(target.exists())

    def test_partial_failure_rolls_back_desktop_and_mcp(self):
        self.run_install()
        desktop = self.desktop_file()
        state = install.managed_path(self.home).parent / 'install-state.json'
        before = {p: p.read_bytes() for p in (desktop, self.config, state)}
        real_write = install.atomic_write
        failed = False
        def fail_once(path, content, *args):
            nonlocal failed
            if Path(path) == state and not failed:
                failed = True
                raise OSError('simulated disk error')
            return real_write(path, content, *args)
        with patch('install.atomic_write', side_effect=fail_once), self.assertRaises(OSError):
            self.run_install(uninstall=True)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertTrue(list((self.home / '.claude/backups').glob('*')))

    def test_windows_multiple_desktop_configs_require_selection(self):
        with patch('install.sys.platform', 'win32'):
            first = self.desktop_file()
            second = self.home / 'AppData/Local/Claude/claude_desktop_config.json'
            second.parent.mkdir(parents=True)
            second.write_text('{}')
            with self.assertRaises(ValueError):
                install.desktop_config_path(self.home)
            self.assertEqual(install.desktop_config_path(self.home, first), first)

    def test_desktop_process_guard(self):
        import subprocess
        for platform, result, fails in [('darwin', subprocess.CompletedProcess([], 1), False),
                                       ('darwin', subprocess.CompletedProcess([], 0), True),
                                       ('win32', subprocess.CompletedProcess([], 0, '"Claude.exe","10"'), True),
                                       ('win32', subprocess.CompletedProcess([], 0, 'INFO: no tasks'), False)]:
            with self.subTest(platform=platform, fails=fails), patch('install.sys.platform', platform), patch('install.subprocess.run', return_value=result):
                if fails:
                    with self.assertRaises(install.RuntimeNotFoundError):
                        install.require_desktop_closed()
                else:
                    install.require_desktop_closed()
        with patch('install.subprocess.run', side_effect=OSError('missing tool')), self.assertRaises(install.RuntimeNotFoundError):
            install.require_desktop_closed()

    def test_inactive_state_with_external_bridge_reinstall_uninstalls_again(self):
        self.run_install()
        installed = self.read()['mcpServers']['cua_repl']
        self.run_install(uninstall=True)
        restored = self.read()
        restored['mcpServers']['cua_repl'] = installed
        self.config.write_text(json.dumps(restored))
        self.assertEqual(self.run_install(uninstall=True)['action'], 'uninstall')
        self.assertNotIn('cua_repl', self.read()['mcpServers'])
        restored['mcpServers']['cua_repl'] = {'command': 'unmanaged'}
        self.config.write_text(json.dumps(restored))
        with self.assertRaises(install.ActionRequiredError):
            self.run_install(uninstall=True)

    def test_desktop_config_cannot_alias_managed_files(self):
        self.run_install()
        destination = install.managed_path(self.home)
        state = destination.parent / 'install-state.json'
        before = {path: path.read_bytes() for path in (self.config, destination, state)}
        for target in (self.config, destination, state, destination.parent / '..' / destination.parent.name / 'install-state.json'):
            with self.subTest(target=target), self.assertRaises(install.ActionRequiredError):
                self.run_install(uninstall=True, desktop_config=target)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_actionable_cli_errors_are_visible(self):
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['install.py']), patch('install.sys.platform', 'win32'), patch('install.configure', side_effect=install.ActionRequiredError('Use --desktop-config PATH')), redirect_stderr(errors):
            self.assertEqual(install.main(), 1)
        self.assertIn('--desktop-config PATH', errors.getvalue())

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
