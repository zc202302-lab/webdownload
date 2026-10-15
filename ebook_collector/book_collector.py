"""Books list: scroll discovery, verified arrow navigation and modal close."""
import html
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import sync_playwright
from chapter_reader import download_best
from collector import ROOT, build_pdf, find_reader, launch_browser, page_number, verified, recover_reader
from library_manager import Library, StorageFull, file_hash, read_settings, safe_name, save_json, validate_pdf

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
    timeout = max(60, float(settings.get('chapter_wait_seconds', 60)))
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
        signature = tuple(found)
        if signature != previous:
            previous, changed, end_since = signature, time.monotonic(), None
            store.db['discovered'] = list(found.values())
            store.flush()
            print(f'已发现 {len(found)} 本电子书，继续向下滚动……')
        if (found and snapshot['more_present'] and not snapshot['more']
                and not snapshot['loading']):
            if end_since is None:
                end_since = time.monotonic()
            elif time.monotonic() - end_since >= 30:
                store.db['scan_complete'] = True
                store.flush()
                print(f'列表扫描完成：共{len(found)}本电子书，开始逐本下载。')
                return list(found.values())
        else:
            end_since = None
        if time.monotonic() - changed >= timeout:
            raise RuntimeError('列表至少60秒未增长，尚未确认全部加载，已停止并保留扫描记录。')
        # Move incrementally, including a small up/down motion at the bottom so
        # the site's scroll listener fires even when the last request was slow.
        page.evaluate('''() => {const e=document.scrollingElement;
          if(e.scrollTop+e.clientHeight>=e.scrollHeight-5) window.scrollBy(0,-100);
          window.scrollBy(0,Math.max(300,e.clientHeight*0.8));}''')
        page.wait_for_timeout(500)


class BookStore:
    def __init__(self, root, reserve_gb):
        from download_paths import category_root
        self.root = category_root(root,'电子书')
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

    def begin_run(self):
        # Reset only failure history, never completion records or page manifests.
        self.db['run_started']=time.strftime('%Y-%m-%d %H:%M:%S')
        for record in self.db['books'].values():
            record.pop('failures',None)
        self.flush()

    def flush(self):
        save_json(self.path, self.db)
        failures=[]
        for identifier,record in self.db['books'].items():
            for failure in record.get('failures',[]):
                failures.append(f'时间：{failure["time"]}\n书名：{record["title"]}（ID {identifier}）\n'
                    f'链接：{record["source"]}\n阶段：{failure["stage"]}\n'
                    f'位置：{failure["position"]}\n原因：{failure["reason"]}\n当前状态：{record["status"]}\n')
        if failures or self.db.get('run_started'):
            temporary=self.root/'缺失记录.tmp'
            header=f'本轮开始：{self.db.get("run_started","未记录")}\n\n'
            temporary.write_text(header+('\n'.join(failures) if failures else '本轮暂无缺失记录。\n'),encoding='utf-8-sig')
            temporary.replace(self.root/'缺失记录.txt')
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


def wait_pages(frame, expected, total, settings, work, seeking=False):
    deadline = time.monotonic() + max(60, float(settings.get('chapter_wait_seconds', 60)))
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
        if pages[0] > expected and not seeking:
            save_json(work / 'diagnostic.json', snapshot)
            raise RuntimeError(f'翻页跳过了尚未保存的第{expected}页，已停止并保留进度。')
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
    raise RuntimeError(f'第{expected}页或图片未完整加载，等待至少60秒后终止。')


