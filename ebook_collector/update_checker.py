"""Read only public release metadata. Never upload local user state."""
import json
import re
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
    if not any(a.get('name') == 'WebDownload.exe' for a in assets):
        raise ValueError('该版本尚未提供完整EXE下载')
    return {'version': tag, 'available': version_tuple(tag) > version_tuple(current),
            'url': url, 'notes': str(data.get('body') or '')[:2000]}


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
