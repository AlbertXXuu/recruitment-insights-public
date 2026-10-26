"""Account registration and enterprise approval as atomic, authorized use cases."""
import re
import time
from .accounts import Accounts
from .identity import live_actor
from .ports import UnitOfWork
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.domain.organizations import organization_key


class Onboarding:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow

    @staticmethod
    def values(body):
        if not isinstance(body, dict):
            raise ValueError('申请内容必须为对象')
        values = {}
        for key, label, minimum, maximum in (
            ('company_name','企业名称',2,120), ('contact_name','联系人',1,60),
            ('contact','联系方式',3,120), ('website','公开介绍链接',0,500), ('description','申请说明',10,1000)):
            value = body.get(key, '')
            if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
                raise ValueError(f'{label}须为 {minimum}–{maximum} 个字符')
            values[key] = value.strip()
        if values['website'] and not re.fullmatch(r'https?://[^\s<>"\x00-\x1f]+', values['website']):
            raise ValueError('公开介绍链接须以 http:// 或 https:// 开头')
        if body.get('dedicated_account') is not True:
            raise ValueError('请确认使用独立企业工作账号；审核通过后该账号用于企业端')
        values['name_key'] = organization_key(values['company_name'])
        return values

    @staticmethod
    def check_version(application, body):
        if not application:
            raise ValueError('申请不存在')
        version = body.get('version')
        if type(version) is not int or version != application['version']:
            raise ConflictError('申请已发生变化，请刷新后重新查看')

    @staticmethod
    def require_admin(actor):
        if actor['role'] != 'admin':
            raise PermissionError('仅平台管理员可以审核企业申请')

    @staticmethod
    def record(tx, application_id, actor, stamp):
        row = tx.onboarding.get(application_id)
        tx.onboarding.event(row, actor['id'], stamp)
        tx.audit.append(actor, 'enterprise_'+row['status'], application_id, {'version': row['version']})
        return row

    def register(self, body):
        values = self.values(body)
        user = Accounts._user_values(body.get('username'), body.get('password'), body.get('display_name'), 'candidate', None, False)
        with self.uow(write=True) as tx:
            if tx.onboarding.name_taken(values['name_key']):
                raise ConflictError('该企业已有记录或待审核申请；加入已有企业请联系企业负责人')
            user_id = Accounts._create(tx, user)
            stamp = time.time()
            application_id = tx.onboarding.create(user_id, values, stamp)
            self.record(tx, application_id, tx.accounts.get(user_id), stamp)
            return application_id

    def mine(self, actor):
        with self.uow() as tx:
            current = live_actor(tx, actor)
            row = tx.onboarding.for_user(current['id'])
            if row:
                row['history'] = tx.onboarding.history(row['id'])
            return row

    def queue(self, actor, page=1):
        if type(page) is not int or not 1 <= page <= 100000:
            raise ValueError('页码无效')
        with self.uow() as tx:
            self.require_admin(live_actor(tx, actor))
            return {'items': tx.onboarding.list((page-1)*20), 'total': tx.onboarding.count(), 'page': page}

    def detail(self, actor, application_id):
        with self.uow() as tx:
            self.require_admin(live_actor(tx, actor))
            row = tx.onboarding.get(application_id)
            if not row:
                raise ValueError('申请不存在')
            row['history'] = tx.onboarding.history(application_id)
            return row

    def change(self, actor, body):
        action = body.get('action')
        if action not in ('withdraw', 'resubmit'):
            raise ValueError('申请操作无效')
        values = self.values(body) if action == 'resubmit' else None
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            row = tx.onboarding.for_user(current['id'])
            self.check_version(row, body)
            if current['role'] != 'candidate' or current['org_id'] is not None:
                raise PermissionError('当前账号不能修改入驻申请')
            stamp = time.time()
            if action == 'withdraw':
                if row['status'] != 'pending':
                    raise ConflictError('只有待审核申请可以撤回')
                tx.onboarding.decide(row['id'], 'withdrawn', None, '', None, stamp)
            else:
                if row['status'] not in ('rejected', 'withdrawn'):
                    raise ConflictError('请先撤回待审核申请，再修改并重新提交')
                if tx.onboarding.name_taken(values['name_key'], row['id']):
                    raise ConflictError('企业已有记录或待审核申请，请联系管理员')
                tx.onboarding.resubmit(row['id'], values, stamp)
            return self.record(tx, row['id'], current, stamp)

    def review(self, actor, body):
        application_id, decision = body.get('application_id'), body.get('decision')
        if type(application_id) is not int or decision not in ('approved', 'rejected'):
            raise ValueError('审核操作无效')
        reason = body.get('reason', '')
        if not isinstance(reason, str) or len(reason.strip()) > 1000 or (decision == 'rejected' and not reason.strip()):
            raise ValueError('驳回必须填写原因，审核说明最多 1000 字符')
        with self.uow(write=True) as tx:
            reviewer = live_actor(tx, actor)
            self.require_admin(reviewer)
            row = tx.onboarding.get(application_id)
            self.check_version(row, body)
            if row['status'] != 'pending':
                raise ConflictError('该申请已处理，请刷新列表')
            applicant = tx.accounts.get(row['applicant_id'])
            if not applicant or applicant['role'] != 'candidate' or applicant['org_id'] is not None:
                raise ConflictError('申请人身份已变化，不能继续审批')
            org_id = None
            if decision == 'approved':
                if tx.onboarding.has_personal_data(applicant['id']):
                    raise ConflictError('该账号已有个人简历或收藏，请驳回并要求使用独立企业工作账号')
                if tx.onboarding.name_taken(row['name_key'], row['id']):
                    raise ConflictError('企业名称已存在，请核对；不会自动加入已有企业')
                org_id = tx.onboarding.create_organization(row['company_name'])
                tx.onboarding.promote(applicant['id'], org_id)
            stamp = time.time()
            tx.onboarding.decide(application_id, decision, reviewer['id'], reason.strip(), org_id, stamp)
            return self.record(tx, application_id, reviewer, stamp)
