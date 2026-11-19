// Offline overflow display assets (built by scripts/构建漫溢展示资料.py) and shared water logic.
export const OVERFLOW_BASE='/spatial/'+encodeURIComponent('漫溢展示')+'/'
export const DEPTH_CLASSES=[[.5,'#f2d35b','0–0.5 m'],[1.5,'#eb9a2f','0.5–1.5 m'],[3,'#d95a24','1.5–3 m'],[Infinity,'#a8231d','> 3 m']]
let cache=null
async function json(url){const r=await fetch(url);if(!r.ok)throw Error('漫溢展示资料缺失，请先运行构建脚本');return r.json()}
async function buffer(url){const r=await fetch(url);if(!r.ok)throw Error('漫溢展示资料缺失，请先运行构建脚本');return r.arrayBuffer()}
export function loadOverflow(){
 if(!cache)cache=(async()=>{
  const meta=await json(OVERFLOW_BASE+'overflow-display.json')
  const [h,d,lakes]=await Promise.all([buffer(OVERFLOW_BASE+meta.east.heights),buffer(OVERFLOW_BASE+meta.east.domain),json(OVERFLOW_BASE+meta.lakes)])
  const raw=new Uint16Array(h),heights=new Float32Array(raw.length)
  for(let i=0;i<raw.length;i++)heights[i]=meta.east.heightOffset+raw[i]*meta.east.heightScale
  return {meta,heights,domain:new Uint8Array(d),lakes,base:OVERFLOW_BASE}
 })().catch(e=>{cache=null;throw e})
 return cache
}
// Lake level (observation datum) to GLO-90 elevation.
export const toDem=(meta,level)=>level-meta.datum.lakeMinusDemM
export const toLake=(meta,dem)=>dem+meta.datum.lakeMinusDemM
function interp(x,xs,ys){if(x<=xs[0])return ys[0];for(let i=1;i<xs.length;i++)if(x<=xs[i]){const f=(x-xs[i-1])/(xs[i]-xs[i-1]||1);return ys[i-1]+f*(ys[i]-ys[i-1])}return ys.at(-1)}
export function bangeRise(meta,volumeKm3){const t=meta.bange.hypsometry;return interp(volumeKm3,t.map(r=>r.volumeKm3),t.map(r=>r.rise))}
export function bangeArea(meta,rise){const t=meta.bange.hypsometry;return interp(rise,t.map(r=>r.rise),t.map(r=>r.areaKm2))}
export function depthColor(d){return (DEPTH_CLASSES.find(c=>d<=c[0])||DEPTH_CLASSES.at(-1))[1]}
const rgb=hex=>[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16))
const DEPTH_RGB=DEPTH_CLASSES.map(c=>[c[0],rgb(c[1])])
/**
 * Per-cell water state on the east grid for one frame.
 * state: 0 land, 1 Siling (today's extent), 2 Siling newly flooded, 3 Bange Co.
 * depth: water depth above ground for newly flooded cells (m).
 */
