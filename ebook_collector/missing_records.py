"""Local, persistent magazine image omissions; one count per DOM position."""
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
from library_manager import save_json


def safe_source(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https'):
        return '非HTTP图片地址' if url else ''
    return urlunsplit((p.scheme,p.hostname or '',p.path,'',''))


def write_missing_report(library):
    lines=['缺失记录（按杂志及正文图片位置记录；图片链接不包含查询参数）','']
    for book in library.db['books'].values():
        missing=book.get('missing_images',[])
        if not missing: continue
        outcome='超过3张，整批停止' if len(missing)>3 else '不超过3张，按允许缺图规则处理'
        lines.extend([f'杂志：{book["title"]}',f'缺失图片：{len(missing)}张；{outcome}'])
        for r in missing:
            lines.extend([f'目录第{r["chapter_position"]}项：{r["chapter_title"]}；正文第{r["image_position"]}张图片',
                          f'图片：{r["source"] or "未提供有效地址"}',f'原因：{r["reason"]}'])
        lines.append('')
    if len(lines)==2: lines.append('暂无缺失图片。')
    path=Path(library.root)/'缺失记录.txt'
    temporary=path.with_suffix('.txt.tmp')
    temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
    temporary.replace(path)


class MissingRecords:
    def __init__(self, library, task, progress, progress_path):
        self.library,self.task,self.progress,self.progress_path=library,task,progress,progress_path
        self.progress.setdefault('missing_images',{})
        self.sync()

    def sync(self):
        values=list(self.progress['missing_images'].values())
        self.library.register(self.task)['missing_images']=values
        self.library.flush()
        write_missing_report(self.library)

    def add(self, chapter, title, image, item, reason):
        key=f'{chapter}:{image}'
        self.progress['missing_images'][key]={'chapter_position':chapter+1,'chapter_title':title,
                'image_position':image+1,'source':safe_source((item.get('sources') or [item.get('src','')])[0]),
                'reason':re.sub(r'https?://\S+',lambda match:safe_source(match[0]),reason)[:500]}
        save_json(self.progress_path,self.progress)
        self.sync()
        count=len(self.progress['missing_images'])
        if count>3:
            raise RuntimeError(f'本杂志累计{count}张图片下载失败，超过允许的3张，整批停止。')
        print(f'本杂志累计缺失{count}/3张，已写入缺失记录，继续采集。')

    def recovered(self, chapter, image):
        if self.progress['missing_images'].pop(f'{chapter}:{image}',None) is not None:
            save_json(self.progress_path,self.progress)
            self.sync()
