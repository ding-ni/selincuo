// Tiny SVG sparkline helpers for map hover cards.
export function linePath(values,{width=116,height=30,pad=3}={}){
 const n=Math.max(1,values.length-1),points=values.map((v,i)=>[i,v]).filter(([,v])=>v!=null&&Number.isFinite(Number(v)))
 if(points.length<2)return null
 const ys=points.map(p=>Number(p[1])),lo=Math.min(...ys),hi=Math.max(...ys),span=hi-lo||Math.max(1e-6,Math.abs(hi)*1e-4)
 const xy=points.map(([i,v])=>[pad+i/n*(width-2*pad),height-pad-(Number(v)-lo)/span*(height-2*pad)])
 const line=xy.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join('')
 const area=line+`L${xy.at(-1)[0].toFixed(1)} ${height}L${xy[0][0].toFixed(1)} ${height}Z`
 return {line,area,last:xy.at(-1),min:lo,max:hi}
}
export function barRects(values,{width=116,height=30}={}){
 const finite=values.map(Number).filter(Number.isFinite),hi=Math.max(1e-9,...finite),w=width/Math.max(1,values.length)
 return values.map((v,i)=>{const value=Number(v),ok=v!=null&&Number.isFinite(value),h=ok&&value>0?Math.max(2,value/hi*(height-3)):ok?1:0
  return {x:i*w+.8,width:Math.max(1,w-1.6),y:height-h,height:h,missing:!ok}})
}
export function formatValue(variable,value){
 if(value==null||!Number.isFinite(Number(value)))return '—'
 const digits={level:2,flow:1,precipitation:1,temperature:1,evaporation:1,snow:1}[variable]??2
 return Number(value).toLocaleString('zh-CN',{useGrouping:variable!=='level',minimumFractionDigits:variable==='level'?2:0,maximumFractionDigits:digits})
}
