"""Shared body scope, visibility and source selection for capture and export."""
BODY_SELECTOR = '.modal-body,.chapter-content,.article-content,#chapterContent,#articleContent'
HELPERS = r'''
const bodySelector='.modal-body,.chapter-content,.article-content,#chapterContent,#articleContent';
const excluded='script,style,nav,button,.modal-header,.modal-footer,.chapter-nav,.chapter-navigation,.close,.icon,.fa,[aria-hidden="true"]';
const shown=e=>{
  for(let x=e;x && x.nodeType===1;x=x.parentElement) {
    const s=getComputedStyle(x);
    if(s.display==='none' || s.visibility==='hidden' || s.visibility==='collapse' || x.hidden) return false;
  }
  return true;
};
const includedImage=e=>{
  if(e.closest?.(excluded)) return false;
  const container=e.closest?.('.modal-content,[role="dialog"],.modal');
  const body=container?.querySelector(bodySelector);
  if(body && !body.contains(e)) return false;
  if(e.closest?.(bodySelector)) return shown(e);
  const r=e.getBoundingClientRect();
  return (r.width>40 && (r.height>40 || !e.naturalHeight)) || (r.width>0 && e.naturalWidth>40 && e.naturalHeight>40);
};
const imageIdentity=e=>e.currentSrc || e.src ||
  new URL(e.getAttribute('data-src') || e.getAttribute('data-original') || '',e.baseURI).href;
'''
