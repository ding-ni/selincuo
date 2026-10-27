const memories=new Map()
export function chartState(key){if(!memories.has(key)){let saved;try{saved=JSON.parse(globalThis.localStorage?.getItem('selinco:chart:'+key)||'null')}catch{}const zoom=saved?.zoom;memories.set(key,{selected:saved?.selected&&typeof saved.selected==='object'?saved.selected:{},zoom:Array.isArray(zoom)&&zoom.length===2&&zoom.every(Number.isFinite)&&zoom[0]>=0&&zoom[1]<=100&&zoom[0]<=zoom[1]?zoom:[0,100]});if(memories.size>100)memories.delete(memories.keys().next().value)}return memories.get(key)}
export function saveChartState(key,state){try{globalThis.localStorage?.setItem('selinco:chart:'+key,JSON.stringify(state))}catch{}}
export function seriesType(variable){return ['precipitation','evaporation'].includes(variable)?'bar':'line'}
