"""Reload permissions inside the same transaction as the protected operation."""
def live_actor(tx, actor):
    if not actor:
        raise PermissionError('请先登录')
    current = tx.accounts.get(actor['id'])
    if current is None or not current['active']:
        raise PermissionError('账号不存在或已停用')
    return current
