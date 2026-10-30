"""One immutable executable configuration for a scheme's ordinary start action."""
from copy import deepcopy

from .common import ROOT, digest, read_json, write_json, sha_file, within


def implementation():
    from .grid_simulation import model_identity
    names = ('calibration_configuration.py', 'two_stage_calibration.py', 'dual_calibration.py',
             'calibration_search.py', 'calibration_config.py', 'calibration_objective.py',
             'fast_calibration.py', 'fast_basin_calibration.py', 'lake_calibration.py',
             'lake_constrained_calibration.py', 'warmup_workflow.py')
    return {'model': model_identity(), 'algorithm': {n: sha_file(ROOT/'backend'/n) for n in names}}


def _path(db, identifier):
    if not isinstance(identifier,str) or len(identifier)!=64 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('率定配置摘要无效')
    return within(db.workspace/'calibration-configurations',identifier+'.json')


def _read(db, identifier):
    record=read_json(_path(db,identifier))
    if not record or digest(record['content'])!=identifier:
        raise ValueError('率定配置不存在或摘要不符')
    return record['content']


def configuration(db, scheme_id):
    from .research_schemes import _scheme
    from .two_stage_calibration import prepare
    from .jobs import grid_observation_snapshot
    from .research_schemes import resolved_initialization
    from .forcing_latest import provenance_summary
    row=_scheme(db,scheme_id)
    value=row['payload']
    if value.get('inputBinding')!='frozen':
        return {'schemeId':scheme_id,'configurationSHA256':None,'form':deepcopy(value),
                'message':'保存为绑定驱动的新修订后运行完整率定'}
    key='calibration-configuration:'+scheme_id
    identifier=db.setting(key)
    if identifier:
        content=_read(db,identifier)
    else:
        request=prepare(db,{'researchSchemeId':scheme_id, 'end':value.get('simulationEnd')})
        request['_observations']=grid_observation_snapshot(db)
        request['_modelIdentity']=implementation()['model']
        initial,first=resolved_initialization(request,request['_observations'],'two-stage-calibration')
        request['_fixedInitialization']=initial
        request['earliestCompleteLakeDay']=first
        product=next(r for r in db.items('input_products') if r['id']==request['inputId'])
        request['inputProvenance']=provenance_summary(db,product['manifest'])
        content={'version':1,'implementation':implementation(),'request':request,
                 'inputManifestSHA256':digest(product['manifest'])}
        identifier=digest(content)
        path=_path(db,identifier)
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():write_json(path,{'content':content})
        db.set_setting(key,identifier)
    request=content['request']
    return {'schemeId':scheme_id,'configurationSHA256':identifier,'revision':value.get('revision',1),
            'form':deepcopy(value), 'initialParameters':deepcopy(request['parameters']),
            'initialization':deepcopy(request['_fixedInitialization']),
            'inputId':request['inputId'],'start':request['start'],'end':request['end'],
            'observationSHA256':digest(request['_observations']),
            'implementation':content['implementation'],
            'matchesCurrentImplementation':content['implementation']==implementation()}


def prepare_request(db, payload):
    allowed={'researchSchemeId','calibrationConfigurationSHA256','projectId','contextKey','name',
             'clientRequestId','adoptOnSuccess'}
    if set(payload)-allowed:
        raise ValueError('请先保存变更设置，再按方案配置开始率定')
    scheme_id=payload['researchSchemeId']
    identifier=payload['calibrationConfigurationSHA256']
    if db.setting('calibration-configuration:'+scheme_id)!=identifier:
        raise ValueError('页面配置与方案修订不一致，请刷新配置')
    content=_read(db,identifier)
    if content['implementation']!=implementation():
        raise ValueError('率定算法版本已更新，请保存新的方案修订')
    request=deepcopy(content['request'])
    product=next((r for r in db.items('input_products') if r['id']==request['inputId']),None)
    if not product or digest(product['manifest'])!=content['inputManifestSHA256']:
        raise ValueError('方案绑定的驱动版本缺失或摘要不符')
    request.update({k:payload[k] for k in ('projectId','contextKey','name','clientRequestId','adoptOnSuccess') if k in payload})
    request['researchSchemeId']=scheme_id
    request['calibrationConfigurationSHA256']=identifier
    request['_configurationBound']=True
    return request


def inherit(db, source_scheme, adopted_scheme):
    """Adopting final parameters does not change the executable initial conditions."""
    identifier=db.setting('calibration-configuration:'+source_scheme)
    if identifier:
        _read(db,identifier)
        db.set_setting('calibration-configuration:'+adopted_scheme,identifier)
