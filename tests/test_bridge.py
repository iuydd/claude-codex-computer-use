import base64
import copy
import json
import os
import pathlib
import plistlib
import time
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import bridge


REQUEST = {'jsonrpc': '2.0', 'id': 7, 'method': 'elicitation/create', 'params': {
    'message': 'Allow Computer Use to use "Finder"?',
    'requestedSchema': {'type': 'object', 'properties': {}},
    '_meta': {'connector_id': 'computer-use', 'codex_approval_kind': 'mcp_tool_call',
              'tool_name': 'get_app_state', 'tool_params': {'app': 'com.apple.finder'}}}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        language = patch('bridge.system_language', return_value='zh-Hans')
        language.start()
        self.addCleanup(language.stop)
        environment = patch.dict(os.environ, {'CUA_BRIDGE_AUTO_APPROVE_APPS': '0'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_config_approves_any_valid_app_without_dialog(self):
        with patch.dict(os.environ, {'CUA_BRIDGE_AUTO_APPROVE_APPS': '1'}):
            for app in ['com.apple.finder', 'com.tencent.xinWeChat', 'org.example.OtherApp']:
                request = copy.deepcopy(REQUEST)
                request['params']['_meta']['tool_params']['app'] = app
                approve = Mock(side_effect=AssertionError('no dialog expected'))
                client, server = self.routed(request, approve)
                approve.assert_not_called()
                self.assertEqual(client, [])
                self.assertEqual(json.loads(server[0])['result'], {'action': 'accept', 'content': {}})

    def test_without_exact_config_value_dialog_remains_required(self):
        for value in [None, '0', 'true', 'yes']:
            with patch.dict(os.environ):
                if value is None:
                    os.environ.pop('CUA_BRIDGE_AUTO_APPROVE_APPS', None)
                else:
                    os.environ['CUA_BRIDGE_AUTO_APPROVE_APPS'] = value
                approve = Mock(return_value=False)
                _, server = self.routed(REQUEST, approve)
                approve.assert_called_once()
                self.assertEqual(json.loads(server[0])['result']['action'], 'decline')

    def test_config_approves_app_access_for_click_action(self):
        with patch.dict(os.environ, {'CUA_BRIDGE_AUTO_APPROVE_APPS': '1'}):
            request = copy.deepcopy(REQUEST)
            request['params']['_meta']['tool_name'] = 'click'
            approve = Mock(side_effect=AssertionError('no dialog expected'))
            _, server = self.routed(request, approve)
            approve.assert_not_called()
            self.assertEqual(json.loads(server[0])['result']['action'], 'accept')

    def test_config_does_not_approve_other_confirmations_or_unknown_requests(self):
        with patch.dict(os.environ, {'CUA_BRIDGE_AUTO_APPROVE_APPS': '1'}):
            request = copy.deepcopy(REQUEST)
            request['params']['_meta']['tool_name'] = 'click'
            for message in ['Delete this file?', 'Allow Computer Use to use ""?',
                            'Allow Computer Use to use "Finder"? Then delete files.']:
                request['params']['message'] = message
                approve = Mock(return_value=False)
                _, server = self.routed(request, approve)
                approve.assert_called_once()
                self.assertEqual(json.loads(server[0])['result']['action'], 'decline')
            request = copy.deepcopy(REQUEST)
            request['params']['_meta']['tool_params']['delete'] = True
            approve = Mock(return_value=False)
            _, server = self.routed(request, approve)
            approve.assert_called_once()
            self.assertEqual(json.loads(server[0])['result']['action'], 'decline')
            self.test_unknown_requests_forward_unchanged()

    def check_open_stdin_shutdown(self, terminate):
        script = 'import sys\nprint("ready", flush=True)\nsys.stdin.readline()\n'
        command = [sys.executable, '-B', bridge.__file__, sys.executable, '-u', '-c', script]
        if terminate and sys.platform == 'win32':
            script = 'import time\nprint("ready", flush=True)\nwhile True:\n time.sleep(.1); print("heartbeat", flush=True)'
            # Windows TerminateProcess cannot invoke a SIGTERM handler. Exercise
            # the same handler through a locally raised signal instead.
            wrapper = 'import bridge,threading,signal,sys; threading.Timer(1, lambda: signal.raise_signal(signal.SIGTERM)).start(); sys.exit(bridge.main(sys.argv[1:]))'
            command = [sys.executable, '-B', '-c', wrapper, sys.executable, '-u', '-c', script]
        process = subprocess.Popen(command,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not bridge.wait_readable(process.stdout.fileno()):
                self.assertLess(time.monotonic(), deadline, 'server did not start')
            self.assertEqual(process.stdout.readline().rstrip(b'\r\n'), b'ready')
            if terminate:
                if sys.platform != 'win32':
                    process.terminate()
            else:
                process.stdin.write(b'exit\n')
                process.stdin.flush()
            self.assertFalse(process.stdin.closed)
            returncode = process.wait(timeout=5)
            errors = process.stderr.read()
            self.assertEqual(returncode, 0, errors.decode(errors='replace'))
            self.assertEqual(errors, b'')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_sigterm_with_client_stdin_still_open(self):
        self.check_open_stdin_shutdown(terminate=True)

    def test_child_exit_with_client_stdin_still_open(self):
        self.check_open_stdin_shutdown(terminate=False)

    def routed(self, request, answer):
        client, server = [], []
        bridge.route(json.dumps(request).encode() + b'\n', client.append, server.append, answer)
        return client, server

    def test_only_explicit_true_accepts(self):
        for answer, action in [(True, 'accept'), (False, 'decline'), (None, 'decline'), ('yes', 'decline')]:
            client, server = self.routed(REQUEST, lambda _: answer)
            self.assertEqual(client, [])
            result = json.loads(server[0])
            self.assertEqual(result['id'], 7)
            self.assertEqual(result['result']['action'], action)
            self.assertEqual(result['result'].get('content'), {} if answer is True else None)

    def test_unknown_requests_forward_unchanged(self):
        variants = []
        for field, value in [('mode', 'url'), ('requestedSchema', {}), ('_meta', {}),
                             ('requestedSchema', {'type': 'object', 'properties': {}, 'required': ['yes']})]:
            request = copy.deepcopy(REQUEST)
            request['params'][field] = value
            variants.append(request)
        audio = copy.deepcopy(REQUEST)
        audio['params']['_meta']['tool_name'] = 'start_audio_recording'
        variants.append(audio)
        for request in variants:
            raw = json.dumps(request).encode() + b'\n'
            client, server = [], []
            bridge.route(raw, client.append, server.append, lambda _: self.fail('unexpected approval'))
            self.assertEqual(client, [raw])
            self.assertEqual(server, [])

    @patch('bridge.sys.platform', 'darwin')
    def test_dialog_result_and_errors(self):
        for output, code, expected in [('allow\n', 0, True), ('deny\n', 0, False),
                                       ('timeout\n', 0, False), ('allow\n', 1, False), ('允许本次请求\n', 0, False)]:
            process = Mock(returncode=code)
            process.communicate.return_value = (output, None)
            process.poll.return_value = code
            runner = Mock(return_value=process)
            self.assertEqual(bridge.prompt_user('app and action', threading.Event(), runner), expected)
            self.assertEqual(runner.call_args.args[0][-4:], ['--', 'app and action', '拒绝', '允许本次请求'])
        self.assertFalse(bridge.prompt_user('x', threading.Event(), Mock(side_effect=OSError())))

    def test_timeout_and_shutdown_kill_dialog(self):
        process = Mock(returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired('osascript', .1)
        process.poll.return_value = None
        self.assertFalse(bridge.prompt_user('x', threading.Event(), Mock(return_value=process)))
        process.kill.assert_called_once()
        stopped = threading.Event()
        stopped.set()
        self.assertFalse(bridge.prompt_user('x', stopped, Mock(return_value=process)))

    def test_approval_exception_denies(self):
        _, server = self.routed(REQUEST, Mock(side_effect=RuntimeError()))
        self.assertEqual(json.loads(server[0])['result']['action'], 'decline')

    def test_message_contains_app_action_and_no_persistent_grant(self):
        text = bridge.approval_text(REQUEST)
        self.assertIn('com.apple.finder', text)
        self.assertIn('get_app_state', text)
        self.assertIn('不创建永久白名单', text)

    def test_stdio_roundtrip_and_eof(self):
        script = 'import sys,json\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"method":r["method"]}}),flush=True)'
        path = pathlib.Path(bridge.__file__)
        process = subprocess.Popen([sys.executable, str(path), sys.executable, '-u', '-c', script],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for i, method in enumerate(['initialize', 'tools/list', 'tools/call']):
                process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method})+'\n').encode())
                process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())['result']['method'], method)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(process.stderr.read(), b'')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_windows_runtime_approval_shape_and_scope(self):
        request = copy.deepcopy(REQUEST)
        params = request['params']
        params['message'] = 'Allow Codex to use 微信?'
        params['_meta'] = {
            'codex_approval_kind': 'mcp_tool_call', 'connector_id': 'computer-use',
            'connector_name': 'Computer Use', 'persist': ['session', 'always'],
            'riskLevel': 'low', 'tool_params': {'app': r'C:\Apps\WeChat.exe'},
            'tool_params_display': [{'name': 'app', 'display_name': 'App', 'value': '微信'}]}
        for auto in ['0', '1']:
            with patch.dict(os.environ, {'CUA_BRIDGE_AUTO_APPROVE_APPS': auto}):
                approve = Mock(return_value=True)
                client, server = self.routed(request, approve)
                self.assertEqual(client, [])
                self.assertEqual(json.loads(server[0])['result'], {'action': 'accept', 'content': {}})
                self.assertEqual(approve.call_count, 0 if auto == '1' else 1)
        for change in ['message', 'audio', 'display', 'params', 'connector']:
            invalid = copy.deepcopy(request)
            if change == 'message':
                invalid['params']['message'] += ' And delete files.'
            elif change == 'audio':
                invalid['params']['_meta']['tool_params']['app'] = 'computer-audio'
            elif change == 'display':
                invalid['params']['_meta']['tool_params_display'][0]['value'] = 'Other'
            elif change == 'params':
                invalid['params']['_meta']['tool_params']['delete'] = True
            else:
                invalid['params']['_meta']['connector_id'] = 'other'
            client, server = self.routed(invalid, Mock(side_effect=AssertionError('must forward')))
            self.assertEqual(len(client), 1)
            self.assertEqual(server, [])

    def test_windows_dialog_uses_encoded_data_and_explicit_allow(self):
        message = '微信 "quoted"; $(Start-Process bad)\n中文'
        with patch('bridge.sys.platform', 'win32'):
            command = bridge.dialog_command(message, '拒绝', '允许')
            script = base64.b64decode(command[-1]).decode('utf-16le')
            self.assertIn("$form.AcceptButton = $deny", script)
            self.assertIn("$form.CancelButton = $deny", script)
            self.assertIn('$timer.Interval = 20000', script)
            self.assertNotIn(message, script)
            payload = script.split("FromBase64String('")[1].split("')")[0]
            self.assertEqual(json.loads(base64.b64decode(payload)), [message, '拒绝', '允许'])
            for output, expected in [('allow', True), ('deny', False), ('', False)]:
                process = Mock(returncode=0)
                process.communicate.return_value = (output, None)
                process.poll.return_value = 0
                runner = Mock(return_value=process)
                self.assertEqual(bridge.prompt_user(message, threading.Event(), runner), expected)
                self.assertEqual(runner.call_args.kwargs['encoding'], 'utf-8')

    def test_binary_unicode_roundtrip(self):
        script = 'import sys; data=sys.stdin.buffer.readline(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush()'
        request = '{"message":"微信 日本語"}\n'.encode()
        process = subprocess.Popen([sys.executable, '-B', bridge.__file__, sys.executable, '-u', '-c', script],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(request)
            process.stdin.flush()
            self.assertEqual(process.stdout.readline(), request)
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(process.stderr.read(), b'')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


class LanguageTests(unittest.TestCase):
    def setUp(self):
        bridge.system_language.cache_clear()
        self.addCleanup(bridge.system_language.cache_clear)

    def test_windows_ui_language_overrides_terminal(self):
        with patch('bridge.sys.platform', 'win32'), patch('bridge.windows_language', return_value='zh-Hant-TW'), patch.dict(os.environ, {'LANG': 'ja_JP'}):
            self.assertEqual(bridge.system_language(), 'zh-Hant')

    def test_language_variants(self):
        for value, expected in [('zh-Hans-JP', 'zh-Hans'), ('zh-Hant-CN', 'zh-Hant'),
                                ('zh_CN.UTF-8', 'zh-Hans'), ('zh_TW.UTF-8', 'zh-Hant'),
                                ('zh-HK', 'zh-Hant'), ('zh-MO', 'zh-Hant'),
                                ('ja_JP.UTF-8', 'ja'), ('en-GB', 'en'), ('fr-FR', 'en'), ('C', 'en')]:
            with self.subTest(value=value):
                self.assertEqual(bridge.normalize_language(value), expected)

    def test_macos_primary_language_overrides_terminal_and_is_cached(self):
        result = Mock(stdout=plistlib.dumps({'AppleLanguages': ['zh-Hans-JP', 'ja-JP']}))
        with patch('bridge.sys.platform', 'darwin'), patch.dict(os.environ, {'LANG': 'ja_JP.UTF-8'}, clear=True), patch('bridge.subprocess.run', return_value=result) as run:
            self.assertEqual(bridge.system_language(), 'zh-Hans')
            self.assertEqual(bridge.system_language(), 'zh-Hans')
            run.assert_called_once_with(['/usr/bin/defaults', 'export', '-g', '-'], capture_output=True, timeout=2, check=True)

    def test_unsupported_primary_language_falls_back_to_english(self):
        result = Mock(stdout=plistlib.dumps({'AppleLanguages': ['fr-FR', 'ja-JP']}))
        with patch('bridge.sys.platform', 'darwin'), patch('bridge.subprocess.run', return_value=result):
            self.assertEqual(bridge.system_language(), 'en')

    def test_system_preference_failures_use_environment(self):
        failures = [OSError(), subprocess.TimeoutExpired('defaults', 2),
                    subprocess.CalledProcessError(1, 'defaults')]
        outputs = [b'not a plist', b'<?xml version="1.0"?><plist><broken>',
                   plistlib.dumps({}), plistlib.dumps({'AppleLanguages': []}),
                   plistlib.dumps({'AppleLanguages': [42]}), plistlib.dumps([])]
        for failure, output in [(e, None) for e in failures] + [(None, o) for o in outputs]:
            bridge.system_language.cache_clear()
            with self.subTest(failure=failure, output=output), patch('bridge.sys.platform', 'darwin'), patch.dict(os.environ, {'LC_MESSAGES': 'zh_TW.UTF-8', 'LANG': 'ja_JP.UTF-8'}, clear=True), patch('bridge.subprocess.run', side_effect=failure, return_value=Mock(stdout=output)):
                self.assertEqual(bridge.system_language(), 'zh-Hant')

    def test_environment_priority_and_empty_environment(self):
        for env, expected in [({'LC_ALL': 'ja_JP', 'LC_MESSAGES': 'zh_TW', 'LANG': 'en_US'}, 'ja'),
                              ({'LC_ALL': '', 'LANG': 'zh_CN'}, 'zh-Hans'), ({}, 'en')]:
            bridge.system_language.cache_clear()
            with patch('bridge.sys.platform', 'linux'), patch.dict(os.environ, env, clear=True), patch('bridge.subprocess.run') as run:
                self.assertEqual(bridge.system_language(), expected)
                run.assert_not_called()

    @patch('bridge.sys.platform', 'darwin')
    def test_dialog_language_and_machine_decisions(self):
        for language, labels, phrase in [('en', ['Deny', 'Allow this request'], 'no permanent allowlist'),
                                         ('zh-Hans', ['拒绝', '允许本次请求'], '不创建永久白名单'),
                                         ('zh-Hant', ['拒絕', '允許本次請求'], '不建立永久允許清單'),
                                         ('ja', ['拒否', '今回のみ許可'], '永続的な許可リスト')]:
            with patch('bridge.system_language', return_value=language):
                request = copy.deepcopy(REQUEST)
                request['params']['_meta']['subtitle'] = 'Raw subtitle'
                text = bridge.approval_text(request)
                self.assertIn(phrase, text)
                self.assertIn(request['params']['message'], text)
                self.assertIn('Raw subtitle', text)
                for output, code, expected in [('allow', 0, True), ('deny', 0, False), ('timeout', 0, False), ('allow', 1, False), (labels[1], 0, False)]:
                    with self.subTest(language=language, output=output):
                        process = Mock(returncode=code)
                        process.communicate.return_value = (output, '')
                        process.poll.return_value = code
                        runner = Mock(return_value=process)
                        self.assertEqual(bridge.prompt_user(text, threading.Event(), runner), expected)
                        self.assertEqual(runner.call_args.args[0][-2:], labels)


if __name__ == '__main__':
    unittest.main()
