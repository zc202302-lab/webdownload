"""Exercise publication against real local Git repositories, without any network."""
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('channel_publisher','publish_update_channel.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
run=subprocess.run
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);source=root/'source';source.mkdir();(source/'dist').mkdir();(source/'ebook_collector').mkdir()
    (source/'ebook_collector/app_runtime.py').write_text("VERSION='9.0.0'\n")
    (source/'settings.json').write_text('private fixture')
    remote=root/'remote.git'
    run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    run(['git','init'],cwd=source,check=True,capture_output=True)
    run(['git','config','http.https://github.com/.extraheader','X-Test: publication'],cwd=source,check=True)
    publisher.ROOT=source
    def local(command,**kwargs):
        if command[:4]==['git','remote','add','origin']:
            command=command[:4]+[str(remote)]
        return run(command,**kwargs)
    for payload in (b'MZverified one',b'MZverified two'):
        digest=hashlib.sha256(payload).hexdigest()
        (source/'dist/WebDownload.exe').write_bytes(payload)
        (source/'dist/SHA256SUMS.txt').write_text(digest+'  WebDownload.exe\n')
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':publisher.REPO,'GITHUB_REF':'refs/heads/main','GITHUB_SHA':'1'*40}),patch.object(publisher.subprocess,'run',side_effect=local):
            publisher.publish()
        def git(*args):return run(['git','--git-dir',str(remote),*args],check=True,capture_output=True).stdout
        manifest=json.loads(git('show','update-channel:update.json'))
        assert manifest['sha256']==digest
        assert git('show',manifest['asset_commit']+':WebDownload.exe')==payload
        assert set(git('ls-tree','--name-only','update-channel').decode().split())=={'WebDownload.exe','SHA256SUMS.txt','update.json'}
print('PASS: real Git channel publication, immutable matching bytes, repeat publication lease and user-data exclusion')
