<script setup>
import {computed,ref,watchEffect} from 'vue'
import {waterImage,toLake,roadStateLabel,cellAt} from './overflow-data.mjs'
const props=defineProps({data:Object,frame:Object,water:Object,risk:{type:Boolean,default:true},overlay:Object,exposures:Object,impact:Object})
const view=ref(null),drag=ref(null),tip=ref(null),eastCanvas=ref(),overlayCanvas=ref(),svg=ref()
const meta=computed(()=>props.data.meta)
const ob=computed(()=>meta.value.overview.bounds),k=1000
const cosLat=computed(()=>Math.cos((ob.value[1]+ob.value[3])/2*Math.PI/180))
const size=computed(()=>[(ob.value[2]-ob.value[0])*cosLat.value*k,(ob.value[3]-ob.value[1])*k])
const xy=(lon,lat)=>[(lon-ob.value[0])*cosLat.value*k,(ob.value[3]-lat)*k]
const box=b=>{const [x0,y0]=xy(b[0],b[3]),[x1,y1]=xy(b[2],b[1]);return {x:x0,y:y0,width:x1-x0,height:y1-y0}}
const vb=computed(()=>view.value||[0,0,...size.value])
const eastBox=computed(()=>box(meta.value.east.bounds))
const channel=computed(()=>meta.value.outlet.channel.map(([lon,lat])=>xy(lon,lat).map(v=>v.toFixed(1)).join(',')).join(' '))
const outlet=computed(()=>xy(meta.value.outlet.lon,meta.value.outlet.lat))
const stations=computed(()=>Object.entries(meta.value.stations).map(([id,[lon,lat]])=>({id,name:{SLC:'主站','SLC-E':'东岸','SLC-N':'北岸','SLC-S':'南岸','SLC-W':'西岸'}[id]||id,p:xy(lon,lat)})))
const lakeLabels=computed(()=>(props.data.lakes.features||[]).filter(f=>/[一-鿿]/.test(f.properties.name||'')&&f.properties.areaKm2>15).map(f=>{const cs=f.geometry.type==='Polygon'?f.geometry.coordinates[0]:f.geometry.coordinates.flatMap(p=>p[0]);let x=0,y=0;for(const c of cs){x+=c[0];y+=c[1]}const n=cs.length;return {name:f.properties.name,p:xy(x/n,y/n),big:f.properties.areaKm2>500}}))
const q=computed(()=>Math.max(0,props.frame?.outflow||0))
const points=cs=>cs.map(([lon,lat])=>xy(lon,lat).map(v=>v.toFixed(2)).join(',')).join(' ')
const roads=computed(()=>(props.exposures?.roads||[]).map(r=>({id:r.id,points:points(r.coords)})))
const highlighted=computed(()=>(props.impact?.roads||[]).map(r=>({...r,points:points(r.coords)})))
const hitSettle=computed(()=>new Set((props.impact?.settlements||[]).map(s=>s.id)))
const settlements=computed(()=>(props.exposures?.settlements||[]).map(s=>({...s,p:xy(s.lon,s.lat),hit:hitSettle.value.has(s.id)})))
watchEffect(()=>{
 const c=eastCanvas.value,water=props.water;if(!c||!water)return
 const {width:w,height:h}=meta.value.east
 c.getContext('2d').putImageData(new ImageData(waterImage(props.data,water,{risk:props.risk,lake:[74,144,184]}),w,h),0,0)
},{flush:'post'})
watchEffect(()=>{const c=overlayCanvas.value,o=props.overlay,risk=props.risk;if(!c||!o)return;const {width:w,height:h,depth}=o,img=new ImageData(w,h)
 for(let n=0;n<depth.length;n++){const d=depth[n];if(d>0){const col=!risk?[96,176,214]:d<=.5?[242,211,91]:d<=1.5?[235,154,47]:d<=3?[217,90,36]:[168,35,29];img.data.set([...col,235],n*4)}}
 c.getContext('2d').putImageData(img,0,0)},{flush:'post'})
