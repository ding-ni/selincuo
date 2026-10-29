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
// Inside the original Ali polygon; also keeps the name clear of the lake on
// the small overview. The remaining names use their presentation anchors.
export const geographicAnchorOverrides=Object.freeze({ali:[88.61037766717594,31.30629809582074]})
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

// Geographic labels stay at their surveyed interior anchors. If neighbouring
// names touch at small viewports, wrap the names instead of moving them out.
export function placeGeographicLabels(labels){
 const result=labels.map(label=>{
  const text=Array.from(label.name),fontSize=label.fontSize||13,lines=[text.join('')]
  return {...label,fontSize,lines,box:{x:label.x-(text.length*fontSize+8)/2,y:label.y-11,w:text.length*fontSize+8,h:22},cx:label.x,cy:label.y}
 })
 const overlaps=(a,b)=>a.x<b.x+b.w+2&&a.x+a.w+2>b.x&&a.y<b.y+b.h+2&&a.y+a.h+2>b.y
 const wrap=label=>{
  const chars=Array.from(label.name);if(label.kind==='lake'||chars.length<4||label.lines.length>1)return false
  const half=Math.ceil(chars.length/2);label.lines=[chars.slice(0,half).join(''),chars.slice(half).join('')]
  const w=Math.max(...label.lines.map(line=>Array.from(line).length))*label.fontSize+8
  label.box={x:label.x-w/2,y:label.y-16,w,h:32};return true
 }
 for(let pass=0;pass<2;pass++)for(let i=0;i<result.length;i++)for(let j=i+1;j<result.length;j++){
  if(overlaps(result[i].box,result[j].box)){wrap(result[i]);wrap(result[j])}
 }
 return result
}

// Intersect the line from a marker to its label with the label rectangle.
export function leaderEnd(anchor,box){
 const cx=box.x+box.w/2,cy=box.y+box.h/2,dx=cx-anchor[0],dy=cy-anchor[1]
 if(!dx&&!dy)return [cx,cy]
 const scale=Math.min(dx?box.w/(2*Math.abs(dx)):Infinity,dy?box.h/(2*Math.abs(dy)):Infinity)
 return [cx-dx*scale,cy-dy*scale]
}
