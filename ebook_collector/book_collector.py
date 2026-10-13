"""Books list: scroll discovery, verified arrow navigation and modal close."""
import html
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import sync_playwright
from chapter_reader import download_best
from collector import ROOT, build_pdf, find_reader, launch_browser, page_number, verified
from library_manager import Library, file_hash, read_settings, safe_name, save_json, validate_pdf

LIST_URL = 'https://www.easteat.com/book_list.php?category=books'


def book_identifier(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    identifier = query.get('id', [''])[0]
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('easteat.com', 'www.easteat.com')
            or parsed.path != '/magazine_detail.php' or query.get('type') != ['detail']
            or not identifier.isdigit()):
        raise ValueError('电子书详情链接异常，已停止。')
    return identifier


def list_snapshot(page):
    return page.evaluate(r'''() => {
      const visible=e=>!!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
      const more=document.querySelector('#loadMore');
      const loading=document.querySelector('#loading');
      const root=document.scrollingElement;
      return {items:[...document.querySelectorAll('#magazineList .magazine-item')].map(e=>({
        title:e.querySelector('h3')?.textContent.trim() || '',source:e.querySelector('a.detail-btn')?.href || ''})),
        more_present:!!more,more:visible(more),loading:visible(loading),
        error:visible(more) && /失败|错误/.test(more.textContent),
        bottom:root.scrollTop+root.clientHeight>=root.scrollHeight-5,height:root.scrollHeight};
    }''')


def discover_books(page, store, settings):
    timeout = max(300, float(settings.get('chapter_wait_seconds', 300)))
    changed = time.monotonic()
    end_since = None
    previous = None
    found = {}
    store.db['scan_complete'] = False
    store.flush()
    while True:
        store.storage.check_space()
        snapshot = list_snapshot(page)
        if snapshot['error']:
            raise RuntimeError('电子书列表加载失败，扫描未完成，已停止。')
        for item in snapshot['items']:
            identifier = book_identifier(item['source'])
            if not item['title']:
                raise RuntimeError('电子书名称为空，已停止。')
            found[identifier] = item
        signature = (tuple(found), snapshot['height'])
        if signature != previous:
            previous, changed, end_since = signature, time.monotonic(), None
            store.db['discovered'] = list(found.values())
            store.flush()
            print(f'已发现 {len(found)} 本电子书，继续向下滚动……')
        if (found and snapshot['more_present'] and not snapshot['more']
                and not snapshot['loading'] and snapshot['bottom']):
            if end_since is None:
                end_since = time.monotonic()
            elif time.monotonic() - end_since >= 30:
                store.db['scan_complete'] = True
                store.flush()
                return list(found.values())
        else:
            end_since = None
        if time.monotonic() - changed >= timeout:
            raise RuntimeError('列表至少300秒未增长，尚未确认全部加载，已停止并保留扫描记录。')
        # Move incrementally, including a small up/down motion at the bottom so
        # the site's scroll listener fires even when the last request was slow.
        page.evaluate('''() => {const e=document.scrollingElement;
          if(e.scrollTop+e.clientHeight>=e.scrollHeight-5) window.scrollBy(0,-100);
          window.scrollBy(0,Math.max(300,e.clientHeight*0.8));}''')
        page.wait_for_timeout(500)


class BookStore:
    def __init__(self, root, reserve_gb):
        self.root = Path(root) / '电子书'
        self.storage = Library(self.root, reserve_gb)
        self.path = self.root / '电子书记录.json'
        self.db = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'books': {}}

    def paths(self, item):
        identifier = book_identifier(item['source'])
        return self.root / f'{safe_name(item["title"])}_{identifier}.pdf', self.root / '_work' / identifier

    def done(self, item):
        record = self.db['books'].get(book_identifier(item['source']), {})
        if record.get('status') != '已完成' or not record.get('pdf'):
            return False
        path = self.root / record['pdf']
        return path.is_file() and file_hash(path) == record.get('sha256')

    def update(self, item, status, **values):
        self.db['books'].setdefault(book_identifier(item['source']), {}).update(item, status=status, **values)
        self.flush()

    def flush(self):
        save_json(self.path, self.db)
        rows = []
        for identifier, record in self.db['books'].items():
            title = html.escape(record['title'])
            if record['status'] == '已完成' and record.get('pdf'):
                title = f'<a href="{quote(record["pdf"], safe="/")}">{title}</a>'
            rows.append(f'<tr><td>{identifier}</td><td>{title}</td><td>{html.escape(record["status"])}</td>'
                        f'<td>{record.get("pages", "")}</td><td>{html.escape(record.get("reason", ""))}</td></tr>')
        markup = ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                  '<title>电子书资料库</title><style>body{font-family:system-ui;margin:32px;background:#f4f6f8;color:#203047}'
                  'table{width:100%;background:white;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}'
                  'input{padding:12px;width:70%;margin-bottom:20px}a{color:#126ea1}</style><h1>电子书资料库</h1>'
                  '<input placeholder="搜索书名、编号或状态" oninput="document.querySelectorAll(\'tbody tr\').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase()))">'
                  '<table><thead><tr><th>编号</th><th>书名</th><th>状态</th><th>页数</th><th>说明</th></tr></thead><tbody>'
                  + ''.join(rows) + '</tbody></table></html>')
        temporary = self.root / '电子书目录.tmp'
        temporary.write_text(markup, encoding='utf-8')
        temporary.replace(self.root / '电子书目录.html')


