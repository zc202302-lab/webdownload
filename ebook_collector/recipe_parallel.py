"""Bounded async recipe pipeline; all Playwright objects stay on one event loop."""
import asyncio
import html
import io
import json
import re
import shutil
import time
from collections import OrderedDict, deque
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from PIL import Image
from playwright.async_api import async_playwright, Error as PlaywrightError
from chapter_reader import body_document
from collector import ROOT, verified
from library_manager import StorageFull, file_hash, read_settings, save_json, validate_pdf
from recipe_collector import BODY_SCRIPT, LIST_SCRIPT, LIST_URL, RecipeStore, recipe_id, video_only


def limits(settings):
    result=[]
    for key,default,maximum in [('recipe_workers',3,4),('recipe_scan_workers',2,3),('recipe_image_workers',4,8)]:
        value=settings.get(key,default)
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=maximum:
            raise ValueError(f'{key}必须为1到{maximum}的整数')
        result.append(value)
    return result


def list_key(url):
    parsed=urlparse(url);query=parse_qs(parsed.query)
    if parsed.scheme not in ('http','https') or parsed.hostname!='www.easteat.com' or parsed.path!='/book_list.php' or query.get('action')!=['cookbook_list']:
        raise RuntimeError('列表分页链接异常，已停止。')
    if 'page' in query and (len(query['page'])!=1 or not query['page'][0].isdigit() or int(query['page'][0])<1):
        raise RuntimeError('列表页码异常，已停止。')
    pairs=sorted((k,v) for k,v in parse_qsl(parsed.query) if not (k=='page' and int(v)==1))
    return urlunparse(parsed._replace(query=urlencode(pairs),fragment=''))


async def goto(page,url):
    for attempt in range(2):
        try:
            response=await page.goto(url,wait_until='domcontentloaded',timeout=45000)
            if response and response.status in (401,403,429):
                raise PermissionError(f'网站返回{response.status}，整批暂停，请检查权限或访问频率。')
            if response and not response.ok:raise RuntimeError(f'页面返回HTTP {response.status}')
            return
        except PermissionError:raise
        except (PlaywrightError,RuntimeError):
            if attempt:raise
            await asyncio.sleep(2)


async def wait_body(page,settings):
    deadline=time.monotonic()+max(60,float(settings.get('chapter_wait_seconds',60)))
    previous=stable=None
    while time.monotonic()<deadline:
        snapshot=await page.evaluate(BODY_SCRIPT)
        if snapshot and snapshot['locked']:raise PermissionError('菜谱正文锁定，请确认完整阅读权限，整批暂停。')
        ready=snapshot and (video_only(snapshot) or (snapshot['has_steps'] and snapshot['sources'] and all(im['ready'] for im in snapshot['sources'])))
        if ready:
            signature=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
            if signature!=previous:previous,stable=signature,time.monotonic()
            elif time.monotonic()-stable>=2:return snapshot
        else:previous=stable=None
        await asyncio.sleep(.25)
    raise RuntimeError('菜谱正文或图片未完整加载，等待至少60秒，进度保留。')


class ImagePool:
    def __init__(self,context,storage,workers):
        self.context,self.storage=context,storage
        self.semaphore=asyncio.Semaphore(workers)
        self.inflight={};self.cache=OrderedDict();self.bytes=0

    async def fetch(self,image,referer):
        key=tuple(dict.fromkeys(image['sources']+[image['src']]))
        if key in self.cache:
            self.cache.move_to_end(key);return self.cache[key]
        if key not in self.inflight:
            task=asyncio.create_task(self.load(key,referer))
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            self.inflight[key]=task
        # One recipe's ordinary failure must not cancel a shared image used elsewhere.
        return await asyncio.shield(self.inflight[key])

    async def load(self,key,referer):
        try:
            result=await self.download(key,referer)
            if len(result[1])<=32*1024*1024:
                self.cache[key]=result;self.bytes+=len(result[1])
                while self.bytes>32*1024*1024:
                    _,old=self.cache.popitem(last=False);self.bytes-=len(old[1])
            return result
        finally:self.inflight.pop(key,None)

    async def download(self,candidates,referer):
        best=None
        for url in candidates:
            if not url.startswith(('http://','https://')):continue
            async with self.semaphore:
                data=None
                for attempt in range(3):
                    response=None
                    try:
                        response=await self.context.request.get(url,headers={'Referer':referer},timeout=300000)
                        if response.status in (401,403,429):raise PermissionError(f'图片返回{response.status}，整批暂停。')
                        if response.status>=500:raise RuntimeError(f'图片服务器HTTP {response.status}')
                        if response.ok:data=await response.body()
                        break
                    except PermissionError:raise
                    except (PlaywrightError,RuntimeError):
                        if attempt==2:raise RuntimeError('原图请求3次仍失败，单次最多300秒。') from None
                        await asyncio.sleep(2)
                    finally:
                        if response is not None:await response.dispose()
            if not data:continue
            self.storage.check_space(len(data)*3)
            try:
                with Image.open(io.BytesIO(data)) as im:size=im.size;im.load()
                with Image.open(io.BytesIO(data)) as im:im.verify()
            except (OSError,ValueError):continue
            if best is None or size[0]*size[1]>best[0][0]*best[0][1]:best=(size,data,url)
        if best is None:raise RuntimeError('正文原图下载或完整解码失败。')
        return best