ARROW_NODES = 'button,a,input[type="button"],[role="button"],img,svg,[onclick],[id],[class]'
ARROW_PROBE = r'''es => {
  const field=document.querySelector('#currentPageIndexTextField');
  if(!field) return [];
  const fr=field.getBoundingClientRect();
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'
      && !e.disabled && e.getAttribute('aria-disabled')!=='true';};
  const rank=e=>{
    const tokens=[e.id,e.getAttribute('class'),e.getAttribute('title'),e.getAttribute('alt'),
      e.getAttribute('aria-label'),e.getAttribute('data-tooltip')].filter(Boolean);
    // This reader labels generic DIV buttons with a pointer-events:none SPAN.
    const label=(e.textContent||'').trim();
    if(/^(下一页|next\s*(page)?)$/i.test(label)) tokens.push('nextpage');
    for(const raw of [e.getAttribute('src'),getComputedStyle(e).backgroundImage]) {
      if(raw) tokens.push(raw.split('/').pop().split(/[?#]/)[0].replace(/["')]/g,''));
    }
    let score=0;
    for(const token of tokens) {
      for(const part of token.split(/\s+/)) {
        const word=part.toLowerCase().replace(/\.(png|gif|svg|jpe?g|webp)$/,'').replace(/[-_]/g,'');
        if(/^(next(page)?(button|btn|arrow|icon)?|btnnext(page)?|buttonnext(page)?|nextbtn|下一页)$/.test(word)) score=Math.max(score,2);
        if(/^(right(button|btn|arrow|icon|page)?|arrowright|btnright|pageright)$/.test(word)) score=Math.max(score,1);
      }
    }
    const joined=tokens.join(' ').toLowerCase();
    if(/last|first|endpage|nextchapter|fullscreen|末页|首页|下一章/.test(joined)) return 0;
    return score;
  };
  const found=new Map();
  es.forEach((e,index)=>{
    if(!visible(e)) return;
    const r=e.getBoundingClientRect();
    // Only the page-number toolbar: never click a next/right image in the book.
    if(Math.abs((r.top+r.height/2)-(fr.top+fr.height/2))>Math.max(36,fr.height*2)
      || r.width>120 || r.height>120) return;
    const score=rank(e);if(!score) return;
    const target=e.closest('button,a,[role="button"],[onclick]') || e;
    if(!visible(target)) return;
    const targetIndex=es.indexOf(target);if(targetIndex<0) return;
    const old=found.get(target);
    if(!old || score>old.score) found.set(target,{index:targetIndex,score,tag:target.tagName,
      id:(target.id||'').slice(0,80),class_name:(target.getAttribute('class')||'').slice(0,120)});
  });
  // Nested DIV wrappers and their icon represent the same control.
  const rows=[...found.entries()].filter(([e])=>![...found.keys()].some(other=>other!==e && e.contains(other)));
  const best=Math.max(0,...rows.map(([,row])=>row.score));
  return rows.map(([,row])=>row).filter(row=>row.score===best);
}'''


def next_arrow(frame, work=None):
    # Common FlipBook toolbar identifiers; semantic labels cover variants.
    selectors = ('#nextPageButton', '#nextPage', '#next', '#btnNext', '.nextPageButton',
                 '#nextPageBtn', '#nextButton', '.next-button', '.next-page',
                 '.nextPage', '#rightButton', '#rightPage', '#rightBtn',
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
    nodes=frame.locator(ARROW_NODES)
    candidates=nodes.evaluate_all(ARROW_PROBE) if nodes.count() else []
    if len(candidates)==1:
        nodes.nth(candidates[0]['index']).click(timeout=10000)
        return
    if work is not None:
        save_json(work / 'navigation_diagnostic.json', {'reason':'下一页箭头未唯一识别',
                   'candidates':candidates})
    raise RuntimeError('无法唯一识别阅读器下一页箭头，已停止；请提供阅读器页面结构以便适配。')


def collect_book(context, frame, total, item, store, settings):
    output, work = store.paths(item)
    work.mkdir(parents=True, exist_ok=True)
    path = work / 'progress.json'
    manifest = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'source': item['source'], 'total': total, 'pages': {}}
    if manifest['total'] != total or book_identifier(manifest['source']) != book_identifier(item['source']):
        raise RuntimeError('阅读器总页数或书籍与原进度不一致，已停止。')
    valid=set()
    for number in range(1,total+1):
        store.storage.check_space()
        old=manifest['pages'].get(str(number),{})
        if old.get('sha256') and verified(work/f'{number:04d}.jpg',old['sha256']):
            valid.add(number)
    expected=next((n for n in range(1,total+1) if n not in valid),total+1)
    seeking=False
    if expected<=total:
        initial=displayed_pages(frame.locator('#currentPageIndexTextField').input_value(),total)
        if expected not in initial:
            field=frame.locator('#currentPageIndexTextField')
            field.fill(str(expected));field.press('Enter');seeking=True
        print(f'继续下载：已校验{len(valid)}页，从第{expected}页开始。')
    else:
        print(f'全部{total}页已校验，直接生成PDF。')
    while expected <= total:
        store.storage.check_space()
        manifest.update(active_page=expected,phase='加载或下载页面')
        save_json(path,manifest)
        frame=recover_reader(frame,settings,expected)
        pages, selected = wait_pages(frame, expected, total, settings, work,seeking=seeking)
        seeking=False
        if pages[0] > expected:
            raise RuntimeError('翻页跳过了尚未保存的页面，已停止。')
        for number in pages:
            manifest.update(active_page=number,phase='下载页面原图')
            save_json(path,manifest)
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
                valid.add(number)
                print(f'[{number}/{total}] 已保存原图 {best[2][0]}×{best[2][1]}')
            else:
                print(f'[{number}/{total}] 校验通过，复用已保存页')
        expected = pages[-1]+1
        while expected in valid:
            expected+=1
        if expected <= total:
            manifest.update(active_page=expected,phase='翻页或恢复断点')
            save_json(path,manifest)
            if expected==pages[-1]+1:
                next_arrow(frame, work)
            else:
                field=frame.locator('#currentPageIndexTextField')
                field.fill(str(expected));field.press('Enter');seeking=True
    images = [work / f'{n:04d}.jpg' for n in range(1, total+1)]
    if not all(verified(p, manifest['pages'].get(str(n), {}).get('sha256')) for n,p in enumerate(images,1)):
        raise RuntimeError('全书页面校验失败，未生成PDF。')
    store.storage.check_space(sum(p.stat().st_size for p in images)*2 + 64*1024*1024)
    manifest.update(active_page=None,phase='生成PDF')
    save_json(path,manifest)
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


