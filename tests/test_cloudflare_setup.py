"""Isolated Cloudflare setup tests; no real credentials, network, or source server."""
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import Mock, patch

from scripts import configure_cloudflare as setup


ACCOUNT_PATH = f'/accounts/{setup.ACCOUNT}'
TUNNEL_PATH = f'{ACCOUNT_PATH}/cfd_tunnel/{setup.TUNNEL_ID}'
DNS_PATH = '/zones/test-zone/dns_records'


def fixture_tunnel_token(**changes):
    # Random synthetic secret has never been issued by Cloudflare or used to connect.
    fields = {'a': setup.ACCOUNT, 't': setup.TUNNEL_ID,
              's': base64.b64encode(os.urandom(32)).decode('ascii')}
    fields.update(changes)
    return base64.b64encode(json.dumps(fields).encode('utf-8')).decode('ascii')


TEST_TOKEN = fixture_tunnel_token()


def approved_config():
    return {'ingress': [
        {'hostname': setup.HOSTNAME, 'service': 'http://127.0.0.1:8775',
         'originRequest': {'httpHostHeader': setup.HOSTNAME, 'disableChunkedEncoding': True}},
        {'service': 'http_status:404'}]}


class FakeClient:
    """Remote state and response shapes follow the Cloudflare v4 envelopes."""
    def __init__(self):
        self.events = []
        self.zones = [{'id': 'test-zone', 'name': 'example.com',
                       'account': {'id': setup.ACCOUNT}}]
        self.tunnel = {'id': setup.TUNNEL_ID, 'name': setup.NAME,
                       'config_src': 'cloudflare', 'deleted_at': None}
        self.config = {}
        self.records = []
        self.main_records = {
            'example.com': [{'id': 'root', 'type': 'AAAA', 'name': 'example.com',
                           'content': '100::', 'proxied': True}],
            'www.example.com': [{'id': 'www', 'type': 'CNAME', 'name': 'www.example.com',
                               'content': 'example.com', 'proxied': True}]}
        self.fail = None
        self.after_route = None
        self.after_dns = None

    def event(self, method, path, payload=None, **query):
        event = (method, path, copy.deepcopy(payload), query)
        self.events.append(event)
        if self.fail and self.fail(event):
            raise setup.SetupError('isolated injected API failure')

    def get(self, path, **query):
        self.event('GET', path, **query)
        if path == TUNNEL_PATH:
            result = self.tunnel
        elif path == TUNNEL_PATH + '/configurations':
            result = {'config': self.config}
        elif path == TUNNEL_PATH + '/token':
            result = TEST_TOKEN
        else:
            raise AssertionError(f'unexpected fixture GET: {path}')
        return copy.deepcopy(result)

    def items(self, path, **query):
        self.event('LIST', path, **query)
        if path == '/zones':
            result = self.zones
        elif path == DNS_PATH:
            name = query.get('name')
            result = self.records if name == setup.HOSTNAME else self.main_records[name]
        else:
            raise AssertionError(f'unexpected fixture LIST: {path}')
        return copy.deepcopy(result)

    def call(self, method, path, payload=None, **query):
        self.event(method, path, payload, **query)
        if method == 'PUT' and path == TUNNEL_PATH + '/configurations':
            self.config = copy.deepcopy(payload['config'])
            if self.after_route:
                self.after_route(self)
            result = {'config': self.config}
        elif method == 'POST' and path == DNS_PATH:
            self.records = [dict(payload, id='test-dns')]
            if self.after_dns:
                self.after_dns(self)
            result = self.records[0]
        else:
            raise AssertionError(f'unexpected fixture mutation: {method} {path}')
        return {'success': True, 'result': copy.deepcopy(result)}

    @property
    def writes(self):
        return [event for event in self.events if event[0] in ('POST', 'PUT', 'DELETE', 'PATCH')]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cloudflare-setup-fixture-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        binary = self.directory / 'tools/cloudflared.exe'
        binary.parent.mkdir()
        binary.write_bytes(b'isolated fixture executable, never run')
        digest = patch.object(setup, 'EXE_SHA256', hashlib.sha256(binary.read_bytes()).hexdigest())
        digest.start()
        self.addCleanup(digest.stop)
        self.client = FakeClient()

    def run_setup(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return setup.configure(self.client, self.directory, **kwargs)

    def assert_no_local_credentials(self):
        self.assertFalse((self.directory / 'tunnel-token.txt').exists())
        self.assertFalse((self.directory / 'tunnel-runtime.json').exists())
        self.assertFalse((self.directory / 'tunnel-runtime.json.tmp').exists())

    def assert_blocked_before_writes(self):
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertEqual([], self.client.writes)
        self.assert_no_local_credentials()

    def test_publish_verifies_application_route_before_exact_dns_and_saves_runtime(self):
        original_main = copy.deepcopy(self.client.main_records)
        runtime = self.run_setup()
        self.assertEqual({'tunnel_id': setup.TUNNEL_ID, 'name': setup.NAME,
                          'hostname': setup.HOSTNAME, 'authentication': 'application',
                          'cloudflared_sha256': setup.EXE_SHA256}, runtime)
        writes = self.client.writes
        self.assertEqual([('PUT', TUNNEL_PATH + '/configurations'), ('POST', DNS_PATH)],
                         [(method, path) for method, path, _, _ in writes])
        events = self.client.events
        route_index, dns_index = (events.index(write) for write in writes)
        self.assertIn(('GET', TUNNEL_PATH + '/configurations'),
                      [(e[0], e[1]) for e in events[route_index + 1:dns_index]])
        self.assertEqual(approved_config(), writes[0][2]['config'])
        self.assertEqual({'type': 'CNAME', 'name': setup.HOSTNAME, 'proxied': True,
                          'content': setup.TUNNEL_ID + '.cfargotunnel.com', 'ttl': 1}, writes[1][2])
        self.assertEqual(approved_config(), self.client.config)
        self.assertEqual(original_main, self.client.main_records)
        self.assertEqual(TEST_TOKEN, (self.directory / 'tunnel-token.txt').read_text(encoding='utf-8'))
        self.assertEqual(runtime, json.loads((self.directory / 'tunnel-runtime.json').read_text()))
        self.assertEqual([], list(self.directory.glob('*.tmp')))
        self.assertTrue(all('/access/' not in event[1] for event in events))
        self.assertTrue(all('havia-dev' not in json.dumps(event) for event in events))
        self.assertTrue(all(event[0] != 'DELETE' for event in events))
        for name in original_main:
            self.assertGreaterEqual(sum(event[1] == DNS_PATH and event[3].get('name') == name
                                        for event in events), 2)

    def test_check_only_never_mutates_requests_access_token_or_saves_configuration(self):
        for config in ({}, approved_config()):
            with self.subTest(has_route=bool(config)):
                self.client = FakeClient()
                self.client.config = config
                before = copy.deepcopy(self.client.__dict__)
                self.run_setup(check_only=True)
                self.assertEqual([], self.client.writes)
                self.assertEqual(before['config'], self.client.config)
                self.assertEqual(before['records'], self.client.records)
                self.assertEqual(before['main_records'], self.client.main_records)
                self.assert_no_local_credentials()
                self.assertNotIn(('GET', TUNNEL_PATH + '/token'), [(e[0], e[1]) for e in self.client.events])
                self.assertTrue(all('/access/' not in e[1] for e in self.client.events))

    def test_approved_existing_resources_are_reused_without_remote_writes(self):
        self.client.config = approved_config()
        self.client.records = [{'id': 'test-dns', 'type': 'CNAME', 'name': setup.HOSTNAME,
                                'content': setup.TUNNEL_ID + '.cfargotunnel.com', 'proxied': True}]
        self.run_setup()
        self.assertEqual([], self.client.writes)
        self.assertTrue(all('/access/' not in e[1] for e in self.client.events))

    def test_binary_digest_mismatch_stops_before_any_api_access(self):
        (self.directory / 'tools/cloudflared.exe').write_bytes(b'changed')
        self.assert_blocked_before_writes()
        self.assertEqual([], self.client.events)

    def test_wrong_account_zone_name_or_nonunique_zone_stops_before_writes(self):
        zone = self.client.zones[0]
        candidates = ([], [zone, dict(zone, id='duplicate')],
                      [dict(zone, name='unrelated.example')],
                      [dict(zone, account={'id': 'unrelated'})],
                      [dict(zone, id='../unrelated')], [dict(zone, id=None)])
        for zones in candidates:
            with self.subTest(zones=zones):
                self.client = FakeClient()
                self.client.zones = zones
                self.assert_blocked_before_writes()

    def test_dns_conflicts_stop_before_remote_writes(self):
        approved = {'id': 'conflict', 'type': 'CNAME', 'name': setup.HOSTNAME,
                    'content': setup.TUNNEL_ID + '.cfargotunnel.com', 'proxied': True}
        for changes in ({'type': 'A', 'content': '127.0.0.1'},
                        {'content': 'unrelated.example'}, {'proxied': False},
                        {'proxied': 'true'}, {'name': 'www.example.com'}):
            with self.subTest(changes=changes):
                self.client = FakeClient()
                self.client.records = [dict(approved, **changes)]
                self.assert_blocked_before_writes()
        self.client = FakeClient()
        self.client.records = [approved, dict(approved, id='duplicate')]
        self.assert_blocked_before_writes()

    def test_unrelated_deleted_or_locally_managed_tunnel_cannot_be_reconfigured(self):
        for changes in ({'id': 'unrelated'}, {'name': 'havia-dev'},
                        {'config_src': 'local'}, {'deleted_at': '2026-10-13'}):
            with self.subTest(changes=changes):
                self.client = FakeClient()
                self.client.tunnel.update(changes)
                self.assert_blocked_before_writes()

    def test_unrelated_existing_route_stops_before_any_mutation(self):
        self.client.config = {'ingress': [{'hostname': 'www.example.com', 'service': 'http://localhost:8000'}]}
        self.assert_blocked_before_writes()

    def test_access_protected_ingress_is_never_silently_rewritten_even_in_check_only(self):
        for check_only in (False, True):
            for required in (True, False):
                with self.subTest(check_only=check_only, required=required):
                    self.client = FakeClient()
                    config = approved_config()
                    config['ingress'][0]['originRequest']['access'] = {
                        'required': required, 'teamName': 'isolated-team', 'audTag': ['fixture-audience']}
                    self.client.config = copy.deepcopy(config)
                    with self.assertRaises(setup.SetupError):
                        self.run_setup(check_only=check_only)
                    self.assertEqual(config, self.client.config)
                    self.assertEqual([], self.client.writes)
                    self.assertTrue(all('/access/' not in e[1] for e in self.client.events))
                    self.assert_no_local_credentials()

    def test_extra_config_or_route_options_are_not_overwritten(self):
        for config in ({'originRequest': {'connectTimeout': 45}},
                       dict(approved_config(), originRequest={'httpHostHeader': 'www.example.com'}),
                       dict(approved_config(), **{'warp-routing': {'enabled': True}})):
            with self.subTest(config=config):
                self.client = FakeClient()
                self.client.config = copy.deepcopy(config)
                self.assert_blocked_before_writes()
                self.assertEqual(config, self.client.config)

    def test_successful_null_or_empty_config_response_remains_supported_without_writes_in_preflight(self):
        for response in (None, {}, {'config': None}, {'config': {}}):
            with self.subTest(response=response):
                self.client = FakeClient()
                original_get = self.client.get
                self.client.get = lambda path, **query: (
                    copy.deepcopy(response) if path == TUNNEL_PATH + '/configurations'
                    else original_get(path, **query))
                self.run_setup(check_only=True)
                self.assertEqual([], self.client.writes)
                self.assert_no_local_credentials()

    def test_successful_null_configuration_can_be_published_then_verified(self):
        original_get = self.client.get

        def get(path, **query):
            if path == TUNNEL_PATH + '/configurations' and not self.client.config:
                self.client.event('GET', path, **query)
                return None
            return original_get(path, **query)

        self.client.get = get
        self.run_setup()
        self.assertEqual(approved_config(), self.client.config)
        self.assertEqual([('PUT', TUNNEL_PATH + '/configurations'), ('POST', DNS_PATH)],
                         [(event[0], event[1]) for event in self.client.writes])

    def test_malformed_config_response_is_not_assumed_empty(self):
        responses = ([], {'config': []}, {'config': False},
                     {'config': 'unexpected'}, {'config': {'ingress': None}})
        for response in responses:
            with self.subTest(response=response):
                self.client = FakeClient()
                original_get = self.client.get
                self.client.get = lambda path, **query: (
                    copy.deepcopy(response) if path == TUNNEL_PATH + '/configurations'
                    else original_get(path, **query))
                self.assert_blocked_before_writes()

    def test_configuration_get_404_stops_without_creating_assumed_configuration(self):
        real_client = setup.Client(TEST_TOKEN)
        real_client._opener = Mock()
        body = json.dumps({'success': False, 'errors': [{'code': 1000, 'message': TEST_TOKEN}]}).encode()
        real_client._opener.open.side_effect = HTTPError(
            'https://fixture.invalid/' + TEST_TOKEN, 404, TEST_TOKEN, {}, io.BytesIO(body))
        original_get = self.client.get
        self.client.get = lambda path, **query: (
            real_client.get(path) if path == TUNNEL_PATH + '/configurations'
            else original_get(path, **query))
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assertIn('HTTP 404', str(caught.exception))
        self.assertNotIn(TEST_TOKEN, str(caught.exception))
        self.assertEqual([], self.client.writes)
        self.assert_no_local_credentials()

    def test_route_readback_mismatch_prevents_dns_or_local_credentials(self):
        self.client.after_route = lambda client: client.config['ingress'][0]['originRequest'].update(httpHostHeader='wrong.example')
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertEqual([], self.client.records)
        self.assert_no_local_credentials()
        self.assertFalse(any(e[0] == 'DELETE' for e in self.client.events))

    def test_route_readback_extra_access_options_prevent_dns(self):
        self.client.after_route = lambda client: client.config.update(originRequest={'access': {'required': True}})
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertEqual([], self.client.records)
        self.assert_no_local_credentials()

    def test_route_write_failure_never_publishes_dns_or_credentials(self):
        self.client.fail = lambda event: event[:2] == ('PUT', TUNNEL_PATH + '/configurations')
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertEqual({}, self.client.config)
        self.assertEqual([], self.client.records)
        self.assert_no_local_credentials()

    def test_dns_changed_during_route_creation_is_never_overwritten(self):
        self.client.after_route = lambda client: client.records.append(
            {'id': 'concurrent', 'type': 'A', 'name': setup.HOSTNAME, 'content': '192.0.2.1'})
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertFalse(any(e[:2] == ('POST', DNS_PATH) for e in self.client.writes))
        self.assertEqual('192.0.2.1', self.client.records[0]['content'])
        self.assert_no_local_credentials()

    def test_dns_api_failure_retains_existing_route_and_never_saves_credentials(self):
        self.client.fail = lambda event: event[:2] == ('POST', DNS_PATH)
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assertEqual(approved_config(), self.client.config)
        self.assertEqual([], self.client.records)
        self.assert_no_local_credentials()
        self.assertFalse(any(e[0] == 'DELETE' for e in self.client.events))

    def test_dns_readback_mismatch_does_not_save_local_credentials(self):
        self.client.after_dns = lambda client: client.records[0].update(name='www.example.com')
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assert_no_local_credentials()

    def test_main_site_dns_changed_externally_is_detected_before_token_save(self):
        self.client.after_dns = lambda client: client.main_records['www.example.com'][0].update(content='changed.example')
        with self.assertRaises(setup.SetupError):
            self.run_setup()
        self.assert_no_local_credentials()
        self.assertNotIn(('GET', TUNNEL_PATH + '/token'), [(e[0], e[1]) for e in self.client.events])
        self.assertFalse(any(e[0] == 'DELETE' for e in self.client.events))

    def test_invalid_tunnel_token_is_not_saved(self):
        for token in (None, '', 'two words', {'token': TEST_TOKEN}, []):
            with self.subTest(token_type=type(token).__name__):
                self.client = FakeClient()
                original_get = self.client.get
                self.client.get = lambda path, **query: (
                    token if path == TUNNEL_PATH + '/token' else original_get(path, **query))
                with self.assertRaises(setup.SetupError):
                    self.run_setup()
                self.assert_no_local_credentials()

    def test_runtime_only_prepares_fixed_local_metadata_without_api_calls(self):
        output = io.StringIO()
        with (patch.object(setup, 'Client') as client_factory,
              contextlib.redirect_stdout(output)):
            runtime = setup.prepare_runtime(TEST_TOKEN, self.directory)
        client_factory.assert_not_called()
        self.assertEqual('application', runtime['authentication'])
        self.assertEqual(setup.TUNNEL_ID, runtime['tunnel_id'])
        self.assertNotIn('team_name', runtime)
        self.assertNotIn('access_aud', runtime)
        self.assertEqual(TEST_TOKEN, (self.directory / 'tunnel-token.txt').read_text())
        self.assertEqual(runtime, json.loads((self.directory / 'tunnel-runtime.json').read_text()))
        self.assertIn('未验证云路由', output.getvalue())
        self.assertIn('验收', output.getvalue())
        self.assertNotIn(TEST_TOKEN, output.getvalue())

    def test_runtime_only_rejects_empty_or_invalid_token_before_writing(self):
        for token in ('', 'two words', None, {}, 'not-base64',
                      fixture_tunnel_token(a='unrelated-account'),
                      fixture_tunnel_token(t='unrelated-tunnel'),
                      fixture_tunnel_token(s='not-base64'),
                      fixture_tunnel_token(s=''),
                      base64.b64encode(b'[]').decode('ascii')):
            with self.subTest(token_type=type(token).__name__):
                with self.assertRaises(setup.SetupError):
                    setup.prepare_runtime(token, self.directory)
                self.assert_no_local_credentials()

    def test_automatic_setup_rejects_wrong_tunnel_token_before_local_files(self):
        original_get = self.client.get
        token = fixture_tunnel_token(t='different-tunnel')
        self.client.get = lambda path, **query: (
            token if path == TUNNEL_PATH + '/token' else original_get(path, **query))
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup()
        self.assert_no_local_credentials()
        self.assertNotIn(token, str(caught.exception))

    def test_runtime_only_different_existing_token_stops_before_metadata_publication(self):
        target = self.directory / 'tunnel-token.txt'
        target.write_text('different-fixture-token', encoding='utf-8')
        with self.assertRaises(setup.SetupError):
            setup.prepare_runtime(TEST_TOKEN, self.directory)
        self.assertEqual('different-fixture-token', target.read_text(encoding='utf-8'))
        self.assertFalse((self.directory / 'tunnel-runtime.json').exists())
        self.assertEqual([], list(self.directory.glob('*.tmp')))

    def test_runtime_only_reuses_matching_credentials_and_metadata(self):
        with contextlib.redirect_stdout(io.StringIO()):
            original = setup.prepare_runtime(TEST_TOKEN, self.directory)
            metadata = (self.directory / 'tunnel-runtime.json').read_bytes()
            current = setup.prepare_runtime(TEST_TOKEN, self.directory)
        self.assertEqual(original, current)
        self.assertEqual(metadata, (self.directory / 'tunnel-runtime.json').read_bytes())
        self.assertEqual(TEST_TOKEN, (self.directory / 'tunnel-token.txt').read_text())
        self.assertEqual([], list(self.directory.glob('*.tmp')))

    def test_old_access_or_different_metadata_is_never_overwritten_in_either_mode(self):
        approved = {'tunnel_id': setup.TUNNEL_ID, 'name': setup.NAME,
                    'hostname': setup.HOSTNAME, 'authentication': 'application',
                    'cloudflared_sha256': setup.EXE_SHA256}
        configurations = (dict(approved, team_name='old-team', access_aud='old-audience'),
                          dict(approved, authentication='access'),
                          dict(approved, hostname='www.example.com'),
                          dict(approved, tunnel_id='unrelated'),
                          {'tunnel_id': setup.TUNNEL_ID, 'team_name': 'old-team'})
        destination = self.directory / 'tunnel-runtime.json'
        for runtime_only in (False, True):
            for metadata in configurations:
                with self.subTest(runtime_only=runtime_only, metadata=metadata):
                    self.client = FakeClient()
                    content = json.dumps(metadata)
                    destination.write_text(content, encoding='utf-8')
                    with self.assertRaises(setup.SetupError):
                        if runtime_only:
                            setup.prepare_runtime(TEST_TOKEN, self.directory)
                        else:
                            self.run_setup()
                    self.assertEqual(content, destination.read_text(encoding='utf-8'))
                    self.assertFalse((self.directory / 'tunnel-token.txt').exists())
                    self.assertEqual([], self.client.events)
        destination.unlink()


class APIClientTests(unittest.TestCase):
    def client(self, response=None, error=None):
        client = setup.Client(TEST_TOKEN)
        opener = Mock()
        if error:
            opener.open.side_effect = error
        else:
            stream = Mock()
            stream.read.return_value = json.dumps(response).encode()
            opener.open.return_value.__enter__ = Mock(return_value=stream)
            opener.open.return_value.__exit__ = Mock(return_value=False)
        client._opener = opener
        return client

    def test_http_403_diagnostics_identify_read_stage_without_disclosing_details(self):
        body = json.dumps({'success': False,
                           'errors': [{'code': 9109, 'message': 'body-secret-' + TEST_TOKEN}],
                           'messages': ['message-secret-' + TEST_TOKEN],
                           'result': {'token': TEST_TOKEN}}).encode()
        stages = (('/zones', '域名列表'),
                  (TUNNEL_PATH + '/configurations', '独立 Tunnel 及配置'),
                  ('/user/tokens/verify', '临时 Token 验证'))
        diagnostics = []
        for path, stage in stages:
            with self.subTest(stage=stage):
                error = HTTPError('https://fixture.invalid/' + TEST_TOKEN, 403,
                                  'reason-secret-' + TEST_TOKEN,
                                  {'X-Fixture': 'header-secret-' + TEST_TOKEN}, io.BytesIO(body))
                client = self.client(error=error)
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(setup.SetupError) as caught:
                        client.get(path)
                diagnostic = str(caught.exception)
                self.assertIn(stage, diagnostic)
                self.assertIn('HTTP 403', diagnostic)
                self.assertIn('9109', diagnostic)
                self.assertIn('JSON', diagnostic)
                for secret in (TEST_TOKEN, 'body-secret-', 'message-secret-',
                               'reason-secret-', 'header-secret-', path, setup.ACCOUNT):
                    self.assertNotIn(secret, diagnostic + output.getvalue() + errors.getvalue())
                client._opener.open.assert_called_once()
                diagnostics.append(diagnostic)
        self.assertEqual(3, len(set(diagnostics)))

    def test_http_error_codes_are_limited_to_five_bounded_positive_integers(self):
        import re
        codes = (True, False, '777777', 0, -42, 1000000, 10 ** 80,
                 1, 999999, 9109, 10000, 10001, 10002, 10003)
        rows = [None, 'untrusted-error-' + TEST_TOKEN, {}]
        rows.extend({'code': code, 'message': TEST_TOKEN} for code in codes)
        body = json.dumps({'success': False, 'errors': rows}).encode()
        error = HTTPError('https://fixture.invalid', 403, TEST_TOKEN, {}, io.BytesIO(body))
        client = self.client(error=error)
        with self.assertRaises(setup.SetupError) as caught:
            client.get('/zones')
        diagnostic = str(caught.exception)
        self.assertEqual([403, 1, 999999, 9109, 10000, 10001],
                         [int(value) for value in re.findall(r'\d+', diagnostic)])
        self.assertNotIn(TEST_TOKEN, diagnostic)
        self.assertLess(len(diagnostic), 1000)
        client._opener.open.assert_called_once()

    def test_nonstandard_http_error_body_never_echoes_details(self):
        path = TUNNEL_PATH + '/configurations'
        for body in (('<html>body-secret-' + TEST_TOKEN + '</html>').encode(),
                     ('{"errors": [broken-json-' + TEST_TOKEN).encode()):
            with self.subTest(body_kind=body[:6]):
                error = HTTPError('https://fixture.invalid/' + TEST_TOKEN, 403, TEST_TOKEN,
                                  {'Location': 'https://unrelated.invalid/' + TEST_TOKEN}, io.BytesIO(body))
                client = self.client(error=error)
                with self.assertRaises(setup.SetupError) as caught:
                    client.get(path)
                diagnostic = str(caught.exception)
                self.assertIn('HTTP 403', diagnostic)
                self.assertIn('非标准', diagnostic)
                for detail in (TEST_TOKEN, setup.ACCOUNT, path, 'path-secret-',
                               'body-secret-', 'broken-json-', 'unrelated.invalid'):
                    self.assertNotIn(detail, diagnostic)
                client._opener.open.assert_called_once()

    def test_oversized_http_error_body_has_bounded_read_and_diagnostic(self):
        body = Mock(wraps=io.BytesIO(b'{"errors":[{"message":"' +
                                    TEST_TOKEN.encode() * 300000 + b'"}]}'))
        error = HTTPError('https://fixture.invalid', 403, TEST_TOKEN, {}, body)
        client = self.client(error=error)
        with self.assertRaises(setup.SetupError) as caught:
            client.get('/zones')
        body.read.assert_called_once()
        self.assertEqual(1, len(body.read.call_args.args))
        limit = body.read.call_args.args[0]
        self.assertIs(type(limit), int)
        self.assertGreater(limit, 0)
        self.assertLessEqual(limit, 4 * 1024 * 1024)
        self.assertLess(len(str(caught.exception)), 1000)
        self.assertNotIn(TEST_TOKEN, str(caught.exception))
        client._opener.open.assert_called_once()

    def test_token_verify_is_allowed_as_an_exact_read_only_user_endpoint(self):
        self.assertEqual('/user/tokens/verify', setup.VERIFY_PATH)
        client = self.client({'success': True, 'result': {'status': 'active'}})
        self.assertEqual({'status': 'active'}, client.get(setup.VERIFY_PATH))
        client._opener.open.assert_called_once()
        request = client._opener.open.call_args.args[0]
        self.assertEqual('GET', request.get_method())
        self.assertIsNone(request.data)
        self.assertEqual(setup.BASE + setup.VERIFY_PATH, request.full_url)

    def test_other_user_paths_and_token_verify_writes_payloads_or_query_are_never_sent(self):
        verification = '/user/tokens/verify'
        requests = (('GET', '/user', None, {}),
                    ('GET', '/user/tokens', None, {}),
                    ('GET', verification + '/', None, {}),
                    ('GET', verification + '/unexpected', None, {}),
                    ('GET', verification + '?secret=' + TEST_TOKEN, None, {}),
                    ('POST', verification, None, {}),
                    ('PUT', verification, {'status': 'active'}, {}),
                    ('DELETE', verification, None, {}),
                    ('GET', verification, {}, {}),
                    ('GET', verification, None, {'page': 1}))
        for method, path, payload, query in requests:
            with self.subTest(method=method, path=path, has_payload=payload is not None,
                              has_query=bool(query)):
                client = self.client({'success': True, 'result': {}})
                with self.assertRaises(setup.SetupError) as caught:
                    client.call(method, path, payload, **query)
                self.assertNotIn(TEST_TOKEN, str(caught.exception))
                client._opener.open.assert_not_called()

    def test_http_or_api_errors_never_echo_secret_or_raw_response(self):
        errors = (HTTPError('https://fixture.invalid', 403, TEST_TOKEN, {}, io.BytesIO(TEST_TOKEN.encode())),
                  HTTPError('https://fixture.invalid', 404, TEST_TOKEN, {}, io.BytesIO(TEST_TOKEN.encode())),
                  URLError(TEST_TOKEN))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(setup.SetupError) as caught:
                    self.client(error=error).get(TUNNEL_PATH)
                self.assertNotIn(TEST_TOKEN, str(caught.exception))
        with self.assertRaises(setup.SetupError) as caught:
            self.client({'success': False, 'errors': [{'message': TEST_TOKEN}]}).get(TUNNEL_PATH)
        self.assertNotIn(TEST_TOKEN, str(caught.exception))

    def test_malformed_json_envelope_fails_with_safe_setup_error(self):
        for response in (None, [], 'unexpected', 123):
            with self.subTest(response=response):
                with self.assertRaises(setup.SetupError):
                    self.client(response).get(TUNNEL_PATH)

    def test_api_request_outside_fixed_account_is_never_sent(self):
        client = self.client({'success': True, 'result': {}})
        with self.assertRaises(setup.SetupError):
            client.get('/accounts/unrelated/access/apps')
        client._opener.open.assert_not_called()

    def test_access_unknown_tunnel_and_zone_paths_are_never_sent(self):
        paths = (ACCOUNT_PATH + '/access/organizations',
                 ACCOUNT_PATH + '/access/identity_providers', ACCOUNT_PATH + '/access/apps',
                 ACCOUNT_PATH + '/access/apps/fixture/policies',
                 ACCOUNT_PATH + '/cfd_tunnel/unrelated', TUNNEL_PATH + '/unexpected',
                 '/zones-other', '/zones/unrelated/dns_records', '/zones/test-zone')
        for path in paths:
            with self.subTest(path=path):
                client = self.client({'success': True, 'result': {}})
                client.zone_id = 'test-zone'
                with self.assertRaises(setup.SetupError):
                    client.get(path)
                client._opener.open.assert_not_called()

    def test_zone_and_tunnel_writes_are_restricted_to_exact_approved_configuration(self):
        calls = (('POST', '/zones', {}, {}), ('DELETE', TUNNEL_PATH, None, {}),
                 ('POST', TUNNEL_PATH + '/token', {}, {}),
                 ('PUT', TUNNEL_PATH + '/configurations', {'config': {'ingress': []}}, {}),
                 ('POST', DNS_PATH, {'name': 'www.example.com'}, {}),
                 ('GET', '/zones', None, {'account.id': 'unrelated'}),
                 ('GET', '/zones', None, {'name': 'unrelated.example'}),
                 ('GET', DNS_PATH, None, {'name': 'unrelated.example'}))
        for method, path, payload, query in calls:
            with self.subTest(method=method, path=path):
                client = self.client({'success': True, 'result': {}})
                client.zone_id = 'test-zone'
                with self.assertRaises(setup.SetupError):
                    client.call(method, path, payload, **query)
                client._opener.open.assert_not_called()

    def test_verified_zone_allows_only_the_required_dns_read_and_write(self):
        client = self.client({'success': True, 'result': []})
        with self.assertRaises(setup.SetupError):
            client.get(DNS_PATH, name=setup.HOSTNAME)
        client._opener.open.assert_not_called()
        client.zone_id = 'test-zone'
        client.get(DNS_PATH, name=setup.HOSTNAME)
        request = client._opener.open.call_args.args[0]
        self.assertEqual(setup.BASE + DNS_PATH + '?name=' + setup.HOSTNAME, request.full_url)
        self.assertEqual('GET', request.get_method())


    def test_redirect_handler_never_forwards_authorization_request(self):
        request = Request(setup.BASE + TUNNEL_PATH, headers={'Authorization': 'Bearer ' + TEST_TOKEN})
        self.assertIsNone(setup.NoRedirect().redirect_request(
            request, None, 302, 'Found', {}, 'https://unrelated.invalid/collect'))
        client = setup.Client(TEST_TOKEN)
        handlers = [handler for handler in client._opener.handlers if isinstance(handler, setup.NoRedirect)]
        self.assertEqual(1, len(handlers))

    def test_list_pagination_is_bounded_and_returns_all_results(self):
        client = setup.Client(TEST_TOKEN)
        client.call = Mock(side_effect=[{'success': True, 'result': [{'id': 'one'}], 'result_info': {'total_pages': 2}},
                                        {'success': True, 'result': [{'id': 'two'}], 'result_info': {'total_pages': 2}}])
        self.assertEqual([{'id': 'one'}, {'id': 'two'}], client.items('/zones'))
        self.assertEqual([1, 2], [call.kwargs['page'] for call in client.call.call_args_list])
        client.call = Mock(return_value={'success': True, 'result': [], 'result_info': {'total_pages': 101}})
        with self.assertRaises(setup.SetupError):
            client.items('/zones')
        self.assertEqual(100, client.call.call_count)

    def test_full_pages_without_metadata_stop_at_the_fixed_bound(self):
        client = setup.Client(TEST_TOKEN)
        rows = [{'id': str(index)} for index in range(50)]
        client.call = Mock(return_value={'success': True, 'result': rows})
        with self.assertRaises(setup.SetupError):
            client.items('/zones')
        self.assertEqual(100, client.call.call_count)


class TokenVerificationTests(unittest.TestCase):
    def test_runtime_only_accepts_exact_windows_command_without_executing_or_echoing_it(self):
        command = 'cloudflared.exe service install ' + TEST_TOKEN
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(setup, 'validate_runtime'),
              patch.object(setup.sys.stdin, 'isatty', return_value=True),
              patch.object(setup.getpass, 'getpass', return_value=command) as password_prompt,
              patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
              patch.object(setup, 'Client') as client_factory,
              patch.object(setup.subprocess, 'run') as run,
              patch.object(setup.os, 'system') as system,
              patch.object(setup, 'prepare_runtime', side_effect=setup.validate_tunnel_token) as prepare,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            self.assertEqual(0, setup.main(['--runtime-only']))
        self.assertEqual(1, prepare.call_count)
        self.assertTrue(prepare.call_args.args == (TEST_TOKEN,))
        self.assertIn('Windows 命令', password_prompt.call_args.args[0])
        client_factory.assert_not_called()
        run.assert_not_called()
        system.assert_not_called()
        self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())
        self.assertNotIn(command, output.getvalue() + errors.getvalue())

    def test_runtime_only_rejects_other_commands_arguments_newlines_and_concatenation(self):
        prefix = 'cloudflared.exe service install '
        values = ('cloudflared service install ' + TEST_TOKEN,
                  '.\\cloudflared.exe service install ' + TEST_TOKEN,
                  'Cloudflared.exe service install ' + TEST_TOKEN,
                  'cloudflared.exe tunnel run --token ' + TEST_TOKEN,
                  'cloudflared.exe  service install ' + TEST_TOKEN,
                  'cloudflared.exe service install\t' + TEST_TOKEN,
                  prefix + '"' + TEST_TOKEN + '"', prefix + TEST_TOKEN + ' extra',
                  prefix + TEST_TOKEN + ' --foo', prefix + TEST_TOKEN + ' & whoami',
                  prefix + TEST_TOKEN + '&&whoami', prefix + TEST_TOKEN + ';whoami',
                  prefix + TEST_TOKEN + '|whoami', prefix + TEST_TOKEN + '`whoami`',
                  prefix + TEST_TOKEN + '$(whoami)', prefix + TEST_TOKEN + '\nwhoami',
                  prefix + TEST_TOKEN + '\r\n', TEST_TOKEN + '\n', '\n' + TEST_TOKEN,
                  prefix, prefix + ' ')
        for index, value in enumerate(values):
            with self.subTest(case=index):
                output, errors = io.StringIO(), io.StringIO()
                with (patch.object(setup, 'validate_runtime'),
                      patch.object(setup.sys.stdin, 'isatty', return_value=True),
                      patch.object(setup.getpass, 'getpass', return_value=value),
                      patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
                      patch.object(setup, 'Client') as client_factory,
                      patch.object(setup.subprocess, 'run') as run,
                      patch.object(setup.os, 'system') as system,
                      patch.object(setup, 'prepare_runtime', side_effect=setup.validate_tunnel_token) as prepare,
                      contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                    self.assertEqual(1, setup.main(['--runtime-only']))
                prepare.assert_not_called()
                client_factory.assert_not_called()
                run.assert_not_called()
                system.assert_not_called()
                self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())

    def test_runtime_command_token_still_requires_the_fixed_account_and_tunnel(self):
        for changes in ({'a': 'another-account'}, {'t': 'another-tunnel'}):
            with self.subTest(changes=changes):
                token = fixture_tunnel_token(**changes)
                output, errors = io.StringIO(), io.StringIO()
                with (patch.object(setup, 'validate_runtime'),
                      patch.object(setup.sys.stdin, 'isatty', return_value=True),
                      patch.object(setup.getpass, 'getpass', return_value='cloudflared.exe service install ' + token),
                      patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
                      patch.object(setup, 'Client') as client_factory,
                      patch.object(setup, 'prepare_runtime', side_effect=setup.validate_tunnel_token) as prepare,
                      contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                    self.assertEqual(1, setup.main(['--runtime-only']))
                self.assertEqual(1, prepare.call_count)
                self.assertTrue(prepare.call_args.args == (token,))
                client_factory.assert_not_called()
                self.assertNotIn(token, output.getvalue() + errors.getvalue())

    def test_management_token_input_does_not_accept_the_runtime_command(self):
        for args in ([], ['--check-only']):
            with self.subTest(args=args):
                output, errors = io.StringIO(), io.StringIO()
                with (patch.object(setup, 'validate_runtime'),
                      patch.object(setup.sys.stdin, 'isatty', return_value=True),
                      patch.object(setup.getpass, 'getpass', return_value='cloudflared.exe service install ' + TEST_TOKEN),
                      patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
                      patch.object(setup, 'Client') as client_factory,
                      patch.object(setup, 'configure') as configure,
                      contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                    self.assertEqual(1, setup.main(args))
                client_factory.assert_not_called()
                configure.assert_not_called()
                self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())

    def test_runtime_only_main_never_constructs_api_client_and_names_runtime_token(self):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(setup, 'validate_runtime'),
              patch.object(setup.sys.stdin, 'isatty', return_value=True),
              patch.object(setup.getpass, 'getpass', return_value=TEST_TOKEN) as password_prompt,
              patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
              patch.object(setup, 'Client') as client_factory,
              patch.object(setup, 'verify_api_token') as verify,
              patch.object(setup, 'configure') as configure,
              patch.object(setup, 'prepare_runtime') as prepare,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            self.assertEqual(0, setup.main(['--runtime-only']))
        prepare.assert_called_once_with(TEST_TOKEN)
        self.assertIn('Tunnel 运行 Token', password_prompt.call_args.args[0])
        client_factory.assert_not_called()
        verify.assert_not_called()
        configure.assert_not_called()
        self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())

    def test_runtime_only_empty_token_does_not_prepare_files_or_call_api(self):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(setup, 'validate_runtime'),
              patch.object(setup.sys.stdin, 'isatty', return_value=True),
              patch.object(setup.getpass, 'getpass', return_value='  '),
              patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
              patch.object(setup, 'Client') as client_factory,
              patch.object(setup, 'prepare_runtime') as prepare,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            self.assertEqual(1, setup.main(['--runtime-only']))
        prepare.assert_not_called()
        client_factory.assert_not_called()

    def test_check_only_and_runtime_only_are_mutually_exclusive(self):
        with (patch.object(setup, 'validate_runtime') as validate,
              contextlib.redirect_stderr(io.StringIO())):
            with self.assertRaises(SystemExit) as caught:
                setup.main(['--check-only', '--runtime-only'])
        self.assertEqual(2, caught.exception.code)
        validate.assert_not_called()

    def test_active_token_verification_is_a_single_read_without_disclosing_result(self):
        client = Mock(spec=['get', 'call', 'items'])
        client.get.return_value = {'status': 'active', 'id': TEST_TOKEN, 'message': TEST_TOKEN}
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            setup.verify_api_token(client)
        client.get.assert_called_once_with(setup.VERIFY_PATH)
        client.call.assert_not_called()
        client.items.assert_not_called()
        self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())

    def test_inactive_or_malformed_verification_result_fails_without_disclosing_it(self):
        responses = ({'status': 'disabled', 'message': TEST_TOKEN},
                     {'status': 'expired', 'id': TEST_TOKEN},
                     {'status': 'revoked'}, {'status': 'ACTIVE'}, {'status': True},
                     {'status': TEST_TOKEN}, {'message': TEST_TOKEN},
                     None, [], TEST_TOKEN, 123)
        for index, response in enumerate(responses):
            with self.subTest(case=index):
                client = Mock(spec=['get', 'call', 'items'])
                client.get.return_value = response
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(setup.SetupError) as caught:
                        setup.verify_api_token(client)
                client.get.assert_called_once_with(setup.VERIFY_PATH)
                client.call.assert_not_called()
                client.items.assert_not_called()
                self.assertNotIn(TEST_TOKEN, str(caught.exception) + output.getvalue() + errors.getvalue())

    def test_main_rejects_inactive_token_before_configuration_and_never_prints_secrets(self):
        for response in ({'status': 'expired', 'message': TEST_TOKEN, 'id': TEST_TOKEN},
                         {'status': TEST_TOKEN}, TEST_TOKEN):
            with self.subTest(case=type(response).__name__):
                client = Mock(spec=['get', 'call', 'items'])
                client.get.return_value = response
                output, errors = io.StringIO(), io.StringIO()
                with (patch.object(setup, 'validate_runtime'),
                      patch.object(setup.sys.stdin, 'isatty', return_value=True),
                      patch.object(setup.getpass, 'getpass', return_value=TEST_TOKEN),
                      patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
                      patch.object(setup, 'Client', return_value=client),
                      patch.object(setup, 'configure') as configure,
                      contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                    self.assertEqual(1, setup.main([]))
                configure.assert_not_called()
                client.get.assert_called_once_with(setup.VERIFY_PATH)
                client.call.assert_not_called()
                client.items.assert_not_called()
                self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())

    def test_main_verifies_active_token_before_entering_check_only_configuration(self):
        client = Mock(spec=['get', 'call', 'items'])
        events = []
        client.get.side_effect = lambda path: events.append(('verify', path)) or {'status': 'active'}
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(setup, 'validate_runtime'),
              patch.object(setup.sys.stdin, 'isatty', return_value=True),
              patch.object(setup.getpass, 'getpass', return_value=TEST_TOKEN),
              patch.object(setup, 'ConsoleLock', return_value=contextlib.nullcontext()),
              patch.object(setup, 'Client', return_value=client),
              patch.object(setup, 'configure', side_effect=lambda *args, **kwargs: events.append(('configure',))) as configure,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            self.assertEqual(0, setup.main(['--check-only']))
        self.assertEqual([('verify', setup.VERIFY_PATH), ('configure',)], events)
        client.get.assert_called_once_with(setup.VERIFY_PATH)
        configure.assert_called_once_with(client, check_only=True)
        client.call.assert_not_called()
        client.items.assert_not_called()
        self.assertNotIn(TEST_TOKEN, output.getvalue() + errors.getvalue())


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cloudflare-token-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_existing_different_token_is_never_overwritten(self):
        target = self.root / 'token.txt'
        target.write_text('different-fixture-token')
        with self.assertRaises(setup.SetupError):
            setup.secure_token_file(target, TEST_TOKEN)
        self.assertEqual('different-fixture-token', target.read_text())

    def test_permission_failure_does_not_leave_new_empty_credential_file(self):
        target = self.root / 'token.txt'
        with patch.object(setup, 'restrict_token_file', side_effect=setup.SetupError('isolated ACL failure')):
            with self.assertRaises(setup.SetupError):
                setup.secure_token_file(target, TEST_TOKEN)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_private_acl_is_installed_while_temporary_file_is_empty_before_publication(self):
        target = self.root / 'token.txt'
        order = []
        original_restrict, original_link = setup.restrict_token_file, os.link

        def restrict(path):
            self.assertFalse(target.exists())
            self.assertEqual('', path.read_text(encoding='utf-8'))
            original_restrict(path)
            order.append('restricted-empty-file')

        def publish(source, destination):
            self.assertEqual(['restricted-empty-file'], order)
            self.assertEqual(TEST_TOKEN, source.read_text(encoding='utf-8'))
            self.assertFalse(target.exists())
            original_link(source, destination)
            order.append('published')

        with (patch.object(setup, 'restrict_token_file', side_effect=restrict),
              patch.object(setup.os, 'link', side_effect=publish)):
            setup.secure_token_file(target, TEST_TOKEN)
        self.assertEqual(['restricted-empty-file', 'published'], order)
        self.assertEqual(TEST_TOKEN, target.read_text())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_concurrent_credential_creation_is_never_overwritten(self):
        target = self.root / 'token.txt'
        original_link = os.link

        def publish(source, destination):
            target.write_text('concurrent-fixture-token', encoding='utf-8')
            return original_link(source, destination)

        with patch.object(setup.os, 'link', side_effect=publish):
            with self.assertRaises(FileExistsError):
                setup.secure_token_file(target, TEST_TOKEN)
        self.assertEqual('concurrent-fixture-token', target.read_text())
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_hard_link_is_rejected_without_changing_original(self):
        original = self.root / 'original.txt'
        original.write_text('different-fixture-token')
        target = self.root / 'linked.txt'
        try:
            os.link(original, target)
        except OSError as error:
            self.skipTest(f'hard links unsupported in temporary filesystem: {error}')
        with self.assertRaises(ValueError):
            setup.secure_token_file(target, TEST_TOKEN)
        self.assertEqual('different-fixture-token', original.read_text())

    def test_new_credential_file_has_private_permissions_and_roundtrips(self):
        target = self.root / 'token.txt'
        setup.secure_token_file(target, TEST_TOKEN)
        self.assertEqual(TEST_TOKEN, target.read_text(encoding='utf-8'))
        if os.name == 'nt':
            # Read actual Windows ACL metadata, never file contents or real credentials.
            encoded_path = base64.b64encode(str(target).encode('utf-8')).decode('ascii')
            command = ('[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); '
                       f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}')); "
                       '$acl = [IO.File]::GetAccessControl($p); '
                       '$rules = $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]); '
                       '@{protected=$acl.AreAccessRulesProtected; '
                       'inherited=@($rules | Where-Object IsInherited).Count; '
                       'allow=@($rules | Where-Object AccessControlType -eq Allow).Count} '
                       '| ConvertTo-Json -Compress')
            encoded_command = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
            result = subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', encoded_command],
                                    capture_output=True, text=True, check=True)
            permissions = json.loads(result.stdout)
            self.assertTrue(permissions['protected'])
            self.assertEqual(0, permissions['inherited'])
            self.assertEqual(1, permissions['allow'])
        else:
            self.assertEqual(0o600, target.stat().st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
