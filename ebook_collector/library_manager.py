import hashlib
import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from pypdf import PdfReader

DEFAULT_SETTINGS = {'output_dir': 'output', 'reserve_gb': 20, 'years': 'all', 'start_year': None, 'keep_page_images': False, 'reading_mode': 'chapters', 'chapter_min_short_edge': 1000, 'chapter_min_long_edge': 1400, 'chapter_wait_seconds': 300}


class StorageFull(RuntimeError):
    pass


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:80].rstrip(' .') or '未命名'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', value, re.I):
        value = '_' + value
    return value


def book_id(source):
    parsed = urlparse(source)
    return hashlib.sha256((parsed.netloc + parsed.path + '?' + parsed.query).encode()).hexdigest()[:16]


def validate_pdf(path, expected=None):
    reader = PdfReader(str(path), strict=True)
    count = len(reader.pages)
    if count < 1 or (expected is not None and count != expected):
        raise ValueError(f'PDF页数不正确：实际{count}，预期{expected}')
    for page in reader.pages:
        if not page.get('/Contents'):
            raise ValueError('PDF存在没有内容的页面')
    return count


def read_settings(root):
    path = Path(root) / 'settings.json'
    if not path.exists():
        save_json(path, DEFAULT_SETTINGS)
    settings = dict(DEFAULT_SETTINGS, **json.loads(path.read_text(encoding='utf-8')))
    if settings['reading_mode'] not in ('auto','chapters'):
        raise ValueError('reading_mode 应为 auto 或 chapters')
    if float(settings['chapter_wait_seconds']) < 10:
        raise ValueError('chapter_wait_seconds至少为10秒')
    for key in ('chapter_min_short_edge','chapter_min_long_edge'):
        if isinstance(settings[key], bool) or int(settings[key]) < 1:
            raise ValueError(f'{key} 必须为正整数')
    reserve = float(settings['reserve_gb'])
    if reserve < 1:
        raise ValueError('reserve_gb 至少为1，建议保持20')
    if settings['years'] != 'all' and (not isinstance(settings['years'], list) or not all(re.fullmatch(r'\d{4}', str(y)) for y in settings['years'])):
        raise ValueError('years 应为 "all" 或年份数组，例如 [2024]')
    start = settings.get('start_year')
    if start is not None:
        if isinstance(start, bool) or not re.fullmatch(r'(19|20)\d{2}', str(start)):
            raise ValueError('start_year 应为1900—2099年的年份或null')
        settings['start_year'] = int(start)
    output = Path(settings['output_dir']).expanduser()
    settings['output_path'] = output if output.is_absolute() else Path(root) / output
    return settings


