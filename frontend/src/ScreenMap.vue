<script setup>
import {ref,computed,onMounted,onBeforeUnmount} from 'vue'
import {colorFor,layers,rivers} from './dashboard.mjs'
import {projector,fitViewport,placeLabels} from './map-view.mjs'
const props=defineProps({stations:Array,selected:String,focus:String,frame:Object,snow:Object,variable:String,showIce:Boolean,showRivers:Boolean,showTerrain:Boolean,showStations:Boolean,showControl:Boolean,showNames:Boolean})
const emit=defineEmits(['select','feature','failure'])
const features=ref([]),riverFeatures=ref([]),unitFeatures=ref([]),context=ref(null),zoom=ref(1),pan=ref([0,0]),drag=ref(null),moved=ref(false),root=ref(),size=ref({width:800,height:500}),target=ref('domain')
let observer
const fallback=[87.39456665,30.05430052,92.23041647,33.33041666]
const bounds=computed(()=>context.value?.domainBounds||fallback),project=computed(()=>projector(bounds.value))
const xy=p=>project.value(p)
const lakeBounds=computed(()=>context.value?.lakeBounds||[88.523,31.504,89.369,32.136])
const fit=computed(()=>fitViewport(target.value==='lake'?lakeBounds.value:bounds.value,size.value,size.value.width<500?25:32))
// fitViewport's origin is its own bounds; translate lake focus into the shared world coordinates.
const camera=computed(()=>{const k=fit.value.scale*zoom.value;let offset=[0,0];if(target.value==='lake')offset=xy([lakeBounds.value[0],lakeBounds.value[3]]);return{k,x:fit.value.x-offset[0]*k+pan.value[0],y:fit.value.y-offset[1]*k+pan.value[1]}})
const screen=p=>{const a=xy(p),c=camera.value;return[a[0]*c.k+c.x,a[1]*c.k+c.y]}
function path(g){if(!g)return '';if(g.type==='LineString')return g.coordinates.map((c,i)=>`${i?'L':'M'}${xy(c).map(v=>v.toFixed(3)).join(',')}`).join(' ');if(g.type==='MultiLineString')return g.coordinates.map(c=>path({type:'LineString',coordinates:c})).join(' ');const ps=g.type==='Polygon'?[g.coordinates]:g.type==='MultiPolygon'?g.coordinates:[];return ps.map(p=>p.map(r=>path({type:'LineString',coordinates:r})+'Z').join(' ')).join(' ')}
const basins=computed(()=>features.value.filter(f=>f.properties.kind==='basin'))
const lake=computed(()=>features.value.filter(f=>f.properties.kind==='lake'))
const currentLayer=computed(()=>layers.find(l=>l.id===props.variable)||layers[0])
const snowIndex=computed(()=>new Map((props.snow?.units||[]).map(u=>[u.id,u.value])))
const positioned=computed(()=>(props.stations||[]).filter(s=>s.located))
const stationObstacles=computed(()=>props.showStations?positioned.value.map(s=>{const [x,y]=screen([s.lon,s.lat]);return{x:x-8,y:y-11,w:75,h:22}}):[])
const labels=computed(()=>placeLabels((context.value?.labels||[]).filter(l=>l.id!=='fringe').map(l=>{const [x,y]=screen(l.anchor);return{...l,x,y,width:l.kind==='lake'?78:118,height:l.kind==='lake'||!(props.focus===l.id||zoom.value>1.8)?24:36}}).filter(l=>l.x>=0&&l.y>=0&&l.x<=size.value.width&&l.y<=size.value.height),size.value,stationObstacles.value))
const terrainBox=computed(()=>{const b=context.value?.bounds||[87.1,29.8,92.5,33.6],p=xy([b[0],b[3]]),q=xy([b[2],b[1]]);return{x:p[0],y:p[1],width:q[0]-p[0],height:q[1]-p[1]}})
const scaleKm=computed(()=>{const max=95/camera.value.k;return [1,2,5,10,20,50,100,200].filter(k=>k<=max).at(-1)||1})
function focusAt(k,point=[size.value.width/2,size.value.height/2]){const z=Math.min(12,Math.max(1,zoom.value*k)),f=z/zoom.value,c=camera.value;const newX=point[0]-(point[0]-c.x)*f,newY=point[1]-(point[1]-c.y)*f;zoom.value=z;const base=camera.value;pan.value=[newX-base.x+pan.value[0],newY-base.y+pan.value[1]];if(z===1)pan.value=[0,0]}
function reset(to='domain'){target.value=to;zoom.value=1;pan.value=[0,0]}
function point(e){const r=root.value.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]}
function down(e){if(e.button!==0)return;drag.value={start:point(e),pan:[...pan.value]};moved.value=false}
function move(e){if(!drag.value)return;const p=point(e),dx=p[0]-drag.value.start[0],dy=p[1]-drag.value.start[1];if(Math.abs(dx)+Math.abs(dy)>4){moved.value=true;root.value.setPointerCapture(e.pointerId)}if(moved.value)pan.value=[drag.value.pan[0]+dx,drag.value.pan[1]+dy]}
function up(e){drag.value=null;if(root.value.hasPointerCapture(e.pointerId))root.value.releasePointerCapture(e.pointerId)}
function selectFeature(f){if(!moved.value)emit('feature',f.properties)}
function selectBasin(id){const f=basins.value.find(f=>f.properties.id===id);if(f)emit('feature',f.properties)}
onMounted(async()=>{observer=new ResizeObserver(([e])=>{size.value={width:e.contentRect.width,height:e.contentRect.height};reset(target.value)});observer.observe(root.value);const requests=await Promise.allSettled(['display.geojson','rivers-display.geojson','units-display.geojson','display-context.json'].map(async n=>{const r=await fetch('/spatial/'+n);if(!r.ok)throw Error(n+'加载失败');return await r.json()}));[features,riverFeatures,unitFeatures].forEach((t,i)=>{if(requests[i].status==='fulfilled')t.value=requests[i].value.features;else emit('failure',requests[i].reason.message)});if(requests[3].status==='fulfilled')context.value=requests[3].value;else emit('failure',requests[3].reason.message)})
onBeforeUnmount(()=>observer?.disconnect())
</script>
<template><div class="screen-map" :class="{'basin-view':variable==='basin'}"><svg ref="root" :viewBox="`0 0 ${size.width} ${size.height}`" role="img" aria-label="色林错全流域、四河分区及测站地图" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @wheel.prevent="focusAt($event.deltaY<0?1.15:1/1.15,point($event))"><defs><pattern id="screen-graticule" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0H0V50" fill="none" stroke="currentColor" stroke-width=".5"/></pattern><clipPath id="screen-domain"><path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="i" :d="path(f.geometry)"/></clipPath><pattern id="fringe-hatch" width="5" height="5" patternUnits="userSpaceOnUse"><path d="M0 5L5 0" stroke="var(--basin-fringe)" stroke-width=".5" opacity=".55"/></pattern></defs>
 <rect :width="size.width" :height="size.height" fill="url(#screen-graticule)" class="graticule"/>
 <g class="map-world" :transform="`translate(${camera.x},${camera.y}) scale(${camera.k})`">
  <image v-if="showTerrain" :href="'/spatial/terrain-display.png'" v-bind="terrainBox" class="terrain-image" clip-path="url(#screen-domain)"/>
  <path v-for="f in basins" :key="f.properties.id" :d="path(f.geometry)" :data-basin="f.properties.id" :class="['screen-geo','basin',f.properties.id,{focused:focus===f.properties.id}]" fill-rule="evenodd" @click="selectFeature(f)"><title>{{f.properties.name}}流域 · {{f.properties.areaKm2.toFixed(1)}} km²</title></path>
  <g v-if="variable!=='basin'&&variable!=='snow'&&frame?.complete" clip-path="url(#screen-domain)"><path v-for="c in frame.cells" :key="c.id" :d="path({type:'Polygon',coordinates:[[[c.lon-.05,c.lat-.05],[c.lon+.05,c.lat-.05],[c.lon+.05,c.lat+.05],[c.lon-.05,c.lat+.05],[c.lon-.05,c.lat-.05]]]})" :fill="colorFor(c.value,currentLayer)" class="grid-cell"><title>{{c.id}}：{{c.value.toFixed(2)}} {{currentLayer.unit}}</title></path></g>
  <g v-if="variable==='snow'&&snow?.complete"><path v-for="f in unitFeatures" :key="f.properties.id" :d="path(f.geometry)" :fill="colorFor(snowIndex.get(f.properties.id),currentLayer)" class="snow-unit" fill-rule="evenodd"/></g>
  <g v-if="showRivers" class="screen-rivers"><path v-for="(f,i) in riverFeatures" :key="i" :data-river="f.properties.basinId" :class="{focused:focus===f.properties.basinId}" :d="path(f.geometry)"><title>{{f.properties.name}} · 提供河网</title></path></g>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='glacier'&&showIce)" :key="'g'+i" :d="path(f.geometry)" class="screen-geo glacier" @click="selectFeature(f)"/>
  <path v-for="(f,i) in lake" :key="'l'+i" :d="path(f.geometry)" class="screen-geo lake" fill-rule="evenodd" @click="selectFeature(f)"/>
  <!-- Draw boundaries after thematic raster so partitions remain visible on every layer. -->
  <path v-for="f in basins" :key="'edge'+f.properties.id" :d="path(f.geometry)" :data-boundary="f.properties.id" :class="['basin-boundary',f.properties.id,{focused:focus===f.properties.id}]" fill-rule="evenodd"/>
  <path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="'d'+i" :d="path(f.geometry)" class="screen-geo domain"/>
  <path v-if="showControl" v-for="(f,i) in features.filter(f=>f.properties.kind==='control')" :key="'c'+i" :d="path(f.geometry)" class="screen-geo control" fill-rule="evenodd" @click="selectFeature(f)"><title>杜加内部控制区</title></path>
 </g>
 <g v-if="showNames" class="basin-labels"><g v-for="l in labels" :key="l.id||l.kind" :data-label="l.id||l.kind" :data-anchor="l.anchor.join(',')" :class="['basin-map-label',l.id||l.kind,{focused:focus===l.id}]" role="button" tabindex="0" :aria-label="l.kind==='lake'?'色林错':l.name+'流域'" @click.stop="emit('feature',l)" @keydown.enter="emit('feature',l)"><path v-if="Math.hypot(l.cx-l.x,l.cy-l.y)>12" :d="`M${l.x},${l.y}L${l.cx},${l.cy}`" class="label-leader"/><circle v-if="Math.hypot(l.cx-l.x,l.cy-l.y)>12" :cx="l.x" :cy="l.y" r="2" class="label-anchor"/><text :x="l.cx" :y="l.box.y+(l.kind==='lake'?16:15)" text-anchor="middle">{{l.name}}{{l.kind==='basin'?'流域':''}}</text><text v-if="l.kind==='basin'&&(focus===l.id||zoom>1.8)" :x="l.cx" :y="l.box.y+29" text-anchor="middle" class="label-area">{{Math.round(l.areaKm2).toLocaleString()}} km²</text></g></g>
 <g v-if="showStations" v-for="s in positioned" :key="s.id" :transform="`translate(${screen([s.lon,s.lat]).join(',')})`" :class="['screen-station',s.kind,{chosen:selected===s.id}]" @click.stop="emit('select',s.id)" @keydown.enter="emit('select',s.id)" @keydown.space.prevent="emit('select',s.id)" role="button" tabindex="0" :aria-label="'查看'+s.name"><circle class="station-halo" :r="selected===s.id?11:8"/><path v-if="s.kind==='rain'" d="M0 -5L5 4H-5Z"/><rect v-else-if="s.kind==='river'" x="-4" y="-4" width="8" height="8" transform="rotate(45)"/><circle v-else r="4"/><text x="10" y="4">{{s.name.replace('色林错','')}}</text></g>
 <text x="18" y="25" class="north-label">↑ N</text><g class="scale-bar" :transform="`translate(18,${size.height-45})`"><path :d="`M0 -5V0H${scaleKm*camera.k}V-5`"/><text x="0" y="15">{{scaleKm}} km</text></g></svg>
 <div class="screen-map-tools"><button aria-label="放大地图" @click="focusAt(1.4)">＋</button><button aria-label="缩小地图" @click="focusAt(1/1.4)">−</button><button :class="{on:target==='domain'&&zoom===1}" @click="reset()">全流域</button><button :class="{on:target==='lake'&&zoom===1}" @click="reset('lake')">湖区</button></div>
 <div class="screen-map-source">WGS84 · {{target==='domain'?'全流域适配':'湖区适配'}} · 滚轮缩放 / 拖动平移</div>
 <div v-if="variable==='basin'" class="basin-map-key"><button v-for="r in rivers" :key="r.id" :class="[r.id,{on:focus===r.id}]" @click="selectBasin(r.id)"><i></i>{{r.name}}{{r.id==='fringe'?'':'流域'}}</button></div>
 <div v-else class="screen-map-legend"><b>{{currentLayer.label}} <small>{{currentLayer.unit}}</small></b><div><span v-for="(c,i) in currentLayer.colors" :key="c" :style="{background:c}" :title="currentLayer.stops[i]+'以上'"></span></div><p><span>{{currentLayer.stops[0]}}</span><span>{{currentLayer.stops.at(-1)}}+</span></p></div>
 </div></template>
