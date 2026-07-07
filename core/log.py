# Logging + error handling. Ported from the old fusionAddInUtils/general_utils.py
# with behavior preserved: console print always, file log on error, console log when
# DEBUG. handle_error logs the full traceback (optionally a message box).

import traceback

import adsk.core

from . import config

_app = adsk.core.Application.get()
_ui = _app.userInterface

DEBUG = getattr(config, 'DEBUG', False)


def log(message: str, level: adsk.core.LogLevels = adsk.core.LogLevels.InfoLogLevel,
        force_console: bool = False):
    """Log a message.

    message       -- the message to log.
    level         -- logging severity level.
    force_console -- force the message to the Text Command window regardless of DEBUG.
    """
    # Always print to stdout (only visible through an attached IDE).
    print(message)

    # Persist errors to the Fusion log file.
    if level == adsk.core.LogLevels.ErrorLogLevel:
        _app.log(message, level, adsk.core.LogTypes.FileLogType)

    # Mirror to the Text Command window when debugging.
    if DEBUG or force_console:
        _app.log(message, level, adsk.core.LogTypes.ConsoleLogType)


def handle_error(name: str, show_message_box: bool = False):
    """Log the current exception's traceback under a label.

    name             -- a label for the error site.
    show_message_box -- if True, also surface the error in a message box.
    """
    log('===== Error =====', adsk.core.LogLevels.ErrorLogLevel)
    log(f'{name}\n{traceback.format_exc()}', adsk.core.LogLevels.ErrorLogLevel)
    if show_message_box:
        _ui.messageBox(f'{name}\n{traceback.format_exc()}')
