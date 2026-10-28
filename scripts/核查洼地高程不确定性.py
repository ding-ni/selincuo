"""Compare local spill height differences to the product's relative error mask."""
import json
import math
from pathlib import Path
import rasterio
from backend.common import ROOT,read_json,write_json,sha_file

def main():
    raw=ROOT/'data/raw/spatial/公开地形高程误差-CopernicusGLO90-20261028'
    files=list(raw.glob('*/AUXFILES/*HEM.tif'))
    lookup={}
    for path in files:
        name=path.name;lat=int(name.split('_N')[1][:2]);lon=int(name.split('_E')[1][:3]);lookup[(lat,lon)]=rasterio.open(path)
    source=ROOT/'build/第三轮整改-20261028/连续地形/洼地溢流门槛.json';report=read_json(source);rows=[]
    def sample(point):
        lon,lat=point;ds=lookup.get((int(math.floor(lat)),int(math.floor(lon))))
        if ds is None:return None
        value=float(next(ds.sample([point]))[0]);return value if value>=0 and value!=ds.nodata and math.isfinite(value) else None
    for row in report['pits']:
        point=row['anchor'];coords=row['saddleCoordinates'];a=sample(point);b=sample(coords[0]) if coords else None
        sigma=math.sqrt(a*a+b*b) if a is not None and b is not None else None
        rows.append({k:row[k] for k in ('rawCode','anchor','geometryAreaKm2','spillDepthM','maxChainPitDepthM','potentialReceiverCode')}|
                    {'minimumHEMStdM':a,'saddleHEMStdM':b,'independentErrorDifferenceStdM':sigma,
                     'localDepthOverStd':row['spillDepthM']/sigma if sigma and row['spillDepthM'] is not None else None})
    for ds in lookup.values():ds.close()
    # Python 3.9 supports mapping union, and JSON stores no NaN/Inf.
    result={'source':'Copernicus GLO-90 HEM standard deviation; source XML retained',
            'sourceReportSHA256':sha_file(source),'availableTiles':len(files),
            'interpretation':'The two-pixel quadrature is an independence approximation. Local spatial errors can be correlated. Small depth relative to HEM does not prove a depression artificial or an outlet open.',
            'twoMetreRule':'An engineering conditioning scenario tested before calibration; mapped water bodies are always excluded.',
            'pits':rows}
    out=ROOT/'build/第三轮整改-20261028/连续地形/洼地高程误差核查.json'
    if out.exists():raise ValueError('保留已有误差核查，不能覆盖')
    write_json(out,result)
    print(json.dumps({'availableTiles':len(files),'pits':len(rows),'largest':sorted(rows,key=lambda r:r['geometryAreaKm2'],reverse=True)[:8]},ensure_ascii=False))

if __name__=='__main__':main()
