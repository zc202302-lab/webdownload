import hashlib
import json
import re
import time
from pathlib import Path
import base64
import html
from pypdf import PdfReader, PdfWriter

from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from library_manager import file_hash, save_json
from library_manager import validate_pdf


def check_resolution(size, settings):
    # Resolution is recorded, but never blocks an otherwise valid source image.
    if min(size) < 1:
        raise RuntimeError('正文图片尺寸无效，未保存本章。')


def catalog_entries(page):
    return page.evaluate(r'''() => {
        const selectors = '[onclick], [data-chapter-id], .catalog-item, .chapter-item, .toc-item, [id*="catalog"] li, [id*="chapter"] li, [class*="catalog"] li, a[href*="chapter"], a[href*="article"]';
        const rows = [...document.querySelectorAll(selectors)].filter(e => {
            const r=e.getBoundingClientRect(), text=e.innerText?.trim() || '';
            if (!r.width || !r.height || text.length<3 || text.length>250) return false;
            if (/^(杂志目录|立即阅读|购买|上一章|下一章|AI解析)/.test(text.replace(/\s/g,''))) return false;
            const handler=e.getAttribute('onclick') || '';
            return /chapter|catalog|article|content/i.test(handler) || e.matches('.catalog-item,.chapter-item,.toc-item,[data-chapter-id]') || e.closest('[id*="catalog"],[id*="chapter"],[class*="catalog"]') || /chapter|article/i.test(e.getAttribute('href') || '');
        });
        const seen=new Set();
        return rows.filter(e=> !rows.some(child=>child!==e && e.contains(child))).map(e=>e.innerText.trim()).filter(t=>!seen.has(t) && seen.add(t));
    }''')


def visible_next(page):
    controls = page.get_by_text(re.compile(r'^\s*下一章\s*[→➜›>]?\s*$'))
    for i in range(controls.count()):
        item = controls.nth(i)
        if item.is_visible():
            return item
    raise RuntimeError('未找到弹窗中的“下一章”按钮。')


def modal_body(next_button):
    modal = next_button.locator('xpath=ancestor::*[.//*[self::img or self::p or self::table]][1]')
    tag = modal.evaluate('(e)=>e.tagName')
    if tag in ('BODY', 'HTML'):
        raise RuntimeError('未能识别正文弹窗，已停止以避免保存广告或导航。')
    return modal


def body_snapshot(modal, title):
    return modal.evaluate(r'''(e,title) => {
        const body=e.querySelector('.modal-body,.chapter-content,.article-content,#chapterContent,#articleContent') || e;
        const clone=body.cloneNode(true);
        clone.querySelectorAll('script,style,iframe,source,button,nav,.modal-header,.modal-footer,.chapter-nav,.chapter-navigation,.close').forEach(x=>x.remove());
        clone.querySelectorAll('a,[role=button]').forEach(x=>{
            if(/上一章|下一章|关闭/.test(x.textContent)) x.remove();
        });
        const norm=s=>s.replace(/\s+/g,'').replace(/^\d+/,'');
        clone.querySelectorAll('h1,h2,h3,h4,.modal-title').forEach(x=>{
            if(norm(x.textContent)===norm(title)) x.remove();
        });
        clone.querySelectorAll('*').forEach(x=>{
            x.removeAttribute('onclick');
            x.removeAttribute('id');
            x.removeAttribute('class');
            x.removeAttribute('style');
        });
        clone.querySelectorAll('img').forEach(x=>{
            x.setAttribute('src',new URL(x.getAttribute('src') || x.getAttribute('data-src') || '',e.ownerDocument.baseURI).href);
            x.removeAttribute('srcset'); x.removeAttribute('loading');
        });
        return {html:clone.innerHTML,text_length:clone.textContent.trim().length,image_urls:[...clone.querySelectorAll('img')].map(im=>im.getAttribute('src'))};
    }''', title)


