"""Immutable user-owned revisions; the operational standard is never updated here."""
from copy import deepcopy
import uuid
import numpy as np
import pandas as pd
from .common import digest,now,canonical
from .model_core import parameters,PARAMETER_DEFINITIONS
from .calibration_config import RUNOFF_KEYS,ROUTING_KEYS
from .operational_scheme import definition,catalog
from . import grid_spatial

def template(db):
    fixed=definition();spatial=grid_spatial.current();choice=catalog(db)[0]
    return {'schemaVersion':2,'name':'我的逐格网连续方案','modelFamily':'grid-continuous-v1','spatialVersion':spatial['version'] if spatial else '',
            'inputId':choice['inputId'],'parameters':deepcopy(fixed['parameters']),'partitionParameters':deepcopy(fixed['partitionParameters']),'routingMode':fixed['routingMode'],'target':'duja',
            'periods':{'warmupStart':'2023-01-01T00:00:00','trainingStart':'2024-01-01T00:00:00','trainingEnd':'2025-01-01T00:00:00','evaluationStart':'2025-01-01T00:00:00','evaluationEnd':'2026-01-01T00:00:00'},
            'search':{'scope':'duja','keys':RUNOFF_KEYS+ROUTING_KEYS,'bounds':{d['key']:[d['min'],d['max']] for d in PARAMETER_DEFINITIONS},'budgets':{'runoff':120,'routing':120,'lake':30},'seed':42,'objectiveProfile':'balanced'},'origin':'user','promotion':'candidate-only'}

def validate(db,payload):
    value=deepcopy(payload)
    if value.get('schemaVersion')!=2 or value.get('modelFamily')!='grid-continuous-v1':raise ValueError('请选择逐格网研究方案')
    if not str(value.get('name','')).strip() or len(value['name'])>120:raise ValueError('请填写1—120字方案名称')
    spatial=grid_spatial.load(value['spatialVersion'])
    if spatial.get('unitStrategy')!='grid-partition-elevation-surface-v2':raise ValueError('研究方案须采用新格网空间')
    value['parameters']=parameters(value.get('parameters'))
    from .parameter_regions import resolve_regions
    value['partitionParameters'],_=resolve_regions(spatial['units'],value['parameters'],value.get('partitionParameters'))
    product=next((i for i in db.items('input_products') if i['id']==value.get('inputId')),None)
    if not product:raise ValueError('方案所选气象输入不存在')
    periods=value['periods'];dates={key:pd.Timestamp(periods[key]) for key in ('warmupStart','trainingStart','trainingEnd','evaluationStart','evaluationEnd')}
    if any(t.tzinfo or t!=t.normalize() for t in dates.values()):raise ValueError('方案时段使用北京时间完整日边界')
    if not dates['warmupStart']<dates['trainingStart']<dates['trainingEnd']<=dates['evaluationStart']<dates['evaluationEnd']:raise ValueError('预热、训练、评价须按顺序且不重叠')
    if dates['trainingStart']-dates['warmupStart']<pd.Timedelta(days=365):raise ValueError('率定前至少保留365天统一预热')
    if pd.Timestamp(product['manifest']['start'])>dates['warmupStart'] or pd.Timestamp(product['manifest']['end'])<dates['evaluationEnd']:raise ValueError('输入不能完整覆盖方案预热和评价时段')
    search=value['search'];defs={d['key']:d for d in PARAMETER_DEFINITIONS}
    keys=search['keys']
    if not keys or len(keys)!=len(set(keys)) or set(keys)-set(defs):raise ValueError('参与搜索参数无效或重复')
    if search.get('scope') not in ('duja','background'):raise ValueError('参数区域无效')
    if value.get('target') not in ('duja','lake'):raise ValueError('率定对象无效')
    if value['target']=='lake' and keys!=['lakeCoefficient']:raise ValueError('湖泊对象仅搜索全湖蒸发系数')
    if value['target']=='duja' and 'lakeCoefficient' in keys:raise ValueError('杜加产汇流不同时搜索湖泊系数')
    baseline={**value['parameters'],**(value['partitionParameters'].get('duja',{}) if search['scope']=='duja' else {})}
    for key in keys:
        bounds=search['bounds'].get(key)
        if not isinstance(bounds,list) or len(bounds)!=2 or any(type(v) not in (int,float) or not np.isfinite(v) for v in bounds):raise ValueError('请填写有效上下界：'+key)
        if not defs[key]['min']<=bounds[0]<bounds[1]<=defs[key]['max']:raise ValueError('搜索上下界超过物理范围或顺序错误：'+key)
        if not bounds[0]<=baseline[key]<=bounds[1]:raise ValueError('初始值必须位于本次搜索范围内：'+key)
    for stage in ('runoff','routing','lake'):
        budget=search['budgets'][stage]
        if type(budget)!=int or not 1<=budget<=2000:raise ValueError('每阶段预算须为1—2000的整数')
    if type(search.get('seed'))!=int or not 0<=search['seed']<2**32:raise ValueError('随机种子无效')
    if search.get('objectiveProfile') not in ('balanced','peaks'):raise ValueError('率定目标无效')
    if value.get('routingMode')!='source-separated':raise ValueError('新研究方案采用分水源调蓄与河道汇流')
    value['promotion']='candidate-only';value['origin']='user'
    return value

