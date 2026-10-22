"""Account-owned resume rows; commits belong to the unit of work."""


class ResumeRepository:
    def __init__(self, connection):
        self.connection = connection

    def metadata(self, user_id):
        row = self.connection.execute(
            'SELECT filename,media_type,size_bytes,uploaded_at FROM resumes WHERE user_id=?',
            (user_id,)).fetchone()
        return dict(row) if row else None

    def get(self, user_id):
        row = self.connection.execute('SELECT * FROM resumes WHERE user_id=?',
                                      (user_id,)).fetchone()
        return dict(row) if row else None

    def save(self, user_id, values):
        self.connection.execute('''INSERT INTO resumes
            (user_id,filename,media_type,size_bytes,content,sha256,uploaded_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            filename=excluded.filename,media_type=excluded.media_type,
            size_bytes=excluded.size_bytes,content=excluded.content,
            sha256=excluded.sha256,uploaded_at=excluded.uploaded_at''',
            (user_id, values['filename'], values['media_type'], values['size_bytes'],
             values['content'], values['sha256'], values['uploaded_at']))
