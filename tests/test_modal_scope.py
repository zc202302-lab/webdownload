"""Duplicate titles cannot select a paragraph; scoped fallback and outer scroll."""
import subprocess
import sys
import types
from pathlib import Path
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright')
sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=Exception)
import chapter_reader as cr
from chapter_dom import HELPERS

script=r'''
const assert=require('assert');
global.getComputedStyle=e=>({position:e.fixed?'fixed':'static',display:e.hidden?'none':'block',visibility:'visible',overflowY:'auto'});
const title='最受瞩目青年厨师沈巍';
const header={textContent:title,closest:()=>null};
const repeated={textContent:title,closest:()=>({})};
const paragraph={tagName:'DIV',innerText:title,fixed:false,getBoundingClientRect:()=>({width:600,height:500}),matches:()=>false,querySelectorAll:()=>[repeated]};
const popup={tagName:'DIV',innerText:title,fixed:false,className:'modal-content',getBoundingClientRect:()=>({width:800,height:600}),matches:s=>s.includes('.modal-content'),querySelectorAll:()=>[header,repeated]};
const probe=PROBE;
assert.equal(probe(paragraph,title),null);
assert(probe(popup,title).score>=100);
assert.equal(probe({...popup,tagName:'BODY'},title),null);
HELPERS
const text={nodeType:1,parentElement:null,closest:()=>null};
const fallback={...popup,querySelector:()=>null,querySelectorAll:()=>[text]};
assert(resolveBody(fallback).identified);
assert.equal(resolveBody({...fallback,matches:()=>false}).identified,false);
const explicit={nodeType:1,parentElement:null};
assert.equal(resolveBody({...fallback,querySelector:()=>explicit}).body,explicit);
const empty={...fallback,querySelectorAll:()=>[]};
assert(!resolveBody(empty).identified);
const outer={tagName:'DIV',clientHeight:400,scrollHeight:2000,scrollTop:0,parentElement:{tagName:'BODY'},matches:()=>true};
const inner={parentElement:outer,clientHeight:300,scrollHeight:300,scrollTop:0,querySelectorAll:()=>[]};
let state,count=0;do{state=SCROLL(inner);count++;assert(count<20);}while(!state.bottom);
assert(count>1 && outer.scrollTop+outer.clientHeight>=outer.scrollHeight);
console.log('PASS JS: repeated body title rejected, modal-header selection, unknown-class scoped body, no global/empty fallback, outer scroll reaches bottom');
'''.replace('PROBE','('+cr.MODAL_PROBE+')').replace('HELPERS',HELPERS)
class Capture:
    def evaluate(self,source): return source
script=script.replace('SCROLL','('+cr.advance_scroll(Capture())+')')
subprocess.run(['node','-e',script],check=True)
