/**
 * PLM Workspaces 2.0 — bridge.js
 *
 * THE single messaging layer between the Fusion webview and the Python backend.
 * Loaded first by every capability's index.html (before theme.js, engine.js, or
 * any capability config).
 *
 * Replaces both the canonical lib/html/plmPaletteUtils.js and the
 * designReview fork — there is now exactly ONE copy of this code.
 *
 * Exposes on window:
 *   plmSend(action, payload)           → Promise<object>  (JS→Python round-trip)
 *   plmParse(result)                   → object           (safe JSON parse)
 *   plmApplyTheme(name)                → void             (add/remove theme-* classes)
 *   plmRequestTheme()                  → void             (fetch + apply Fusion theme)
 *   plmCreateAuthPoller(onReady, ms)   → {start, stop}    (poll until signed in)
 *   plmOpenUrl(url)                    → void             (open in system browser)
 *   plmShowNoAccess(urlGroups, urlTpl) → void             (reveal #noAccessView, wire buttons)
 *   plmOpenInFusionIconHtml(wsId, itemId) → string        (HTML for the open-in-Fusion icon)
 *   window.fusionJavaScriptHandler     = {handle}         (Python→JS push-message receiver)
 *
 * The push-message handler is a DEFAULT stub. Each capability's index.html
 * sets window.fusionJavaScriptHandler AFTER this script loads; this stub is
 * the fallback and also the documented interface so capabilities know what to
 * override.
 */
