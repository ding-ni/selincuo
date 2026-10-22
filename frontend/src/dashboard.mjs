export const rivers=[{id:'zajia',name:'扎加藏布',short:'扎加'},{id:'zagen',name:'扎根藏布',short:'扎根'},{id:'boqu',name:'波曲藏布',short:'波曲'},{id:'ali',name:'阿里藏布',short:'阿里'},{id:'fringe',name:'湖周补充区',short:'湖周'}]
export const layers=[{id:'precipitation',label:'日降水',unit:'mm/d',colors:['#e1f4d1','#b4df86','#78be6d','#399ca1','#3772b3','#745b9b'],stops:[0,.1,2,5,10,25]},
 {id:'temperature',label:'日均气温',unit:'℃',colors:['#486caa','#77a7c1','#afcfcb','#e6d99e','#dcb06c','#bd7554'],stops:[-30,-15,-5,0,5,15]},
 {id:'evaporation',label:'日蒸发能力',unit:'mm/d',colors:['#eef1d1','#dfdf9e','#ccc37a','#c6a15b','#b67a40','#96562f'],stops:[0,1,2,4,6,8]},
 {id:'snow',label:'日末雪水当量',unit:'mm',colors:['#e8edf0','#c4dcea','#91bfd9','#6593be','#536c9c','#534e80'],stops:[0,1,10,30,100,300]},
 {id:'basin',label:'流域分区',unit:'',colors:[],stops:[]}]
export function colorFor(value,layer){if(value==null||!Number.isFinite(value))return 'transparent';let i=0;for(let j=0;j<layer.stops.length;j++)if(value>=layer.stops[j])i=j;return layer.colors[i]||'transparent'}
export function exactValue(station,day){return station?.daily?.find(r=>r.date===day)?.value??null}
export function timelineDates(mode,monitor,result){
 if(mode==='model')return (result?.daily||[]).map(r=>r.date.slice(0,10))
 const main=monitor?.stations?.find(s=>s.id==='SLC')?.daily||[]
 if(!main.length)return []
 const start=Date.parse(main[0].date+'T00:00:00Z'),end=Date.parse(main.at(-1).date+'T00:00:00Z'),dates=[]
 for(let t=start;t<=end;t+=86400000)dates.push(new Date(t).toISOString().slice(0,10))
 return dates
}
export function balanceTerms(row){return [{key:'lakeInflowM3',label:'入湖来水',sign:1},{key:'lakePrecipitationM3',label:'湖面降水',sign:1},{key:'lakeEvaporationM3',label:'湖面蒸发',sign:-1},{key:'lakeDeltaVolumeM3',label:'蓄水变化',sign:1}].map(t=>({...t,value:row?.[t.key]==null?null:row[t.key]/1e6}))}
export function defaultReplayDay(dates,result){const latest=(result?.daily||[]).filter(r=>r.lakeLevel!=null&&r.observedLevel!=null).at(-1);return latest?.date.slice(0,10)||dates.at(-1)||''}
