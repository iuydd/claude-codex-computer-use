import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


class InstallTests(unittest.TestCase):
    def setUp(self):
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
        return json.loads(self.config.read_text())

    def test_discovery_and_missing_runtime(self):
        self.assertEqual(install.discover(self.home)[0], self.definition)
        (self.runtime / 'node').unlink()
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse(self.config.exists())

    def test_preserves_config_permissions_and_backs_up(self):
        original = {'mcpServers': {'other': {'command': 'other'}}, 'secret': 'preserve', 'projects': {'x': {}}}
        self.config.write_text(json.dumps(original))
        self.config.chmod(0o640)
        plan = self.run_install()
        result = self.read()
        self.assertEqual(result['secret'], original['secret'])
        self.assertEqual(result['projects'], original['projects'])
        self.assertEqual(result['mcpServers']['other'], original['mcpServers']['other'])
        self.assertEqual(json.loads(Path(plan['backup']).read_text()), original)
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


if __name__ == '__main__':
    unittest.main()
