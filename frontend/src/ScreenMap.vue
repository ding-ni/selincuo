<script setup>
import {ref,computed,onMounted,onBeforeUnmount,watch,nextTick} from 'vue'
import {colorFor,layers,rivers} from './dashboard.mjs'
import {mapHover} from './map-hover.mjs'
import {linePath,barRects,formatValue} from './sparkline.mjs'
import {api} from './api.mjs'
import {projector,placeLabels,placeGeographicLabels,leaderEnd,detailMapUrl,geographicAnchorOverrides} from './map-view.mjs'
import {selectedGeometry,geometryBounds} from './state-map.mjs'
import {recordMapTiming} from './map-performance.mjs'
const props=defineProps({stations:Array,selected:String,focus:String,frame:Object,gridFrame:Object,snow:Object,variable:String,showIce:Boolean,showRivers:Boolean,showTerrain:Boolean,showStations:Boolean,showControl:Boolean,showNames:Boolean,showBoundaries:{type:Boolean,default:true},stateLayer:Object,lakeState:Object,selectedUnit:String,selectedObject:Object,rangeLabel:String,spatialBase:{default:'/spatial/'},showGrid:Boolean,showUnits:Boolean,rangeLayer:Object,reducedMotion:Boolean,resultId:String})
const emit=defineEmits(['select','feature','failure','unit'])
const displayFeatures=ref([]),detailedFeatures=ref(null),riverFeatures=ref([]),unitFeatures=ref([]),detailedUnits=ref(null),gridFeatures=ref([]),context=ref(null),presentation=ref(null),terrain=ref(null),spatial=ref(null),loadedBase=ref(''),zoom=ref(1),pan=ref([0,0]),drag=ref(null),moved=ref(false),root=ref(),size=ref({width:800,height:500}),target=ref('domain')
const hover=ref(null),hoverCard=ref(),stationHoverCache=new Map(),pointSeriesCache=new Map();let hoverTicket=0,seriesTimer
const CARD_WIDTH=268
// The card never sits under the pointer: it flips to the left/top near edges.
function place(x,y,height){const W=size.value.width,H=size.value.height;let left=x+18,top=y+18;if(left+CARD_WIDTH>W-8)left=x-18-CARD_WIDTH;if(top+height>H-8)top=y-18-height;return{x:Math.max(8,left),y:Math.max(8,top)}}
function cardHeight(v){return hoverCard.value?.offsetHeight||(70+(v.groups?.length||1)*62)}
function hoverAt(event,value){if(drag.value)return;const [x,y]=point(event);hover.value={...value,px:x,py:y,...place(x,y,cardHeight(value))}}
watch(hover,()=>nextTick(()=>{const h=hover.value;if(h&&hoverCard.value){const p=place(h.px,h.py,hoverCard.value.offsetHeight);if(p.x!==h.x||p.y!==h.y)hover.value={...h,...p}}}),{flush:'post'})
function weatherHover(event,kind,item){
 const base=mapHover(kind,item,{frame:weatherFrame.value,snow:props.snow,spatial:spatial.value})
 const variable=kind==='snow'?'snow':weatherFrame.value?.variable,day=kind==='snow'?props.snow?.day:weatherFrame.value?.day
 const key=[kind,item.id,variable,day].join('|'),cached=pointSeriesCache.get(key)
 hoverAt(event,{...base,variable,seriesKey:key,series:cached||null})
 if(cached||!day||!item.id)return
 clearTimeout(seriesTimer);const ticket=hoverTicket
 seriesTimer=setTimeout(async()=>{try{const query={id:item.id,kind:kind==='grid'?'grid':'unit',variable,day,days:7,...(variable==='snow'?{taskId:props.resultId||''}:{inputId:weatherFrame.value?.inputId||'',spatialVersion:weatherFrame.value?.spatialVersion||'',...(props.resultId?{resultId:props.resultId}:{})})}
  const data=await api('map-point-series?'+new URLSearchParams(query));pointSeriesCache.set(key,data.rows);if(pointSeriesCache.size>300)pointSeriesCache.delete(pointSeriesCache.keys().next().value)
  if(ticket===hoverTicket&&hover.value?.seriesKey===key)hover.value={...hover.value,series:data.rows}}catch{}},250)
}
async function stationHover(event,station){const ticket=++hoverTicket;const hit=stationHoverCache.get(station.id),cached=hit&&Date.now()-hit.at<300000?hit:null;hoverAt(event,cached?{...cached,mode:'station'}:{title:station.name,mode:'station',loading:true,groups:[]});if(cached)return;try{const data=await api('station-hover?'+new URLSearchParams({stationId:station.id}));stationHoverCache.set(station.id,{...data,at:Date.now()});if(ticket===hoverTicket&&hover.value?.mode==='station'){const h=hover.value;hover.value={...data,mode:'station',px:h.px,py:h.py,...place(h.px,h.py,cardHeight(data))}}}catch{if(ticket===hoverTicket&&hover.value)hover.value={...hover.value,loading:false,failed:true}}}
function clearHover(){hoverTicket++;clearTimeout(seriesTimer);hoverStation=null;hover.value=null}
watch(()=>[props.variable,props.frame?.day,props.snow?.day,props.spatialBase],clearHover)
const trend=v=>v==null||Math.abs(v)<1e-9?'':v>0?'up':'down'
const shortTime=(t,latest)=>{if(!t)return '';const s=String(t).replace('T',' '),midnight=s.endsWith(' 00:00:00');if(latest&&s.slice(0,4)!==String(latest).slice(0,4))return s.slice(0,10);return midnight?s.slice(5,10):s.slice(5,16)}
const legendKey=ref(),legendGroup=ref(),mapTools=ref(),overlayBoxes=ref([])
let observer,overlayObserver,loadTicket=0,detailTicket=0,paintTicket=0,geometryWorkMs=0,hasGeometry=false
const geometryPaths=new WeakMap(),fallbackGridGeometry=new Map()
const fallback=[87.39456665,30.05430052,92.23041647,33.33041666]
const bounds=computed(()=>presentation.value?.domainBounds||context.value?.domainBounds||fallback),project=computed(()=>projector(bounds.value))
const xy=p=>project.value(p)
const lakeBounds=computed(()=>presentation.value?.lakeBounds||context.value?.lakeBounds||[88.523,31.504,89.369,32.136])
const features=computed(()=>zoom.value>=2&&detailedFeatures.value?detailedFeatures.value:displayFeatures.value)
const shownUnits=computed(()=>zoom.value>=2&&detailedUnits.value?detailedUnits.value:unitFeatures.value)
const selectedFeatures=computed(()=>selectedGeometry(shownUnits.value,lake.value,spatial.value?.units,props.selectedObject))
const selectedBounds=computed(()=>geometryBounds(selectedFeatures.value))
const viewBounds=computed(()=>target.value==='lake'?lakeBounds.value:target.value==='selection'&&selectedBounds.value?selectedBounds.value:bounds.value)
const fit=computed(()=>{const b=viewBounds.value,a=xy([b[0],b[3]]),z=xy([b[2],b[1]]),w=Math.max(1e-6,z[0]-a[0]),h=Math.max(1e-6,z[1]-a[1]),padding=presentation.value?.viewPadding?.[target.value]??(size.value.width<500?25:32),scale=Math.min(Math.max(1,size.value.width-2*padding)/w,Math.max(1,size.value.height-2*padding)/h);return{scale,x:(size.value.width-w*scale)/2,y:(size.value.height-h*scale)/2}})
// All views use the same projection origin, including object and lake focus.
const camera=computed(()=>{const k=fit.value.scale*zoom.value;const offset=xy([viewBounds.value[0],viewBounds.value[3]]);return{k,x:fit.value.x-offset[0]*k+pan.value[0],y:fit.value.y-offset[1]*k+pan.value[1]}})
const screen=p=>{const a=xy(p),c=camera.value;return[a[0]*c.k+c.x,a[1]*c.k+c.y]}
function path(g){
 if(!g)return ''
 const old=geometryPaths.get(g);if(old?.projection===project.value)return old.value
 const started=performance.now(),line=coordinates=>coordinates.map((c,i)=>`${i?'L':'M'}${xy(c).map(v=>v.toFixed(3)).join(',')}`).join(' ')
 let value
 if(g.type==='LineString')value=line(g.coordinates)
 else if(g.type==='MultiLineString')value=g.coordinates.map(line).join(' ')
 else{const polygons=g.type==='Polygon'?[g.coordinates]:g.type==='MultiPolygon'?g.coordinates:[];value=polygons.map(p=>p.map(r=>line(r)+'Z').join(' ')).join(' ')}
 geometryPaths.set(g,{projection:project.value,value});geometryWorkMs+=performance.now()-started
 return value
}
const basins=computed(()=>features.value.filter(f=>f.properties.kind==='basin'))
const displayedBasinIds=new Set(['zajia','zagen','boqu','ali','fringe'])
const namedBasinIds=new Set(['zajia','zagen','boqu','ali'])
const businessName=f=>f.id==='fringe'?'湖周直接入湖区':f.name
const namedBasins=computed(()=>basins.value.filter(f=>displayedBasinIds.has(f.properties.id)))
const lake=computed(()=>features.value.filter(f=>f.properties.kind==='lake'))
const currentLayer=computed(()=>props.variable==='terrain'&&terrain.value?{...layers.find(l=>l.id==='terrain'),stops:terrain.value.stops,colors:terrain.value.colors}:props.rangeLayer||props.stateLayer||layers.find(l=>l.id===props.variable)||layers[0])
const rawGrid=computed(()=>['precipitation','temperature','evaporation'].includes(props.variable)&&props.frame?.complete)
const lakeFill=computed(()=>props.lakeState?colorFor(props.lakeState.value,currentLayer.value):undefined)
const snowIndex=computed(()=>new Map((props.snow?.units||[]).map(u=>[u.id,u.value])))
const geometryReady=computed(()=>loadedBase.value===props.spatialBase)
const stationPriority={'SLC':100,'SLC-N':98,'SLC-E':97,'SLC-S':96,'SLC-W':95,'DUJA':99}
const stationRadius=computed(()=>presentation.value?.symbols?.stationRadius||4)
const positioned=computed(()=>(props.stations||[]).filter(s=>s.located).map(s=>{
 const config=presentation.value?.stations?.find(l=>l.id===s.id),coordinate=config?.coordinate||config?.anchor
 return {...s,...(coordinate?{lon:coordinate[0],lat:coordinate[1]}:{}),priority:config?.priority??stationPriority[s.id]??80,preferredOffset:config?.preferredOffset||[34,-17],name:config?.name||s.name}
}))
const stationPoints=computed(()=>positioned.value.map(s=>{const [x,y]=screen([s.lon,s.lat]);return{...s,x,y,name:s.name.replace('色林错','')}}).filter(s=>s.x>=0&&s.y>=0&&s.x<=size.value.width&&s.y<=size.value.height))
const stationLabels=computed(()=>{
 const obstacles=[...overlayBoxes.value,...(props.showNames?labels.value.map(l=>l.box):[]),...stationPoints.value.map(s=>({x:s.x-8,y:s.y-8,w:16,h:16}))]
 return placeLabels(stationPoints.value.map(s=>({...s,width:Math.max(48,s.name.length*13+8),height:22})),size.value,obstacles).map(s=>{const anchor=screen([s.lon,s.lat]);return {...s,anchor,leaderEnd:leaderEnd(anchor,s.box)}})
})
const stationLabel=id=>stationLabels.value.find(s=>s.id===id)
// Names remain on their geographic interior points, regardless of stations.
const labels=computed(()=>placeGeographicLabels((presentation.value?.labels||context.value?.labels||[]).filter(l=>l.kind==='lake'||l.kind==='basin'&&namedBasinIds.has(l.id)).map(l=>{const anchor=geographicAnchorOverrides[l.id]||l.anchor||l.coordinate,geo=namedBasins.value.find(f=>f.properties.id===l.id),[x,y]=screen(anchor);return{...l,anchor,areaKm2:geo?.properties.areaKm2??l.areaKm2,x,y,fontSize:l.kind==='lake'?16:13}}).filter(l=>l.x>=0&&l.y>=0&&l.x<=size.value.width&&l.y<=size.value.height)))
const terrainLevel=computed(()=>[...(terrain.value?.levels||[])].sort((a,b)=>(a.minZoom||0)-(b.minZoom||0)).filter(level=>zoom.value>=(level.minZoom||0)).at(-1))
const terrainBox=computed(()=>{const b=terrainLevel.value?.bounds||terrain.value?.bounds||context.value?.bounds||[87.1,29.8,92.5,33.6],p=xy([b[0],b[3]]),q=xy([b[2],b[1]]);return{x:p[0],y:p[1],width:q[0]-p[0],height:q[1]-p[1]}})
const terrainImage=computed(()=>terrainLevel.value?.image||terrain.value?.image||props.spatialBase+'terrain-display.png')
const terrainDescription=computed(()=>{
 const t=terrain.value,level=terrainLevel.value;if(!t)return '地形显示资料'
 const source=t.source||'地形源分辨率待核实',resolution=t.sourceResolutionMetresAtDomainCentre||t.sourceResolutionMeters
 const sourceSize=resolution?(Array.isArray(resolution)?resolution.map(v=>Math.round(v)).join(' × '):Math.round(resolution))+' m':''
 const pixels=level?.displayPixelSizeMetres??t.displayPixelSizeMetres??level?.displayPixelSizeDegrees??t.displayPixelSizeDegrees
 const pixel=Array.isArray(pixels)?pixels.map(v=>Number(v).toPrecision(3)).join(' × '):pixels?Number(pixels).toPrecision(3):null
 const pixelUnit=level?.displayPixelSizeMetres||t.displayPixelSizeMetres?'m':'°'
 return source+(sourceSize?' · 源像元约 '+sourceSize:'')+(pixel?' · 显示像元 '+pixel+' '+pixelUnit:'')
})
const scaleKm=computed(()=>{const max=95/camera.value.k;return [1,2,5,10,20,50,100,200].filter(k=>k<=max).at(-1)||1})
function focusAt(k,point=[size.value.width/2,size.value.height/2]){const z=Math.min(12,Math.max(1,zoom.value*k)),f=z/zoom.value,c=camera.value;const newX=point[0]-(point[0]-c.x)*f,newY=point[1]-(point[1]-c.y)*f;zoom.value=z;const base=camera.value;pan.value=[newX-base.x+pan.value[0],newY-base.y+pan.value[1]];if(z===1)pan.value=[0,0]}
function reset(to='domain'){target.value=to;zoom.value=1;pan.value=[0,0]}
function locateSelection(){if(selectedBounds.value)reset('selection')}
watch(()=>[props.selectedObject?.scope,props.selectedObject?.id],()=>{if(props.selectedObject)locateSelection()},{flush:'post'})
function point(e){const r=root.value.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]}
function down(e){if(e.button!==0)return;drag.value={start:point(e),pan:[...pan.value]};moved.value=false}
// Stations cluster near the lake at full-domain scale; pick the closest one
// to the pointer instead of whichever SVG element happens to be on top.
let hoverStation=null
function stationProbe(e){
 if(!props.showStations)return
 const [x,y]=point(e);let best=null,distance=14
 for(const s of stationPoints.value){const d=Math.hypot(s.x-x,s.y-y);if(d<distance){distance=d;best=s}}
 if(!best){if(hover.value?.mode==='station'){hoverStation=null;clearHover()}return}
 if(hoverStation===best.id&&hover.value?.mode==='station'&&!hover.value.loading){hover.value={...hover.value,px:x,py:y,...place(x,y,cardHeight(hover.value))};return}
 if(hoverStation===best.id&&hover.value?.mode==='station')return
 hoverStation=best.id;stationHover(e,positioned.value.find(s=>s.id===best.id)||best)
}
function move(e){if(!drag.value){stationProbe(e);return}clearHover();const p=point(e),dx=p[0]-drag.value.start[0],dy=p[1]-drag.value.start[1];if(Math.abs(dx)+Math.abs(dy)>4){moved.value=true;root.value.setPointerCapture(e.pointerId)}if(moved.value)pan.value=[drag.value.pan[0]+dx,drag.value.pan[1]+dy]}
function up(e){drag.value=null;if(root.value.hasPointerCapture(e.pointerId))root.value.releasePointerCapture(e.pointerId)}
function selectFeature(f){if(geometryReady.value&&!moved.value)emit('feature',{...f.properties,name:businessName(f.properties)})}
function selectBasin(id){const f=basins.value.find(f=>f.properties.id===id);if(f)selectFeature(f)}
// Meteorological overlays keep their own forcing data when the base is terrain or a model state.
const weatherFrame=computed(()=>['precipitation','temperature','evaporation'].includes(props.variable)?props.frame:props.gridFrame)
const weatherLayer=computed(()=>layers.find(l=>l.id===weatherFrame.value?.variable)||layers[0])
const cellValues=computed(()=>new Map((weatherFrame.value?.complete?(weatherFrame.value.cells||[]):[]).map(c=>[c.id,c.value])))
const gridGeometry=computed(()=>{
 const geometries=new Map()
 // ERA cells are rectangles clipped to the domain by the SVG. Reuse those
 // rectangles: grid.geojson is already clipped and can have many vertices.
 for(const c of props.frame?.cells||[]){
  const key=[c.id,c.lon,c.lat].join('|')
  if(!fallbackGridGeometry.has(key))fallbackGridGeometry.set(key,{type:'Polygon',coordinates:[[[c.lon-.05,c.lat-.05],[c.lon+.05,c.lat-.05],[c.lon+.05,c.lat+.05],[c.lon-.05,c.lat+.05],[c.lon-.05,c.lat-.05]]]})
  geometries.set(c.id,fallbackGridGeometry.get(key))
 }
 return geometries
})
const gridPaint=computed(()=>['basin','terrain'].includes(props.variable))
function gridSelect(f){if(moved.value)return;const p=f.properties,index=spatial.value?.grids?.findIndex(g=>g.id===p.id),ids=new Set((spatial.value?.aggregation||[]).filter(g=>g.members.some(m=>m.gridIndex===index)).map(g=>g.unitId));const members=unitFeatures.value.filter(u=>u.properties.grid===p.id||ids.has(u.properties.id)).map(f=>f.properties);emit('feature',{...p,name:'气象格网 · '+(weatherFrame.value?.variable==='precipitation'?'日降水':weatherLayer.value.label),value:cellValues.value.get(p.id),unit:weatherLayer.value.unit,unitCount:members.length,members})}
async function loadGeometry(){
 const n=++loadTicket,base=props.spatialBase
 const requests=await Promise.allSettled(['display.geojson','rivers-display.geojson','units-display.geojson','display-context.json','manifest.json','grid.geojson'].map(async file=>{const r=await fetch(base+file);if(!r.ok)throw Error(file+'加载失败');return r.json()}))
 if(n!==loadTicket)return
 const missing=requests.find((item,i)=>i<5&&item.status==='rejected');if(missing){emit('failure',missing.reason.message);return}
 ;[displayFeatures,riverFeatures,unitFeatures].forEach((t,i)=>{t.value=requests[i].value.features})
 if(requests[3].status==='fulfilled')context.value=requests[3].value
 if(requests[4].status==='fulfilled')spatial.value=requests[4].value
 if(requests[5].status==='fulfilled')gridFeatures.value=requests[5].value.features
 else if(requests[4].status==='fulfilled')gridFeatures.value=requests[4].value.grids.map(g=>({properties:g,geometry:{type:'Polygon',coordinates:[[[g.lon-.05,g.lat-.05],[g.lon+.05,g.lat-.05],[g.lon+.05,g.lat+.05],[g.lon-.05,g.lat+.05],[g.lon-.05,g.lat-.05]]]}}))
 loadedBase.value=base
 detailTicket++;detailedFeatures.value=null;detailedUnits.value=null
 if(!hasGeometry){reset(props.selectedObject&&selectedBounds.value?'selection':'domain');hasGeometry=true}
 else if(props.selectedObject&&selectedBounds.value&&target.value!=='selection')locateSelection()
 else if(target.value==='selection'&&!selectedBounds.value)reset('domain')
 if(zoom.value>=2)loadDetail()
}
function fineBoundariesMatch(fine,display){
 const key=f=>(f.properties.kind||'')+':'+(f.properties.id||'')
 const indexed=new Map(display.map(f=>[key(f),f]))
 if(indexed.size!==display.length||fine.length!==display.length)return null
 const matched=fine.map(f=>{const same=indexed.get(key(f));return same?{...f,properties:same.properties}:null})
 return matched.every(Boolean)&&new Set(fine.map(key)).size===indexed.size?matched:null
}
async function loadDetail(){
 const base=loadedBase.value;if(!base||!geometryReady.value)return
 const url=detailMapUrl(base,spatial.value),ticket=++detailTicket
 const fetchJson=async path=>{const r=await fetch(path);if(!r.ok)throw Error('地图精细图层暂不可用');return r.json()}
 const requests=await Promise.allSettled([url?fetchJson(url):Promise.reject(Error('无匹配精细边界')),fetchJson(base+'units.geojson')])
 if(ticket!==detailTicket||base!==loadedBase.value)return
 if(requests[0].status==='fulfilled'){
  const detailed=fineBoundariesMatch(requests[0].value.features,displayFeatures.value)
  if(detailed)detailedFeatures.value=detailed
 }
 if(requests[1].status==='fulfilled'){
  const byId=new Map(unitFeatures.value.map(f=>[f.properties.id,f])),units=requests[1].value.features
  if(units.length===byId.size&&new Set(units.map(f=>f.properties.id)).size===byId.size&&units.every(f=>byId.has(f.properties.id)))detailedUnits.value=units.map(f=>({...f,properties:byId.get(f.properties.id).properties}))
 }
}
function updateOverlayBoxes(){
 if(!root.value)return
 const origin=root.value.getBoundingClientRect()
 overlayBoxes.value=[legendKey.value,legendGroup.value,mapTools.value].filter(Boolean).map(el=>el.getBoundingClientRect())
  .filter(r=>r.width>0&&r.height>0).map(r=>({x:r.left-origin.left,y:r.top-origin.top,w:r.width,h:r.height}))
}
function observeOverlays(){
 if(!overlayObserver)return
 overlayObserver.disconnect()
 for(const el of [legendKey.value,legendGroup.value,mapTools.value])if(el)overlayObserver.observe(el)
 updateOverlayBoxes()
}
watch([legendKey,legendGroup,mapTools],observeOverlays,{flush:'post'})
watch(()=>[props.variable,props.frame,props.snow,loadedBase.value],async()=>{
 const ticket=++paintTicket,started=performance.now(),geometryBefore=geometryWorkMs,variable=props.variable||'basin'
 await nextTick()
 if(ticket!==paintTicket)return
 const committed=performance.now()
 recordMapTiming('geometry:'+variable,started,geometryWorkMs-geometryBefore)
 recordMapTiming('dom:'+variable,started,committed-started)
 requestAnimationFrame(()=>requestAnimationFrame(()=>{
  if(ticket===paintTicket)recordMapTiming('present:'+variable,committed,performance.now()-committed)
 }))
},{flush:'pre'})
watch(()=>props.spatialBase,loadGeometry,{immediate:true})
watch(zoom,value=>{if(value>=2&&!detailedFeatures.value&&!detailedUnits.value)loadDetail()})
onMounted(()=>{observer=new ResizeObserver(([e])=>{
 const old=size.value,oldCamera=camera.value,oldPan=[...pan.value]
 const centre=[(old.width/2-oldCamera.x)/oldCamera.k,(old.height/2-oldCamera.y)/oldCamera.k]
 size.value={width:e.contentRect.width,height:e.contentRect.height}
 const next=camera.value
 if(old.width&&old.height&&next.k)pan.value=[size.value.width/2-centre[0]*next.k-(next.x-oldPan[0]),size.value.height/2-centre[1]*next.k-(next.y-oldPan[1])]
 updateOverlayBoxes()
 });observer.observe(root.value);overlayObserver=new ResizeObserver(updateOverlayBoxes);observeOverlays()})
