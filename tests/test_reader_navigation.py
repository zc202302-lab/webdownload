"""Execute the actual browser arrow probe against synthetic toolbar DOM."""
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('ebook_collector').resolve()))
try:
    import playwright.sync_api
except ModuleNotFoundError:
    sys.modules['playwright']=types.ModuleType('playwright')
    sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=Exception)
import book_collector as books

script=r'''
const probe=PROBE;
const assert=require('assert');
function node(attrs={},top=500,parent=null){
  const n={id:attrs.id||'',tagName:attrs.tag||'IMG',disabled:false,parent,textContent:attrs.text||'',
    getAttribute:k=>attrs[k]||null,
    getBoundingClientRect:()=>({top,height:24,width:24}),
    contains:other=>{for(let p=other.parent;p;p=p.parent)if(p===n)return true;return false;},
    closest:()=>{for(let p=n;p;p=p.parent)if(p.tagName==='BUTTON'||p.tagName==='A'||p.onclick)return p;return null;},
    style:{display:'block',visibility:'visible',backgroundImage:attrs.background||'none'},
    onclick:attrs.onclick};return n;
}
global.getComputedStyle=e=>e.style;
const field=node({tag:'INPUT'});
global.document={querySelector:()=>field};
const prev=node({src:'https://example.test/images/previous.png'});
const last=node({src:'https://example.test/images/last.png'});
const next=node({src:'https://example.test/images/next.png?cache=1'});
assert.deepStrictEqual(probe([prev,field,next,last]).map(x=>x.index),[2]);
const button=node({tag:'BUTTON',id:'customControl'});
const icon=node({src:'/icons/nextPage.svg'},500,button);
assert.deepStrictEqual(probe([button,icon,field]).map(x=>x.index),[0]);
const wrapper=node({tag:'DIV',class:'next-page'});
const inner=node({src:'/images/next.png'},500,wrapper);
assert.deepStrictEqual(probe([wrapper,inner,field]).map(x=>x.index),[1]);
const bg=node({tag:'DIV',background:'url("https://example.test/img/right.png")'});
assert.deepStrictEqual(probe([field,bg]).map(x=>x.index),[1]);
assert.strictEqual(probe([field,next,node({src:'/img/next.png'})]).length,2);
assert.strictEqual(probe([field,node({src:'/img/next.png'},150)]).length,0);
assert.strictEqual(probe([field,node({class:'next-page last-page'})]).length,0);
// User's actual control: generic DIV + embedded PNG + noninteractive text SPAN.
const labeled=node({tag:'DIV',class:'button',text:'下一页'});
const generic1=node({tag:'DIV',class:'button right',text:'末页'});
const generic2=node({tag:'DIV',class:'button right',text:'前进'});
assert.deepStrictEqual(probe([field,generic1,labeled,generic2]).map(x=>x.index),[2]);
const arrowImage=node({src:'data:image/png;base64,example'},500,labeled);
assert.deepStrictEqual(probe([field,labeled,arrowImage,generic1,generic2]).map(x=>x.index),[1]);
next.disabled=true;assert.strictEqual(probe([field,next]).length,0);
console.log('PASS: actual arrow JS handles image/background icons, nested controls, rejects ambiguity, last-page, disabled and content images');
'''.replace('PROBE',books.ARROW_PROBE)
subprocess.run(['node','-e',script],check=True)

class Empty:
    def count(self):return 0
class Nodes:
    def __init__(self,rows):self.rows=rows;self.clicked=[]
    def count(self):return 3
    def evaluate_all(self,script):
        if script==books.SIDE_ARROW_PROBE:return getattr(self,'side',[])
        assert script==books.ARROW_PROBE;return self.rows
    def nth(self,index):
        return types.SimpleNamespace(click=lambda **kwargs:self.clicked.append(index))
class Frame:
    def __init__(self,rows):self.nodes=Nodes(rows)
    def locator(self,selector):return self.nodes if selector==books.ARROW_NODES else Empty()
    def get_by_role(self,*args,**kwargs):return Empty()
frame=Frame([{'index':2}]);books.next_arrow(frame);assert frame.nodes.clicked==[2]
for rows in ([],[{'index':1},{'index':2}]):
    frame=Frame(rows)
    try:books.next_arrow(frame)
    except RuntimeError:pass
    else:raise AssertionError('ambiguous/missing arrow must stop')
    assert not frame.nodes.clicked
print('PASS: fallback clicks only unique candidate, never guesses ambiguous arrows')

