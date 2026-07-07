/**
 * PLM Workspaces 2.0 — theme.js
 *
 * User preferences (theme + text size) and the settings gear popover.
 * Ported from lib/html/plmUiPrefs.js; ALL inline style strings have been
 * moved to web/core/components.css (.plm-settings-*, .plm-segmented).
 *
 * Depends on: bridge.js (loaded first) for plmSend().
 *
 * Exposes window.plmUiPrefs:
 *   .init()                    — load prefs, mount the settings gear, fetch Fusion theme
 *   .get()                     → {theme, textSize}
 *   .set(partial, opts?)       — merge partial prefs, persist, apply
 *   .apply()                   — re-apply body classes from current state
 *   .mountOn(container)        — inject the settings gear into a specific element
 *   .mountAll()                — inject into every .palette-header on the page
 *   .onFusionThemeName(name)   — notify of the Fusion app theme name
 *   .handlePythonMessage(a,d)  → bool  (called by fusionJavaScriptHandler)
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'plm_ui_prefs_v1';
    var THEMES = ['auto', 'light', 'dark'];
    var SIZES  = ['sm', 'md', 'lg', 'xl'];
    var DEFAULTS = { theme: 'auto', textSize: 'md' };

    var current = Object.assign({}, DEFAULTS);
    var fusionIsDark = false;
    var popoverOpen = false;

    // -------------------------------------------------------------------------
    // Sanitize / read / write prefs
    // -------------------------------------------------------------------------

    function sanitize(prefs) {
        var out = Object.assign({}, DEFAULTS);
        if (prefs && typeof prefs === 'object') {
            if (THEMES.indexOf(prefs.theme) !== -1)    out.theme    = prefs.theme;
            if (SIZES.indexOf(prefs.textSize) !== -1)  out.textSize = prefs.textSize;
        }
        return out;
    }

    function readLocal() {
        try {
            var raw = localStorage.getItem(STORAGE_KEY);
            if (raw) return sanitize(JSON.parse(raw));
        } catch (e) { /* ignore */ }
        return null;
    }

    function writeLocal(prefs) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs)); } catch (e) { /* ignore */ }
    }

    // -------------------------------------------------------------------------
    // Apply body classes
    // -------------------------------------------------------------------------

    function apply() {
        var body = document.body;
        if (!body) return;

        // Text-size class
        SIZES.forEach(function (s) { body.classList.remove('pref-text-' + s); });
        body.classList.add('pref-text-' + current.textSize);

        // Preference-theme marker (informational only)
        THEMES.forEach(function (t) { body.classList.remove('pref-theme-' + t); });
        body.classList.add('pref-theme-' + current.theme);

        // Resolved applied-theme class (consumed by tokens.css)
        var applied = (current.theme === 'auto')
            ? (fusionIsDark ? 'dark' : 'light')
            : current.theme;
        body.classList.remove('theme-applied-light', 'theme-applied-dark');
        body.classList.add('theme-applied-' + applied);

        syncPopover();
    }

    // -------------------------------------------------------------------------
    // Fusion theme
    // -------------------------------------------------------------------------

    function onFusionThemeName(name) {
        fusionIsDark = /dark/i.test(String(name || ''));
        apply();
    }

    function requestFusionTheme() {
        if (typeof plmSend !== 'function') return;
        try {
            var p = plmSend('getTheme', {});
            if (!p || typeof p.then !== 'function') return;
            p.then(function (r) {
                var d = {};
                try { d = typeof r === 'string' ? JSON.parse(r) : (r || {}); } catch (e) {}
                onFusionThemeName(d.theme || d.themeName || d.name || '');
            }).catch(function () { /* ignore */ });
        } catch (e) { /* ignore */ }
    }

    // -------------------------------------------------------------------------
    // Set prefs
    // -------------------------------------------------------------------------

    function setPrefs(partial, opts) {
        opts = opts || {};
        current = sanitize(Object.assign({}, current, partial || {}));
        writeLocal(current);
        if (!opts.skipPython && typeof plmSend === 'function') {
            try { plmSend('setUiPrefs', current); } catch (e) { /* ignore */ }
        }
        apply();
    }

    // -------------------------------------------------------------------------
    // Load initial prefs (localStorage fast-path + Python authoritative)
    // -------------------------------------------------------------------------

    function loadInitial() {
        var local = readLocal();
        if (local) { current = local; apply(); }

        if (typeof plmSend === 'function') {
            try {
                var p = plmSend('getUiPrefs', {});
                if (p && typeof p.then === 'function') {
                    p.then(function (r) {
                        var d = {};
                        try { d = typeof r === 'string' ? JSON.parse(r) : (r || {}); } catch (e) {}
                        var incoming = sanitize((d.prefs || d));
                        if (incoming.theme !== current.theme || incoming.textSize !== current.textSize) {
                            current = incoming;
                            writeLocal(current);
                            apply();
                        }
                    }).catch(function () { /* ignore */ });
                }
            } catch (e) { /* ignore */ }
        }

        requestFusionTheme();
    }

    // Cross-palette live sync (another palette writes to localStorage → storage event fires here)
    window.addEventListener('storage', function (e) {
        if (e.key !== STORAGE_KEY) return;
        try {
            var incoming = sanitize(JSON.parse(e.newValue || '{}'));
            current = incoming;
            apply();
        } catch (err) { /* ignore */ }
    });

    // -------------------------------------------------------------------------
    // Settings popover (DOM built in JS, styled by components.css)
    // -------------------------------------------------------------------------

    function buildSegmented(group, values, labels) {
        var wrap = document.createElement('div');
        wrap.className = 'plm-segmented';
        wrap.setAttribute('data-group', group);
        values.forEach(function (v, i) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.setAttribute('data-value', v);
            btn.textContent = labels[i];
            wrap.appendChild(btn);
        });
        wrap.addEventListener('click', function (e) {
            var t = e.target;
            while (t && t !== wrap && !t.hasAttribute('data-value')) { t = t.parentNode; }
            if (!t || t === wrap) return;
            var partial = {};
            partial[group] = t.getAttribute('data-value');
            setPrefs(partial);
        });
        return wrap;
    }

    function buildPopover() {
        var pop = document.createElement('div');
        pop.className = 'plm-settings-popover';
        pop.setAttribute('role', 'dialog');
        pop.setAttribute('aria-label', 'Display settings');

        function makeGroup(labelText, segmented) {
            var g = document.createElement('div');
            g.className = 'plm-settings-group';
            var lbl = document.createElement('label');
            lbl.className = 'plm-settings-label';
            lbl.textContent = labelText;
            g.appendChild(lbl);
            g.appendChild(segmented);
            return g;
        }

        pop.appendChild(makeGroup('Theme',
            buildSegmented('theme', THEMES, ['Auto', 'Light', 'Dark'])));
        pop.appendChild(makeGroup('Text Size',
            buildSegmented('textSize', SIZES, ['S', 'M', 'L', 'XL'])));
        return pop;
    }

    function syncPopover() {
        var pop = document.querySelector('.plm-settings-popover');
        if (!pop) return;
        pop.querySelectorAll('.plm-segmented').forEach(function (g) {
            var group = g.getAttribute('data-group');
            g.querySelectorAll('button').forEach(function (btn) {
                btn.classList.toggle('active', btn.getAttribute('data-value') === current[group]);
            });
        });
    }

    function closePopover() {
        var pop = document.querySelector('.plm-settings-popover');
        if (pop) pop.classList.remove('open');
        popoverOpen = false;
    }

    function togglePopover() {
        var pop = document.querySelector('.plm-settings-popover');
        if (!pop) return;
        if (popoverOpen) { closePopover(); return; }
        pop.classList.add('open');
        popoverOpen = true;
        syncPopover();
    }

    function onDocumentClick(e) {
        if (!popoverOpen) return;
        var pop = document.querySelector('.plm-settings-popover');
        var btn = document.querySelector('.plm-settings-btn');
        if (pop && (pop.contains(e.target) || (btn && btn.contains(e.target)))) return;
        closePopover();
    }

    // -------------------------------------------------------------------------
    // Mount the settings gear button
    // -------------------------------------------------------------------------

    /** Inject a settings gear button into container. Safe to call multiple times;
     *  only mounts once per container. */
    function mountSettingsButton(container) {
        if (!container) return;
        if (container.querySelector('.plm-settings-btn')) return;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'plm-settings-btn';
        btn.title = 'Display settings';
        btn.setAttribute('aria-label', 'Display settings');
        btn.innerHTML = '<span class="sr-only">Settings</span>&#9881;';
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            togglePopover();
        });
        container.appendChild(btn);

        // Attach popover once per document
        if (!document.querySelector('.plm-settings-popover')) {
            document.body.appendChild(buildPopover());
            document.addEventListener('click', onDocumentClick, true);
            document.addEventListener('keydown', function (e) {
                if (e.key === 'Escape' && popoverOpen) closePopover();
            });
        }
    }

    /** Mount on every .palette-header (and any [data-plm-settings-mount]) on the page. */
    function mountAll() {
        document.querySelectorAll('.palette-header, [data-plm-settings-mount]')
            .forEach(function (el) { mountSettingsButton(el); });
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    window.plmUiPrefs = {
        init: function () {
            loadInitial();
            mountAll();
        },
        get: function () { return Object.assign({}, current); },
        set: setPrefs,
        apply: apply,
        mountOn: mountSettingsButton,
        mountAll: mountAll,
        onFusionThemeName: onFusionThemeName,
        /** Called from fusionJavaScriptHandler for 'uiPrefsChanged' push messages. */
        handlePythonMessage: function (action, data) {
            if (action !== 'uiPrefsChanged') return false;
            try {
                var d = typeof data === 'string' ? JSON.parse(data) : (data || {});
                current = sanitize(d.prefs || d);
                writeLocal(current);
                apply();
            } catch (e) { /* ignore */ }
            return true;
        }
    };

    // Auto-init when included as a standalone script
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { window.plmUiPrefs.init(); });
    } else {
        window.plmUiPrefs.init();
    }

})();
