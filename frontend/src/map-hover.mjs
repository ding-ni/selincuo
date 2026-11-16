const labels={precipitation:'日降水',temperature:'日平均气温',evaporation:'日蒸发能力',snow:'日末雪水当量',level:'水位',flow:'流量'}
const units={precipitation:'mm',temperature:'℃',evaporation:'mm',snow:'mm'}
const partitions={duja:'杜加站以上',zajia_rest:'扎加藏布（杜加以下）',zagen:'扎根藏布',boqu:'波曲',ali:'阿里藏布',fringe:'湖周直接入湖区'}
// Business summary for a weather grid cell, model unit or snow unit; no internal identifiers.
export function mapHover(kind,item,{frame,snow}={}){
 const variable=kind==='snow'?'snow':frame?.variable
 const found=kind==='unit'?(frame?.unitValues||[]).find(v=>v.id===item.id):kind==='snow'?(snow?.units||[]).find(v=>v.id===item.id):item
 const lat=item.lat??item.latitude??item.centerLat,lon=item.lon??item.longitude??item.centerLon
 const place=lat!=null&&lon!=null?`${Number(lat).toFixed(2)}°N  ${Number(lon).toFixed(2)}°E`:''
 const band=item.elevationBandMin!=null&&item.elevationBandMax!=null?`${item.elevationBandMin}–${item.elevationBandMax} m`:''
 const title=kind==='grid'?'气象格点':(partitions[item.partition]||'计算单元')+(item.kind==='glacier'?' · 冰川':'')
 const tag=kind==='snow'?'模型状态':kind==='grid'?'格点值':'单元采用值'
 return {mode:'weather',title,subtitle:[place,band].filter(Boolean).join(' · '),tag,time:kind==='snow'?snow?.day:frame?.day,
  label:labels[variable]||'数值',unit:units[variable]||frame?.unit||'',value:found?.value??null}
}
