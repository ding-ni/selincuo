<script setup>
import {ref,computed,onMounted} from 'vue'
import {colorFor,layers} from './dashboard.mjs'
const props=defineProps({stations:Array,selected:String,frame:Object,snow:Object,variable:String,showIce:Boolean,showRivers:Boolean,showTerrain:Boolean,showStations:Boolean,showControl:Boolean,showNames:Boolean})
const emit=defineEmits(['select','feature','failure'])
const features=ref([]),riverFeatures=ref([]),unitFeatures=ref([]),zoom=ref(1),pan=ref([0,0]),drag=ref(null),moved=ref(false),root=ref()
const box=[87.1,29.8,92.5,33.6],cos=Math.cos(31.7*Math.PI/180),ratio=5.4*cos/3.8,W=1000,H=W/ratio
const xy=([x,y])=>[(x-box[0])/5.4*W,(box[3]-y)/3.8*H]
function path(g){if(!g)return '';if(g.type==='LineString')return g.coordinates.map((c,i)=>`${i?'L':'M'}${xy(c).map(v=>v.toFixed(2)).join(',')}`).join(' ');if(g.type==='MultiLineString')return g.coordinates.map(c=>path({type:'LineString',coordinates:c})).join(' ');const ps=g.type==='Polygon'?[g.coordinates]:g.type==='MultiPolygon'?g.coordinates:[];return ps.map(p=>p.map(r=>path({type:'LineString',coordinates:r})+'Z').join(' ')).join(' ')}
const outlines=computed(()=>features.value.filter(f=>f.properties.kind!=='glacier'||props.showIce))
const currentLayer=computed(()=>layers.find(l=>l.id===props.variable)||layers[0])
const snowIndex=computed(()=>new Map((props.snow?.units||[]).map(u=>[u.id,u.value])))
const positioned=computed(()=>(props.stations||[]).filter(s=>s.located))
const labels=[['zajia','扎加藏布',[88.65,32.55]],['zagen','扎根藏布',[91.25,32.48]],['boqu','波曲藏布',[89.17,31.15]],['ali','阿里藏布',[87.97,31.56]],['fringe','湖周补充区',[89.81,31.2]]]
function focusAt(k,point=[W/2,H/2]){const z=Math.min(5,Math.max(1,zoom.value*k));const f=z/zoom.value;pan.value=[point[0]-(point[0]-pan.value[0])*f,point[1]-(point[1]-pan.value[1])*f];zoom.value=z;if(z===1)pan.value=[0,0]}
function reset(){zoom.value=1;pan.value=[0,0]}
function point(e){const r=root.value.getBoundingClientRect();const scale=Math.min(r.width/W,r.height/H),ox=(r.width-W*scale)/2,oy=(r.height-H*scale)/2;return[(e.clientX-r.left-ox)/scale,(e.clientY-r.top-oy)/scale]}
function down(e){if(e.button!==0)return;drag.value={start:point(e),pan:[...pan.value]};moved.value=false}
function move(e){if(!drag.value)return;const p=point(e),dx=p[0]-drag.value.start[0],dy=p[1]-drag.value.start[1];if(Math.abs(dx)+Math.abs(dy)>4){moved.value=true;if(zoom.value>1)root.value.setPointerCapture(e.pointerId)}if(zoom.value>1)pan.value=[drag.value.pan[0]+dx,drag.value.pan[1]+dy]}
function up(e){drag.value=null;if(root.value.hasPointerCapture(e.pointerId))root.value.releasePointerCapture(e.pointerId)}
function selectFeature(f){if(!moved.value)emit('feature',f.properties)}
function selectStation(id){emit('select',id)}
onMounted(async()=>{const requests=await Promise.allSettled(['display.geojson','rivers-display.geojson','units-display.geojson'].map(async n=>{const r=await fetch('/spatial/'+n);if(!r.ok)throw Error(n+'加载失败');return(await r.json()).features}));[features,riverFeatures,unitFeatures].forEach((target,i)=>{if(requests[i].status==='fulfilled')target.value=requests[i].value;else emit('failure',requests[i].reason.message)})})
</script>
<template><div class="screen-map"><svg ref="root" :viewBox="`0 0 ${W} ${H}`" role="img" aria-label="色林错湖泊、四河流域、真实格网和站点交互地图" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @wheel.prevent="focusAt($event.deltaY<0?1.15:1/1.15,point($event))"><defs><pattern id="screen-graticule" width="100" height="100" patternUnits="userSpaceOnUse"><path d="M100 0H0V100" fill="none" stroke="currentColor" stroke-width=".5"/></pattern><clipPath id="screen-domain"><path v-for="(f,i) in features.filter(f=>f.properties.kind==='domain')" :key="i" :d="path(f.geometry)"/></clipPath></defs>
 <rect :width="W" :height="H" fill="url(#screen-graticule)" class="graticule"/>
 <g :transform="`translate(${pan.join(',')}) scale(${zoom})`">
  <image v-if="showTerrain" :href="'/spatial/terrain-display.png'" :width="W" :height="H" class="terrain-image" clip-path="url(#screen-domain)"/>
  <path v-for="(f,i) in outlines.filter(f=>['domain','basin'].includes(f.properties.kind))" :key="'b'+i" :d="path(f.geometry)" :class="['screen-geo',f.properties.kind,f.properties.id]" fill-rule="evenodd" @click="selectFeature(f)"><title>{{f.properties.name}}</title></path>
  <g v-if="variable!=='basin'&&variable!=='snow'&&frame?.complete" clip-path="url(#screen-domain)"><rect v-for="c in frame.cells" :key="c.id" :x="xy([c.lon-.05,c.lat+.05])[0]" :y="xy([c.lon-.05,c.lat+.05])[1]" :width="W/54" :height="H/38" :fill="colorFor(c.value,currentLayer)" class="grid-cell"><title>{{c.id}}：{{c.value.toFixed(2)}} {{currentLayer.unit}}</title></rect></g>
  <g v-if="variable==='snow'&&snow?.complete"><path v-for="f in unitFeatures" :key="f.properties.id" :d="path(f.geometry)" :fill="colorFor(snowIndex.get(f.properties.id),currentLayer)" class="snow-unit" fill-rule="evenodd"><title>{{f.properties.id}}：{{snowIndex.get(f.properties.id)?.toFixed(2)}} mm</title></path></g>
  <g v-if="showRivers" class="screen-rivers"><path v-for="(f,i) in riverFeatures" :key="i" :d="path(f.geometry)"/></g>
  <path v-for="(f,i) in outlines.filter(f=>['lake','glacier'].includes(f.properties.kind))" :key="'l'+i" :d="path(f.geometry)" :class="['screen-geo',f.properties.kind]" fill-rule="evenodd" @click="selectFeature(f)"><title>{{f.properties.name||f.properties.id}}</title></path>
  <path v-if="showControl" v-for="(f,i) in features.filter(f=>f.properties.kind==='control')" :key="'c'+i" :d="path(f.geometry)" class="screen-geo control" fill-rule="evenodd" @click="selectFeature(f)"><title>杜加内部控制区</title></path>
  <g v-if="showNames" class="screen-labels"><text v-for="l in labels" :key="l[0]" :x="xy(l[2])[0]" :y="xy(l[2])[1]">{{l[1]}}</text><text :x="xy([88.83,31.85])[0]" :y="xy([88.83,31.85])[1]" class="lake-label">色 林 错</text></g>
  <g v-if="showStations" v-for="s in positioned" :key="s.id" :transform="`translate(${xy([s.lon,s.lat]).join(',')})`" :class="['screen-station',s.kind,{chosen:selected===s.id}]" @click.stop="selectStation(s.id)" @keydown.enter="emit('select',s.id)" @keydown.space.prevent="emit('select',s.id)" role="button" :tabindex="0" :aria-label="'查看'+s.name"><circle class="station-halo" :r="selected===s.id?14:9"/><path v-if="s.kind==='rain'" d="M0 -6L6 5H-6Z"/><rect v-else-if="s.kind==='river'" x="-5" y="-5" width="10" height="10" transform="rotate(45)"/><circle v-else r="5"/><text x="12" y="4">{{s.name.replace('色林错','')}}</text><title>{{s.name}}</title></g>
 </g>
 <text x="945" y="40" class="north-label">↑ N</text><g class="scale-bar" transform="translate(35,745)"><path d="M0 -5V0H98V-5"/><text x="0" y="18">0</text><text x="70" y="18">{{(50/zoom).toFixed(0)}} km</text></g></svg>
 <div class="screen-map-tools"><button aria-label="放大地图" @click="focusAt(1.4)">＋</button><button aria-label="缩小地图" @click="focusAt(1/1.4)">−</button><button @click="reset">复位</button></div><div class="screen-map-source">WGS84 · 1 km地形与河网背景 · 气象0.1°<span>拖动平移 / 滚轮缩放</span></div>
 <div v-if="variable!=='basin'" class="screen-map-legend"><b>{{currentLayer.label}} <small>{{currentLayer.unit}}</small></b><div><span v-for="(c,i) in currentLayer.colors" :key="c" :style="{background:c}" :title="currentLayer.stops[i]+'以上'"></span></div><p><span>{{currentLayer.stops[0]}}</span><span>{{currentLayer.stops.at(-1)}}+</span></p></div>
 </div></template>
