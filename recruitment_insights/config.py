"""Deployment paths, independent of the process working directory."""
from pathlib import Path
import hashlib
import re
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
VERSION = '2.5.0'
PROJECT_NAME = 'recruitment-insights'
SESSION_COOKIE = 'recruitment_insights_session'
LEGACY_SESSION_COOKIE = 'joblens_session'
INVITE_SESSION_COOKIE = '__Host-recruitment_insights_test_session'
DEMO_PASSWORD = 'JobLens-Demo-2026!'
INSTANCE_ID = hashlib.sha256(str(ROOT).lower().encode('utf-8')).hexdigest()[:16]


@dataclass(frozen=True)
class WebPolicy:
    """Fixed external HTTPS contract; forwarded headers never select a policy."""
    test_origin: str | None = None
    allow_candidate_registration: bool = False

    def __post_init__(self):
        if not isinstance(self.allow_candidate_registration, bool):
            raise ValueError('普通端注册开关必须为布尔值')
        if self.test_origin is None:
            return
        label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
        suffix = r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?'
        if (not isinstance(self.test_origin, str) or
                not re.fullmatch(r'https://(?:' + label + r'\.)+' + suffix, self.test_origin)):
            raise ValueError('测试来源须为 https://小写精确域名，不带端口、路径或尾斜线')
        host = self.test_origin[8:]
        if len(host) > 253 or host.endswith('.localhost'):
            raise ValueError('测试来源须为完整外部域名')

    @property
    def invited(self):
        return self.test_origin is not None

    @property
    def registration_enabled(self):
        """Keep local registration; external HTTPS requires a server-side opt-in."""
        return not self.invited or self.allow_candidate_registration

    @property
    def cookie_name(self):
        return INVITE_SESSION_COOKIE if self.invited else SESSION_COOKIE

    def cookies(self, token=''):
        attributes = '; Path=/; HttpOnly; SameSite=Strict'
        if self.invited:
            attributes += '; Secure'
        current = f'{self.cookie_name}={token}{attributes}; Max-Age={28800 if token else 0}'
        if self.invited:
            return (current,)
        return (current, f'{LEGACY_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
