"""Run on Windows with Python 3.12. Bundle runtime, not user data or browsers."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.platform != 'win32':
    raise SystemExit('Windows EXE must be built on Windows.')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                '--console', '--name', 'WebDownload', '--collect-all', 'playwright',
                '--paths', str(ROOT / 'ebook_collector'),
                str(ROOT / 'ebook_collector' / 'desktop_app.py')], cwd=ROOT, check=True)
exe = ROOT / 'dist' / 'WebDownload.exe'
subprocess.run([str(exe), '--self-test'], cwd=ROOT, check=True, timeout=120)
(exe.parent / 'SHA256SUMS.txt').write_text(hashlib.sha256(exe.read_bytes()).hexdigest() + '  WebDownload.exe\n', encoding='utf-8')
