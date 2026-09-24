#!/usr/bin/env python3
"""Install the bridge using an existing local Codex computer-use runtime."""
import argparse
import copy
import csv
import io
import subprocess
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid

from bridge import tr


class ActionRequiredError(ValueError):
    """A safe, localized message the CLI may display without configuration values."""


class RuntimeNotFoundError(ActionRequiredError):
    """No valid installed runtime manifest was discovered."""


def managed_path(home):
    return Path(home) / '.claude/mcp-servers/claude-codex-computer-use/bridge.py'


def owned(server, home):
    if not isinstance(server, dict):
        return False
    args = server.get('args', [])
    paths = {str(managed_path(home)), str(Path(home) / '.claude/mcp-servers/cua-repl-bridge/bridge.py')}
    return (isinstance(args, list) and len(args) >= 4 and all(isinstance(arg, str) for arg in args) and args[0] == '-B'
            and args[1] in paths and isinstance(server.get('command'), str)
            and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', Path(server['command']).name, re.IGNORECASE) is not None)


def discover(home):
    codex_home = Path(os.environ.get('CODEX_HOME') or Path(home) / '.codex')
    root = codex_home / 'plugins/cache/openai-bundled/unified-computer-use'
    candidates = sorted(root.glob('*/.mcp.json'), key=lambda p: p.parent.stat().st_mtime, reverse=True)
    for source in candidates:
        try:
            server = json.loads(source.read_text(encoding='utf-8'))['mcpServers']['cua_repl']
            command, args = server['command'], server['args']
            if not isinstance(command, str) or not isinstance(args, list) or not args:
                continue
            if not Path(command).is_absolute() or not Path(command).is_file():
                continue
            if not all(isinstance(arg, str) and arg for arg in args):
                continue
            if any(Path(arg).is_absolute() and not Path(arg).exists() for arg in args):
                continue
            env = server.get('env', {})
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                continue
            required_paths = ['CODEX_CLI_PATH', 'CUA_REPL_NODE_REPL_PATH']
            if sys.platform != 'win32':
                required_paths.append('SKY_CUA_SERVICE_PATH')
            for key in required_paths:
                if key not in env or not Path(env[key]).is_absolute() or not Path(env[key]).exists():
                    raise ValueError('Missing runtime')
            return source, copy.deepcopy(server)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise RuntimeNotFoundError(tr('No usable Codex computer-use runtime found. Install/enable the Codex computer-use plugin first.', '未找到可用的 Codex Computer Use runtime。请先安装或启用 Codex Computer Use 插件。', '找不到可用的 Codex Computer Use runtime。請先安裝或啟用 Codex Computer Use 外掛。', '使用可能な Codex Computer Use runtime が見つかりません。先に Codex Computer Use プラグインをインストールまたは有効化してください。'))


def atomic_write(path, content, default_mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(tr('Refusing to replace a symbolic-link configuration or bridge file.', '拒绝覆盖符号链接形式的配置或桥接文件。', '拒絕覆寫符號連結形式的設定或橋接檔案。', 'シンボリックリンクの設定ファイルやブリッジファイルは上書きできません。'))
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else default_mode
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            if os.name != 'nt':
                os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_checked(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, path.parent)):
        raise ValueError('Refusing a symbolic-link configuration, state, or bridge path.')
    return path.read_bytes() if path.exists() else None


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def baseline_from_backups(home):
    root = Path(home) / '.claude/backups'
    candidates = [p for p in root.glob('claude.json.before-*')
                  if p.name.startswith(('claude.json.before-codex-cua-bridge-',
                                        'claude.json.before-cua-repl-',
                                        'claude.json.before-cua-bridge-'))]
    if not candidates:
        raise ActionRequiredError(tr('Cannot restore the original MCP configuration: installation state and legacy backup are missing. Restore a known backup before uninstalling.', '无法恢复原 MCP 配置：缺少安装状态和旧版备份。请先找回已知备份再卸载。', '無法還原原始 MCP 設定：缺少安裝狀態和舊版備份。請先找回已知備份再解除安裝。', '元の MCP 設定を復元できません。インストール状態と旧バックアップがありません。既知のバックアップを復元してから再試行してください。'))
    # The first backup predates the initial replacement; later snapshots can already contain our bridge.
    source = min(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    value = json.loads(read_checked(source))
    if not isinstance(value, dict) or not isinstance(value.get('mcpServers', {}), dict):
        raise ValueError('Invalid legacy baseline backup.')
    servers = value.get('mcpServers', {})
    if 'cua_repl' in servers and (not isinstance(servers['cua_repl'], dict) or owned(servers['cua_repl'], home)):
        raise ActionRequiredError(tr('The earliest backup already contains this bridge. Locate a pre-installation backup before uninstalling.', '最早的备份已包含本桥接程序，请先找回安装前的备份再卸载。', '最早的備份已包含本橋接程式，請先找回安裝前的備份再解除安裝。', '最古のバックアップにもブリッジが含まれます。インストール前のバックアップを確認してください。'))
    return {'present': 'cua_repl' in servers, 'value': servers.get('cua_repl')}


def desktop_config_path(home, explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    if os.environ.get('CLAUDE_USER_DATA_DIR'):
        return Path(os.environ['CLAUDE_USER_DATA_DIR']) / 'claude_desktop_config.json'
    if sys.platform != 'win32':
        return Path(home) / 'Library/Application Support/Claude/claude_desktop_config.json'
    roaming = Path(os.environ.get('APPDATA') or Path(home) / 'AppData/Roaming')
    local = Path(os.environ.get('LOCALAPPDATA') or Path(home) / 'AppData/Local')
    roots = [roaming / 'Claude', local / 'Claude', local / 'Claude-Data']
    roots.extend(local / 'Packages' / package / 'LocalCache/Roaming/Claude'
                 for package in ('Claude_pzs8sxrjxfjjc', 'AnthropicPBC.Claude_fnn82j28hfe8t'))
    paths = [root / 'claude_desktop_config.json' for root in roots]
    existing = list(dict.fromkeys(path for path in paths if path.exists() or path.is_symlink()))
    if len(existing) > 1:
        raise ActionRequiredError(tr('Multiple Claude Desktop configurations found; select one with --desktop-config PATH.', '找到多个 Claude Desktop 配置，请用 --desktop-config PATH 指定要恢复的文件。', '找到多個 Claude Desktop 設定，請用 --desktop-config PATH 指定要還原的檔案。', 'Claude Desktop 設定が複数あります。--desktop-config PATH で復元対象を指定してください。'))
    return existing[0] if existing else paths[0]


def require_desktop_closed():
    try:
        if sys.platform == 'win32':
            result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Claude.exe', '/FO', 'CSV', '/NH'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise OSError('Process inspection failed.')
            running = any(row and row[0].casefold() == 'claude.exe'
                          for row in csv.reader(io.StringIO(result.stdout)))
        else:
            result = subprocess.run(['/usr/bin/pgrep', '-x', 'Claude'], capture_output=True, timeout=10)
            if result.returncode not in (0, 1):
                raise OSError('Process inspection failed.')
            running = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        raise RuntimeNotFoundError(tr('Cannot verify that Claude Desktop is closed. Close it and retry.',
                                     '无法确认 Claude Desktop 已退出。请退出后重试。',
                                     '無法確認 Claude Desktop 已結束。請結束後重試。',
                                     'Claude Desktop が終了しているか確認できません。終了して再試行してください。')) from None
    if running:
        raise RuntimeNotFoundError(tr('Quit Claude Desktop before uninstalling so it cannot overwrite restored settings.',
                                     '请先退出 Claude Desktop 再卸载，避免它覆盖恢复后的设置。',
                                     '請先結束 Claude Desktop 再解除安裝，避免覆寫還原後的設定。',
                                     '復元した設定が上書きされないよう、アンインストール前に Claude Desktop を終了してください。'))


def apply_changes(home, changes, originals, plan):
    for path, original in originals.items():
        if read_checked(path) != original:
            raise ValueError('Configuration changed during installation; retry.')
    backups = []
    for path in changes:
        original = originals[path]
        if original is not None:
            backup = Path(home) / '.claude/backups' / (path.name.lstrip('.') + '.before-codex-cua-bridge-' + uuid.uuid4().hex)
            read_checked(backup)
            atomic_write(backup, original, stat.S_IMODE(path.stat().st_mode))
            backups.append(str(backup))
            if path.name == '.claude.json':
                plan['backup'] = str(backup)
    written = []
    try:
        for path, content in changes.items():
            if read_checked(path) != originals[path]:
                raise ValueError('Configuration changed during installation; retry.')
            atomic_write(path, content)
            written.append(path)
    except (OSError, ValueError):
        rollback_errors = []
        for path in reversed(written):
            try:
                if read_checked(path) != changes[path]:
                    raise ValueError('Configuration changed during rollback.')
                if originals[path] is None:
                    path.unlink()
                else:
                    atomic_write(path, originals[path])
            except (OSError, ValueError):
                rollback_errors.append(str(path))
        if rollback_errors:
            raise ActionRequiredError(tr('Rollback incomplete. Recover affected files from .claude/backups before retrying.', '回滚未完成，请先从 .claude/backups 恢复受影响的文件再重试。', '復原未完成，請先從 .claude/backups 還原受影響的檔案再重試。', 'ロールバックが完了しませんでした。.claude/backups から対象ファイルを復元してから再試行してください。')) from None
        raise
    plan['backups'] = backups


def configure(home, *, dry_run=False, uninstall=False, auto_approve_apps=False, project_dir=None, desktop_config=None):
    home = Path(home)
    config_path = home / '.claude.json'
    destination = managed_path(home)
    state_path = destination.parent / 'install-state.json'
    original = read_checked(config_path)
    state_original = read_checked(state_path)
    config = json.loads(original) if original is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get('mcpServers', {}), dict):
        raise ValueError('Claude MCP configuration must be an object.')
    state = json.loads(state_original) if state_original is not None else None
    if state is not None:
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('active'), bool)
                or not isinstance(state.get('baseline'), dict)
                or not isinstance(state['baseline'].get('present'), bool)
                or (state['baseline']['present'] and (not isinstance(state['baseline'].get('value'), dict)
                    or owned(state['baseline']['value'], home)))):
            raise ValueError('Invalid installation baseline state.')
    servers = config.get('mcpServers', {})
    current = servers.get('cua_repl')
    if uninstall and (state is not None and not state['active']):
        baseline = state['baseline']
        if (('cua_repl' in servers) == baseline['present']
                and (not baseline['present'] or current == baseline['value'])):
            return {'action': 'nothing to uninstall', 'dry_run': dry_run}
        if not owned(current, home):
            raise ActionRequiredError(tr('MCP configuration changed after uninstall; refusing to overwrite it.', '卸载后 MCP 配置已改变，拒绝覆盖。', '解除安裝後 MCP 設定已變更，拒絕覆寫。', 'アンインストール後に MCP 設定が変更されたため、上書きできません。'))
        state['active'] = True
    if 'cua_repl' in servers and not owned(current, home):
        raise ValueError('An unmanaged cua_repl server exists; refusing to overwrite or remove it.')
    if uninstall and current is None and state is None:
        return {'action': 'nothing to uninstall', 'dry_run': dry_run}
    if state is None or not state['active']:
        baseline = baseline_from_backups(home) if current is not None else {'present': False, 'value': None}
        state = {'version': 1, 'active': True, 'baseline': baseline}
    changes = {}
    originals = {config_path: original, state_path: state_original}
    if uninstall:
        baseline = state['baseline']
        if baseline['present']:
            config.setdefault('mcpServers', {})['cua_repl'] = baseline['value']
        else:
            servers.pop('cua_repl', None)
        state['active'] = False
        plan = {'action': 'uninstall', 'config': str(config_path), 'dry_run': dry_run,
                'mcp_restored': 'original entry' if baseline['present'] else 'original absence'}
        desktop_path = desktop_config_path(home, desktop_config)
        if desktop_path.resolve() in {path.resolve() for path in (config_path, state_path, destination)}:
            raise ActionRequiredError(tr('The Desktop configuration path conflicts with an MCP, state, or bridge file. Select the actual Desktop configuration.', 'Desktop 配置路径与 MCP、状态或桥接文件冲突，请指定真实的 Desktop 配置文件。', 'Desktop 設定路徑與 MCP、狀態或橋接檔案衝突，請指定真正的 Desktop 設定檔案。', 'Desktop 設定のパスが MCP、状態、ブリッジのファイルと競合します。実際の Desktop 設定を指定してください。'))
        desktop_original = read_checked(desktop_path)
        originals[desktop_path] = desktop_original
        plan['desktop_config'] = str(desktop_path)
        if desktop_original is None:
            plan['desktop_computer_use'] = 'configuration missing; unchanged'
        else:
            desktop = json.loads(desktop_original)
            if not isinstance(desktop, dict) or not isinstance(desktop.get('preferences', {}), dict):
                raise ValueError('Claude Desktop configuration must contain an object of preferences.')
            desktop.setdefault('preferences', {})['chicagoEnabled'] = True
            changes[desktop_path] = json_bytes(desktop)
            plan['desktop_computer_use'] = 'enabled'
    else:
        source, runtime = discover(home)
        content = (Path(project_dir or Path(__file__).parent) / 'bridge.py').read_bytes()
        originals[destination] = read_checked(destination)
        changes[destination] = content
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
    changes[config_path] = json_bytes(config)
    changes[state_path] = json_bytes(state)
    if not dry_run:
        apply_changes(home, changes, originals, plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=tr(__doc__, '使用本机已安装的 Codex Computer Use runtime 安装桥接程序。', '使用本機已安裝的 Codex Computer Use runtime 安裝橋接程式。', 'ローカルにインストール済みの Codex Computer Use runtime を使ってブリッジをインストールします。'))
    parser.add_argument('--dry-run', action='store_true', help=tr('Show the plan without changing files.', '显示执行计划，不修改文件。', '顯示執行計畫，不修改檔案。', 'ファイルを変更せずに実行計画を表示します。'))
    parser.add_argument('--desktop-config', help=tr('Claude Desktop configuration path for uninstall restoration.', '卸载恢复时使用的 Claude Desktop 配置路径。', '解除安裝還原時使用的 Claude Desktop 設定路徑。', 'アンインストール時に復元する Claude Desktop 設定のパス。'))
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--uninstall', action='store_true', help=tr('Restore the original MCP entry and enable Claude Desktop computer use; keep recovery files.', '恢复原 MCP 配置并启用 Claude Desktop Computer Use，保留恢复文件。', '還原原始 MCP 設定並啟用 Claude Desktop Computer Use，保留復原檔案。', '元の MCP 設定を復元し Claude Desktop Computer Use を有効にします。復旧用ファイルは保持します。'))
    choice.add_argument('--auto-approve-apps', action='store_true', help=tr('Explicitly authorize automatic native-app access approvals.', '明确授权自动批准原生应用访问请求。', '明確授權自動核准原生應用程式存取請求。', 'ネイティブアプリへのアクセス要求の自動承認を明示的に許可します。'))
    args = parser.parse_args()
    if sys.platform not in ('darwin', 'win32'):
        parser.error(tr('This integration requires macOS or Windows.', '此集成需要 macOS 或 Windows。', '此整合需要 macOS 或 Windows。', 'この連携には macOS または Windows が必要です。'))
    try:
        if args.uninstall and not args.dry_run:
            require_desktop_closed()
        plan = configure(Path.home(), **vars(args))
    except ActionRequiredError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        # Do not print configuration values or JSON parse excerpts.
        print(tr('Installation failed ({error}). Check runtime paths, config JSON, and conflicting cua_repl entries.',
                 '安装失败（{error}）。请检查 runtime 路径、配置 JSON 以及冲突的 cua_repl 配置项。',
                 '安裝失敗（{error}）。請檢查 runtime 路徑、設定 JSON 以及衝突的 cua_repl 設定項。',
                 'インストールに失敗しました（{error}）。runtime のパス、設定 JSON、競合する cua_repl 設定を確認してください。').format(error=type(error).__name__), file=sys.stderr)
        return 1
    print(tr('Plan prepared.' if args.dry_run else 'Completed.',
             '执行计划已生成。' if args.dry_run else '操作已完成。',
             '執行計畫已產生。' if args.dry_run else '操作已完成。',
             '実行計画を作成しました。' if args.dry_run else '完了しました。'), file=sys.stderr)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
