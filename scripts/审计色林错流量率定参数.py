"""Audit current Duja response using real forcing, a fixed 2024 mask and no validation data.

The only output is a new, exclusive small JSON receipt. No service, user
scheme or existing result is modified; no cleanup or automatic deletion occurs.
"""
from contextlib import contextmanager
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs/evidence/第三轮参数作用与率定审计-20261028.json'
PROGRESS = ROOT / 'build/第三轮整改-20261028/参数真实响应审计进度.jsonl'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['NUMEXPR_NUM_THREADS'] = '1'
os.environ['SELINCO_WORKSPACE'] = str(ROOT / 'build/第三轮整改-20261028/参数审计数值缓存')
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import numpy as np
import pandas as pd

from backend.calibration_config import FLOW_SELECTABLE_KEYS, FLOW_FORBIDDEN_KEYS, flow_objective
from backend.calibration_diagnostics import _sensitivity
from backend.common import digest, sha_file
from backend.grid_simulation import prepare, run, unit_forcing, model_identity
from backend.model_core import DEFAULT_PARAMETERS, PARAMETER_DEFINITIONS, parameters
from backend.observation_query import selected_sql
from backend.observations import daily_series
from backend.parameter_regions import region_initial_states, run_region_kernel
from backend.routing import route_partition
from backend.simulation import metrics

WORKSPACE = ROOT / 'build/grid-revision-20261026/workspace'
INPUT = 'INPUT-fc731ba1bf2b5e79d3e03d92'
SPACE = 'AGG-SPATIAL-dbb6e03518db589dc16f'


class ReadOnlyDB:
    def __init__(self):
        self.workspace = WORKSPACE
        self.path = self.workspace / 'database/hydrology.sqlite3'

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def rows(self, sql, args=()):
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, args)]


class TrainingView:
    def __init__(self, db):
        self.db = db

    def observations(self, station, variable, **kwargs):
        if (station, variable) != ('DUJA', 'flow'):
            raise ValueError('流量诊断不读取其他观测')
        sql, args = selected_sql(station, variable, start='2024-01-01', end='2024-12-31', lean=True)
        return self.db.rows(sql + ' ORDER BY time,id', args)

    def rows(self, *args):
        return self.db.rows(*args)


def history():
    rows = []
    for seed in (42, 142, 242):
        for stage in ('runoff', 'routing', 'lake'):
            path = ROOT / f'build/model-assessment-20261023/seed-{seed}/{stage}/result.json'
            c = json.loads(path.read_text(encoding='utf-8'))['calibration']
            objectives = [float(x['objective']) for x in c['trace']]
            rows.append({'path':str(path.relative_to(ROOT)).replace('\\','/'),'seed':seed,'stage':stage,
                         'evaluations':c['evaluations'],'maxEvaluations':c['maxEvaluations'],
                         'termination':c['termination'],'converged':c['converged'],
                         'boundaryParameters':c['boundaryParameters'],'searchParameters':c['searchParameters'],
                         'baselineObjective':objectives[0],'bestObjective':c['bestObjective'],
                         'distinctObjectives':len(set(objectives))})
    return rows


