"""SQLite review rows bound to an existing unit-of-work connection."""


class ReviewRepository:
    def __init__(self, connection):
        self.connection = connection

    def fields(self, ids):
        result = {}
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            query = 'SELECT * FROM field_reviews WHERE job_id IN (' + ','.join('?' for _ in chunk) + ')'
            for row in self.connection.execute(query, chunk):
                result[(row['job_id'], row['field'])] = dict(row)
        return result

    def field(self, job_id, field):
        row = self.connection.execute('SELECT * FROM field_reviews WHERE job_id=? AND field=?',
                                      (job_id, field)).fetchone()
        return dict(row) if row is not None else None

    def save_field(self, job_id, field, values):
        columns = ('conclusion', 'source_hash', 'state', 'version', 'user_id', 'reviewed_at')
        self.connection.execute('''INSERT INTO field_reviews(
            job_id,field,conclusion,source_hash,state,version,user_id,reviewed_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,field) DO UPDATE SET
            conclusion=excluded.conclusion,source_hash=excluded.source_hash,state=excluded.state,
            version=excluded.version,user_id=excluded.user_id,reviewed_at=excluded.reviewed_at''',
            (job_id, field, *(values[key] for key in columns)))

    def decisions(self):
        return {(row['left_id'], row['right_id']): dict(row)
                for row in self.connection.execute('SELECT * FROM duplicate_decisions')}

    def decision(self, left_id, right_id):
        row = self.connection.execute('SELECT * FROM duplicate_decisions WHERE left_id=? AND right_id=?',
                                      (left_id, right_id)).fetchone()
        return dict(row) if row is not None else None

    def save_decision(self, left_id, right_id, values):
        columns = ('decision', 'reason', 'left_hash', 'right_hash', 'version', 'user_id', 'reviewed_at')
        self.connection.execute('''INSERT INTO duplicate_decisions(
            left_id,right_id,decision,reason,left_hash,right_hash,version,user_id,reviewed_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(left_id,right_id) DO UPDATE SET
            decision=excluded.decision,reason=excluded.reason,left_hash=excluded.left_hash,
            right_hash=excluded.right_hash,version=excluded.version,user_id=excluded.user_id,
            reviewed_at=excluded.reviewed_at''', (left_id, right_id, *(values[key] for key in columns)))
