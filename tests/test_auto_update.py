"""Verified staged downloads, swap rollback and no user-data changes."""
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path('ebook_collector').resolve()))
import update_checker as checks
import auto_updater as updater

payload = b'MZnew-Windows-program'
expected = hashlib.sha256(payload).hexdigest()
release = {'size': len(payload), 'exe_url': 'https://github.com/example/program',
           'checksum_url': 'https://github.com/example/checksum'}
def response(request, **kwargs):
    if request.full_url == release['checksum_url']:
        return io.BytesIO((expected + '  WebDownload.exe\n').encode())
    return io.BytesIO(payload)

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    target = root / 'Program.exe'
    target.write_bytes(b'MZold-Windows-program')
    state = root / 'settings.json'
    state.write_text('{"output_dir":"D:/书库"}', encoding='utf-8')
    unchanged = state.read_bytes()
    with patch.object(checks, 'urlopen', side_effect=response):
        progress = []
        candidate, digest = checks.download_update(release, root, lambda done, total: progress.append((done, total)))
    assert candidate.read_bytes() == payload and digest == expected
    assert target.read_bytes() == b'MZold-Windows-program' and state.read_bytes() == unchanged
    assert progress[-1] == (len(payload), len(payload))
    with patch.object(updater.subprocess, 'run') as smoke:
        helper, plan = updater.prepare_update(candidate, digest, target=target, parent_pid=123)
        assert smoke.call_args.kwargs['check'] and helper.read_bytes() == target.read_bytes()
        assert json.loads(plan.read_text())['parent_pid'] == 123
    backup = candidate.parent / 'previous.exe'
    # Failure during the second rename must restore the previous program.
    real_replace = updater.replace_retry
    def failing_replace(source, destination):
        if Path(source) == candidate:
            raise PermissionError('replacement denied')
        real_replace(source, destination)
    with patch.object(updater, 'replace_retry', side_effect=failing_replace):
        try: updater.install_candidate(target, candidate, backup, expected)
        except PermissionError: pass
        else: raise AssertionError('replacement failure ignored')
    assert target.read_bytes() == b'MZold-Windows-program' and candidate.exists()
    updater.install_candidate(target, candidate, backup, expected)
    assert target.read_bytes() == payload and backup.read_bytes() == b'MZold-Windows-program'
    assert state.read_bytes() == unchanged
    # Failed checksum/size downloads must remove the incomplete candidate.
    def tampered(request, **kwargs):
        if request.full_url == release['checksum_url']:
            return response(request)
        return io.BytesIO(b'MZcorrupted-program!')
    with patch.object(checks, 'urlopen', side_effect=tampered):
        try: checks.download_update(release, root)
        except RuntimeError: pass
        else: raise AssertionError('tampered update accepted')
    assert not list((root / '.updates').rglob('*.part'))
    # Installation must reject even a previously downloaded file if modified.
    fake = root / 'changed.exe'; fake.write_bytes(b'tampered')
    try: updater.install_candidate(target, fake, backup, expected)
    except ValueError: pass
    else: raise AssertionError('changed candidate installed')
    assert target.read_bytes() == payload
    malformed = dict(json.loads(plan.read_text()), candidate=str(root / 'outside.exe'))
    plan.write_text(json.dumps(malformed))
    try: updater.apply_update(plan)
    except ValueError: pass
    else: raise AssertionError('outside stage candidate accepted')
print('PASS: verified download, progress, tamper rejection, EXE-only replacement, rollback and data preservation')
