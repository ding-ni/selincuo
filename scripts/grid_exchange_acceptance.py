# -*- coding: utf-8 -*-
"""Import/export and recoverability using actual data in a dedicated workspace."""
import sys,shutil,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from backend.common import ROOT,read_json,write_json,sha_file,digest
from backend.database import Database
from backend.data_exchange import import_records
from backend.forcing import register_era5,register_lake_correction
from backend.operational_scheme import definition
from backend.grid_storage import Writer,open_arrays,export_npz
from backend.maintenance import backup_workspace,restore_preview,restore_workspace
from backend.observation_query import catalog
from backend.forcing_latest import provenance_summary

out=ROOT/'build/grid-revision-20261026';business=Database(out/'workspace');workspace=out/'exchange-acceptance-workspace';db=Database(workspace)
db.set_setting('workspacePurpose',{'kind':'acceptance','createdBy':'grid_exchange_acceptance.py'})
with db.connect() as c:
    for s in business.rows('SELECT * FROM stations'):
        c.execute('INSERT OR IGNORE INTO stations VALUES(?,?,?,?,?,?)',tuple(s[k] for k in ('id','name','kind','lon','lat','metadata')))
row=business.rows("SELECT * FROM observations WHERE station_id='SLC' AND variable='level' AND quality='valid' AND value IS NOT NULL AND time>='2026-01-01' ORDER BY time LIMIT 1")[0]
payload={'schemaVersion':1,'projectId':'selinco','sourceKey':'acceptance-real-SLC-source-row-'+str(row['id']),'timeZone':'Asia/Shanghai','records':[{'station':'SLC','variable':'level','time':row['time'],'step':row['step'],'value':row['value'],'unit':row['unit']}]}
imported=import_records(db,payload);assert db.observations('SLC','level')[0]['value']==row['value']
incoming=workspace/'incoming';incoming.mkdir(exist_ok=True)
path=next((business.workspace/'downloads/era5-land').glob('era5-land-202610-*.nc'));target=incoming/path.name
if not target.exists():shutil.copy2(path,target)
input_product=register_era5([target],db,name='真实2026年10月资料接入验收')
corrected=register_lake_correction(db,input_product['id'],definition()['inputRequirement']['lakePrecipitationCorrection'])
assert corrected['end']>'2026-10-20'
source_folder=business.workspace/'tasks'/read_json(out/'baseline-evidence.json')['taskId'];arrays=open_arrays(source_folder)
times=pd.to_datetime(arrays['time'][:]);a=int(times.get_loc('2025-06-01T00:00:00'));b=a+49
sample=workspace/'reports/real-history-frames';sample.mkdir(parents=True,exist_ok=True)
if not (sample/'hourly.zarr').exists():
    writer=Writer(sample,b-a,1597,8);writer.put(0,b-a,{k:arrays[k][a:b] for k in arrays.array_keys()});writer.finish()
write_json(sample/'provenance.json',{'sourceTaskId':source_folder.name,'sourceHourlyManifestSHA256':sha_file(source_folder/'hourly-manifest.json'),'start':times[a].isoformat(),'end':times[b-1].isoformat(),'historyFramesOnly':True,'completeContinuationState':False})
t=time.monotonic();exported=export_npz(sample)
with np.load(exported,allow_pickle=False) as npz:
    for key in arrays.array_keys():np.testing.assert_array_equal(npz[key],arrays[key][a:b])
export_seconds=time.monotonic()-t
t=time.monotonic();backup=backup_workspace(db);archive=workspace/'exports'/backup['filename'];preview=restore_preview(archive)
destination=out/('restore-acceptance-'+backup['filename'][:-4]);restored=restore_workspace(archive,destination)
original=open_arrays(sample);recovered=open_arrays(destination/'reports/real-history-frames')
for key in original.array_keys():np.testing.assert_array_equal(original[key][:],recovered[key][:])
restored_db=Database(destination);assert restored_db.observations('SLC','level')[0]['value']==row['value']
original_db=Database(ROOT/'workspace')
unchanged={}
for table in ('observations','sources'):
    unchanged[table]=digest(original_db.rows('SELECT * FROM '+table+' ORDER BY '+('id' if table=='observations' else 'id')))==digest(business.rows('SELECT * FROM '+table+' ORDER BY id'))
assert all(unchanged.values())
preparation=read_json(out/'preparation.json');assert sha_file(ROOT/'config/operational/historical-standard-v1.json')==preparation['standardConfigurationSHA256']
evidence={'observationImport':{'originalSourceRecordId':row['id'],'actualTime':row['time'],'actualValue':row['value'],'newSourceId':imported['sourceId']},'futureNetCDF':{'rawSHA256':sha_file(target),'inputId':corrected['id'],'start':corrected['start'],'end':corrected['end'],'frozenLakeCorrectionSHA256':corrected['transformationSHA256']},'zarrExport':{'hours':49,'units':1597,'allVariablesExactlyEqual':True,'seconds':export_seconds,'bytes':exported.stat().st_size},'backup':{**backup,'previewFiles':len(preview['files']),'restore':restored,'secondsIncludingRestore':time.monotonic()-t,'allZarrValuesExactlyEqual':True},'originalTablesUnchanged':unchanged,'standardConfigurationUnchanged':True,'standardResultId':read_json(ROOT/'data/final-results/catalog.json')['currentResultId']}
latest=read_json(business.workspace/'downloads/era5-land/latest-status.json');manifest=read_json(business.workspace/'inputs/products'/latest['inputId']/'manifest.json');evidence['latestInputProvenance']=provenance_summary(business,manifest)
write_json(out/'exchange-evidence.json',evidence);print(json.dumps({k:v for k,v in evidence.items() if k!='latestInputProvenance'},ensure_ascii=False),flush=True)
