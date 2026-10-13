"""Portable state paths and public release version checks, without network."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path('ebook_collector').resolve()))
import app_runtime as runtime
import update_checker as update

with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(Path('portable/WebDownload.exe').resolve())):
    assert runtime.app_root() == Path('portable').resolve()
with patch.object(sys, 'frozen', False, create=True):
    assert runtime.app_root() == Path('ebook_collector').resolve()
release = {'tag_name': 'v1.10.0', 'html_url': update.RELEASES_URL + '/tag/v1.10.0',
           'assets': [{'name': 'WebDownload.exe', 'size': 100,
                       'browser_download_url': 'https://github.com/zc202302-lab/webdownload/releases/download/v1.10.0/WebDownload.exe'},
                      {'name': 'SHA256SUMS.txt',
                       'browser_download_url': 'https://github.com/zc202302-lab/webdownload/releases/download/v1.10.0/SHA256SUMS.txt'}], 'body': 'New release'}
assert update.parse_release(release, '1.9.0')['available']
assert not update.parse_release(release, '1.10.0')['available']
assert not update.parse_release(release, '2.0.0')['available']
for invalid in ({'prerelease': True}, {'draft': True}, {'tag_name': 'latest'},
                {'html_url': 'https://evil.com/download'}, {'assets': []}):
    try: update.parse_release(dict(release, **invalid))
    except ValueError: pass
    else: raise AssertionError('invalid release accepted')
from urllib.error import HTTPError
for code in (404, 403, 429):
    with patch.object(update, 'urlopen', side_effect=HTTPError(update.API_URL, code, 'failure', {}, None)):
        try: update.check_update()
        except RuntimeError as exc: assert str(exc)
        else: raise AssertionError('update error ignored')
print('PASS: portable data paths, numeric version comparisons, release validation and network error handling')
