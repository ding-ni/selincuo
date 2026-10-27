"""Read-only shore/main level comparison. Does not calibrate or correct data."""
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.assemble_release import ReadOnlySeed
from backend.observation_query import SeriesView
from backend.observations import daily_series
from backend.common import sha_file,read_json


def audit(workspace):
    db=ReadOnlySeed(workspace)
    identifiers=['SLC','SLC-N','SLC-E','SLC-S','SLC-W']
    frame=pd.concat({s:daily_series(SeriesView(db),s,'level') for s in identifiers},axis=1)
    report={'workspace':str(db.workspace),'statistic':'existing effective daily observation values; paired differences = shore minus main',
            'independence':'matched days are descriptive temporal samples, not independent replicates',
            'uncertainty':'p05-p95 describes daily difference distribution, not a confidence interval',
            'commonDays':int(frame.dropna().shape[0]),'coverage':{},'pairedMain':{},'coordinates':[],
            'standardConfigurationSHA256':sha_file(ROOT/'config/operational/historical-standard-v1.json'),
            'modelPolicy':'standard parameters, main-station anchors and 2024 calibration/2025 evaluation boundary unchanged',
            'datumConfirmation':read_json(ROOT/'config/station-datums.json')}
    for station in identifiers:
        values=frame[station].dropna()
        report['coverage'][station]={'validDays':len(values),'start':str(values.index.min().date()) if len(values) else None,'end':str(values.index.max().date()) if len(values) else None}
        if station=='SLC':continue
        pair=frame[['SLC',station]].dropna();difference=pair[station]-pair.SLC
        summary={'matchedDays':len(pair),'start':str(pair.index.min().date()) if len(pair) else None,'end':str(pair.index.max().date()) if len(pair) else None,
                 'medianM':float(difference.median()) if len(pair) else None,'p05M':float(difference.quantile(.05)) if len(pair) else None,'p95M':float(difference.quantile(.95)) if len(pair) else None,
                 'byYear':{str(y):{'matchedDays':len(g),'medianM':float((g[station]-g.SLC).median())} for y,g in pair.groupby(pair.index.year)}}
        report['pairedMain'][station]=summary
    for station in db.rows("SELECT * FROM stations WHERE id LIKE 'SLC-%' ORDER BY id"):
        station['metadata']=json.loads(station['metadata']);report['coordinates'].append(station)
    from backend.published_results import current_id,detail,result_file
    identifier=current_id()
    if identifier:
        task=detail(identifier)
        modeled=pd.Series({r['date'][:10]:r['lakeLevel'] for r in task['result']['daily']},dtype=float)
        modeled.index=pd.to_datetime(modeled.index)
        comparison=frame.join(modeled.rename('model'))
        with np.load(result_file(identifier,'hourly.npz'),allow_pickle=False) as hourly:
            finite=np.isfinite(hourly['lakeLevel']);starts=np.flatnonzero(finite & ~np.r_[False,finite[:-1]])
            excluded=sorted(set(pd.DatetimeIndex(hourly['time'].astype('datetime64[ns]'))[starts].strftime('%Y-%m-%d')))
        comparison=comparison.loc[~comparison.index.strftime('%Y-%m-%d').isin(excluded)]
        common=comparison.dropna(subset=identifiers+['model'])
        def evaluate(selected):
            result={}
            for station in identifiers:
                pair=selected[[station,'model']].dropna();error=pair.model-pair[station]
                result[station]={'count':len(pair),'start':str(pair.index.min().date()) if len(pair) else None,'end':str(pair.index.max().date()) if len(pair) else None,
                                 'biasM':float(error.mean()) if len(pair) else None,'maeM':float(error.abs().mean()) if len(pair) else None,'rmseM':float(np.sqrt((error**2).mean())) if len(pair) else None}
            return result
        report['absoluteModelEvaluation']={'resultId':identifier,'resultArtifactSHA256':sha_file(result_file(identifier,'result.json')),
                                           'errorDirection':'model minus observation; no offset correction or fitted alignment','excludedInitializationDays':excluded,
                                           'allMatched':evaluate(comparison),'commonDates':evaluate(common),'commonDateCount':len(common),
                                           'limitations':'conditional main-station-anchored model replay; no shore-specific hydrodynamics; supplementary evaluation does not replace frozen metrics'}
    try:
        from shapely.geometry import shape,Point
        from shapely.ops import transform,unary_union
        from pyproj import Transformer
        collection=json.loads((ROOT/'data/derived/spatial/display.geojson').read_text(encoding='utf-8'))
        lake=unary_union([shape(f['geometry']) for f in collection['features'] if f['properties']['kind']=='lake'])
        project=Transformer.from_crs('EPSG:4326','EPSG:32645',always_xy=True).transform
        boundary=transform(project,lake).boundary
        for station in report['coordinates']:
            station['distanceToDisplayShorelineM']=round(transform(project,Point(station['lon'],station['lat'])).distance(boundary),1)
        report['shorelineNote']='distance to generalized display shoreline only; no coordinate snapping, survey validation or model geometry update'
    except ImportError:
        report['shorelineNote']='optional geometry tools unavailable; coordinates are not snapped or surveyed'
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'workspace')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.workspace)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
