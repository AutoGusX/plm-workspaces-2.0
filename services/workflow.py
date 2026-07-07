# Workflow transitions: list available transitions + execute one.
#
# These use the /api/rest/v1 endpoints (not /api/v3) and require the X-Tenant header
# in addition to Authorization + x-User-id. Ported from lib/fusion_manage_api.py.

import re  # noqa: F401 — kept for parity; not currently used


def transitions(client, workspace_id, item_id):
    """GET .../workflows/transitions. Returns (dict, error) with 'transitions' + 'currentStep'."""
    url = (f'{client.base}/api/rest/v1/workspaces/{workspace_id}'
           f'/items/{item_id}/workflows/transitions')
    r = client._request('GET', url, content_type='application/json', send_tenant=True)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    at = (r.data or {}).get('availableTransitions') or {}
    current_step = at.get('currentStep')
    if current_step is None:
        current_step = 0
    raw = (at.get('transitions') or {}).get('transition')
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    out = [{'transitionID': t.get('transitionID'),
            'shortName': t.get('shortName') or '',
            'description': t.get('description') or ''} for t in raw]
    return {'transitions': out, 'currentStep': int(current_step)}, None


def run_transition(client, workspace_id, item_id, transition_id, current_step, comments=''):
    """PUT .../workflows/transitions/{id} — advances the workflow one step.

    FM expects workflowStep = current_step + 1. Returns an error string (None on success).
    """
    url = (f'{client.base}/api/rest/v1/workspaces/{workspace_id}'
           f'/items/{item_id}/workflows/transitions/{transition_id}')
    body = {'workflowStep': int(current_step) + 1, 'workflowComments': comments or ''}
    r = client._request('PUT', url, body=body, content_type='application/json', send_tenant=True)
    if not r.ok:
        return ('unauthorized' if r.unauthorized else r.error)
    return None
