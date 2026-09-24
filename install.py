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

from bridge import tr


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
    raise ValueError(tr('No usable Codex computer-use runtime found. Install/enable the Codex computer-use plugin first.', '未找到可用的 Codex Computer Use runtime。请先安装或启用 Codex Computer Use 插件。', '找不到可用的 Codex Computer Use runtime。請先安裝或啟用 Codex Computer Use 外掛。', '使用可能な Codex Computer Use runtime が見つかりません。先に Codex Computer Use プラグインをインストールまたは有効化してください。'))


def atomic_write(path, content, default_mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(tr('Refusing to replace a symbolic-link configuration or bridge file.', '拒绝覆盖符号链接形式的配置或桥接文件。', '拒絕覆寫符號連結形式的設定或橋接檔案。', 'シンボリックリンクの設定ファイルやブリッジファイルは上書きできません。'))
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
        raise ValueError(tr('Refusing to replace a symbolic-link configuration.', '拒绝覆盖符号链接形式的配置文件。', '拒絕覆寫符號連結形式的設定檔案。', 'シンボリックリンクの設定ファイルは上書きできません。'))
    original = config_path.read_bytes() if config_path.exists() else None
    config = json.loads(original) if original is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get('mcpServers', {}), dict):
        raise ValueError(tr('Claude MCP configuration must be an object.', 'Claude MCP 配置必须是 JSON 对象。', 'Claude MCP 設定必須是 JSON 物件。', 'Claude MCP 設定は JSON オブジェクトである必要があります。'))
    servers = config.get('mcpServers', {})
    current = servers.get('cua_repl')
    if 'cua_repl' in servers and not owned(current, home):
        raise ValueError(tr('An unmanaged cua_repl server exists; refusing to overwrite or remove it.', '已存在非本项目管理的 cua_repl 服务，拒绝覆盖或删除。', '已存在非本專案管理的 cua_repl 服務，拒絕覆寫或刪除。', 'このプロジェクトで管理していない cua_repl サーバーが存在するため、上書きや削除はできません。'))
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
            raise ValueError(tr('Refusing to replace a symbolic-link bridge file.', '拒绝覆盖符号链接形式的桥接文件。', '拒絕覆寫符號連結形式的橋接檔案。', 'シンボリックリンクのブリッジファイルは上書きできません。'))
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
        raise ValueError(tr('Claude configuration changed during installation; retry.', 'Claude 配置在安装期间发生变化，请重试。', 'Claude 設定在安裝期間發生變更，請重試。', 'インストール中に Claude 設定が変更されました。再試行してください。'))
    if original is not None:
        backup = home / '.claude/backups' / ('claude.json.before-codex-cua-bridge-' + uuid.uuid4().hex)
        atomic_write(backup, original, stat.S_IMODE(config_path.stat().st_mode))
        plan['backup'] = str(backup)
    if not uninstall:
        atomic_write(destination, content)
    atomic_write(config_path, (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode())
    return plan


def main():
    parser = argparse.ArgumentParser(description=tr(__doc__, '使用本机已安装的 Codex Computer Use runtime 安装桥接程序。', '使用本機已安裝的 Codex Computer Use runtime 安裝橋接程式。', 'ローカルにインストール済みの Codex Computer Use runtime を使ってブリッジをインストールします。'))
    parser.add_argument('--dry-run', action='store_true', help=tr('Show the plan without changing files.', '显示执行计划，不修改文件。', '顯示執行計畫，不修改檔案。', 'ファイルを変更せずに実行計画を表示します。'))
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--uninstall', action='store_true', help=tr('Remove only this managed MCP entry; keep files.', '仅移除本项目管理的 MCP 配置项，保留文件。', '僅移除本專案管理的 MCP 設定項，保留檔案。', '管理対象の MCP 設定のみを削除し、ファイルは保持します。'))
    choice.add_argument('--auto-approve-apps', action='store_true', help=tr('Explicitly authorize automatic native-app access approvals.', '明确授权自动批准原生应用访问请求。', '明確授權自動核准原生應用程式存取請求。', 'ネイティブアプリへのアクセス要求の自動承認を明示的に許可します。'))
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error(tr('This integration requires macOS.', '此集成需要 macOS。', '此整合需要 macOS。', 'この連携には macOS が必要です。'))
    try:
        plan = configure(Path.home(), **vars(args))
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
