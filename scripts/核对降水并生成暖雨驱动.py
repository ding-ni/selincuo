"""Audit recorded gauge observations and register an immutable warm-rain input."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sys
import uuid

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import now, read_json, sha_file
from backend.database import Database
from backend.forcing import lake_precipitation_series
from backend.grid_simulation import prepare, unit_forcing
from backend.land_precipitation import register_product
from backend.observations import station_for, xls_records, xlsx_records
from 站点暖雨订正对照 import (INPUT_ID, SPATIAL_ID, STATIONS, make_profile,
                        station_diagnostics, station_samples, summarize_samples)


def main():
    db=Database()
    old_path=db.workspace/'tasks/JOB-3354c8660e854cbfa96f/站点暖雨对照报告.json'
    old=read_json(old_path)
    confirmed=old['profile']['confirmedEventSources']
    for item in confirmed:
        rows=db.rows('SELECT sha256 FROM sources WHERE id=?',(item['sourceId'],))
        if len(rows)!=1 or rows[0]['sha256']!=item['sourceSHA256']:
            raise ValueError('确认的逐次雨量来源已变，不能沿用覆盖记录')
        coverage=db.rows('SELECT start,end,metadata FROM coverage WHERE source_id=? AND station_id=? AND variable=? AND step=?',
                         (item['sourceId'],item['station'],'precipitation','irregular'))
        if len(coverage)!=1:
            raise ValueError('逐次雨量覆盖记录不存在或不唯一')
        meta=json.loads(coverage[0]['metadata'])
        if not (coverage[0]['start']==item['start'] and coverage[0]['end']==item['endExclusive']
                and meta.get('coverageConfirmed') and meta.get('zeroForAbsent')
                and not meta.get('stoppedIntervals') and meta.get('eventTimeMeaning')=='occurrence-time'):
            raise ValueError('逐次雨量覆盖与用户确认不一致')
    source_audit=[]
    for row in db.rows("SELECT DISTINCT s.id,s.path,s.sha256,s.bytes,s.metadata FROM sources s JOIN observations o ON o.source_id=s.id WHERE o.variable='precipitation' ORDER BY s.id"):
        meta=json.loads(row['metadata'])
        archive=Path(meta.get('archivePath') or meta.get('archive') or '')
        if not archive.is_file():
            matches=list((db.workspace/'observation-sources'/row['sha256']).glob('*'))
            archive=next((p for p in matches if p.is_file()),Path(row['path']))
        row['archiveAvailable']=archive.is_file()
        row['archivedSHA256']=sha_file(archive) if archive.is_file() else None
        row['shaMatches']=row['archivedSHA256']==row['sha256'] if archive.is_file() else None
        row['observationCounts']=db.rows("SELECT station_id,step,unit,quality,COUNT(*) count FROM observations WHERE source_id=? AND variable='precipitation' GROUP BY station_id,step,unit,quality ORDER BY station_id,step,unit,quality",(row['id'],))
        if archive.is_file() and archive.suffix.lower() in ('.xls','.xlsx'):
            parser=xls_records if archive.suffix.lower()=='.xls' else xlsx_records
            # The immutable archive lives under a SHA folder, while the legacy
            # parser infers stations from the *original* path (dj/, blq/, ...).
            # Derive the station independently from that recorded source path.
            station=station_for(row['path'])
            parsed=list(parser(archive, station_override=station, variable_override='precipitation'))
            actual=db.rows("SELECT station_id,variable,time,period_start,period_end,step,unit,value,raw_text,quality,sheet,cell FROM observations WHERE source_id=? AND variable='precipitation'",(row['id'],))
            fields=lambda r:(r['station'],r['variable'],r['time'],r['start'],r['end'],r['step'],'mm',r['value'],r['raw'],r['quality'],r['sheet'],r['cell'])
            db_fields=lambda r:(r['station_id'],r['variable'],r['time'],r['period_start'],r['period_end'],r['step'],r['unit'],r['value'],r['raw_text'],r['quality'],r['sheet'],r['cell'])
            parsed_counter=Counter(fields(r) for r in parsed)
            db_counter=Counter(db_fields(r) for r in actual)
            row['stationInferredFromOriginalPath']=station
            row['excelParsedRecords']=len(parsed)
            row['sqliteRecords']=len(actual)
            row['excelOnlyRecords']=sum((parsed_counter-db_counter).values())
            row['sqliteOnlyRecords']=sum((db_counter-parsed_counter).values())
            row['excelOnlyExamples']=[list(record) for record in list((parsed_counter-db_counter).elements())[:3]]
            row['sqliteOnlyExamples']=[list(record) for record in list((db_counter-parsed_counter).elements())[:3]]
        row.pop('metadata',None)
        source_audit.append(row)
    source=prepare(db,INPUT_ID,SPATIAL_ID)
    samples,conflicts,snapshot_sha=station_samples(db,source,confirmed)
    stats=summarize_samples(samples)
    profile=make_profile(source,samples,stats,confirmed,snapshot_sha)
    profile.update(qualityRule='unresolved-daily-event-conflicts-excluded-v1',
                   interpretation='ERA格温暖时近似，设备暂按液态雨量；2017—2023使用后期站雨估计为时间外推。')
    diagnostics=station_diagnostics(profile,stats)
    manifest=register_product(db,INPUT_ID,profile,lake_warm_rain=True)
    times=source['times'];arrays=source['arrays'];spatial=source['spatial']
    area=np.asarray([u['areaKm2'] for u in spatial['units']]);lake_area=spatial['lakeAreaKm2']
    months=[]
    for period in pd.period_range('2017-01','2026-10',freq='M'):
        start=period.start_time;end=(period+1).start_time
        ids=np.flatnonzero((times>start)&(times<=end))
        if not len(ids):continue
        raw,_,_=unit_forcing(spatial,arrays,ids,{'lapseRate':-.0065},{},None,times)
        corrected,_,_=unit_forcing(spatial,arrays,ids,{'lapseRate':-.0065},{},profile,times)
        original_lake=lake_precipitation_series(source['manifest'],spatial,arrays,times,ids)
        corrected_lake=lake_precipitation_series(manifest,spatial,arrays,times,ids)
        months.append({'month':str(period),'hours':int(len(ids)),
             'originalLandMm':float(raw.sum(axis=0)@area/area.sum()),
             'correctedLandMm':float(corrected.sum(axis=0)@area/area.sum()),
             'originalLakeMm':float(original_lake.sum()),'correctedLakeMm':float(corrected_lake.sum()),
             'landAreaKm2':float(area.sum()),'lakeAreaKm2':float(lake_area)})
    folder=db.workspace/'diagnostics'/('降水驱动核对-'+uuid.uuid4().hex[:12])
    folder.mkdir(parents=True,exist_ok=False)
    report={'createdAt':now(),'baselineInputId':INPUT_ID,'correctedInputId':manifest['id'],
      'forcingSHA256':manifest['dataSHA256'],'landTransformationSHA256':manifest['landTransformationSHA256'],
      'lakeTransformationSHA256':manifest['transformationSHA256'],'profile':profile,
      'stationStatistics':stats,'conflictingDailyRecords':conflicts,'stationDiagnostics':diagnostics,
      'sourceFilesAndSQLite':source_audit,
      'monthComparison':months,'observationSnapshotSHA256':snapshot_sha,
      'previousReportSHA256':sha_file(old_path),
      'notes':['日表优先；日表与逐次不一致日排除于倍率估计，不覆盖观测。',
       '2025年估计与2026年时间对照，以及全部合格样本估计分别保留。',
       '新输入湖面暖时逐格订正，不叠加原湖面全年倍率；冷时原降水保留。',
       '2026年9—10月站雨缺行按已确认设备连续运行口径记零，仍应关注是否存在额外的数据缺口。']}
    path=folder/'降水资料核对与驱动对照.json'
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps({'inputId':manifest['id'],'report':str(path),'reportSHA256':sha_file(path),
                      'conflicts':len(conflicts),'months':len(months),'factors':
                      {s:profile['stations'][s]['seasonFactors'] for s in STATIONS}},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
