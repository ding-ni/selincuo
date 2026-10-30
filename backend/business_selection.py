"""Persist the selected working scheme without changing scientific releases."""
from copy import deepcopy
import math

from .common import canonical, now

CURRENT_SETTING='business-adopted-scheme-v1'


def current(db):
    return db.setting(CURRENT_SETTING) or {} if hasattr(db,'setting') else {}


def automatic_adoption_decision(db, result):
    """Apply the agreed flow/lake improvement rule to automatic adoption."""
    workflow = result.get('calibrationWorkflow') or {}
    if workflow.get('selectionMode') != 'flow-constrained-lake':
        return {'eligible': True, 'method': 'existing-workflow'}
    selection = workflow.get('selection') or {}
    comparison = selection.get('adoptionComparison') or {}
    baseline = comparison.get('baselineTaskId')
    if not baseline or current(db).get('resultId') != baseline:
        return {'eligible': False, 'message': '当前采用已变更，本次成果已保存。',
                'comparison': comparison}
    limits = selection.get('flowConstraints') or {'minNse': .70, 'maxAbsPbias': 5.0}
    nse, pbias = comparison.get('nse'), comparison.get('pbias')
    flow_ok = (isinstance(nse, (int, float)) and isinstance(pbias, (int, float)) and
               math.isfinite(nse) and math.isfinite(pbias) and
               nse >= limits['minNse'] and abs(pbias) <= limits['maxAbsPbias'])
    eligible = bool(flow_ok and comparison.get('eligible') and
                    comparison.get('lakeRmseImproved') and comparison.get('lakeScoreImproved'))
    return {'eligible': eligible, 'comparison': comparison,
            'message': '流量与湖位均满足采用条件。' if eligible else '本次成果已保存，当前采用继续保留。'}


def purpose(db, row):
    family=row['payload'].get('familyId') or row['id']
    return db.setting('scheme-purpose:'+family,row['payload'].get('purpose','business'))


def visible_tasks(db, rows, include_experiments=False):
    if include_experiments:
        return rows
    hidden=set(db.setting('development-task-ids',[]))
    development_schemes={row['id'] for row in db.items('schemes') if purpose(db,row)=='development'}
    chosen=current(db).get('resultId')
    return [row for row in rows if row['id']==chosen or row.get('userSaved') or row.get('user_saved') or
            (row.get('kind')=='calibration-replay' and row.get('status') in ('queued','running','stopping')) or
            (row.get('kind')!='calibration-replay' and row['id'] not in hidden and row.get('request',{}).get('researchSchemeId') not in development_schemes
             and row.get('request',{}).get('purpose')!='development')]


def adopt_result(db, task_id, name=None):
    from .research_schemes import _scheme, save, save_parameters_from_task
    from .business_context import builtin_result_available
    task=db.task(task_id)
    result=task.get('result') or {}
    if task['status']!='succeeded' or not builtin_result_available(db,task_id):
        raise ValueError('请选择已完成并有完整逐时资料的成果')
    if current(db).get('resultId')==task_id:
        return current(db)
    source_id=task['request'].get('researchSchemeId')
    if not source_id:
        raise ValueError('该成果没有可采用的业务方案')
    source=_scheme(db,source_id)
    payload=deepcopy(source['payload'])
    payload.update(parameters=deepcopy(result['parameters']),partitionParameters=deepcopy(result.get('partitionParameters',{})),
                   parameterTransfer=deepcopy(result['parameterTransfer']),purpose='business')
    payload['name']=name or payload['name']
    # The workflow output may have different run/display dates from its flow
    # training period. Store these as the default history window.
    payload['historyPeriod']={'start':result.get('displayCoverage',{}).get('start','2021-04-23T00:00:00'),
                              'end':result.get('displayCoverage',{}).get('endExclusive',result['end'])}
    parameter=save_parameters_from_task(db,task_id,payload['name']+' · 参数')
    payload['adoptedParameterId']=parameter['id']
    payload['parameterSources']={**deepcopy(result.get('parameterSources') or source['payload'].get('parameterSources',{})),
                                 'sourceTaskId':task_id}
    if result.get('calibrationWorkflow',{}).get('lake'):
        payload['lakeParameterSource']={'method':'conditional-lake-level-calibration','coefficient':result['parameters']['lakeCoefficient'],
                                       'sourceTaskId':task_id,'calibratedToLakeLevel':True}
    # Saving from an older source revision starts a new family if necessary;
    # existing user edits are never rolled back by an asynchronous completion.
    family=source['payload'].get('familyId')
    newer=any(r['payload'].get('familyId')==family and r['payload'].get('revision',1)>source['payload'].get('revision',1)
              for r in db.items('schemes'))
    saved=save(db,payload,None if newer else source_id)
    selection={'schemeId':saved['id'],'familyId':saved['payload']['familyId'],
               'parameterId':parameter['id'],'resultId':task_id,'name':payload['name'],
               'adoptedAt':now(),'spatialVersion':result['spatialVersion'],'inputId':result['inputId']}
    with db.connect() as connection:
        connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(CURRENT_SETTING,canonical(selection)))
        connection.execute('UPDATE tasks SET favorite=1,user_saved=1,collection=? WHERE id=?',('采用成果',task_id))
        connection.execute('INSERT INTO audit(at,action,payload) VALUES(?,?,?)',(now(),'business.adopt',canonical(selection)))
    return selection
