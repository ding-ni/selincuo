"""Derive one display raster from the existing DEM; leave source and frozen assets intact."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import rasterio
from rasterio.warp import reproject,Resampling
from rasterio.transform import from_bounds
from PIL import Image
from backend.common import sha_file,read_json,write_json

def main():
    folder=ROOT/'data/derived/spatial'
    source=ROOT/'data/raw/spatial/terrain-reference/dem-1km-raw.tif'
    bounds=read_json(folder/'display-context.json')['bounds']
    identity={'sourceSHA256':sha_file(source),'bounds':bounds,'width':1200,'height':850,'style':'hypsometric-shaded-v1'}
    target=folder/'terrain-elevation.png';meta=folder/'terrain-elevation.json'
    previous=read_json(meta)
    if previous:
        if previous['identity']!=identity or sha_file(target)!=previous['imageSHA256']:
            raise ValueError('已有高程展示与来源不符，需明确新版本后再生成')
        print(json.dumps(previous,ensure_ascii=False));return
    if target.exists():raise FileExistsError(target)
    values=np.full((850,1200),np.nan,dtype=np.float32)
    with rasterio.open(source) as ds:
        reproject(rasterio.band(ds,1),values,src_transform=ds.transform,src_crs=ds.crs,
                  dst_transform=from_bounds(*bounds,1200,850),dst_crs='EPSG:4326',
                  src_nodata=ds.nodata,dst_nodata=np.nan,resampling=Resampling.bilinear)
    valid=np.isfinite(values)&(values>0)
    stops=np.array([4400,4600,4800,5000,5500,6000],dtype=float)
    colors=np.array([[120,153,138],[165,178,153],[202,199,161],[197,176,147],[177,165,150],[235,233,226]],dtype=float)
    filled=np.where(valid,values,5000);dy,dx=np.gradient(filled)
    shade=np.clip(.94-dx*.0015+dy*.002,.7,1.08)
    rgb=np.stack([np.interp(filled,stops,colors[:,i]) for i in range(3)],axis=-1)*shade[:,:,None]
    rgba=np.dstack([np.clip(rgb,0,255),np.where(valid,255,0)]).astype(np.uint8)
    Image.fromarray(rgba,'RGBA').save(target)
    result={'identity':identity,'bounds':bounds,'image':'/spatial/terrain-elevation.png',
            'imageSHA256':sha_file(target),'stops':stops.tolist(),'colors':['#78998a','#a5b299','#cac7a1','#c5b093','#b1a596','#ebe9e2'],
            'minimum':float(values[valid].min()),'maximum':float(values[valid].max()),
            'unit':'m','source':'已登记1 km DEM','displayOnly':True}
    write_json(meta,result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
