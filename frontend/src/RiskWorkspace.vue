<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,reactive,ref,shallowRef,watch} from 'vue'
import Chart from './Chart.vue'
import OverflowScene from './OverflowScene.vue'
import OverflowMap2D from './OverflowMap2D.vue'
import {loadOverflow,bangeRise,bangeArea,DEPTH_CLASSES,waterState,loadExposures,scenarioExposures,dailyFrameIndex,paintArchivedDepth,toLake,exposureImpact,peakRoadInundation,inEast,ROAD_RULE,ROAD_STATES} from './overflow-data.mjs'
import {api,number,fixed} from './api.mjs'
const props=defineProps({boot:Object,busy:Boolean,forecastId:String})
const emit=defineEmits(['refresh','navigate'])
const COLORS={blue:'#1677ff',yellow:'#f5c400',orange:'#ff7d00',red:'#f5222d'},NAMES={blue:'蓝色',yellow:'黄色',orange:'橙色',red:'红色'}
const time=t=>t?String(t).replace('T',' ').slice(0,16):'—'
const mode=ref('replay'),view=ref('3d'),preset=ref('overview'),risk=ref(true),data=shallowRef(null),error=ref(''),notice=ref(''),index=ref(0),playing=ref(false),speed=ref(10)
const forecasts=ref([]),rules=ref(null),result=shallowRef(null),pending=ref(null),savedFrame=shallowRef(null),choice=ref(0),exposures=ref([]),working=ref(false),planMap=ref()
const form=reactive({forecastId:'',initialLevelOffsetM:0,outlet:null,alternatives:[],exposureIds:[],scenarioId:''})
let timer,poller,disposed=false
const shipped=shallowRef(null),scenarioRoads=shallowRef(null),exposureError=ref(''),frameError=ref(''),frameRequests=new Map()
onMounted(async()=>{
 const terrain=loadOverflow().then(value=>{if(disposed)return;data.value=value;return loadExposures(value.meta).then(v=>{if(!disposed)shipped.value=v})}).catch(e=>{if(!disposed)error.value=e.message})
 try{const [f,r,d,c]=await Promise.all([api('forecasts'),api('warnings/rules'),api('risk-scenarios/default'),api('risk-scenarios')]);if(disposed)return;forecasts.value=(f.items||[]).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)));rules.value=r.items?.[0]||r.default;Object.assign(form,{forecastId:forecasts.value[0]?.id||'',outlet:d.outlet,alternatives:d.alternatives,exposureIds:[...(d.exposureIds||[])],defaults:d});exposures.value=c.exposures||[];const live=new Set(forecasts.value.map(f=>f.id)),sorted=(c.results||[]).filter(r=>live.has(r.forecastId)).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)))
  if(props.forecastId){
   // Arriving from a forecast: open its scenario, computing it once if needed.
   mode.value='scenario';form.forecastId=props.forecastId
   const done=sorted.find(r=>r.forecastId===props.forecastId)
   if(done)await openResult(done);else{await run();notice.value='正在按该次预报推演漫溢情景'}
 }else if(sorted[0])openResult(sorted[0])}catch(e){if(!disposed)error.value=e.message}
 await terrain
 if(!disposed)poller=setInterval(poll,2000)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);clearInterval(poller)})
const meta=computed(()=>data.value?.meta)
const replay=computed(()=>meta.value?.replay)
// Frames shared by the 3D scene, the plan map and the HUD.
const replayFrames=computed(()=>(replay.value?.frames||[]).map(f=>({date:f.d,lake:f.H??f.Ho,observed:f.Ho,base:replay.value.frames[0].H,outflow:f.Q,volumeKm3:f.V,bangeRiseM:f.Hb,bangeAreaKm2:f.Ab,width:f.width,crest:f.crest,inflow:f.I})))
function scenarioFrames(c){if(!c?.series||!meta.value)return [];let v=0;const out=[];c.series.dates.forEach((d,i)=>{v+=(c.series.outflow[i]||0)*86400/1e9;const rise=bangeRise(meta.value,v);out.push({date:d,lake:c.series.level[i],base:result.value?.currentLevelM??c.initialLevelM,outflow:c.series.outflow[i],volumeKm3:v,bangeRiseM:rise,bangeAreaKm2:bangeArea(meta.value,rise),crest:c.outlet?.crestM,width:c.outlet?.widthM})});return out}
const comparisons=computed(()=>result.value?.comparisons||[])
const frames=computed(()=>mode.value==='replay'?replayFrames.value:scenarioFrames(comparisons.value[choice.value]))
const frame=computed(()=>frames.value[Math.min(index.value,frames.value.length-1)]||null)
watch([mode,choice],()=>{index.value=mode.value==='replay'?0:Math.max(0,frames.value.length-1);playing.value=false})
watch(playing,on=>{clearInterval(timer);if(on)timer=setInterval(()=>{if(index.value<frames.value.length-1)index.value++;else playing.value=false},1000/speed.value)})
watch(speed,()=>{if(playing.value){playing.value=false;playing.value=true}})
function jump(date){const i=frames.value.findIndex(f=>f.date===date);if(i>=0){index.value=i;playing.value=false}}
function togglePlay(){if(!playing.value&&index.value>=frames.value.length-1)index.value=0;playing.value=!playing.value}
const chartRows=computed(()=>frames.value.map(f=>({date:f.date,lake:f.lake,observed:f.observed,outflow:f.outflow,width:f.width,bange:f.bangeRiseM,volume:f.volumeKm3})))
// Comparison charts overlay every scheme on one date axis.
const compareRows=computed(()=>{const cs=comparisons.value.filter(c=>c.series);if(!cs.length||!meta.value)return [];const vol=cs.map(()=>0)
 return cs[0].series.dates.map((d,i)=>{const r={date:d};cs.forEach((c,k)=>{vol[k]+=(c.series.outflow[i]||0)*86400/1e9;r['l'+k]=c.series.level[i];r['q'+k]=c.series.outflow[i];r['b'+k]=bangeRise(meta.value,vol[k])});return r})})
