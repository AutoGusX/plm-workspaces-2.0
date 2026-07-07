# @action decorator + per-class dispatch table.
#
# A PaletteCommand subclass declares handlers with @action('name'); the base class
# looks up the handler by the incoming action string and dispatches to it. This
# replaces the long if/elif action chains in the old command modules.
#
#   class MyWork(PaletteCommand):
#       @action('getTasks')
#       def get_tasks(self, ctx, data):
#           return {'success': True, 'tasks': [...]}
#
# The decorator simply tags the method; the table is built per class (walking the
# MRO so subclasses inherit the shared actions defined on PaletteCommand).

_ACTION_ATTR = '_plm_action_name'
_ASYNC_ATTR  = '_plm_action_async'


def action(name, async_=False):
    """Mark a method as the handler for the given JS action name.

    async_=True opts the handler into background execution via async_dispatcher:
      - The incomingFromHTML handler returns {pending:true, requestId} immediately.
      - The real result is delivered via palette.sendInfoToHTML('plmAsyncResult').
      - bridge.js resolves the waiting plmSend() Promise transparently.
    Only mark async_ on handlers that are pure HTTP (FmClient calls).  Handlers
    that call Fusion API methods (getTheme, openInFusion, getSelectedComponents …)
    must stay sync because those APIs are main-thread-only.
    """
    def decorator(func):
        setattr(func, _ACTION_ATTR, name)
        if async_:
            setattr(func, _ASYNC_ATTR, True)
        return func
    return decorator


def build_action_map(cls):
    """Return {action_name: function} for a class, walking its MRO so that actions
    declared on base classes (e.g. the shared actions on PaletteCommand) are
    included and subclass overrides win.

    MRO order is most-derived first, so we iterate in reverse and let later (more
    derived) definitions overwrite earlier (base) ones.
    """
    mapping = {}
    for klass in reversed(cls.__mro__):
        for attr_name, attr in vars(klass).items():
            action_name = getattr(attr, _ACTION_ATTR, None)
            if action_name:
                mapping[action_name] = attr
    return mapping
