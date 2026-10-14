"""Restart only current magazine once; log final errors including unknown images."""
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright')
sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=type('PWError',(Exception,),{}))
import collector as c
import chapter_reader as cr
from library_manager import Library,save_json,StorageFull
from missing_records import MissingRecords,record_failure,write_missing_report

with tempfile.TemporaryDirectory() as folder:
    lib=Library(Path(folder)/'output',1)
    report=lib.root/'缺失记录.txt'
    task={'id':'retry-success','title':'重试成功杂志','year':2017,'source':'https://example.com/book'}
    lib.register(task)
    write_missing_report(lib)
    calls=[]
    def attempt(context,item,library,settings):
        calls.append(item['id'])
        output,work=library.paths(item)
        if len(calls)==1:
            work.mkdir(parents=True)
            save_json(work/'chapters.json',{'entries':['One','Two'],'completed_entries':{'0':{'empty':True}}})
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_bytes(b'preserved first attempt PDF')
            library.register(item)['active_operation']={'stage':'等待正文加载与完整性检查','chapter_position':2,'chapter_title':'Two'}
            error=RuntimeError('first error should not reach report')
            record_failure(library,item,error)
            assert 'first error' not in report.read_text(encoding='utf-8-sig')
            raise error
        assert not work.exists() and not output.exists()  # No original cache.
        assert library.register(item).get('missing_images',[])==[]
        assert not library.register(item).get('failure_events')
        assert 'first error' not in report.read_text(encoding='utf-8-sig')
        library.update(item,'已完成')
    with patch.object(c,'download_magazine_once',side_effect=attempt):
        c.run_magazine_with_retry(None,task,lib,{})
    assert calls==[task['id']]*2
    _,work=lib.paths(task)
    archive=list(work.parent.glob(work.name+'_首次失败备份_*'))
    assert len(archive)==1 and (archive[0]/'chapters.json').exists()
    assert (archive[0]/'previous_output.pdf').read_bytes()==b'preserved first attempt PDF'
    assert 'first error' not in report.read_text(encoding='utf-8-sig')
    assert not lib.defer_missing_report

    failed=dict(task,id='retry-fails',title='重试仍失败杂志')
    shared_error=RuntimeError('正文标题不匹配 https://example.com/a?token=secret')
    calls=[]
    def failing(context,item,library,settings):
        calls.append(item['id'])
        library.register(item)['active_operation']={'stage':'等待正文加载与完整性检查','chapter_position':20,'chapter_title':'p63 小吃（二）'}
        record_failure(library,item,shared_error)
        if len(calls)==1:
            assert failed['title'] not in report.read_text(encoding='utf-8-sig')
        raise shared_error
    with patch.object(c,'download_magazine_once',side_effect=failing):
        try: c.run_magazine_with_retry(None,failed,lib,{})
        except RuntimeError as exc: assert '从头重试后仍失败' in str(exc)
        else: raise AssertionError('second failure ignored')
    assert len(calls)==2
    book=lib.db['books'][failed['id']]
    events=list(book['failure_events'].values())
    assert len(events)==1 and events[0]['occurrences']==1
    assert not book.get('missing_images') and book['status']=='未完成已终止'
    text=report.read_text(encoding='utf-8-sig')
    assert '目录第20项：p63 小吃（二）' in text and '图片位置未确定（不计入缺图数）' in text
    assert '正文标题不匹配' in text and 'secret' not in text
    assert '发生次数：1' in text

    # Fatal restrictions and exhausted storage are recorded but never retried.
    for exc in (PermissionError('HTTP403'),StorageFull('disk full')):
        strict=dict(task,id=type(exc).__name__,title=type(exc).__name__)
        with patch.object(c,'download_magazine_once',side_effect=exc) as once:
            try: c.run_magazine_with_retry(None,strict,lib,{})
            except type(exc): pass
            else: raise AssertionError('fatal restriction ignored')
        assert once.call_count==1 and strict['title'] in report.read_text(encoding='utf-8-sig')

    # A successful attempt with <=3 omissions keeps its omission log, not a
    # fictitious termination; allowance remains separate from retry policy.
    partial=dict(task,id='partial',title='允许缺图杂志')
    def partial_success(context,item,library,settings):
        _,work=library.paths(item)
        work.mkdir(parents=True,exist_ok=True)
        progress={'chapters':{}}
        log=MissingRecords(library,item,progress,work/'chapters.json')
        log.add(0,'One',0,{'src':'https://example.com/image.jpg'},'missing original')
        library.update(item,'已完成')
    with patch.object(c,'download_magazine_once',side_effect=partial_success) as once:
        c.run_magazine_with_retry(None,partial,lib,{})
    assert once.call_count==1 and len(lib.db['books']['partial']['missing_images'])==1
    assert '允许缺图杂志' in report.read_text(encoding='utf-8-sig')

    # Actual chapter wrapper captures wait failure, missing image position and
    # close failure stages; each event is distinct from the image omission count.
    class Page:
        url='https://example.com/book'
        def get_by_text(self,*args,**kwargs):
            return types.SimpleNamespace(first=types.SimpleNamespace(click=lambda **kwargs:None))
    waittask=dict(task,id='chapter-error',title='正文失败杂志')
    with patch.object(cr,'catalog_entries',return_value=['One']),patch.object(cr,'wait_chapter',side_effect=RuntimeError('loader pending')):
        try: cr.collect_chapters(None,Page(),waittask,lib,{},c.build_pdf)
        except RuntimeError: pass
    record=list(lib.db['books']['chapter-error']['failure_events'].values())[0]
    assert record['stage']=='等待正文加载与完整性检查' and record['chapter_position']==1
    assert not record.get('image_position')
    closetask=dict(task,id='close-error',title='关闭失败杂志')
    blank={'body_identified':True,'text_length':0,'html':'','image_urls':[]}
    with patch.object(cr,'catalog_entries',return_value=['One']),patch.object(cr,'wait_chapter',return_value=([],'signature',object())),patch.object(cr,'body_snapshot',return_value=blank),patch.object(cr,'close_chapter',side_effect=RuntimeError('close failed')):
        try: cr.collect_chapters(None,Page(),closetask,lib,{},c.build_pdf)
        except RuntimeError: pass
    record=list(lib.db['books']['close-error']['failure_events'].values())[0]
    assert record['stage']=='关闭空白正文弹窗' and not lib.done(closetask)

print('PASS: current-magazine fresh restart once, backup preservation, first-error suppression, second-error detail/dedup, unknown images not counted, successful omission log, strict permission/space and wait/close stages')
