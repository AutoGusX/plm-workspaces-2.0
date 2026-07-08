# PLM Workspaces 2.0 — command registry.
#
# Each capability is a PaletteCommand subclass; we instantiate one per command and
# start/stop them, isolating per-command failures so one broken module doesn't block
# the rest (preserved from the old commands/__init__.py behavior).
#
# The OAuth token custom event is registered exactly ONCE here at add-in scope.
# Fusion custom events are process-global per id and are NOT ref-counted, so
# registering per command (as before) meant the 2nd..Nth command's register
# returned None and the first command's stop() unregistered it for everyone.
# We register the shared CustomEvent object once and hand it to every command so
# each subscribes its own handler to the SAME event object.
#
# Panel button order (left-to-right in the PLM panel):
#   My Work → Change Management → Requirements → Engineering Projects → Supplier Packages
# This order is determined by the sequence of start() calls below (panel buttons are
# added in the order commands are started).

import adsk.core

from ..core import async_dispatcher
from ..core import config
from ..core import log
from .login.command import LoginCommand
from .my_work.command import MyWorkCommand
from .change_management.command import ChangeManagementCommand
from .requirements.command import RequirementsCommand
from .engineering_projects.command import EngineeringProjectsCommand
from .supplier_packages.command import SupplierPackagesCommand
from .export_gcode.command import ExportGcodeCommand
from .export_pdf.command import ExportPdfCommand
from .export_to_plm.command import ExportToPlmCommand
from .export_dxf.command import ExportDxfCommand
from .plm_charts.command import PlmChartsCommand
from .export_electronics_bom.command import ExportElectronicsBomCommand

# Instantiate the singletons that back each command.
# Order matters: the login command has no panel button; the remaining commands
# appear in the PLM panel in the order they call add_command_to_plm_panels().
# ExportGcodeCommand has panel='cam' so it appears in the Manufacture workspace
# CAM Manage panel rather than the PLM panel.
_commands = [
    LoginCommand(),
    MyWorkCommand(),
    ChangeManagementCommand(),
    RequirementsCommand(),
    EngineeringProjectsCommand(),
    SupplierPackagesCommand(),
    ExportGcodeCommand(),
    ExportPdfCommand(),
    ExportDxfCommand(),
    ExportToPlmCommand(),
    PlmChartsCommand(),
    ExportElectronicsBomCommand(),
]

# The shared OAuth custom event, registered once at start().
_oauth_event = None


def _label(command):
    return type(command).__name__


def _register_oauth_event():
    """Register the OAuth token custom event once and return the CustomEvent (or None).

    Self-healing: if a prior unclean reload left the id registered, registerCustomEvent
    returns None. We then unregister and re-register so sign-in notifications / auto-open
    don't silently die after a reload.
    """
    global _oauth_event
    try:
        app = adsk.core.Application.get()
        _oauth_event = app.registerCustomEvent(config.OAUTH_CUSTOM_EVENT_ID)
        if _oauth_event is None:
            log.log('commands: OAuth event already registered; re-registering after cleanup',
                    adsk.core.LogLevels.WarningLogLevel)
            try:
                app.unregisterCustomEvent(config.OAUTH_CUSTOM_EVENT_ID)
            except Exception:
                pass
            _oauth_event = app.registerCustomEvent(config.OAUTH_CUSTOM_EVENT_ID)
        log.log(f'commands: OAuth custom event registered (ok={_oauth_event is not None})')
    except Exception:
        _oauth_event = None
        log.handle_error('commands._register_oauth_event')
    return _oauth_event


def _unregister_oauth_event():
    """Unregister the shared OAuth custom event once at add-in teardown."""
    global _oauth_event
    try:
        app = adsk.core.Application.get()
        app.unregisterCustomEvent(config.OAUTH_CUSTOM_EVENT_ID)
    except Exception:
        pass
    _oauth_event = None


def start():
    # Async dispatcher must be running before commands start so any async
    # action that fires during start() (unlikely, but defensive) has a pool.
    async_dispatcher.start()
    oauth_event = _register_oauth_event()
    for command in _commands:
        try:
            command.start(oauth_event=oauth_event)
        except Exception:
            log.handle_error(f'commands.start[{_label(command)}]')


def stop():
    for command in _commands:
        try:
            command.stop()
        except Exception:
            log.handle_error(f'commands.stop[{_label(command)}]')
    _unregister_oauth_event()
    # Stop after commands so any in-flight actions can attempt delivery.
    async_dispatcher.stop()
