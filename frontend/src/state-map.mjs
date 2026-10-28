const regionNames={duja:'杜加站上游',zajia_rest:'扎加下游',zajia:'扎加藏布',zagen:'扎根藏布',boqu:'波曲藏布',ali:'阿里藏布',fringe:'湖周直接入湖区',unresolved:'地形待判定范围',external:'独立排水范围',total:'全部入湖范围',lake:'色林错'}
export const regionName=id=>regionNames[id]||id
export function unitName(unit){return `${regionName(unit.partition)} · ${unit.elevationBandMin}—${unit.elevationBandMax} m · ${unit.kind==='glacier'?'冰川':'非冰川'}`}
export function objectUnitIds(units=[],object){
 if(!object)return []
 if(object.scope==='unit')return units.filter(u=>u.id===object.id).map(u=>u.id)
 if(object.scope==='partition')return units.filter(u=>u.partition===object.id).map(u=>u.id)
 if(object.scope==='river')return units.filter(u=>object.id==='total'?['duja','zajia_rest','zagen','boqu','ali','fringe'].includes(u.partition):object.id==='duja'?u.partition==='duja':object.id==='zajia'?['duja','zajia_rest'].includes(u.partition):u.basin===object.id||u.partition===object.id).map(u=>u.id)
 return []
}
export function selectedGeometry(features=[],lake=[],units=[],object){
 if(object?.scope==='lake')return lake
 const ids=new Set(objectUnitIds(units,object))
 return features.filter(f=>ids.has(f.properties.id))
}
export function geometryBounds(features=[]){
 let left=Infinity,bottom=Infinity,right=-Infinity,top=-Infinity
 const visit=value=>{if(typeof value?.[0]==='number'){left=Math.min(left,value[0]);right=Math.max(right,value[0]);bottom=Math.min(bottom,value[1]);top=Math.max(top,value[1])}else if(Array.isArray(value))value.forEach(visit)}
 for(const f of features)visit(f.geometry?.coordinates)
 return Number.isFinite(left)?[left,bottom,right,top]:null
}
