const memories=new Map()
const validZoom=z=>Array.isArray(z)&&z.length===2&&z.every(Number.isFinite)&&z[0]>=0&&z[1]<=100&&z[0]<=z[1]
export function chartState(key){if(!memories.has(key)){let saved;try{saved=JSON.parse(globalThis.localStorage?.getItem('selinco:chart:'+key)||'null')}catch{}memories.set(key,{selected:saved?.selected&&typeof saved.selected==='object'?saved.selected:{},zoom:validZoom(saved?.zoom)?saved.zoom:[0,100],yRange:Array.isArray(saved?.yRange)&&saved.yRange.length===2&&saved.yRange.every(Number.isFinite)&&saved.yRange[0]<saved.yRange[1]?saved.yRange:null});if(memories.size>100)memories.delete(memories.keys().next().value)}return memories.get(key)}
export function saveChartState(key,state){try{globalThis.localStorage?.setItem('selinco:chart:'+key,JSON.stringify(state))}catch{}}
export function seriesType(variable){return ['precipitation','evaporation'].includes(variable)?'bar':'line'}
export function rowKey(row,hourly=false){return String(row?.date??row?.time??'').slice(0,hourly?16:10)}
export function isolatedObservation(values,index){return values[index]!=null&&Number.isFinite(Number(values[index]))&&values[index-1]==null&&values[index+1]==null}
export function dateIndex(rows,day,hourly=false){
 if(!day)return -1
 return rows.findIndex(row=>{const key=rowKey(row,hourly);return key===day||(hourly&&day.length===10&&key.slice(0,10)===day)})
}
export function rangeContains(index,length,zoom){
 if(index<0||!length)return false
 if(length===1)return true
 const point=index/(length-1)*100
 return point>=zoom[0]-1e-6&&point<=zoom[1]+1e-6
}
export function centeredZoom(index,length,zoom){
 if(index<0||length<2)return [...zoom]
 const width=Math.min(100,Math.max(0,zoom[1]-zoom[0])),point=index/(length-1)*100
 const start=Math.min(100-width,Math.max(0,point-width/2))
 return [start,start+width]
}
