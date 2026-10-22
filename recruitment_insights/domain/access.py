"""Role and record visibility policies; no storage or session dependencies."""


def can_manage(actor):
    return bool(actor and (actor['role'] in ('admin', 'boss') or
                           (actor['role'] == 'hr' and bool(actor['can_manage_jobs']))))


def may_review(row, actor):
    return bool(actor and (actor['role'] == 'admin' or (
        row['owner_org_id'] is not None and row['owner_org_id'] == actor.get('org_id')
        and can_manage(actor))))


def may_manage_lifecycle(row, actor):
    """Admins maintain public sources; enterprise maintainers own their jobs."""
    if not actor:
        return False
    if actor['role'] == 'admin':
        return row['owner_org_id'] is None
    return bool(actor['role'] in ('boss', 'hr') and can_manage(actor)
                and row['owner_org_id'] is not None
                and row['owner_org_id'] == actor.get('org_id'))


def may_read_duplicate_reason(left, right, actor):
    return bool(actor and (actor['role'] == 'admin' or (
        actor['role'] in ('boss', 'hr') and actor.get('org_id') is not None and
        left['owner_org_id'] == actor['org_id'] == right['owner_org_id'])))


def can_read_job(row, actor, include_inactive=False):
    """Apply record visibility after a repository read, including detail reads."""
    active = bool(row['active'])
    if not active and not include_inactive:
        return False
    if actor is None or actor['role'] == 'admin':
        return True
    public = row['owner_org_id'] is None or bool(row['published'])
    if actor['role'] == 'candidate':
        return active and public
    if actor['role'] in ('boss', 'hr'):
        own = row['owner_org_id'] is not None and row['owner_org_id'] == actor.get('org_id')
        return own or (active and public)
    return False
