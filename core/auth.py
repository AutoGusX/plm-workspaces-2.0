# Authentication: OAuth 2.0 PKCE + local callback server for APS 3-legged auth,
# token cache + file persistence (stay-signed-in), token refresh, FM tenant
# resolution, and UI preferences (theme/text size) persistence.
#
# Ported from the old lib/oauth_pkce.py, lib/auth_utils.py, and lib/user_prefs.py
# with behavior PRESERVED — this is a working flow; only its location changed.
#
# Token/prefs persistence strategy (unchanged): write to the stable add-in root
# (shared across all Fusion processes) and, when set, also to the live add-in root.

import base64
import hashlib
import json
import os
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from . import config
from . import paths

EXPIRY_BUFFER_SECONDS = 300

# ---------------------------------------------------------------------------
# Module state (OAuth flow + token cache)
# ---------------------------------------------------------------------------
_current_state = None
_current_code_verifier = None
_callback_server = None
_server_thread = None
_app_ref = None
_event_id = None
_last_token_json = None
_last_token_error = None
_pending_stay_signed_in = False
_cache_access_token = None
_cache_expires_at = None
_cache_refresh_token = None
_trace_log = []
_TRACE_MAX = 200

_TOKENS_FILENAME = 'tokens.json'
_PREFS_FILENAME = 'prefs.json'           # stay-signed-in flag
_UI_PREFS_FILENAME = 'ui_prefs.json'     # theme / text size
_APP_PREFS_FILENAME = 'app_prefs.json'   # feature config (e.g. electronics-BOM export mapping)
_addin_root = None


def set_addin_root(root):
    """Set the live add-in root so tokens/prefs are written to both it and the stable path."""
    global _addin_root
    _addin_root = root


# ---------------------------------------------------------------------------
# Trace log (in-memory, bounded) — useful when diagnosing sign-in issues.
# ---------------------------------------------------------------------------
def _trace(msg, data=None):
    global _trace_log
    entry = {'t': round(time.time() * 1000), 'msg': msg}
    if data is not None:
        entry['data'] = data
    _trace_log.append(entry)
    if len(_trace_log) > _TRACE_MAX:
        _trace_log = _trace_log[-_TRACE_MAX:]


def get_trace_log():
    return list(_trace_log)


def clear_trace_log():
    global _trace_log
    _trace_log = []


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------
def generate_pkce():
    raw = secrets.token_bytes(32)
    code_verifier = base64.urlsafe_b64encode(raw).decode('utf-8').rstrip('=')
    digest = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge


def build_authorize_url(client_id, redirect_uri, scope, state, code_challenge):
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    return f'{config.APS_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}'