def advance_scroll(modal):
    return modal.evaluate(r'''e=>{
        const targets=[e,...e.querySelectorAll('*')].filter(x=>{
            const s=getComputedStyle(x);
            return x.clientHeight>50 && x.scrollHeight>x.clientHeight+10 && /auto|scroll/.test(s.overflowY);
        });
        for(const x of targets) x.scrollTop=Math.min(x.scrollTop+Math.max(100,x.clientHeight*0.8),x.scrollHeight);
        return {bottom:targets.every(x=>x.scrollTop+x.clientHeight>=x.scrollHeight-3),height:targets.map(x=>x.scrollHeight).join(',')};
    }''')


def image_sources(modal):
    return modal.locator('img').evaluate_all(r'''async es => {
      const body=es.filter(e=> {
        const r=e.getBoundingClientRect();
        return (r.width>40 && (r.height>40 || !e.naturalHeight)) || (r.width>0 && e.naturalWidth>40 && e.naturalHeight>40);
      });
      return await Promise.all(body.map(async e=>{
        let ready=e.complete && e.naturalWidth>0 && e.naturalHeight>0;
        if(ready && e.decode) { try { await e.decode(); } catch { ready=false; } }
        return {
        src:e.currentSrc || e.src,
        dom_src:e.src,
        sources:[e.getAttribute('data-original'),e.getAttribute('data-src'),e.getAttribute('data-large'),...((e.getAttribute('srcset') || '').split(',').map(s=>s.trim().split(/\s+/)[0]))].filter(Boolean).map(s=>new URL(s,e.baseURI).href),
        width:e.naturalWidth,height:e.naturalHeight,ready
        };
      }));
    }''')


def wait_chapter(page, expected, previous, settings):
    deadline=time.monotonic()+max(300.0,float(settings.get('chapter_wait_seconds',300)))
    stable_since=None
    last_signature=None
    title=re.sub(r'^\s*\d+\s+', '',expected)
    while time.monotonic()<deadline:
        try:
            # On the final chapter the Next control may disappear. Identify
            # its content modal through the chapter heading instead.
            headings=page.get_by_text(title,exact=True)
            modal=None
            for index in range(headings.count()-1,-1,-1):
                heading=headings.nth(index)
                if heading.is_visible():
                    candidate=heading.locator('xpath=ancestor::*[.//img or .//p or .//table][1]')
                    if candidate.evaluate('(e)=>e.tagName') not in ('BODY','HTML'):
                        modal=candidate
                        break
            if modal is None:
                modal=modal_body(visible_next(page))
            text=modal.inner_text()
            scroll=advance_scroll(modal)
            snapshot=body_snapshot(modal,title)
            sources=image_sources(modal)
            signature=json.dumps([[(im['src'],im['width'],im['height']) for im in sources],snapshot['html'],scroll['height']])
            captured={url for im in sources for url in (im['src'],im.get('dom_src',im['src']))}
            all_body_images=all(url in captured for url in snapshot.get('image_urls',[]))
            ready=(bool(sources) or snapshot['text_length']>40) and all_body_images and all(im['ready'] for im in sources) and title in text and signature != previous and scroll['bottom']
        except (RuntimeError,PlaywrightError):
            ready=False
        if ready:
            if signature != last_signature:
                last_signature=signature
                stable_since=time.monotonic()
            elif time.monotonic()-stable_since>=2:
                return sources,signature,modal
        else:
            stable_since=None
            last_signature=None
        page.wait_for_timeout(250)
    raise RuntimeError(f'正文图片在等待时间内未全部加载完成，未保存本章：{expected}')


def request_image(context, url, referer):
    for attempt in range(1,4):
        try:
            response=context.request.get(url,headers={'Referer':referer},timeout=300000)
            if response.status in (401,403,429):
                raise PermissionError(f'图片请求返回{response.status}，暂停采集。')
            if response.status >= 500:
                if attempt < 3:
                    print(f'图片服务器暂时出错，重试{attempt+1}/3……')
                    time.sleep(2)
                    continue
                raise RuntimeError(f'原图服务器返回{response.status}，3次尝试后仍未成功。')
            return response
        except PlaywrightError:
            if attempt == 3:
                raise RuntimeError('原图下载超时或网络失败：单次等待300秒，3次尝试均未成功，进度已保留。') from None
            print(f'原图下载超时或网络失败，重试{attempt+1}/3（每次最多300秒）……')
            time.sleep(2)


