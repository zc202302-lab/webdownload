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
from library_manager import StorageFull
from chapter_dom import HELPERS


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
    modal = next_button.locator('xpath=ancestor::*[@role="dialog" or contains(concat(" ",normalize-space(@class)," ")," modal-content ") or .//*[self::img or self::p or self::table]][1]')
    tag = modal.evaluate('(e)=>e.tagName')
    if tag in ('BODY', 'HTML'):
        raise RuntimeError('未能识别正文弹窗，已停止以避免保存广告或导航。')
    return modal


def body_snapshot(modal, title):
    return modal.evaluate(r'''(e,title) => {''' + HELPERS + r'''
        const selector='.modal-body,.chapter-content,.article-content,#chapterContent,#articleContent';
        const found=e.matches(selector) ? e : e.querySelector(selector);
        const body=found || e;
        const visible=x=>shown(x) && !!(x.getBoundingClientRect().width && x.getBoundingClientRect().height);
        const restricted=[...e.querySelectorAll('.locked-content,.paywall')].some(visible);
        let pending=e.getAttribute('aria-busy')==='true' || [...e.querySelectorAll('[aria-busy="true"],.loading,.spinner,.load-error,.error-message,[role="alert"]')].some(visible);
        const unsupported=[...body.querySelectorAll('iframe,canvas,video,object,embed,svg')].some(x=>shown(x) && !x.closest(excluded));
        const clone=body.cloneNode(true);
        const originals=[body,...body.querySelectorAll('*')];
        const copies=[clone,...clone.querySelectorAll('*')];
        originals.forEach((x,i)=>{
          if(i && (!shown(x) || x.matches(excluded))) copies[i].remove();
          if(x.tagName==='IMG') {
            if(!includedImage(x)) copies[i].remove();
            else copies[i].setAttribute('src',imageIdentity(x));
          }
        });
        clone.querySelectorAll('script,style,iframe,source,button,nav,.modal-header,.modal-footer,.chapter-nav,.chapter-navigation,.close').forEach(x=>x.remove());
        clone.querySelectorAll('a,[role=button]').forEach(x=>{
            if(/上一章|下一章|关闭/.test(x.textContent)) x.remove();
        });
        const norm=s=>s.replace(/\s+/g,'').replace(/^\d+/,'');
        clone.querySelectorAll('h1,h2,h3,h4,.modal-title').forEach(x=>{
            if(norm(x.textContent)===norm(title)) x.remove();
        });
        if(/^(加载中|正在加载|加载失败|网络错误|请稍候|loading|failed to load)[.。…!！\s]*$/i.test(clone.textContent.trim())) pending=true;
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
        return {html:clone.innerHTML,text_length:clone.textContent.trim().length,image_urls:[...clone.querySelectorAll('img')].map(im=>im.getAttribute('src')),body_identified:!!found,restricted,pending,unsupported};
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
    return modal.locator('img').evaluate_all(r'''async es => {''' + HELPERS + r'''
      const body=es.filter(includedImage);
      return await Promise.all(body.map(async e=>{
        let ready=e.complete && e.naturalWidth>0 && e.naturalHeight>0;
        if(ready && e.decode) { try { await e.decode(); } catch { ready=false; } }
        return {
        src:imageIdentity(e),
        dom_src:e.src,
        sources:[e.getAttribute('data-original'),e.getAttribute('data-src'),e.getAttribute('data-large'),...((e.getAttribute('srcset') || '').split(',').map(s=>s.trim().split(/\s+/)[0]))].filter(Boolean).map(s=>new URL(s,e.baseURI).href),
        width:e.naturalWidth,height:e.naturalHeight,ready
        };
      }));
    }''')


def empty_body(snapshot, sources):
    return (snapshot.get('body_identified', False) and snapshot['text_length']==0
            and not sources and not snapshot.get('image_urls')
            and not any(snapshot.get(key) for key in ('restricted','pending','unsupported')))


def wait_chapter(page, expected, previous, settings):
    deadline=time.monotonic()+max(300.0,float(settings.get('chapter_wait_seconds',300)))
    stable_since=None
    last_signature=None
    empty_since=None
    empty_signature=None
    empty_result=None
    partial_result=None
    partial_since=None
    partial_signature=None
    diagnostics={'reasons':['未识别正文弹窗']}
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
                    candidate=heading.locator('xpath=ancestor::*[@role="dialog" or contains(concat(" ",normalize-space(@class)," ")," modal-content ") or .//img or .//p or .//table][1]')
                    if candidate.evaluate('(e)=>e.tagName') not in ('BODY','HTML'):
                        modal=candidate
                        break
            if modal is None:
                modal=modal_body(visible_next(page))
            text=modal.inner_text()
            scroll=advance_scroll(modal)
            snapshot=body_snapshot(modal,title)
            sources=image_sources(modal)
            if snapshot.get('restricted'):
                raise PermissionError(f'正文存在权限锁定提示，未保存本章：{expected}')
            signature=json.dumps([[(im['src'],im['width'],im['height']) for im in sources],snapshot['html'],scroll['height']])
            captured={url for im in sources for url in (im['src'],im.get('dom_src',im['src']))}
            all_body_images=all(url in captured for url in snapshot.get('image_urls',[]))
            title_matches=re.sub(r'\s+','',title) in re.sub(r'\s+','',text)
            structural=(snapshot.get('body_identified',True) and title_matches and signature != previous
                        and scroll['bottom'] and not snapshot.get('pending') and not snapshot.get('unsupported') and all_body_images)
            ready=(bool(sources) or snapshot['text_length']>0) and all(im['ready'] for im in sources) and structural
            is_empty=empty_body(snapshot,sources) and title in text and scroll['bottom']
            reasons=[]
            for condition,reason in ((not snapshot.get('body_identified',True),'未识别正文区域'),
                    (not title_matches,'正文标题不匹配'),(not scroll['bottom'],'正文未滚到底'),
                    (snapshot.get('pending'),'正文仍有加载或错误提示'),(snapshot.get('unsupported'),'正文存在未支持的媒体'),
                    (not all_body_images,'正文图片与采集列表不一致'),
                    (any(not im['ready'] for im in sources),'图片未加载或解码失败')):
                if condition: reasons.append(reason)
            diagnostics={'reasons':reasons,'images':[{'position':i+1,'ready':im['ready'],
                         'width':im['width'],'height':im['height']} for i,im in enumerate(sources)]}
            if structural and sources and snapshot.get('body_identified',False):
                if signature!=partial_signature:
                    partial_signature,partial_since=signature,time.monotonic()
                partial_result=(sources,signature,modal)
            else:
                partial_result,partial_since,partial_signature=None,None,None
        except (RuntimeError,PlaywrightError):
            ready=False
            is_empty=False
            partial_result,partial_since,partial_signature=None,None,None
            diagnostics={'reasons':['识别正文弹窗或读取页面失败']}
        if is_empty:
            if signature!=empty_signature:
                empty_since=time.monotonic()
                empty_signature=signature
            empty_result=(sources,signature,modal)
        else:
            empty_since=None
            empty_signature=None
            empty_result=None
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
    # Empty content waits the full configured interval (at least 300 seconds).
    # A last-moment DOM change, missing image or unresolved loader cannot pass.
    if empty_result and empty_since is not None and time.monotonic()-empty_since>=30:
        return empty_result
    if settings.get('_diagnostic_path'):
        save_json(settings['_diagnostic_path'],dict(diagnostics,title=expected,wait_seconds=max(300,float(settings.get('chapter_wait_seconds',300)))))
    if settings.get('_allow_missing_images') and partial_result and partial_since is not None and time.monotonic()-partial_since>=30:
        print('正文已稳定，但存在未就绪图片；尝试下载实际原图，失败计入本杂志缺失数。')
        return partial_result
    detail='；'.join(diagnostics['reasons']) or '正文内容未稳定或仍为空'
    raise RuntimeError(f'正文等待至少300秒后仍未通过检查：{detail}；未保存本章：{expected}')


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
    from modal_close import close_by_backdrop
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
    print('未找到正文弹窗右上角叉号，尝试点击左右遮罩区域关闭……')
    if close_by_backdrop(modal):
        return
    raise RuntimeError('没有找到正文弹窗右上角叉号，点击左右遮罩后仍未确认关闭，整批暂停。')


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
    from missing_records import MissingRecords
    missing=MissingRecords(library,task,progress,progress_path)
    all_paths = []
    chapter_pdfs=[]
    rendered_entries=[]
    blank_entries=[]
    for index, expected in enumerate(entries):
        library.check_space()
        chapter_pdf=work/f'chapter_{index+1:04d}.pdf'
        complete=progress.get('completed_entries',{}).get(str(index))
        if complete and complete.get('missing_only') and complete.get('title')==expected:
            print(f'目录 {index+1}/{len(entries)} 图片已记录缺失，跳过：{expected}')
            continue
        if complete and complete.get('empty') and complete.get('title')==expected:
            blank_entries.append(expected)
            print(f'目录 {index+1}/{len(entries)} 已记录为空白，跳过：{expected}')
            continue
        if complete and chapter_pdf.exists() and file_hash(chapter_pdf)==complete['sha256']:
            valid=all((work / name).exists() and file_hash(work / name)==digest for name,digest in complete['images'].items())
            if valid:
                validate_pdf(chapter_pdf,complete['pages'])
                chapter_pdfs.append(chapter_pdf)
                rendered_entries.append(expected)
                print(f'目录 {index+1}/{len(entries)} 已完整保存，跳过：{expected}')
                continue
        print(f'打开杂志目录 {index+1}/{len(entries)}：{expected}')
        page.get_by_text(expected,exact=True).first.click(timeout=10000)
        print(f'等待正文目录 {index+1}/{len(entries)} 完整加载……')
        wait_settings=dict(settings,_allow_missing_images=True,_diagnostic_path=work/f'chapter_{index+1:04d}_diagnostic.json')
        sources,signature,modal=wait_chapter(page,expected,None,wait_settings)
        snapshot=body_snapshot(modal,re.sub(r'^\s*\d+\s+','',expected))
        if snapshot.get('restricted'):
            raise PermissionError(f'保存前发现正文权限锁定：{expected}')
        if snapshot.get('pending') or snapshot.get('unsupported'):
            raise RuntimeError(f'保存前正文仍有加载提示或未支持的媒体：{expected}')
        captured={url for im in sources for url in (im['src'],im.get('dom_src',im['src']))}
        if not all(url in captured for url in snapshot.get('image_urls',[])):
            raise RuntimeError(f'正文在保存前发生变化，图片列表不一致：{expected}')
        if empty_body(snapshot,sources):
            close_chapter(modal)
            progress.setdefault('completed_entries',{})[str(index)]={'empty':True,'title':expected}
            save_json(progress_path,progress)
            blank_entries.append(expected)
            library.update(task,'处理中',blank_entries=blank_entries)
            print(f'目录 {index+1}/{len(entries)} 无文字和图片，记录为空白并跳过：{expected}')
            continue
        if not sources and not snapshot['text_length']:
            raise RuntimeError(f'正文在保存前发生变化，未能确认是否空白：{expected}')
        mixed=snapshot['text_length']>0
        chapter_images=[]
        mapping={}
        for image_index, item in enumerate(sources):
            path = work / f'chapter_{index+1:04d}_{image_index+1:03d}.jpg'
            key = f'{index}:{image_index}'
            old = progress['chapters'].get(key)
            candidates=set(item.get('sources',[])+[item['src'],item.get('dom_src',item['src'])])
            if old and old.get('source') in candidates and path.exists() and file_hash(path)==old['sha256']:
                size=tuple(old['size'])
            else:
                part=path.with_suffix('.part')
                try:
                    size, source=download_best(context,item,page.url,part,library)
                    if not mixed:
                        check_resolution(size,settings)
                except (StorageFull,PermissionError):
                    raise
                except RuntimeError as exc:
                    missing.add(index,expected,image_index,item,'正文原图失败：'+str(exc))
                    # Remove only this failed DOM image from text/image export;
                    # preserve captions and all other body content.
                    aliases={item['src'],item.get('dom_src',item['src'])}
                    removed=[False]
                    def remove_failed(match):
                        src=re.search(r'\bsrc\s*=\s*([\"\x27])(.*?)\1',match.group(0),re.I|re.S)
                        if not removed[0] and src and html.unescape(src[2]) in aliases:
                            removed[0]=True
                            return ''
                        return match.group(0)
                    snapshot['html']=re.sub(r'<img\b[^>]*>',remove_failed,snapshot['html'],flags=re.I)
                    continue
                part.replace(path)
                progress['chapters'][key]={'sha256':file_hash(path),'size':list(size),'source':source,'title':expected}
                save_json(progress_path,progress)
            missing.recovered(index,image_index)
            if not mixed:
                check_resolution(size,settings)
            all_paths.append(path)
            chapter_images.append(path)
            mapping[item['src']]=path
            mapping[item.get('dom_src',item['src'])]=path
            print(f'正文目录 {index+1}/{len(entries)}，原图{size[0]}×{size[1]}')
        library.check_space(sum(p.stat().st_size for p in chapter_images)*3+64*1024*1024)
        if not chapter_images and not mixed:
            close_chapter(modal)
            progress.setdefault('completed_entries',{})[str(index)]={'missing_only':True,'title':expected}
            save_json(progress_path,progress)
            continue
        if mixed:
            print_body(context,snapshot,mapping,chapter_pdf)
        else:
            build_pdf(chapter_images,chapter_pdf)
        pages=validate_pdf(chapter_pdf)
        close_chapter(modal)
        progress.setdefault('completed_entries',{})[str(index)]={'sha256':file_hash(chapter_pdf),'pages':pages,'images':{p.name:file_hash(p) for p in chapter_images}}
        save_json(progress_path,progress)
        chapter_pdfs.append(chapter_pdf)
        rendered_entries.append(expected)
    if not chapter_pdfs:
        library.update(task,'已完成',pdf='',pages=0,bytes=0,mode='目录正文',
                       blank_entries=blank_entries,chapter_count=len(entries),
                       empty_record=progress_path.relative_to(library.root).as_posix(),
                       empty_record_sha256=file_hash(progress_path),reason='未生成PDF；缺失图片已记录' if progress['missing_images'] else '全部目录均为空白，已跳过；未生成PDF')
        missing.sync()
        return
    library.check_space(sum(p.stat().st_size for p in chapter_pdfs)*2+64*1024*1024)
    page_count=merge_chapters(chapter_pdfs,output,rendered_entries)
    library.complete(task,output,page_count)
    library.update(task,'已完成',mode='目录正文',chapter_count=len(entries),blank_entries=blank_entries,
                   reason=('缺失'+str(len(progress['missing_images']))+'张图片，已按允许≤3张视为成功；详见缺失记录.txt。' if progress['missing_images'] else '')
                         +(f'已跳过{len(blank_entries)}个空白条目：'+ '；'.join(blank_entries) if blank_entries else ''),
                   body_image_sizes=[x['size'] for x in progress['chapters'].values()])
    missing.sync()
    if not settings['keep_page_images']:
        import shutil
        shutil.rmtree(work)