function toWorld(e){const r=svg.value.getBoundingClientRect(),[x,y,w,h]=vb.value,s=Math.max(w/r.width,h/r.height),ox=x+(w-r.width*s)/2,oy=y+(h-r.height*s)/2;return [ox+(e.clientX-r.left)*s,oy+(e.clientY-r.top)*s,s]}
function wheel(e){const [px,py]=toWorld(e),[x,y,w,h]=vb.value,f=e.deltaY<0?.8:1.25,nw=Math.min(size.value[0],Math.max(40,w*f)),nh=nw*h/w;view.value=[px-(px-x)*nw/w,py-(py-y)*nh/h,nw,nh]}
function down(e){drag.value={x:e.clientX,y:e.clientY,v:[...vb.value]};svg.value.setPointerCapture(e.pointerId)}
function move(e){if(drag.value){const [,,s]=toWorld(e),d=drag.value;view.value=[d.v[0]-(e.clientX-d.x)*s,d.v[1]-(e.clientY-d.y)*s,d.v[2],d.v[3]];tip.value=null;return}
 const [px,py]=toWorld(e),lon=ob.value[0]+px/(cosLat.value*k),lat=ob.value[3]-py/k,r=svg.value.getBoundingClientRect()
 const water=props.water,archive=props.overlay,stored=archive?.depth[cellAt(archive,lon,lat)],i=cellAt(meta.value.east,lon,lat);let text=''
 if(stored>0)text='色林错新增淹没 · 水深 '+stored.toFixed(2)+' m'
 else if(water&&i>=0){const s=water.state[i];text=s===1?'色林错水域':s===3?'班戈错水域':s===2?'色林错新增淹没 · 水深 '+water.depth[i].toFixed(2)+' m':s===4?'班戈错新增淹没 · 水深 '+water.depth[i].toFixed(2)+' m':'陆地 · 相当湖位 '+toLake(meta.value,props.data.heights[i]).toFixed(2)+' m'}
 tip.value={x:e.clientX-r.left,y:e.clientY-r.top,text:lat.toFixed(4)+'°N  '+lon.toFixed(4)+'°E',sub:text}}
function roadTip(e,road){if(drag.value){move(e);return}const r=svg.value.getBoundingClientRect()
 tip.value={x:e.clientX-r.left,y:e.clientY-r.top,text:road.road.name,sub:roadStateLabel(road.status)+' · 地面水深 '+road.depth.toFixed(2)+' m'}}
function up(){drag.value=null}
function reset(target){if(target==='east'){const b=eastBox.value;view.value=[b.x-20,b.y-20,b.width+40,b.height+40]}else if(Array.isArray(target)){const [x,y]=xy(...target);view.value=[x-52,y-38,104,76]}else view.value=null}
defineExpose({reset})
</script>
<template>
<div class="plan">
 <svg ref="svg" :viewBox="vb.join(' ')" preserveAspectRatio="xMidYMid meet" @wheel.prevent="wheel" @pointerdown="down" @pointermove="move" @pointerup="up" @pointerleave="tip=null">
  <image :href="data.base+meta.overview.image" x="0" y="0" :width="size[0]" :height="size[1]" preserveAspectRatio="none"/>
  <foreignObject v-if="water" v-bind="eastBox" class="water-layer"><canvas ref="eastCanvas" :width="meta.east.width" :height="meta.east.height"></canvas></foreignObject>
  <foreignObject v-if="overlay" v-bind="box(overlay.bounds)" class="water-layer"><canvas ref="overlayCanvas" :width="overlay.width" :height="overlay.height"></canvas></foreignObject>
  <rect v-bind="eastBox" class="east-frame"/>
  <polyline v-for="r in roads" :key="'c'+r.id" :points="r.points" class="road-case"/>
  <polyline v-for="r in roads" :key="r.id" :points="r.points" class="road"/>
  <polyline :points="channel" class="channel-bed"/>
  <polyline v-if="q>0" :points="channel" class="channel-flow" :style="{strokeWidth:(1.6+5*Math.sqrt(Math.min(1,q/120)))*vb[2]/size[0]+'px',animationDuration:Math.max(.6,3-2.4*Math.min(1,q/120))+'s'}"/>
  <g v-for="(r,i) in highlighted" :key="i" @pointermove.stop="roadTip($event,r)">
   <polyline :points="r.points" class="road-highlight-case"/>
   <polyline :points="r.points" :class="['road-highlight',r.status]"/>
  </g>
  <g :transform="`translate(${outlet[0]},${outlet[1]}) scale(${vb[2]/size[0]})`"><circle v-if="q>0" r="14" class="pulse"/><circle r="6" class="outlet"/></g>
  <g v-for="s in settlements" :key="s.id" :transform="`translate(${s.p[0]},${s.p[1]}) scale(${vb[2]/size[0]})`" :class="['settle',{hit:s.hit}]"><circle r="5"/><text x="8" y="4">{{s.name}}</text></g>
  <g v-for="s in stations" :key="s.id" :transform="`translate(${s.p[0]},${s.p[1]}) scale(${vb[2]/size[0]})`" class="station"><rect x="-4" y="-4" width="8" height="8"/><text x="8" y="4">{{s.name}}</text></g>
  <g v-for="l in lakeLabels" :key="l.name" :transform="`translate(${l.p[0]},${l.p[1]}) scale(${vb[2]/size[0]})`"><text :class="['lake-name',{big:l.big}]" text-anchor="middle">{{l.name}}</text></g>
 </svg>
 <div v-if="tip" class="plan-tip" :style="{left:tip.x+14+'px',top:tip.y+14+'px'}"><b>{{tip.sub||'—'}}</b><span>{{tip.text}}</span></div>
 <div class="plan-tools"><button @click="reset()">全湖</button><button @click="reset('east')">溃口与班戈错</button></div>