async def export_pdf(context,snapshot,mapping,path):
    page=await context.new_page()
    async def abort(route):await route.abort()
    try:
        await page.route('http://**/*',abort);await page.route('https://**/*',abort)
        await page.set_content(body_document(snapshot,mapping),wait_until='load')
        await page.evaluate('async()=>{await document.fonts.ready;await Promise.all([...document.images].map(im=>im.decode()));}')
        if not await page.locator('img').evaluate_all('es=>es.every(e=>e.src.startsWith("data:")&&e.complete&&e.naturalWidth>0)'):
            raise RuntimeError('PDF存在未嵌入图片，未提交完成。')
        await page.pdf(path=str(path),format='A4',print_background=True,margin={'top':'12mm','bottom':'12mm','left':'12mm','right':'12mm'},display_header_footer=False)
        return validate_pdf(path)
    finally:await page.close()


async def collect(context,page,item,store,settings,pool,pdf_semaphore):
    snapshot=await wait_body(page,settings)
    if video_only(snapshot):
        store.update(item,'已跳过见视频',reason='正文仅提示见视频，无实际配方或步骤；未生成PDF。')
        print(f'跳过见视频：{item["title"]}');return
    identifier=recipe_id(item['source']);pdf,data_path=store.paths(item);pdf.parent.mkdir(parents=True,exist_ok=True)
    work=store.root/'_work'/identifier;work.mkdir(parents=True,exist_ok=True)
    manifest_path=work/'images.json';manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    mapping={}
    async def image_task(index,image):
        path=work/f'{index:04d}.jpg';old=manifest.get(str(index),{});identity=[image['src'],image['sources']]
        if not (old.get('identity')==identity and old.get('sha256') and verified(path,old.get('sha256'))):
            size,data,source=await pool.fetch(image,page.url)
            store.storage.check_space(len(data)*3)
            part=path.with_suffix('.part');part.write_bytes(data);part.replace(path)
            manifest[str(index)]={'identity':identity,'sha256':file_hash(path),'size':list(size),'source':source}
            save_json(manifest_path,manifest)
        mapping[image['src']]=path;mapping[image['dom_src']]=path
    image_tasks=[asyncio.create_task(image_task(i,im)) for i,im in enumerate(snapshot['sources'])]
    try:await asyncio.gather(*image_tasks)
    finally:
        for task in image_tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*image_tasks,return_exceptions=True)
    # A shared browser is printed by one task at a time, while others keep downloading.
    async with pdf_semaphore:
        store.storage.check_space(sum(p.stat().st_size for p in work.glob('*.jpg'))*3+64*1024*1024)
        temporary=work/'recipe.pdf'
        count=await export_pdf(context,{'html':'<h1>'+html.escape(snapshot['title'])+'</h1>'+snapshot['html']},mapping,temporary)
        temporary.replace(pdf)
        data={**item,'id':identifier,'page_title':snapshot['title'],'sections':snapshot['sections'],'ingredients':snapshot['ingredients'],
              'images':[manifest[str(i)] for i in range(len(snapshot['sources']))],'scope':'网页版文字与图片，不含APP视频'}
        save_json(data_path,data)
        store.update(item,'已完成',pdf=pdf.relative_to(store.root).as_posix(),data=data_path.relative_to(store.root).as_posix(),pages=count,
                     pdf_sha256=file_hash(pdf),data_sha256=file_hash(data_path),reason='')
    if not settings.get('keep_page_images'):shutil.rmtree(work)


