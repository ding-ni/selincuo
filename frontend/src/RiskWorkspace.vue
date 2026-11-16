<script setup>
import {computed,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue'
import Chart from './Chart.vue'
import OverflowScene from './OverflowScene.vue'
import OverflowMap2D from './OverflowMap2D.vue'
import {loadOverflow,bangeRise,bangeArea,DEPTH_CLASSES} from './overflow-data.mjs'
import {api,number,fixed} from './api.mjs'
defineProps({boot:Object,busy:Boolean})
const emit=defineEmits(['refresh'])
const COLORS={blue:'#2d7fc1',yellow:'#c9a21a',orange:'#d9771f',red:'#c0392b'},NAMES={blue:'蓝色',yellow:'黄色',orange:'橙色',red:'红色'}
const mode=ref('replay'),view=ref('3d'),preset=ref('overview'),risk=ref(true),data=ref(null),error=ref(''),notice=ref(''),index=ref(0),playing=ref(false),speed=ref(10)
const forecasts=ref([]),rules=ref(null),result=ref(null),pending=ref(null),overlay=ref(null),choice=ref(0),exposures=ref([]),working=ref(false)
const form=reactive({forecastId:'',initialLevelOffsetM:0,outlet:null,alternatives:[],exposureIds:[],scenarioId:''})
let timer,poller
onMounted(async()=>{try{data.value=await loadOverflow();index.value=0}catch(e){error.value=e.message}
 try{const [f,r,d,c]=await Promise.all([api('forecasts'),api('warnings/rules'),api('risk-scenarios/default'),api('risk-scenarios')]);forecasts.value=(f.items||[]).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)));rules.value=r.items?.[0]||r.default;Object.assign(form,{forecastId:forecasts.value[0]?.id||'',outlet:d.outlet,alternatives:d.alternatives,defaults:d});exposures.value=c.exposures||[];const last=(c.results||[]).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)))[0];if(last)await openResult(last)}catch(e){error.value=e.message}
 poller=setInterval(poll,2000)})
onBeforeUnmount(()=>{clearInterval(timer);clearInterval(poller)})
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
 return [['2023-09-21','色林错起溢','主站湖位 4544.65 m，湖水经东岸低鞍部进入班戈错'],['2023-09-23','溃口初宽约 60 m','此后冲刷扩宽至约 200 m'],[first(.3),'累计外泄 0.3 km³','与文献报道的外溢水量相当'],[c.peakDate,'外泄峰值 '+number(c.peakOutflowM3s,0)+' m³/s','溃口冲刷后槛高降至 '+fixed(replay.value.outlet.currentCrestM,2)+' m'],[fs.at(-1).date,'复现结束','累计外泄 '+number(fs.at(-1).volumeKm3,2)+' km³']].filter(e=>e[0])})
function grade(levels,flows){const ls=rules.value?.levels||[];let g=null;for(const l of ls){const hit=levels.some(v=>v>=l.levelM)||(l.outflowM3s!=null&&flows.some(v=>v>=l.outflowM3s));if(hit)g=l}return g}
const scenarioGrade=computed(()=>{const c=comparisons.value[choice.value];return c?grade(c.series.level,c.series.outflow):null})
async function action(fn){working.value=true;error.value='';notice.value='';try{await fn()}catch(e){error.value=e.message}finally{working.value=false}}
async function run(){await action(async()=>{const d=form.defaults,payload={...d,name:form.initialLevelOffsetM?`起报湖位抬升 ${form.initialLevelOffsetM} m`:'现状溃口',initialLevelOffsetM:Number(form.initialLevelOffsetM),outlet:{...form.outlet},alternatives:form.alternatives.map(a=>({...a})),exposureIds:form.exposureIds}
 const scenario=await api('risk-scenarios'+(form.scenarioId?'/'+form.scenarioId:''),payload);form.scenarioId=scenario.id
 pending.value=await api('risk-scenarios/'+scenario.id+'/run',{forecastId:form.forecastId,clientRequestId:'risk-'+Date.now()});notice.value='正在计算漫溢情景'})}
