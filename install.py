#!/usr/bin/env python3
"""Install the bridge using an existing local Codex computer-use runtime."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid


def managed_path(home):
    return Path(home) / '.claude/mcp-servers/claude-codex-computer-use/bridge.py'


def owned(server, home):
    if not isinstance(server, dict):
        return False
    args = server.get('args', [])
    paths = {str(managed_path(home)), str(Path(home) / '.claude/mcp-servers/cua-repl-bridge/bridge.py')}
    return (isinstance(args, list) and len(args) >= 4 and all(isinstance(arg, str) for arg in args) and args[0] == '-B'
            and args[1] in paths and isinstance(server.get('command'), str)
            and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(server['command']).name) is not None)


def discover(home):
    root = Path(home) / '.codex/plugins/cache/openai-bundled/unified-computer-use'
    candidates = sorted(root.glob('*/.mcp.json'), key=lambda p: p.parent.stat().st_mtime, reverse=True)
    for source in candidates:
        try:
            server = json.loads(source.read_text())['mcpServers']['cua_repl']
            command, args = server['command'], server['args']
            if not isinstance(command, str) or not isinstance(args, list) or not args:
                continue
            if not all(isinstance(p, str) and Path(p).is_absolute() and Path(p).is_file() for p in [command, *args]):
                continue
            env = server.get('env', {})
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                continue
            for key in ('SKY_CUA_SERVICE_PATH', 'CODEX_CLI_PATH', 'CUA_REPL_NODE_REPL_PATH'):
                if key not in env or not Path(env[key]).exists():
                    raise ValueError('Missing runtime')
            return source, copy.deepcopy(server)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise ValueError('No usable Codex computer-use runtime found. Install/enable the Codex computer-use plugin first.')


def atomic_write(path, content, default_mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing to replace a symbolic-link configuration or bridge file.')
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else default_mode
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(home, *, dry_run=False, uninstall=False, auto_approve_apps=False, project_dir=None):
    home = Path(home)
    config_path = home / '.claude.json'
    if config_path.is_symlink():
        raise ValueError('Refusing to replace a symbolic-link configuration.')
    original = config_path.read_bytes() if config_path.exists() else None
    config = json.loads(original) if original is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get('mcpServers', {}), dict):
        raise ValueError('Claude MCP configuration must be an object.')
    servers = config.get('mcpServers', {})
    current = servers.get('cua_repl')
    if 'cua_repl' in servers and not owned(current, home):
        raise ValueError('An unmanaged cua_repl server exists; refusing to overwrite or remove it.')
    if uninstall:
        if current is None:
            return {'action': 'nothing to uninstall', 'dry_run': dry_run}
        del servers['cua_repl']
        plan = {'action': 'uninstall', 'config': str(config_path), 'dry_run': dry_run}
    else:
        source, runtime = discover(home)
        bridge_source = Path(project_dir or Path(__file__).parent) / 'bridge.py'
        content = bridge_source.read_bytes()
        destination = managed_path(home)
        if destination.is_symlink():
            raise ValueError('Refusing to replace a symbolic-link bridge file.')
        env = {k: v for k, v in runtime.get('env', {}).items()
               if not any(word in k.upper() for word in ('BROWSER', 'CHROME', 'IAB'))}
        env['CUA_REPL_ENABLED_SURFACES'] = 'computer'
        env['NODE_REPL_TRUSTED_SERVICES'] = '{"sky":"@oai/sky/service"}'
        env['CUA_BRIDGE_AUTO_APPROVE_APPS'] = '1' if auto_approve_apps else '0'
        config.setdefault('mcpServers', {})['cua_repl'] = {
            'type': 'stdio', 'command': sys.executable,
            'args': ['-B', str(destination), runtime['command'], *runtime['args']], 'env': env}
        plan = {'action': 'install', 'config': str(config_path), 'bridge': str(destination),
                'runtime_definition': str(source), 'auto_approve_apps': auto_approve_apps, 'dry_run': dry_run}
    if dry_run:
        return plan
    # Recheck before writing so a concurrent Claude settings update is not silently lost.
    if (config_path.read_bytes() if config_path.exists() else None) != original:
        raise ValueError('Claude configuration changed during installation; retry.')
    if original is not None:
        backup = home / '.claude/backups' / ('claude.json.before-codex-cua-bridge-' + uuid.uuid4().hex)
        atomic_write(backup, original, stat.S_IMODE(config_path.stat().st_mode))
        plan['backup'] = str(backup)
    if not uninstall:
        atomic_write(destination, content)
    atomic_write(config_path, (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode())
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Show the plan without changing files.')
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--uninstall', action='store_true', help='Remove only this managed MCP entry; keep files.')
    choice.add_argument('--auto-approve-apps', action='store_true', help='Explicitly authorize automatic native-app access approvals.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This integration requires macOS.')
    try:
        plan = configure(Path.home(), **vars(args))
    except (OSError, ValueError) as error:
        # Do not print configuration values or JSON parse excerpts.
        print(f'Installation failed ({type(error).__name__}). Check runtime paths, config JSON, and conflicting cua_repl entries.', file=sys.stderr)
        return 1
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
