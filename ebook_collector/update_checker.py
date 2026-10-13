"""Read only public release metadata. Never upload local user state."""
import json
import re
import hashlib
import shutil
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_runtime import VERSION

REPO = 'zc202302-lab/webdownload'
RELEASES_URL = f'https://github.com/{REPO}/releases'
API_URL = f'https://api.github.com/repos/{REPO}/releases/latest'


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


def check_update():
    request = Request(API_URL, headers={'Accept': 'application/vnd.github+json',
                                       'User-Agent': 'WebDownload/' + VERSION})
    try:
        with urlopen(request, timeout=20) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise RuntimeError('更新信息过大')
        return parse_release(json.loads(data))
    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError('尚未发布正式EXE版本，请稍后再检查。') from None
        if exc.code in (403, 429):
            raise RuntimeError('更新服务暂时限制请求，请稍后重试。') from None
        raise RuntimeError(f'更新服务返回HTTP {exc.code}') from None


def download_update(release, root, progress=None):
    """Stage only a hash-verified EXE. The running executable is never touched."""
    root = Path(root)
    if shutil.disk_usage(root).free < release['size'] * 3 + 64 * 1024 * 1024:
        raise RuntimeError('磁盘空间不足，无法下载更新。')
    headers = {'User-Agent': 'WebDownload/' + VERSION}
    with urlopen(Request(release['checksum_url'], headers=headers), timeout=30) as response:
        checksum = response.read(1024 * 1024 + 1)
    if len(checksum) > 1024 * 1024:
        raise RuntimeError('校验文件过大')
    matches = re.findall(r'^([a-fA-F0-9]{64})\s+\*?WebDownload\.exe\s*$', checksum.decode('utf-8-sig'), re.M)
    if len(matches) != 1:
        raise RuntimeError('校验文件缺少唯一的EXE校验值')
    expected = matches[0].lower()
    update_root = root / '.updates'
    update_root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='version-', dir=update_root))
    candidate = folder / 'WebDownload.exe'
    part = candidate.with_suffix('.part')
    digest = hashlib.sha256()
    received = 0
    try:
        with urlopen(Request(release['exe_url'], headers=headers), timeout=30) as response, part.open('wb') as file:
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