async function poll(){if(!pending.value?.id)return;try{const t=await api('tasks/'+pending.value.id);pending.value=t;if(t.status==='succeeded'){pending.value=null;await openResult(t.result);notice.value='计算完成';emit('refresh')}else if(['failed','cancelled','interrupted'].includes(t.status)){pending.value=null;error.value=t.message}}catch(e){error.value=e.message}}
async function openResult(value){result.value=value;choice.value=0;overlay.value=null
 const levels=(value.frameRows||[]).map(i=>value.rows[i]?.level??-Infinity);const peak=levels.length?levels.indexOf(Math.max(...levels)):0
 try{const f=await api('risk-results/'+value.id+'/frame?index='+peak);overlay.value={bounds:f.terrain.bounds,width:f.terrain.width,height:f.terrain.height,depths:f.depths}}catch{}
 if(mode.value!=='replay')index.value=Math.max(0,frames.value.length-1)}
function addAlternative(){form.alternatives.push({name:'方案 '+(form.alternatives.length+1),...form.outlet})}
async function osm(){await action(async()=>{pending.value=await api('exposures/osm',{bbox:[88.45,31.45,89.75,32.2],features:['roads','settlements']});notice.value='正在读取湖周道路和居民点'})}
async function exportComparison(){await action(async()=>{pending.value=await api('reports',{reportType:'comparison',riskResultId:result.value.id});notice.value='正在生成方案对比报告'})}
const time=t=>t?String(t).replace('T',' ').slice(0,16):'—'
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
   <OverflowScene v-if="view==='3d'" :data="data" :frame="frame" :risk="risk" :preset="preset"/>
   <OverflowMap2D v-else :data="data" :frame="frame" :risk="risk" :overlay="mode==='replay'?null:overlay"/>
   <div v-if="frame" class="ov-hud">
    <b class="ov-date">{{frame.date}}</b>
    <div><span>色林错湖位</span><strong>{{fixed(frame.lake,2)}}<em>m</em></strong><small v-if="frame.observed!=null">实测 {{fixed(frame.observed,2)}} m</small></div>
    <div><span>外泄流量</span><strong>{{number(frame.outflow,1)}}<em>m³/s</em></strong><small v-if="frame.width">等效宽 {{number(frame.width,0)}} m · 槛高 {{fixed(frame.crest,2)}} m</small></div>
    <div><span>累计外泄</span><strong>{{number(frame.volumeKm3,3)}}<em>km³</em></strong></div>
    <div><span>班戈错涨幅</span><strong>+{{fixed(frame.bangeRiseM,2)}}<em>m</em></strong><small>面积 {{number(frame.bangeAreaKm2,0)}} km²</small></div>
   </div>
   <div class="ov-tools">
    <div class="seg small"><button :class="{on:view==='3d'}" @click="view='3d'">三维</button><button :class="{on:view==='2d'}" @click="view='2d'">平面</button></div>
    <div v-if="view==='3d'" class="seg small"><button :class="{on:preset==='overview'}" @click="preset='overview'">全景</button><button :class="{on:preset==='outlet'}" @click="preset='outlet'">溃口</button><button :class="{on:preset==='bange'}" @click="preset='bange'">班戈错</button></div>
   </div>
   <div class="ov-legend"><b>水域与新增淹没</b><span><i style="background:#1e6896"></i>湖泊水域</span><template v-if="risk"><span v-for="c in DEPTH_CLASSES" :key="c[2]"><i :style="{background:c[1]}"></i>{{c[2]}}</span></template><span><i class="flow"></i>溃口外泄</span><label><input type="checkbox" v-model="risk">按水深分级</label></div>
   <div v-if="frames.length" class="ov-timeline">
    <button class="ov-play" @click="togglePlay" :aria-label="playing?'暂停':'播放'">{{playing?'❚❚':'▶'}}</button>
    <input type="range" min="0" :max="frames.length-1" v-model.number="index" aria-label="时间">
    <select v-model.number="speed" aria-label="播放速度"><option :value="4">慢</option><option :value="10">中</option><option :value="24">快</option></select>
   </div>
  </div>

  <aside class="ov-side">
   <template v-if="mode==='replay'&&replay">
    <h3>2023 年色林错漫溢</h3>
    <p class="ov-lead">2023 年 9 月 21 日，色林错湖水经东岸低鞍部（距东岸站 {{number(meta.outlet.distanceToEastStationKm,1)}} km）进入班戈错，溃口持续冲刷外泄。下图按逐日湖泊水量平衡复现该过程。</p>
    <ol class="ov-events"><li v-for="e in events" :key="e[0]+e[1]" :class="{on:frame?.date===e[0]}" @click="jump(e[0])"><time>{{e[0]}}</time><b>{{e[1]}}</b><span>{{e[2]}}</span></li></ol>
    <h4>模拟与报道对照</h4>
    <table class="ov-check"><thead><tr><th></th><th>模拟</th><th>报道</th></tr></thead><tbody>
     <tr><td>累计外泄（至 2024-06-30）</td><td>{{number(replay.checks.outflowTo20240630Km3,2)}} km³</td><td>约 0.3 km³</td></tr>
     <tr><td>班戈错水位上涨</td><td>{{fixed(replay.checks.bangeRiseTo20240630M,2)}} m</td><td>约 2.3 m</td></tr>
     <tr><td>班戈错面积增加</td><td>{{number(replay.checks.bangeAreaIncreasePercent20240630,0)}}%</td><td>约 18%</td></tr>
     <tr><td>色林错湖位 RMSE</td><td colspan="2">{{fixed(replay.fit.all.revised.rmseM,3)}} m（{{replay.fit.all.revised.days}} 天主站观测）</td></tr>
    </tbody></table>
    <p class="ov-refs">资料：{{replay.literature.map(r=>r.journal+' '+r.year).join('；')}}。溃口位置由 Copernicus GLO-90 最低鞍部确定。</p>
   </template>

   <template v-if="mode==='scenario'">
    <h3>预报情景推演</h3>
    <label>来水与气象<select v-model="form.forecastId"><option value="">请选择预报</option><option v-for="f in forecasts" :key="f.id" :value="f.id">{{f.name}} · {{time(f.issueTime)}}</option></select></label>
    <label>起报湖位抬升<div class="ov-range"><input type="range" min="0" max="1.5" step=".05" v-model.number="form.initialLevelOffsetM"><b>+{{fixed(form.initialLevelOffsetM,2)}} m</b></div></label>
    <details v-if="form.outlet"><summary>溃口参数</summary><div class="ov-pair"><label>槛高 m<input type="number" step=".01" v-model.number="form.outlet.crestM"></label><label>等效宽度 m<input type="number" step="1" v-model.number="form.outlet.widthM"></label></div></details>
    <details><summary>承灾体（{{form.exposureIds.length}}）</summary><label v-for="e in exposures" :key="e.id" class="ov-check-row"><input type="checkbox" :value="e.id" v-model="form.exposureIds">{{e.name}} · {{e.featureCount}} 项</label><p v-if="!exposures.length" class="muted">尚未读取湖周道路与居民点。</p><button :disabled="working||!!pending" @click="osm">读取湖周道路与居民点</button></details>
    <button class="primary ov-run" :disabled="working||!!pending||!form.forecastId" @click="run">计算漫溢情景</button>
    <div v-if="pending" class="running-strip">{{pending.message||'计算中'}}<progress :value="pending.progress||0" max="100"/></div>
    <template v-if="comparisons[0]">
     <div class="ov-grade" :style="{'--g':scenarioGrade?COLORS[scenarioGrade.color]:'#7d969e'}"><b>{{scenarioGrade?scenarioGrade.grade+' '+NAMES[scenarioGrade.color]:'未达预警'}}</b><span>{{scenarioGrade?.meaning||'湖位与外泄低于蓝色预警标准'}}</span></div>
     <dl class="ov-kpi">
      <dt>最高湖位</dt><dd>{{fixed(comparisons[0].peakLevelM,2)}} m · {{time(comparisons[0].peakTime)}}</dd>
      <dt>最大外泄</dt><dd>{{number(comparisons[0].peakTotalOutflowM3s,1)}} m³/s</dd>
      <dt>外泄总量</dt><dd>{{number(comparisons[0].totalOutflowM3/1e4,0)}} 万 m³</dd>
      <dt>班戈错涨幅</dt><dd>+{{fixed(comparisons[0].bangeRiseM,2)}} m</dd>
      <dt>湖周新增淹没</dt><dd>{{number(comparisons[0].inundatedAreaKm2,2)}} km² · 最深 {{number(comparisons[0].maxDepthM,1)}} m</dd>
      <dt>受影响对象</dt><dd>{{comparisons[0].effects.count}} 处<template v-if="comparisons[0].effects.roadLengthM"> · 道路 {{number(comparisons[0].effects.roadLengthM/1000,1)}} km</template></dd>
     </dl>
    </template>
   </template>

   <template v-if="mode==='compare'">
    <h3>应急方案比选</h3>
    <p class="ov-lead">以同一预报来水分别计算各方案的湖位、外泄与下游班戈错响应。</p>
    <div v-for="(a,i) in form.alternatives" :key="i" class="ov-alt">
     <input v-model="a.name" aria-label="方案名称">
     <div class="ov-pair three"><label>槛高 m<input type="number" step=".01" v-model.number="a.crestM"></label><label>宽度 m<input type="number" step="1" v-model.number="a.widthM"></label><label>人工泄流 m³/s<input type="number" step="1" min="0" v-model.number="a.releaseCapacityM3s"></label></div>
     <button class="link" @click="form.alternatives.splice(i,1)">移除</button>
    </div>
    <div class="actions"><button @click="addAlternative">添加方案</button><button class="primary" :disabled="working||!!pending||!form.forecastId" @click="run">计算比选</button></div>
    <div v-if="pending" class="running-strip">{{pending.message||'计算中'}}<progress :value="pending.progress||0" max="100"/></div>
   </template>
  </aside>
 </div>

 <div v-if="data&&frames.length" class="ov-charts">
  <template v-if="mode==='compare'&&compareRows.length">
   <div><h4>色林错湖位</h4><Chart :rows="compareRows" :series="compareSeries('l')" unit="m" :day="frame?.date" @day="jump" context="ov-cmp-l"/></div>
   <div><h4>溃口外泄流量</h4><Chart :rows="compareRows" :series="compareSeries('q')" unit="m³/s" zero :day="frame?.date" @day="jump" context="ov-cmp-q"/></div>
   <div><h4>班戈错涨幅</h4><Chart :rows="compareRows" :series="compareSeries('b')" unit="m" zero :day="frame?.date" @day="jump" context="ov-cmp-b"/></div>
  </template>
  <template v-else>
  <div><h4>色林错湖位</h4><Chart :rows="chartRows" :series="[{key:'lake',label:'模拟湖位'},...(mode==='replay'?[{key:'observed',label:'主站实测',observed:true}]:[])]" unit="m" :day="frame?.date" @day="jump" :context="'ov-lake-'+mode"/></div>
  <div><h4>溃口外泄流量</h4><Chart :rows="chartRows" :series="[{key:'outflow',label:'外泄流量',options:{areaStyle:{opacity:.15}}}]" unit="m³/s" zero :day="frame?.date" @day="jump" :context="'ov-q-'+mode"/></div>
  <div><h4>班戈错涨幅</h4><Chart :rows="chartRows" :series="[{key:'bange',label:'水位上涨'}]" unit="m" zero :day="frame?.date" @day="jump" :context="'ov-b-'+mode"/></div>
  </template>
 </div>

 <section v-if="mode==='compare'&&comparisons.length" class="panel ov-table">
  <div class="panel-title"><h3>方案对比</h3><button :disabled="working||!!pending" @click="exportComparison">导出对比报告</button></div>
  <div class="table-wrap"><table><thead><tr><th>方案</th><th>最高湖位 m</th><th>期末湖位 m</th><th>最大外泄 m³/s</th><th>外泄总量 万m³</th><th>班戈错涨幅 m</th><th>湖周新增淹没 km²</th><th>受影响对象</th></tr></thead>
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
.ov-stage{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:16px;margin-bottom:16px}
.ov-view{position:relative;height:max(470px,62vh);border-radius:10px;overflow:hidden;border:1px solid var(--line);background:#dfe9ec}
.ov-hud{position:absolute;left:14px;top:14px;display:grid;gap:6px;padding:12px 14px;background:#0f2e3bd9;color:#fff;border-radius:9px;min-width:200px;backdrop-filter:blur(4px);pointer-events:none}
.ov-date{font-size:19px;font-variant-numeric:tabular-nums;letter-spacing:.02em;margin-bottom:2px}
.ov-hud div{display:grid;grid-template-columns:auto 1fr;column-gap:10px;align-items:baseline}
.ov-hud span{font-size:11.5px;color:#a9c6d0;grid-row:1/3}
.ov-hud strong{font-size:17px;font-variant-numeric:tabular-nums;text-align:right}
.ov-hud em{font-style:normal;font-size:11px;color:#a9c6d0;margin-left:3px;font-weight:400}
.ov-hud small{font-size:11px;color:#9fbdc8;text-align:right;font-variant-numeric:tabular-nums}
.ov-tools{position:absolute;right:14px;top:14px;display:flex;gap:8px}
.ov-legend{position:absolute;right:14px;top:60px;display:grid;gap:4px;padding:10px 12px;background:#ffffffe6;border-radius:8px;font-size:12px;color:#355360}
.ov-legend b{font-size:12px;margin-bottom:2px}
.ov-legend span{display:flex;align-items:center;gap:7px}
.ov-legend i{width:14px;height:10px;border-radius:2px;display:inline-block}
.ov-legend i.flow{height:3px;background:#5cc7ea}
.ov-legend label{display:flex;flex-direction:row;align-items:center;gap:6px;font-size:12px;margin-top:4px}
.ov-timeline{position:absolute;left:14px;right:200px;bottom:12px;display:flex;gap:10px;align-items:center;padding:6px 10px;background:#ffffffe6;border-radius:8px}
.ov-timeline input[type=range]{flex:1;min-height:0;accent-color:#1c7885}
.ov-timeline select{width:68px;min-height:30px;padding:2px 6px;font-size:12px}
.ov-play{min-height:30px;width:38px;padding:0;border-radius:50%;background:#1c7885;color:#fff;border:0;font-size:12px}
.ov-side{border:1px solid var(--line);border-radius:10px;background:#fff;padding:18px;display:grid;gap:12px;align-content:start;overflow:auto;max-height:max(470px,62vh)}
.ov-side h3{margin:0}.ov-side h4{margin:6px 0 0;font-size:14px}
.ov-lead{font-size:13px;color:#4c6772;margin:0;line-height:1.7}
.ov-events{list-style:none;padding:0;margin:0;display:grid;gap:2px;border-left:2px solid #d5e3e7;margin-left:6px}
.ov-events li{position:relative;padding:7px 0 7px 16px;cursor:pointer;display:grid;gap:1px}
.ov-events li::before{content:'';position:absolute;left:-6px;top:12px;width:10px;height:10px;border-radius:50%;background:#fff;border:2px solid #4f9fae}
.ov-events li.on::before,.ov-events li:hover::before{background:#e0702c;border-color:#e0702c}
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
@media(max-width:1300px){.ov-stage{grid-template-columns:1fr}.ov-side{max-height:none}.ov-charts{grid-template-columns:1fr}}
</style>
