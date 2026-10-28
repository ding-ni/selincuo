// Coordinates are local equirectangular kilometres at the control-domain latitude.
export function projector(bounds) {
 const lat=(bounds[1]+bounds[3])/2, k=Math.cos(lat*Math.PI/180)*111.195
 return ([lon,y])=>[(lon-bounds[0])*k,(bounds[3]-y)*111.195]
}
export function fitViewport(bounds,size,padding=32) {
 const project=projector(bounds),[w,h]=project([bounds[2],bounds[1]])
 const scale=Math.min(Math.max(1,size.width-2*padding)/w,Math.max(1,size.height-2*padding)/h)
 return {scale,x:(size.width-w*scale)/2,y:(size.height-h*scale)/2,width:w*scale,height:h*scale}
}
// A parent map may only supply visual detail when the aggregated manifest
// explicitly records that same parent's display-layer digest.
export function detailMapUrl(base,manifest){
 const parent=manifest?.parentSpatialVersion,hash=manifest?.sourceAssetsSHA256?.['display.geojson']
 if(manifest?.unitStrategy==='partition-elevation-surface-area-weighted-v1'&&/^[A-Za-z0-9_-]+$/.test(parent||'')&&/^[a-f0-9]{64}$/i.test(hash||''))return '/spatial/versions/'+parent+'/map.geojson'
 if(!parent&&/^(\/spatial\/|\/spatial\/versions\/[A-Za-z0-9_-]+\/)$/.test(base))return base+'map.geojson'
 return null
}
export function placeLabels(labels,size,obstacles=[]) {
 const boxes=[...obstacles], result=[]
 const overlaps=(a,b)=>a.x<b.x+b.w+5&&a.x+a.w+5>b.x&&a.y<b.y+b.h+5&&a.y+a.h+5>b.y
 // Priority and ID make placement independent of API or feature iteration order.
 const ordered=[...labels].sort((a,b)=>(b.priority??0)-(a.priority??0)||String(a.id||a.kind||'').localeCompare(String(b.id||b.kind||'')))
 for(const label of ordered){
  const w=label.width||112,h=label.height||34
  let chosen
  const preferred=Array.isArray(label.preferredOffset)?label.preferredOffset:[0,0]
  const candidates=[preferred,...(preferred[0]||preferred[1]?[[0,0]]:[])]
  for(const radius of [35,65,100,145,205,275])for(let i=0;i<8;i++)candidates.push([Math.cos(i*Math.PI/4)*radius,Math.sin(i*Math.PI/4)*radius])
  for(const [dx,dy] of candidates){
   const box={x:Math.max(8,Math.min(size.width-w-8,label.x-w/2+dx)),y:Math.max(8,Math.min(size.height-h-8,label.y-h/2+dy)),w,h}
   if(!chosen)chosen=box
   if(!boxes.some(b=>overlaps(box,b))){chosen=box;break}
  }
  boxes.push(chosen);result.push({...label,box:chosen,cx:chosen.x+w/2,cy:chosen.y+h/2})
 }
 return result
}