class Library:
    def __init__(self, root, reserve_gb=20):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.reserve = int(float(reserve_gb) * 1_000_000_000)
        self.db_path = self.root / '书库记录.json'
        self.db = json.loads(self.db_path.read_text(encoding='utf-8')) if self.db_path.exists() else {'version': 1, 'books': {}, 'years': {}}
        self.import_legacy()

    def import_legacy(self):
        known = {b.get('pdf') for b in self.db['books'].values()}
        for marker in self.root.glob('*/complete.json'):
            try:
                metadata = json.loads(marker.read_text(encoding='utf-8'))
                path = marker.parent / Path(metadata['pdf']).name
                relative = path.relative_to(self.root).as_posix()
                if relative in known or not path.is_file():
                    continue
                pages = validate_pdf(path, metadata['pages'])
                year = re.search(r'(19\d{2}|20\d{2})年', path.stem)
                if not year:
                    continue
                digest = file_hash(path)
                identifier = 'legacy_' + digest[:16]
                self.db['books'][identifier] = {'id': identifier, 'year': int(year.group(1)), 'title': path.stem, 'status': '已完成', 'pdf': relative, 'pages': pages, 'bytes': path.stat().st_size, 'sha256': digest, 'legacy': True}
                known.add(relative)
            except Exception:
                continue

    def check_space(self, extra=0):
        free = shutil.disk_usage(self.root).free
        if free < self.reserve + int(extra):
            raise StorageFull(f'磁盘可用{free/1e9:.2f}GB，需保留{self.reserve/1e9:.0f}GB及本次写入空间；进度已保留。')
        return free

    def paths(self, task):
        title = safe_name(task['title'])
        match = re.search(r'《([^》]+)》', task['title'])
        journal = safe_name(match.group(1) if match else '其他刊物')[:30]
        pdf = self.root / str(task['year']) / journal / f'{title}_{task["id"][:6]}.pdf'
        work = self.root / '_work' / task['id']
        return pdf, work

    def register(self, task):
        existing = self.db['books'].setdefault(task['id'], dict(task, status='待处理'))
        existing.update({k: task[k] for k in ('title', 'year', 'source')})
        return existing

    def done(self, task):
        item = self.register(task)
        if item.get('status') != '已完成':
            normalize = lambda title: re.sub(r'\s+', '', title).removesuffix('版')
            matches = [(key, b) for key, b in self.db['books'].items() if b.get('legacy') and normalize(b['title']) == normalize(task['title'])]
            if len(matches) == 1:
                key, old = matches[0]
                item.update({k: old[k] for k in ('pdf', 'pages', 'bytes', 'sha256', 'status')})
                del self.db['books'][key]
        if item.get('status')=='已完成' and item.get('empty_record') and not item.get('pdf'):
            record=self.root / item['empty_record']
            return record.is_file() and file_hash(record)==item.get('empty_record_sha256')
        if item.get('status') != '已完成' or not item.get('pdf'):
            return False
        path = self.root / item['pdf']
        if not path.is_file() or file_hash(path) != item.get('sha256'):
            item['status'] = '文件缺失或已改动'
            return False
        return True

    def update(self, task, status, **values):
        item = self.register(task)
        item.update(status=status, updated=datetime.now().isoformat(timespec='seconds'), **values)
        self.flush()

    def complete(self, task, pdf, count):
        self.update(task, '已完成', pdf=pdf.relative_to(self.root).as_posix(), pages=count, bytes=pdf.stat().st_size, sha256=file_hash(pdf), reason='')

    def flush(self):
        save_json(self.db_path, self.db)
        self.write_index()

    def write_index(self):
        rows = []
        for b in sorted(self.db['books'].values(), key=lambda x: (-int(x['year']), x['title'])):
            title = html.escape(b['title'])
            path = b.get('pdf', '')
            if b['status'] == '已完成' and path:
                title = f'<a href="{quote(path, safe="/")}" target="_blank">{title}</a>'
            rows.append(f'<tr><td>{html.escape(str(b["year"]))}</td><td>{title}</td><td>{html.escape(b["status"])}</td><td>{b.get("pages", "")}</td><td>{b.get("bytes", 0)/1e6:.1f}</td><td>{html.escape(b.get("reason", ""))}</td></tr>')
        count = sum(b['status'] == '已完成' for b in self.db['books'].values())
        body = ''.join(rows)
        content = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>电子书资料库</title><style>body{font-family:system-ui,sans-serif;background:#f4f6f8;color:#203047;max-width:1200px;margin:auto;padding:24px}h1{font-size:26px}input{padding:12px;width:min(90%,650px);border:1px solid #ccd5dd;border-radius:8px}table{width:100%;border-collapse:collapse;background:white;margin-top:20px}td,th{padding:14px;text-align:left;border-bottom:1px solid #eee}a{color:#126ea1;text-decoration:none}small{color:#64748b}@media(max-width:600px){td,th{padding:8px;font-size:14px}}</style><h1>电子书资料库</h1>'''
        content += f'<p>已完成 {count} 本 · 按年份和刊物分类</p><input id="search" placeholder="搜索年份、书名或状态"><p><small>点击书名打开PDF。复制资料库时保留整个文件夹，方便目录链接继续使用。</small></p><div style="overflow:auto"><table><thead><tr><th>年份</th><th>书名</th><th>状态</th><th>页数</th><th>MB</th><th>说明</th></tr></thead><tbody>{body}</tbody></table></div>'
        content += '<script>document.getElementById("search").addEventListener("input",function(){let q=this.value.toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))})</script></html>'
        target = self.root / '书库目录.html'
        tmp = target.with_suffix('.tmp')
        tmp.write_text(content, encoding='utf-8')
        tmp.replace(target)