def displayed_pages(value, total):
    match = re.fullmatch(r'\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*/\s*(\d+)\s*', value)
    if not match or int(match[3]) != total:
        raise RuntimeError(f'阅读器页码异常：{value}')
    first, last = int(match[1]), int(match[2] or match[1])
    if not 1 <= first <= last <= total or last-first > 1:
        raise RuntimeError(f'阅读器页码范围异常：{value}')
    return list(range(first, last+1))


def reader_images(frame):
    return frame.locator('img').evaluate_all(r'''es => es.map(e=>({
      src:e.currentSrc || e.src,ready:e.complete && e.naturalWidth>0 && e.naturalHeight>0,
      sources:[e.getAttribute('data-original'),e.getAttribute('data-src'),e.getAttribute('data-large'),
        ...(e.getAttribute('srcset') || '').split(',').map(s=>s.trim().split(/\s+/)[0])]
        .filter(Boolean).map(s=>new URL(s,e.baseURI).href)}))''')


def wait_pages(frame, expected, total, settings, work):
    deadline = time.monotonic() + max(300, float(settings.get('chapter_wait_seconds', 300)))
    previous, stable = None, None
    snapshot = {}
    while time.monotonic() < deadline:
        value = frame.locator('#currentPageIndexTextField').input_value()
        pages = displayed_pages(value, total)
        images = reader_images(frame)
        # Numerical image filenames are required; never infer book page order
        # from arbitrary preload images or the printed folio on the paper.
        selected = {n: [im for im in images if page_number(im['src']) == n] for n in pages}
        snapshot = {'page_field': value, 'requested_page': expected,
                    'image_pages': [page_number(im['src']) for im in images]}
        if expected in pages and all(selected[n] and all(im['ready'] for im in selected[n]) for n in pages):
            signature = json.dumps([pages, selected], sort_keys=True)
            if previous != signature:
                previous, stable = signature, time.monotonic()
            elif time.monotonic()-stable >= 2:
                return pages, selected
        else:
            previous, stable = None, None
        frame.wait_for_timeout(250)
    save_json(work / 'diagnostic.json', snapshot)
    raise RuntimeError(f'第{expected}页或图片未完整加载，等待至少300秒后终止。')


def next_arrow(frame):
    # Common FlipBook toolbar identifiers; semantic labels cover variants.
    selectors = ('#nextPageButton', '#nextPage', '#next', '#btnNext', '.nextPageButton',
                 '[title="Next Page"]', '[title="Next page"]', '[title="下一页"]',
                 '[aria-label="Next Page"]', '[aria-label="下一页"]')
    for selector in selectors:
        buttons = frame.locator(selector)
        visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
        if len(visible) == 1:
            visible[0].click(timeout=10000)
            return
    controls = frame.get_by_role('button', name=re.compile(r'^(Next( Page)?|下一页)$', re.I))
    if controls.count() == 1 and controls.is_visible():
        controls.click(timeout=10000)
        return
    raise RuntimeError('无法唯一识别阅读器下一页箭头，已停止；请提供阅读器页面结构以便适配。')


