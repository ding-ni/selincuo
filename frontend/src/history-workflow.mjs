import {runIssue} from './workflow.mjs'

export function readHistory(storage){try{const v=JSON.parse(storage.getItem('selinco:history:v1'));return v&&typeof v==='object'&&!Array.isArray(v)?v:{}}catch{return {}}}
export function historyDefaults(boot,draft={}){
 const scheme=(boot.operationalSchemes||[]).find(s=>s.id===draft.schemeId)||(boot.operationalSchemes||[])[0]
 if(!scheme)return {name:'',...draft}
 const preferred=draft.inputId||boot.defaultResultInputId
 const source=boot.inputs.find(i=>i.id===preferred&&i.compatible!==false&&schemeInputCompatible(scheme,i.manifest)&&i.manifest.start<=scheme.initialization.origin&&i.manifest.end>='2025-01-01')
 const end=source?source.manifest.end.slice(0,10)+'T00:00':scheme.end?.slice(0,16)||''
 const start=draft.start>='2024-01-01T00:00'&&draft.start<end?draft.start:scheme.start.slice(0,16)
 return {name:'',...draft,schemeId:scheme.id,schemeRevision:scheme.revision,inputId:source?.id||scheme.inputId,
         start,end:draft.end>start&&draft.end<=end?draft.end:end}
}
export function historyIssue(run,boot,active=false){
 const scheme=boot.operationalSchemes?.find(s=>s.id===run.schemeId)
 if(!scheme)return '内置方案尚未加载，请刷新页面。'
 if(!scheme.available)return scheme.message
 if(!schemeInputCompatible(scheme,boot.inputs.find(i=>i.id===run.inputId)?.manifest||{}))return '请采用内置方案对应的标准驱动。'
 const issue=runIssue(run,boot.inputs,'simulation',active)
 if(issue)return issue
 if(run.start<'2024-01-01T00:00'||run.start>=run.end||![run.start,run.end].every(t=>t.slice(11,16)==='00:00'))return '请选择2024年起的完整历史日期。'
 return ''
}
export function schemeInputCompatible(scheme,manifest){return !scheme?.inputRequirement?.transformationSHA256||scheme.inputRequirement.transformationSHA256===manifest.transformationSHA256}
export function historyRequest(run,clientRequestId){
 return {kind:'simulation',schemeId:run.schemeId,schemeRevision:run.schemeRevision,
         inputId:run.inputId,start:run.start,end:run.end,name:run.name||'',clientRequestId}
}
export function shiftCalendarDay(date,days){return new Date(Date.parse(date.slice(0,10)+'T00:00:00Z')+days*86400000).toISOString().slice(0,10)}
