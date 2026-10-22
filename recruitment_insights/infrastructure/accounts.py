"""SQLite account and audit repositories; transaction ownership stays with the UOW."""
import json
import time


class AccountRepository:
    def __init__(self, connection):
        self.db = connection

    def organization(self, name):
        self.db.execute('INSERT OR IGNORE INTO organizations(name) VALUES(?)', (name,))
        return self.db.execute('SELECT id FROM organizations WHERE name=?', (name,)).fetchone()[0]

    def org_name(self, org_id):
        row = self.db.execute('SELECT name FROM organizations WHERE id=?', (org_id,)).fetchone()
        if row is None:
            raise ValueError('企业不存在')
        return row['name']

    def by_username(self, username):
        row = self.db.execute('''SELECT u.*,o.name AS org_name FROM users u
            LEFT JOIN organizations o ON o.id=u.org_id WHERE u.username=?''', (username,)).fetchone()
        return dict(row) if row else None

    def get(self, user_id):
        row = self.db.execute('''SELECT u.*,o.name AS org_name FROM users u
            LEFT JOIN organizations o ON o.id=u.org_id WHERE u.id=?''', (user_id,)).fetchone()
        return dict(row) if row else None

    def create(self, values):
        keys = ('username', 'display_name', 'password_hash', 'role', 'org_id', 'can_manage_jobs')
        return self.db.execute('''INSERT INTO users(
            username,display_name,password_hash,role,org_id,can_manage_jobs) VALUES(?,?,?,?,?,?)''',
            tuple(values[key] for key in keys)).lastrowid

    def expire_sessions(self, timestamp):
        self.db.execute('DELETE FROM sessions WHERE expires<?', (timestamp,))

    def create_session(self, token_hash, user_id, expires):
        self.db.execute('INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)',
                        (token_hash, user_id, expires))

    def session_user(self, token_hash, timestamp):
        row = self.db.execute('''SELECT u.*,o.name AS org_name FROM sessions s
            JOIN users u ON u.id=s.user_id LEFT JOIN organizations o ON o.id=u.org_id
            WHERE s.token_hash=? AND s.expires>?''', (token_hash, timestamp)).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash):
        self.db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash,))

    def members(self, org_id):
        return [dict(row) for row in self.db.execute('''SELECT id,username,display_name,can_manage_jobs
            FROM users WHERE org_id=? AND role='hr' ORDER BY id''', (org_id,))]

    def grant(self, user_id, org_id, allowed):
        return bool(self.db.execute('''UPDATE users SET can_manage_jobs=?
            WHERE id=? AND org_id=? AND role='hr' ''', (int(allowed), user_id, org_id)).rowcount)

    def favorites(self, user_id):
        return {row['job_id']: row['note'] for row in self.db.execute(
            'SELECT job_id,note FROM favorites WHERE user_id=?', (user_id,))}

    def save_favorite(self, user_id, job_id, note, remove):
        if remove:
            self.db.execute('DELETE FROM favorites WHERE user_id=? AND job_id=?', (user_id, job_id))
        elif note is None:
            self.db.execute('INSERT OR IGNORE INTO favorites(user_id,job_id) VALUES(?,?)', (user_id, job_id))
        else:
            self.db.execute('''INSERT INTO favorites(user_id,job_id,note) VALUES(?,?,?)
                ON CONFLICT(user_id,job_id) DO UPDATE SET note=excluded.note''', (user_id, job_id, note))


class AuditRepository:
    def __init__(self, connection):
        self.db = connection

    def append(self, actor, action, target, details=None):
        self.db.execute('''INSERT INTO audit_events(user_id,org_id,action,target,created,details)
            VALUES(?,?,?,?,?,?)''', (actor['id'], actor['org_id'], action, str(target), time.time(),
                                     json.dumps(details or {}, ensure_ascii=False)))

    def recent(self, actor):
        clause, args = ('1=1', ()) if actor['role'] == 'admin' else ('a.org_id=?', (actor['org_id'],))
        return [dict(row) for row in self.db.execute('''SELECT a.action,a.target,a.created,a.details,u.display_name
            FROM audit_events a JOIN users u ON u.id=a.user_id WHERE ''' + clause +
            ' ORDER BY a.id DESC LIMIT 50', args)]
