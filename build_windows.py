"""Run on Windows with Python 3.12. Bundle runtime, not user data or browsers."""
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.platform != 'win32':
    raise SystemExit('Windows EXE must be built on Windows.')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                '--windowed', '--name', 'WebDownload', '--collect-all', 'playwright',
                '--paths', str(ROOT / 'ebook_collector'),
                str(ROOT / 'ebook_collector' / 'desktop_app.py')], cwd=ROOT, check=True)
exe = ROOT / 'dist' / 'WebDownload.exe'
import tempfile
import json
with tempfile.TemporaryDirectory() as folder:
    result = Path(folder) / 'result.json'
    process = subprocess.run([str(exe), '--self-test', '--self-test-result', str(result)], cwd=ROOT, timeout=120)
    if process.returncode != 0 or not result.exists():
        raise RuntimeError(result.read_text() if result.exists() else 'EXE self-test failed without result')
    assert json.loads(result.read_text())['console'] is False
with tempfile.TemporaryDirectory() as folder:
    result = Path(folder) / 'update-result.json'
    process = subprocess.run([str(exe), '--self-test-update', '--self-test-result', str(result)], cwd=ROOT, timeout=240)
    if process.returncode != 0 or not result.exists():
        raise RuntimeError(result.read_text() if result.exists() else 'EXE update smoke test failed without result')
    assert json.loads(result.read_text())['data_preserved']
(exe.parent / 'SHA256SUMS.txt').write_text(hashlib.sha256(exe.read_bytes()).hexdigest() + '  WebDownload.exe\n', encoding='utf-8')
