"""Whole-magazine omission budget, partial bodies and actionable diagnostics."""
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
from PIL import Image

sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright')
sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=type('PWError',(Exception,),{}))
import chapter_reader as cr
import collector as collector
from chapter_dom import HELPERS
from library_manager import Library,StorageFull,validate_pdf
from missing_records import MissingRecords

# Real shared browser code must exclude headers/hidden content while retaining
# small legitimate body images and using responsive currentSrc consistently.
script='''
const assert=require('assert');
global.getComputedStyle=e=>({display:e.hide?'none':'block',visibility:'visible'});
const body={nodeType:1,parentElement:null};
const container={querySelector:()=>body};
body.contains=e=>e.inBody;
const mk=(inBody,hide=false,control=false)=>({nodeType:1,parentElement:body,inBody,hide,control,
  src:'https://example.com/fallback.jpg',currentSrc:'https://example.com/responsive.jpg',
  closest(s){if(s===excluded)return this.control?this:null;if(s===bodySelector)return this.inBody?body:null;return container;},
  getBoundingClientRect:()=>({width:10,height:10})});
HELPERS
assert(includedImage(mk(true)));
assert(!includedImage(mk(false)));
assert(!includedImage(mk(true,true)));
assert(!includedImage(mk(true,false,true)));
assert.equal(imageIdentity(mk(true)),'https://example.com/responsive.jpg');
console.log('PASS JS: shared body scope, hidden/control exclusion, small body images and currentSrc');
'''.replace('HELPERS',HELPERS)
subprocess.run(['node','-e',script],check=True)


class Page:
    url='https://example.com/book'
    def get_by_text(self,*args,**kwargs):
        return types.SimpleNamespace(first=types.SimpleNamespace(click=lambda **kwargs:None),count=lambda:0)
    def wait_for_timeout(self,ms):
        clock[0]+=ms/1000


class Modal:
    def inner_text(self):
        return 'Chapter'


clock=[0]
pending={'src':'https://example.com/1.jpg','dom_src':'https://example.com/1.jpg','sources':[],
         'width':0,'height':0,'ready':False}
snapshot={'body_identified':True,'text_length':0,'html':'<img src="https://example.com/1.jpg">',
          'image_urls':[pending['src']]}
