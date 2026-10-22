"""Exercise exact font routes without opening a database or reading private files."""
import unittest
from unittest.mock import patch

from recruitment_insights.web.routes import Routes
from scripts.verify_delivery_zip import SOFTWARE_FILES


class UnauthenticatedAccounts:
    def user(self, token):
        return None


class FontRouteTests(unittest.TestCase):
    def setUp(self):
        self.routes = Routes(type('FixtureStore', (), {'accounts': UnauthenticatedAccounts()})())

    def test_official_font_routes_return_woff2_with_public_versioned_cache(self):
        for weight in ('Regular', 'Medium', 'Semibold'):
            with self.subTest(weight=weight):
                response = self.routes.get(f'/fonts/MiSans-{weight}.woff2', 'v=4.009', None)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, 'font/woff2')
                self.assertEqual(response.data[:4], b'wOF2')
                self.assertIn('immutable', response.headers['Cache-Control'])

    def test_font_prefix_is_not_a_general_file_server(self):
        for path in ('/fonts/../config.py', '/fonts/MiSans-font-license.pdf',
                     '/fonts/../../outputs/invitation-test/tunnel-token.txt',
                     '/fonts/missing.woff2', '/fonts/%2e%2e/config.py'):
            with self.subTest(path=path), patch('pathlib.Path.read_bytes') as read:
                response = self.routes.get(path, '', None)
                self.assertEqual(response.status, 404)
                read.assert_not_called()

    def test_business_and_script_responses_do_not_gain_public_font_cache(self):
        script = self.routes.get('/app.js', '', None)
        anonymous = self.routes.get('/api/auth/me', '', None)
        self.assertEqual(anonymous.status, 401)
        self.assertNotIn('Cache-Control', script.headers)
        self.assertNotIn('Cache-Control', anonymous.headers)

    def test_delivery_manifest_includes_every_served_font_and_its_license(self):
        for weight in ('Regular', 'Medium', 'Semibold'):
            self.assertIn(f'static/fonts/MiSans-{weight}.woff2', SOFTWARE_FILES)
        self.assertIn('static/fonts/MiSans-font-license.pdf', SOFTWARE_FILES)
        self.assertIn('static/fonts/README.md', SOFTWARE_FILES)

    def test_delivery_manifest_includes_all_runtime_python_modules(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        missing = [str(path.relative_to(root)).replace('\\', '/')
                   for path in (root/'recruitment_insights').rglob('*.py')
                   if str(path.relative_to(root)).replace('\\', '/') not in SOFTWARE_FILES]
        self.assertEqual(missing, [], 'The next delivery must include every runtime module')


if __name__ == '__main__':
    unittest.main()
