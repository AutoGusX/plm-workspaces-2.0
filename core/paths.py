# Stable add-in root path + addin-root resolution.
#
# ALL OS-specific path logic lives here (Windows + macOS). Everything else in the
# add-in must go through these helpers so platform branches never leak elsewhere.
#
# Why a stable path: Fusion 360 runs multiple processes; using __file__ can resolve
# to a per-process temp copy. We hardcode the folder name so every process resolves
# the same tokens/prefs directory.

import os

from . import config


def get_stable_addin_root():
    """Return the canonical add-in root path — identical in every Fusion process.

    Windows: %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\AddIns\\<folder>
    macOS:   ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/<folder>
    """
    folder = config.STABLE_ADDIN_FOLDER_NAME
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA', '')
        return os.path.join(
            appdata, 'Autodesk', 'Autodesk Fusion 360', 'API', 'AddIns', folder
        )
    # macOS
    base = os.path.expanduser('~/Library/Application Support')
    return os.path.join(
        base, 'Autodesk', 'Autodesk Fusion 360', 'API', 'AddIns', folder
    )


def get_addin_root():
    """Return the directory containing this add-in's package (the folder that holds
    PLM Workspaces.py). Resolved from this file's location: core/ -> add-in root.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def to_file_url_path(path):
    """Normalize a filesystem path for use as a palette htmlFileURL.

    Fusion accepts forward-slash paths on both platforms; backslashes from Windows
    os.path.join are converted here so callers never special-case the OS.
    """
    return (path or '').replace('\\', '/')
