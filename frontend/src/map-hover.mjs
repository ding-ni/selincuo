const labels={precipitation:'日降水',temperature:'日平均气温',evaporation:'日蒸发需求',snow:'日末雪水当量',level:'水位',flow:'流量'}
export function mapHover(kind,item,{frame,snow,spatial}={}){
 const variable=kind==='snow'?'snow':frame?.variable
 const unit=kind==='snow'?'mm':frame?.unit||''
 const found=kind==='unit'?(frame?.unitValues||[]).find(v=>v.id===item.id):kind==='snow'?(snow?.units||[]).find(v=>v.id===item.id):item
 const value=found?.value
 const rows=[{label:labels[variable]||'数值',value:value==null?'缺测':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:3})+' '+unit}]
 // Grid hover: only show the grid-point raw value — no internal unit IDs, no mapping breakdown.
 // Unit hover: show aggregated model value only, no ID fragments.
 const source=kind==='snow'?'模型日末状态':kind==='grid'?'再分析格点原值':frame?.variable==='precipitation'?'模型订正后采用值':frame?.unitValuesRepresent==='model-elevation-corrected'?'模型高程订正后采用值':'气象单元均值'
 // Build a clean title: use name if available; for grid points format as coordinates.
 const lat=item.lat??item.latitude,lon=item.lon??item.longitude
 const gridTitle=lat!=null&&lon!=null?`格点 ${Number(lat).toFixed(1)}°N ${Number(lon).toFixed(1)}°E`:'气象格点'
 const title=item.name||(kind==='grid'?gridTitle:undefined)||item.id||'气象格点'
 return {title,time:kind==='snow'?snow?.day:frame?.day,rows,source,
  note:kind==='grid'&&variable==='temperature'?'格点气温未经高程订正，模型单元已按高程分区订正':undefined}
}
