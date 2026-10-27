from pathlib import Path
import json
import fiona
import rasterio
import pandas as pd
from shapely.geometry import shape
from shapely.ops import unary_union, transform
from pyproj import Transformer
import fitz
from PIL import Image

root = Path(__file__).resolve().parents[1]
base = Path('F:/新安江水文模型')
packages = [base/'selinco_data_package_20260721/selinco_data_package_20260721', base/'selinco_stream_networks_20260721/selinco_stream_networks_20260721']
records = []
for folder in packages:
    for p in sorted(folder.rglob('*')):
        if p.suffix.lower() not in {'.shp', '.tif', '.xls'}:
            continue
        rec = {'文件': str(p.relative_to(base))}
        try:
            if p.suffix.lower() == '.shp':
                with fiona.open(p) as src:
                    features = list(src)
                    rec.update(类型='矢量', 数量=len(src), 坐标系=str(src.crs), 范围=list(src.bounds), 字段=dict(src.schema['properties']), 无效几何数=sum(not shape(f['geometry']).is_valid for f in features if f['geometry']), 空几何数=sum(f['geometry'] is None for f in features))
                    rec['必要配套完整'] = all(p.with_suffix(ext).exists() for ext in ['.shx', '.dbf', '.prj'])
            elif p.suffix.lower() == '.tif':
                with rasterio.open(p) as src:
                    rec.update(类型='栅格', 坐标系=str(src.crs), 范围=list(src.bounds), 分辨率=list(src.res), 宽=src.width, 高=src.height, 波段=src.count, 无数据值=src.nodata)
            else:
                book = pd.ExcelFile(p)
                rec.update(类型='工作簿', 工作表={s:{'行数':len(pd.read_excel(book,s)), '列':list(map(str,pd.read_excel(book,s).columns))} for s in book.sheet_names})
        except Exception as exc:
            rec['错误'] = str(exc)
        records.append(rec)
with (root/'文档/核查记录/空间资料元数据.json').open('x', encoding='utf-8') as f:
    json.dump(records,f,ensure_ascii=False,indent=2)
print(json.dumps({'核查数量':len(records),'错误':[r for r in records if '错误' in r], '矢量摘要':[r for r in records if r.get('类型')=='矢量' and any(s in r['文件'] for s in ['full_basin','lake_shp','subbasin'])], '站点表':[r for r in records if r.get('类型')=='工作簿']},ensure_ascii=False))
doc=fitz.open(root/'任务书最终盖章签字版本.pdf')
pix=doc[35].get_pixmap(matrix=fitz.Matrix(2,2))
Image.frombytes('RGB',[pix.width,pix.height],pix.samples).rotate(90,expand=True).save(root/'文档/核查记录/绩效附表核查.png')
