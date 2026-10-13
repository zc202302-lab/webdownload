"""Download accessible cookbook pages using the user's existing browser profile."""
import html
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright
from chapter_reader import download_best, print_body
from collector import ROOT, launch_browser
from library_manager import Library, file_hash, read_settings, safe_name, save_json, validate_pdf

LIST_URL = 'https://www.easteat.com/book_list.php?action=cookbook_list'


def recipe_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    identifier = query.get('id', [''])[0]
    if (parsed.scheme not in ('https', 'http') or parsed.hostname != 'www.easteat.com'
            or parsed.path != '/book_list.php'
            or query.get('action') != ['cookbook_detail'] or not identifier.isdigit()):
        raise ValueError('不是有效的东方美食菜谱链接')
    return identifier


def list_snapshot(page):
    return page.evaluate(r'''() => ({
      items:[...document.querySelectorAll('.cookbook-list .cookbook-item a')].map(a=>({
        source:a.href,title:a.querySelector('.cookbook-name')?.textContent.trim() || ''})),
      current:document.querySelector('.pagination a.active')?.textContent.trim() || '1',
      next:[...document.querySelectorAll('.pagination a')].find(a=>a.textContent.trim()==='下一页')?.href || null
    })''')


def body_snapshot(page):
    return page.evaluate(r'''() => {
      const title=document.querySelector('.cookbook-title');
      if(!title) return null;
      const root=title.closest('.page-header')?.parentElement;
      if(!root) return null;
      const sections=[...root.querySelectorAll(':scope > .section')];
      const visible=e=>!!(e.getBoundingClientRect().width && e.getBoundingClientRect().height);
      const locked=sections.some(s=>[...s.querySelectorAll('.locked-content')].some(visible));
      const gallery=root.querySelector('.images-gallery');
      const selected=[...(gallery ? [gallery] : []),...sections];
      const images=selected.flatMap(s=>[...s.querySelectorAll('img')]);
      images.forEach(im=>im.scrollIntoView({block:'center'}));
      const sources=images.map(im=>({src:im.currentSrc || im.src,dom_src:im.src,
        sources:[im.getAttribute('data-original'),im.getAttribute('data-src'),im.getAttribute('data-large'),
          ...(im.getAttribute('srcset') || '').split(',').map(s=>s.trim().split(/\s+/)[0])]
          .filter(Boolean).map(s=>new URL(s,im.baseURI).href),
        ready:im.complete && im.naturalWidth>0 && im.naturalHeight>0}));
      const clean=s=>{
        const clone=s.cloneNode(true);
        clone.querySelectorAll('script,style,iframe,button,nav,.locked-content,.locked-actions').forEach(e=>e.remove());
        clone.querySelectorAll('img').forEach((im,i)=>{
          const original=s.querySelectorAll('img')[i];
          im.setAttribute('src',original.currentSrc || original.src);
          im.removeAttribute('srcset');im.removeAttribute('loading');
        });
        clone.querySelectorAll('.food-category').forEach(category=>{
          const list=category.querySelector('.food-list');
          if(!list) return;
          const table=document.createElement('table');
          for(const li of list.querySelectorAll('li')) {
            const row=table.insertRow();
            row.insertCell().textContent=li.querySelector('span:first-child')?.textContent || '';
            row.insertCell().textContent=li.querySelector('span:last-child')?.textContent || '';
          }
          list.replaceWith(table);
        });
        clone.querySelectorAll('.steps').forEach(list=>{
          const ordered=document.createElement('ol');
          ordered.innerHTML=list.innerHTML;list.replaceWith(ordered);
        });
        clone.querySelectorAll('*').forEach(e=>{
          for(const a of [...e.attributes]) if(a.name!=='src' && a.name!=='alt' && a.name!=='colspan' && a.name!=='rowspan') e.removeAttribute(a.name);
        });
        return clone.innerHTML;
      };
      const sectionData=sections.map(s=>({heading:s.querySelector('.section-title')?.textContent.trim() || '',text:s.innerText.trim()}));
      const foods=[...root.querySelectorAll('.food-category')].map(c=>({
        category:c.querySelector('h4')?.textContent.trim() || '',
        items:[...c.querySelectorAll('.food-list li')].map(li=>({
          name:li.querySelector('span:first-child')?.textContent.trim() || '',
          amount:li.querySelector('span:last-child')?.textContent.trim() || ''}))}));
      const stepSection=sections.find(s=>s.querySelector('.section-title')?.textContent.includes('制作步骤'));
      const stepText=stepSection ? stepSection.innerText.replace('制作步骤','').trim() : '';
      return {title:title.textContent.trim(),locked,sections:sectionData,ingredients:foods,
        has_steps:!!stepText && !locked,html:selected.map(clean).join('\n'),sources};
    }''')


