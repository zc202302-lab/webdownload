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
    def evaluate_all(self,script):assert script==books.ARROW_PROBE;return self.rows
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
