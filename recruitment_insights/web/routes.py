"""HTTP routes translate requests to application calls, without database access."""
from dataclasses import dataclass, field
import logging
import re
from urllib.parse import parse_qs, quote

from recruitment_insights.config import ROOT, VERSION, INSTANCE_ID, PROJECT_NAME, DEMO_PASSWORD, WebPolicy
from recruitment_insights.domain.requirements import EDUCATION_VALUES, EXPERIENCE_VALUES, CONDITIONS
from recruitment_insights.web.login_throttle import LoginThrottle
from recruitment_insights.web.registration_throttle import RegistrationThrottle

LOG = logging.getLogger('recruitment')
STATIC = {name: (name.lstrip('/'), mime) for name, mime in (
    ('/app.js', 'application/javascript; charset=utf-8'),
    ('/login.js', 'application/javascript; charset=utf-8'),
    ('/onboarding.js', 'application/javascript; charset=utf-8'),
    ('/onboarding.css', 'text/css; charset=utf-8'),
    ('/style.css', 'text/css; charset=utf-8'))}
FONT_FILES = {f'/fonts/MiSans-{weight}.woff2': f'MiSans-{weight}.woff2'
              for weight in ('Regular', 'Medium', 'Semibold')}


@dataclass
class Response:
    status: int
    data: object
    content_type: str = 'application/json; charset=utf-8'
    download: str | None = None
    headers: dict = field(default_factory=dict)


def require_role(actor, *roles):
    if actor['role'] not in roles:
        raise PermissionError('当前账号没有此操作权限')


def query_params(query):
    params = {key: values[0] for key, values in parse_qs(query).items()}
    if params.get('dataset', 'real') not in ('real', 'demo'):
        raise ValueError('数据范围无效')
    if params.get('scope', 'all') not in ('all', 'public', 'company'):
        raise ValueError('企业范围无效')
    return params