const compareSeries=prefix=>comparisons.value.map((c,k)=>({key:prefix+k,label:c.name.replace(/（.*）/,'')}))
const events=computed(()=>{const fs=replayFrames.value;if(!fs.length)return [];const c=replay.value.checks,first=v=>fs.find(f=>f.volumeKm3>=v)?.date
 return [['2023-09-21','色林错起溢','主站湖位 4544.65 m，湖水经东岸低鞍部进入班戈错'],['2023-09-23','溃口初宽约 60 m','此后冲刷扩宽至约 200 m'],[first(.3),'累计出流 0.3 km³','与文献报道的外溢水量相当'],[c.peakDate,'出流洪峰 '+number(c.peakOutflowM3s,0)+' m³/s','溃口冲刷后堰顶降至 '+fixed(replay.value.outlet.currentCrestM,2)+' m'],[fs.at(-1).date,'复现结束','累计出流 '+number(fs.at(-1).volumeKm3,2)+' km³']].filter(e=>e[0])})
function grade(levels,flows){const ls=rules.value?.levels||[];let g=null;for(const l of ls){const hit=levels.some(v=>v>=l.levelM)||(l.outflowM3s!=null&&flows.some(v=>v>=l.outflowM3s));if(hit)g=l}return g}
// Process stage from the outflow trend: rising limb, recession or before onset.
const stage=computed(()=>{const fs=frames.value,i=Math.min(index.value,fs.length-1),f=fs[i];if(!f)return '';if(!(f.outflow>0))return mode.value==='replay'?'起溢前':'无出流'
 const prev=fs[Math.max(0,i-3)]?.outflow||0;return f.outflow>prev*1.02?'涨水段':f.outflow<prev*.98?'退水段':'稳定出流'})
// One shared terrain calculation for the map, its tooltip and the exposure display.
const water=computed(()=>data.value&&frame.value?waterState(data.value,mode.value==='replay'?frame.value:{...frame.value,lake:frame.value.base}):null)
const replayRoad=computed(()=>data.value&&shipped.value?peakRoadInundation(data.value,replayFrames.value,shipped.value):-1)
const scenarioId=computed(()=>mode.value!=='replay'?result.value?.id:'')
watch(scenarioId,async id=>{
 exposureError.value=''
 if(!id||scenarioRoads.value?.id===id)return
 try{
  const {items}=await api('risk-results/'+encodeURIComponent(id)+'/exposures')
  if(!disposed&&id===scenarioId.value)scenarioRoads.value={id,exposures:scenarioExposures(items)}
 }catch(e){if(!disposed&&id===scenarioId.value)exposureError.value=e.message}
})
const mapExposures=computed(()=>mode.value==='replay'?shipped.value:scenarioId.value&&scenarioRoads.value?.id===scenarioId.value?scenarioRoads.value.exposures:null)
const frameKey=computed(()=>{
 if(!result.value||!frame.value||mode.value==='replay'||choice.value!==0)return ''
 const k=dailyFrameIndex(result.value,frame.value.date)
 return k>=0?result.value.id+'/frame?index='+k:''
})
async function loadSavedFrame(key){
 frameError.value=''
 if(!key)return
 if(!frameRequests.has(key)){
  frameRequests.set(key,api('risk-results/'+key+'&depthOnly=1').then(f=>{
   const {bounds,width,height}=f.terrain
   const depth=new Float32Array(width*height)
   f.depths.forEach((r,i)=>depth.set(r,i*width))
   return {key,bounds,width,height,depth,state:Uint8Array.from(depth,d=>d>0?2:0),level:f.level,demOffsetM:f.demOffsetM,time:f.time,areaKm2:f.inundatedAreaKm2}
  }).catch(e=>{frameRequests.delete(key);throw e}))
 }
 try{const f=await frameRequests.get(key);if(!disposed&&key===frameKey.value)savedFrame.value=f}catch(e){if(!disposed&&key===frameKey.value)frameError.value=e.message}
}
watch(frameKey,loadSavedFrame)
const overlay=computed(()=>savedFrame.value?.key===frameKey.value?savedFrame.value:null)
const sceneWater=computed(()=>{
 const w=water.value,o=overlay.value
 return view.value==='3d'&&w&&o?paintArchivedDepth({...w,state:w.state.slice(),depth:w.depth.slice()},meta.value.east,o):w
})
const sceneFrame=computed(()=>{
 if(!frame.value||mode.value==='replay')return frame.value
 const o=overlay.value
 return {...frame.value,lake:o?toLake(meta.value,o.level+o.demOffsetM):frame.value.base}
})
const roadStatus=computed(()=>{
 if(!scenarioId.value||!frame.value)return ''
 if(exposureError.value)return '承灾体资料读取失败：'+exposureError.value
 if(!mapExposures.value)return '正在读取本次情景采用的道路与居民点…'
 if(!mapExposures.value.roads.length&&!mapExposures.value.settlements.length)return '本次计算未选用道路或点状承灾体。'
 if(!frameKey.value)return '当前日期或方案没有保存淹没帧。'
 if(frameError.value)return '淹没帧读取失败：'+frameError.value
 return overlay.value?'':'正在读取当前日期的淹没帧…'
})
const impact=computed(()=>{
 if(!data.value||!mapExposures.value)return null
 if(mode.value==='replay')return water.value?exposureImpact(data.value,water.value,mapExposures.value):null
 return overlay.value&&!roadStatus.value?exposureImpact(data.value,overlay.value,mapExposures.value,ROAD_RULE,overlay.value):null
})
const roadLength=computed(()=>impact.value?impact.value.roadKm+impact.value.wetRoadKm+impact.value.corridorKm:0)
const lengthLabel=km=>km<1?number(km*1000,0)+' m':number(km,2)+' km'
function locateRoad(){
 const road=impact.value?.roads[0]
 if(!road)return
 if(view.value==='3d'&&inEast(meta.value,...road.mid)){preset.value=road.mid.slice();return}
 view.value='2d';nextTick(()=>planMap.value?.reset(road.mid))
}
async function showReplayRoad(){
 if(replayRoad.value<0)return
 index.value=replayRoad.value;playing.value=false
 await nextTick();locateRoad()
}
const scenarioGrade=computed(()=>{const c=comparisons.value[choice.value];return c?grade(c.series.level,c.series.outflow):null})
async function action(fn){working.value=true;error.value='';notice.value='';try{await fn()}catch(e){error.value=e.message}finally{working.value=false}}
async function run(){await action(async()=>{const d=form.defaults,payload={...d,name:form.initialLevelOffsetM?`起报湖位抬升 ${form.initialLevelOffsetM} m`:'现状溃口',initialLevelOffsetM:Number(form.initialLevelOffsetM),outlet:{...form.outlet},alternatives:form.alternatives.map(a=>({...a})),exposureIds:form.exposureIds}
 const scenario=await api('risk-scenarios'+(form.scenarioId?'/'+form.scenarioId:''),payload);form.scenarioId=scenario.id
 pending.value=await api('risk-scenarios/'+scenario.id+'/run',{forecastId:form.forecastId,clientRequestId:'risk-'+Date.now()});notice.value='正在计算漫溢情景'})}