def exchange_code_for_token(client_id, redirect_uri, code, code_verifier, token_url):
    body = urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'code': code,
        'redirect_uri': redirect_uri,
        'code_verifier': code_verifier,
    })
    req = urllib.request.Request(
        token_url, data=body.encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def exchange_refresh_for_token(refresh_token, token_url, client_id, scope):
    body = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'refresh_token': refresh_token,
        'scope': scope,
    })
    req = urllib.request.Request(
        token_url, data=body.encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _get_pending_state():
    return _current_state, _current_code_verifier


def _set_pending_state(state, code_verifier):
    global _current_state, _current_code_verifier
    _current_state = state
    _current_code_verifier = code_verifier


# ---------------------------------------------------------------------------
# Token / prefs persistence (stable path + optional addin path)
# ---------------------------------------------------------------------------
def _paths_for(filename):
    stable = paths.get_stable_addin_root()
    stable_path = os.path.join(stable, filename) if stable else None
    addin_path = os.path.join(_addin_root, filename) if _addin_root else None
    return stable_path, addin_path


def _write_secure(path, content):
    """Write content to path with 0600 perms, creating the parent dir. Silent on failure."""
    try:
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, mode=0o700, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _load_prefs():
    stable_path, addin_path = _paths_for(_PREFS_FILENAME)
    default = {'staySignedIn': False}
    for path in (stable_path, addin_path):
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {'staySignedIn': bool(data.get('staySignedIn', False))}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return default


def _save_prefs(prefs):
    stable_path, addin_path = _paths_for(_PREFS_FILENAME)
    content = json.dumps(prefs)
    for path in (stable_path, addin_path):
        if path:
            _write_secure(path, content)


def get_prefs():
    return _load_prefs()


def set_stay_signed_in(stay_signed_in):
    prefs = _load_prefs()
    prefs['staySignedIn'] = bool(stay_signed_in)
    _save_prefs(prefs)
    if not stay_signed_in:
        # Drop persisted tokens so they don't outlive the user's choice.
        stable_path, addin_path = _paths_for(_TOKENS_FILENAME)
        for path in (stable_path, addin_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    _trace('set_stay_signed_in', {'staySignedIn': stay_signed_in})


def set_pending_stay_signed_in(stay_signed_in):
    global _pending_stay_signed_in
    _pending_stay_signed_in = bool(stay_signed_in)


def _load_tokens_from_file():
    global _cache_access_token, _cache_expires_at, _cache_refresh_token
    if not _load_prefs().get('staySignedIn'):
        return
    stable_path, addin_path = _paths_for(_TOKENS_FILENAME)
    for path in (stable_path, addin_path):
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            at = data.get('access_token')
            ea = data.get('expires_at')
            rt = data.get('refresh_token') or ''
            if at and ea is not None:
                _cache_access_token = at
                _cache_expires_at = float(ea)
                _cache_refresh_token = rt if rt else _cache_refresh_token
                _trace('Loaded tokens from file', {'expires_at': ea})
                return
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue


def _save_tokens_to_file(access_token, expires_at, refresh_token):
    stable_path, addin_path = _paths_for(_TOKENS_FILENAME)
    payload = json.dumps({
        'access_token': access_token,
        'expires_at': expires_at,
        'refresh_token': refresh_token,
    })
    for path in (stable_path, addin_path):
        if not path:
            continue
        if _write_secure(path, payload):
            _trace('Saved tokens to file', {'path': path})
        else:
            _trace('Failed to save tokens to file', {'path': path})


def update_cache_from_token_data(token_data, stay_signed_in):
    global _cache_access_token, _cache_expires_at, _cache_refresh_token
    access_token = token_data.get('access_token')
    refresh_token = token_data.get('refresh_token')
    expires_in = token_data.get('expires_in')
    if not access_token:
        return
    expires_at = time.time() + int(expires_in) if expires_in is not None else time.time() + 3600
    _cache_access_token = access_token
    _cache_expires_at = expires_at
    _cache_refresh_token = refresh_token or _cache_refresh_token
    if stay_signed_in and refresh_token:
        _save_tokens_to_file(access_token, expires_at, refresh_token)
    _trace('Cache updated', {'stay_signed_in': stay_signed_in, 'expires_at': expires_at})


def get_valid_access_token(force=False):
    """Return (access_token, error). error is None on success; on failure one of
    'refresh_failed' | 'refresh_transient' | 'not_signed_in'.

    Uses the cached token if still within the expiry buffer; otherwise refreshes.

    force=True bypasses the buffered cache and forces a refresh-token exchange.
    Used by the 401-refresh-retry: a server-side 401 on a locally-unexpired token
    means the cached bearer is actually stale, so re-using it would just 401 again.
    """
    global _cache_access_token, _cache_expires_at, _cache_refresh_token
    now = time.time()
    if _cache_access_token is None and _cache_expires_at is None:
        _load_tokens_from_file()
    if not force and _cache_access_token and _cache_expires_at is not None:
        if now < _cache_expires_at - EXPIRY_BUFFER_SECONDS:
            _trace('get_valid_access_token: using cached token', {'expires_at': _cache_expires_at})
            return _cache_access_token, None
    if _cache_refresh_token:
        try:
            _trace('get_valid_access_token: trying refresh token', None)
            token_data = exchange_refresh_for_token(
                _cache_refresh_token, config.APS_TOKEN_URL,
                config.APS_CLIENT_ID, config.APS_SCOPE,
            )
            stay = _load_prefs().get('staySignedIn', False)
            update_cache_from_token_data(token_data, stay)
            return _cache_access_token, None
        except urllib.error.HTTPError as e:
            _trace('get_valid_access_token: refresh HTTP error', {'code': e.code})
            _cache_access_token = None
            _cache_expires_at = None
            if e.code in (400, 401):
                # Token endpoint explicitly rejected the refresh token — it's invalid.
                _cache_refresh_token = None
            # For 5xx / other codes keep the refresh token so the next call can retry.
            return None, 'refresh_failed'
        except (OSError, ValueError, KeyError):
            _trace('get_valid_access_token: refresh transient error', None)
            # Network timeout / parse error — keep refresh token for a later retry.
            return None, 'refresh_transient'
    return None, 'not_signed_in'


def has_valid_token():
    """True if a valid access token is available (cached or refreshable)."""
    token, _ = get_valid_access_token()
    return bool(token)


def set_manual_token(access_token, expires_in_seconds=3600):
    """Set a token from manual paste (testing). Persists to file; no refresh token."""
    global _cache_access_token, _cache_expires_at, _cache_refresh_token
    token = (access_token or '').strip()
    if not token:
        return False
    expires_at = time.time() + int(expires_in_seconds)
    _cache_access_token = token
    _cache_expires_at = expires_at
    _cache_refresh_token = _cache_refresh_token or ''
    _save_tokens_to_file(token, expires_at, '')
    _set_last_token_result(None, None)
    _trace('Manual token set', {'expires_at': expires_at})
    return True


def clear_token_cache():
    """Clear the in-memory token cache and remove token files (e.g. after a 401)."""
    global _cache_access_token, _cache_expires_at, _cache_refresh_token
    _cache_access_token = None
    _cache_expires_at = None
    _cache_refresh_token = None
    stable_path, addin_path = _paths_for(_TOKENS_FILENAME)
    for path in (stable_path, addin_path):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                _trace('Removed token file', {'path': path})
            except OSError:
                pass


def get_last_token_result():
    return _last_token_json, _last_token_error


def _set_last_token_result(token_json=None, error_msg=None):
    global _last_token_json, _last_token_error
    _last_token_json = token_json
    _last_token_error = error_msg


# ---------------------------------------------------------------------------
# Tenant resolution (from the active hub only — no manual override).
# ---------------------------------------------------------------------------
def _tenant_from_fusion_web_url(url):
    if not url or not isinstance(url, str):
        return ''
    try:
        host = (urlparse(url).netloc or '').strip().lower()
        if host.endswith('.autodesk360.com'):
            prefix = host[:-len('.autodesk360.com')].strip()
            if prefix:
                return prefix
        return ''
    except Exception:
        return ''


def _b64_url_safe_decode_hub_id(hub_id):
    if not hub_id or not isinstance(hub_id, str):
        return ''
    try:
        raw = hub_id.strip().lstrip('a.') + '==='
        return base64.urlsafe_b64decode(raw).decode('utf-8')
    except Exception:
        return ''


def _tenant_from_hub_id(hub_id):
    decoded = _b64_url_safe_decode_hub_id(hub_id)
    if not decoded:
        return ''
    parts = decoded.split(':')
    return parts[-1].strip() if parts else ''


def get_tenant(app):
    """Return the Fusion Manage tenant from the active hub only."""
    try:
        data_obj = app.data
        if not data_obj:
            return ''
        active_hub = getattr(data_obj, 'activeHub', None)
        if not active_hub:
            return ''
        hub_id = getattr(active_hub, 'id', None) or ''
        tenant = _tenant_from_hub_id(hub_id)
        if tenant:
            return tenant
        web_url = getattr(active_hub, 'fusionWebURL', None) or ''
        return _tenant_from_fusion_web_url(web_url)
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# UI preferences (theme / text size). Persisted to ui_prefs.json in the stable
# add-in root. Source of truth for the getUiPrefs/setUiPrefs shared actions.
# ---------------------------------------------------------------------------
_VALID_THEMES = ('auto', 'light', 'dark')
_VALID_SIZES = ('sm', 'md', 'lg', 'xl')


def default_ui_prefs():
    return {'theme': 'auto', 'textSize': 'md'}


def _ui_prefs_path():
    root = paths.get_stable_addin_root()
    return os.path.join(root, _UI_PREFS_FILENAME) if root else None


def _sanitize_ui_prefs(prefs):
    out = default_ui_prefs()
    if isinstance(prefs, dict):
        if prefs.get('theme') in _VALID_THEMES:
            out['theme'] = prefs['theme']
        if prefs.get('textSize') in _VALID_SIZES:
            out['textSize'] = prefs['textSize']
    return out


def load_ui_prefs():
    """Return current UI prefs merged with defaults. Silent on read errors."""
    path = _ui_prefs_path()
    if not path or not os.path.isfile(path):
        return default_ui_prefs()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return _sanitize_ui_prefs(json.load(f))
    except (OSError, ValueError):
        return default_ui_prefs()


def save_ui_prefs(partial):
    """Merge a partial UI-prefs dict into the stored prefs and persist. Returns the full dict."""
    path = _ui_prefs_path()
    merged = load_ui_prefs()
    if isinstance(partial, dict):
        if partial.get('theme') in _VALID_THEMES:
            merged['theme'] = partial['theme']
        if partial.get('textSize') in _VALID_SIZES:
            merged['textSize'] = partial['textSize']
    if path:
        _write_secure(path, json.dumps(merged))
    return merged


# ---------------------------------------------------------------------------
# App preferences (feature config, e.g. the electronics-BOM export workspace/field
# mapping). Generic namespaced JSON in app_prefs.json — NOT sanitized like ui_prefs.
# ---------------------------------------------------------------------------
def _app_prefs_path():
    root = paths.get_stable_addin_root()
    return os.path.join(root, _APP_PREFS_FILENAME) if root else None


def load_app_prefs(section=None):
    """Return the full app-prefs dict, or a single top-level section dict if named."""
    path = _app_prefs_path()
    data = {}
    if path and os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    if section is not None:
        val = data.get(section)
        return val if isinstance(val, dict) else {}
    return data


def save_app_prefs(section, partial):
    """Shallow-merge `partial` into the named top-level section and persist. Returns the
    merged section dict."""
    path = _app_prefs_path()
    data = load_app_prefs()
    current = data.get(section) if isinstance(data.get(section), dict) else {}
    if isinstance(partial, dict):
        current.update(partial)
    data[section] = current
    if path:
        _write_secure(path, json.dumps(data))
    return current


# ---------------------------------------------------------------------------
# Local callback server + branded success/failure page.
# ---------------------------------------------------------------------------
def _parse_code_state_from_query_or_fragment(parsed):
    code, state = None, None
    qs = urllib.parse.parse_qs(parsed.query)
    if qs.get('code'):
        code = qs['code'][0]
    if qs.get('state'):
        state = qs['state'][0]
    if (not code or not state) and parsed.fragment:
        frag_qs = urllib.parse.parse_qs(parsed.fragment)
        if not code and frag_qs.get('code'):
            code = frag_qs['code'][0]
        if not state and frag_qs.get('state'):
            state = frag_qs['state'][0]
    return code, state


_CALLBACK_PAGE_STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 100%; height: 100%; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    font-size: 14px;
    background: #f0f2f5;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
}
.container { width: 340px; padding: 0 16px; text-align: center; }
.brand { display: flex; justify-content: center; margin-bottom: 6px; }
.adsk-logo-img { height: 26px; width: auto; }
.product-name { font-size: 12px; color: #888; margin-bottom: 24px; letter-spacing: 0.02em; }
.card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.10); padding: 28px 24px; }
.icon { width: 52px; height: 52px; border-radius: 50%; display: flex; align-items: center;
        justify-content: center; margin: 0 auto 16px; font-size: 26px; }
