<script setup>
import {ref,computed,onMounted,onBeforeUnmount,watch} from 'vue'
import {colorFor,layers,rivers} from './dashboard.mjs'
import {projector,placeLabels,detailMapUrl} from './map-view.mjs'
import {selectedGeometry,geometryBounds} from './state-map.mjs'
const props=defineProps({stations:Array,selected:String,focus:String,frame:Object,gridFrame:Object,snow:Object,variable:String,showIce:Boolean,showRivers:Boolean,showTerrain:Boolean,showStations:Boolean,showControl:Boolean,showNames:Boolean,showBoundaries:{type:Boolean,default:true},stateLayer:Object,lakeState:Object,selectedUnit:String,selectedObject:Object,rangeLabel:String,spatialBase:{default:'/spatial/'},showGrid:Boolean,showUnits:Boolean,rangeLayer:Object,reducedMotion:Boolean})
const emit=defineEmits(['select','feature','failure','unit'])
const displayFeatures=ref([]),detailedFeatures=ref(null),riverFeatures=ref([]),unitFeatures=ref([]),detailedUnits=ref(null),gridFeatures=ref([]),context=ref(null),presentation=ref(null),terrain=ref(null),spatial=ref(null),loadedBase=ref(''),zoom=ref(1),pan=ref([0,0]),drag=ref(null),moved=ref(false),root=ref(),size=ref({width:800,height:500}),target=ref('domain')
const legendKey=ref(),legendGroup=ref(),mapTools=ref(),overlayBoxes=ref([])
let observer,overlayObserver,loadTicket=0,detailTicket=0,hasGeometry=false
const geometryPaths=new WeakMap()
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
function path(g){if(!g)return '';const old=geometryPaths.get(g);if(old?.projection===project.value)return old.value;let value;if(g.type==='LineString')value=g.coordinates.map((c,i)=>`${i?'L':'M'}${xy(c).map(v=>v.toFixed(3)).join(',')}`).join(' ');else if(g.type==='MultiLineString')value=g.coordinates.map(c=>path({type:'LineString',coordinates:c})).join(' ');else{const ps=g.type==='Polygon'?[g.coordinates]:g.type==='MultiPolygon'?g.coordinates:[];value=ps.map(p=>p.map(r=>path({type:'LineString',coordinates:r})+'Z').join(' ')).join(' ')}geometryPaths.set(g,{projection:project.value,value});return value}
const basins=computed(()=>features.value.filter(f=>f.properties.kind==='basin'))
const displayedBasinIds=new Set(['zajia','zagen','boqu','ali','fringe'])
const businessName=f=>f.id==='fringe'?'湖周直接入湖区':f.name
const namedBasins=computed(()=>basins.value.filter(f=>displayedBasinIds.has(f.properties.id)))
const lake=computed(()=>features.value.filter(f=>f.properties.kind==='lake'))
const currentLayer=computed(()=>props.variable==='terrain'&&terrain.value?{...layers.find(l=>l.id==='terrain'),stops:terrain.value.stops,colors:terrain.value.colors}:props.rangeLayer||props.stateLayer||layers.find(l=>l.id===props.variable)||layers[0])
const modelRain=computed(()=>props.variable==='precipitation'&&props.frame?.displaySupport==='model-units-and-lake')
const modelValues=computed(()=>new Map((props.frame?.unitValues||[]).map(u=>[u.id,u.value])))
const lakeFill=computed(()=>props.lakeState?colorFor(props.lakeState.value,currentLayer.value):modelRain.value&&props.frame?.complete?colorFor(props.frame.lakeMean,currentLayer.value):undefined)
const snowIndex=computed(()=>new Map((props.snow?.units||[]).map(u=>[u.id,u.value])))
const geometryReady=computed(()=>loadedBase.value===props.spatialBase)
const stationPriority={'SLC':100,'SLC-N':98,'SLC-E':97,'SLC-S':96,'SLC-W':95,'DUJA':99}
const stationRadius=computed(()=>presentation.value?.symbols?.stationRadius||4)
const positioned=computed(()=>(props.stations||[]).filter(s=>s.located).map(s=>{
 const config=presentation.value?.stations?.find(l=>l.id===s.id),coordinate=config?.coordinate||config?.anchor
 return {...s,...(coordinate?{lon:coordinate[0],lat:coordinate[1]}:{}),priority:config?.priority??stationPriority[s.id]??80,preferredOffset:config?.preferredOffset||[34,-17],name:config?.name||s.name}
}))
const stationPoints=computed(()=>positioned.value.map(s=>{const [x,y]=screen([s.lon,s.lat]);return{...s,x,y,name:s.name.replace('色林错','')}}).filter(s=>s.x>=0&&s.y>=0&&s.x<=size.value.width&&s.y<=size.value.height))
const stationPointObstacles=computed(()=>props.showStations?stationPoints.value.map(s=>({x:s.x-8,y:s.y-8,w:16,h:16})):[])
const stationLabels=computed(()=>{
 const obstacles=[...overlayBoxes.value,...(props.showNames?labels.value.map(l=>l.box):[]),...stationPoints.value.map(s=>({x:s.x-8,y:s.y-8,w:16,h:16}))]
 return placeLabels(stationPoints.value.map(s=>({...s,width:Math.max(48,s.name.length*13+8),height:22})),size.value,obstacles).map(s=>({...s,anchor:screen([s.lon,s.lat])}))
})
const stationLabel=id=>stationLabels.value.find(s=>s.id===id)
// Geographic names own their positions. Stations are placed around them.
const geographicObstacles=computed(()=>[...overlayBoxes.value,...stationPoints.value.map(s=>({x:s.x-8,y:s.y-8,w:16,h:16}))])
const labels=computed(()=>placeLabels((presentation.value?.labels||context.value?.labels||[]).filter(l=>l.kind==='lake'||l.kind==='basin'&&displayedBasinIds.has(l.id)).map(l=>{const anchor=l.anchor||l.coordinate,geo=namedBasins.value.find(f=>f.properties.id===l.id),[x,y]=screen(anchor);return{...l,anchor,areaKm2:geo?.properties.areaKm2??l.areaKm2,name:businessName(l),x,y,width:l.kind==='lake'?78:Math.max(118,l.name.length*13),height:l.kind==='lake'||!(props.focus===l.id||zoom.value>1.8)?24:36}}).filter(l=>l.x>=0&&l.y>=0&&l.x<=size.value.width&&l.y<=size.value.height),size.value,geographicObstacles.value))
const selectionPins=computed(()=>geometryReady.value?selectedFeatures.value.flatMap(f=>(f.geometry?.type==='MultiPolygon'?f.geometry.coordinates.map(c=>({geometry:{type:'Polygon',coordinates:c}})):[f])).map(f=>geometryBounds([f])).filter(Boolean).map(b=>{const a=screen([b[0],b[3]]),z=screen([b[2],b[1]]);return{x:(a[0]+z[0])/2,y:(a[1]+z[1])/2,small:Math.max(z[0]-a[0],z[1]-a[1])<9}}).filter(p=>p.small):[])
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
function move(e){if(!drag.value)return;const p=point(e),dx=p[0]-drag.value.start[0],dy=p[1]-drag.value.start[1];if(Math.abs(dx)+Math.abs(dy)>4){moved.value=true;root.value.setPointerCapture(e.pointerId)}if(moved.value)pan.value=[drag.value.pan[0]+dx,drag.value.pan[1]+dy]}
function up(e){drag.value=null;if(root.value.hasPointerCapture(e.pointerId))root.value.releasePointerCapture(e.pointerId)}
function selectFeature(f){if(geometryReady.value&&!moved.value)emit('feature',{...f.properties,name:businessName(f.properties)})}
function selectBasin(id){const f=basins.value.find(f=>f.properties.id===id);if(f)selectFeature(f)}
// Meteorological overlays keep their own forcing data when the base is terrain or a model state.
const weatherFrame=computed(()=>['precipitation','temperature','evaporation'].includes(props.variable)?props.frame:props.gridFrame)
const weatherLayer=computed(()=>layers.find(l=>l.id===weatherFrame.value?.variable)||layers[0])
const cellValues=computed(()=>new Map((weatherFrame.value?.complete?(weatherFrame.value.cells||[]):[]).map(c=>[c.id,c.value])))
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
onBeforeUnmount(()=>{observer?.disconnect();overlayObserver?.disconnect();loadTicket++;detailTicket++})
</script>
<template><div class="screen-map" :class="{'basin-view':variable==='basin'}" :style="{'--map-label-stroke':(presentation?.symbols?.labelStrokeWidth||2)+'px','--map-station-stroke':(presentation?.symbols?.stationStrokeWidth||1)+'px'}" :data-spatial-ready="geometryReady" :data-spatial-base="loadedBase" :data-boundary-detail="zoom>=2&&!!detailedFeatures" :data-unit-detail="zoom>=2&&!!detailedUnits"><svg ref="root" :viewBox="`0 0 ${size.width} ${size.height}`" :data-zoom="zoom" role="img" aria-label="色林错全流域、四河分区及测站地图" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @wheel.prevent="focusAt($event.deltaY<0?1.15:1/1.15,point($event))"><defs><pattern id="screen-graticule" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0H0V50" fill="none" stroke="currentColor" stroke-width=".5"/></pattern><clipPath id="screen-domain"><path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="i" :d="path(f.geometry)"/></clipPath><pattern id="fringe-hatch" width="5" height="5" patternUnits="userSpaceOnUse"><path d="M0 5L5 0" stroke="var(--basin-fringe)" stroke-width=".5" opacity=".55"/></pattern></defs>
 <rect :width="size.width" :height="size.height" fill="url(#screen-graticule)" class="graticule"/>
 <g class="map-world" :transform="`translate(${camera.x},${camera.y}) scale(${camera.k})`">
  <image v-if="showTerrain||variable==='terrain'" :href="terrainImage" v-bind="terrainBox" class="terrain-image" clip-path="url(#screen-domain)"/>
  <path v-for="f in basins" :key="f.properties.id" :d="path(f.geometry)" :data-basin="f.properties.id" :class="['screen-geo','basin',f.properties.id,{'basin-background':!displayedBasinIds.has(f.properties.id),focused:focus===f.properties.id}]" fill-rule="evenodd" @click="displayedBasinIds.has(f.properties.id)&&selectFeature(f)"><title v-if="displayedBasinIds.has(f.properties.id)">{{f.properties.name}}流域 · {{f.properties.areaKm2.toFixed(1)}} km²</title></path>
  <g v-if="geometryReady&&!['basin','terrain','snow'].includes(variable)&&frame?.complete&&!modelRain" clip-path="url(#screen-domain)"><path v-for="c in frame.cells" :key="c.id" :d="path({type:'Polygon',coordinates:[[[c.lon-.05,c.lat-.05],[c.lon+.05,c.lat-.05],[c.lon+.05,c.lat+.05],[c.lon-.05,c.lat+.05],[c.lon-.05,c.lat-.05]]]})" :fill="colorFor(c.value,currentLayer)" class="grid-cell"><title>{{c.id}}：{{c.value?.toFixed(2)??'缺测'}} {{currentLayer.unit}}</title></path></g>
  <g v-if="geometryReady&&modelRain&&frame?.complete"><path v-for="f in shownUnits" :key="f.properties.id" :d="path(f.geometry)" :fill="colorFor(modelValues.get(f.properties.id),currentLayer)" class="rain-unit" fill-rule="evenodd" @click.stop="!moved&&emit('unit',f.properties.id)"><title>{{modelValues.get(f.properties.id)?.toFixed(2)??'缺测'}} mm/d</title></path></g>
  <g v-if="geometryReady&&variable==='snow'&&snow?.complete"><path v-for="f in shownUnits" :key="f.properties.id" :d="path(f.geometry)" :fill="colorFor(snowIndex.get(f.properties.id),currentLayer)" class="snow-unit" :class="{chosen:selectedUnit===f.properties.id}" fill-rule="evenodd" @click.stop="emit('unit',f.properties.id)"/></g>
  <g v-if="geometryReady&&showUnits" class="calculation-unit-edges"><path v-for="f in shownUnits" :key="f.properties.id" :d="path(f.geometry)" :class="{chosen:selectedUnit===f.properties.id}" fill-rule="evenodd" @click.stop="!moved&&emit('unit',f.properties.id)"><title>{{f.properties.id}} · {{f.properties.areaKm2.toFixed(3)}} km² · {{f.properties.partition}} · {{f.properties.elevationBandMin}}—{{f.properties.elevationBandMax}}m</title></path></g>
  <g v-if="geometryReady&&showGrid" class="meteorological-grid" :data-variable="weatherFrame?.variable" clip-path="url(#screen-domain)"><path v-for="f in gridFeatures" :key="f.properties.id" :d="path(f.geometry)" :data-grid="f.properties.id" :data-value="cellValues.get(f.properties.id)" :style="{fill:gridPaint?colorFor(cellValues.get(f.properties.id),weatherLayer):'transparent',fillOpacity:gridPaint?0.4:1}" fill-rule="evenodd" @click.stop="gridSelect(f)"><title>{{f.properties.id}} · {{weatherFrame?.variable==='precipitation'?'日降水':weatherLayer.label}} {{cellValues.get(f.properties.id)?.toFixed(2)??'缺测'}} {{weatherLayer.unit}} · 再分析格网原值</title></path></g>
  <g v-if="showRivers" class="screen-rivers"><path v-for="(f,i) in riverFeatures" :key="i" :data-river="f.properties.basinId" :class="{focused:focus===f.properties.basinId}" :d="path(f.geometry)"><title>{{f.properties.name}}</title></path></g>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='glacier'&&showIce)" :key="'g'+i" :d="path(f.geometry)" class="screen-geo glacier" @click="selectFeature(f)"/>
  <path v-for="(f,i) in lake" :key="'l'+i" :d="path(f.geometry)" class="screen-geo lake" :style="lakeFill?{fill:lakeFill}:undefined" fill-rule="evenodd" @click="selectFeature(f)"><title v-if="modelRain">湖面采用降水 {{frame?.lakeMean?.toFixed(2)}} mm/d</title></path>
  <!-- Draw boundaries after thematic raster so partitions remain visible on every layer. -->
  <template v-if="showBoundaries"><path v-for="f in namedBasins" :key="'edge'+f.properties.id" :d="path(f.geometry)" :data-boundary="f.properties.id" :class="['basin-boundary',f.properties.id]" fill-rule="evenodd"/></template>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="'d'+i" :d="path(f.geometry)" class="screen-geo domain"/>
  <path v-if="showControl" v-for="(f,i) in features.filter(f=>f.properties.kind==='control')" :key="'c'+i" :d="path(f.geometry)" class="screen-geo control" fill-rule="evenodd" @click="selectFeature(f)"><title>杜加内部控制区</title></path>
  <path v-for="f in namedBasins.filter(f=>f.properties.id===focus)" :key="'focus'+f.properties.id" :d="path(f.geometry)" class="basin-selection" fill="url(#fringe-hatch)" fill-rule="evenodd" pointer-events="none"/>
  <g v-if="geometryReady&&selectedObject" class="selected-object-layer" :data-object="selectedObject.id" :data-scope="selectedObject.scope"><path v-for="f in selectedFeatures" :key="'selected-'+f.properties.id" :d="path(f.geometry)" class="selected-object-highlight" fill-rule="evenodd"/></g>
 </g>
 <g class="selected-location-markers"><circle v-for="(pin,i) in selectionPins" :key="i" :cx="pin.x" :cy="pin.y" r="7" class="selected-object-pin"><title>微小片区位置标记，原几何面积未放大</title></circle></g>
 <g v-if="showNames" class="basin-labels"><g v-for="l in labels" :key="l.id||l.kind" :data-label="l.id||l.kind" :data-anchor="l.anchor.join(',')" :class="['basin-map-label',l.id||l.kind,{focused:focus===l.id}]" role="button" tabindex="0" :aria-label="l.kind==='lake'?'色林错':l.name+(l.id==='fringe'?'':'流域')" @click.stop="emit('feature',l)" @keydown.enter="emit('feature',l)"><path v-if="Math.hypot(l.cx-l.x,l.cy-l.y)>12" :d="`M${l.x},${l.y}L${l.cx},${l.cy}`" class="label-leader"/><circle v-if="Math.hypot(l.cx-l.x,l.cy-l.y)>12" :cx="l.x" :cy="l.y" r="2" class="label-anchor"/><text :x="l.cx" :y="l.box.y+(l.kind==='lake'?16:15)" text-anchor="middle">{{l.name}}{{l.kind==='basin'&&l.id!=='fringe'?'流域':''}}</text><text v-if="l.kind==='basin'&&l.areaKm2!=null&&(focus===l.id||zoom>1.8)" :x="l.cx" :y="l.box.y+29" text-anchor="middle" class="label-area">{{Math.round(l.areaKm2).toLocaleString()}} km²</text></g></g>
 <g v-if="showStations" v-for="s in positioned" :key="s.id" :data-station="s.id" :transform="`translate(${screen([s.lon,s.lat]).join(',')})`" :class="['screen-station',s.kind,{chosen:selected===s.id}]" @click.stop="emit('select',s.id)" @keydown.enter="emit('select',s.id)" @keydown.space.prevent="emit('select',s.id)" role="button" tabindex="0" :aria-label="'查看'+s.name"><title>{{s.name}} · {{s.id.startsWith('SLC-')?'用户GCJ-02坐标近似转换至WGS84':'已登记站点位置'}}</title><path v-if="stationLabel(s.id)" :d="`M0 0L${stationLabel(s.id).cx-stationLabel(s.id).anchor[0]},${stationLabel(s.id).cy-stationLabel(s.id).anchor[1]}`" class="label-leader"/><circle class="station-halo" :r="selected===s.id?11:8"/><path v-if="s.kind==='rain'" :d="`M0 ${-stationRadius-1}L${stationRadius+1} ${stationRadius}H${-stationRadius-1}Z`"/><rect v-else-if="s.kind==='river'" :x="-stationRadius" :y="-stationRadius" :width="2*stationRadius" :height="2*stationRadius" transform="rotate(45)"/><circle v-else :r="stationRadius"/><text v-if="stationLabel(s.id)" :x="stationLabel(s.id).cx-stationLabel(s.id).anchor[0]" :y="stationLabel(s.id).cy-stationLabel(s.id).anchor[1]+4" text-anchor="middle">{{s.name.replace('色林错','')}}</text></g>
 <text x="18" y="25" class="north-label">↑ N</text><g class="scale-bar" :transform="`translate(18,${size.height-45})`"><path :d="`M0 -5V0H${scaleKm*camera.k}V-5`"/><text x="0" y="15">{{scaleKm}} km</text></g></svg>
 <div ref="mapTools" class="screen-map-tools"><button aria-label="放大地图" @click="focusAt(1.4)">＋</button><button aria-label="缩小地图" @click="focusAt(1/1.4)">−</button><button v-if="selectedObject" :disabled="!selectedFeatures.length" @click="locateSelection">定位到{{selectedObject.scope==='unit'?'单元':'区域'}}</button><button :class="{on:target==='domain'&&zoom===1}" @click="reset()">全流域</button><button v-if="!selectedObject" :class="{on:target==='lake'&&zoom===1}" @click="reset('lake')">湖区</button></div>
 <div v-if="selectedObject" class="selected-object-caption">{{selectedObject.label}}<span v-if="!selectedFeatures.length"> · 该对象没有对应空间几何</span></div>
 <div class="screen-map-source">{{target==='selection'?'所选区域':target==='domain'?'全流域':'湖区'}} · 滚轮缩放 / 拖动平移</div>
 <div v-if="!geometryReady" class="map-status" role="status">地图资料正在更新…</div>
 <div v-if="variable==='basin'&&!showGrid" ref="legendKey" class="basin-map-key"><button v-for="f in namedBasins" :key="f.properties.id" :class="[f.properties.id,{on:focus===f.properties.id}]" @click="selectBasin(f.properties.id)"><i></i>{{businessName(f.properties)}}</button></div>
 <div ref="legendGroup" class="screen-map-legends">
  <div v-if="showGrid&&gridPaint" class="screen-map-legend weather-grid-legend"><b>格网{{weatherFrame?.variable==='precipitation'?'日降水':weatherLayer.label}} <small>{{weatherLayer.unit}}</small></b><div><span v-for="(c,i) in weatherLayer.colors" :key="c" :style="{background:c}" :title="weatherLayer.stops[i]+'以上'"></span></div><p class="legend-ticks"><span v-for="s in weatherLayer.stops" :key="s">{{s}}</span></p><p>再分析格网原值 · 空白为缺测</p></div>
  <div v-if="variable!=='basin'" class="screen-map-legend"><b>{{variable==='terrain'&&!terrain?'地形阴影':currentLayer.label}} <small>{{variable==='terrain'&&!terrain?'':currentLayer.unit}}</small></b><template v-if="variable!=='terrain'||terrain"><div><span v-for="(c,i) in currentLayer.colors" :key="c" :style="{background:c}" :title="currentLayer.stops[i]+'以上'"></span></div><p class="legend-ticks"><span v-for="s in currentLayer.stops" :key="s">{{s}}</span></p><p>{{variable==='terrain'?terrainDescription:rangeLabel||(stateLayer?'当前时刻范围':variable==='precipitation'?'含湖面订正 · 全时段统一色阶':'全时段统一色阶')}} · 最高档 ≥ {{currentLayer.stops.at(-1)}}</p><p v-if="variable!=='terrain'">空白：缺测 · 最浅档含零值</p></template></div>
 </div>
 </div></template>