with tempfile.TemporaryDirectory() as folder:
    diagnostic=Path(folder)/'diagnostic.json'
    for blocked in (False,True):
        clock[0]=0
        with patch.object(cr.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cr,'visible_next'),patch.object(cr,'modal_body',return_value=Modal()),patch.object(cr,'body_snapshot',return_value=dict(snapshot,pending=blocked)),patch.object(cr,'image_sources',return_value=[pending]),patch.object(cr,'advance_scroll',return_value={'bottom':True,'height':'1000'}):
            try:
                result=cr.wait_chapter(Page(),'Chapter',None,{'_allow_missing_images':True,'_diagnostic_path':diagnostic})
            except RuntimeError:
                assert blocked
            else:
                assert not blocked and result[0]==[pending]
        assert clock[0]>=60 and diagnostic.exists()
        detail=json.loads(diagnostic.read_text())
        assert '图片未加载或解码失败' in detail['reasons']
        if blocked: assert '正文仍有加载或错误提示' in detail['reasons']

    root=Path(folder)
    lib=Library(root/'output',1)
    state={'title':None}
    images=[dict(pending,src=f'https://example.com/{n}.jpg?token=secret',dom_src=f'https://example.com/{n}.jpg?token=secret') for n in range(5)]
    def waiting(page,title,*args):
        state['title']=title
        if title=='First': selected=images[:2]
        elif title=='Second': selected=images[2:4]
        else: selected=images[4:]
        return selected,'signature',Modal()
    def body(*args):
        selected=images[:2] if state['title']=='First' else images[2:4] if state['title']=='Second' else images[4:]
        return {'text_length':0,'html':''.join('<img src="'+i['src']+'">' for i in selected),'image_urls':[i['src'] for i in selected]}
    def download(context,item,referer,path,library):
        if item['src']!=images[3]['src']:
            raise RuntimeError('failed image')
        Image.new('RGB',(100,150),'white').save(path,format='JPEG')
        return (100,150),item['src']
    settings={'keep_page_images':True}
    task={'id':'budget3','year':2017,'title':'《测试》2017年第3期','source':Page.url}
    with patch.object(cr,'catalog_entries',return_value=['First','Second']),patch.object(cr,'wait_chapter',side_effect=waiting),patch.object(cr,'body_snapshot',side_effect=body),patch.object(cr,'download_best',side_effect=download),patch.object(cr,'close_chapter') as close:
        cr.collect_chapters(None,Page(),task,lib,settings,collector.build_pdf)
    pdf,work=lib.paths(task)
    assert close.call_count==2 and lib.done(task) and validate_pdf(pdf)==1
    assert len(lib.db['books'][task['id']]['missing_images'])==3
    text=(lib.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    assert task['title'] in text and '目录第2项：Second；正文第1张图片' in text and 'secret' not in text
    record=json.loads((work/'chapters.json').read_text())
    with patch.object(cr,'catalog_entries',return_value=['First','Second']),patch.object(cr,'wait_chapter',side_effect=AssertionError('completed entries reopened')):
        cr.collect_chapters(None,Page(),task,lib,settings,collector.build_pdf)
    assert len(json.loads((work/'chapters.json').read_text())['missing_images'])==3
    task4=dict(task,id='budget4',title='超过阈值杂志')
    with patch.object(cr,'catalog_entries',return_value=['First','Second','Third']),patch.object(cr,'wait_chapter',side_effect=waiting),patch.object(cr,'body_snapshot',side_effect=body),patch.object(cr,'download_best',side_effect=download),patch.object(cr,'close_chapter'):
        try: cr.collect_chapters(None,Page(),task4,lib,settings,collector.build_pdf)
        except RuntimeError as exc: assert '累计4张' in str(exc)
        else: raise AssertionError('four failures accepted')
    assert not lib.done(task4) and len(lib.db['books']['budget4']['missing_images'])==4
    assert '超过3张，整批停止' in (lib.root/'缺失记录.txt').read_text(encoding='utf-8-sig')

    # Budget counts DOM positions, not repeated failures/restarts; recovery
    # removes one precise omission while retaining other magazines' records.
    _,work4=lib.paths(task4)
    path4=work4/'chapters.json'
    progress=json.loads(path4.read_text())
    log=MissingRecords(lib,task4,progress,path4)
    log.recovered(2,0)
    assert len(progress['missing_images'])==3
    log.add(0,'First',0,images[0],'same failure retried')
    assert len(progress['missing_images'])==3
    assert len(lib.db['books']['budget3']['missing_images'])==3

    # Image-only missing content is recorded without manufacturing empty PDFs.
    emptytask=dict(task,id='onlymissing',title='全缺图杂志')
    with patch.object(cr,'catalog_entries',return_value=['First']),patch.object(cr,'wait_chapter',side_effect=waiting),patch.object(cr,'body_snapshot',side_effect=body),patch.object(cr,'download_best',side_effect=RuntimeError('fail')),patch.object(cr,'close_chapter'):
        cr.collect_chapters(None,Page(),emptytask,lib,settings,collector.build_pdf)
    assert lib.done(emptytask) and not lib.paths(emptytask)[0].exists()

    # Captions/text survive removal of the failed image in mixed export.
    mixedtask=dict(task,id='mixedmissing',title='图文杂志')
    def print_text(context,snapshot,mapping,path):
        assert '保留文字' in snapshot['html'] and '<img' not in snapshot['html'] and not mapping
        from reportlab.pdfgen import canvas
        pdf=canvas.Canvas(str(path));pdf.drawString(20,20,'text retained');pdf.save()
    with patch.object(cr,'catalog_entries',return_value=['Chapter']),patch.object(cr,'wait_chapter',return_value=([images[0]],'signature',Modal())),patch.object(cr,'body_snapshot',return_value={'text_length':4,'html':'<p>保留文字</p><img src="'+images[0]['src']+'">'}),patch.object(cr,'download_best',side_effect=RuntimeError('fail')),patch.object(cr,'print_body',side_effect=print_text),patch.object(cr,'close_chapter'):
        cr.collect_chapters(None,Page(),mixedtask,lib,settings,collector.build_pdf)
    assert lib.done(mixedtask) and validate_pdf(lib.paths(mixedtask)[0])==1

    for failure in (StorageFull('space'),PermissionError('locked')):
        strict=dict(task,id=type(failure).__name__)
        with patch.object(cr,'catalog_entries',return_value=['First']),patch.object(cr,'wait_chapter',side_effect=waiting),patch.object(cr,'body_snapshot',side_effect=body),patch.object(cr,'download_best',side_effect=failure),patch.object(cr,'close_chapter'):
            try: cr.collect_chapters(None,Page(),strict,lib,settings,collector.build_pdf)
            except type(failure): pass
            else: raise AssertionError('non-image failure tolerated')
        assert not lib.db['books'][strict['id']]['missing_images']

    cachetask=dict(task,id='old-cache',title='旧缓存来源核对')
    _,cachework=lib.paths(cachetask)
    cachework.mkdir(parents=True)
    cached=cachework/'chapter_0001_001.jpg'
    Image.new('RGB',(80,100),'red').save(cached)
    from library_manager import file_hash,save_json
    save_json(cachework/'chapters.json',{'entries':['Chapter'],'chapters':{'0:0':{
        'source':'https://example.com/old-other-image.jpg','sha256':file_hash(cached),'size':[80,100]}}})
    with patch.object(cr,'catalog_entries',return_value=['Chapter']),patch.object(cr,'wait_chapter',return_value=([images[3]],'signature',Modal())),patch.object(cr,'body_snapshot',return_value={'text_length':0,'html':'<img>'}),patch.object(cr,'download_best',side_effect=download) as fresh,patch.object(cr,'close_chapter'):
        cr.collect_chapters(None,Page(),cachetask,lib,settings,collector.build_pdf)
    assert fresh.call_count==1
    assert Image.open(cached).size==(100,150)

print('PASS: 60-second partial-image wait, diagnostics, whole-magazine 3/4 threshold, cross-chapter count, persistent dedup/recovery, missing-only records, mixed text retention, permission and storage remain strict')