def collect_book(context, frame, total, item, store, settings):
    output, work = store.paths(item)
    work.mkdir(parents=True, exist_ok=True)
    path = work / 'progress.json'
    manifest = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'source': item['source'], 'total': total, 'pages': {}}
    if manifest['total'] != total or book_identifier(manifest['source']) != book_identifier(item['source']):
        raise RuntimeError('阅读器总页数或书籍与原进度不一致，已停止。')
    initial = displayed_pages(frame.locator('#currentPageIndexTextField').input_value(), total)
    if initial[0] != 1:
        # Only rewind at startup. Subsequent navigation always clicks the arrow.
        field = frame.locator('#currentPageIndexTextField')
        field.fill('1')
        field.press('Enter')
    expected = 1
    while expected <= total:
        store.storage.check_space()
        pages, selected = wait_pages(frame, expected, total, settings, work)
        if pages[0] > expected:
            raise RuntimeError('翻页跳过了尚未保存的页面，已停止。')
        for number in pages:
            image_path = work / f'{number:04d}.jpg'
            old = manifest['pages'].get(str(number), {})
            if not (old and verified(image_path, old.get('sha256'))):
                best = None
                for image in selected[number]:
                    part = image_path.with_suffix('.part')
                    size, source = download_best(context, image, frame.url, part, store.storage)
                    if best is None or size[0]*size[1] > best[0]:
                        best = (size[0]*size[1], part.read_bytes(), size, source)
                part.write_bytes(best[1])
                part.replace(image_path)
                manifest['pages'][str(number)] = {'sha256': file_hash(image_path), 'size': list(best[2]), 'source': best[3]}
                save_json(path, manifest)
                print(f'[{number}/{total}] 已保存原图 {best[2][0]}×{best[2][1]}')
            else:
                print(f'[{number}/{total}] 校验通过，复用已保存页')
        expected = pages[-1]+1
        if expected <= total:
            next_arrow(frame)
    images = [work / f'{n:04d}.jpg' for n in range(1, total+1)]
    if not all(verified(p, manifest['pages'].get(str(n), {}).get('sha256')) for n,p in enumerate(images,1)):
        raise RuntimeError('全书页面校验失败，未生成PDF。')
    store.storage.check_space(sum(p.stat().st_size for p in images)*2 + 64*1024*1024)
    build_pdf(images, output)
    validate_pdf(output, total)
    # Completion is committed only AFTER successfully closing the reader.
    return output, work


def close_book_reader(page):
    from modal_close import close_by_backdrop
    modal = page.locator('#readerModal')
    button = page.locator('#closeReader')
    if button.count() and button.is_visible():
        button.click(timeout=10000)
        modal.wait_for(state='hidden', timeout=10000)
        return
    print('未找到阅读器右上角叉号，尝试点击左右遮罩区域关闭……')
    if not close_by_backdrop(modal):
        raise RuntimeError('阅读器没有可用关闭按钮，点击左右遮罩后仍未确认关闭，整批暂停。')


def batch_books(context, page, items, store, settings):
    for position, item in enumerate(items, 1):
        if store.done(item):
            print(f'已完成，跳过：{item["title"]}')
            continue
        store.update(item, '处理中', reason='')
        try:
            # Visit the real detail link from the discovered list, rather than
            # constructing reader URLs or bypassing the permission-gated button.
            response = page.goto(item['source'], wait_until='domcontentloaded', timeout=45000)
            if response and response.status in (401,403,429):
                raise PermissionError(f'网站返回{response.status}，请核查阅读权限。')
            if response and not response.ok:
                raise RuntimeError(f'详情页返回HTTP {response.status}')
            button = page.locator('.reader-btn[data-url]').filter(has_text='立即阅读')
            button.wait_for(state='visible', timeout=10000)
            button.click(timeout=10000)
            frame, value, _ = find_reader(context, timeout=max(300, float(settings['chapter_wait_seconds'])), pages=[page])
            match = re.search(r'/\s*(\d+)', value)
            total = int(match[1])
            print(f'[{position}/{len(items)}] 下载：{item["title"]}，共{total}页')
            output, work = collect_book(context, frame, total, item, store, settings)
            close_book_reader(page)
            store.update(item, '已完成', pdf=output.relative_to(store.root).as_posix(),
                         sha256=file_hash(output), pages=total, bytes=output.stat().st_size, reason='')
            if not settings.get('keep_page_images'):
                shutil.rmtree(work)
        except Exception as exc:
            store.update(item, '未完成已终止', reason=str(exc)[:500])
            raise RuntimeError(f'电子书“{item["title"]}”未完成，整批终止，进度已保留：{exc}') from exc


def main():
    settings = read_settings(ROOT)
    store = BookStore(settings['output_path'], settings['reserve_gb'])
    store.storage.check_space()
    with sync_playwright() as playwright:
        context = launch_browser(playwright)
        try:
            page = context.new_page()
            page.goto(LIST_URL, wait_until='domcontentloaded', timeout=45000)
            input('请自行登录并确认电子书有完整阅读权限，然后回到下载窗口按回车：')
            page.goto(LIST_URL, wait_until='domcontentloaded', timeout=45000)
            items = discover_books(page, store, settings)
            batch_books(context, page, items, store, settings)
            print(f'完成。打开：{store.root / "电子书目录.html"}')
        finally:
            context.close()


if __name__ == '__main__':
    main()
