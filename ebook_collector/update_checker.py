"""Read only public release metadata. Never upload local user state."""
import json
import re
import hashlib
import shutil
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from http.client import HTTPException
from urllib.request import Request, urlopen

from app_runtime import VERSION

REPO = 'zc202302-lab/webdownload'
RELEASES_URL = f'https://github.com/{REPO}/releases'
API_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
CHANNEL_URL = f'https://raw.githubusercontent.com/{REPO}/update-channel/update.json'


def version_tuple(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError('发布版本号格式不正确')
    return tuple(map(int, match.groups()))


def parse_release(data, current=VERSION):
    if data.get('draft') or data.get('prerelease'):
        raise ValueError('不是正式发布版本')
    tag = data.get('tag_name', '')
    url = data.get('html_url', '')
    if not url.startswith(RELEASES_URL + '/tag/'):
        raise ValueError('更新地址不属于本项目')
    assets = data.get('assets', [])
    prefix = f'https://github.com/{REPO}/releases/download/{tag}/'
    selected = {}
    for name in ('WebDownload.exe', 'SHA256SUMS.txt'):
        matches = [a for a in assets if a.get('name') == name]
        if len(matches) != 1 or matches[0].get('browser_download_url') != prefix + name:
            raise ValueError('发布版的程序或校验文件地址无效')
        selected[name] = matches[0]
    size = selected['WebDownload.exe'].get('size', 0)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 300 * 1024 * 1024:
        raise ValueError('发布程序大小无效')
    return {'version': tag, 'available': version_tuple(tag) > version_tuple(current),
            'url': url, 'notes': str(data.get('body') or '')[:2000],
            'exe_url': selected['WebDownload.exe']['browser_download_url'],
            'checksum_url': selected['SHA256SUMS.txt']['browser_download_url'], 'size': size}


def open_remote(request, timeout=30):
    """Retry transient transport/server failures once; never retry authorization errors."""
    for attempt in range(2):
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code not in (408,500,502,503,504) or attempt:
                raise
        except (URLError, TimeoutError, OSError, HTTPException):
            if attempt:raise


def fallback_release(reason):
    try:
        return check_public_release()
    except Exception as fallback:
        raise RuntimeError(f'{reason}，正式发布页备用查询也失败：{fallback}。请使用离线更新包替换EXE，保留原文件夹其他内容。') from None


def check_public_release():
    """Public web fallback avoids the unauthenticated API quota."""
    headers={'User-Agent':'WebDownload/'+VERSION}
    with open_remote(Request(RELEASES_URL+'/latest',headers=headers),timeout=20) as response:
        url=response.geturl()
    match=re.fullmatch(re.escape(RELEASES_URL)+r'/tag/(v?\d+\.\d+\.\d+)',url)
    if not match:
        raise RuntimeError('备用更新入口未返回本项目正式版本。')
    tag=match[1]
    prefix=f'https://github.com/{REPO}/releases/download/{tag}/'
    # Read real asset length, rather than guessing metadata or trusting a mirror.
    with open_remote(Request(prefix+'WebDownload.exe',headers=headers,method='HEAD'),timeout=30) as response:
        length=response.headers.get('Content-Length','')
    if not re.fullmatch(r'\d+',length):
        raise RuntimeError('备用更新入口未提供程序大小，请手动下载正式发布版。')
    return parse_release({'tag_name':tag,'html_url':url,'assets':[
        {'name':'WebDownload.exe','browser_download_url':prefix+'WebDownload.exe','size':int(length)},
        {'name':'SHA256SUMS.txt','browser_download_url':prefix+'SHA256SUMS.txt'}]})


def check_legacy_update():
    request = Request(API_URL, headers={'Accept': 'application/vnd.github+json',
                                       'User-Agent': 'WebDownload/' + VERSION})
    try:
        with open_remote(request, timeout=20) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise RuntimeError('更新信息过大')
        return parse_release(json.loads(data))
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError('尚未发布正式EXE版本，请稍后再检查。') from None
        if exc.code in (403,429,408,500,502,503,504):
            return fallback_release(f'GitHub API查询受限或暂不可用（HTTP {exc.code}）')
        raise RuntimeError(f'更新服务返回HTTP {exc.code}') from None
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        return fallback_release(f'GitHub API连接失败（{type(exc).__name__}）')


def parse_channel(data, current=VERSION):
    release=parse_release(data,current)
    commit=data.get('asset_commit','')
    digest=data.get('sha256','')
    if not re.fullmatch(r'[0-9a-f]{40}',commit) or not re.fullmatch(r'[0-9a-f]{64}',digest):
        raise ValueError('更新通道的固定版本或校验值无效')
    release['fallback_exe_url']=release['exe_url']
    release['fallback_checksum_url']=release['checksum_url']
    prefix=f'https://raw.githubusercontent.com/{REPO}/{commit}/'
    release.update(exe_url=prefix+'WebDownload.exe',checksum_url=prefix+'SHA256SUMS.txt',sha256=digest)
    return release


def check_channel():
    with open_remote(Request(CHANNEL_URL,headers={'User-Agent':'WebDownload/'+VERSION,'Cache-Control':'no-cache'}),timeout=15) as response:
        data=response.read(1024*1024+1)
    if len(data)>1024*1024:raise RuntimeError('更新通道信息过大')
    return parse_channel(json.loads(data))


def check_update():
    try:
        return check_channel()
    except Exception as channel_error:
        try:
            return check_legacy_update()
        except Exception as legacy_error:
            raise RuntimeError(f'独立更新通道失败：{channel_error}；原发布通道失败：{legacy_error}。当前程序未修改。') from None


def download_update(release, root, progress=None):
    try:
        return download_update_one(release,root,progress)
    except (URLError,TimeoutError,OSError,HTTPException) as primary:
        if not release.get('fallback_exe_url'):
            raise RuntimeError(f'下载更新文件失败：{primary}。当前程序未修改。') from None
        alternate=dict(release,exe_url=release['fallback_exe_url'],checksum_url=release['fallback_checksum_url'])
        try:
            return download_update_one(alternate,root,progress)
        except Exception as secondary:
            raise RuntimeError(f'独立通道下载失败：{primary}；备用通道下载失败：{secondary}。当前程序未修改。') from None


def download_update_one(release, root, progress=None):
    """Stage only a hash-verified EXE. The running executable is never touched."""
    root = Path(root)
    if shutil.disk_usage(root).free < release['size'] * 3 + 64 * 1024 * 1024:
        raise RuntimeError('磁盘空间不足，无法下载更新。')
    headers = {'User-Agent': 'WebDownload/' + VERSION}
    with open_remote(Request(release['checksum_url'], headers=headers), timeout=30) as response:
        checksum = response.read(1024 * 1024 + 1)
    if len(checksum) > 1024 * 1024:
        raise RuntimeError('校验文件过大')
    matches = re.findall(r'^([a-fA-F0-9]{64})\s+\*?WebDownload\.exe\s*$', checksum.decode('utf-8-sig'), re.M)
    if len(matches) != 1:
        raise RuntimeError('校验文件缺少唯一的EXE校验值')
    expected = matches[0].lower()
    if release.get('sha256') and release['sha256']!=expected:
        raise RuntimeError('更新清单与校验文件不一致，已拒绝更新。')
    update_root = root / '.updates'
    update_root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='version-', dir=update_root))
    candidate = folder / 'WebDownload.exe'
    part = candidate.with_suffix('.part')
    digest = hashlib.sha256()
    received = 0
    try:
        with open_remote(Request(release['exe_url'], headers=headers), timeout=30) as response, part.open('wb') as file:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                received += len(chunk)
                if received > release['size']:
                    raise RuntimeError('更新程序大小与发布信息不符')
                digest.update(chunk)
                file.write(chunk)
                if progress:
                    progress(received, release['size'])
        if received != release['size'] or digest.hexdigest() != expected:
            raise RuntimeError('更新下载不完整或SHA256校验失败，原程序未修改。')
        with part.open('rb') as file:
            if file.read(2) != b'MZ':
                raise RuntimeError('更新文件不是Windows EXE')
        part.replace(candidate)
        return candidate, expected
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