onMounted(async()=>{
 try{let r=await fetch('/spatial/presentation.json');if(!r.ok)r=await fetch('/spatial/display-context.json');if(r.ok)presentation.value=await r.json()}catch{}
 try{let r=await fetch('/spatial/terrain-current.json');if(!r.ok)r=await fetch('/spatial/terrain-elevation.json');if(r.ok)terrain.value=await r.json()}catch{}
})
onBeforeUnmount(()=>{observer?.disconnect();overlayObserver?.disconnect();loadTicket++;detailTicket++;paintTicket++})
</script>
<template><div class="screen-map" :class="{'basin-view':variable==='basin'}" :style="{'--map-label-stroke':(presentation?.symbols?.labelStrokeWidth||2)+'px','--map-station-stroke':(presentation?.symbols?.stationStrokeWidth||1)+'px'}" :data-spatial-ready="geometryReady" :data-spatial-base="loadedBase" :data-boundary-detail="zoom>=2&&!!detailedFeatures" :data-unit-detail="zoom>=2&&!!detailedUnits"><svg ref="root" :viewBox="`0 0 ${size.width} ${size.height}`" :data-zoom="zoom" role="img" aria-label="色林错全流域、四河分区及测站地图" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @pointerleave="clearHover" @wheel.prevent="focusAt($event.deltaY<0?1.15:1/1.15,point($event))"><defs><pattern id="screen-graticule" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0H0V50" fill="none" stroke="currentColor" stroke-width=".5"/></pattern><clipPath id="screen-domain"><path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="i" :d="path(f.geometry)"/></clipPath><pattern id="fringe-hatch" width="5" height="5" patternUnits="userSpaceOnUse"><path d="M0 5L5 0" stroke="var(--basin-fringe)" stroke-width=".5" opacity=".55"/></pattern></defs>
 <rect :width="size.width" :height="size.height" fill="url(#screen-graticule)" class="graticule"/>
 <g class="map-world" :transform="`translate(${camera.x},${camera.y}) scale(${camera.k})`">
  <image v-if="showTerrain||variable==='terrain'" :href="terrainImage" v-bind="terrainBox" class="terrain-image" clip-path="url(#screen-domain)"/>
  <path v-for="f in basins" :key="f.properties.id" :d="path(f.geometry)" :data-basin="f.properties.id" :class="['screen-geo','basin',f.properties.id,{'basin-background':!displayedBasinIds.has(f.properties.id),focused:focus===f.properties.id}]" fill-rule="evenodd" @click="displayedBasinIds.has(f.properties.id)&&selectFeature(f)"><title v-if="displayedBasinIds.has(f.properties.id)">{{f.properties.name}}流域 · {{f.properties.areaKm2.toFixed(1)}} km²</title></path>
  <g v-if="geometryReady&&rawGrid" class="thematic-grid" clip-path="url(#screen-domain)"><path v-for="c in frame.cells" :key="c.id" :data-grid="c.id" :data-value="c.value" :d="path(gridGeometry.get(c.id))" :fill="colorFor(c.value,currentLayer)" class="grid-cell" @pointermove="weatherHover($event,'grid',c)" @pointerleave="clearHover" @click.stop="gridSelect({properties:c})"></path></g>
  <g v-if="geometryReady&&variable==='snow'&&snow?.complete"><path v-for="f in shownUnits" :key="f.properties.id" :d="path(f.geometry)" :fill="colorFor(snowIndex.get(f.properties.id),currentLayer)" class="snow-unit" @pointermove="weatherHover($event,'snow',f.properties)" @pointerleave="clearHover" :class="{chosen:selectedUnit===f.properties.id}" fill-rule="evenodd" @click.stop="emit('unit',f.properties.id)"/></g>
  <g v-if="geometryReady&&showUnits" class="calculation-unit-edges"><path v-for="f in shownUnits" :key="f.properties.id" :d="path(f.geometry)" :class="{chosen:selectedUnit===f.properties.id}" fill-rule="evenodd" @pointermove="weatherHover($event,variable==='snow'?'snow':'unit',f.properties)" @pointerleave="clearHover" @click.stop="!moved&&emit('unit',f.properties.id)"></path></g>
  <g v-if="geometryReady&&showGrid" class="meteorological-grid" :data-variable="weatherFrame?.variable" clip-path="url(#screen-domain)"><path v-for="f in gridFeatures" :key="f.properties.id" :d="path(f.geometry)" :data-grid="f.properties.id" :data-value="cellValues.get(f.properties.id)" @pointermove="weatherHover($event,'grid',{...f.properties,value:cellValues.get(f.properties.id)})" @pointerleave="clearHover" :style="{fill:gridPaint?colorFor(cellValues.get(f.properties.id),weatherLayer):'transparent',fillOpacity:gridPaint?0.4:1}" fill-rule="evenodd" @click.stop="gridSelect(f)"></path></g>
  <g v-if="showRivers" class="screen-rivers"><path v-for="(f,i) in riverFeatures" :key="i" :data-river="f.properties.basinId" :class="{focused:focus===f.properties.basinId}" :d="path(f.geometry)"><title>{{f.properties.name}}</title></path></g>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='glacier'&&showIce)" :key="'g'+i" :d="path(f.geometry)" class="screen-geo glacier" @click="selectFeature(f)"/>
  <path v-for="(f,i) in lake" :key="'l'+i" :d="path(f.geometry)" class="screen-geo lake" :style="rawGrid?{fill:'none',pointerEvents:'none'}:lakeFill?{fill:lakeFill}:undefined" fill-rule="evenodd" @click="selectFeature(f)"/>
  <!-- Draw boundaries after thematic raster so partitions remain visible on every layer. -->
  <template v-if="showBoundaries"><path v-for="f in namedBasins" :key="'edge'+f.properties.id" :d="path(f.geometry)" :data-boundary="f.properties.id" :class="['basin-boundary',f.properties.id]" fill-rule="evenodd"/></template>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="'d'+i" :d="path(f.geometry)" class="screen-geo domain"/>
  <path v-if="showControl" v-for="(f,i) in features.filter(f=>f.properties.kind==='control')" :key="'c'+i" :d="path(f.geometry)" class="screen-geo control" fill-rule="evenodd" @click="selectFeature(f)"><title>杜加内部控制区</title></path>
  <path v-for="f in namedBasins.filter(f=>f.properties.id===focus)" :key="'focus'+f.properties.id" :d="path(f.geometry)" class="basin-selection" fill="url(#fringe-hatch)" fill-rule="evenodd" pointer-events="none"/>
  <g v-if="geometryReady&&selectedObject" class="selected-object-layer" :data-object="selectedObject.id" :data-scope="selectedObject.scope"><path v-for="f in selectedFeatures" :key="'selected-'+f.properties.id" :d="path(f.geometry)" class="selected-object-highlight" fill-rule="evenodd"/></g>
 </g>
 <g v-if="showStations" v-for="s in positioned" :key="s.id" :data-station="s.id" :transform="`translate(${screen([s.lon,s.lat]).join(',')})`" :class="['screen-station',s.kind,{chosen:selected===s.id}]" @click.stop="emit('select',s.id)" @keydown.enter="emit('select',s.id)" @keydown.space.prevent="emit('select',s.id)" role="button" tabindex="0" :aria-label="'查看'+s.name"><path v-if="stationLabel(s.id)" :d="`M0 0L${stationLabel(s.id).leaderEnd[0]-stationLabel(s.id).anchor[0]},${stationLabel(s.id).leaderEnd[1]-stationLabel(s.id).anchor[1]}`" class="label-leader"/><circle class="station-halo" :r="selected===s.id?11:8"/><path v-if="s.kind==='rain'" :d="`M0 ${-stationRadius-1}L${stationRadius+1} ${stationRadius}H${-stationRadius-1}Z`"/><rect v-else-if="s.kind==='river'" :x="-stationRadius" :y="-stationRadius" :width="2*stationRadius" :height="2*stationRadius" transform="rotate(45)"/><circle v-else :r="stationRadius"/><text v-if="stationLabel(s.id)" :x="stationLabel(s.id).cx-stationLabel(s.id).anchor[0]" :y="stationLabel(s.id).cy-stationLabel(s.id).anchor[1]+4" text-anchor="middle">{{s.name.replace('色林错','')}}</text></g>
 <g v-if="showNames" class="basin-labels"><g v-for="l in labels" :key="l.id||l.kind" :data-label="l.id||l.kind" :data-anchor="l.anchor.join(',')" :class="['basin-map-label',l.id||l.kind,{focused:focus===l.id}]" role="button" tabindex="0" :aria-label="l.kind==='lake'?'色林错':l.name+'流域'" @click.stop="emit('feature',l)" @keydown.enter="emit('feature',l)"><text :x="l.cx" :y="l.cy+(l.lines.length===1?4:-3)" text-anchor="middle"><tspan v-for="(line,i) in l.lines" :key="i" :x="l.cx" :dy="i?13:0">{{line}}</tspan></text></g></g>
 <text x="18" y="25" class="north-label">↑ N</text><g class="scale-bar" :transform="`translate(18,${size.height-45})`"><path :d="`M0 -5V0H${scaleKm*camera.k}V-5`"/><text x="0" y="15">{{scaleKm}} km</text></g></svg>
 <aside v-if="hover" ref="hoverCard" class="map-hover-card" role="status" :style="{left:hover.x+'px',top:hover.y+'px'}">
  <header><strong>{{hover.title}}</strong><span class="hv-tag">{{hover.mode==='station'?hover.kindLabel||'监测站':hover.tag}}</span></header>
  <p class="hv-sub" v-if="hover.mode==='station'">{{hover.loading?'正在读取最新观测…':hover.failed?'暂时无法读取观测':hover.latestTime?'最新 '+hover.latestTime.replace('T',' ').slice(0,16):'最近观测'}}</p>
  <p class="hv-sub" v-else>{{hover.time}}<template v-if="hover.subtitle"> · {{hover.subtitle}}</template></p>
  <template v-if="hover.mode==='station'">
   <div v-for="g in hover.groups" :key="g.variable" class="hv-row">
    <span class="hv-label">{{g.label}}</span>
    <span class="hv-value"><b>{{formatValue(g.variable,g.latest?.value)}}</b><small>{{g.latest?g.unit:''}}</small><em v-if="trend(g.change24h)" :class="trend(g.change24h)">{{g.change24h>0?'▲':'▼'}}{{formatValue(g.variable,Math.abs(g.change24h))}}</em></span>
    <svg class="hv-spark" viewBox="0 0 116 30" preserveAspectRatio="none" aria-hidden="true">
     <template v-if="g.variable==='precipitation'"><rect v-for="(b,i) in barRects(g.daily.map(d=>d.value))" :key="i" :x="b.x" :y="b.y" :width="b.width" :height="b.height" :class="{gap:b.missing}"/></template>
     <template v-else-if="linePath(g.daily.map(d=>d.value))"><path class="area" :d="linePath(g.daily.map(d=>d.value)).area"/><path class="line" :d="linePath(g.daily.map(d=>d.value)).line"/><circle :cx="linePath(g.daily.map(d=>d.value)).last[0]" :cy="linePath(g.daily.map(d=>d.value)).last[1]" r="2.2"/></template>
    </svg>
    <span class="hv-foot">{{g.variable==='precipitation'?(g.sum7d!=null?'近7日 '+formatValue('precipitation',g.sum7d)+' mm · ':'')+(g.latest?.time||'').slice(5,10):shortTime(g.latest?.time,hover.latestTime)}}</span>
   </div>
   <p v-if="!hover.loading&&!hover.groups?.length" class="hv-empty">暂无观测</p>
  </template>
  <template v-else>
   <div class="hv-main"><span class="hv-label">{{hover.label}}</span><span class="hv-value big"><b>{{formatValue(hover.variable,hover.value)}}</b><small>{{hover.value!=null?hover.unit:''}}</small></span></div>
   <div class="hv-week">
    <svg class="hv-spark wide" viewBox="0 0 116 30" preserveAspectRatio="none" aria-hidden="true">
     <template v-if="hover.series&&hover.variable==='precipitation'"><rect v-for="(b,i) in barRects(hover.series.map(d=>d.value))" :key="i" :x="b.x" :y="b.y" :width="b.width" :height="b.height" :class="{gap:b.missing,today:i===hover.series.length-1}"/></template>
     <template v-else-if="hover.series&&linePath(hover.series.map(d=>d.value))"><path class="area" :d="linePath(hover.series.map(d=>d.value)).area"/><path class="line" :d="linePath(hover.series.map(d=>d.value)).line"/><circle :cx="linePath(hover.series.map(d=>d.value)).last[0]" :cy="linePath(hover.series.map(d=>d.value)).last[1]" r="2.2"/></template>
    </svg>
    <span class="hv-foot">{{hover.series?'近7日 '+hover.series[0].date.slice(5)+' — '+hover.series.at(-1).date.slice(5):'近7日读取中…'}}</span>
   </div>
  </template>
 </aside>
 <div ref="mapTools" class="screen-map-tools"><button aria-label="放大地图" @click="focusAt(1.4)">＋</button><button aria-label="缩小地图" @click="focusAt(1/1.4)">−</button><button v-if="selectedObject" :disabled="!selectedFeatures.length" @click="locateSelection">定位到{{selectedObject.scope==='unit'?'单元':'区域'}}</button><button :class="{on:target==='domain'&&zoom===1}" @click="reset()">全流域</button><button v-if="!selectedObject" :class="{on:target==='lake'&&zoom===1}" @click="reset('lake')">湖区</button></div>
 <div v-if="selectedObject" class="selected-object-caption">{{selectedObject.label}}<span v-if="!selectedFeatures.length"> · 该对象没有对应空间几何</span></div>
 <div class="screen-map-source">{{target==='selection'?'所选区域':target==='domain'?'全流域':'湖区'}} · 滚轮缩放 / 拖动平移</div>
 <div v-if="!geometryReady" class="map-status" role="status">地图资料正在更新…</div>
 <div v-if="variable==='basin'&&!showGrid" ref="legendKey" class="basin-map-key"><button v-for="f in namedBasins" :key="f.properties.id" :class="[f.properties.id,{on:focus===f.properties.id}]" @click="selectBasin(f.properties.id)"><i></i>{{businessName(f.properties)}}</button></div>
 <div ref="legendGroup" class="screen-map-legends">
  <div v-if="showGrid&&gridPaint" class="screen-map-legend weather-grid-legend"><b>格网{{weatherFrame?.variable==='precipitation'?'日降水':weatherLayer.label}} <small>{{weatherLayer.unit}}</small></b><div><span v-for="(c,i) in weatherLayer.colors" :key="c" :style="{background:c}" :title="weatherLayer.stops[i]+'以上'"></span></div><p class="legend-ticks"><span v-for="s in weatherLayer.stops" :key="s">{{s}}</span></p><p>再分析格网原值 · 空白为缺测</p></div>
  <div v-if="variable!=='basin'" class="screen-map-legend"><b>{{variable==='terrain'&&!terrain?'地形阴影':currentLayer.label}} <small>{{variable==='terrain'&&!terrain?'':currentLayer.unit}}</small></b><template v-if="variable!=='terrain'||terrain"><div><span v-for="(c,i) in currentLayer.colors" :key="c" :style="{background:c}" :title="currentLayer.stops[i]+'以上'"></span></div><p class="legend-ticks"><span v-for="s in currentLayer.stops" :key="s">{{s}}</span></p><p>{{variable==='terrain'?terrainDescription:rangeLabel||(stateLayer?'当前时刻范围':variable==='precipitation'?'0.1°格网原值 · 全时段统一色阶':'全时段统一色阶')}} · 最高档 ≥ {{currentLayer.stops.at(-1)}}</p><p v-if="variable!=='terrain'">空白：缺测 · 最浅档含零值</p></template></div>
 </div>
 </div></template>

