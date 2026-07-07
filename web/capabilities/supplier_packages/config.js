/**
 * PLM Workspaces 2.0 — Supplier Packages capability config
 *
 * Sets window._plmCfg before engine.js is loaded (per ARCHITECTURE §5.1 load order).
 * This is the ONLY file that differs between workspace capabilities.
 *
 * _plmCfg keys:
 *   workspaceKey      — identifies the workspace on the Python side (WorkspaceCommand subclass)
 *   title             — human-readable name shown in the palette header h1
 *   detailAction      — generic Python @action name for fetching + enriching an item
 *   showAffectedItems — show Affected Items tab + count column in list view
 *   viewSelectKey     — localStorage key for the selected tableau
 *   newItemLabel      — label for the "+ New" button
 *   hooks             — optional extension points for custom rendering/behaviour
 */
window._plmCfg = {
    workspaceKey:       'supplierPackages',
    title:              'Supplier Packages',
    detailAction:       'getItemDetail',
    showAffectedItems:  false,
    viewSelectKey:      'sp_tableau_id',
    newItemLabel:       'New Supplier Package',

    hooks: {
        /** buildCustomFormField(fieldId, def, currentVal) → element|null */
        buildCustomFormField: null,

        /** renderCustomDetailField(el, val, def) → bool */
        renderCustomDetailField: null,

        /** onDetailToolbarExtra(toolbar, item, wsId) → void */
        onDetailToolbarExtra: null,

        /** afterSave(itemId, wsId) → void */
        afterSave: null
    }
};