def main(output=OUTPUT, progress_path=None):
    output = Path(output).resolve()
    progress_path = (Path(progress_path).resolve() if progress_path else
                     PROGRESS if output == OUTPUT else output.with_suffix('.progress.jsonl'))
    if output == progress_path:
        raise ValueError('收据与进度必须使用不同文件')
    for path in (output, progress_path):
        if ROOT not in path.parents:
            raise ValueError('新增核验输出必须位于本项目内')
        if path.exists():
            raise FileExistsError('核验产物已存在，请另设批次，不覆盖：' + str(path))
    output.parent.mkdir(parents=True, exist_ok=True)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    progress = progress_path.open('x', encoding='utf-8', newline='\n')
    def checkpoint(event):
        progress.write(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')
        progress.flush()
        os.fsync(progress.fileno())
    from backend import grid_calibration
    global_guard = False
    try:
        grid_calibration.run(None,{'target':'duja','periods':{},'search':{'scope':'global','keys':['WM']}},Path('unused'),lambda *_:None,lambda:False)
    except ValueError as exc:
        global_guard = '杜加控制区' in str(exc)
    if not global_guard:
        raise ValueError('全局范围防线未生效')
    db = ReadOnlyDB()
    flow = daily_series(TrainingView(db), 'DUJA', 'flow', start='2024-01-01', end='2024-12-31').dropna()
    observed = {d.isoformat():float(v) for d,v in flow.items()}
    if len(observed) < 30:
        raise ValueError('2024杜加可用流量不足30日')
    source = prepare(db, INPUT, SPACE)
    cfg = json.loads((ROOT / 'config/operational/continuous-builtin-v1.json').read_text(encoding='utf-8'))
    global_values = parameters(cfg['parameters'])
    duja_values = parameters({**global_values, **cfg['partitionParameters']['duja']})
    obs = {'profile':'2024-quality-flow-only','flow':observed,'level':{}}
    initial = {'time':'2021-04-23T12:00:00','level':4543.66,
               'method':'first-quality-daily-level-at-noon','source':'主站质量控制日值作为12时代表',
               'reference':'SLC-working-datum'}
    req = {'inputId':INPUT,'spatialVersion':SPACE,'start':'2020-01-01T00:00:00',
           'end':'2025-01-01T00:00:00','parameters':global_values,
           'partitionParameters':{'duja':{k:v for k,v in duja_values.items() if k!='lakeCoefficient'}},
           'routingMode':'source-separated','_fixedInitialization':initial,'_observations':obs,
           '_modelIdentity':model_identity(),'_metricsOnly':True}
    print('读取真实资料完成；开始一次完整空间杜加流量对照', flush=True)
    full = run(db, req, prepared=source)
    spatial = source['spatial']
    selected = [i for i,u in enumerate(spatial['units']) if u['partition']=='duja']
    subset = dict(spatial)
    subset['units'] = [spatial['units'][i] for i in selected]
    if 'aggregation' in spatial:
        subset['aggregation'] = [spatial['aggregation'][i] for i in selected]
    times = source['times']
    indexes = np.flatnonzero((times >= pd.Timestamp(req['start'])) & (times <= pd.Timestamp(req['end'])))
    selected_times = times[indexes]
    areas = np.array([u['areaKm2'] for u in subset['units']])
    glaciers = np.array([u['kind']=='glacier' for u in subset['units']])
    blocks = []
    for offset in range(0,len(indexes),168):
        ids=indexes[offset:offset+168]
        blocks.append((ids, unit_forcing(subset,source['arrays'],ids,duja_values,{'duja':duja_values})))

    def duja_flow(candidate, initial_fraction=.5):
        state = region_initial_states(subset['units'], candidate, {'duja':candidate})
        for i, unit in enumerate(subset['units']):
            if unit['kind'] == 'land':
                state[i,2:5] *= initial_fraction/.5
        routed, out = {}, []
        for ids, cached in blocks:
            if candidate['lapseRate'] == duja_values['lapseRate']:
                rain, temperature, evap = cached
            else:
                rain, temperature, evap = unit_forcing(subset,source['arrays'],ids,candidate,{'duja':candidate})
            diagnostic, _, state = run_region_kernel(rain,temperature,evap,glaciers,state,subset['units'],candidate,{'duja':candidate})
            volume = (diagnostic[:,:,:4] * areas[None,:,None] * 1000).sum(axis=1)
            q, _, routed, _, _ = route_partition(volume,routed,candidate,'source-separated')
            out.extend(q)
        hourly = pd.Series(out,index=selected_times)
        grouped = hourly.resample('D',closed='right',label='left')
        return grouped.mean().where(grouped.count().eq(24))

    baseline = duja_flow(duja_values)
    expected = pd.Series({pd.Timestamp(r['date']):r['duja'] for r in full['daily'] if r['duja'] is not None})
    compared = pd.concat([baseline.rename('subset'),expected.rename('full')],axis=1).dropna()
    if len(compared) < 1800:
        raise ValueError('完整与杜加子集的共同完整日不足')
    difference = float(np.max(np.abs(compared['subset']-compared['full'])))
    if difference > 1e-8:
        raise ValueError('子集流量未复现完整空间：' + str(difference))
    dates = pd.DatetimeIndex(sorted(set(flow.index) & set(baseline.dropna().index)))
    if len(dates) < 30:
        raise ValueError('固定训练日期不足')
    observed_vec = flow.reindex(dates).to_numpy(float)
    base_vec = baseline.reindex(dates).to_numpy(float)
    scale = float(np.std(observed_vec))
    if scale <= 1e-10:
        raise ValueError('观测流量方差不足')
    observed_series = pd.Series(observed_vec,index=dates)
    baseline_metric = metrics(observed_series,pd.Series(base_vec,index=dates))
    baseline_objective = flow_objective(baseline_metric,'balanced')[0]
    checkpoint({'phase':'baseline','trainingPairs':len(dates),'baselineMetrics':baseline_metric,
                'baselineObjective':baseline_objective,'fullSubsetCommonDays':len(compared),
                'fullSubsetMaxFlowDifferenceM3s':difference})
    definitions = {d['key']:d for d in PARAMETER_DEFINITIONS}
    rows, columns = [], {}
    for index,key in enumerate(FLOW_SELECTABLE_KEYS + FLOW_FORBIDDEN_KEYS[:2]):
        d = definitions[key]
        lo,hi = d['min'],d['max']
        step = (hi-lo)*.05
        choices = [min(hi,duja_values[key]+step), max(lo,duja_values[key]-step)]
        proposal = None
        for value in choices:
            if abs(value-duja_values[key]) <= 1e-15:
                continue
            try:
                proposal = parameters({**duja_values,key:value})
                break
            except ValueError:
                continue
        if proposal is None:
            rows.append({'key':key,'status':'constraint-blocked','baseline':duja_values[key]})
            continue
        predicted = duja_flow(proposal).reindex(dates)
        if not np.isfinite(predicted.to_numpy(float)).all():
            raise ValueError('候选缺少固定配对日期：'+key)
        delta = predicted.to_numpy(float)-base_vec
        maximum = float(np.max(np.abs(delta)))
        tolerance = 1e-10*max(1.,float(np.max(np.abs(base_vec))))
        normal_step=(proposal[key]-duja_values[key])/(hi-lo)
        normalized=delta/scale/normal_step
        if key in FLOW_SELECTABLE_KEYS and maximum>tolerance:
            columns[key]=normalized
        metric=metrics(observed_series,pd.Series(predicted.to_numpy(float),index=dates))
        row = {'key':key,'status':'responsive' if maximum>tolerance else 'no-local-response',
                     'flowSearchAllowed':key in FLOW_SELECTABLE_KEYS,'baseline':duja_values[key],
                     'probe':proposal[key],'min':lo,'max':hi,'maxFlowChangeM3s':maximum,
                     'rmsFlowChangeM3s':float(np.sqrt(np.mean(delta**2))),
                     'normalizedSensitivityRms':float(np.sqrt(np.mean(normalized**2))),
                     'toleranceM3s':tolerance,'balancedObjective':flow_objective(metric,'balanced')[0],
                     'nse':metric['nse']}
        rows.append(row)
        checkpoint({'phase':'response',**row})
        print('真实训练期响应 %d/%d: %s max ΔQ %.8g' % (index+1,len(FLOW_SELECTABLE_KEYS)+2,key,maximum),flush=True)

    initial_sensitivity = []
    for fraction in (.25,.75):
        predicted = duja_flow(duja_values,fraction).reindex(dates)
        if not np.isfinite(predicted.to_numpy(float)).all():
            raise ValueError('初始土壤含水诊断缺少固定训练日期')
        delta = predicted.to_numpy(float)-base_vec
        metric = metrics(observed_series,pd.Series(predicted.to_numpy(float),index=dates))
        row = {'initialTensionWaterFraction':fraction, 'maxFlowDifferenceM3s':float(np.max(np.abs(delta))),
               'rmsFlowDifferenceM3s':float(np.sqrt(np.mean(delta**2))),'metrics':metric,
               'deltaNse':float(metric['nse']-baseline_metric['nse']),
               'deltaPbiasPercentagePoints':float(metric['pbias']-baseline_metric['pbias'])}
        initial_sensitivity.append(row)
        checkpoint({'phase':'initial-storage',**row})
        print('真实初态诊断 %.0f%% max ΔQ %.8g ΔPBIAS %.4g' % (fraction*100,row['maxFlowDifferenceM3s'],row['deltaPbiasPercentagePoints']),flush=True)

    inventory = [{'key':d['key'],'label':d['label'],'unit':d['unit'],'group':d['group'],
                  'default':DEFAULT_PARAMETERS[d['key']],'min':d['min'],'max':d['max'],
                  'currentGlobal':global_values[d['key']],'currentDuja':duja_values[d['key']] if d['key']!='lakeCoefficient' else None,
                  'flowCalibration':d['flowCalibration']} for d in PARAMETER_DEFINITIONS]
    report = {'description':'真实色林错资料：2020统一预热、2024杜加训练日单参数局部响应；不读取2025/26观测，不按湖位筛选。',
      'sourceScriptSHA256':sha_file(__file__),
      'inputId':INPUT,'spatialVersion':SPACE,'modelIdentity':model_identity(),
      'parameterConfigurationSHA256':sha_file(ROOT/'config/operational/continuous-builtin-v1.json'),
      'flowObservationsSHA256':digest(observed),'initialization':initial,'trainingStart':'2024-01-01',
      'trainingEndExclusive':'2025-01-01','trainingPairs':len(dates),'trainingDates':[d.date().isoformat() for d in dates],
      'baselineFlowMetrics':baseline_metric,'baselineBalancedObjective':baseline_objective,
      'fullVersusDujaOnly':{'sourceUnits':len(spatial['units']),'dujaUnits':len(selected),
          'commonCompleteDays':len(compared),'maxAbsoluteFlowDifferenceM3s':difference,
          'note':'仅证明同初始条件及所测基线下杜加流量相同；杜加子集不是全域湖位成果。'},
      'perturbationFractionOfBounds':.05,'response':rows,'localSVD':_sensitivity(columns),
      'initialTensionWaterSensitivity':{'baselineFraction':.5,'alternatives':initial_sensitivity,
          'note':'仅改变2020年初始陆地张力水，积雪及汇流初态沿原实现；不改变正式模型或湖位起算。'},
      'limits':['一次单侧局部扰动不能证明参数全局可辨识；边界和参数耦合影响结果。',
                '杜加流量间接约束雪冰过程，不代替独立雪盖、雪水当量、冰川观测。',
                '真实2025和2026观测未进入本次响应计算；历史开发中已多次查看，不称全新独立检验。',
                '裸冰升华在当前模型不改变径流，湖面系数不改变杜加流量。'],
      'parameters':inventory,'historicalSearches':history(),
      'scopeReproduction':{'type':'archived-experiment-reference-not-rerun',
          'archivedReceipt':str(OUTPUT.relative_to(ROOT)).replace('\\','/'),
          'archivedReceiptSHA256':sha_file(OUTPUT) if OUTPUT.exists() else None,
          'currentGlobalScopeRejected':global_guard,
          'currentNumericalTest':'tests/test_parameter_transfer_integration.py',
          'note':'修复前实验不再由本脚本伪重算；当前真实内核合成遮蔽对照由独立自动回归复现。'},
      'elapsedSeconds':round(time.monotonic()-started,3)}
    with output.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')
    checkpoint({'phase':'completed','receipt':str(output)})
    progress.close()
    print(json.dumps({'receipt':str(output),'trainingPairs':len(dates),'fullSubsetMaxDifferenceM3s':difference,
                      'elapsedSeconds':report['elapsedSeconds']},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT, help='新的独占JSON路径；已有收据不覆盖')
    parser.add_argument('--progress', type=Path, help='新的独占进度路径；默认随新收据生成')
    args = parser.parse_args()
    main(args.output, args.progress)
