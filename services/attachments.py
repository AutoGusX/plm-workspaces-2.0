# Attachment CRUD + S3 upload flow. Ported from lib/fusion_manage_api.py.
#
# Upload is a three-step dance:
#   1. request_upload  -> FM returns a pre-signed S3 URL + extraHeaders
#   2. upload_to_s3    -> PUT raw bytes to S3 (NO FM auth header; the URL is signed)
#   3. checkin         -> PATCH attachment status to CheckIn so the version commits

import json  # noqa: F401 — parity import


def list_attachments(client, workspace_id, item_id):
    """GET /items/{id}/attachments?asc=name. Returns (list, error).

    The bulk Accept header is REQUIRED — without it the endpoint returns a
    pre-signed S3 upload URL instead of the attachment list.
    """
    url = (f'{client.base}/api/v3/workspaces/{workspace_id}'
           f'/items/{item_id}/attachments?asc=name')
    r = client._request('GET', url,
                        accept='application/vnd.autodesk.plm.attachments.bulk+json')
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    if not isinstance(r.data, dict):
        snippet = (r.text or '')[:300].replace('\n', ' ')
        return None, f'Attachments response not JSON: {snippet!r}'
    attachments = r.data.get('attachments', [])
    return (attachments if isinstance(attachments, list) else []), None


def request_upload(client, workspace_id, item_id, filename, resource_name, file_size,
                   existing_attachment_id=None, comment=''):
    """Request a pre-signed S3 URL for a new attachment or a new version.

    POST .../attachments       -> new attachment (existing_attachment_id is None)
    POST .../attachments/{id}   -> new version of an existing attachment

    comment is stored as the attachment description in Fusion Manage.
    Returns (dict, error) with 'attachment_id', 's3_url', 'extra_headers'.
    """
    base = f'{client.base}/api/v3/workspaces/{workspace_id}/items/{item_id}/attachments'
    url = f'{base}/{existing_attachment_id}' if existing_attachment_id is not None else base
    body = {'description': str(comment or ''), 'name': filename, 'folder': None,
            'resourceName': resource_name, 'size': int(file_size)}
    r = client._request('POST', url, body=body)
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    if not isinstance(r.data, dict):
        snippet = (r.text or '')[:300].replace('\n', ' ')
        return None, f'Upload-request response not JSON: {snippet!r}'
    return {'attachment_id': r.data.get('id'), 's3_url': r.data.get('url', ''),
            'extra_headers': r.data.get('extraHeaders') or {}}, None


def upload_to_s3(client, s3_url, extra_headers, file_bytes):
    """PUT raw bytes to a pre-signed S3 URL. Returns an error string (None on success).

    No FM Authorization header — the pre-signed URL already carries credentials, so
    with_auth=False. extra_headers carries S3 metadata (e.g. x-amz-meta-filename).
    """
    if not s3_url or not isinstance(s3_url, str):
        return 'Missing S3 URL.'
    r = client._request('PUT', s3_url, body=file_bytes, content_type='application/octet-stream',
                        extra_headers=dict(extra_headers or {}),
                        with_auth=False, accept=None, timeout=120, raw=True)
    if not r.ok:
        return f'S3 upload failed: {r.error}'
    if r.status not in (200, 204):
        return f'S3 PUT returned status {r.status}.'
    return None


def checkin(client, workspace_id, item_id, attachment_id):
    """PATCH attachment status to CheckIn after a successful S3 upload.
    Returns (version, error)."""
    url = (f'{client.base}/api/v3/workspaces/{workspace_id}'
           f'/items/{item_id}/attachments/{attachment_id}')
    r = client._request('PATCH', url, body={'status': {'name': 'CheckIn'}}, empty_body_as={})
    if not r.ok:
        return None, ('unauthorized' if r.unauthorized else r.error)
    data = r.data if isinstance(r.data, dict) else {}
    return data.get('version', 1), None
