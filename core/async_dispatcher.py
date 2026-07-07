# Async action dispatcher — background thread pool + main-thread result delivery.
#
# Fusion's `incomingFromHTML` fires on the main UI thread. Every HTTP call we make
# blocks that thread for up to several seconds, freezing Fusion's modelling
# environment while the palette waits for a response.
#
# This module provides an opt-in async execution model:
#   1. `palette_base._on_incoming` detects a handler decorated with `async_=True`.
#   2. It pre-resolves the RequestContext (Fusion API calls, main thread only).
#   3. Returns `{pending: true, requestId: "…"}` to the webview immediately.
#   4. Submits the handler to the shared ThreadPoolExecutor here.
#   5. When the handler returns, the worker stores the result and calls
#      `app.fireCustomEvent(ASYNC_RESULT_EVENT_ID, requestId)`.
#   6. The Fusion custom event fires on the main thread; our handler looks up the
#      result by requestId and calls `palette.sendInfoToHTML('plmAsyncResult', …)`.
#   7. bridge.js sees the push, matches it to the waiting Promise, and resolves it.
#
# From every caller's perspective — engine.js, my_work.js — `plmSend` still
# returns a Promise<data>.  The async handshake is completely internal to
# bridge.js + this module.
#
# Thread-safety notes:
#   - `_pending` is a plain dict guarded by `_lock` (written from pool threads,
#     read+deleted from the main-thread custom event handler).
#   - `_thread_ctx` is a threading.local so each pool worker carries its own
#     pre-resolved RequestContext without any shared-state races.
#   - `app.fireCustomEvent` is explicitly safe to call from non-UI threads (it is
#     the canonical Fusion mechanism for background→UI thread marshalling).
#   - `app.log` (used by core.log) is thread-safe in Fusion's implementation.

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import adsk.core

from . import config
from . import events
from . import log

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ASYNC_RESULT_EVENT_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_async_result_V2'

# Maximum concurrently executing async action handlers.
# Each handler may spawn its own internal ThreadPoolExecutor for sub-tasks
# (e.g. getRecordsEnrichment, getUnifiedChangeRecords), so the outer pool is
# kept intentionally small to avoid thread explosion.
_POOL_MAX_WORKERS = 4

# ---------------------------------------------------------------------------
# Thread-local storage: pre-resolved RequestContext.
#
# palette_base._on_incoming resolves a RequestContext on the main thread
# (safe: it calls Fusion API for user_id / tenant), then stores it here
# before submitting to the pool.  _resolve_ctx() in palette_base reads it
# back so action handlers never call Fusion API from a background thread.
# ---------------------------------------------------------------------------
_thread_ctx = threading.local()
_UNSET = object()          # sentinel: "no ctx pre-resolved for this thread"

# ---------------------------------------------------------------------------
# Module-level singletons (initialised in start(), cleared in stop())
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_pending = {}              # {requestId: (palette_id, action_name, result)}
_pool = None               # ThreadPoolExecutor
_event = None              # adsk.core.CustomEvent
_handler_refs = []         # keeps the Fusion event handler from being GC'd


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start():
    """Start the dispatcher: register the Fusion custom event + create the pool.

    Called once from commands/__init__.start().  Self-healing: if a previous
    unclean add-in reload left the event id registered, we unregister and
    re-register so event delivery still works.
    """
    global _pool, _event

    _pool = ThreadPoolExecutor(
        max_workers=_POOL_MAX_WORKERS,
        thread_name_prefix='plm_async',
    )

    app = adsk.core.Application.get()
    _event = app.registerCustomEvent(ASYNC_RESULT_EVENT_ID)
    if _event is None:
        log.log(
            'async_dispatcher: event already registered; re-registering after cleanup',
            adsk.core.LogLevels.WarningLogLevel,
        )
        try:
            app.unregisterCustomEvent(ASYNC_RESULT_EVENT_ID)
        except Exception:
            pass
        _event = app.registerCustomEvent(ASYNC_RESULT_EVENT_ID)

    if _event is not None:
        events.add_handler(_event, _on_result_ready, local_handlers=_handler_refs)

    log.log(
        f'async_dispatcher: started '
        f'(pool={_POOL_MAX_WORKERS} workers, event_ok={_event is not None})'
    )


