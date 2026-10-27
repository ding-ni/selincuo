export function scaledLayer(base,values=[],range=null,custom=null){
 const valid=values.filter(v=>v!=null&&Number.isFinite(v))
 let lo=custom?.[0]??range?.minimum??(valid.length?Math.min(...valid):0),hi=custom?.[1]??range?.maximum??(valid.length?Math.max(...valid):1)
 if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo>hi)return base
 if(base.id==='FR'){lo=0;hi=1}
 else if(!custom&&(['temperature','balanceError','lakeFlux','volumeChange','continuousLakeVolumeChange'].includes(base.id)||(lo<0&&hi>0))){hi=Math.max(Math.abs(lo),Math.abs(hi),1e-9);lo=-hi}
 else if(!custom&&['precipitation','evaporation','snow'].includes(base.id))lo=0
 if(lo===hi)hi=lo+Math.max(1,Math.abs(lo)*.01)
 return {...base,stops:base.colors.map((_,i)=>Number((lo+(hi-lo)*i/Math.max(1,base.colors.length-1)).toPrecision(4)))}
}
