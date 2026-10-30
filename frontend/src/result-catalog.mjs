import {shiftCalendarDay} from './history-workflow.mjs'

// The task centre also contains data imports, downloads and exports. Only
// model-result tasks belong in the result catalogue and its recycle bin.
export function isResultRecord(task){
 if(task?.builtin)return true
 if(['simulation','continuation','trial','calibration','two-stage-calibration','calibration-replay'].includes(task?.kind))return true
 return ['calibration-workflow','sensitivity'].includes(task?.kind)&&task.status!=='succeeded'
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
