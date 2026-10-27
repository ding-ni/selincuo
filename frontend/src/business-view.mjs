export function businessTasks(tasks=[],maintenance=false){
 return maintenance?tasks:tasks.filter(t=>['simulation','continuation'].includes(t.kind))
}
export const qualityLabel=value=>({valid:'正常',qualified:'附注',missing:'缺测',ice_or_stopped:'停测',zero_needs_review:'待核查'}[value]||value||'—')
export const stepLabel=value=>({day:'日值',hour:'小时',irregular:'时段记录'}[value]||value||'—')
export const variableDefinitions=[
 {id:'level',label:'水位',unit:'m'}, {id:'flow',label:'流量',unit:'m³/s'},
 {id:'precipitation',label:'降水',unit:'mm'}, {id:'temperature',label:'气温',unit:'℃'},
 {id:'evaporation',label:'蒸发',unit:'mm'}, {id:'area',label:'湖面面积',unit:'km²'},
 {id:'snowWaterEquivalent',label:'雪水当量',unit:'mm'}
]