</div>
</template>
<style scoped>
.plan{position:relative;width:100%;height:100%;background:#dfe9ec;overflow:hidden}
.plan svg{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
.water-layer{pointer-events:none}.water-layer canvas{width:100%;height:100%;display:block}
.east-frame{fill:none;stroke:#ffffffaa;stroke-width:1;stroke-dasharray:5 4;vector-effect:non-scaling-stroke}
.channel-bed{fill:none;stroke:#2c6f8c66;stroke-width:1.2px;vector-effect:non-scaling-stroke}
.channel-flow{fill:none;stroke:#6fd3f2;stroke-linecap:round;stroke-dasharray:6 6;animation:flow linear infinite;vector-effect:non-scaling-stroke}
@keyframes flow{to{stroke-dashoffset:-24}}
.outlet{fill:#e0702c;stroke:#fff;stroke-width:2}
.pulse{fill:none;stroke:#e0702c;stroke-width:2;animation:pulse 1.6s ease-out infinite;transform-origin:center}
@keyframes pulse{from{r:6;opacity:.9}to{r:22;opacity:0}}
.station rect{fill:#fff;stroke:#1f5566;stroke-width:1.5}
.road-case{fill:none;stroke:#6b5a3e;stroke-width:3.4px;opacity:.55;stroke-linecap:round;vector-effect:non-scaling-stroke}
.road{fill:none;stroke:#f6edd3;stroke-width:1.8px;stroke-linecap:round;vector-effect:non-scaling-stroke}
.road-highlight,.road-highlight-case{fill:none;stroke-linecap:round;vector-effect:non-scaling-stroke}
.road-highlight-case{stroke:#172b34;stroke-width:8px}
.road-highlight{stroke-width:5px}
.road-highlight.corridor{stroke:#16e5df}
.road-highlight.overtopped{stroke:#ffac22}
.road-highlight.closed{stroke:#ff3a49}
.settle circle{fill:#fff;stroke:#5b4636;stroke-width:2}
.settle text{font-size:11px;fill:#33444c;paint-order:stroke;stroke:#fff;stroke-width:3px}
.settle.hit circle{fill:#c0321f;stroke:#fff;stroke-width:2}
.settle.hit text{fill:#9b2412;font-weight:600}
.station text{font-size:12px;fill:#16404e;paint-order:stroke;stroke:#fff;stroke-width:3px}
.lake-name{font-size:13px;fill:#0e3d52;paint-order:stroke;stroke:#ffffffcc;stroke-width:3px;letter-spacing:.1em}
.lake-name.big{font-size:20px;font-weight:600;letter-spacing:.3em}
.plan-tip{position:absolute;pointer-events:none;background:#10303dee;color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;display:grid;gap:2px;white-space:nowrap}
.plan-tip span{color:#bcd4dc;font-variant-numeric:tabular-nums}
.plan-tools{position:absolute;right:12px;bottom:12px;display:flex;gap:6px}
.plan-tools button{min-height:30px;padding:3px 10px;font-size:12px;background:#ffffffe6}
</style>