export function waterState(data,{lake,base,bangeRiseM=0}){
 // state: 0 land, 1 Siling original extent, 2 Siling newly flooded, 3 Bange original extent, 4 Bange newly flooded.
 const {meta,heights,domain}=data,n=heights.length,state=new Uint8Array(n),depth=new Float32Array(n)
 const s=toDem(meta,lake),s0=toDem(meta,base??lake),b0=meta.bange.surfaceDemM,b=b0+Math.max(0,bangeRiseM)
 let sCells=0,bCells=0
 for(let i=0;i<n;i++){const h=heights[i],d=domain[i]
  if(d===1&&h<=s){if(h>s0){state[i]=2;depth[i]=s-h;sCells++}else state[i]=1}
  else if(d===2&&h<=b){if(h>b0+.05){state[i]=4;depth[i]=b-h;bCells++}else state[i]=3}}
 const [dx,dy]=meta.east.pixelMetres,cell=dx*dy/1e6
 return {state,depth,newAreaKm2:sCells*cell,bangeNewAreaKm2:bCells*cell}
}
// Shipped road and settlement geometry for the map extent.
let exposureCache=null
const ROADS=new Set(['motorway','trunk','primary','secondary','tertiary','unclassified','residential','track','service'])
export function loadExposures(meta){
 if(!exposureCache)exposureCache=(async()=>{
  const base='/spatial/'+encodeURIComponent('承灾体')+'/',r=await fetch(base+encodeURIComponent('承灾体资料.json'));if(!r.ok)return {roads:[],settlements:[]}
  const index=await r.json(),eb=meta.overview.bounds,inside=(lon,lat)=>lon>=eb[0]&&lon<=eb[2]&&lat>=eb[1]&&lat<=eb[3]
  const out={roads:[],settlements:[],source:index.source+' · '+index.license}
  const assets=await Promise.all((index.adopted||[]).map(item=>fetch(base+encodeURIComponent(item.file)).then(x=>x.json())))
  for(const g of assets){
   for(const f of g.features||[]){const p=f.properties||{}
    if(f.geometry.type==='LineString'&&ROADS.has(p.class||p.highway))out.roads.push({id:f.id,name:p.name||p.ref||'未命名道路',cls:p.class||p.highway,coords:f.geometry.coordinates})
    else if(f.geometry.type==='Point'&&inside(...f.geometry.coordinates))out.settlements.push({id:f.id,name:p.name||'未命名居民点',cls:p.class||p.place||'',lon:f.geometry.coordinates[0],lat:f.geometry.coordinates[1]})}}
  return out})().catch(()=>({roads:[],settlements:[]}))
 return exposureCache
}
export function scenarioExposures(items=[]){
 const roads=[],settlements=[]
 for(const item of items)for(const [i,f] of item.features.entries()){
  const g=f.geometry,p=f.properties||{},id=item.id+':'+i
  if(g.type==='LineString'||g.type==='MultiLineString'){
   const lines=g.type==='LineString'?[g.coordinates]:g.coordinates
   lines.forEach((coords,k)=>roads.push({id:id+':'+k,name:p.name||p['名称']||p.ref||'未命名道路',cls:p.class||p.highway,coords}))
  }else if(g.type==='Point'){
   settlements.push({id,name:p.name||p['名称']||'未命名对象',lon:g.coordinates[0],lat:g.coordinates[1]})
  }
 }
 return {roads,settlements,source:[...new Set(items.map(item=>item.source).filter(Boolean))].join(' · ')}
}
export function dailyFrameIndex(result,day){
 const start=Date.parse(day+'T00:00:00Z'),end=start+86400000
 const rows=result.rows||[],picks=result.frameRows||rows.map((_,i)=>i)
 // The simulation records time at the end of each hour, including the next midnight.
 return picks.findLastIndex(i=>{
  const ts=rows[i]?.time
  const t=ts?Date.parse(ts.slice(0,19).replace(' ','T')+'Z'):NaN
  return t>start&&t<=end
 })
}
const FLOODED=s=>s===2||s===4
function gridPoint({bounds:[west,south,east,north],width,height},[lon,lat]){return [(lon-west)/(east-west)*width,(north-lat)/(north-south)*height]}
export function cellAt(grid,lon,lat){const [x,y]=gridPoint(grid,[lon,lat]),c=Math.floor(x),r=Math.floor(y);return c>=0&&c<grid.width&&r>=0&&r<grid.height?r*grid.width+c:-1}
export function paintArchivedDepth(water,east,archive){
 const {bounds:[west,south,eastLon,north],width,height}=east
 const [aw,as,ae,an]=archive.bounds,dx=(eastLon-west)/width,dy=(north-south)/height
 const cols=Array.from({length:width},(_,c)=>Math.floor((west+(c+.5)*dx-aw)/(ae-aw)*archive.width))
 for(let r=0;r<height;r++){
  const ar=Math.floor((an-north+(r+.5)*dy)/(an-as)*archive.height)
  if(ar<0||ar>=archive.height)continue
  for(let c=0;c<width;c++){
   const ac=cols[c]
   if(ac<0||ac>=archive.width)continue
   const i=r*width+c,depth=archive.depth[ar*archive.width+ac]
   if(depth>0){water.state[i]=2;water.depth[i]=depth}
  }
 }
 return water
}
export function inEast(meta,lon,lat){return cellAt(meta.east,lon,lat)>=0}
function km(a,b){const k=Math.PI/180,x=(b[0]-a[0])*Math.cos((a[1]+b[1])/2*k)*111.32,y=(b[1]-a[1])*110.95;return Math.hypot(x,y)}
// Road surface = DEM ground + embankment; a segment is closed when water over the surface exceeds closeDepthM.
export const ROAD_RULE={embankmentM:1.0,closeDepthM:0.3}
export const ROAD_STATES=[
 ['corridor','#16e5df','道路沿线受淹'],
 ['overtopped','#ffac22','路面过水'],
 ['closed','#ff3a49','预计断行']
]
export const roadStateLabel=status=>ROAD_STATES.find(row=>row[0]===status)?.[2]
const gridRoads=new WeakMap()
export function roadGridSegments(exposures,grid){
 const key=[grid.width,grid.height,...grid.bounds].join(',')
 let grids=gridRoads.get(exposures)
 if(!grids)gridRoads.set(exposures,grids=new Map())
 if(grids.has(key))return grids.get(key)
 const segments=[]
 for(const road of exposures.roads)for(let i=1;i<road.coords.length;i++){
  const a=road.coords[i-1],b=road.coords[i],p=gridPoint(grid,a),q=gridPoint(grid,b),delta=[q[0]-p[0],q[1]-p[1]]
  let start=0,end=1
  const bounds=[grid.width,grid.height]
  for(let axis=0;axis<2;axis++){
   const d=delta[axis],x=p[axis],limit=bounds[axis]
   if(!d){if(x<0||x>=limit)end=0;continue}
   const t0=-x/d,t1=(limit-x)/d
   start=Math.max(start,Math.min(t0,t1));end=Math.min(end,Math.max(t0,t1))
  }
  if(end-start<=1e-10||(!delta[0]&&!delta[1]))continue
  const cuts=[start,end]
  for(let axis=0;axis<2;axis++){
   const d=delta[axis],x=p[axis];if(!d)continue
   const lo=x+Math.min(start*d,end*d),hi=x+Math.max(start*d,end*d)
   for(let edge=Math.floor(lo)+1;edge<hi;edge++)cuts.push((edge-x)/d)
  }
  cuts.sort((x,y)=>x-y)
  const at=t=>[a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t]
  for(let j=1;j<cuts.length;j++){
   const u=cuts[j-1],v=cuts[j]
   if(v-u<=1e-10)continue
   const mid=at((u+v)/2),cell=cellAt(grid,...mid),from=at(u),to=at(v)
   if(cell>=0)segments.push({cell,road,coords:[from,to],mid,km:km(from,to)})
  }
 }
 const settlements=exposures.settlements.map(s=>({s,cell:cellAt(grid,s.lon,s.lat)})).filter(s=>s.cell>=0)
 const indexed={segments,settlements}
 grids.set(key,indexed)
 return indexed
}
export function exposureImpact(data,water,exposures,rule=ROAD_RULE,grid=data.meta.east){
 const {segments,settlements}=roadGridSegments(exposures,grid),hit=[],roads=[]
 let roadKm=0,wetRoadKm=0,corridorKm=0,maxRoadDepth=0
 for(const {s,cell} of settlements){if(FLOODED(water.state[cell]))hit.push({...s,depth:water.depth[cell]})}
 for(const segment of segments){
  const {cell}=segment
  if(!FLOODED(water.state[cell])||!(water.depth[cell]>0))continue
  const depth=water.depth[cell],over=Math.max(0,depth-rule.embankmentM)
  const status=depth>rule.embankmentM+rule.closeDepthM?'closed':over>0?'overtopped':'corridor'
  if(status==='closed')roadKm+=segment.km
  else if(status==='overtopped')wetRoadKm+=segment.km
  else corridorKm+=segment.km
  maxRoadDepth=Math.max(maxRoadDepth,over)
  roads.push({...segment,depth,over,status})
 }
 return {settlements:hit,roads,roadKm,wetRoadKm,corridorKm,maxRoadDepth,rule}
}
// Find the replay day with the most road exposure, using only the cells crossed by roads.
export function peakRoadInundation(data,frames,exposures){
 const {segments}=roadGridSegments(exposures,data.meta.east)
 const {meta,heights,domain}=data
 const lengths=new Map()
 for(const {cell,km} of segments){
  if(domain[cell])lengths.set(cell,(lengths.get(cell)||0)+km)
 }
 let best=0,index=-1
 frames.forEach((frame,i)=>{
  const s=toDem(meta,frame.lake),s0=toDem(meta,frame.base??frame.lake),b0=meta.bange.surfaceDemM
  let km=0
  for(const [cell,length] of lengths){
   const h=heights[cell],d=domain[cell]
   if(d===1&&h>s0&&h<s||d===2&&h>b0+.05&&h<b0+frame.bangeRiseM)km+=length
  }
  if(km>best){best=km;index=i}
 })
 return index
}
/** RGBA image of the water state; risk=true colours new flooding by depth class. */
export function waterImage(data,water,{risk=true,flipY=false,landAlpha=0,lake=[37,112,158],shallow=[96,176,214]}={}){
 const {width:w,height:h}=data.meta.east,out=new Uint8ClampedArray(w*h*4)
 for(let r=0;r<h;r++)for(let c=0;c<w;c++){const i=r*w+c,o=((flipY?h-1-r:r)*w+c)*4,s=water.state[i]
  let col=null,a=0
  if(s===1||s===3){col=lake;a=255}
  else if(s===2||s===4){col=risk?DEPTH_RGB.find(x=>water.depth[i]<=x[0])[1]:shallow;a=240}
  if(col){out[o]=col[0];out[o+1]=col[1];out[o+2]=col[2];out[o+3]=a}else out[o+3]=landAlpha}
 return out
}
export function lonLatToEast(meta,lon,lat){const b=meta.east.bounds;return [(lon-b[0])/(b[2]-b[0]),(b[3]-lat)/(b[3]-b[1])]}
