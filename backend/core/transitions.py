"""User-facing state changes; worker-owned operations must be reconciled first."""
APPLICATION_TRANSITIONS = {
    'assisting': {'needs_human', 'submitted'},
    'pending_review': {'approved', 'rejected_by_user', 'needs_human'},
    'approved': {'pending_review', 'rejected_by_user', 'needs_human'},
    'needs_human': {'approved', 'rejected_by_user', 'submitted', 'failed'},
    'failed': {'approved', 'rejected_by_user', 'needs_human', 'submitted'},
    'rejected_by_user': {'pending_review'},
    'submitted': {'replied', 'interview', 'rejected', 'offer'},
    'replied': {'interview', 'rejected', 'offer'},
    'interview': {'replied', 'rejected', 'offer'},
    'rejected': {'interview', 'offer'},
    'offer': {'rejected'},
}


def application_transition(app, status, note=None):
    if status not in APPLICATION_TRANSITIONS.get(app.status, set()):
        raise ValueError(f'Cannot change {app.status} to {status}')
    if app.status == 'assisting':
        from backend.core import browser
        if browser.health()['busy']: raise ValueError('Manual assistance currently owns the browser')
        if not (note or '').strip(): raise ValueError('Record what happened during manual assistance')
    if status == 'approved':
        from backend.core.submissions import approval_issue
        issue = approval_issue(app)
        if issue: raise ValueError(issue)
        if app.status in ('needs_human', 'failed') and not (note or '').strip():
            raise ValueError('Check employer history and record why retrying will not duplicate an application')
    if status == 'submitted' and not (note or '').strip():
        raise ValueError('Record employer confirmation evidence before marking submitted')
