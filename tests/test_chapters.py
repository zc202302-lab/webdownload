import json,sys,types,tempfile,subprocess,os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright');sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=type('PWError',(Exception,),{}))
import chapter_reader as cr
import collector as c
from PIL import Image
from pypdf import PdfReader

# Test actual browser-side image extraction with ready, pending and icon images.
class Capture:
 def locator(self,*args):return self
 def evaluate_all(self,source):return source
js=cr.image_sources(Capture())
fixture='''const assert=require('assert');const mk=(w,h,nw,nh,attrs={})=>({complete:!!nh,naturalWidth:nw,naturalHeight:nh,currentSrc:'https://example.com/body.jpg',src:'https://example.com/body.jpg',baseURI:'https://example.com/',getBoundingClientRect:()=>({width:w,height:h}),getAttribute:k=>attrs[k]||null,scrollIntoView(){},async decode(){}});
const imgs=[mk(500,700,1200,1600,{'srcset':'large.jpg 1600w, small.jpg 800w'}),mk(500,0,0,0),mk(20,20,20,20)];
'''
code=fixture+'('+js+')(imgs).then(r=>{assert.equal(r.length,2);assert(r[0].ready);assert(!r[1].ready);assert(r[0].sources.includes("https://example.com/large.jpg"));console.log("PASS JS: pending images included, icons excluded, srcset resolved")})'
node=os.environ.get('CODEX_PRIMARY_RUNTIME_NODE','node')
subprocess.run([node,'-e',code],check=True)
# Simulated lazy loading cannot pass until every image is ready and stable.
clock=[0]
class Page:
 def get_by_text(self,*args,**kwargs):return types.SimpleNamespace(count=lambda:0)
 def wait_for_timeout(self,ms):clock[0]+=ms/1000
class Modal:
 def inner_text(self):return 'Chapter One'
page=Page();polls=[0]
def snapshots(modal):
 polls[0]+=1
 return [{'src':'body','width':1200,'height':1600,'ready':polls[0]>3}]
with patch.object(cr.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cr,'visible_next'),patch.object(cr,'modal_body',return_value=Modal()),patch.object(cr,'image_sources',side_effect=snapshots),patch.object(cr,'body_snapshot',return_value={'html':'<img src=body>','text_length':0}),patch.object(cr,'advance_scroll',return_value={'bottom':True,'height':'1000'}):
 result,_,_=cr.wait_chapter(page,'Chapter One',None,{'chapter_wait_seconds':20})
 assert polls[0]>=11 and clock[0]>=2.5 and result[0]['ready']
cr.check_resolution((550,739),{'chapter_min_short_edge':1000,'chapter_min_long_edge':1400})
cr.check_resolution((1200,1600),{})
root=Path('tmp/pdfs/chapter_test');root.mkdir(parents=True,exist_ok=True)
paths=[]
for n in range(2):
 p=root/f'{n}.jpg';Image.new('RGB',(1200,1600),('white','lightblue')[n]).save(p);paths.append(p)
c.build_pdf(paths,root/'chapter.pdf',outline=[('第一章',1),('第二章',2)])
r=PdfReader(root/'chapter.pdf');assert len(r.pages)==2 and len(r.outline)==2
# JPEG bytes must be embedded directly, not recompressed or captured with chrome.
for page,p in zip(r.pages,paths):
 obj=next(iter(page['/Resources']['/XObject'].values())).get_object()
 assert obj.get_data()==p.read_bytes()
print('PASS: slow image wait, low-resolution acceptance, chapter bookmarks, original JPEG bytes preserved')

# A body image absent from the captured set must block chapter completion.
clock[0]=0
page=Page()
with patch.object(cr.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cr,'visible_next'),patch.object(cr,'modal_body',return_value=Modal()),patch.object(cr,'image_sources',return_value=[{'src':'body','width':550,'height':739,'ready':True}]),patch.object(cr,'body_snapshot',return_value={'html':'body','text_length':0,'image_urls':['body','missing']}),patch.object(cr,'advance_scroll',return_value={'bottom':True,'height':'1000'}):
    try:cr.wait_chapter(page,'Chapter One',None,{'chapter_wait_seconds':10})
    except RuntimeError:pass
    else:raise AssertionError('missing image was accepted')
truncated=root/'truncated.jpg';data=paths[0].read_bytes();truncated.write_bytes(data[:len(data)//2]);assert not c.verified(truncated)
print('PASS: missing body image blocks completion; truncated JPEG rejected')
