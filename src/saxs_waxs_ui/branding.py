"""Release identity and portable resource/user-storage locations."""
import os
from pathlib import Path
import sys

APP_NAME = 'XRelay'
VERSION = '1.0.2'
TITLE = f'{APP_NAME} {VERSION}'


def resource_path(relative):
    return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[2]))/'resources'/relative


def artifacts_directory():
    if getattr(sys,'frozen',False):
        return Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'XRelay'
    return Path(__file__).resolve().parents[2]/'artifacts'