async def pipeline(context,store,settings):
    workers,scan_workers,image_workers=limits(settings)
    queue=asyncio.Queue(maxsize=workers*2);pool=ImagePool(context,store.storage,image_workers);pdf_semaphore=asyncio.Semaphore(1)
    seen=set();signatures={};failures=[]
    store.db.update(scan_complete=False,download_complete=False);store.flush()
    async def scan_one(url):
        page=await context.new_page()
        try:
            await goto(page,url)
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                snapshot=await page.evaluate(LIST_SCRIPT)
                if snapshot['items']:return snapshot
                await asyncio.sleep(.25)
            raise RuntimeError('列表60秒仍为空，未确认完整扫描。')
        finally:await page.close()
    async def producer():
        frontier=deque([LIST_URL]);scheduled={list_key(LIST_URL)};running={}
        try:
            while frontier or running:
                while frontier and len(running)<scan_workers:
                    url=frontier.popleft();running[asyncio.create_task(scan_one(url))]=url
                done,_=await asyncio.wait(running,return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    url=running.pop(task);snapshot=task.result()
                    signature=tuple(recipe_id(item['source']) for item in snapshot['items'])
                    if signature in signatures and signatures[signature]!=list_key(url):raise RuntimeError('不同分页重复返回同一列表，扫描停止。')
                    signatures[signature]=list_key(url)
                    for link in snapshot.get('links',[])+([snapshot['next']] if snapshot.get('next') else []):
                        key=list_key(link)
                        if key not in scheduled:scheduled.add(key);frontier.append(link)
                    store.db['current_list']=url;store.flush()
                    for item in snapshot['items']:
                        identifier=recipe_id(item['source'])
                        if not item['title']:raise RuntimeError('菜谱名称为空。')
                        if identifier in seen:continue
                        seen.add(identifier)
                        if store.done(item):continue
                        if store.db['recipes'].get(identifier,{}).get('status')=='已跳过见视频' and not settings.get('recipe_recheck_video',False):continue
                        await queue.put(item)
            store.db['scan_complete']=True;store.flush()
            for _ in range(workers):await queue.put(None)
        finally:
            for task in running:task.cancel()
            await asyncio.gather(*running,return_exceptions=True)
    async def worker(number):
        while True:
            item=await queue.get()
            try:
                if item is None:return
                store.storage.check_space();store.update(item,'处理中',reason='')
                for attempt in range(2):
                    page=None
                    try:
                        page=await context.new_page();await goto(page,item['source'])
                        print(f'并行任务{number}：{item["title"]}')
                        await collect(context,page,item,store,settings,pool,pdf_semaphore)
                        break
                    except (PermissionError,StorageFull) as exc:
                        store.update(item,'未完成已暂停',reason=str(exc)[:500]);raise
                    except Exception as exc:
                        if attempt==0:
                            print(f'重试菜谱：{item["title"]}');await asyncio.sleep(2)
                        else:
                            store.update(item,'下载失败待重试',reason=str(exc)[:500]);failures.append(item)
                            print(f'菜谱失败已记录，继续其他任务：{item["title"]}')
                    finally:
                        if page is not None:await page.close()
            finally:queue.task_done()
    tasks=[asyncio.create_task(producer())]+[asyncio.create_task(worker(i+1)) for i in range(workers)]
    try:
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.cancelled():raise asyncio.CancelledError
            if task.exception():raise task.exception()
        await asyncio.gather(*pending)
        store.db.update(download_complete=not failures,found=len(seen),failed=len(failures));store.flush()
        print(f'本轮结束：发现{len(seen)}道，失败{len(failures)}道。请查看菜谱目录及失败记录。')
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for task in pool.inflight.values():task.cancel()
        await asyncio.gather(*pool.inflight.values(),return_exceptions=True)


async def run():
    settings=read_settings(ROOT);store=RecipeStore(settings['output_path'],settings['reserve_gb']);store.storage.check_space()
    counts=limits(settings)
    print(f'并行配置：列表{counts[1]}页，菜谱{counts[0]}道，图片{counts[2]}张；PDF串行生成。')
    async with async_playwright() as playwright:
        context=None
        for channel in ('msedge','chrome',None):
            try:
                options=dict(headless=False,viewport={'width':1440,'height':1000},accept_downloads=True,chromium_sandbox=True)
                if channel:options['channel']=channel
                profile=ROOT/('browser_profile_'+channel if channel else 'browser_profile')
                context=await playwright.chromium.launch_persistent_context(str(profile),**options);break
            except PlaywrightError as exc:
                if not any(word in str(exc).lower() for word in ("executable doesn't exist",'distribution','executable does not exist')):raise
        if context is None:raise RuntimeError('未找到可用Edge/Chrome。')
        try:
            page=await context.new_page();await goto(page,LIST_URL)
            input('请在软件浏览器登录并确认完整阅读权限，再按确认开始并行下载：')
            await page.close();await pipeline(context,store,settings)
        finally:await context.close()


def main():asyncio.run(run())
