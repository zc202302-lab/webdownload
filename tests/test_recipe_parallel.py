"""Bounded async scheduling, dedupe, failure isolation and fatal cancellation."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('ebook_collector').resolve()))
import recipe_collector as recipes
import recipe_parallel as parallel

def item(n):return {'title':f'菜{n}','source':f'https://www.easteat.com/book_list.php?action=cookbook_detail&id={n}'}
assert recipes.video_only({'ingredients':[{'items':[{'name':'见视频','amount':'见视频'}]}],'sections':[]})
assert recipes.video_only({'ingredients':[{'items':[{'name':'盐','amount':'见视频'},{'name':'土豆','amount':'200克'}]}],'sections':[]})
assert recipes.video_only({'ingredients':[],'sections':[{'heading':'制作步骤','text':'制作步骤\n详见视频'}]})
assert not recipes.video_only({'ingredients':[{'items':[{'name':'土豆','amount':'200克'}]}],'sections':[{'heading':'制作步骤','text':'制作步骤\n切片煎熟，可见视频演示'}]})
assert parallel.list_key(recipes.LIST_URL+'#top')==parallel.list_key(recipes.LIST_URL+'&page=1')

class Context:
 def __init__(self,lists):self.lists=lists;self.pages=[]
 async def new_page(self):
  p=Page(self);self.pages.append(p);return p
class Page:
 def __init__(self,context):self.context=context;self.url='';self.closed=False
 async def goto(self,url,**kwargs):self.url=url;await asyncio.sleep(.001)
 async def evaluate(self,script):
  assert script==recipes.LIST_SCRIPT
  return self.context.lists[self.url]
 async def close(self):self.closed=True

async def checks():
 with tempfile.TemporaryDirectory() as folder:
  store=recipes.RecipeStore(Path(folder)/'output',1)
  url2=recipes.LIST_URL+'&page=2';url3=recipes.LIST_URL+'&page=3'
  lists={recipes.LIST_URL:{'items':[item(1),item(2)],'current':'1','next':url2,'links':[url2,url3]},
   url2:{'items':[item(2),item(3)],'current':'2','next':url3,'links':[]},
   url3:{'items':[item(4),item(5)],'current':'3','next':None,'links':[]}}
  context=Context(lists);active=peak=0;calls={}
  async def collect(ctx,page,entry,book_store,settings,*args):
   nonlocal active,peak
   identifier=recipes.recipe_id(entry['source']);calls[identifier]=calls.get(identifier,0)+1
   active+=1;peak=max(peak,active)
   try:
    await asyncio.sleep(.015)
    if identifier=='3':raise RuntimeError('ordinary failure')
    book_store.update(entry,'已跳过见视频',reason='fixture')
   finally:active-=1
  async def no_delay(seconds):await real_sleep(0)
  real_sleep=asyncio.sleep
  with patch.object(parallel,'collect',side_effect=collect):
   await parallel.pipeline(context,store,{'recipe_workers':3})
  assert 1<peak<=3 and calls=={'1':1,'2':1,'3':2,'4':1,'5':1}
  assert store.db['scan_complete'] and not store.db['download_complete']
  assert store.db['recipes']['3']['status']=='下载失败待重试'
  assert 'ordinary failure' in (store.root/'失败记录.txt').read_text(encoding='utf-8-sig')
  assert all(p.closed for p in context.pages)
  # Previously skipped video-only entries are not reopened; failed entries retry.
  calls.clear()
  async def recovered(*args):
   entry=args[2];calls[recipes.recipe_id(entry['source'])]=1;args[3].update(entry,'已跳过见视频',reason='fixture')
  with patch.object(parallel,'collect',side_effect=recovered):await parallel.pipeline(Context(lists),store,{})
  assert calls=={'3':1} and store.db['download_complete']
  # Fatal permission errors cancel running work and leave scan/download incomplete.
  for permission in (PermissionError('locked'),parallel.StorageFull('no space')):
   context=Context(lists);fatal_store=recipes.RecipeStore(Path(folder)/str(type(permission).__name__)/'output',1)
   async def fail(*args):raise permission
   with patch.object(parallel,'collect',side_effect=fail):
    try:await parallel.pipeline(context,fatal_store,{})
    except type(permission):pass
    else:raise AssertionError('fatal error swallowed')
   assert not fatal_store.db['download_complete'] and all(p.closed for p in context.pages)
  # Concurrent requests for identical images share the same in-flight download.
  pool=parallel.ImagePool(None,store.storage,2);downloads=[]
  async def image_download(key,referer):downloads.append(key);await asyncio.sleep(.01);return ((10,20),b'bytes',key[0])
  with patch.object(pool,'download',side_effect=image_download):
   result=await asyncio.gather(*(pool.fetch({'sources':[],'src':'https://image.test/a.jpg'},'ref') for _ in range(5)))
  assert len(downloads)==1 and all(r==result[0] for r in result)
  pool=parallel.ImagePool(None,store.storage,2);downloads.clear()
  with patch.object(pool,'download',side_effect=image_download):
   first=asyncio.create_task(pool.fetch({'sources':[],'src':'https://image.test/b.jpg'},'ref'))
   other=asyncio.create_task(pool.fetch({'sources':[],'src':'https://image.test/b.jpg'},'ref'))
   await asyncio.sleep(.001);first.cancel()
   await asyncio.gather(first,return_exceptions=True)
   assert await other and len(downloads)==1

 # Identical URLs with different recipe referrers must not share bytes.
 pool=parallel.ImagePool(None,store.storage,8)
 async def referer_image(key,referer):return ((10,20),referer.encode(),key[0])
 with patch.object(pool,'download',side_effect=referer_image) as download:
  first,second=await asyncio.gather(pool.fetch({'sources':[],'src':'https://image.test/same.jpg'},'recipe1'),pool.fetch({'sources':[],'src':'https://image.test/same.jpg'},'recipe2'))
 assert first[1]!=second[1] and download.call_count==2
 # Healthy completion ramps to 5/8, errors reduce and enforce recovery cooldown.
 health=parallel.AdaptiveTune(5,8)
 assert (health.recipes.limit,health.images.limit)==(3,4)
 for _ in range(6):await health.success()
 assert (health.recipes.limit,health.images.limit)==(5,8)
 with patch.object(parallel.time,'monotonic',return_value=100):
  await health.failure();assert (health.recipes.limit,health.images.limit)==(2,4)
  for _ in range(10):await health.success()
  assert (health.recipes.limit,health.images.limit)==(2,4)
 with patch.object(parallel.time,'monotonic',return_value=131):
  for _ in range(3):await health.success()
  assert (health.recipes.limit,health.images.limit)==(3,6)
 target=item(1)
 parallel.check_identity(target['source'],target,{'title':'菜1 VIP免费'})
 for url,title in [(item(2)['source'],'菜1'),(target['source'],'菜2')]:
  try:parallel.check_identity(url,target,{'title':title})
  except RuntimeError:pass
  else:raise AssertionError('cross-recipe content accepted')
 assert parallel.origin_label(dict(target,_list_page='12',_list_total=1679,_list_position=3,_list_count=12))=='列表第12/1679页 · 本页第3/12道'
 print('PASS: async recipe concurrency bound, multi-page dedupe, video skip, failure retry/report/resume, fatal cancellation and shared image dedupe')
asyncio.run(checks())
