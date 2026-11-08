"""CI only: publish verified release bytes to a separate public GitHub raw channel."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO='zc202302-lab/webdownload'
BRANCH='update-channel'
ROOT=Path(__file__).resolve().parent

def publish():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_REF')!='refs/heads/main':
        raise RuntimeError('Updates are published only by this repository main workflow')
    import sys
    sys.path.insert(0,str(ROOT/'ebook_collector'))
    from app_runtime import VERSION
    exe=ROOT/'dist/WebDownload.exe'
    digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    if (ROOT/'dist/SHA256SUMS.txt').read_text().split()[0]!=digest:
        raise RuntimeError('Built executable checksum mismatch')
    # Reuse checkout's workflow-owned credential without writing or printing it.
    credential=subprocess.run(['git','config','--get','http.https://github.com/.extraheader'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    env=dict(os.environ,GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',GIT_CONFIG_VALUE_0=credential)
    with tempfile.TemporaryDirectory(prefix='webdownload-update-channel-') as folder:
        work=Path(folder)
        def git(*args):
            result=subprocess.run(['git',*args],cwd=work,env=env,capture_output=True,text=True)
            if result.returncode:
                raise RuntimeError('Update channel git operation failed: '+args[0])
            return result.stdout.strip()
        git('init','-b',BRANCH)
        git('remote','add','origin',f'https://github.com/{REPO}.git')
        old=git('ls-remote','--heads','origin',f'refs/heads/{BRANCH}')
        expected=old.split()[0] if old else ''
        git('config','user.name','github-actions[bot]')
        git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
        shutil.copyfile(exe,work/'WebDownload.exe')
        shutil.copyfile(ROOT/'dist/SHA256SUMS.txt',work/'SHA256SUMS.txt')
        git('add','WebDownload.exe','SHA256SUMS.txt')
        git('commit','-m',f'Verified update payload v{VERSION}')
        asset_commit=git('rev-parse','HEAD')
        tag='v'+VERSION
        prefix=f'https://github.com/{REPO}/releases/download/{tag}/'
        manifest={'tag_name':tag,'html_url':f'https://github.com/{REPO}/releases/tag/{tag}',
                  'asset_commit':asset_commit,'sha256':digest,'source_commit':os.environ['GITHUB_SHA'],
                  'assets':[{'name':'WebDownload.exe','browser_download_url':prefix+'WebDownload.exe','size':exe.stat().st_size},
                            {'name':'SHA256SUMS.txt','browser_download_url':prefix+'SHA256SUMS.txt'}]}
        (work/'update.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        git('add','update.json');git('commit','-m',f'Update manifest v{VERSION}')
        # Two-commit immutable payload + manifest; lease avoids racing another publisher.
        git('push',f'--force-with-lease=refs/heads/{BRANCH}:{expected}','origin',f'HEAD:refs/heads/{BRANCH}')
        print(f'Published independent update channel v{VERSION}; SHA256 {digest}')

if __name__=='__main__':publish()
