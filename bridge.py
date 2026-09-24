#!/usr/bin/env python3
"""Relay MCP, rendering narrowly identified native-app approvals for a human."""
import concurrent.futures
import json
from functools import lru_cache
import plistlib
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from xml.parsers.expat import ExpatError


def normalize_language(value):
    parts = value.split('.')[0].split('@')[0].replace('_', '-').lower().split('-')
    if parts[0] == 'zh':
        if 'hant' in parts or ('hans' not in parts and any(p in parts for p in ('tw', 'hk', 'mo'))):
            return 'zh-Hant'
        return 'zh-Hans'
    return 'ja' if parts[0] == 'ja' else 'en'


@lru_cache(maxsize=1)
def system_language():
    # GUI preferences take precedence: terminal LANG often differs from the desktop.
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/bin/defaults', 'export', '-g', '-'],
                                    capture_output=True, timeout=2, check=True)
            languages = plistlib.loads(result.stdout).get('AppleLanguages')
            if isinstance(languages, list) and languages and isinstance(languages[0], str) and languages[0].strip():
                return normalize_language(languages[0])
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, ExpatError):
            pass
    value = next((os.environ[key] for key in ('LC_ALL', 'LC_MESSAGES', 'LANG') if os.environ.get(key)), 'en')
    return normalize_language(value)


def tr(en, hans, hant, ja):
    return {'en': en, 'zh-Hans': hans, 'zh-Hant': hant, 'ja': ja}[system_language()]


DIALOG = '''on run argv
set denyLabel to item 2 of argv
set allowLabel to item 3 of argv
set resultValue to display dialog (item 1 of argv) with title "Claude · Codex Computer Use" buttons {denyLabel, allowLabel} default button denyLabel cancel button denyLabel giving up after 20
if gave up of resultValue then return "timeout"
if button returned of resultValue is allowLabel then return "allow"
return "deny"
end run'''


def approval_text(request):
    if not isinstance(request, dict) or request.get('method') != 'elicitation/create' or 'id' not in request:
        return None
    params = request.get('params')
    if not isinstance(params, dict) or params.get('mode', 'form') != 'form':
        return None
    schema = params.get('requestedSchema')
    if not isinstance(schema, dict) or schema.get('type') != 'object' or schema.get('properties') != {}:
        return None
    if set(schema) - {'type', 'properties', 'required', 'additionalProperties'} or schema.get('required', []) != []:
        return None
    meta = params.get('_meta')
    if not isinstance(meta, dict) or meta.get('connector_id') != 'computer-use' or meta.get('codex_approval_kind') != 'mcp_tool_call':
        return None
    tool_params = meta.get('tool_params')
    if not isinstance(tool_params, dict):
        return None
    app, action = tool_params.get('app'), meta.get('tool_name')
    def label(value):
        return isinstance(value, str) and 0 < len(value) <= 512 and value.strip() == value and not any(ord(c) < 32 for c in value)
    if not label(app) or not label(action) or app == 'computer-audio' or action == 'start_audio_recording':
        return None
    message = params.get('message')
    if not isinstance(message, str) or len(message) > 4096:
        return None
    subtitle = meta.get('subtitle', '')
    if not isinstance(subtitle, str) or len(subtitle) > 4096:
        return None
    return tr(
        'Claude requests access to an app through Codex Computer Use.\n\nApp: {app}\nAction: {action}\n\nOriginal request:\n"{message}"\n"{subtitle}"\n\nApprove only this request; no permanent allowlist is created.\nThe underlying service may retain app access for this session.\nChoose Deny if you did not initiate this action.',
        'Claude 请求通过 Codex Computer Use 访问应用。\n\n应用：{app}\n请求动作：{action}\n\n原始请求：\n“{message}”\n“{subtitle}”\n\n仅批准本次权限请求，不创建永久白名单。\n底层服务可能在当前会话内保留此应用授权。\n如果你没有发起这项操作，请选择拒绝。',
        'Claude 請求透過 Codex Computer Use 存取應用程式。\n\n應用程式：{app}\n請求動作：{action}\n\n原始請求：\n「{message}」\n「{subtitle}」\n\n僅核准本次權限請求，不建立永久允許清單。\n底層服務可能在目前工作階段內保留此應用程式授權。\n如果你沒有發起這項操作，請選擇拒絕。',
        'Claude が Codex Computer Use を通じてアプリへのアクセスを要求しています。\n\nアプリ：{app}\n操作：{action}\n\n元のリクエスト：\n「{message}」\n「{subtitle}」\n\n今回のリクエストのみを許可し、永続的な許可リストは作成しません。\n基盤サービスは、このセッション中アプリの許可を保持する場合があります。\nこの操作を開始していない場合は「拒否」を選んでください。'
    ).format(app=app, action=action, message=message, subtitle=subtitle)


