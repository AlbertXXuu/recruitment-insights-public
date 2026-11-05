"""Account use cases with authorization and writes inside one unit of work."""
import hashlib
import hmac
import re
import secrets
import time

from .identity import live_actor
from .ports import AccountContext
from recruitment_insights.domain.errors import ConflictError


class Accounts:
    def __init__(self, store: AccountContext):
        # Schema initialization belongs to the composition root, not a service.
        self.uow = store.uow
        self.queries = store.queries

    def organization(self, name):
        with self.uow(write=True) as tx:
            return tx.accounts.organization(name)

    @staticmethod
    def _user_values(username, password, display_name, role, org_id, can_manage):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', username):
            raise ValueError('账号须为 3–40 位英文字母、数字、下划线或横线')
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValueError('密码须为 10–128 个字符')
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 60:
            raise ValueError('显示名称须为 1–60 个字符')
        if role not in ('admin', 'boss', 'hr', 'candidate') or ((role in ('boss', 'hr')) != (org_id is not None)):
            raise ValueError('角色与企业归属不匹配')
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 240000).hex()
        return dict(username=username, display_name=display_name.strip(),
                    password_hash=salt + ':' + digest, role=role, org_id=org_id,
                    can_manage_jobs=int(can_manage))

    @staticmethod
    def _create(tx, values):
        if tx.accounts.by_username(values['username']):
            raise ValueError('账号已存在')
        return tx.accounts.create(values)

    def create_user(self, username, password, display_name, role='candidate', org_id=None, can_manage=False):
        """Create a registration or an explicitly provisioned local account.

        Public registration fixes role='candidate' in the HTTP adapter. Enterprise
        membership creation must use create_hr(), which checks the acting owner.
        """
        values = self._user_values(username, password, display_name, role, org_id, can_manage)
        with self.uow(write=True) as tx:
            return self._create(tx, values)

    @staticmethod
    def _require_boss(actor):
        if actor['role'] != 'boss' or actor['org_id'] is None:
            raise PermissionError('仅企业负责人可管理本企业 HR')

    def create_hr(self, actor, body):
        if not isinstance(body, dict):
            raise ValueError('企业成员信息必须为对象')
        allowed = body.get('can_manage', False)
        if not isinstance(allowed, bool):
            raise ValueError('授权状态必须为布尔值')
        with self.uow() as tx:
            owner = live_actor(tx, actor)
            self._require_boss(owner)
        # Password work holds no snapshot or write lock. Recheck its authority
        # and organization before committing the prepared account and audit.
        values = self._user_values(body.get('username'), body.get('password'),
                                   body.get('display_name'), 'hr', owner['org_id'], allowed)
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            self._require_boss(current)
            if current['org_id'] != owner['org_id']:
                raise PermissionError('企业归属已变化，请刷新后重试')
            user_id = self._create(tx, values)
            self.audit(tx, current, 'create_hr', user_id)
            return user_id

    def public_user(self, row):
        return {key: row[key] for key in (
            'id', 'username', 'display_name', 'role', 'org_id', 'org_name', 'can_manage_jobs')}

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise ValueError('账号或密码错误')
        with self.uow() as tx:
            row = tx.accounts.by_username(username)
        salt, expected = row['password_hash'].split(':') if row else ('0' * 32, '0' * 64)
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 240000).hex()
        if not row or not row['active'] or not hmac.compare_digest(actual, expected):
            raise ValueError('账号或密码错误')
        with self.uow(write=True) as tx:
            # A successful calculation only validates the captured credentials.
            # A deleted/replaced user or changed password must not get a session.
            current = tx.accounts.by_username(username)
            if (not current or current['id'] != row['id'] or
                    not current['active'] or current['password_hash'] != row['password_hash']):
                raise ValueError('账号或密码错误')
            token = secrets.token_urlsafe(32)
            timestamp = time.time()
            tx.accounts.expire_sessions(timestamp)
            tx.accounts.create_session(self.token_hash(token), current['id'], timestamp + 8 * 3600)
            return token, self.public_user(current)

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def user(self, token):
        if not token:
            return None
        with self.uow() as tx:
            row = tx.accounts.session_user(self.token_hash(token), time.time())
            return self.public_user(row) if row else None

    def logout(self, token):
        with self.uow(write=True) as tx:
            tx.accounts.delete_session(self.token_hash(token))

    def members(self, actor):
        with self.uow() as tx:
            current = live_actor(tx, actor)
            self._require_boss(current)
            return tx.accounts.members(current['org_id'])

    def grant(self, actor, user_id, allowed):
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            self._require_boss(current)
            if not isinstance(allowed, bool):
                raise PermissionError('仅企业负责人可授权本企业 HR')
            if not tx.accounts.grant(user_id, current['org_id'], allowed):
                raise PermissionError('无法授权该账号')
            self.audit(tx, current, 'grant_hr', str(user_id))

    @staticmethod
    def _require_admin(actor):
        if actor['role'] != 'admin':
            raise PermissionError('仅管理员可管理全部账号')

    @staticmethod
    def _management_target(tx, body):
        if not isinstance(body, dict) or type(body.get('id')) is not int or body['id'] <= 0:
            raise ValueError('账号 ID 无效')
        if type(body.get('version')) is not int or body['version'] <= 0:
            raise ValueError('账号版本无效')
        target = tx.accounts.get(body['id'])
        if not target:
            raise ValueError('账号不存在')
        if target['account_version'] != body['version']:
            raise ConflictError('账号已发生变化，请刷新后重试')
        return target

    def management_list(self, actor, query='', role='', status='', page=1):
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError('搜索内容最多 100 字符')
        if role not in ('', 'admin', 'boss', 'hr', 'candidate') or status not in ('', 'active', 'inactive'):
            raise ValueError('角色或账号状态筛选无效')
        if type(page) is not int or not 1 <= page <= 100000:
            raise ValueError('页码无效')
        with self.uow() as tx:
            self._require_admin(live_actor(tx, actor))
            items, total = tx.accounts.management_list(query.strip(), role, status, (page-1)*20)
            return {'items': items, 'total': total, 'page': page}

    def management_edit(self, actor, body):
        if not isinstance(body, dict) or set(body) - {'id', 'version', 'display_name', 'can_manage_jobs'}:
            raise ValueError('不支持的账号修改字段')
        name = body.get('display_name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError('显示名称须为 1–60 个字符')
        with self.uow(write=True) as tx:
            admin = live_actor(tx, actor)
            self._require_admin(admin)
            target = self._management_target(tx, body)
            allowed = body.get('can_manage_jobs', bool(target['can_manage_jobs']))
            if not isinstance(allowed, bool) or ('can_manage_jobs' in body and target['role'] != 'hr'):
                raise ValueError('仅 HR 可以修改岗位维护权限')
            if not target['active'] and allowed:
                raise ValueError('停用账号不能授予维护权限')
            tx.accounts.management_edit(target['id'], name.strip(), allowed)
            self.audit(tx, admin, 'account_edit', target['id'], {'role': target['role'], 'hr_manage': allowed})

    def management_status(self, actor, body, *, boss_removal=False):
        if not isinstance(body, dict) or set(body) - {'id', 'version', 'active'}:
            raise ValueError('不支持的账号状态字段')
        active = body.get('active')
        if type(active) is not bool:
            raise ValueError('账号状态必须为布尔值')
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            if boss_removal:
                self._require_boss(current)
            else:
                self._require_admin(current)
            target = self._management_target(tx, body)
            if boss_removal:
                if active or target['role'] != 'hr' or target['org_id'] != current['org_id']:
                    raise PermissionError('仅能停用本企业 HR')
            if not active and target['id'] == current['id']:
                raise ValueError('不能停用当前账号')
            if not active and target['role'] == 'boss':
                raise ValueError('企业负责人需先完成交接；当前暂不支持停用负责人')
            if not active and target['role'] == 'admin' and tx.accounts.active_admin_count() <= 1:
                raise ValueError('不能停用最后一名有效管理员')
            if bool(target['active']) == active:
                raise ConflictError('账号状态已变化，请刷新后重试')
            tx.accounts.management_status(target['id'], active, target['role'] == 'hr')
            self.audit(tx, current, 'account_restore' if active else 'account_deactivate',
                       target['id'], {'role': target['role'], 'org_id': target['org_id']})

    def management_delete(self, actor, body):
        if not isinstance(body, dict) or set(body) != {'id', 'version', 'confirm_username'}:
            raise ValueError('永久删除参数无效')
        if not isinstance(body['confirm_username'], str):
            raise ValueError('请输入账号名以确认删除')
        with self.uow(write=True) as tx:
            admin = live_actor(tx, actor)
            self._require_admin(admin)
            target = self._management_target(tx, body)
            if target['id'] == admin['id']:
                raise ValueError('不能删除当前账号')
            if target['role'] == 'boss':
                raise ValueError('企业负责人需要先完成交接，当前不能永久删除')
            if target['active']:
                raise ValueError('请先停用账号，再永久删除')
            if target['username'] != body['confirm_username']:
                raise ValueError('确认账号名不一致，请重新输入')
            blockers = tx.accounts.deletion_blockers(target['id'])
            if blockers:
                raise ValueError('该账号关联共享历史（' + '、'.join(blockers) + '），暂不能永久删除')
            tx.accounts.delete_account(target['id'])
            self.audit(tx, admin, 'account_delete', target['id'], {'role': target['role'], 'org_id': target['org_id']})

    @staticmethod
    def audit(db_or_tx, actor, action, target, details=None):
        # Both a transaction and the legacy Store.connect() adapter expose .audit.
        # This keeps the service independent of SQLite and preserves one patch point.
        db_or_tx.audit.append(actor, action, str(target), details)

    @staticmethod
    def _require_candidate(actor):
        if actor['role'] != 'candidate':
            raise PermissionError('收藏与个人备注属于普通端')

    def favorites(self, actor):
        with self.uow() as tx:
            current = live_actor(tx, actor)
            self._require_candidate(current)
            saved = tx.accounts.favorites(current['id'])
            jobs = self.queries.jobs_in(tx, {'dataset': 'all'}, current)
            return [dict(job, note=saved[job['id']]) for job in jobs if job['id'] in saved]

    def save_favorite(self, actor, job_id, note=None, remove=False):
        if note is not None and (not isinstance(note, str) or len(note) > 3000):
            raise ValueError('备注最多 3000 字符')
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            self._require_candidate(current)
            if not remove and not self.queries.detail_in(tx, job_id, current):
                raise PermissionError('岗位不可访问')
            tx.accounts.save_favorite(current['id'], job_id, note, remove)
