"""Owned connector lifecycle tests; no public tunnel or real credential is used."""
import base64
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import invitation_tunnel as tunnel


class OwnedProcess:
    def __init__(self, *, refuse_stop=False, returncode=None):
        self.pid = 12345
        self.returncode = returncode
        self.signals = []
        self.refuse_stop = refuse_stop

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        if not self.refuse_stop:
            self.returncode = 0

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('owned fixture', timeout)
        return self.returncode


class InvitationTunnelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='invitation-tunnel-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / 'tools' / 'cloudflared.exe'
        self.binary.parent.mkdir()
        self.binary.write_bytes(b'non-executable fixture; never run')
        self.token = self.root / 'tunnel-token.txt'
        self.token_payload = {
            'a': '00000000000000000000000000000000', 't': tunnel.PINNED_TUNNEL_ID,
            's': base64.b64encode(b'isolated-fixture-secret-not-a-real-credential').decode('ascii'),
        }
        self.token_value = self.encode_token(self.token_payload)
        self.token.write_text(self.token_value + '\n', encoding='utf-8')
        self.settings = dict(
            tunnel_id=tunnel.PINNED_TUNNEL_ID, name='recruitment-insights',
            hostname=tunnel.HOSTNAME, authentication='application',
            cloudflared_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.runtime = self.root / 'tunnel-runtime.json'
        self.save_settings()
        self.controller = tunnel.InvitationTunnel(self.root)

    def save_settings(self):
        self.runtime.write_text(json.dumps(self.settings), encoding='utf-8')

    @staticmethod
    def encode_token(payload):
        return base64.b64encode(json.dumps(payload).encode('utf-8')).decode('ascii')

    def test_shared_token_validator_accepts_only_the_pinned_token_structure(self):
        self.assertIsNone(tunnel.validate_tunnel_token(self.token_value))
        with_endpoint = self.encode_token(dict(self.token_payload, e='isolated-fixture-endpoint'))
        self.assertIsNone(tunnel.validate_tunnel_token(with_endpoint))

    def test_shared_token_validator_rejects_invalid_type_encoding_structure_and_identity_safely(self):
        invalid = [None, True, 5, {}, self.token_value.encode('ascii'), '', 'invalid-token-secret',
                   'A' * 16385, self.token_value + '\n', self.encode_token([]),
                   self.encode_token('private-secret-string'), self.encode_token({})]
        invalid.extend(self.encode_token(dict(self.token_payload, **fields)) for fields in (
            {'a': 'another-cloudflare-account'}, {'a': None}, {'a': 7},
            {'t': 'e2d1b38a-7e0c-4cbb-b6e7-f9c8b38f12a8'}, {'t': None}, {'t': 7},
            {'s': ''}, {'s': None}, {'s': 7}, {'s': 'private-secret-not-base64'},
            {'s': 'YQ='}, {'s': 'YQ==\n'},
            {'A': 'another-cloudflare-account'},
            {'T': 'e2d1b38a-7e0c-4cbb-b6e7-f9c8b38f12a8'}, {'S': 'YQ=='},
        ))
        for token_value in invalid:
            with self.subTest(token_type=type(token_value).__name__):
                with self.assertRaises(ValueError) as rejected:
                    tunnel.validate_tunnel_token(token_value)
                self.assertEqual(str(rejected.exception),
                                 'Tunnel 运行 Token 格式或固定账号/Tunnel 身份不符合要求')
                self.assertTrue(rejected.exception.__suppress_context__)

    def test_foreign_or_malformed_token_file_is_rejected_before_launch_or_config_changes(self):
        config = self.root / 'cloudflared-config.json'
        before = b'{"preserve-existing-config": true}\n'
        invalid = [b'\xffinvalid-secret', b'invalid-token-secret\n', b'A' * 16385]
        invalid.extend(self.encode_token(dict(self.token_payload, **fields)).encode('ascii')
                       for fields in ({'a': 'another-cloudflare-account'},
                                      {'t': 'e2d1b38a-7e0c-4cbb-b6e7-f9c8b38f12a8'},
                                      {'s': 'private-secret-not-base64'}))
        for contents in invalid:
            self.token.write_bytes(contents)
            config.write_bytes(before)
            with self.subTest(length=len(contents)), \
                    patch.object(self.controller, 'check_metrics_port') as metrics, \
                    patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaises(ValueError):
                    self.controller.start()
                metrics.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(config.read_bytes(), before)

    def test_launch_uses_only_token_file_sanitizes_environment_and_hides_output(self):
        process = OwnedProcess()
        environment = {'TUNNEL_TOKEN': 'secret-from-another-tunnel',
                       'TUNNEL_CONFIG': 'foreign-config', 'CLOUDFLARE_API_TOKEN': 'foreign-api-secret',
                       'CLOUDFLARED_CONFIG': 'foreign-config', 'KEEP_THIS_VALUE': 'safe'}
        with patch.dict(os.environ, environment), \
                patch.object(self.controller, 'check_metrics_port') as metrics, \
                patch.object(tunnel.subprocess, 'Popen', return_value=process) as launch, \
                patch.object(self.controller, 'check_ready', return_value=True):
            self.assertTrue(self.controller.start())
        metrics.assert_called_once_with()
        arguments = launch.call_args.args[0]
        options = launch.call_args.kwargs
        self.assertEqual(arguments[0], str(self.binary))
        self.assertEqual(arguments[arguments.index('--token-file') + 1], str(self.token))
        self.assertNotIn(self.token_value, ' '.join(arguments))
        self.assertNotIn(self.token_payload['s'], ' '.join(arguments))
        self.assertFalse(any(key.upper().startswith(('TUNNEL_', 'CLOUDFLARE_', 'CLOUDFLARED_'))
                             for key in options['env']))
        self.assertEqual(options['env']['KEEP_THIS_VALUE'], 'safe')
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(options[stream], subprocess.DEVNULL)
        if os.name == 'nt':
            self.assertEqual(options['creationflags'], subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(json.loads((self.root / 'cloudflared-config.json').read_text()), {})
        self.assertTrue(self.controller.stop())
        expected = signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM
        self.assertEqual(process.signals, [expected])
        self.assertIsNone(self.controller.process)

    def test_absent_configuration_and_altered_binary_never_launch(self):
        self.runtime.unlink()
        with patch.object(tunnel.subprocess, 'Popen') as launch:
            with self.assertRaises(ValueError):
                self.controller.start()
            launch.assert_not_called()
        self.save_settings()
        self.binary.write_bytes(b'altered fixture')
        with patch.object(tunnel.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, '客户端'):
                self.controller.start()
            launch.assert_not_called()

    def test_wrong_identity_or_invalid_client_hash_never_launches(self):
        invalid = (
            ('name', 'another-project'), ('hostname', 'example.com'),
            ('tunnel_id', 'bad-id'), ('cloudflared_sha256', 'not-a-sha'),
        )
        for key, value in invalid:
            original = self.settings[key]
            self.settings[key] = value
            self.save_settings()
            with self.subTest(key=key), patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaises(ValueError):
                    self.controller.start()
                launch.assert_not_called()
            self.settings[key] = original
        self.save_settings()

    def test_missing_access_or_unknown_authentication_mode_is_rejected_before_launch(self):
        old_access = dict(self.settings)
        del old_access['authentication']
        old_access.update(team_name='invite-fixture', access_aud='a' * 64)
        missing = dict(self.settings)
        del missing['authentication']
        invalid = [old_access, missing]
        invalid.extend(dict(self.settings, authentication=value)
                       for value in ('access', 'unknown', 'APPLICATION', '', None, True, {}))
        config = self.root / 'cloudflared-config.json'
        before = b'{"preserve-existing-config": true}\n'
        config.write_bytes(before)
        for settings in invalid:
            self.runtime.write_text(json.dumps(settings), encoding='utf-8')
            with self.subTest(settings=settings), \
                    patch.object(self.controller, 'check_metrics_port') as metrics, \
                    patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, '应用账号登录模式'):
                    self.controller.start()
                metrics.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(config.read_bytes(), before)
        self.save_settings()

    def test_application_mode_with_old_access_fields_is_not_silently_downgraded(self):
        for fields in ({'team_name': 'invite-fixture'}, {'access_aud': 'a' * 64},
                       {'team_name': 'invite-fixture', 'access_aud': 'a' * 64}):
            self.runtime.write_text(json.dumps(dict(self.settings, **fields)), encoding='utf-8')
            with self.subTest(fields=fields), patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'Access'):
                    self.controller.start()
                launch.assert_not_called()
            self.assertFalse((self.root / 'cloudflared-config.json').exists())
        self.save_settings()

    def test_nondefault_source_origin_and_port_cannot_open_tunnel(self):
        for settings in ({'origin': 'https://another.example.test'}, {'source_port': 0},
                         {'source_port': 8765}):
            with self.subTest(settings=settings), patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaises(ValueError):
                    tunnel.InvitationTunnel(self.root, **settings).start()
                launch.assert_not_called()

    def test_valid_but_unrelated_tunnel_uuid_cannot_open_connector(self):
        self.settings['tunnel_id'] = 'e2d1b38a-7e0c-4cbb-b6e7-f9c8b38f12a8'
        self.save_settings()
        with patch.object(tunnel.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, '本次独立邀请入口'):
                self.controller.start()
            launch.assert_not_called()
        self.assertFalse((self.root / 'cloudflared-config.json').exists())

    def test_shared_token_file_rejected_without_reading_or_changing_it(self):
        alias = self.root / 'token-alias.txt'
        os.link(self.token, alias)
        before = self.token.read_bytes()
        with patch.object(tunnel.subprocess, 'Popen') as launch:
            with self.assertRaises(ValueError):
                self.controller.start()
            launch.assert_not_called()
        self.assertEqual(self.token.read_bytes(), before)

    def test_occupied_metrics_port_does_not_launch_or_write_config(self):
        with patch.object(self.controller, 'check_metrics_port', side_effect=OSError('port occupied')), \
                patch.object(tunnel.subprocess, 'Popen') as launch:
            with self.assertRaises(OSError):
                self.controller.start()
            launch.assert_not_called()
        self.assertFalse((self.root / 'cloudflared-config.json').exists())

    def test_ready_probe_requires_http_200_and_live_owned_process(self):
        process = OwnedProcess()
        self.controller.process = process
        response = Mock(status=503)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(tunnel, 'HTTPConnection', return_value=connection) as connector:
            self.assertFalse(self.controller.check_ready())
            connector.assert_called_once_with('127.0.0.1', 8776, timeout=0.5)
            connection.request.assert_called_once_with('GET', '/ready')
            response.status = 200
            self.assertTrue(self.controller.check_ready())
            process.returncode = 1
            self.assertFalse(self.controller.check_ready())
        self.assertFalse(self.controller.ready)

    def test_readiness_timeout_keeps_ownership_and_can_be_rechecked_without_second_process(self):
        process = OwnedProcess()
        with patch.object(self.controller, 'check_metrics_port'), \
                patch.object(tunnel.subprocess, 'Popen', return_value=process) as launch, \
                patch.object(self.controller, 'check_ready', return_value=False):
            self.assertFalse(self.controller.start(timeout=0))
            self.assertIs(self.controller.process, process)
            self.assertTrue(self.controller.running)
            self.assertFalse(self.controller.ready)
            self.assertIn('超时', self.controller.last_error)
            with patch.object(self.controller, 'check_ready', return_value=True):
                self.assertTrue(self.controller.start(timeout=0))
            self.assertEqual(launch.call_count, 1)
        self.controller.stop()

    def test_stop_timeout_keeps_only_owned_process_visible_and_allows_retry(self):
        process = OwnedProcess(refuse_stop=True)
        self.controller.process = process
        with self.assertRaisesRegex(RuntimeError, '12345'):
            self.controller.stop(timeout=0)
        self.assertIs(self.controller.process, process)
        self.assertTrue(self.controller.running)
        self.assertFalse(self.controller.ready)
        process.refuse_stop = False
        self.assertTrue(self.controller.stop(timeout=0))
        self.assertFalse(self.controller.running)
        self.assertIsNone(self.controller.process)
        self.assertFalse(self.controller.stop())

    def test_proxy_is_loopback_only_for_the_owned_http2_process_and_stops_with_it(self):
        self.settings.update(edge_proxy_port=7890,
                             edge_preferred_addresses=['198.41.192.57', '198.41.200.193'])
        self.save_settings()
        relay = Mock()
        relay.start.return_value = ['127.0.0.1:18001', '127.0.0.1:18002']
        process = OwnedProcess()
        with patch.object(self.controller, 'check_metrics_port'), \
                patch.object(tunnel, 'LoopbackEdgeProxy', return_value=relay) as make_proxy, \
                patch.object(tunnel.subprocess, 'Popen', return_value=process) as launch, \
                patch.object(self.controller, 'check_ready', return_value=True):
            self.assertTrue(self.controller.start())
        make_proxy.assert_called_once_with(7890, ('198.41.192.57', '198.41.200.193'))
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[arguments.index('--protocol') + 1], 'http2')
        self.assertIn('--no-prechecks', arguments)
        self.assertEqual([arguments[index + 1] for index, arg in enumerate(arguments)
                          if arg == '--edge'], relay.start.return_value)
        self.assertNotIn(self.token_value, ' '.join(arguments))
        self.assertTrue(self.controller.stop())
        relay.stop.assert_called_once_with()
        self.assertIsNone(self.controller.edge_proxy)

    def test_proxy_is_cleaned_up_if_owned_client_launch_fails(self):
        self.settings['edge_proxy_port'] = 7890
        self.save_settings()
        relay = Mock()
        relay.start.return_value = ['127.0.0.1:18001', '127.0.0.1:18002']
        with patch.object(self.controller, 'check_metrics_port'), \
                patch.object(tunnel, 'LoopbackEdgeProxy', return_value=relay), \
                patch.object(tunnel.subprocess, 'Popen', side_effect=OSError('fixture launch failure')):
            with self.assertRaises(OSError):
                self.controller.start()
        relay.stop.assert_called_once_with()
        self.assertIsNone(self.controller.edge_proxy)
        self.assertIsNone(self.controller.process)

    def test_retry_after_client_exits_stops_old_proxy_before_replacing_it(self):
        self.settings['edge_proxy_port'] = 7890
        self.save_settings()
        old_relay, new_relay = Mock(), Mock()
        new_relay.start.return_value = ['127.0.0.1:18001', '127.0.0.1:18002']
        self.controller.edge_proxy = old_relay
        self.controller.process = OwnedProcess(returncode=1)
        events = []
        old_relay.stop.side_effect = lambda: events.append('old stopped')
        new_relay.start.side_effect = lambda: events.append('new started') or ['127.0.0.1:18001', '127.0.0.1:18002']
        with patch.object(self.controller, 'check_metrics_port'), \
                patch.object(tunnel, 'LoopbackEdgeProxy', return_value=new_relay), \
                patch.object(tunnel.subprocess, 'Popen', return_value=OwnedProcess()), \
                patch.object(self.controller, 'check_ready', return_value=True):
            self.assertTrue(self.controller.start())
        self.assertEqual(events, ['old stopped', 'new started'])
        self.controller.stop()

    def test_invalid_proxy_configuration_is_rejected_before_launch(self):
        invalid = [{'edge_proxy_port': value} for value in (True, False, 0, -1, 65536, '7890', {})]
        invalid += [{'edge_proxy_port': 7890, 'edge_preferred_addresses': value}
                    for value in (None, {}, '198.41.192.57', ['127.0.0.1:7890'],
                                  ['::1'], [1], ['198.41.192.57'] * 3)]
        invalid.append({'edge_preferred_addresses': ['198.41.192.57']})
        for fields in invalid:
            self.runtime.write_text(json.dumps(dict(self.settings, **fields)), encoding='utf-8')
            with self.subTest(fields=fields), patch.object(tunnel.subprocess, 'Popen') as launch:
                with self.assertRaises(ValueError):
                    self.controller.start()
                launch.assert_not_called()

    def test_child_that_exited_before_ready_is_not_reported_as_connected(self):
        process = OwnedProcess(returncode=1)
        with patch.object(self.controller, 'check_metrics_port'), \
                patch.object(tunnel.subprocess, 'Popen', return_value=process):
            self.assertFalse(self.controller.start(timeout=0))
        self.assertFalse(self.controller.ready)
        self.assertIn('已退出', self.controller.last_error)
        self.assertTrue(self.controller.stop())


if __name__ == '__main__':
    unittest.main()
