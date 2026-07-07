# Event-handler lifecycle. Ported from the old fusionAddInUtils/event_utils.py.
#
# Fusion event handlers must be kept referenced or they are garbage-collected and
# stop firing. add_handler builds a handler of the correct type for the event,
# subscribes it, and retains a reference (globally or in a caller-supplied list).
# clear_handlers releases the global references at add-in stop.

import sys
from typing import Callable

import adsk.core

from . import log

# Global list holding references to event handlers so they are not released.
_handlers = []


def add_handler(event: adsk.core.Event, callback: Callable, *,
                name: str = None, local_handlers: list = None):
    """Subscribe ``callback`` to ``event`` and retain the handler.

    event          -- the event to connect to.
    callback       -- function invoked with the event args.
    name           -- label used when logging errors from this handler.
    local_handlers -- a caller-owned list to hold the reference; if None, the
                      global list is used (cleared by clear_handlers at stop).

    Returns the created handler.
    """
    module = sys.modules[event.__module__]
    handler_type = module.__dict__[event.add.__annotations__['handler']]
    handler = _create_handler(handler_type, callback, event, name, local_handlers)
    event.add(handler)
    return handler


def clear_handlers():
    """Release the global list of handlers (call at add-in stop)."""
    global _handlers
    _handlers = []


def _create_handler(handler_type, callback: Callable, event: adsk.core.Event,
                    name: str = None, local_handlers: list = None):
    handler = _define_handler(handler_type, callback, name)()
    (local_handlers if local_handlers is not None else _handlers).append(handler)
    return handler


def _define_handler(handler_type, callback, name: str = None):
    name = name or handler_type.__name__

    class Handler(handler_type):
        def __init__(self):
            super().__init__()

        def notify(self, args):
            try:
                callback(args)
            except:
                log.handle_error(name)

    return Handler
