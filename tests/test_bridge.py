import copy
import json
import os
import pathlib
import select
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
        process = subprocess.Popen([sys.executable, '-B', bridge.__file__,
                                    sys.executable, '-u', '-c', script],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'server did not start')
            self.assertEqual(process.stdout.readline(), b'ready\n')
            if terminate:
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

    def test_dialog_result_and_errors(self):
        for output, code, expected in [('允许本次请求\n', 0, True), ('拒绝\n', 0, False),
                                       ('timeout\n', 0, False), ('允许本次请求\n', 1, False)]:
            process = Mock(returncode=code)
            process.communicate.return_value = (output, None)
            process.poll.return_value = code
            runner = Mock(return_value=process)
            self.assertEqual(bridge.prompt_user('app and action', threading.Event(), runner), expected)
            self.assertEqual(runner.call_args.args[0][-2:], ['--', 'app and action'])
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


if __name__ == '__main__':
    unittest.main()
