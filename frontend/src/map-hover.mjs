const labels={precipitation:'日降水',temperature:'日平均气温',evaporation:'日蒸发需求',snow:'日末雪水当量',level:'水位',flow:'流量'}
export function mapHover(kind,item,{frame,snow,spatial}={}){
 const variable=kind==='snow'?'snow':frame?.variable
 const unit=kind==='snow'?'mm':frame?.unit||''
 const found=kind==='unit'?(frame?.unitValues||[]).find(v=>v.id===item.id):kind==='snow'?(snow?.units||[]).find(v=>v.id===item.id):item
 const value=found?.value
 const rows=[{label:labels[variable]||'数值',value:value==null?'缺测':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:3})+' '+unit}]
 if(kind==='grid'&&['precipitation','temperature'].includes(frame?.variable)){
  const gridIndex=spatial?.grids?.findIndex(g=>g.id===item.id)
  const units=new Set((spatial?.aggregation||[]).filter(g=>g.members.some(m=>m.gridIndex===gridIndex)).map(g=>g.unitId))
  const applied=(frame.unitValues||[]).filter(u=>units.has(u.id))
  const appliedLabel=frame.variable==='temperature'?'高程订正后单元采用值': '降水订正后单元采用值'
  for(const u of applied.slice(0,4))rows.push({label:appliedLabel+' · '+u.id.replace('AGG-','').slice(0,8),value:u.value==null?'缺测':Number(u.value).toFixed(3)+' '+unit})
  if(applied.length>4)rows.push({label:'关联单元',value:'共 '+applied.length+' 个，点击查看'})
 }
 return {title:item.name||item.id,time:kind==='snow'?snow?.day:frame?.day,rows,
  source:kind==='snow'?'模型日末状态':kind==='grid'?'再分析气象格点原值':frame?.variable==='precipitation'?'模型订正后单元采用值':frame?.unitValuesRepresent==='model-elevation-corrected'?'模型高程订正后单元采用值':'气象单元面积均值',
  note:kind==='grid'&&variable==='temperature'?'格点原值未作高程订正；关联行显示模型单元采用值':undefined}
}