def batch_books(context, page, items, store, settings, opened_reader=None):
    for position, item in enumerate(items, 1):
        if store.done(item):
            print(f'已完成，跳过：{item["title"]}')
            continue
        store.update(item, '处理中', reason='')
        baseline=set(context.pages) if context is not None else set()
        stage='打开详情页'
        try:
            # Visit the real detail link from the discovered list, rather than
            # constructing reader URLs or bypassing the permission-gated button.
            if opened_reader is None:
                response = page.goto(item['source'], wait_until='domcontentloaded', timeout=45000)
                if response and response.status in (401,403,429):
                    raise PermissionError(f'网站返回{response.status}，请核查阅读权限。')
                if response and not response.ok:
                    raise RuntimeError(f'详情页返回HTTP {response.status}')
                stage='打开阅读器'
                button = page.locator('.reader-btn[data-url]').filter(has_text='立即阅读')
                button.wait_for(state='visible', timeout=10000)
                button.click(timeout=10000)
                frame, value, _ = find_reader(context, timeout=max(60, float(settings['chapter_wait_seconds'])), pages=[page])
            else:
                frame,value,_=opened_reader
            match = re.search(r'/\s*(\d+)', value)
            total = int(match[1])
            print(f'[{position}/{len(items)}] 下载：{item["title"]}，共{total}页')
            stage='下载书页'
            output, work = collect_book(context, frame, total, item, store, settings)
            stage='关闭阅读器'
            if opened_reader is None or (page.locator('#readerModal').count() and page.locator('#readerModal').is_visible()):
                close_book_reader(page)
            else:
                print('当前为独立阅读页，下载完成；请自行关闭此页，再打开下一本。')
            if hasattr(frame,'page') and frame.page is not page and not frame.page.is_closed():
                frame.page.close()
            store.update(item, '已完成', pdf=output.relative_to(store.root).as_posix(),
                         sha256=file_hash(output), pages=total, bytes=output.stat().st_size, reason='')
            if not settings.get('keep_page_images'):
                shutil.rmtree(work)
        except Exception as exc:
            position_text='未确认页码'
            if stage=='下载书页':
                progress=store.paths(item)[1]/'progress.json'
                try:
                    saved=json.loads(progress.read_text(encoding='utf-8'))
                    stage=saved.get('phase',stage)
                    if saved.get('active_page'):
                        position_text=f'阅读器第{saved["active_page"]}页'
                except (OSError,ValueError):pass
            fatal=isinstance(exc,(PermissionError,StorageFull))
            record=store.db['books'][book_identifier(item['source'])]
            record.setdefault('failures',[]).append({'time':time.strftime('%Y-%m-%d %H:%M:%S'),
                'stage':stage,'position':position_text,'reason':str(exc)[:1000]})
            store.update(item,'未完成已暂停' if fatal else '下载失败已跳过',reason=str(exc)[:500])
            print(f'电子书“{item["title"]}”失败，进度保留，已记入缺失记录.txt：{exc}')
            if fatal:
                raise
            # Discard only popups created by this book. Next detail navigation
            # replaces a stuck modal on the owned work page.
            if context is not None:
                for popup in list(context.pages):
                    if popup not in baseline and popup is not page and not popup.is_closed():
                        popup.close()
            print('跳过当前电子书，继续下一本。')


