"""仅新增复制；目标存在时校验，不覆盖、不移动、不删除。"""
from pathlib import Path
import csv
import hashlib
import json
import shutil
import argparse

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('F:/新安江水文模型')
RECORDS = ROOT/'文档/核查记录'
plan = []

def add(src, dst, purpose):
    plan.append({'来源':str(src), '目标':str(dst.relative_to(ROOT)), '字节数':src.stat().st_size, '用途':purpose})

def folder(src, dst, purpose):
    for p in sorted(src.rglob('*')):
        if p.is_file():
            add(p,dst/p.relative_to(src),purpose)

base=SOURCE/'selinco_data_package_20260721/selinco_data_package_20260721'
for old,new in [('full_basin_shp','流域总边界'),('subbasin_shp','入湖子流域'),('lake_shp','湖泊边界'),('dem_1km','地形高程'),('stations','站点位置')]:
    folder(base/old,ROOT/'数据/原始资料'/new,'原包资料；需质量审查后标准化')
glacier=base/'glacier'
stems={'glacier_in_basin','阿里分类','波曲分类','扎根分类','扎加分类','阿里分类冰','扎根分类冰','扎加分类冰'}
for p in sorted(glacier.iterdir()):
    if p.is_file() and p.name.split('.')[0] in stems:
        add(p,ROOT/'数据/原始资料/冰川与分类'/p.name,'保留冰川边界及分类栅格；类别语义与年代待核实')
streams=SOURCE/'selinco_stream_networks_20260721/selinco_stream_networks_20260721'
for old,new in [('ali','阿里藏布'),('boqu','波曲藏布'),('zagen','扎根藏布'),('zajia','扎加藏布')]:
    for suffix in ['dem_filled.tif','d8_pointer.tif','flow_accum_cells.tif']:
        add(streams/old/f'{old}_{suffix}',ROOT/'数据/外部衍生成果/河网地形'/new/f'{old}_{suffix}','旧项目地形水文处理结果；算法配置待验证')
for p in sorted((streams/'merged').iterdir()):
    if p.suffix.lower()!='.gpkg':
        add(p,ROOT/'数据/外部衍生成果/河网地形/合并河网'/p.name,'50与150格阈值河网；保留两种尺度用于敏感性分析')
add(streams/'summary.json',ROOT/'数据/外部衍生成果/河网地形/河网摘要.json','原摘要；Web Mercator长度不能直接作精确长度')
app=SOURCE/'frontend-demo/app'
for old,new in [('scripts/build_selinco_assets.py','色林错资源构建参考.py'),('src/data/selinco.js','色林错页面数据参考.js'),('src/services/taskService.js','任务服务参考.js'),('src/services/adapters/httpAdapter.js','未接入接口参考.js'),('package.json','原前端依赖.json')]:
    add(app/old,ROOT/'参考资料/旧项目参考'/new,'只读设计参考；禁止直接执行或作为科学计算模块导入')
add(app/'public/data/selinco/forcing/metadata.json',ROOT/'参考资料/旧项目参考/合成降水元数据.json','仅作证据；对应NetCDF为合成演示降水，未复制')

def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

parser=argparse.ArgumentParser()
parser.add_argument('--执行',action='store_true')
args=parser.parse_args()
if not args.执行:
    with (RECORDS/'复制计划.csv').open('x',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(plan[0]))
        w.writeheader(); w.writerows(plan)
    print(json.dumps({'文件数':len(plan),'字节数':sum(r['字节数'] for r in plan),'分组':sorted(set(str(Path(r['目标']).parent) for r in plan))},ensure_ascii=False))
else:
    manifest=RECORDS/'复制校验清单.csv'
    if manifest.exists():
        raise FileExistsError('校验清单已存在；请核查现有结果，不覆盖。')
    with (RECORDS/'复制计划.csv').open(encoding='utf-8-sig',newline='') as f:
        expected=list(csv.DictReader(f))
    if [(r['来源'],r['目标'],str(r['字节数'])) for r in plan] != [(r['来源'],r['目标'],r['字节数']) for r in expected]:
        raise ValueError('来源清单已变化，请重新核查。')
    with manifest.open('x',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=[*plan[0],'来源SHA256','目标SHA256','校验'])
        w.writeheader()
        for r in plan:
            src,dst=Path(r['来源']),ROOT/r['目标']
            if not dst.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError('目标越界')
            before=digest(src)
            dst.parent.mkdir(parents=True,exist_ok=True)
            if not dst.exists():
                with src.open('rb') as reader,dst.open('xb') as writer:
                    shutil.copyfileobj(reader,writer)
            after=digest(dst)
            ok=before==after==digest(src)
            w.writerow({**r,'来源SHA256':before,'目标SHA256':after,'校验':'一致' if ok else '失败'})
            f.flush()
            if not ok:
                raise ValueError(f'校验失败: {dst}')
    print(json.dumps({'复制校验通过':len(plan),'总字节数':sum(r['字节数'] for r in plan)},ensure_ascii=False))
