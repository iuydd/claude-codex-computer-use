#!/usr/bin/env python3
"""Relay MCP, rendering narrowly identified native-app approvals for a human."""
import base64
import concurrent.futures
import ctypes
from ctypes import wintypes
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
    if sys.platform == 'win32':
        try:
            return normalize_language(windows_language())
        except (OSError, AttributeError, ValueError):
            pass
    value = next((os.environ[key] for key in ('LC_ALL', 'LC_MESSAGES', 'LANG') if os.environ.get(key)), 'en')
    return normalize_language(value)


def windows_language():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    preferred = kernel.GetUserPreferredUILanguages
    preferred.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.ULONG),
                          wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    preferred.restype = wintypes.BOOL
    count, size = wintypes.ULONG(), wintypes.ULONG()
    if preferred(8, ctypes.byref(count), None, ctypes.byref(size)) and size.value:
        buffer = ctypes.create_unicode_buffer(size.value)
        if preferred(8, ctypes.byref(count), buffer, ctypes.byref(size)) and buffer.value:
            return buffer.value
    # Fall back to the user's UI language, never the machine installation language.
    kernel.GetUserDefaultUILanguage.restype = wintypes.WORD
    convert = kernel.LCIDToLocaleName
    convert.argtypes = [wintypes.DWORD, wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD]
    convert.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(85)
    if convert(kernel.GetUserDefaultUILanguage(), buffer, len(buffer), 0):
        return buffer.value
    raise OSError('Cannot determine Windows UI language')


def wait_readable(descriptor, timeout=0.1):
    if sys.platform != 'win32':
        return bool(select.select([descriptor], [], [], timeout)[0])
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    peek.restype = wintypes.BOOL
    available = wintypes.DWORD()
    if not peek(handle, None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error in (109, 232):  # Closed pipe: allow os.read to observe EOF.
            return True
        raise OSError(error, 'Cannot inspect MCP input pipe')
    if available.value:
        return True
    time.sleep(timeout)
    return False


def dialog_command(message, deny, allow):
    if sys.platform != 'win32':
        return ['/usr/bin/osascript', '-e', DIALOG, '--', message, deny, allow]
    # Encode data independently; app names and messages can never become PS code.
    payload = base64.b64encode(json.dumps([message, deny, allow], ensure_ascii=False).encode()).decode()
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$data = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$form = New-Object Windows.Forms.Form
$form.Text = 'Claude · Codex Computer Use'
$form.Size = New-Object Drawing.Size(620, 540)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.Tag = 'deny'
$text = New-Object Windows.Forms.TextBox
$text.Multiline = $true
$text.ReadOnly = $true
$text.ScrollBars = 'Vertical'
$text.Location = New-Object Drawing.Point(15, 15)
$text.Size = New-Object Drawing.Size(570, 410)
$text.Text = $data[0].Replace("`n", "`r`n")
$form.Controls.Add($text)
$deny = New-Object Windows.Forms.Button
$deny.Text = $data[1]
$deny.Location = New-Object Drawing.Point(265, 445)
$deny.Size = New-Object Drawing.Size(150, 35)
$deny.DialogResult = 'Cancel'
$form.Controls.Add($deny)
$allow = New-Object Windows.Forms.Button
$allow.Text = $data[2]
$allow.Location = New-Object Drawing.Point(425, 445)
$allow.Size = New-Object Drawing.Size(160, 35)
$allow.Add_Click({ $form.Tag = 'allow'; $form.Close() })
$form.Controls.Add($allow)
$form.AcceptButton = $deny
$form.CancelButton = $deny
$form.Add_Shown({ $deny.Select() })
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 20000
$timer.Add_Tick({ $timer.Stop(); $form.Close() })
$timer.Start()
try { [void]$form.ShowDialog(); [Console]::WriteLine($form.Tag) }
finally { $timer.Dispose(); $form.Dispose() }
""".replace('PAYLOAD', payload)
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    executable = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                              'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    return [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand', encoded]


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


def windows_app_request(params):
    meta = params.get('_meta', {})
    display = meta.get('tool_params_display')
    if (meta.get('connector_name') != 'Computer Use' or 'tool_name' in meta
            or meta.get('persist') not in (['session'], ['session', 'always'])
            or set(meta.get('tool_params', {})) != {'app'}
            or not isinstance(display, list) or len(display) != 1
            or not isinstance(display[0], dict)
            or set(display[0]) != {'name', 'display_name', 'value'}
            or display[0].get('name') != 'app' or display[0].get('display_name') != 'App'):
        return False
    name = display[0].get('value')
    return (isinstance(name, str) and 0 < len(name) <= 512 and name.strip() == name
            and not any(ord(c) < 32 for c in name)
            and params.get('message') == f'Allow Codex to use {name}?')


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
    if action is None and windows_app_request(params):
        action = 'app_access'
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
        process = runner(dialog_command(message, tr('Deny', '拒绝', '拒絕', '拒否'),
                                        tr('Allow this request', '允许本次请求', '允許本次請求', '今回のみ許可')),
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                         encoding='utf-8', creationflags=0x08000000 if sys.platform == 'win32' else 0)
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
            and (re.fullmatch(r'Allow Computer Use to use "[^"\x00-\x1f]+"\?', params['message'])
                 or windows_app_request(params))):
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
    if sys.platform == 'win32':
        import msvcrt
        for stream in (sys.stdin, sys.stdout):
            msvcrt.setmode(stream.fileno(), os.O_BINARY)
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=sys.stderr, start_new_session=sys.platform != 'win32')
    stopped = threading.Event()
    input_lock, output_lock = threading.Lock(), threading.Lock()
    def force_stop():
        if child.poll() is None:
            try:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                                   creationflags=0x08000000)
                else:
                    os.killpg(child.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                child.kill()
    def stop(*_):
        if stopped.is_set():
            return
        stopped.set()
        if child.poll() is None:
            try:
                if sys.platform == 'win32':
                    force_stop()
                else:
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
                if not wait_readable(descriptor):
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