async function poll(){if(!pending.value?.id)return;try{const t=await api('tasks/'+pending.value.id);pending.value=t;if(t.status==='succeeded'){pending.value=null;if(t.result?.exposures){const c=await api('risk-scenarios');exposures.value=c.exposures||[];form.exposureIds=[...new Set([...form.exposureIds,...t.result.exposures.map(e=>e.id)])];notice.value='承灾体已更新';return}await openResult(t.result);notice.value='计算完成';emit('refresh')}else if(['failed','cancelled','interrupted'].includes(t.status)){pending.value=null;error.value=t.message}}catch(e){error.value=e.message}}
function openResult(value){frameRequests.clear();result.value=value;choice.value=0
 if(mode.value!=='replay')index.value=Math.max(0,frames.value.length-1)}
function addAlternative(){form.alternatives.push({name:'方案 '+(form.alternatives.length+1),...form.outlet})}
async function osm(){await action(async()=>{pending.value=await api('exposures/osm',{bbox:[88.45,31.45,89.75,32.2],features:['roads','settlements']});notice.value='正在读取湖周道路和居民点'})}
async function exportComparison(){await action(async()=>{pending.value=await api('reports',{reportType:'comparison',riskResultId:result.value.id});notice.value='正在生成方案对比报告'})}
const GRADE_HUD={'blue':'#1a3a5cd9','yellow':'#4a3800d9','orange':'#4a1e00d9','red':'#4a0000d9'}
const GRADE_BORDER=COLORS
function frameGrade(f){if(!f||!rules.value)return null;return grade([f.lake??-Infinity],[f.outflow??0])}
const currentFrameGrade=computed(()=>frameGrade(frame.value))
const hudStyle=computed(()=>({background:currentFrameGrade.value?GRADE_HUD[currentFrameGrade.value.color]:'#0f2e3bd9'}))
const replayGrades=computed(()=>mode.value==='replay'?replayFrames.value.map(f=>frameGrade(f)):[])
const replayPeakGrade=computed(()=>{const gs=replayGrades.value.filter(Boolean);if(!gs.length)return null;const order=['blue','yellow','orange','red'];return gs.reduce((best,g)=>order.indexOf(g.color)>order.indexOf(best.color)?g:best)})
const replayPeakFrame=computed(()=>{const pg=replayPeakGrade.value;if(!pg)return null;const idx=replayGrades.value.findIndex(g=>g&&g.color===pg.color);return idx>=0?replayFrames.value[idx]:null})
const timelineGradient=computed(()=>{const gs=replayGrades.value;if(!gs.length)return 'none';const STOP=COLORS;const stops=gs.map((g,i)=>`${STOP[g?.color]||'#9fb3ba'} ${(i/gs.length*100).toFixed(2)}%`);return 'linear-gradient(to right,'+stops.join(',')+')'})
</script>
<template>
<section class="ov">
 <div class="ov-head">
  <div class="seg ov-modes"><button :class="{on:mode==='replay'}" @click="mode='replay'">2023 漫溢复现</button><button :class="{on:mode==='scenario'}" @click="mode='scenario'">预报情景推演</button><button :class="{on:mode==='compare'}" @click="mode='compare'">应急方案比选</button></div>
  <p v-if="error" class="message error">{{error}}<button @click="error=''">×</button></p>
  <p v-else-if="notice" class="message success">{{notice}}<button @click="notice=''">×</button></p>
 </div>
 <div v-if="!data&&!error" class="ov-loading">正在载入地形与湖泊资料…</div>
 <div v-if="data" class="ov-stage">
  <div class="ov-view">
   <OverflowScene v-if="view==='3d'" :data="data" :frame="sceneFrame" :water="sceneWater" :risk="risk" :preset="preset" :exposures="mapExposures" :impact="impact" :archived-surface="overlay?overlay.level+overlay.demOffsetM:undefined"/>
   <OverflowMap2D v-else ref="planMap" :data="data" :frame="frame" :water="water" :risk="risk" :overlay="overlay" :exposures="mapExposures" :impact="impact"/>
   <div v-if="frame" class="ov-hud">
    <div class="ov-tile ov-tile-date" :class="currentFrameGrade?'g-'+currentFrameGrade.color:''"><b>{{frame.date}}</b><small>{{stage}}</small></div>
    <div class="ov-tile"><span>色林错湖位</span><b>{{fixed(frame.lake,2)}}<i>m</i></b><small v-if="frame.observed!=null">实测 {{fixed(frame.observed,2)}}</small></div>
    <div class="ov-tile"><span>溃口出流</span><b>{{number(frame.outflow,1)}}<i>m³/s</i></b><small v-if="frame.width">宽 {{number(frame.width,0)}} m · 堰顶 {{fixed(frame.crest,2)}}</small></div>
    <div class="ov-tile"><span>累计出流量</span><b>{{number(frame.volumeKm3,3)}}<i>km³</i></b></div>
    <div class="ov-tile"><span>班戈错涨幅</span><b>+{{fixed(frame.bangeRiseM,2)}}<i>m</i></b><small>水面 {{number(frame.bangeAreaKm2,0)}} km²</small></div>
    <div v-if="mode==='replay'" class="ov-tile"><span>新增淹没</span><b>{{number((water?.newAreaKm2||0)+(water?.bangeNewAreaKm2||0),1)}}<i>km²</i></b><small>班戈错 {{number(water?.bangeNewAreaKm2||0,1)}}</small></div>
    <div v-else-if="overlay" class="ov-tile"><span>色林错新增淹没</span><b>{{number(overlay.areaKm2,1)}}<i>km²</i></b><small>{{time(overlay.time)}} · 存档帧</small></div>
    <div v-if="impact" class="ov-tile" :class="{alert:impact.settlements.length||roadLength}"><span>道路沿线受淹</span><b>{{lengthLabel(roadLength)}}</b><small>预计断行 {{lengthLabel(impact.roadKm)}}</small></div>
   </div>
   <div v-if="currentFrameGrade" class="ov-alarm" :class="'g-'+currentFrameGrade.color"><b>{{currentFrameGrade.grade}} {{NAMES[currentFrameGrade.color]}}预警</b><span>{{frame.outflow>=(currentFrameGrade.outflowM3s??Infinity)&&frame.lake<currentFrameGrade.levelM?'溃口出流 '+number(frame.outflow,1)+' m³/s ≥ '+number(currentFrameGrade.outflowM3s,0):'湖位 '+fixed(frame.lake,2)+' m ≥ '+fixed(currentFrameGrade.levelM,2)}}</span></div>
   <div class="ov-tools">
    <div class="seg small"><button :class="{on:view==='3d'}" @click="view='3d'">三维</button><button :class="{on:view==='2d'}" @click="view='2d'">平面</button></div>
    <div v-if="view==='3d'" class="seg small"><button :class="{on:preset==='overview'}" @click="preset='overview'">全景</button><button :class="{on:preset==='outlet'}" @click="preset='outlet'">溃口</button><button :class="{on:preset==='bange'}" @click="preset='bange'">班戈错</button></div>
   </div>
   <div class="ov-legend"><b>水域与新增淹没水深</b><span><i style="background:#1e6896"></i>湖泊原水域</span><template v-if="risk"><span v-for="c in DEPTH_CLASSES" :key="c[2]"><i :style="{background:c[1]}"></i>{{c[2]}}</span></template><span><i class="flow"></i>溃口出流</span><span v-if="mapExposures?.roads.length"><i class="road"></i>道路</span><template v-if="mapExposures?.roads.length"><span v-for="[state,color,label] in ROAD_STATES" :key="state"><i class="road" :style="{background:color}"></i>{{label}}</span></template><span v-if="mapExposures?.settlements.length"><i class="settle"></i>居民点（红色为受淹）</span><label><input type="checkbox" v-model="risk">按水深分级</label></div>
   <div v-if="frames.length" class="ov-timeline">
    <button class="ov-play" @click="togglePlay" :aria-label="playing?'暂停':'播放'">{{playing?'❚❚':'▶'}}</button>
    <div class="ov-timeline-track">
     <input type="range" min="0" :max="frames.length-1" v-model.number="index" aria-label="时间">
     <div v-if="mode==='replay'&&replayGrades.length" class="ov-timeline-color" :style="{background:timelineGradient}"></div>
    </div>
    <select v-model.number="speed" aria-label="播放速度"><option :value="4">慢</option><option :value="10">中</option><option :value="24">快</option></select>
   </div>
  </div>

  <aside class="ov-side">
   <template v-if="mode==='replay'&&replay">
    <div v-if="replayPeakGrade&&replayPeakFrame" class="ov-grade ov-peak-grade" :style="{'--g':GRADE_BORDER[replayPeakGrade.color]}">
     <b>{{replayPeakGrade.grade}} {{NAMES[replayPeakGrade.color]}}预警</b>
     <span>峰值湖位 {{fixed(replayPeakFrame.lake,2)}} m · {{replayPeakFrame.date}} · 超过 2023 年起溢水位 {{fixed(replayPeakGrade.levelM,2)}} m 阈值</span>
    </div>
    <h3>2023 年色林错漫溢</h3>
    <p class="ov-lead">2023 年 9 月 21 日，色林错湖位达 4544.65 m，湖水经东岸低鞍部（距东岸站 {{number(meta.outlet.distanceToEastStationKm,1)}} km）漫溢进入班戈错，溃口持续冲刷下切、展宽。下图按逐日湖泊水量平衡与宽顶堰出流复现该过程。</p>
    <ol class="ov-events"><li v-for="(e,ei) in events" :key="e[0]+e[1]" :class="{on:frame?.date===e[0]}" @click="jump(e[0])" :style="{'--ec':GRADE_BORDER[replayGrades[replayFrames.findIndex(f=>f.date===e[0])]?.color]||'#4f9fae'}"><time>{{e[0]}}</time><b>{{e[1]}}</b><span>{{e[2]}}</span></li></ol>
    <details class="ov-more"><summary>模拟与报道对照</summary>
    <table class="ov-check"><thead><tr><th></th><th>模拟</th><th>报道</th></tr></thead><tbody>
     <tr><td>累计出流量（至 2024-06-30）</td><td>{{number(replay.checks.outflowTo20240630Km3,2)}} km³</td><td>约 0.3 km³</td></tr>
     <tr><td>班戈错水位上涨</td><td>{{fixed(replay.checks.bangeRiseTo20240630M,2)}} m</td><td>约 2.3 m</td></tr>
     <tr><td>班戈错面积增加</td><td>{{number(replay.checks.bangeAreaIncreasePercent20240630,0)}}%</td><td>约 18%</td></tr>
     <tr><td>色林错湖位 RMSE</td><td colspan="2">{{fixed(replay.fit.all.revised.rmseM,3)}} m（{{replay.fit.all.revised.days}} 天主站观测）</td></tr>
    </tbody></table>
    </details>
    <p class="ov-refs">资料：{{replay.literature.map(r=>r.journal+' '+r.year).join('；')}}。溃口位置由 Copernicus GLO-90 最低鞍部确定。淹没范围按水位与地形推求，出流按宽顶堰公式计算。</p>
   </template>

   <template v-if="mode==='scenario'">
    <h3>预报情景推演</h3>
    <div v-if="!forecasts.length" class="ov-emptycase"><b>尚无可推演的预报</b><span>先在“预测预警”中完成实况更新和一次预报，再回到这里推演湖位、溃口出流与淹没范围。</span><button class="primary" @click="emit('navigate','forecast')">前往预测预警 →</button></div>
    <label>来水与气象<select v-model="form.forecastId"><option value="">请选择预报</option><option v-for="f in forecasts" :key="f.id" :value="f.id">{{f.name}} · {{time(f.issueTime)}}</option></select></label>
    <label>起报湖位抬升<div class="ov-range"><input type="range" min="0" max="1.5" step=".05" v-model.number="form.initialLevelOffsetM"><b>+{{fixed(form.initialLevelOffsetM,2)}} m</b></div></label>
    <details v-if="form.outlet"><summary>溃口参数</summary><div class="ov-pair"><label>堰顶高程 m<input type="number" step=".01" v-model.number="form.outlet.crestM"></label><label>等效宽度 m<input type="number" step="1" v-model.number="form.outlet.widthM"></label></div></details>
    <details><summary>承灾体（{{form.exposureIds.length}}）</summary><p class="muted" style="font-size:12px;margin:4px 0 8px">读取湖周道路与居民点，可加入情景计算受影响对象估算。</p><label v-for="e in exposures" :key="e.id" class="ov-check-row"><input type="checkbox" :value="e.id" v-model="form.exposureIds">{{e.name}} · {{e.featureCount}} 项</label><p v-if="!exposures.length" class="muted">尚未读取承灾体数据。</p><button class="primary" :disabled="working||!!pending" @click="osm" style="margin-top:8px">读取湖周道路与居民点</button></details>
    <button class="primary ov-run" :disabled="working||!!pending||!form.forecastId" @click="run">计算漫溢情景</button>
    <div v-if="pending" class="running-strip">{{pending.message||'计算中'}}<progress :value="pending.progress||0" max="100"/></div>
    <template v-if="comparisons[0]">
     <div class="ov-grade" :style="{'--g':scenarioGrade?COLORS[scenarioGrade.color]:'#7d969e'}"><b>{{scenarioGrade?scenarioGrade.grade+' '+NAMES[scenarioGrade.color]:'未达预警'}}</b><span>{{scenarioGrade?.meaning||'湖位与溃口出流均低于蓝色预警标准'}}</span></div>
     <dl class="ov-kpi">
      <dt>最高湖位</dt><dd>{{fixed(comparisons[0].peakLevelM,2)}} m · {{time(comparisons[0].peakTime)}}</dd>
      <dt>最大出流</dt><dd>{{number(comparisons[0].peakTotalOutflowM3s,1)}} m³/s</dd>
      <dt>出流总量</dt><dd>{{number(comparisons[0].totalOutflowM3/1e4,0)}} 万 m³</dd>
      <dt>班戈错涨幅</dt><dd>+{{fixed(comparisons[0].bangeRiseM,2)}} m</dd>
      <dt>湖周新增淹没</dt><dd>{{number(comparisons[0].inundatedAreaKm2,2)}} km² · 最深 {{number(comparisons[0].maxDepthM,1)}} m</dd>
      <dt>受影响对象</dt><dd>{{comparisons[0].effects.count}} 处<template v-if="comparisons[0].effects.roadLengthM"> · 道路 {{number(comparisons[0].effects.roadLengthM/1000,1)}} km</template></dd>
     </dl>
    </template>
   </template>

   <template v-if="mode==='compare'">
    <h3>应急方案比选</h3>
    <p class="ov-lead">以同一预报来水分别计算各方案的湖位、溃口出流与下游班戈错响应。</p>
    <div v-for="(a,i) in form.alternatives" :key="i" class="ov-alt">
     <input v-model="a.name" aria-label="方案名称">
     <div class="ov-pair three"><label>堰顶高程 m<input type="number" step=".01" v-model.number="a.crestM"></label><label>宽度 m<input type="number" step="1" v-model.number="a.widthM"></label><label>人工泄流 m³/s<input type="number" step="1" min="0" v-model.number="a.releaseCapacityM3s"></label></div>
     <button class="link" @click="form.alternatives.splice(i,1)">移除</button>
    </div>
    <div class="actions"><button @click="addAlternative">添加方案</button><button class="primary" :disabled="working||!!pending||!form.forecastId" @click="run">计算比选</button></div>
    <div v-if="pending" class="running-strip">{{pending.message||'计算中'}}<progress :value="pending.progress||0" max="100"/></div>
   </template>
   <section v-if="impact||roadStatus" class="ov-impact" aria-label="道路与居民点">
    <h4>道路与居民点 · {{frame.date}}</h4>
    <p v-if="roadStatus" class="muted">{{roadStatus}}</p>
    <ul v-else-if="roadLength||impact.settlements.length">
     <li v-if="impact.corridorKm" class="corridor"><b>沿线受淹，未达路面</b><span>{{lengthLabel(impact.corridorKm)}}</span></li>
     <li v-if="impact.wetRoadKm" class="wet"><b>路面过水</b><span>{{lengthLabel(impact.wetRoadKm)}}</span></li>
     <li v-if="impact.roadKm" class="closed"><b>预计断行</b><span>{{lengthLabel(impact.roadKm)}}</span></li>
     <li v-for="s in impact.settlements" :key="s.id"><b>{{s.name}}</b><span>水深 {{fixed(s.depth,2)}} m</span></li>
    </ul>
    <p v-else-if="mapExposures?.roads.length||mapExposures?.settlements.length" class="muted">该时刻未识别到受淹道路或居民点。</p>
    <p v-else class="muted">道路资料未载入。</p>
    <div class="ov-road-actions"><button v-if="roadLength" @click="locateRoad">定位受淹道路</button><button v-else-if="mode==='replay'&&replayRoad>=0" @click="showReplayRoad">查看历史受淹路段</button></div>
    <p v-if="roadLength" class="ov-rule">按默认路基 {{fixed(impact.rule.embankmentM,1)}} m 估算，路面水深超过 {{fixed(impact.rule.closeDepthM,1)}} m 标为预计断行。</p>
    <small v-if="mapExposures?.source">{{mapExposures.source}}</small>
   </section>
  </aside>
 </div>

 <div v-if="data&&frames.length" class="ov-charts">
  <template v-if="mode==='compare'&&compareRows.length">
   <div><h4>色林错湖位</h4><Chart :rows="compareRows" :series="compareSeries('l')" unit="m" :day="frame?.date" @day="jump" context="ov-cmp-l"/></div>
   <div><h4>溃口出流过程</h4><Chart :rows="compareRows" :series="compareSeries('q')" unit="m³/s" zero :day="frame?.date" @day="jump" context="ov-cmp-q"/></div>
   <div><h4>班戈错涨幅</h4><Chart :rows="compareRows" :series="compareSeries('b')" unit="m" zero :day="frame?.date" @day="jump" context="ov-cmp-b"/></div>
  </template>
  <template v-else>
  <div><h4>色林错湖位</h4><Chart :rows="chartRows" :series="[{key:'lake',label:'模拟湖位'},...(mode==='replay'?[{key:'observed',label:'主站实测',observed:true}]:[])]" unit="m" :day="frame?.date" @day="jump" :context="'ov-lake-'+mode"/></div>
  <div><h4>溃口出流过程</h4><Chart :rows="chartRows" :series="[{key:'outflow',label:'溃口出流',options:{areaStyle:{opacity:.15}}}]" unit="m³/s" zero :day="frame?.date" @day="jump" :context="'ov-q-'+mode"/></div>
  <div><h4>班戈错涨幅</h4><Chart :rows="chartRows" :series="[{key:'bange',label:'水位上涨'}]" unit="m" zero :day="frame?.date" @day="jump" :context="'ov-b-'+mode"/></div>
  </template>
 </div>

 <section v-if="mode==='compare'&&comparisons.length" class="panel ov-table">
  <div class="panel-title"><h3>方案对比</h3><button :disabled="working||!!pending" @click="exportComparison">导出对比报告</button></div>
  <div class="table-wrap"><table><thead><tr><th>方案</th><th>最高湖位 m</th><th>期末湖位 m</th><th>最大出流 m³/s</th><th>出流总量 万m³</th><th>班戈错涨幅 m</th><th>湖周新增淹没 km²</th><th>受影响对象</th></tr></thead>
   <tbody><tr v-for="(c,i) in comparisons" :key="c.name" :class="{selected:choice===i}" @click="choice=i"><td>{{c.name}}</td><td>{{fixed(c.peakLevelM,3)}}</td><td>{{fixed(c.finalLevelM,3)}}</td><td>{{number(c.peakTotalOutflowM3s,1)}}</td><td>{{number(c.totalOutflowM3/1e4,0)}}</td><td>{{fixed(c.bangeRiseM,2)}}</td><td>{{number(c.inundatedAreaKm2,2)}}</td><td>{{c.effects.count}}</td></tr></tbody></table></div>
  <p class="muted">点击某一方案，在上方三维或平面视图中播放该方案过程。</p>
 </section>
