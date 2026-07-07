# The single HTTP request helper.
#
# Collapses the ~30 copies of "build request / urlopen / except HTTPError / 401
# branch / parse error body" from the old lib/fusion_manage_api.py into one
# function. Every FM call in services/ goes through request() and never writes its
# own try/except again.
#
# Pure standard library (urllib). No external deps.

import json
import urllib.error
import urllib.request

from . import config


class Result:
    """Outcome of a single HTTP request.

    Attributes:
      ok           -- True on a 2xx response.
      status       -- HTTP status code (int) or None on a non-HTTP error.
      data         -- parsed JSON (dict/list) when the body was JSON, else None.
      text         -- raw decoded body text (always set when there was a body).
      raw_bytes    -- raw bytes when raw=True was requested (e.g. image/binary).
      content_type -- response Content-Type (lowercased, parameters stripped).
      etag         -- ETag response header (used for if-match concurrency), or ''.
      error        -- human-readable error message, or None when ok.
      unauthorized -- True when the server returned 401 (drives token refresh-retry).
    """

    __slots__ = ('ok', 'status', 'data', 'text', 'raw_bytes', 'content_type',
                 'etag', 'error', 'unauthorized')

    def __init__(self, ok=False, status=None, data=None, text='', raw_bytes=None,
                 content_type='', etag='', error=None, unauthorized=False):
        self.ok = ok
        self.status = status
        self.data = data
        self.text = text
        self.raw_bytes = raw_bytes
        self.content_type = content_type
        self.etag = etag
        self.error = error
        self.unauthorized = unauthorized

    def __repr__(self):
        return (f'Result(ok={self.ok}, status={self.status}, '
                f'unauthorized={self.unauthorized}, error={self.error!r})')


def _build_headers(bearer, user_id, tenant, accept, content_type, extra_headers):
    """Assemble the standard FM header set in one place."""
    headers = {}
    if bearer:
        headers['Authorization'] = f'Bearer {bearer}'
    if accept:
        headers['Accept'] = accept
    if user_id:
        # FM identifies the acting user via this header on most v3 endpoints.
        headers['x-User-id'] = user_id
    if tenant:
        # The /api/rest/v1 workflow endpoints additionally require X-Tenant.
        headers['X-Tenant'] = tenant
    if content_type:
        headers['Content-Type'] = content_type
    if extra_headers:
        for k, v in extra_headers.items():
            headers[str(k)] = str(v)
    return headers


def _encode_body(body):
    """Return bytes for the request body. dict/list -> JSON; str -> utf-8; bytes as-is."""
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode('utf-8')
    # dict / list -> JSON
    return json.dumps(body).encode('utf-8')


def parse_error_body(status_code, body):
    """Extract a validation/error message from a PLM error response body.

    Ported verbatim in behavior from the old _parse_workflow_error_response so
    workflow/create/update surfaces show the server's field-level messages.
    """
    if not body or not body.strip():
        return f'Request failed ({status_code}).'
    try:
        data = json.loads(body)
        # error as list of {message}
        err_list = data.get('error')
        if isinstance(err_list, list) and err_list:
            messages = [it.get('message') for it in err_list
                        if isinstance(it, dict) and it.get('message')]
            if messages:
                return '\n'.join(messages).strip() or body[:500]
        if isinstance(data.get('error'), str):
            return data['error']
        if isinstance(data.get('error'), dict) and data['error'].get('message'):
            return data['error']['message']
        if isinstance(data.get('message'), str) and data['message'].strip():
            return data['message'].strip()
        errors = data.get('errors')
        if isinstance(errors, list) and errors:
            messages = []
            for it in errors:
                if isinstance(it, dict) and it.get('message'):
                    messages.append(it['message'])
                elif isinstance(it, str):
                    messages.append(it)
            if messages:
                return '\n'.join(messages).strip()
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return body[:500] if len(body) > 500 else body


def request(method, url, *, bearer, user_id=None, tenant=None,
            accept='application/json', body=None, content_type=None,
            extra_headers=None, timeout=None, raw=False,
            empty_body_as=None):
    """Build, send, and parse a single HTTP request.

    method        -- 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'.
    url           -- absolute URL.
    bearer        -- OAuth access token (omit for pre-signed S3 PUTs).
    user_id       -- value for the x-User-id header.
    tenant        -- value for the X-Tenant header (workflow endpoints).
    accept        -- Accept header; pass a custom value for bulk/attachment endpoints.
    body          -- dict/list (JSON-encoded), str, or bytes; None for no body.
    content_type  -- Content-Type header; auto-set to application/json for dict/list
                     bodies when not provided.
    extra_headers -- dict of additional headers (e.g. S3 x-amz-meta-*).
    timeout       -- per-request timeout (defaults to config.HTTP_TIMEOUT_SECONDS).
    raw           -- when True, return the raw response bytes in Result.raw_bytes and
                     do NOT attempt JSON parsing (images, binary).
    empty_body_as -- value placed in Result.data when the response body is empty.
                     Some FM endpoints (search-results, affected-items) return an
                     empty body instead of {"items": []} when there are zero rows;
                     pass empty_body_as=[] (or {}) so callers see a normal result
                     rather than a parse failure.

    Returns a Result. Callers never write try/except for HTTP themselves.
    """
    if timeout is None:
        timeout = getattr(config, 'HTTP_TIMEOUT_SECONDS', 30)

    # Auto-set Content-Type for structured bodies when the caller didn't specify one.
    if content_type is None and isinstance(body, (dict, list)):
        content_type = 'application/json'

    data_bytes = _encode_body(body)
    headers = _build_headers(bearer, user_id, tenant, accept, content_type, extra_headers)

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', None) or resp.getcode()
            etag = resp.headers.get('ETag') or resp.headers.get('etag') or ''
            resp_ct = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            raw_bytes = resp.read()

            if raw:
                return Result(ok=True, status=status, raw_bytes=raw_bytes,
                              content_type=resp_ct, etag=etag)

            text = raw_bytes.decode('utf-8', errors='replace')
            if not text.strip():
                # Empty body. Honour the documented "empty means []/{}" quirk when asked.
                return Result(ok=True, status=status, data=empty_body_as, text=text,
                              content_type=resp_ct, etag=etag)
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                # Non-JSON 2xx body — hand back the text so callers can decide.
                return Result(ok=True, status=status, data=None, text=text,
                              content_type=resp_ct, etag=etag)
            return Result(ok=True, status=status, data=parsed, text=text,
                          content_type=resp_ct, etag=etag)

    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = ''
        if e.code == 401:
            return Result(ok=False, status=401, text=err_body,
                          error='Authentication failed.', unauthorized=True)
        return Result(ok=False, status=e.code, text=err_body,
                      error=parse_error_body(e.code, err_body))
    except urllib.error.URLError as e:
        return Result(ok=False, status=None, error=str(getattr(e, 'reason', e)))
    except Exception as e:  # noqa: BLE001 — network/timeout/parse safety net
        return Result(ok=False, status=None, error=str(e))
