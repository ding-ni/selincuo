from pathlib import Path
import csv
import hashlib
import json
import re

root=Path(__file__).resolve().parents[1]
errors=[]
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()

with (root/'文档/核查记录/复制校验清单.csv').open(encoding='utf-8-sig',newline='') as f:
    rows=list(csv.DictReader(f))
for r in rows:
    src,dst=Path(r['来源']),root/r['目标']
    if not src.is_file() or not dst.is_file():
        errors.append(f'文件缺失: {r["目标"]}')
        continue
    if sha(src)!=r['来源SHA256'] or sha(dst)!=r['目标SHA256'] or r['来源SHA256']!=r['目标SHA256']:
        errors.append(f'哈希异常: {r["目标"]}')
    if dst.suffix.lower()=='.shp':
        for ext in ['.shx','.dbf','.prj']:
            if not dst.with_suffix(ext).exists():
                errors.append(f'SHP配套缺失: {dst.name} {ext}')

links=0
for p in root.rglob('*.md'):
    for target in re.findall(r'\]\(([^)]+)\)',p.read_text(encoding='utf-8')):
        if '://' in target or target.startswith('#'):
            continue
        links+=1
        if not (p.parent/target.split('#')[0]).exists():
            errors.append(f'链接缺失: {p.name}: {target}')
required=['前端','后端','算法/遥感反演','算法/寒区水文','算法/水动力耦合','算法/风险预警','数据/原始资料','数据/外部衍生成果','数据/标准数据','数据/数据目录','文档/核查记录','工具','测试','部署','成果/模型运行','成果/科研交付','参考资料/旧项目参考']
for d in required:
    if not (root/d).is_dir():
        errors.append(f'目录缺失: {d}')
result={'复制文件数':len(rows),'复制总字节':sum(int(r['字节数']) for r in rows),'源文件与目标二次哈希校验':'通过' if not errors else '检查错误列表','文档本地链接数':links,'必要目录数':len(required),'原任务书SHA256':sha(root/'任务书最终盖章签字版本.pdf'),'错误':errors}
with (root/'文档/核查记录/交付核验.json').open('x',encoding='utf-8') as f:
    json.dump(result,f,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=False))
raise SystemExit(1 if errors else 0)