</section>
</template>
<style scoped>
.ov-head{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:14px}
.ov-head .message{margin:0;flex:1}
.seg{display:inline-flex;border:1px solid #cddce3;border-radius:7px;overflow:hidden;background:#fff}
.seg button{border:0;border-radius:0;min-height:40px;padding:6px 18px;background:transparent}
.seg button+button{border-left:1px solid #cddce3}
.seg button.on{background:#1c7885;color:#fff}
.seg.small button{min-height:30px;padding:3px 11px;font-size:12.5px}
.ov-modes button{font-size:15px}
.ov-loading{padding:80px;text-align:center;color:var(--muted)}

.ov-stage{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:14px;margin-bottom:16px}
.ov-view{position:relative;height:max(520px,calc(100vh - 230px));border-radius:10px;overflow:hidden;border:1px solid var(--line);background:#dfe9ec}
.ov-hud{position:absolute;left:12px;top:12px;right:280px;display:flex;flex-wrap:wrap;gap:6px;pointer-events:none;align-items:stretch}
.ov-tile{display:grid;gap:1px;align-content:start;padding:6px 10px 7px;min-width:84px;background:#0f2e3bcc;color:#fff;border-radius:8px;backdrop-filter:blur(4px)}
.ov-tile span{font-size:11px;color:#a9c6d0}
.ov-tile b{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.ov-tile b i{font-style:normal;font-size:10.5px;color:#a9c6d0;margin-left:3px;font-weight:400}
.ov-tile small{font-size:10.5px;color:#9fbdc8;font-variant-numeric:tabular-nums;white-space:nowrap}
.ov-tile-date{transition:background .4s,color .4s}
.ov-tile-date b{font-size:17px}.ov-tile-date small{color:#d9eef4}
.ov-tile-date.g-blue{background:#1677ffe6}.ov-tile-date.g-yellow{background:#f5c400f0;color:#3a2c00}.ov-tile-date.g-yellow small{color:#5a4600}.ov-tile-date.g-orange{background:#ff7d00f0}.ov-tile-date.g-red{background:#f5222df0}
.ov-alarm{position:absolute;left:50%;bottom:66px;transform:translateX(-50%);display:flex;gap:12px;align-items:baseline;padding:7px 18px;border-radius:20px;color:#fff;box-shadow:0 4px 14px #0004;pointer-events:none;white-space:nowrap}
.ov-alarm b{font-size:15px;letter-spacing:.04em}.ov-alarm span{font-size:12.5px;font-variant-numeric:tabular-nums;opacity:.95}
.ov-alarm.g-blue{background:#1677ff}.ov-alarm.g-yellow{background:#f5c400;color:#3a2c00}.ov-alarm.g-orange{background:#ff7d00;animation:ov-beat 1.6s ease-in-out infinite}.ov-alarm.g-red{background:#f5222d;animation:ov-beat 1s ease-in-out infinite}
@keyframes ov-beat{0%,100%{box-shadow:0 4px 14px #0004,0 0 0 0 currentColor}50%{box-shadow:0 4px 14px #0004,0 0 0 8px #ffffff33}}
@media(prefers-reduced-motion:reduce){.ov-alarm{animation:none!important}}
.ov-emptycase{display:grid;gap:8px;padding:14px;border:1px dashed #c9d9de;border-radius:8px;background:#f8fbfc}.ov-emptycase span{font-size:12.5px;color:#4c6772;line-height:1.7}.ov-emptycase button{justify-self:start}
.ov-more summary{cursor:pointer;font-size:13.5px;font-weight:600;color:#1f4652;margin:4px 0}
.ov-tile.alert{background:#6e1d14d9}
.ov-impact{border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:grid;gap:6px}
.ov-impact h4{margin:0;font-size:13.5px}.ov-impact ul{list-style:none;margin:0;padding:0;display:grid;gap:3px;max-height:150px;overflow:auto}
.ov-impact li{display:flex;justify-content:space-between;font-size:12.5px}.ov-impact li b{color:#a12d1c;font-weight:600}.ov-impact small{font-size:11px;color:var(--muted)}
.ov-legend i.road{height:3px;background:#e9dfc3;border:1px solid #b5a98a}
.ov-legend i.settle{width:9px;height:9px;border-radius:50%;background:#fff;border:1.5px solid #c0321f}
.ov-tools{position:absolute;right:14px;top:14px;display:flex;gap:8px}
.ov-legend{position:absolute;right:14px;top:60px;max-width:180px;display:grid;gap:4px;padding:10px 12px;background:#ffffffe6;border-radius:8px;font-size:12px;color:#355360}
.ov-legend b{font-size:12px;margin-bottom:2px}
.ov-legend span{display:flex;align-items:center;gap:7px}
.ov-legend i{width:14px;height:10px;border-radius:2px;display:inline-block}
.ov-legend i.flow{height:3px;background:#5cc7ea}
.ov-legend label{display:flex;flex-direction:row;align-items:center;gap:6px;font-size:12px;margin-top:4px}
.ov-timeline{position:absolute;left:14px;right:200px;bottom:12px;display:flex;gap:10px;align-items:center;padding:6px 10px;background:#ffffffe6;border-radius:8px}
.ov-timeline-track{flex:1;display:grid;gap:3px}
.ov-timeline-track input[type=range]{width:100%;min-height:0;accent-color:#1c7885}
.ov-timeline-color{height:7px;border-radius:4px;pointer-events:none}
.ov-timeline select{width:68px;min-height:30px;padding:2px 6px;font-size:12px}
.ov-peak-grade{margin-bottom:4px}
.ov-play{min-height:30px;width:38px;padding:0;border-radius:50%;background:#1c7885;color:#fff;border:0;font-size:12px}
.ov-side{border:1px solid var(--line);border-radius:10px;background:#fff;padding:18px;display:grid;gap:12px;align-content:start;overflow:auto;max-height:max(520px,calc(100vh - 230px))}
.ov-side h3{margin:0}.ov-side h4{margin:6px 0 0;font-size:14px}
.ov-lead{font-size:13px;color:#4c6772;margin:0;line-height:1.7}
.ov-events{list-style:none;padding:0;margin:0;display:grid;gap:2px;border-left:2px solid #d5e3e7;margin-left:6px}
.ov-events li{position:relative;padding:7px 0 7px 16px;cursor:pointer;display:grid;gap:1px}
.ov-events li::before{content:'';position:absolute;left:-6px;top:12px;width:10px;height:10px;border-radius:50%;background:#fff;border:2px solid var(--ec,#4f9fae)}
.ov-events li.on::before,.ov-events li:hover::before{background:var(--ec,#e0702c);border-color:var(--ec,#e0702c)}
.ov-events time{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.ov-events b{font-size:13.5px;color:#1f4652}
.ov-events span{font-size:12px;color:#5b7680}
.ov-check{font-size:12.5px}.ov-check th,.ov-check td{padding:6px 8px}
.ov-refs{font-size:11.5px;color:var(--muted);margin:0}
.ov-side label{font-size:13px}
.ov-range{display:flex;align-items:center;gap:10px}.ov-range input{flex:1;min-height:0}.ov-range b{font-variant-numeric:tabular-nums;min-width:62px;text-align:right}
.ov-pair{display:grid;grid-template-columns:1fr 1fr;gap:8px}.ov-pair.three{grid-template-columns:repeat(3,1fr)}
.ov-pair input{min-height:34px;padding:4px 8px}
.ov-side details{margin:0;font-size:13px}
.ov-check-row{display:flex!important;flex-direction:row;align-items:center;gap:8px}
.ov-run{width:100%}
.ov-grade{--g:#7d969e;display:grid;gap:3px;padding:12px 14px;border-radius:8px;border-left:5px solid var(--g);background:color-mix(in srgb,var(--g) 10%,#fff)}
.ov-grade b{color:var(--g);font-size:17px}.ov-grade span{font-size:12.5px;color:#4c6772}
.ov-kpi{grid-template-columns:96px 1fr;gap:6px 10px;margin:0;font-size:13px}
.ov-alt{display:grid;gap:6px;padding:10px;border:1px solid var(--line);border-radius:8px}
.ov-alt>input{min-height:34px;padding:4px 8px;font-weight:600}
.ov-alt label{font-size:12px}
.ov-alt .link{justify-self:end;min-height:22px;font-size:12px}
.ov-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:16px}
.ov-charts>div{border:1px solid var(--line);border-radius:10px;background:#fff;padding:12px 14px 4px;min-width:0}
.ov-charts h4{margin:0 0 2px;font-size:14px}
.ov-charts :deep(.chart){height:230px}
.ov-table tr{cursor:pointer}
@media(max-width:1300px){.ov-charts{grid-template-columns:1fr}}
@media(max-width:1050px){.ov-stage{grid-template-columns:1fr}.ov-side{max-height:none}}
.ov-impact li.closed b{color:#cf1322}
.ov-impact li.wet b{color:#d46b08}
.ov-impact li.corridor b{color:#007e7b}
.ov-road-actions:empty{display:none}
.ov-rule{font-size:12px;color:var(--muted);margin:6px 0 0;line-height:1.6}
</style>
