"""Real Chromium fixture: concurrent list/detail tabs, original-image dedupe and PDFs."""
import asyncio
import base64
import io
import json
import hashlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlparse
from unittest.mock import patch
from PIL import Image
from playwright.async_api import async_playwright
sys.path.insert(0,str(Path('ebook_collector').resolve()))
import recipe_parallel as p
import recipe_collector as r

buf=io.BytesIO();Image.new('RGB',(160,240),'#d9e8f0').save(buf,format='JPEG');image=buf.getvalue()
preview_buf=io.BytesIO();Image.new('RGB',(80,120),'#d9e8f0').save(preview_buf,format='JPEG');preview=preview_buf.getvalue();preview_uri='data:image/jpeg;base64,'+base64.b64encode(preview).decode()
requests=[]
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  requests.append(self.path);self.send_response(200);self.send_header('Content-Type','image/jpeg');self.end_headers();self.wfile.write(image if self.path=='/original.jpg' else preview)
 def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
async def check():
 with tempfile.TemporaryDirectory() as folder:
  store=r.RecipeStore(Path(folder)/'output',1);active=peak=0;detail_calls=[];scan_active=scan_peak=0
  async with async_playwright() as pw:
   browser=await pw.chromium.launch(headless=True,**({'channel':'msedge'} if sys.platform=='win32' else {}))
   context=await browser.new_context()
   async def route(handler):
    nonlocal scan_active,scan_peak
    query=parse_qs(urlparse(handler.request.url).query)
    if query.get('action')==['cookbook_list']:
     scan_active+=1;scan_peak=max(scan_peak,scan_active)
     try:
      await asyncio.sleep(.1)
      number=int(query.get('page',['1'])[0]);ids={1:[1,2],2:[2,3],3:[4,5]}[number]
      body='<div class="cookbook-list">'+''.join(f'<div class="cookbook-item"><a href="?action=cookbook_detail&id={i}"><span class="cookbook-name">Recipe {i}</span></a></div>' for i in ids)+'</div>'
      body+='<div class="pagination">'+''.join(f'<a href="?action=cookbook_list&page={i}" class="'+('active' if i==number else '')+f'">{i}</a>' for i in [1,2,3])+'</div>'
      await handler.fulfill(status=200,body=body,content_type='text/html; charset=utf-8')
     finally:scan_active-=1
    else:
     number=int(query['id'][0]);detail_calls.append(number)
     ingredient='见视频' if number==4 else '土豆'
     amount='见视频' if number==4 else '200克'
     steps='见视频' if number==4 else '切片，然后煎熟。'
     if number==5:steps='详细制作步骤请查看专题内容'
     body=f'<div class="container"><div class="page-header"><h1 class="cookbook-title">Recipe {number}</h1></div><div class="images-gallery"><img src="{preview_uri}" data-original="http://127.0.0.1:{server.server_port}/original.jpg"></div><div class="section"><h3 class="section-title">食材准备</h3><div class="food-category"><h4>主料</h4><ul class="food-list"><li><span>{ingredient}</span><span>{amount}</span></li></ul></div></div><div class="section"><h3 class="section-title">制作步骤</h3><div class="steps"><p>{steps}</p></div></div></div>'
     if number==5:
      body='<div class="container"><div class="page-header"><h1 class="cookbook-title">Recipe 5</h1></div><div class="section"><h3 class="section-title">食材准备</h3><p>详细食材清单请查看专题内容</p></div><div class="section"><h3 class="section-title">制作步骤</h3><p>详细制作步骤请查看专题内容</p></div></div>'
     await handler.fulfill(status=200,body=body,content_type='text/html; charset=utf-8')
   await context.route('https://www.easteat.com/book_list.php**',route)
   original=p.collect
   async def observed(*args):
    nonlocal active,peak
    active+=1;peak=max(peak,active)
    try:return await original(*args)
    finally:active-=1
   with patch.object(p,'collect',side_effect=observed):await p.pipeline(context,store,{})
   assert 1<peak<=5 and scan_peak==2
   assert sorted(detail_calls)==[1,2,3,4,5] and requests.count('/original.jpg')==1
   assert store.db['scan_complete'] and store.db['download_complete']
   assert store.db['recipes']['4']['status']=='已跳过见视频'
   assert store.db['recipes']['5']['status']=='已跳过专题内容'
   assert len(list((store.root/'菜谱').glob('*.pdf')))==3
   for number in (1,2,3):
    entry={'title':f'Recipe {number}','source':r.LIST_URL.replace('cookbook_list','cookbook_detail')+f'&id={number}'}
    assert store.done(entry)
    _,data_path=store.paths(entry)
    data=json.loads(data_path.read_text(encoding='utf-8'))
    assert data['images'][0]['size']==[160,240]
    assert data['images'][0]['sha256']==hashlib.sha256(image).hexdigest()
    assert data['ingredients'][0]['items'][0]['amount']=='200克'
   previous=list(detail_calls);await p.pipeline(context,store,{})
   assert detail_calls==previous and requests.count('/original.jpg')==1
   await context.close();await browser.close()
 print('PASS: real browser multi-page overlap, 3-recipe bound, one original-image request, complete PDF/data validation, video skip and restart without duplicate downloads')
try:asyncio.run(asyncio.wait_for(check(),timeout=120))
finally:server.shutdown();server.server_close()