def wait_body(page, settings):
    deadline = time.monotonic() + max(300, float(settings.get('chapter_wait_seconds', 300)))
    previous = None
    stable = None
    while time.monotonic() < deadline:
        snapshot = body_snapshot(page)
        if snapshot and snapshot['locked']:
            raise PermissionError('菜谱正文已锁定，请自行登录并确认有完整阅读权限后重新运行。')
        ready = (snapshot and snapshot['has_steps'] and snapshot['sources']
                 and all(im['ready'] for im in snapshot['sources']))
        if ready:
            signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            if signature != previous:
                previous, stable = signature, time.monotonic()
            elif time.monotonic() - stable >= 2:
                return snapshot
        else:
            previous, stable = None, None
        page.wait_for_timeout(250)
    raise RuntimeError('菜谱正文或图片未完整加载，等待至少300秒后终止。')


class RecipeStore:
    def __init__(self, root, reserve_gb):
        self.root = Path(root) / '爆款菜谱'
        self.storage = Library(self.root, reserve_gb)
        self.path = self.root / '菜谱记录.json'
        self.db = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'recipes': {}}

    def paths(self, item):
        name = f'{safe_name(item["title"])}_{recipe_id(item["source"])}'
        return self.root / '菜谱' / (name + '.pdf'), self.root / '菜谱' / (name + '.json')

    def done(self, item):
        record = self.db['recipes'].get(recipe_id(item['source']), {})
        if record.get('status') != '已完成':
            return False
        for key in ('pdf', 'data'):
            path = self.root / record[key]
            if not path.is_file() or file_hash(path) != record[key + '_sha256']:
                return False
        return True

    def update(self, item, status, **values):
        self.db['recipes'].setdefault(recipe_id(item['source']), {}).update(item, status=status, **values)
        self.flush()

    def flush(self):
        save_json(self.path, self.db)
        rows = []
        for identifier, r in self.db['recipes'].items():
            title = html.escape(r['title'])
            if r['status'] == '已完成':
                from urllib.parse import quote
                title = f'<a href="{quote(r["pdf"], safe="/")}">{title}</a>'
            rows.append(f'<tr><td>{identifier}</td><td>{title}</td><td>{html.escape(r["status"])}</td><td>{html.escape(r.get("reason", ""))}</td></tr>')
        markup = ('<!doctype html><meta charset="utf-8"><title>爆款菜谱资料库</title>'
                  '<style>body{font-family:system-ui;margin:32px}td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}table{width:100%}input{padding:10px;width:70%}</style>'
                  '<h1>爆款菜谱资料库</h1><input placeholder="搜索菜名、编号或状态" oninput="document.querySelectorAll(\'tbody tr\').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase()))">'
                  '<table><thead><tr><th>编号</th><th>菜名</th><th>状态</th><th>说明</th></tr></thead><tbody>'
                  + ''.join(rows) + '</tbody></table>')
        temporary = self.root / '菜谱目录.tmp'
        temporary.write_text(markup, encoding='utf-8')
        temporary.replace(self.root / '菜谱目录.html')


