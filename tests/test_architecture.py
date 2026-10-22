"""Architecture boundaries and real CLI boot from an unrelated working directory."""
import ast
import csv
import importlib.util
import io
import json
from http.client import HTTPConnection
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def modules():
    result = {}
    for path in (ROOT/'recruitment_insights').rglob('*.py'):
        parts = path.relative_to(ROOT).with_suffix('').parts
        name = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        result[name] = (path, ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path)))
    return result


def imports(name, path, tree):
    package = name if path.name == '__init__.py' else name.rsplit('.', 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                base = importlib.util.resolve_name('.'*node.level+base, package)
            yield base
            for alias in node.names:
                if alias.name != '*':
                    yield base+'.'+alias.name


class ArchitectureTests(unittest.TestCase):
    def test_domain_application_and_web_respect_dependency_direction(self):
        disallowed = []
        for name, (path, tree) in modules().items():
            layer = name.split('.')[1] if '.' in name else ''
            if layer not in ('domain', 'application', 'web'):
                continue
            allowed_layers = {'domain': {'domain'}, 'application': {'domain', 'application'},
                              'web': {'domain', 'application', 'web'}}[layer]
            for dependency in imports(name, path, tree):
                if dependency.startswith('recruitment_insights.'):
                    target_layer = dependency.split('.')[1]
                    if target_layer not in allowed_layers and not (layer != 'domain' and target_layer == 'config'):
                        disallowed.append((name, dependency))
                elif dependency.split('.')[0] not in sys.stdlib_module_names:
                    disallowed.append((name, dependency))
                if layer in ('domain', 'application') and dependency.split('.')[0] in {'sqlite3', 'http', 'urllib', 'socket'}:
                    disallowed.append((name, dependency))
        self.assertEqual(disallowed, [], 'Domain is pure; application uses ports; web uses application services.')

    def test_package_import_graph_has_no_cycles(self):
        source = modules()
        graph = {}
        for name, (path, tree) in source.items():
            graph[name] = {dependency for dependency in imports(name, path, tree)
                           if dependency in source and dependency != name}
        complete, active = set(), []
        def visit(name):
            if name in active:
                self.fail('Circular package dependency: '+' -> '.join(active+[name]))
            if name in complete:
                return
            active.append(name)
            for dependency in graph[name]:
                visit(dependency)
            active.pop()
            complete.add(name)
        for name in graph:
            visit(name)

    def test_http_transport_and_routes_do_not_execute_database_operations(self):
        violations = []
        for path in (ROOT/'recruitment_insights/web').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ('connect', 'execute', 'executemany', 'executescript', 'commit', 'rollback'):
                        violations.append((path.name, node.lineno, node.func.attr))
        self.assertEqual(violations, [])

    def test_root_imports_have_no_database_or_network_side_effects(self):
        script = '''
import json, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch('sqlite3.connect', side_effect=AssertionError('Import opened a database')), \\
     patch('urllib.request.urlopen', side_effect=AssertionError('Import accessed the network')):
    import app, core, accounts, reviews, requirements
    import recruitment_insights.config
    from recruitment_insights.web.server import make_server
    from recruitment_insights.domain.errors import ConflictError
    assert app.make_server is make_server
    assert reviews.ConflictError is ConflictError
    assert core.ROOT == recruitment_insights.config.ROOT
    print(json.dumps({'root': str(core.ROOT)}))
'''
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script, str(ROOT)],
                                     cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(Path(json.loads(process.stdout)['root']), ROOT)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_transport_keeps_request_limits_and_conflict_status_without_a_database(self):
        from recruitment_insights.domain.errors import ConflictError
        from recruitment_insights.web.server import make_server
        calls = []
        def conflicting_save(actor, body):
            calls.append(body)
            raise ConflictError('stale edit')
        store = SimpleNamespace(accounts=SimpleNamespace(user=lambda token: {'role': 'boss'}),
                                manage_job=conflicting_save)
        server = make_server(store, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def send(body=b'{}', changes=None):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            headers = {'Content-Type': 'application/json', 'X-Local-Request': '1', **(changes or {})}
            try:
                connection.request('POST', '/api/company/jobs', body=body, headers=headers)
                response = connection.getresponse()
                status, payload = response.status, response.read()
            finally:
                connection.close()
            return status, json.loads(payload)
        try:
            # Header rejection must happen before a body is uploaded. Sending an
            # unrelated short body with a huge declared length races an early
            # connection close on Windows and does not test the intended limit.
            self.assertEqual(send(body=b'', changes={'Host': 'untrusted.example'})[0], 403)
            self.assertEqual(send(body=b'', changes={'Origin': 'https://untrusted.example'})[0], 403)
            self.assertEqual(send(body=b'', changes={'X-Local-Request': ''})[0], 403)
            self.assertEqual(send(body=b'', changes={'Content-Type': 'text/plain'})[0], 415)
            self.assertEqual(send(body=b'', changes={'Content-Length': str(3*1024*1024+1)})[0], 413)
            self.assertEqual(send(body=b'not json')[0], 400)
            self.assertEqual(send(body=b'[]')[0], 400)
            self.assertEqual(calls, [])
            self.assertEqual(send(), (409, {'error': 'stale edit'}))
            self.assertEqual(calls, [{}])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_root_cli_serves_assets_and_temporary_database_from_other_cwd(self):
        with tempfile.TemporaryDirectory(prefix='recruitment_insights-cli-') as directory:
            database = Path(directory)/'isolated.sqlite3'
            process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(ROOT/'app.py'),
                                        '--db', str(database), '--port', '0'], cwd=directory,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8')
            lines = queue.Queue()
            def collect():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)
            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            try:
                try:
                    ready = lines.get(timeout=15)
                except queue.Empty:
                    self.fail('CLI did not announce a ready address within 15 seconds')
                if ready is None:
                    self.fail('CLI exited before readiness: '+process.stderr.read())
                match = re.search(r'READY (http://127\.0\.0\.1:\d+) \| database:', ready)
                self.assertIsNotNone(match, ready)
                self.assertIn(str(database), ready)
                base = match[1]
                with urlopen(base+'/api/health', timeout=5) as response:
                    self.assertEqual(json.load(response)['version'], '2.5.0')
                with urlopen(base+'/', timeout=5) as response:
                    self.assertIn('recruitment-insights', response.read().decode())
                    self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                for asset in ('app.js', 'login.js', 'style.css'):
                    with urlopen(base+'/'+asset, timeout=5) as response:
                        self.assertGreater(len(response.read()), 100)
                with self.assertRaises(HTTPError) as error:
                    urlopen(base+'/api/jobs', timeout=5)
                self.assertEqual(error.exception.code, 401)
                headers = {'Content-Type': 'application/json', 'X-Local-Request': '1'}
                account = {'username': 'cli_candidate', 'password': 'CLI-temporary-password', 'display_name': 'CLI 临时用户'}
                with urlopen(Request(base+'/api/auth/register', data=json.dumps(account).encode(), headers=headers), timeout=5) as response:
                    self.assertEqual(response.status, 201)
                with urlopen(Request(base+'/api/auth/login', data=json.dumps(account).encode(), headers=headers), timeout=5) as response:
                    cookie = response.headers['Set-Cookie'].split(';')[0]
                    self.assertIn('HttpOnly', response.headers['Set-Cookie'])
                    self.assertEqual(json.load(response)['role'], 'candidate')
                with urlopen(Request(base+'/api/stats', headers={'Cookie': cookie}), timeout=5) as response:
                    self.assertEqual(json.load(response)['total'], 0)
                with urlopen(Request(base+'/api/export', headers={'Cookie': cookie}), timeout=5) as response:
                    rows = csv.DictReader(io.StringIO(response.read().decode('utf-8-sig')))
                    self.assertIn('salary_monthly_min', rows.fieldnames)
                    self.assertEqual(list(rows), [])
                self.assertTrue(database.is_file())
                self.assertFalse((Path(directory)/'data').exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=5)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