def stop():
    """Tear down the dispatcher.  Called once from commands/__init__.stop()."""
    global _pool, _event

    _handler_refs.clear()

    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None

    try:
        app = adsk.core.Application.get()
        app.unregisterCustomEvent(ASYNC_RESULT_EVENT_ID)
    except Exception:
        pass
    _event = None

    with _lock:
        _pending.clear()

    log.log('async_dispatcher: stopped')


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def new_request_id():
    """Generate a short, unique request id."""
    return uuid.uuid4().hex[:16]


def submit(request_id, palette_id, action_name, handler_fn, command_self, pre_ctx, data):
    """Run `handler_fn(command_self, None, data)` in a background thread.

    pre_ctx   -- RequestContext resolved on the main thread before this call.
                 Stored in threading.local so _resolve_ctx() can use it without
                 touching any Fusion API from the background thread.

    On completion, the result is stored in _pending and
    app.fireCustomEvent triggers the main-thread delivery path.
    """
    if _pool is None:
        log.log(
            f'async_dispatcher.submit [{action_name}]: pool not started — '
            'falling back to inline execution.',
            adsk.core.LogLevels.WarningLogLevel,
        )
        # Fallback: run inline so the action still works even if start() was never called.
        try:
            result = handler_fn(command_self, None, data)
        except Exception:
            log.handle_error(f'async_dispatcher.{action_name}')
            result = {'success': False, 'error': 'Handler error.'}
        # Can't push asynchronously here, but returning won't help the caller
        # either since we already sent the ack.  Log and discard.
        log.log(
            f'async_dispatcher.submit [{action_name}]: inline fallback result discarded '
            '(client already received a pending ack).',
            adsk.core.LogLevels.WarningLogLevel,
        )
        return

    def _run():
        # Install pre-resolved ctx so _resolve_ctx() never calls Fusion API here.
        _thread_ctx.ctx = pre_ctx
        try:
            result = handler_fn(command_self, None, data)
        except Exception:
            log.handle_error(f'async_dispatcher.{action_name}')
            result = {'success': False, 'error': 'Handler error.'}
        finally:
            # Clear so the thread can be reused without leaking a stale context.
            try:
                del _thread_ctx.ctx
            except AttributeError:
                pass

        if result is None:
            result = {'success': True}

        with _lock:
            _pending[request_id] = (palette_id, action_name, result)

        try:
            app = adsk.core.Application.get()
            app.fireCustomEvent(ASYNC_RESULT_EVENT_ID, request_id)
        except Exception as exc:
            log.log(
                f'async_dispatcher: fireCustomEvent failed for {request_id} '
                f'({action_name}): {exc}',
                adsk.core.LogLevels.WarningLogLevel,
            )

    _pool.submit(_run)


# ---------------------------------------------------------------------------
# Main-thread custom event handler
# ---------------------------------------------------------------------------

def _on_result_ready(args: adsk.core.CustomEventArgs):
    """Fires on the Fusion UI thread when a background action completes.

    Looks up the result by requestId and pushes it to the palette via
    sendInfoToHTML('plmAsyncResult', …).  If the palette has been closed or
    the palette reference is stale the send is silently dropped.
    """
    try:
        request_id = args.additionalInfo
        with _lock:
            entry = _pending.pop(request_id, None)
        if entry is None:
            return

        palette_id, action_name, result = entry

        app = adsk.core.Application.get()
        palette = app.userInterface.palettes.itemById(palette_id)
        if palette is None:
            return   # palette was closed before the result arrived

        try:
            palette.sendInfoToHTML('plmAsyncResult', json.dumps({
                'requestId': request_id,
                'action': action_name,
                'data': result,
            }))
        except Exception as exc:
            log.log(
                f'async_dispatcher: sendInfoToHTML failed for {palette_id} '
                f'/{action_name}: {exc}',
                adsk.core.LogLevels.WarningLogLevel,
            )
    except Exception:
        log.handle_error('async_dispatcher._on_result_ready')
