"""Run offline checks in fixture-generation order."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name in ('test_collector.py', 'test_chapters.py', 'test_scroll_body.py', 'test_catalog_clicks.py', 'test_blank_chapters.py', 'test_recipes.py', 'test_desktop.py'):
    print(f'Running {name}', flush=True)
    subprocess.run([sys.executable, str(ROOT / 'tests' / name)], cwd=ROOT, check=True)
print('All offline checks passed.')
