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
 const {meta,heights,domain}=data,n=heights.length,state=new Uint8Array(n),depth=new Float32Array(n)
 const s=toDem(meta,lake),s0=toDem(meta,base??lake),b=meta.bange.surfaceDemM+bangeRiseM
 let newCells=0
 for(let i=0;i<n;i++){const h=heights[i],d=domain[i]
  if(d===1&&h<=s){if(h>s0){state[i]=2;depth[i]=s-h;newCells++}else state[i]=1}
  else if(d===2&&h<=b)state[i]=3}
 const [dx,dy]=meta.east.pixelMetres
 return {state,depth,newAreaKm2:newCells*dx*dy/1e6}
}
/** RGBA image of the water state; risk=true colours new flooding by depth class. */
export function waterImage(data,water,{risk=true,flipY=false,landAlpha=0,lake=[37,112,158],shallow=[96,176,214]}={}){
 const {width:w,height:h}=data.meta.east,out=new Uint8ClampedArray(w*h*4)
 for(let r=0;r<h;r++)for(let c=0;c<w;c++){const i=r*w+c,o=((flipY?h-1-r:r)*w+c)*4,s=water.state[i]
  let col=null,a=0
  if(s===1||s===3){col=lake;a=255}
  else if(s===2){col=risk?DEPTH_RGB.find(x=>water.depth[i]<=x[0])[1]:shallow;a=240}
  if(col){out[o]=col[0];out[o+1]=col[1];out[o+2]=col[2];out[o+3]=a}else out[o+3]=landAlpha}
 return out
}
export function lonLatToEast(meta,lon,lat){const b=meta.east.bounds;return [(lon-b[0])/(b[2]-b[0]),(b[3]-lat)/(b[3]-b[1])]}