frame=Frame([]);frame.nodes.side=[{'index':1}];books.next_arrow(frame);assert frame.nodes.clicked==[1]
frame=Frame([]);frame.nodes.side=[{'index':0},{'index':2}]
try:books.next_arrow(frame)
except RuntimeError:pass
else:raise AssertionError('ambiguous side arrows accepted')
assert not frame.nodes.clicked

side_script=r"""
const probe=PROBE, assert=require('assert');
function node(attrs={},r={left:1020,right:1060,top:300,bottom:350,width:40,height:50},parent=null){
 const n={id:attrs.id||'',src:attrs.src||'',currentSrc:attrs.src||'',textContent:attrs.text||'',parent,
 getAttribute:k=>attrs[k]||null,getBoundingClientRect:()=>r,
 contains:o=>{for(let p=o.parent;p;p=p.parent)if(p===n)return true;return false;},
 closest:()=>{for(let p=n;p;p=p.parent)if(p.button)return p;return null;},
 style:{display:'block',visibility:'visible',backgroundImage:'none'},button:attrs.button};return n;
}
global.getComputedStyle=e=>e.style;
let images=[node({src:'https://example.test/pages/2.jpg'},
 {left:200,right:1000,top:50,bottom:700,width:800,height:650})];
global.document={querySelectorAll:()=>images,documentElement:{clientWidth:1200}};
const right=node({class:'flip-button right'}),left=node({class:'flip-button left'});
assert.deepStrictEqual(probe([left,right]).map(x=>x.index),[1]);
const bookIcon=node({class:'arrow-right'},{left:800,right:840,top:300,bottom:350,width:40,height:50});
assert.strictEqual(probe([bookIcon]).length,0);
const last=node({class:'right last-page'});assert.strictEqual(probe([last]).length,0);
right.disabled=true;assert.strictEqual(probe([right]).length,0);right.disabled=false;
assert.strictEqual(probe([right,node({class:'chevron-right'})]).length,2);
const parent=node({class:'right',button:true});const icon=node({class:'chevron-right'},undefined,parent);
assert.deepStrictEqual(probe([parent,icon]).map(x=>x.index),[0]);
const hidden=node({class:'right'});hidden.style.display='none';assert.strictEqual(probe([hidden]).length,0);
images=[];assert.strictEqual(probe([right]).length,0);
console.log('PASS: side arrow actual JS, right-side book bounds, nested icons, disabled/hidden/left/last/content exclusion and ambiguity');
""".replace('PROBE',books.SIDE_ARROW_PROBE)
subprocess.run(['node','-e',side_script],check=True)
# Hidden page-number field is accepted only alongside visible numerical book images.
from collector import reader_field_ready
field=types.SimpleNamespace(count=lambda:1,is_visible=lambda:False)
for visible in [True,False]:
 frame=types.SimpleNamespace(locator=lambda selector:types.SimpleNamespace(evaluate_all=lambda script:visible))
 assert reader_field_ready(frame,field)==visible

left_script=side_script.replace('const probe='+books.SIDE_ARROW_PROBE, 'const probe='+books.PREVIOUS_SIDE_PROBE)
# Execute a left-specific fixture separately from the right-arrow expectations.
left_script=left_script[:left_script.index('const right=node')]+"const left=node({class:'flip-button left'},{left:100,right:140,top:300,bottom:350,width:40,height:50});assert.deepStrictEqual(probe([left]).map(x=>x.index),[0]);"
subprocess.run(['node','-e',left_script],check=True)
# Hidden-toolbar resume moves through validated spreads both ways.
from unittest.mock import MagicMock
for initial,target,backwards in [('5-6/8',2,True),('1-2/8',7,False)]:
 frame=MagicMock(); field=frame.locator.return_value
 field.is_visible.return_value=False
 field.input_value.side_effect=[initial, f'{target}/8']
 field.evaluate_all.return_value=[{'index':0}]
 with patch.object(books,'next_arrow') as advance,patch.object(books,'wait_pages',return_value=([target],{})) as wait:
  books.seek_page(frame,target,8,{'chapter_wait_seconds':60},Path('.'))
  assert wait.call_count==1 and advance.call_count==(0 if backwards else 1)
 field.fill.assert_not_called()
print('PASS: hidden toolbar resume uses verified left/right navigation')

previous_script=side_script[:side_script.index('const right=node')]
previous_script=previous_script.replace('const probe='+books.SIDE_ARROW_PROBE,'const probe='+books.PREVIOUS_TOOLBAR_PROBE)
previous_script+="const field=node({id:'currentPageIndexTextField'});document.querySelector=()=>field;const prev=node({class:'button left',text:'上一页'});assert.deepStrictEqual(probe([field,prev]).map(x=>x.index),[1]);"
subprocess.run(['node','-e',previous_script],check=True)
