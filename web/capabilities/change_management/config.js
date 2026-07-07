/**
 * PLM Workspaces 2.0 — Change Management capability config
 *
 * Sets window._plmCfg before engine.js is loaded (per ARCHITECTURE §5.1 load order).
 * This is the ONLY file that differs between workspace capabilities.
 *
 * _plmCfg keys:
 *   workspaceKey      — identifies the workspace on the Python side (WorkspaceCommand subclass)
 *   title             — human-readable name shown in the palette header h1
 *   detailAction      — generic Python @action name for fetching + enriching an item
 *   showAffectedItems — show Affected Items tab + count column in list view (drill-down only)
 *   viewSelectKey     — localStorage key for the selected tableau
 *   newItemLabel      — label for the "+ New" button (shown in drill-down scope only)
 *   scopes            — enable the unified multi-workspace scope selector
 *   scopesAction      — Python action that returns {scopes: [{key, label, ...}]}
 *   unifiedAction     — Python action that returns the merged table {rows, columns, ...}
 *   unifiedDefaultKey — the key value that means "show the unified (all) view"
 *   hooks             — optional extension points for custom rendering/behaviour
 */
window._plmCfg = {
    workspaceKey:       'changeManagement',
    title:              'Change Management',
    detailAction:       'getItemDetail',
    showAffectedItems:  true,
    viewSelectKey:      'cm_tableau_id',
    newItemLabel:       'New Change Order',

    // Unified multi-workspace scope selector
    scopes:             true,
    scopesAction:       'getChangeScopes',
    unifiedAction:      'getUnifiedChangeRecords',
    unifiedDefaultKey:  'all',

    hooks: {
        /** buildCustomFormField(fieldId, def, currentVal) → element|null
         *  Return a DOM element to use instead of the default input, or null to
         *  fall through to the engine's built-in field renderer. */
        buildCustomFormField: null,

        /** renderCustomDetailField(el, val, def) → bool
         *  Fill el with rendered value for val; return true to suppress default. */
        renderCustomDetailField: null,

        /** onDetailToolbarExtra(toolbar, item, wsId) → void
         *  Append extra buttons to the detail view toolbar. */
        onDetailToolbarExtra: null,

        /** afterSave(itemId, wsId) → void
         *  Called after a successful createItem / updateItem response.
         *  Default behaviour: navigate to detail view of the saved item. */
        afterSave: null
    }
};