def download_best(context, item, referer, temporary, library):
    candidates = list(dict.fromkeys(item['sources'] + [item['src']]))
    best = None
    for url in candidates:
        if not url.startswith(('https://', 'http://')):
            continue
        response = request_image(context,url,referer)
        if not response.ok:
            continue
        data = response.body()
        library.check_space(len(data)*3)
        temporary.write_bytes(data)
        try:
            with Image.open(temporary) as im:
                size = im.size
                im.load()
            with Image.open(temporary) as im:
                im.verify()
            if best is None or size[0]*size[1] > best[0][0]*best[0][1]:
                best = (size, data, url)
        except (OSError, ValueError):
            continue
    if best is None:
        raise RuntimeError('正文图片未下载成功。')
    temporary.write_bytes(best[1])
    return best[0], best[2]


def print_body(context, snapshot, mapping, path):
    markup=snapshot['html']
    for source,image_path in mapping.items():
        with Image.open(image_path) as im:
            mime=Image.MIME[im.format]
        embedded='data:'+mime+';base64,'+base64.b64encode(image_path.read_bytes()).decode()
        markup=markup.replace('src="'+html.escape(source,quote=True)+'"','src="'+embedded+'"')
    page=context.new_page()
    try:
        # The export document contains only cloned body content and embedded images.
        page.route('http://**/*',lambda route:route.abort())
        page.route('https://**/*',lambda route:route.abort())
        page.set_content('<!doctype html><meta charset="utf-8"><style>body{font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:16px;line-height:1.7;color:#111}img{max-width:100%;max-height:250mm;object-fit:contain;width:auto;height:auto;display:block;margin:12px auto;break-inside:avoid}p,table{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}td,th{border:1px solid #aaa;padding:5px}*{overflow:visible!important}</style><main>'+markup+'</main>',wait_until='load')
        page.evaluate('async()=>{await document.fonts.ready;await Promise.all([...document.images].map(im=>im.decode()));}')
        count=page.locator('img').count()
        embedded=page.locator('img').evaluate_all('es=>es.every(e=>e.src.startsWith("data:") && e.complete && e.naturalWidth>0)')
        if count and not embedded:
            raise RuntimeError('正文仍有未嵌入的图片，暂停以免遗漏。')
        page.pdf(path=str(path),format='A4',print_background=True,margin={'top':'12mm','bottom':'12mm','left':'12mm','right':'12mm'},display_header_footer=False)
        return validate_pdf(path)
    finally:
        page.close()


def merge_chapters(pdfs, output, entries):
    writer=PdfWriter()
    position=0
    for chapter,title in zip(pdfs,entries):
        reader=PdfReader(str(chapter))
        writer.append(reader,import_outline=False)
        writer.add_outline_item(title,position)
        position+=len(reader.pages)
    temporary=output.with_suffix('.tmp.pdf')
    with temporary.open('wb') as file:
        writer.write(file)
    validate_pdf(temporary,position)
    temporary.replace(output)
    return position


def close_chapter(modal):
    scope=modal
    for _ in range(6):
        controls=scope.locator('[aria-label="Close"],[aria-label="关闭"],.close,.modal-close,.btn-close,[data-dismiss="modal"],[data-bs-dismiss="modal"]')
        cross=scope.get_by_text(re.compile(r'^\s*[×✕✖]\s*$'))
        for group in (controls,cross):
            for index in range(group.count()):
                control=group.nth(index)
                if control.is_visible():
                    control.click(timeout=10000)
                    modal.wait_for(state='hidden',timeout=15000)
                    return
        parent=scope.locator('xpath=..')
        if parent.evaluate('(e)=>e.tagName') in ('BODY','HTML'):
            break
        scope=parent
    raise RuntimeError('没有找到正文弹窗右上角叉号，当前条目未关闭，整批暂停。')