def save(db,payload,parent_id=None):
    value=validate(db,payload);parent=None
    for key in ('configurationSHA256','revision','familyId','parentRevisionId','id','created_at'):
        value.pop(key,None)
    if parent_id:
        parent=next((r for r in db.items('schemes') if r['id']==parent_id),None)
        if not parent:raise ValueError('原方案修订不存在')
    value['revision']=int(parent['payload'].get('revision',1))+1 if parent else 1
    value['parentRevisionId']=parent_id;value['familyId']=parent['payload'].get('familyId',parent_id) if parent else 'FAMILY-'+uuid.uuid4().hex[:16]
    value['configurationSHA256']=digest(value);identifier='SCHEME-'+uuid.uuid4().hex[:20]
    db.save_item('schemes',identifier,value['name'],value);db.log('schemes.revision',{'id':identifier,'parentId':parent_id,'sha256':value['configurationSHA256']})
    return {'id':identifier,'name':value['name'],'payload':value}

def prepare_request(db,request):
    identifier=request.get('researchSchemeId')
    row=next((s for s in db.items('schemes') if s['id']==identifier),None)
    if not row:raise ValueError('请先保存我的方案修订')
    value=validate(db,row['payload'])
    p=value['periods']
    if request.get('candidateParameterId'):
        candidate=next((s for s in db.items('parameter_versions') if s['id']==request['candidateParameterId']),None)
        if not candidate:raise ValueError('候选参数不存在')
        task=db.task(candidate['payload'].get('sourceTaskId'))
        if task['status']!='succeeded' or task['result'].get('modelFamily')!='grid-continuous-v1' or task['result']['spatialVersion']!=value['spatialVersion']:
            raise ValueError('候选参数必须来自同一逐格网空间的成功成果')
        value['parameters']=deepcopy(candidate['payload']['parameters']);value['partitionParameters']=deepcopy(candidate['payload'].get('partitionParameters',{}))
        request={**request,'candidateSnapshot':deepcopy(candidate)}
    return {**request,'modelFamily':'grid-continuous-v1','spatialVersion':value['spatialVersion'],'inputId':value['inputId'],'parameters':deepcopy(value['parameters']),'partitionParameters':deepcopy(value['partitionParameters']),
            'routingMode':value['routingMode'],'start':p['warmupStart'],'end':request.get('end') or p['evaluationEnd'],'schemeSnapshot':deepcopy({'id':identifier,**row['payload']}),'researchConfiguration':value,'target':value['target'],'periods':deepcopy(p),'search':deepcopy(value['search']),'origin':'user-research'}
