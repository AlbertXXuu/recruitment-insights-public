"""Enterprise applications; the caller owns each transaction."""
import json
import sqlite3
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.domain.organizations import organization_key


class OnboardingRepository:
    def __init__(self, connection):
        self.db = connection

    def get(self, application_id):
        row = self.db.execute('SELECT * FROM enterprise_applications WHERE id=?', (application_id,)).fetchone()
        return dict(row) if row else None

    def for_user(self, user_id):
        row = self.db.execute('SELECT * FROM enterprise_applications WHERE applicant_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def list(self, offset):
        rows = self.db.execute('''SELECT a.*,u.username,u.display_name FROM enterprise_applications a
            JOIN users u ON u.id=a.applicant_id ORDER BY (a.status='pending') DESC,a.updated_at DESC,a.id DESC
            LIMIT 20 OFFSET ?''', (offset,))
        return [dict(r) for r in rows]

    def count(self):
        return self.db.execute('SELECT count(*) FROM enterprise_applications').fetchone()[0]

    def name_taken(self, key, exclude=None):
        if any(organization_key(r['name']) == key for r in self.db.execute('SELECT name FROM organizations')):
            return True
        return self.db.execute('''SELECT 1 FROM enterprise_applications WHERE name_key=?
            AND status IN ('pending','approved') AND (? IS NULL OR id<>?)''', (key, exclude, exclude)).fetchone() is not None

    def create(self, user_id, values, stamp):
        return self.db.execute('''INSERT INTO enterprise_applications
            (applicant_id,company_name,name_key,contact_name,contact,website,description,status,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'pending',1,?,?)''',
            (user_id, *(values[k] for k in ('company_name','name_key','contact_name','contact','website','description')), stamp, stamp)).lastrowid

    def resubmit(self, application_id, values, stamp):
        self.db.execute('''UPDATE enterprise_applications SET company_name=?,name_key=?,contact_name=?,contact=?,
            website=?,description=?,status='pending',version=version+1,reviewer_id=NULL,review_reason='',updated_at=? WHERE id=?''',
            (*(values[k] for k in ('company_name','name_key','contact_name','contact','website','description')), stamp, application_id))

    def decide(self, application_id, status, reviewer, reason, org_id, stamp):
        self.db.execute('''UPDATE enterprise_applications SET status=?,reviewer_id=?,review_reason=?,org_id=?,
            version=version+1,updated_at=? WHERE id=?''', (status, reviewer, reason, org_id, stamp, application_id))

    def create_organization(self, name):
        try:
            return self.db.execute('INSERT INTO organizations(name) VALUES(?)', (name,)).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ConflictError('企业名称已存在，请重新核对申请') from exc

    def promote(self, user_id, org_id):
        self.db.execute("UPDATE users SET role='boss',org_id=? WHERE id=? AND role='candidate' AND org_id IS NULL", (org_id, user_id))

    def has_personal_data(self, user_id):
        return any(self.db.execute('SELECT 1 FROM '+table+' WHERE user_id=? LIMIT 1', (user_id,)).fetchone()
                   for table in ('favorites', 'resumes'))

    def event(self, application, actor_id, stamp):
        self.db.execute('''INSERT INTO enterprise_application_events(application_id,actor_id,created_at,snapshot)
            VALUES(?,?,?,?)''', (application['id'], actor_id, stamp, json.dumps(application, ensure_ascii=False)))

    def history(self, application_id):
        return [dict(r) for r in self.db.execute('''SELECT actor_id,created_at,snapshot FROM enterprise_application_events
            WHERE application_id=? ORDER BY id''', (application_id,))]
