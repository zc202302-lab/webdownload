import sys,types,os,subprocess
from pathlib import Path
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright');sys.modules['playwright.sync_api']=types.SimpleNamespace(Error=Exception,sync_playwright=None)
import chapter_reader as cr
class Capture:
 def evaluate(self,source):return source
js=cr.advance_scroll(Capture())
fixture='''global.getComputedStyle=()=>({overflowY:'auto'});const e={clientHeight:400,scrollHeight:2400,scrollTop:0,querySelectorAll:()=>[]};let passes=0;let state;do{state=FUNC(e);passes++;if(passes>20)throw Error('scroll failed')}while(!state.bottom);if(passes<5 || e.scrollTop+e.clientHeight<e.scrollHeight)throw Error('content truncated');console.log('PASS: scrolling reaches full body bottom');'''.replace('FUNC','('+js+')')
subprocess.run([os.environ.get('CODEX_PRIMARY_RUNTIME_NODE','node'),'-e',fixture],check=True)
# Merge preserves full page sequence and sets correct chapter destinations.
from pypdf import PdfReader
root=Path('tmp/pdfs/chapter_test')
count=cr.merge_chapters([root/'chapter.pdf',root/'chapter.pdf'],root/'merged.pdf',['Chapter 1','Chapter 2'])
r=PdfReader(root/'merged.pdf');assert count==4 and len(r.outline)==2
assert r.get_destination_page_number(r.outline[1])==2
print('PASS: merged chapter PDF pages and outline destinations')