def collect_chapters(context, page, task, library, settings, build_pdf):
    entries = catalog_entries(page)
    if not entries:
        raise RuntimeError('未识别到杂志目录条目。请提供这本书的详情页网址以便适配。')
    output, work = library.paths(task)
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    progress_path = work / 'chapters.json'
    progress = json.loads(progress_path.read_text(encoding='utf-8')) if progress_path.exists() else {'chapters': {}, 'entries': entries}
    if progress['entries'] != entries:
        raise RuntimeError('目录发生变化，暂停以便核查原进度。')
    all_paths = []
    chapter_pdfs=[]
    for index, expected in enumerate(entries):
        library.check_space()
        chapter_pdf=work/f'chapter_{index+1:04d}.pdf'
        complete=progress.get('completed_entries',{}).get(str(index))
        if complete and chapter_pdf.exists() and file_hash(chapter_pdf)==complete['sha256']:
            valid=all((work / name).exists() and file_hash(work / name)==digest for name,digest in complete['images'].items())
            if valid:
                validate_pdf(chapter_pdf,complete['pages'])
                chapter_pdfs.append(chapter_pdf)
                print(f'目录 {index+1}/{len(entries)} 已完整保存，跳过：{expected}')
                continue
        print(f'打开杂志目录 {index+1}/{len(entries)}：{expected}')
        page.get_by_text(expected,exact=True).first.click(timeout=10000)
        print(f'等待正文目录 {index+1}/{len(entries)} 完整加载……')
        sources,signature,modal=wait_chapter(page,expected,None,settings)
        snapshot=body_snapshot(modal,re.sub(r'^\s*\d+\s+','',expected))
        mixed=snapshot['text_length']>40
        chapter_images=[]
        mapping={}
        for image_index, item in enumerate(sources):
            path = work / f'chapter_{index+1:04d}_{image_index+1:03d}.jpg'
            key = f'{index}:{image_index}'
            old = progress['chapters'].get(key)
            if old and path.exists() and file_hash(path)==old['sha256']:
                size=tuple(old['size'])
            else:
                part=path.with_suffix('.part')
                size, source=download_best(context,item,page.url,part,library)
                if not mixed:
                    check_resolution(size,settings)
                part.replace(path)
                progress['chapters'][key]={'sha256':file_hash(path),'size':list(size),'source':source,'title':expected}
                save_json(progress_path,progress)
            if not mixed:
                check_resolution(size,settings)
            all_paths.append(path)
            chapter_images.append(path)
            mapping[item['src']]=path
            mapping[item.get('dom_src',item['src'])]=path
            print(f'正文目录 {index+1}/{len(entries)}，原图{size[0]}×{size[1]}')
        library.check_space(sum(p.stat().st_size for p in chapter_images)*3+64*1024*1024)
        if mixed:
            print_body(context,snapshot,mapping,chapter_pdf)
        else:
            build_pdf(chapter_images,chapter_pdf)
        pages=validate_pdf(chapter_pdf)
        progress.setdefault('completed_entries',{})[str(index)]={'sha256':file_hash(chapter_pdf),'pages':pages,'images':{p.name:file_hash(p) for p in chapter_images}}
        save_json(progress_path,progress)
        chapter_pdfs.append(chapter_pdf)
        close_chapter(modal)
    library.check_space(sum(p.stat().st_size for p in chapter_pdfs)*2+64*1024*1024)
    page_count=merge_chapters(chapter_pdfs,output,entries)
    library.complete(task,output,page_count)
    library.update(task,'已完成',mode='目录正文',chapter_count=len(entries),body_image_sizes=[x['size'] for x in progress['chapters'].values()])
    if not settings['keep_page_images']:
        import shutil
        shutil.rmtree(work)
