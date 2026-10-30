<script setup>
import {ref,reactive,computed,onMounted,onBeforeUnmount,watch} from 'vue'
import ScreenMap from './ScreenMap.vue'
import ScreenChart from './ScreenChart.vue'
import MonitoringTrend from './MonitoringTrend.vue'
import LakeMark from './LakeMark.vue'
import {rivers,layers,exactValue,timelineDates,defaultReplayDay,balanceTerms,basinRain,observationDay,timelineTicks,screenResults,replayResultName,hasDashboardCapabilities,replayRows,latestObservation,monitoringWindow,recentObservations} from './dashboard.mjs'
import {api,number} from './api.mjs'
import {scaledLayer} from './map-scale.mjs'
import {createFrameCache} from './frame-cache.mjs'
import {recordMapTiming} from './map-performance.mjs'
const props=defineProps({boot:Object,tasks:Array,active:Array,selectedId:String,viewDay:String,summary:Object}),emit=defineEmits(['navigate','result','station','context'])
const mode=ref('model'),task=ref(null),monitor=ref(null),meta=ref(null),shown=ref(null),requestedDay=ref(''),variable=ref('basin'),error=ref(''),loading=ref(true),pending=ref(false),playing=ref(false),speed=ref(1200),host=ref(),expanded=ref(false),feature=ref(null)
const monitoringDays=ref(90)
const dark=ref(localStorage.getItem('selinco:screen-theme')!=='light'),show=reactive({ice:false,rivers:true,stations:true,names:true,boundaries:true,grid:false,units:false})
const viewport=ref({width:1600,height:900})
const density=computed(()=>viewport.value.height<700?'compact':viewport.value.height<900||viewport.value.width<1400?'tight':'regular')
const currentId=ref(''),choices=ref([]),candidates=computed(()=>screenResults(props.boot.publishedResults||[],props.tasks||[],props.boot.homeResultId))
const frames=createFrameCache(),ranges=new Map(),choiceChecks=new Map();let sequence=0,choiceTicket=0,choicesReady=false,timer,sliderTimer,disposed=false,resizeObserver
async function refreshChoices(){
 const ticket=++choiceTicket,homeId=props.boot.homeResultId,values=candidates.value
 const checks=values.map(async record=>{
  if(record.id===homeId)return record
  const key=[record.id,record.updated_at,record.userSaved,(props.boot.inputs||[]).map(i=>i.id).join(',')].join('|')
  if(!choiceChecks.has(key)){
   const promise=Promise.all([api('tasks/'+encodeURIComponent(record.id)),api('result-capabilities?resultId='+encodeURIComponent(record.id))])
    .then(([task,metadata])=>hasDashboardCapabilities(task,metadata,props.boot.inputs||[])).catch(()=>{choiceChecks.delete(key);return false})
   choiceChecks.set(key,promise)
  }
  return await choiceChecks.get(key)?record:null
 })
 const verified=(await Promise.all(checks)).filter(Boolean)
 if(ticket!==choiceTicket||disposed)return
 choices.value=verified
 if(choicesReady&&mode.value==='model'&&!verified.some(item=>item.id===currentId.value)){
  currentId.value=verified.find(item=>item.id===homeId)?.id||''
  if(currentId.value)await selectResult();else{mode.value='observation';await requestView(defaultDay('observation'))}
 }
}
const result=computed(()=>mode.value==='model'?task.value?.result:null),stations=computed(()=>monitor.value?.stations||[]),main=computed(()=>stations.value.find(s=>s.id==='SLC'))
const day=computed(()=>shown.value?.day||''),shownVariable=computed(()=>shown.value?.variable||'basin'),dates=computed(()=>timelineDates(mode.value,monitor.value,result.value)),index=computed(()=>Math.max(0,dates.value.indexOf(day.value)))
const current=computed(()=>result.value?.daily?.find(r=>r.date.slice(0,10)===day.value)),observed=computed(()=>mode.value==='model'?current.value?.observedLevel:exactValue(main.value,day.value)),level=computed(()=>mode.value==='model'?current.value?.lakeLevel:observed.value)
const lakeRows=computed(()=>mode.value==='model'?replayRows(result.value):main.value?.daily||[]),flowRows=computed(()=>mode.value==='model'?replayRows(result.value):stations.value.find(s=>s.id==='DUJA')?.daily||[])
const shores=computed(()=>stations.value.filter(s=>s.kind==='lake'&&s.id!=='SLC')),rain=computed(()=>shown.value?.rain)
const mainRecent=computed(()=>recentObservations(main.value,day.value))
const mainWindow=computed(()=>monitoringWindow(main.value,day.value,monitoringDays.value).filter(r=>r.value!=null))
const mainWindowChange=computed(()=>mainWindow.value.length>1?mainWindow.value.at(-1).value-mainWindow.value[0].value:null)
const rainObserved=computed(()=>main.value?.rainDaily?.find(r=>r.date===day.value)?.value)
const flowValue=computed(()=>mode.value==='model'?current.value?.total:exactValue(stations.value.find(s=>s.id==='DUJA'),day.value))
const lakeSeries=computed(()=>mode.value==='model'?[{key:'lakeLevel',label:'模拟湖位'},{key:'observedLevel',label:'主站实测',observed:true}]:[{key:'value',label:'主站实测',observed:true}])
const flowSeries=computed(()=>mode.value==='model'?[{key:'total',label:'入湖流量'},{key:'duja',label:'杜加模拟'},{key:'observedFlow',label:'杜加实测',observed:true}]:[{key:'value',label:'杜加实测',observed:true}])
const delta=computed(()=>{if(!day.value)return null;const previous=new Date(Date.parse(day.value+'T00:00:00Z')-86400000).toISOString().slice(0,10),value=mode.value==='model'?result.value?.daily?.find(r=>r.date.slice(0,10)===previous)?.lakeLevel:exactValue(main.value,previous);return value!=null&&level.value!=null?level.value-value:null})
const mapLayer=computed(()=>scaledLayer(layers.find(l=>l.id===shownVariable.value)||layers.at(-1),shownVariable.value==='snow'?(shown.value?.snow?.units||[]).map(u=>u.value):(shown.value?.frame?.cells||[]).map(c=>c.value),shown.value?.range))
const mapMissing=computed(()=>!['basin','terrain'].includes(shownVariable.value)&&!(shownVariable.value==='snow'?shown.value?.snow?.complete:shown.value?.frame?.complete))
const ticks=computed(()=>timelineTicks(dates.value))
const selectedFlow=computed(()=>rivers.map(r=>({...r,value:current.value?.[r.id]}))),balance=computed(()=>balanceTerms(current.value))
const rainfall=computed(()=>[...basinRain(rain.value,result.value?.units),{id:'lake',name:'湖面',value:rain.value?.complete?rain.value.lakeMean:null}])
const rainPeak=computed(()=>Math.max(1,...rainfall.value.map(r=>r.value||0))),flowPeak=computed(()=>Math.max(1,...selectedFlow.value.map(r=>r.value||0)))
const duja=computed(()=>stations.value.find(s=>s.id==='DUJA'))
function shoreValue(station,date){const value=observationDay(station,date);return value.value==null?({outside:'当日缺测',missing:'当日缺测',failed:'读取失败'}[value.status]||value.label):number(value.value,3)+' m'}
function defaultDay(m=mode.value,t=task.value){const a=timelineDates(m,monitor.value,t?.result);return a.includes(props.viewDay)?props.viewDay:m==='observation'?(main.value?.latest?.date||a.at(-1)||''):defaultReplayDay(a,t?.result)}
function stop(){playing.value=false;clearTimeout(timer)}
function schedule(){clearTimeout(timer);if(playing.value)timer=setTimeout(async()=>{if(index.value>=dates.value.length-1){stop();return}await requestView(dates.value[index.value+1]);schedule()},Number(speed.value))}
function play(){if(playing.value){stop();return}playing.value=true;if(index.value===dates.value.length-1)requestView(dates.value[0]).then(schedule);else schedule()}
async function rangeFor(t,m,v){
 if(['basin','terrain'].includes(v))return null
 const r=t.result,inputId=r.inputId,key=[t.id,inputId,m.spatialVersion,v,r.start,r.end].join('|')
 if(!ranges.has(key)){const promise=api('map-range?'+new URLSearchParams({inputId,resultId:t.id,spatialVersion:m.spatialVersion||r.spatialVersion,variable:v,start:r.daily[0].date.slice(0,10),end:r.daily.at(-1).date.slice(0,10)})).catch(e=>{ranges.delete(key);throw e});ranges.set(key,promise);while(ranges.size>8)ranges.delete(ranges.keys().next().value)}
 return ranges.get(key)
}
function frameFor(t,m,d,v,prefetch=false){
 const inputId=t.result.inputId,spatialVersion=m.spatialVersion||t.result.spatialVersion,key=[t.id,inputId,spatialVersion,v,d].join('|')
 return frames.get(key,async signal=>{
  const started=performance.now()
  const params={inputId,day:d,spatialVersion},options={signal}
  const requests=[api('map-frame?'+new URLSearchParams({...params,variable:'precipitation'}),null,options)]
  if(v==='snow')requests.push(api('snow-frame?'+new URLSearchParams({taskId:t.id,day:d}),null,options))
  else if(!['basin','terrain','precipitation'].includes(v))requests.push(api('map-frame?'+new URLSearchParams({...params,variable:v}),null,options))
  const values=await Promise.all(requests)
  recordMapTiming('api:'+v,started,performance.now()-started)
  return {day:d,variable:v,rain:values[0],frame:v==='precipitation'?values[0]:values[1],snow:v==='snow'?values[1]:null,range:null}
 },prefetch)
}
function prefetch(){
 if(mode.value!=='model'||!task.value)return
 for(const d of dates.value.slice(index.value+1,index.value+4))frameFor(task.value,meta.value,d,variable.value,true).catch(()=>{})
}
async function requestView(d,t=task.value,m=meta.value){
 if(!d)return
 const ticket=++sequence,v=variable.value;frames.cancelPending();requestedDay.value=d;pending.value=true;error.value=''
 try{
  const view=mode.value==='observation'?{day:d,variable:['basin','terrain'].includes(v)?v:'basin'}:await frameFor(t,m,d,v)
  if(disposed||ticket!==sequence)return
  if(mode.value==='model'){task.value=t;meta.value=m}
  shown.value=view;feature.value=null;pending.value=false;emit('context',mode.value==='model'?t.id:undefined,d);prefetch()
  // The daily frame is usable immediately. A slow first full-period scan may
  // update its scale later, but cannot replace a newer day or another layer.
  if(mode.value==='model'&&!['basin','terrain'].includes(v))rangeFor(t,m,v).then(range=>{
   if(!disposed&&ticket===sequence&&shown.value?.day===d&&shown.value?.variable===v)shown.value={...shown.value,range}
  }).catch(()=>{})
 }catch(e){if(ticket===sequence&&!disposed){pending.value=false;if(e.name!=='AbortError'){error.value=e.message;stop()}}}
}
async function selectResult(){
 stop();const id=currentId.value,ticket=++sequence;pending.value=true;error.value=''
 try{const [t,m]=await Promise.all([api((props.boot.publishedResults||[]).some(r=>r.id===id)?'published-result?id='+id:'tasks/'+id),api('result-capabilities?resultId='+id)]);if(ticket!==sequence||disposed)return;await requestView(defaultDay('model',t),t,m)}
 catch(e){if(ticket===sequence){error.value=e.message;pending.value=false}}
}
function chooseDay(d){stop();if(dates.value.includes(d))requestView(d)}
function slide(value){stop();clearTimeout(sliderTimer);const d=dates.value[Number(value)];requestedDay.value=d;sliderTimer=setTimeout(()=>requestView(d),75)}
function move(n){stop();const d=dates.value[index.value+n];if(d)requestView(d)}
function changeMode(m){stop();sequence++;mode.value=m;feature.value=null;if(m==='observation'&&!['basin','terrain'].includes(variable.value))variable.value='basin';if(m==='model')selectResult();else requestView(defaultDay('observation'))}
function changeLayer(){requestView(day.value||defaultDay())}
async function fullscreen(){if(document.fullscreenElement){await document.exitFullscreen();expanded.value=false;return}expanded.value=true;try{await host.value.requestFullscreen()}catch{}}
function close(){if(document.fullscreenElement)document.exitFullscreen();expanded.value=false}
function sync(){if(!document.fullscreenElement)expanded.value=false}
function keys(e){if(e.key==='Escape')close()}
function unit(id){const u=result.value?.units?.find(u=>u.id===id);feature.value={id,name:'计算单元',...u}}
watch(()=>candidates.value.map(t=>[t.id,t.updated_at,t.userSaved,t.trashed].join(':')).join('|')+';'+(props.boot.inputs||[]).map(i=>i.id).join(','),()=>{if(choicesReady)refreshChoices()})
watch(()=>props.selectedId,id=>{if(id&&id!==currentId.value&&mode.value==='model'&&choices.value.some(item=>item.id===id)){currentId.value=id;selectResult()}})
watch(()=>props.viewDay,value=>{if(value&&value!==day.value&&value!==requestedDay.value&&dates.value.includes(value))requestView(value)})
watch(dark,v=>localStorage.setItem('selinco:screen-theme',v?'dark':'light'))
onMounted(async()=>{resizeObserver=new ResizeObserver(([entry])=>{viewport.value={width:entry.contentRect.width,height:entry.contentRect.height}});resizeObserver.observe(host.value);document.addEventListener('fullscreenchange',sync);document.addEventListener('keydown',keys);try{const [,data]=await Promise.all([refreshChoices(),api('dashboard')]);monitor.value=data;choicesReady=true;currentId.value=choices.value.some(item=>item.id===props.selectedId)?props.selectedId:(choices.value.find(item=>item.id===props.boot.homeResultId)?.id||'');if(currentId.value)await selectResult();else{mode.value='observation';await requestView(defaultDay('observation'))}refreshChoices()}catch(e){error.value=e.message}finally{loading.value=false}})
onBeforeUnmount(()=>{disposed=true;sequence++;choiceTicket++;stop();clearTimeout(sliderTimer);frames.clear();resizeObserver?.disconnect();document.removeEventListener('fullscreenchange',sync);document.removeEventListener('keydown',keys)})
</script>
<template>
<section ref="host" class="lake-screen" :class="['screen-density-'+density,{'screen-dark':dark,'screen-expanded':expanded}]" :style="{'--screen-height':viewport.height+'px'}" :aria-busy="pending">
 <div class="screen-masthead"><div class="screen-wordmark"><LakeMark/><h2>色林错流域湖情总览</h2></div><div class="screen-header-actions"><button @click="emit('navigate','simulation')">历史模拟</button><button @click="dark=!dark">{{dark?'日常浅色':'汇报深色'}}</button><button @click="expanded?close():fullscreen()">{{expanded?'退出全屏':'全屏展示'}}</button></div></div>
 <div class="screen-context"><div class="screen-modes"><button :class="{on:mode==='model'}" :disabled="!currentId" @click="changeMode('model')">模拟回放</button><button :class="{on:mode==='observation'}" @click="changeMode('observation')">站点监测</button></div><label v-if="mode==='model'" class="screen-result-choice"><span>成果</span><select v-model="currentId" aria-label="当前成果" @change="selectResult"><option v-for="t in choices" :key="t.id" :value="t.id">{{replayResultName(t,boot.homeResultId,task)}}</option></select></label><span v-else class="monitor-clock">{{boot.builtinResultAvailable===false?boot.builtinResultMessage:'主站最近有效观测 '+(main?.latest?.date||'暂无')+' · 北京时间'}}</span><button v-if="task&&mode==='model'" class="link" @click="emit('result',task.id)">成果详情 →</button><span class="business-date">北京时间 <b>{{day||'—'}}</b></span></div>
 <div v-if="loading&&!shown" class="screen-initial" role="status">正在读取湖情资料…</div>
 <template v-else>
 <div class="screen-metrics">
  <section class="screen-panel metric-card"><span>主站湖位 <small>{{mode==='model'?'模拟':'实测'}}</small></span><b>{{number(level,3)}}<small>m</small></b><p>{{mode==='model'?'实测 '+number(observed,3)+' m':'主站工作基准'}}</p></section>
  <section class="screen-panel metric-card"><span>当日涨落</span><b :class="{'metric-rise':delta>0}">{{delta>0?'+':''}}{{number(delta==null?null:delta*100,1)}}<small>cm</small></b><p>{{delta==null?'相邻日期无完整值':'较前一日 · 相同日值口径'}}</p></section>
  <section class="screen-panel metric-card"><span>{{mode==='model'?'入湖流量':'杜加站流量'}} <small>{{mode==='model'?'模拟':'实测'}}</small></span><b>{{number(flowValue,2)}}<small>m³/s</small></b><p>{{mode==='model'?(result?.inflowComplete===false?'已判定入湖范围 · 当日平均':'当日平均入湖流量'):observationDay(duja,day).label}}</p></section>
  <section class="screen-panel metric-card"><span>{{mode==='model'?'流域日降水':'主站日降水'}}</span><b v-if="mode==='model'">{{rain?.complete?number(rain.domainMean,2):'无完整日'}}<small v-if="rain?.complete">mm</small></b><b v-else>{{number(rainObserved,2)}}<small>mm</small></b><p>{{mode==='model'?'模型采用 · 面积加权含湖面':'站点实测 · 无记录保持空白'}}</p></section>
 </div>
 <div class="screen-main">
  <aside class="screen-panel hydrology-stack" :class="{'monitoring-main':mode==='observation'}">
   <template v-if="mode==='model'">
   <section class="rain-summary"><div class="screen-panel-heading"><h3>流域雨情</h3><span>日降水 · mm</span></div><div class="rain-list"><div v-for="r in rainfall" :key="r.id" class="rain-summary-row"><span>{{r.name}}</span><b>{{number(r.value,2)}}</b><div class="hydrology-bar"><i :style="{width:((r.value||0)/rainPeak*100)+'%'}"></i></div></div></div><p class="screen-note">{{rain?.complete?'流域面雨量 · 湖面采用订正值':'当日气象资料不完整'}}</p></section>
   <section class="observation-summary"><div class="screen-panel-heading"><h3>当日实测</h3></div><div><span>主站湖位</span><b v-if="exactValue(main,day)!=null">{{number(exactValue(main,day),3)}} <small>m</small></b><small v-else>{{observationDay(main,day).label}}</small></div><div><span>杜加流量</span><b v-if="exactValue(duja,day)!=null">{{number(exactValue(duja,day),2)}} <small>m³/s</small></b><small v-else>{{observationDay(duja,day).label}}</small></div></section>
   <section class="shore-summary"><div class="screen-panel-heading"><h3>四岸观测</h3><span>原基准 · m</span></div><div class="shore-list"><button v-for="s in shores" :key="s.id" class="shore-row" :data-shore="s.id" :data-status="observationDay(s,day).status" :title="s.name+' · '+observationDay(s,day).label" @click="emit('station',s.id,day)"><span>{{s.name.replace('色林错','')}}</span><b>{{shoreValue(s,day)}}</b></button></div></section>
   </template>
   <section v-else class="monitoring-lake-summary">
    <div class="monitoring-lake-heading"><h3>主站近期湖位</h3><label><select v-model.number="monitoringDays" aria-label="湖位近期过程时长"><option :value="30">近30日</option><option :value="90">近90日</option><option :value="180">近180日</option></select></label></div>
    <div class="monitoring-lake-change"><span>时段涨落 <small>m · 主站工作基准</small></span><b>{{mainWindowChange>0?'+':''}}{{number(mainWindowChange==null?null:mainWindowChange*100,1)}} <small>cm</small></b><small v-if="mainWindowChange!=null">{{mainWindow[0].date}} 至 {{mainWindow.at(-1).date}}</small><small v-else>该时段有效记录不足</small></div>
    <MonitoringTrend :station="main" :day="day" :days="monitoringDays" :dark="dark"/>
    <div class="monitoring-period"><span>{{monitoringWindow(main,day,monitoringDays)[0]?.date?.slice(5)}}</span><span>{{day?.slice(5)}}</span></div>
    <div class="monitoring-recent"><div class="screen-panel-heading"><h3>近期读数</h3><span>实测 · m</span></div><div v-for="r in mainRecent" :key="r.date" class="monitoring-reading"><span>{{r.date}}</span><b>{{number(r.value,3)}}</b></div><small v-if="!mainRecent.length">该日期前无有效观测</small></div>
   </section>
  </aside>
  <section class="screen-panel map-panel"><div class="screen-map-heading"><h3>{{mode==='model'?'流域水情':'站点分布'}}</h3><select v-model="variable" aria-label="地图主题" @change="changeLayer"><option value="basin">流域分区</option><option value="terrain">地形高程</option><template v-if="mode==='model'"><option v-for="l in layers.filter(l=>!['basin','terrain','evaporation'].includes(l.id))" :key="l.id" :value="l.id">{{l.label}}</option></template></select><details><summary>叠加要素</summary><div class="map-overlay-options"><label v-for="(label,key) in {boundaries:'流域边界',rivers:'河网',ice:'冰川边界',stations:'测站',names:'名称',grid:'气象格网',units:'计算单元'}" :key="key"><input type="checkbox" v-model="show[key]">{{label}}</label></div></details></div>
   <div class="map-stage"><ScreenMap :spatial-base="mode==='model'?(meta?.spatialBase||'/spatial/'):'/spatial/'" :show-grid="mode==='model'&&show.grid" :show-units="mode==='model'&&show.units" :show-boundaries="show.boundaries" :range-layer="mapLayer" :stations="stations" :focus="feature?.id" :selected-unit="feature?.id" :frame="shown?.frame" :grid-frame="shown?.rain" :snow="shown?.snow" :variable="shownVariable" :show-ice="show.ice" :show-rivers="show.rivers" :show-terrain="shownVariable==='terrain'" :show-stations="show.stations" :show-names="show.names" @unit="unit" @select="emit('station',$event,day)" @feature="feature=$event" @failure="error=$event"/><div v-if="mapMissing" class="map-status">{{shownVariable==='snow'?'无日末状态':'气象资料缺少完整日'}}</div><div v-if="feature" class="feature-summary"><b>{{feature.name}}</b><span v-if="feature.areaKm2!=null">{{number(feature.areaKm2,2)}} km²</span><span v-if="feature.value!=null">{{number(feature.value,2)}} {{feature.unit}}</span><button aria-label="关闭区域信息" @click="feature=null">×</button></div></div>
  </section>
  <aside class="screen-panel summary-stack" :class="{'monitoring-side':mode==='observation'}">
   <template v-if="mode==='model'">
    <section class="inflow-summary"><div class="screen-panel-heading"><h3>入湖来水组成</h3><span>{{result?.inflowComplete===false?'已判定范围 · ':''}}m³/s</span></div><div v-for="r in selectedFlow" :key="r.id" :data-inflow="r.id" class="river-row"><i :class="r.id"></i><span>{{r.name}}</span><b>{{number(r.value,2)}}</b><div class="hydrology-bar"><i :style="{width:((r.value||0)/flowPeak*100)+'%'}"></i></div></div><div class="inflow-total"><span>{{result?.inflowComplete===false?'已判定入湖合计':'入湖合计'}}</span><b>{{number(current?.total,2)}} <small>m³/s</small></b></div></section>
    <section class="balance-summary"><div class="screen-panel-heading"><h3>当日湖泊收支</h3><span>百万 m³</span></div><div class="balance-grid"><div v-for="b in balance" :key="b.key"><span>{{b.label}}</span><b>{{number(b.value,2)}}</b></div></div></section>
   </template>
   <section v-else class="monitoring-shores"><div class="screen-panel-heading"><h3>四岸水位</h3><span>各站原基准 · m</span></div><div class="monitoring-shore-list"><article v-for="s in shores" :key="s.id" class="monitoring-shore" :data-shore="s.id" :data-status="observationDay(s,day).status"><button type="button" class="monitoring-shore-name" @click="emit('station',s.id,day)">{{s.name.replace('色林错','')}}</button><b>{{shoreValue(s,day)}}</b><small v-if="exactValue(s,day)==null&&latestObservation(s,day)">最近 {{latestObservation(s,day).date}} · {{number(latestObservation(s,day).value,3)}} m</small><small v-else>{{exactValue(s,day)!=null?day:'该日期前无有效观测'}}</small><MonitoringTrend :station="s" :day="day" :days="monitoringDays" :dark="dark" compact/></article></div></section>
  </aside>
 </div>
 <div class="screen-processes"><section class="screen-panel"><div class="screen-panel-heading"><h3>湖位过程</h3><span>m · {{mode==='model'?'连续模拟 / 主站实测':'主站实测'}}</span></div><ScreenChart :rows="lakeRows" :series="lakeSeries" unit="m" :day="day" :dark="dark" :context="(task?.id||'monitor')+':'+mode+':lake'" @day="chooseDay"/></section><section class="screen-panel"><div class="screen-panel-heading"><h3>{{mode==='model'?'入湖与杜加过程':'杜加流量过程'}}</h3><span>m³/s</span></div><ScreenChart :rows="flowRows" :series="flowSeries" unit="m³/s" :day="day" :dark="dark" :context="(task?.id||'monitor')+':'+mode+':flow'" @day="chooseDay"/></section></div>
 <div class="screen-replay"><button aria-label="前一天" :disabled="!dates.length||index===0" @click="move(-1)">‹</button><button :disabled="!dates.length" @click="play">{{playing?'暂停':'播放'}}</button><div class="timeline-track"><input aria-label="过程回放日期" type="range" min="0" :max="Math.max(0,dates.length-1)" :value="index" @input="slide($event.target.value)"><div class="timeline-ticks"><span v-for="(t,i) in ticks" :key="t.date" :class="{first:i===0,last:i===ticks.length-1}" :style="{left:t.position+'%'}" :data-date="t.date">{{t.date.slice(0,7)}}</span></div></div><button aria-label="后一天" :disabled="!dates.length||index===dates.length-1" @click="move(1)">›</button><input aria-label="选择回放日期" type="date" :value="day" :min="dates[0]" :max="dates.at(-1)" @input="chooseDay($event.target.value)"><select v-model="speed" aria-label="播放速度" @change="schedule"><option :value="2000">慢速</option><option :value="1200">常速</option><option :value="600">快速</option></select><span v-if="pending" class="frame-progress" role="status">正在读取 {{requestedDay}}</span><span v-else-if="error" class="frame-error" role="alert">{{error}}<button @click="requestView(requestedDay||day)">重试</button></span><span v-else class="frame-progress">已显示 {{day}}</span></div>
 </template>
</section>
</template>
