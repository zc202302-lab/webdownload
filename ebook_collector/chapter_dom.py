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
const dialogSelector='.modal-content,[role="dialog"],.modal,.chapter-modal,.content-modal';
const resolveBody=e=>{
  if(e.tagName==='BODY' || e.tagName==='HTML') return {body:e,identified:false,method:'拒绝全页采集'};
  const explicit=e.matches(bodySelector)?e:e.querySelector(bodySelector);
  if(explicit) return {body:explicit,identified:true,method:'正文选择器'};
  const dialog=e.tagName!=='BODY' && e.tagName!=='HTML' && (e.matches(dialogSelector) || getComputedStyle(e).position==='fixed');
  const content=[...e.querySelectorAll('p,table,img,article')].some(x=>shown(x) && !x.closest(excluded));
  return {body:e,identified:dialog && content,method:dialog && content?'已确认弹窗内正文备用识别':'未识别'};
};
const includedImage=e=>{
  if(e.closest?.(excluded)) return false;
  let container=e.closest?.(dialogSelector);
  if(!container) {
    for(let p=e.parentElement;p && p.tagName!=='BODY' && p.tagName!=='HTML';p=p.parentElement) {
      if(getComputedStyle(p).position==='fixed') { container=p;break; }
    }
  }
  const body=container?.querySelector(bodySelector);
  if(body && !body.contains(e)) return false;
  if(e.closest?.(bodySelector)) return shown(e);
  if(container && resolveBody(container).identified) return shown(e);
  const r=e.getBoundingClientRect();
  return (r.width>40 && (r.height>40 || !e.naturalHeight)) || (r.width>0 && e.naturalWidth>40 && e.naturalHeight>40);
};
const imageIdentity=e=>e.currentSrc || e.src ||
  new URL(e.getAttribute('data-src') || e.getAttribute('data-original') || '',e.baseURI).href;
'''
