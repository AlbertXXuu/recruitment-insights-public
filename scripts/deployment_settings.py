"""Operator-owned deployment identity. Defaults are documentation placeholders."""
import os

ZONE = os.environ.get('RECRUITMENT_CF_ZONE', 'example.com')
HOSTNAME = os.environ.get('RECRUITMENT_CF_HOSTNAME', 'recruitment-insights.' + ZONE)
ACCOUNT = os.environ.get('RECRUITMENT_CF_ACCOUNT', '00000000000000000000000000000000')
TUNNEL_ID = os.environ.get('RECRUITMENT_CF_TUNNEL_ID', '11111111-1111-4111-8111-111111111111')
ORIGIN = 'https://' + HOSTNAME
