"""Independent operator identities keep the existing bounded deployment contract."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DeploymentSettingsTests(unittest.TestCase):
    def test_custom_identity_is_shared_and_zone_bounds_remain_enforced(self):
        environment = dict(os.environ, RECRUITMENT_CF_ZONE='fixture.org',
                           RECRUITMENT_CF_HOSTNAME='jobs.fixture.org',
                           RECRUITMENT_CF_ACCOUNT='a'*32,
                           RECRUITMENT_CF_TUNNEL_ID='22222222-2222-4222-8222-222222222222')
        script = '''
from unittest.mock import Mock
from scripts import configure_cloudflare as setup, invitation_tunnel as tunnel, invitation_console as console
assert setup.HOSTNAME == tunnel.HOSTNAME == 'jobs.fixture.org'
assert console.TEST_ORIGIN == tunnel.ORIGIN == 'https://jobs.fixture.org'
assert setup.ACCOUNT == tunnel.PINNED_ACCOUNT_ID == 'a'*32
assert setup.TUNNEL_ID == tunnel.PINNED_TUNNEL_ID
client = setup.Client('synthetic-management-fixture')
client._opener = Mock()
try:
    client.call('GET', '/zones', name='another.org')
except setup.SetupError:
    pass
else:
    raise AssertionError('Out-of-zone call accepted')
client._opener.open.assert_not_called()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=environment,
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
