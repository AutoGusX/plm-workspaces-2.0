# PLM Workspaces 2.0 — add-in entry point.
# Delegates to commands.start()/stop(); errors are isolated and logged via core.log.

from . import commands
from .core import log


def run(context):
    try:
        commands.start()
    except:
        log.handle_error('run')


def stop(context):
    try:
        # Release all event handlers this add-in created, then tear down commands.
        from .core import events
        events.clear_handlers()
        commands.stop()
    except:
        log.handle_error('stop')