def collect_recipe(context, page, item, store, settings):
    identifier = recipe_id(item['source'])
    snapshot = wait_body(page, settings)
    pdf, data_path = store.paths(item)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    work = store.root / '_work' / identifier
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / 'images.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    mapping = {}
    for index, image in enumerate(snapshot['sources']):
        store.storage.check_space()
        path = work / f'{index:04d}.jpg'
        old = manifest.get(str(index), {})
        identity = [image['src'], image['sources']]
        if not (old.get('identity') == identity and path.is_file() and file_hash(path) == old.get('sha256')):
            part = path.with_suffix('.part')
            size, source = download_best(context, image, page.url, part, store.storage)
            part.replace(path)
            manifest[str(index)] = {'identity': identity, 'sha256': file_hash(path), 'size': list(size), 'source': source}
            save_json(manifest_path, manifest)
        mapping[image['src']] = path
        mapping[image['dom_src']] = path
    markup = '<h1>' + html.escape(snapshot['title']) + '</h1>' + snapshot['html']
    store.storage.check_space(sum(p.stat().st_size for p in work.glob('*.jpg')) * 3 + 64 * 1024 * 1024)
    temporary = work / 'recipe.pdf'
    print_body(context, {'html': markup}, mapping, temporary)
    count = validate_pdf(temporary)
    temporary.replace(pdf)
    data = {**item, 'id': identifier, 'page_title': snapshot['title'],
            'sections': snapshot['sections'], 'ingredients': snapshot['ingredients'],
            'images': list(manifest.values()), 'scope': '网页版文字与图片，不含APP视频'}
    save_json(data_path, data)
    store.update(item, '已完成', pdf=pdf.relative_to(store.root).as_posix(),
                 data=data_path.relative_to(store.root).as_posix(), pages=count,
                 pdf_sha256=file_hash(pdf), data_sha256=file_hash(data_path), reason='')
    if not settings.get('keep_page_images'):
        import shutil
        shutil.rmtree(work)


def batch_recipes(context, list_page, store, settings):
    visited = set()
    store.db['scan_complete'] = False
    store.flush()
    while True:
        store.storage.check_space()
        snapshot = list_snapshot(list_page)
        items = snapshot['items']
        if not items:
            raise RuntimeError('未找到菜谱列表，停止以避免漏页。')
        signature = tuple(recipe_id(item['source']) for item in items)
        if signature in visited:
            raise RuntimeError('分页重复返回同一列表，已停止。')
        visited.add(signature)
        store.db['current_list'] = list_page.url
        store.flush()
        for item in items:
            if not item['title']:
                raise RuntimeError('菜谱名称为空，停止以便核查。')
            if store.done(item):
                print(f'已完成，跳过：{item["title"]}')
                continue
            store.update(item, '处理中', reason='')
            page = context.new_page()
            try:
                response = page.goto(item['source'], wait_until='domcontentloaded', timeout=45000)
                if response and response.status in (401, 403, 429):
                    raise PermissionError(f'网站返回{response.status}，停止采集。')
                if response and not response.ok:
                    raise RuntimeError(f'详情页返回HTTP {response.status}')
                print(f'列表第{snapshot["current"]}页：{item["title"]}')
                collect_recipe(context, page, item, store, settings)
            except Exception as exc:
                store.update(item, '未完成已终止', reason=str(exc)[:500])
                raise RuntimeError(f'菜谱“{item["title"]}”未完成，整批终止，进度已保留：{exc}') from exc
            finally:
                page.close()
            list_page.wait_for_timeout(1000)
        next_url = snapshot['next']
        if not next_url:
            store.db['scan_complete'] = True
            store.flush()
            return
        parsed = urlparse(next_url)
        if parsed.hostname != 'www.easteat.com' or parse_qs(parsed.query).get('action') != ['cookbook_list']:
            raise RuntimeError('下一页链接异常，已停止。')
        response = list_page.goto(next_url, wait_until='domcontentloaded', timeout=45000)
        if response and not response.ok:
            raise RuntimeError(f'列表页返回HTTP {response.status}，进度已保留。')


def main():
    settings = read_settings(ROOT)
    store = RecipeStore(settings['output_path'], settings['reserve_gb'])
    store.storage.check_space()
    with sync_playwright() as playwright:
        context = launch_browser(playwright)
        try:
            page = context.new_page()
            page.goto(LIST_URL, wait_until='domcontentloaded')
            input('请自行登录并确认菜谱有完整阅读权限，然后回到此窗口按回车：')
            # Start at page one to avoid missing items shifted by new publications.
            # Completed recipe IDs are checked and skipped on each run.
            page.goto(LIST_URL, wait_until='domcontentloaded')
            batch_recipes(context, page, store, settings)
            print(f'完成。打开：{store.root / "菜谱目录.html"}')
        finally:
            context.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已停止，进度保留。')
    except Exception as exc:
        print(f'未完成：{exc}')
    input('按回车退出。')