<style scoped>
.map-hover-card{--hv-panel:var(--sc-panel,var(--panel,#fff));--hv-ink:var(--sc-ink,var(--ink,#193b49));--hv-muted:var(--sc-muted,var(--muted,#667f89));--hv-line:var(--sc-line,var(--line,#d7e3e8));--hv-soft:var(--sc-soft,#eef4f6);--hv-accent:var(--sc-accent,#227d8c);position:absolute;z-index:30;width:268px;padding:12px 14px 10px;border:1px solid var(--hv-line);border-radius:9px;background:color-mix(in srgb,var(--hv-panel) 95%,transparent);backdrop-filter:blur(6px);color:var(--hv-ink);box-shadow:0 10px 28px #0f2c3a2e,0 1px 3px #0f2c3a1f;font-size:12px;pointer-events:none;transition:left .06s linear,top .06s linear}.map-hover-card header{background:none;border:0;padding:0;margin:0;position:static;height:auto;min-height:0;box-shadow:none;display:flex;align-items:center;justify-content:space-between;gap:10px}.map-hover-card header strong{font-size:14px;font-weight:650;overflow-wrap:anywhere}.hv-tag{flex-shrink:0;font-size:10.5px;padding:2px 7px;border-radius:10px;background:var(--hv-soft);color:var(--hv-muted)}.hv-sub{margin:3px 0 8px;color:var(--hv-muted);font-size:11px;font-variant-numeric:tabular-nums}.hv-row{display:grid;grid-template-columns:34px 1fr 74px;grid-template-rows:auto auto;column-gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--hv-line)}.hv-label{color:var(--hv-muted);font-size:11.5px}.hv-value{display:flex;align-items:baseline;gap:3px;white-space:nowrap;font-variant-numeric:tabular-nums}.hv-value b{font-size:16px;font-weight:650;letter-spacing:-.01em}.hv-value.big b{font-size:22px}.hv-value small{color:var(--hv-muted);font-size:11px}.hv-value em{font-style:normal;font-size:10.5px;margin-left:5px}.hv-value em.up{color:#c4612f}.hv-value em.down{color:#2b7fa2}.hv-spark{width:74px;height:26px;grid-row:1/3;grid-column:3;overflow:visible}.hv-spark.wide{width:100%;height:34px}.hv-spark .line{fill:none;stroke:var(--hv-accent);stroke-width:1.6;vector-effect:non-scaling-stroke}.hv-spark .area{fill:color-mix(in srgb,var(--hv-accent) 16%,transparent)}.hv-spark circle{fill:var(--hv-accent)}.hv-spark rect{fill:color-mix(in srgb,var(--hv-accent) 75%,transparent)}.hv-spark rect.today{fill:var(--hv-accent)}.hv-spark rect.gap{fill:none}.hv-foot{grid-column:2;color:var(--hv-muted);font-size:10.5px;font-variant-numeric:tabular-nums}.hv-main{display:flex;align-items:baseline;justify-content:space-between;padding:4px 0 6px}.hv-week{border-top:1px solid var(--hv-line);padding-top:7px;display:grid;gap:3px}.hv-empty{margin:6px 0 0;color:var(--hv-muted)}
</style>