class Routes:
    def __init__(self, store, policy=None):
        self.store = store
        self.accounts = store.accounts
        self.policy = WebPolicy() if policy is None else policy
        self.login_throttle = LoginThrottle() if self.policy.invited else None
        self.registration_throttle = (RegistrationThrottle()
            if self.policy.invited and self.policy.registration_enabled else None)

    def login_page(self):
        page = (ROOT/'static/login.html').read_text(encoding='utf-8')
        if self.policy.registration_enabled:
            page = page.replace('<!-- enterprise-registration-entry -->',
                '<a id="enterprise-register" class="quiet" href="/join">企业入驻：申请工作账号</a>')
        if not self.policy.registration_enabled:
            page = page.replace('<body class="login-page">', '<body class="login-page" data-registration="disabled">')
            page = page.replace('<button id="switch-auth" class="quiet" type="button">',
                                '<button id="switch-auth" class="quiet" type="button" hidden>')
        if self.policy.invited:
            start = page.index('    <details class="demo-accounts">')
            end = page.index('</details>', start) + len('</details>')
            guidance = ('可注册普通端账号；企业账号申请入驻后由管理员审核开通。'
                        if self.policy.registration_enabled else '请使用收到的邀请账号登录。')
            page = page[:start] + '    <p>' + guidance + '</p>' + page[end:]
        return page.encode('utf-8')

    def get(self, path, query, token):
        store, accounts = self.store, self.accounts
        if path in FONT_FILES:
            return Response(200, (ROOT/'static/fonts'/FONT_FILES[path]).read_bytes(), 'font/woff2',
                            headers={'Cache-Control': 'public, max-age=31536000, immutable'})
        if path in STATIC:
            name, mime = STATIC[path]
            return Response(200, (ROOT/'static'/name).read_bytes(), mime)
        if path == '/favicon.ico':
            return Response(204, b'', 'image/x-icon')
        if path == '/api/health':
            return Response(200, dict(status='ok', version=VERSION, service=PROJECT_NAME, instance_id=INSTANCE_ID))
        actor = accounts.user(token)
        if path in ('/join', '/enterprise-review'):
            if path == '/enterprise-review':
                if not actor:
                    return Response(303, b'', headers={'Location': '/login'})
                require_role(actor, 'admin')
            elif not actor and not self.policy.registration_enabled:
                return Response(403, {'error': '本实例暂未开放注册'})
            return Response(200, (ROOT/'static/onboarding.html').read_bytes(), 'text/html; charset=utf-8')
        if path in ('/', '/login', '/enterprise', '/public'):
            if path == '/login' or (not actor and path == '/'):
                return Response(200, self.login_page(), 'text/html; charset=utf-8')
            if not actor:
                return Response(303, b'', headers={'Location': '/login'})
            destination = '/public' if actor['role'] == 'candidate' else '/enterprise'
            application = store.onboarding.mine(actor) if actor['role'] == 'candidate' else None
            if application:
                return Response(303, b'', headers={'Location': '/join'})
            if path != destination:
                return Response(303, b'', headers={'Location': destination})
            page = (ROOT/'static/index.html').read_text(encoding='utf-8').replace('<!-- enterprise-review-entry -->',
                '<a class="nav" href="/enterprise-review" data-roles="admin" hidden>企业入驻审核</a>')
            return Response(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        if not path.startswith('/api/'):
            return Response(404, {'error': '页面或接口不存在'})
        if not actor:
            return Response(401, {'error': '请先登录'})
        if path == '/api/auth/me':
            return Response(200, actor)
        if path == '/api/enterprise-application':
            return Response(200, store.onboarding.mine(actor))
        if path == '/api/admin/enterprise-applications':
            params = parse_qs(query)
            return Response(200, store.onboarding.queue(actor, int(params.get('page', ['1'])[0])))
        if path.startswith('/api/admin/enterprise-applications/'):
            return Response(200, store.onboarding.detail(actor, int(path.rsplit('/', 1)[1])))
        if path == '/api/resume':
            require_role(actor, 'candidate')
            return Response(200, store.resumes.info(actor))
        if path == '/api/resume/download':
            require_role(actor, 'candidate')
            saved = store.resumes.download(actor)
            if saved is None:
                return Response(404, {'error': '尚未上传简历'})
            extension = '.pdf' if saved['media_type'] == 'application/pdf' else '.docx'
            disposition = ('attachment; filename="resume' + extension + '"; filename*=UTF-8\'\''
                           + quote(saved['filename'], safe=''))
            return Response(200, saved['content'], saved['media_type'],
                            headers={'Content-Disposition': disposition})
        params = query_params(query)
        if path == '/api/overview':
            include_options = params.get('include_options', '1')
            if include_options not in ('0', '1'):
                raise ValueError('筛选选项参数无效')
            return Response(200, store.queries.overview(params, actor, include_options == '1'))
        if path == '/api/jobs':
            return Response(200, store.queries.paginated(params, actor))
        if path == '/api/admin/jobs':
            require_role(actor, 'admin')
            return Response(200, store.admin_jobs(params, actor))
        if path.startswith('/api/jobs/'):
            job = store.detail(int(path.rsplit('/', 1)[1]), actor)
            return Response(200 if job else 404, job or {'error': '岗位不存在或不可访问'})
        if path == '/api/stats':
            return Response(200, store.queries.stats(params, actor))
        if path == '/api/options':
            return Response(200, store.queries.options(params, actor))
        if path == '/api/quality':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.quality(params, actor))
        if path == '/api/review-options':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, dict(education_values=EDUCATION_VALUES,
                                     experience_values=EXPERIENCE_VALUES, conditions=CONDITIONS))
        if path == '/api/company/jobs':
            require_role(actor, 'boss', 'hr')
            return Response(200, store.company_jobs(actor))
        if path == '/api/team':
            require_role(actor, 'boss')
            return Response(200, accounts.members(actor))
        if path == '/api/favorites':
            require_role(actor, 'candidate')
            return Response(200, accounts.favorites(actor))
        if path == '/api/runs':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.runs(actor))
        if path == '/api/audit':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.queries.audit(actor))
        if path == '/api/export':
            return Response(200, store.queries.export(params, actor), 'text/csv; charset=utf-8', 'jobs.csv')
        if path == '/api/template':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, (ROOT/'data/import_template.csv').read_bytes(),
                            'text/csv; charset=utf-8', 'import_template.csv')
        return Response(404, {'error': '接口不存在'})

    def post(self, path, body, token):
        store, accounts = self.store, self.accounts
        if path in ('/api/auth/register', '/api/auth/register-enterprise'):
            if not self.policy.registration_enabled:
                return Response(403, {'error': '本工作空间仅接受邀请账号'})
            if self.registration_throttle is not None:
                retry_after = self.registration_throttle.reserve()
                if retry_after:
                    return Response(429, {'error': f'注册请求较多，请在 {retry_after} 秒后重试'},
                                    headers={'Retry-After': str(retry_after)})
            if self.policy.invited and body.get('password') == DEMO_PASSWORD:
                raise ValueError('普通端账号不能使用公开演示密码')
            if path == '/api/auth/register-enterprise':
                if accounts.user(token):
                    raise ValueError('请先退出当前账号，再使用独立企业工作账号申请')
                store.onboarding.register(body)
                return Response(201, {'message': '企业申请已提交，请登录查看审核状态'})
            accounts.create_user(body.get('username'), body.get('password'), body.get('display_name'), role='candidate')
            return Response(201, {'message': '普通端账号已创建，请登录'})
        if path == '/api/auth/login':
            throttle, attempt, outcome = self.login_throttle, None, None
            if throttle is not None:
                username = body.get('username') if isinstance(body, dict) else None
                password = body.get('password') if isinstance(body, dict) else None
                if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', username)
                        or not isinstance(password, str) or len(password) > 128):
                    return Response(401, {'error': '账号或密码错误'})
                attempt, retry_after = throttle.begin(username)
                if attempt is None:
                    return Response(429, {'error': f'登录尝试过于频繁，请稍后（{retry_after} 秒后）重试'},
                                    headers={'Retry-After': str(retry_after)})
            try:
                new_token, actor = accounts.login(body.get('username'), body.get('password'))
            except ValueError as exc:
                if str(exc) == '账号或密码错误':
                    outcome = 'failure'
                return Response(401, {'error': '账号或密码错误' if throttle is not None else str(exc)})
            else:
                outcome = 'success'
            finally:
                if attempt is not None:
                    throttle.finish(attempt, outcome)
            if token:
                accounts.logout(token)
            return Response(200, actor, headers={'Set-Cookie': self.policy.cookies(new_token)})
        actor = accounts.user(token)
        if not actor:
            return Response(401, {'error': '请先登录'})
        if path == '/api/enterprise-application':
            return Response(200, store.onboarding.change(actor, body))
        if path == '/api/admin/enterprise-applications/review':
            return Response(200, store.onboarding.review(actor, body))
        if path == '/api/auth/logout':
            accounts.logout(token)
            return Response(200, {'message': '已退出'}, headers={
                'Set-Cookie': self.policy.cookies()})
        if path == '/api/resume':
            require_role(actor, 'candidate')
            return Response(200, store.resumes.upload(actor, body))
        if path == '/api/company/jobs':
            require_role(actor, 'boss', 'hr')
            return Response(200, store.manage_job(actor, body))
        if path == '/api/jobs/lifecycle':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.set_lifecycle(actor, body))
        if path == '/api/reviews':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.review_field(actor, body))
        if path == '/api/duplicates':
            require_role(actor, 'admin', 'boss', 'hr')
            return Response(200, store.decide_duplicate(actor, body))
        if path == '/api/team':
            require_role(actor, 'boss')
            if not isinstance(body.get('can_manage', False), bool):
                raise ValueError('授权状态必须为布尔值')
            if body.get('id'):
                accounts.grant(actor, int(body['id']), body.get('can_manage', False))
            else:
                if self.policy.invited and body.get('password') == DEMO_PASSWORD:
                    raise ValueError('邀请账号不能使用公开演示密码')
                accounts.create_hr(actor, body)
            return Response(200, {'message': '企业成员权限已保存'})
        if path == '/api/favorites':
            accounts.save_favorite(actor, int(body.get('job_id', 0)), body.get('note'), body.get('remove') is True)
            return Response(200, {'message': '收藏已更新'})
        if path == '/api/import':
            return Response(200, store.import_for(actor, body))
        if path == '/api/public-sample':
            require_role(actor, 'admin')
            return Response(200, store.load_public_sample_for(actor))
        if path in ('/api/demo', '/api/sync'):
            require_role(actor, 'admin')
            if path == '/api/demo':
                return Response(200, store.import_demo_for(actor, dict(
                    csv=(ROOT/'data/demo_jobs.csv').read_text(encoding='utf-8-sig'), source='课程模拟数据', is_demo=True)))
            try:
                return Response(200, store.sync_for(actor))
            except PermissionError:
                raise
            except ValueError:
                raise
            except Exception:
                LOG.exception('Source sync failed')
                return Response(502, {'error': '公开接口暂时不可用；已有数据已保留，失败详情见同步记录'})
        return Response(404, {'error': '接口不存在'})