(function (global) {
    'use strict';

    // -------------------------------------------------------------------------
    // JS → Python: send an action and return a Promise<parsed object>
    // -------------------------------------------------------------------------

    // Fusion injects the `adsk.fusionSendData` bridge into the palette webview
    // ASYNCHRONOUSLY — it is frequently not present yet at DOMContentLoaded
    // (observed in remote/sandbox Fusion especially). Sending before it exists
    // silently dropped the call and returned '{}', which surfaced as
    // hasToken=undefined and a blank palette. We poll until the bridge appears.
    var _adskReadyPromise = null;
    function _whenAdskReady() {
        if (typeof adsk !== 'undefined' && adsk.fusionSendData) {
            return Promise.resolve(true);
        }
        if (_adskReadyPromise) return _adskReadyPromise;
        _adskReadyPromise = new Promise(function (resolve) {
            var waited = 0, step = 50, limit = 15000;
            var timer = setInterval(function () {
                if (typeof adsk !== 'undefined' && adsk.fusionSendData) {
                    clearInterval(timer);
                    resolve(true);
                } else if ((waited += step) >= limit) {
                    clearInterval(timer);
                    resolve(false);
                }
            }, step);
        });
        return _adskReadyPromise;
    }

    // -------------------------------------------------------------------------
    // Async result routing
    //
    // When Python handles an action with async_=True it immediately returns
    //   {pending: true, requestId: "<hex>"}
    // and later pushes the real result via sendInfoToHTML('plmAsyncResult', …).
    //
    // _pendingAsync maps requestId → {resolve, reject} so plmSend can return
    // a Promise that resolves when the push arrives — no changes required in
    // engine.js, config.js, or any capability code.
    // -------------------------------------------------------------------------
    var _pendingAsync = {};
    var _ASYNC_TIMEOUT_MS = 120000;   // 2-minute safety valve per request

    function _waitForAsyncResult(requestId) {
        return new Promise(function (resolve, reject) {
            _pendingAsync[requestId] = {resolve: resolve, reject: reject};
            setTimeout(function () {
                if (_pendingAsync[requestId]) {
                    delete _pendingAsync[requestId];
                    reject(new Error('plmSend async timeout (' + requestId + ')'));
                }
            }, _ASYNC_TIMEOUT_MS);
        });
    }

    /**
     * Handle a 'plmAsyncResult' push from Python.
     * Called by fusionJavaScriptHandler.handle() — and re-exported as
     * window.plmHandleAsyncResult so overriding handlers (my_work.js, login.html)
     * can delegate to it without duplicating the routing logic.
     *
     * @returns {boolean} true if the action was consumed (caller should return early).
     */
    function plmHandleAsyncResult(action, data) {
        if (action !== 'plmAsyncResult') return false;
        try {
            var d = plmParse(data);
            var pending = d && d.requestId && _pendingAsync[d.requestId];
            if (pending) {
                delete _pendingAsync[d.requestId];
                pending.resolve(d.data || {});
            }
        } catch (e) { /* ignore */ }
        return true;
    }

    /**
     * Send an action + JSON payload to Python via the Fusion webview bridge.
     *
     * Transparent async support: if Python returns {pending:true, requestId}
     * the Promise suspends until the 'plmAsyncResult' push arrives, then
     * resolves with the real data.  Callers see no difference.
     */
    function plmSend(action, payload) {
        return _whenAdskReady().then(function (ready) {
            if (!ready) {
                throw new Error('Fusion bridge (adsk.fusionSendData) unavailable.');
            }
            return adsk.fusionSendData(action, JSON.stringify(payload || {}));
        }).then(function (rawResult) {
            var result = plmParse(rawResult);
            if (result && result.pending === true && result.requestId) {
                return _waitForAsyncResult(result.requestId);
            }
            return result;
        });
    }

    /** Safely parse a JSON string returned from Python, or return the value as-is
     *  if it is already an object (the Fusion API sometimes does this). */
    function plmParse(result) {
        try {
            return typeof result === 'string' ? JSON.parse(result) : (result || {});
        } catch (e) {
            return {};
        }
    }

    // -------------------------------------------------------------------------
    // Python → JS push-message receiver (default / fallback implementation)
    //
    // Every capability's index.html SHOULD override this with its own version
    // that handles 'tokenResult' and any capability-specific push actions.
    // The default stub handles 'tokenResult' generically (reload via plmCoreInit
    // if available) so simple palettes work without any boilerplate.
    // -------------------------------------------------------------------------
    // The default handler is used by all engine.js-based capabilities.
    // Capabilities with a custom handler (my_work.js) override window.fusionJavaScriptHandler
    // but must call plmHandleAsyncResult first to preserve async routing.
    global.fusionJavaScriptHandler = {
        handle: function (action, data) {
            try {
                // Async result routing takes priority — must run before any
                // capability-specific handling.
                if (plmHandleAsyncResult(action, data)) return 'OK';

                if (action === 'tokenResult') {
                    // Fast-path: a fresh OAuth token arrived. Stop any auth poller
                    // and re-initialize the palette if plmCoreInit is available.
                    if (global._authPoller && typeof global._authPoller.stop === 'function') {
                        global._authPoller.stop();
                    }
                    if (typeof global.plmCoreInit === 'function') {
                        global.plmCoreInit();
                    }
                } else if (action === 'uiPrefsChanged') {
                    // Settings changed from another palette (cross-palette sync).
                    if (global.plmUiPrefs && typeof global.plmUiPrefs.handlePythonMessage === 'function') {
                        global.plmUiPrefs.handlePythonMessage(action, data);
                    }
                }
            } catch (e) { /* ignore */ }
            return 'OK';
        }
    };

    // -------------------------------------------------------------------------
    // Theme helpers
    // -------------------------------------------------------------------------

    var _THEME_CLASSES = [
        'theme-classic', 'theme-light-gray', 'theme-dark-blue',
        'theme-dark-gray', 'theme-device'
    ];

    /** Remove all theme-* classes then add the new one. */
    function plmApplyTheme(name) {
        _THEME_CLASSES.forEach(function (c) { document.body.classList.remove(c); });
        if (name) {
            document.body.classList.add('theme-' + String(name).replace(/\s+/g, '-'));
        }
    }

    /** Fetch the Fusion UI theme via getTheme and apply it to the page. */
    function plmRequestTheme() {
        plmSend('getTheme', {}).then(function (r) {
            var d = plmParse(r);
            if (d.success && d.themeName) {
                plmApplyTheme(d.themeName);
            }
        }).catch(function () { /* ignore */ });
    }

    // -------------------------------------------------------------------------
    // Auth poller
    //
    // Polls getAuthStatus on an interval. Used as a reliability layer when the
    // page opens unauthenticated and waits for the user to complete OAuth in the
    // browser. The Python-push 'tokenResult' event is the fast path; this poller
    // fires in the (rare) case where the push arrives before the JS handler is
    // registered, or when Fusion's event delivery is delayed.
    // -------------------------------------------------------------------------

    /**
     * Create an auth poller that calls onReady() when getAuthStatus returns hasToken=true.
     * @param {Function} onReady  — called exactly once when auth is confirmed.
     * @param {number}   intervalMs — polling interval (default 3000 ms).
     * @returns {{start: function, stop: function}}
     */
    function plmCreateAuthPoller(onReady, intervalMs) {
        var timer = null;
        var ms = (typeof intervalMs === 'number' && intervalMs > 0) ? intervalMs : 3000;
        return {
            start: function () {
                if (timer) return;
                timer = setInterval(function () {
                    plmSend('getAuthStatus', {}).then(function (r) {
                        var d = plmParse(r);
                        if (d.hasToken) {
                            clearInterval(timer);
                            timer = null;
                            try { onReady(); } catch (e) { /* ignore */ }
                        }
                    }).catch(function () { /* ignore */ });
                }, ms);
            },
            stop: function () {
                if (timer) { clearInterval(timer); timer = null; }
            }
        };
    }

    // -------------------------------------------------------------------------
    // URL helper
    // -------------------------------------------------------------------------

    /** Open a URL in the system's default browser via the Python openInBrowser action. */
    function plmOpenUrl(url) {
        if (!url) return;
        plmSend('openInBrowser', { url: url });
    }

    // -------------------------------------------------------------------------
    // No-access view helper
    //
    // Shows the standard workspace-not-entitled view. Expects the page to have
    // #noAccessView, optionally #btnManageAccess and #btnTemplateLibrary.
    // -------------------------------------------------------------------------

    /**
     * Show the no-access panel and wire the admin admin URLs to the buttons.
     * @param {string} urlGroups   — Fusion Manage admin groups URL
     * @param {string} urlTpl      — Fusion Manage template library URL
     */
    function plmShowNoAccess(urlGroups, urlTpl) {
        var noView = document.getElementById('noAccessView');
        var defaultContent = document.getElementById('defaultContent');
        if (noView) noView.classList.add('visible');
        if (defaultContent) defaultContent.classList.add('hidden');
        // Hide list/detail/form views so only the no-access panel is visible.
        ['listView', 'detailView', 'formView'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
        var btnA = document.getElementById('btnManageAccess');
        var btnT = document.getElementById('btnTemplateLibrary');
        if (btnA) {
            btnA.setAttribute('data-url', urlGroups || '');
            btnA.onclick = function () { plmOpenUrl(urlGroups || ''); };
        }
        if (btnT) {
            btnT.setAttribute('data-url', urlTpl || '');
            btnT.onclick = function () { plmOpenUrl(urlTpl || ''); };
        }
    }

    // -------------------------------------------------------------------------
    // Open-in-Fusion icon HTML helper
    //
    // Ported from the designReview fork of plmPaletteUtils.js (the only addition
    // that fork made). Now lives in the canonical bridge so there is one copy.
    //
    // Returns an HTML string for a compact icon button carrying data-workspace-id
    // and data-item-id attributes. Consumers add a click-delegation listener
    // (see engine.js) rather than attaching onclick inline.
    // -------------------------------------------------------------------------

    var _OPEN_IN_FUSION_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        + 'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        + '<path d="M10 3h4v4"/><path d="M14 3l-7 7"/>'
        + '<path d="M12 10v3a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 13V5.2A1.2 1.2 0 0 1 3.2 4H6"/>'
        + '</svg>';

    function _escAttr(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    /**
     * Return an HTML string for the compact Open-in-Fusion button.
     * @param {string|number} wsId    — workspace numeric id
     * @param {string|number} itemId  — item numeric id
     * @returns {string} HTML for a <button> element
     */
    function plmOpenInFusionIconHtml(wsId, itemId) {
        return '<button type="button" class="open-in-fusion-icon" title="Open in Fusion" '
            + 'aria-label="Open in Fusion" data-workspace-id="' + _escAttr(wsId) + '" '
            + 'data-item-id="' + _escAttr(itemId) + '">' + _OPEN_IN_FUSION_SVG + '</button>';
    }

    // -------------------------------------------------------------------------
    // Exports
    // -------------------------------------------------------------------------

    global.plmSend                 = plmSend;
    global.plmParse                = plmParse;
    global.plmHandleAsyncResult    = plmHandleAsyncResult;
    global.plmApplyTheme           = plmApplyTheme;
    global.plmRequestTheme         = plmRequestTheme;
    global.plmCreateAuthPoller     = plmCreateAuthPoller;
    global.plmOpenUrl              = plmOpenUrl;
    global.plmShowNoAccess         = plmShowNoAccess;
    global.plmOpenInFusionIconHtml = plmOpenInFusionIconHtml;

}(window));
