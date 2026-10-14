"""Keep user state beside the portable EXE, never in PyInstaller's temp folder."""
import sys
from pathlib import Path

VERSION = '1.0.7'


def app_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent

