import {shiftCalendarDay} from './history-workflow.mjs'

// The task centre also contains data imports, downloads and exports. Only
// model-result tasks belong in the result catalogue and its recycle bin.
export function isResultRecord(task){
 if(task?.builtin)return true
 if(['simulation','continuation','trial','calibration','two-stage-calibration','calibration-replay'].includes(task?.kind))return true
 return ['calibration-workflow','sensitivity'].includes(task?.kind)&&task.status!=='succeeded'
}

export function resultTypeName(task,homeId=''){
 if(task?.id===homeId)return '当前最佳 · 连续模拟'
 return ({simulation:'历史模拟',continuation:'续算',trial:'试算',calibration:'率定',
  'two-stage-calibration':'两阶段率定','calibration-replay':'率定复现','calibration-workflow':'率定',
  sensitivity:'敏感性分析'})[task?.kind]||'模拟成果'
}

export function calculationProcess(task){
 const result=task?.result||{},request=task?.request||{},calibration=result.calibration||{},search=request.search||{}
 const selected=Array.isArray(search.keys)?new Set(search.keys):null,mode=request.twoStage?.mode
 const phase=key=>key==='lakeCoefficient'&&(['both','lake'].includes(mode)||selected?.has(key))?'lake':
  !selected?'unknown':mode!=='lake'&&selected.has(key)?'flow':'fixed'
 return {
  modelFamily:result.modelFamily,
  start:result.start,
  end:result.end,
  warmupStart:request.periods?.warmupStart||request.start,
  inputId:result.inputId||request.inputId,
  spatialVersion:result.spatialVersion,
  seeds:request.twoStage?.seeds||search.seeds||[],
  evaluations:calibration.evaluations,
  converged:calibration.converged===true,
  stages:calibration.stages||[],
  parameters:Object.entries(search.bounds||{}).map(([key,bounds])=>({
   key,bounds,phase:phase(key),initial:request.parameters?.[key],value:result.parameters?.[key]
  }))
 }
}

export function resultDisplayPeriod(task,selectedResult){
 const coverage=selectedResult?.id===task.id?selectedResult.result?.displayCoverage:null
 const start=coverage?.start||task.request?.displayStart||task.request?.start||task.start
 const end=coverage?.endExclusive||task.request?.end||task.end
 if(!start||!end)return task.period||'查看成果详情'
 const isExclusiveMidnight=(coverage?.endExclusive&&end.length<=10)||end.slice(11,16)==='00:00'
 return start.slice(0,10)+' — '+(isExclusiveMidnight?shiftCalendarDay(end,-1):end.slice(0,10))
}

export function displayCoverageDates(coverage){
 const start=coverage?.start?.slice(0,10)||''
 const exclusive=coverage?.endExclusive
 const end=coverage?.end?.slice(0,10)||(exclusive?(exclusive.length<=10||exclusive.slice(11,16)==='00:00'?shiftCalendarDay(exclusive,-1):exclusive.slice(0,10)):'')
 return {start,end}
}

export function displayAnnualMetrics(annual={},coverage){
 const {start,end}=displayCoverageDates(coverage)
 return Object.fromEntries(Object.entries(annual).filter(([year])=>!/^\d{4}$/.test(year)||(!start||year>=start.slice(0,4))&&(!end||year<=end.slice(0,4))))
}
