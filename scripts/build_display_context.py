# coding: utf-8
"""Display-only terrain and supplied 1 km river lines; numerical assets unchanged."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import backend
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.warp import reproject,Resampling
from rasterio.transform import from_bounds
from PIL import Image
from backend.common import sha_file,write_json,now

def main():
    folder=ROOT/'data/derived/spatial'
    terrain=ROOT/'data/raw/spatial/terrain-reference/dem-1km-filled.tif'
    rivers=ROOT/'data/raw/spatial/stream-package/merged/selinco_4basin_streams_thr150cells.shp'
    bounds=[87.1,29.8,92.5,33.6]
    width,height=1200,850
    values=np.full((height,width),np.nan,dtype=np.float32)
    with rasterio.open(terrain) as ds:
        reproject(rasterio.band(ds,1),values,src_transform=ds.transform,src_crs=ds.crs,
                  dst_transform=from_bounds(*bounds,width,height),dst_crs='EPSG:4326',
                  src_nodata=ds.nodata,dst_nodata=np.nan,resampling=Resampling.bilinear)
    valid=np.isfinite(values)
    filled=np.where(valid,values,5000)
    dy,dx=np.gradient(filled)
    shade=np.clip(.66-dx*.006+dy*.008,.22,.98)
    color=np.stack([212*shade+35,220*shade+31,207*shade+40,np.where(valid,210,0)],axis=-1)
    Image.fromarray(np.clip(color,0,255).astype(np.uint8),'RGBA').save(folder/'terrain-display.png')
    g=gpd.read_file(rivers).to_crs('EPSG:4326')
    features=[]
    for _,row in g.iterrows():
        geom=row.geometry
        if geom is not None and not geom.is_empty:
            features.append({'type':'Feature','properties':{'kind':'river'},'geometry':geom.simplify(.0006,preserve_topology=True).__geo_interface__})
    write_json(folder/'rivers-display.geojson',{'type':'FeatureCollection','features':features})
    from shapely.geometry import shape,mapping
    units=json.loads((folder/'units.geojson').read_text(encoding='utf-8'))
    units['features']=[{**f,'geometry':mapping(shape(f['geometry']).simplify(.0015,preserve_topology=True))} for f in units['features']]
    write_json(folder/'units-display.geojson',units)
    write_json(folder/'display-context.json',{'createdAt':now(),'bounds':bounds,'displayOnly':True,
        'note':'基于已提供的1km地形与150格阈值河网制作，仅作地图展示，不改变模型汇流。',
        'terrainSource':{'path':terrain.relative_to(ROOT).as_posix(),'sha256':sha_file(terrain)},
        'riverSource':{'path':rivers.relative_to(ROOT).as_posix(),'sha256':sha_file(rivers),'features':len(features)}})
    print('Built display terrain and',len(features),'supplied river features')
if __name__=='__main__':main()