def normalized_book_title(title):
    title=unicodedata.normalize('NFKC',title).casefold()
    return re.sub(r'\s+','',title).replace('《','').replace('》','')


def completed_title(store,title):
    key=normalized_book_title(title)
    return any(normalized_book_title(record['title'])==key and store.done(record)
               for record in store.db['books'].values())


def manual_reader(context,settings):
    # Pump browser navigation events before inspecting pages opened by the user.
    for page in list(context.pages):
        if not page.is_closed():page.wait_for_timeout(100)
    frame,value,title=find_reader(context,timeout=max(60,float(settings['chapter_wait_seconds'])))
    readers=[]
    for page in list(context.pages):
        if page.is_closed():continue
        for candidate in list(page.frames):
            try:
                field=candidate.locator('#currentPageIndexTextField')
                if not candidate.is_detached() and field.count() and field.is_visible():
                    readers.append(candidate)
            except Exception:continue
    if len(readers)!=1 or frame not in readers:
        raise RuntimeError('请只保留一本已打开的电子书阅读器，关闭其他阅读器后重新输入书名。')
    page=frame.page
    sources=[page.url]
    opener=page.opener()
    if opener is not None and not opener.is_closed():sources.append(opener.url)
    for source in sources:
        try:
            book_identifier(source)
            return page,source,(frame,value,title)
        except ValueError:pass
    raise RuntimeError('未找到此书的东方美食详情链接。请从电子书详情页点击立即阅读后，再输入书名。')


def manual_session(context,store,settings,ask=None):
    ask=ask or input
    while True:
        name=ask('请在软件浏览器打开一本电子书，输入书名后确认开始（输入“退出”结束）：').strip()
        if name=='退出':return
        if not normalized_book_title(name):
            print('书名不能为空，请重新输入。');continue
        if completed_title(store,name):
            print(f'已完成且PDF校验通过，按书名跳过：{name}。请打开下一本并输入书名。');continue
        try:
            page,source,reader=manual_reader(context,settings)
        except Exception as exc:
            print(f'尚未启动下载：{exc}');continue
        item={'title':name,'source':source}
        if store.done(item):
            print('此书的网站ID已完成且PDF校验通过，即使输入不同书名也不重复下载。');continue
        # Keep the original filename/title when resuming an existing book ID.
        old=store.db['books'].get(book_identifier(source))
        if old:item['title']=old['title']
        batch_books(context,page,[item],store,settings,opened_reader=reader)
        print('本次单本任务结束，等待你打开下一本并输入书名；不会自动开始下一本。')


def manual_main():
    settings=read_settings(ROOT)
    store=BookStore(settings['output_path'],settings['reserve_gb'])
    store.storage.check_space();store.begin_run()
    with sync_playwright() as playwright:
        context=launch_browser(playwright)
        try:
            page=context.new_page()
            page.goto(LIST_URL,wait_until='domcontentloaded',timeout=45000)
            print('单本模式：请在此浏览器自行登录、打开目标书的立即阅读页面；无需扫描全部书籍。')
            manual_session(context,store,settings)
        finally:context.close()


def main():
    settings = read_settings(ROOT)
    store = BookStore(settings['output_path'], settings['reserve_gb'])
    store.storage.check_space()
    store.begin_run()
    with sync_playwright() as playwright:
        context = launch_browser(playwright)
        try:
            page = context.new_page()
            page.goto(LIST_URL, wait_until='domcontentloaded', timeout=45000)
            input('请自行登录并确认电子书有完整阅读权限，然后回到下载窗口按回车：')
            page.goto(LIST_URL, wait_until='domcontentloaded', timeout=45000)
            items = discover_books(page, store, settings)
            batch_books(context, page, items, store, settings)
            failed=sum(store.db['books'].get(book_identifier(item['source']),{}).get('status')=='下载失败已跳过' for item in items)
            print(f'本轮结束：{failed}本失败已跳过；详见缺失记录.txt。打开：{store.root / "电子书目录.html"}')
        finally:
            context.close()


if __name__ == '__main__':
    main()