.icon.success { background: #e8f5e9; }
.icon.error   { background: #fdecea; }
.card h1 { font-size: 18px; font-weight: 700; color: #1f1f1f; margin-bottom: 8px; }
.card p { font-size: 13px; color: #666; line-height: 1.5; }
.error-detail { margin-top: 12px; padding: 10px 12px; background: #fafafa; border: 1px solid #e0e0e0;
    border-radius: 6px; font-family: Consolas, 'Courier New', monospace; font-size: 11px;
    color: #b71c1c; text-align: left; word-break: break-all; }
.footer { margin-top: 20px; font-size: 11px; color: #bbb; }
"""


def _get_adsk_logo_img_tag():
    logo_path = os.path.join(os.path.dirname(__file__), 'autodesk-logo.png')
    try:
        with open(logo_path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return f'<img class="adsk-logo-img" src="data:image/png;base64,{data}" alt="Autodesk">'
    except Exception:
        return ('<span style="font-size:16px;font-weight:700;letter-spacing:.07em;'
                'color:#1f1f1f">AUTODESK</span>')


def _make_callback_page(success=True, message=''):
    if success:
        icon_html = '<div class="icon success">&#10003;</div>'
        heading = 'Signed in successfully'
        body_text = 'You can close this window and return to Fusion.'
        detail_html = ''
    else:
        icon_html = '<div class="icon error">&#10007;</div>'
        heading = 'Sign-in failed'
        body_text = 'Something went wrong during authentication.'
        safe_msg = (message or 'Unknown error').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        detail_html = f'<div class="error-detail">{safe_msg}</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{'Signed in' if success else 'Sign-in failed'} – Autodesk PLM Workspaces</title>
<style>{_CALLBACK_PAGE_STYLE}</style>
</head>
<body>
<div class="container">
  <div class="brand">
    {_get_adsk_logo_img_tag()}
  </div>
  <div class="product-name">Fusion Manage &middot; PLM Workspaces</div>
  <div class="card">
    {icon_html}
    <h1>{heading}</h1>
    <p>{body_text}</p>
    {detail_html}
  </div>
  <div class="footer">Autodesk Fusion Add-in</div>
</div>
</body>
</html>"""
    return html.encode('utf-8')


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _pending_stay_signed_in
        raw_path = self.path
        parsed = urllib.parse.urlparse(raw_path)
        path = parsed.path or ''
        code, state = _parse_code_state_from_query_or_fragment(parsed)

        _trace('Callback server: GET received', {
            'raw_path': raw_path[:200], 'path': path,
            'has_code': bool(code), 'has_state': bool(state),
        })

        body = b''
        if code and state:
            expected_state, code_verifier = _get_pending_state()
            if expected_state is not None and state == expected_state and code_verifier:
                try:
                    _trace('Exchanging code for token', {'redirect_uri': config.APS_REDIRECT_URI})
                    token_data = exchange_code_for_token(
                        config.APS_CLIENT_ID, config.APS_REDIRECT_URI,
                        code, code_verifier, config.APS_TOKEN_URL,
                    )
                    token_json = json.dumps(token_data)
                    _set_last_token_result(token_json=token_json)
                    update_cache_from_token_data(token_data, _pending_stay_signed_in)
                    _trace('Token exchange success', {'token_keys': list(token_data.keys())})
                    if _app_ref and _event_id:
                        try:
                            _app_ref.fireCustomEvent(_event_id, token_json)
                        except Exception:
                            pass
                    body = _make_callback_page(success=True)
                except Exception as e:
                    err_msg = str(e)
                    _set_last_token_result(error_msg=err_msg)
                    _trace('Token exchange failed', {'error': err_msg})
                    body = _make_callback_page(success=False, message=err_msg)
            else:
                _set_last_token_result(error_msg='Invalid state or missing verifier.')
                body = _make_callback_page(success=False, message='Invalid state or missing verifier.')
        else:
            if path in ('/', '') and _last_token_json is None:
                _set_last_token_result(error_msg='No code or state in callback.')
            _trace('Callback: no code or state', {'path': path})

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode('utf-8'))
        # Shut down after responding so the port is free for re-sign-in in the same
        # session. Must run on another thread: this handler runs on the server thread,
        # so calling shutdown() inline would deadlock.
        server = self.server

        def _close_after_response():
            time.sleep(0.25)
            try:
                if server:
                    server.shutdown()
            except Exception:
                pass
            stop_callback_server()

        threading.Thread(target=_close_after_response, daemon=True).start()

    def log_message(self, format, *args):
        pass


def stop_callback_server():
    """Stop the callback server and release the port for a later sign-in."""
    global _callback_server, _server_thread
    server = _callback_server
    _callback_server = None
    _server_thread = None
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        _trace('Callback server stopped', None)


def start_callback_server(app, event_id):
    global _callback_server, _server_thread, _app_ref, _event_id
    _trace('start_callback_server called', {'port': config.APS_CALLBACK_PORT})
    _app_ref = app
    _event_id = event_id
    if _callback_server is not None:
        _trace('Callback server already running', None)
        return
    _callback_server = HTTPServer(('127.0.0.1', config.APS_CALLBACK_PORT), _CallbackHandler)
    _server_thread = threading.Thread(target=_callback_server.serve_forever, daemon=True)
    _server_thread.start()
    _trace('Callback server started', {'bind': '127.0.0.1', 'port': config.APS_CALLBACK_PORT})


def prepare_login():
    """Begin a PKCE login: reset state, build and return the APS authorize URL."""
    _set_last_token_result(None, None)
    clear_trace_log()
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce()
    _set_pending_state(state, code_verifier)
    url = build_authorize_url(
        config.APS_CLIENT_ID, config.APS_REDIRECT_URI,
        config.APS_SCOPE, state, code_challenge,
    )
    _trace('prepare_login: authorize URL built', {
        'redirect_uri': config.APS_REDIRECT_URI,
        'state_length': len(state), 'url_length': len(url),
    })
    return url