def prompt_user(message, stopped, runner=subprocess.Popen):
    process = None
    started = time.monotonic()
    print("[cua-bridge] approval dialog opened", file=sys.stderr, flush=True)
    try:
        process = runner(['/usr/bin/osascript', '-e', DIALOG, '--', message,
                          tr('Deny', '拒绝', '拒絕', '拒否'),
                          tr('Allow this request', '允许本次请求', '允許本次請求', '今回のみ許可')],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for _ in range(230):
            if stopped.is_set():
                return False
            try:
                output, _ = process.communicate(timeout=0.1)
                accepted = process.returncode == 0 and output.strip() == 'allow'
                print(f'[cua-bridge] dialog result={output.strip()!r} exit={process.returncode} elapsed={time.monotonic()-started:.1f}s accepted={accepted}', file=sys.stderr, flush=True)
                return accepted
            except subprocess.TimeoutExpired:
                continue
        return False
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[cua-bridge] dialog error={type(error).__name__}", file=sys.stderr, flush=True)
        return False
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def route(line, send_client, send_server, approve):
    try:
        request = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        send_client(line)
        return
    message = approval_text(request)
    if message is None:
        send_client(line)
        return
    params = request['params']
    if (os.environ.get('CUA_BRIDGE_AUTO_APPROVE_APPS') == '1'
            and set(params['_meta']['tool_params']) == {'app'}
            and re.fullmatch(r'Allow Computer Use to use "[^"\x00-\x1f]+"\?', params['message'])):
        accepted = True
        print('[cua-bridge] app access approved by explicit configuration', file=sys.stderr, flush=True)
    else:
        try:
            accepted = approve(message) is True
        except Exception:
            accepted = False
    result = {'action': 'accept', 'content': {}} if accepted else {'action': 'decline'}
    send_server((json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}) + '\n').encode())


def main(argv):
    if not argv:
        print('Usage: bridge.py COMMAND [ARGS...]', file=sys.stderr)
        return 2
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=sys.stderr, start_new_session=True)
    stopped = threading.Event()
    input_lock, output_lock = threading.Lock(), threading.Lock()
    def force_stop():
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
    def stop(*_):
        if stopped.is_set():
            return
        stopped.set()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
            timer = threading.Timer(3, force_stop)
            timer.daemon = True
            timer.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    def write(stream, lock, data):
        with lock:
            try:
                stream.write(data)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                stop()
    def send_client(data):
        write(sys.stdout.buffer, output_lock, data)
    def send_server(data):
        write(child.stdin, input_lock, data)
    def read_client():
        try:
            descriptor = sys.stdin.fileno()
            while not stopped.is_set():
                readable, _, _ = select.select([descriptor], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                send_server(chunk)
        except OSError:
            pass
        finally:
            stop()
    client_reader = threading.Thread(target=read_client)
    client_reader.start()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        for line in child.stdout:
            try:
                recognized = approval_text(json.loads(line)) is not None
            except (ValueError, UnicodeDecodeError):
                recognized = False
            if recognized:
                pool.submit(route, line, send_client, send_server,
                            lambda message: prompt_user(message, stopped))
            else:
                send_client(line)
    finally:
        stop()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            force_stop()
            if child.poll() is None:
                child.kill()
            child.wait()
        pool.shutdown(wait=True, cancel_futures=True)
        client_reader.join()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
