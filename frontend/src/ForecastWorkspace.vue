<script setup>
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import ForecastWeather from './ForecastWeather.vue'
import ForecastResult from './ForecastResult.vue'
import WarningPanel from './WarningPanel.vue'
import LakeCurvePanel from './LakeCurvePanel.vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({boot:Object,busy:Boolean})
const emit=defineEmits(['refresh','navigate','forecast'])
const tabs=[['weather','未来气象'],['run','预报计算'],['results','预报成果'],['warning','预警发布'],['curve','库容曲线']]
const tab=ref('weather'),sources=ref([]),sourceId=ref(''),issue=ref(null),inputs=ref([]),forecasts=ref([]),inputId=ref(''),startLevel=ref('model'),selected=ref(''),error=ref(''),message=ref(''),pending=ref([]),working=ref(false)
let timer,disposed=false
const source=computed(()=>sources.value.find(s=>s.id===sourceId.value))
const usableInputs=computed(()=>inputs.value.filter(v=>v.sourceResultId===sourceId.value&&v.issueTime===issue.value?.issueTime))
const chosenInput=computed(()=>inputs.value.find(v=>v.id===inputId.value))
const time=t=>t?String(t).replace('T',' ').slice(0,16):'—'
const shortDate=t=>t?String(t).slice(0,10):'—'
// The latest main-station level can start a forecast only when it was observed within 6 h before issue.
const mainObservation=computed(()=>issue.value?.latest?.SLC)
const mainUsable=computed(()=>{const o=mainObservation.value,t=issue.value?.issueTime;if(!o||!t)return false;const gap=(Date.parse(t)-Date.parse(o.time))/3600000;return gap>=0&&gap<=6})
const outlet=computed(()=>issue.value?.lakeModel?.outlet)
const outflowNow=computed(()=>{const o=outlet.value,h=issue.value?.stateLakeLevelM;if(!o||h==null)return null;const head=h-o.currentCrestM;return head>0?2/3*o.coefficient*o.currentWidthM*Math.sqrt(2*9.80665)*head**1.5:0})
async function run(fn){working.value=true;error.value='';message.value='';try{return await fn()}catch(e){error.value=e.message}finally{working.value=false}}
async function loadSources(){const s=await api('forecasts/sources');sources.value=s.items||[];if(!sources.value.some(v=>v.id===sourceId.value))sourceId.value=sources.value.find(v=>v.id===props.boot?.homeResultId)?.id||sources.value[0]?.id||''}
async function loadIssue(){issue.value=sourceId.value?await api('forecasts/issue?'+new URLSearchParams({sourceResultId:sourceId.value})):null}
async function loadLists(){const [i,f]=await Promise.all([api('forecast-inputs'),api('forecasts')]);inputs.value=(i.items||[]).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)));forecasts.value=(f.items||[]).sort((a,b)=>String(b.createdAt||b.issueTime).localeCompare(String(a.createdAt||a.issueTime)));if(!usableInputs.value.some(v=>v.id===inputId.value))inputId.value=usableInputs.value[0]?.id||'';if(!selected.value)selected.value=forecasts.value[0]?.id||''}
async function load(){await loadSources();await Promise.all([loadIssue(),loadLists()])}
watch(sourceId,()=>run(async()=>{await loadIssue();await loadLists()}))
function weatherSaved(id){inputId.value=id;run(loadLists);tab.value='run';message.value='未来气象已保存，可开始预报'}
function weatherTask(task){pending.value.push({...task,purpose:'weather'})}
function request(){return {sourceResultId:sourceId.value,inputId:inputId.value,useLatestMainStation:startLevel.value==='observed'&&mainUsable.value,name:(chosenInput.value?.name||'色林错')+' · 预报'}}
async function predict(){await run(async()=>{const check=await api('forecasts/preflight',request());if(!check.ready)throw Error(check.deficiencies.join('；'));const task=await api('forecasts',{...request(),clientRequestId:'fc-'+Date.now()});pending.value.push({...task,purpose:'forecast'});message.value='预报计算已开始'})}
async function poll(){if(!pending.value.length||disposed)return;for(const old of [...pending.value]){try{const task=await api('tasks/'+encodeURIComponent(old.id));const i=pending.value.findIndex(v=>v.id===old.id);if(i<0)continue
 if(task.status==='succeeded'){pending.value.splice(i,1);await loadLists();if(old.purpose==='forecast'){selected.value=task.result?.id||selected.value;tab.value='results';message.value='预报完成'}else{inputId.value=task.result?.id||inputId.value;message.value='集合预报已获取'}emit('refresh')}
 else if(['failed','cancelled','interrupted'].includes(task.status)){pending.value.splice(i,1);error.value=task.message||'任务未完成'}else pending.value[i]={...task,purpose:old.purpose}}catch(e){error.value=e.message}}}
onMounted(()=>{run(load);timer=setInterval(poll,2500)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer)})
</script>
<template>
<div class="fc">
 <section class="fc-status" aria-label="起报状态">
  <div class="fc-cell wide"><span>连续状态</span>
   <select v-if="sources.length>1" v-model="sourceId" aria-label="连续状态来源"><option v-for="s in sources" :key="s.id" :value="s.id">{{s.name||s.id}}</option></select>
   <b v-else>{{source?.name||'—'}}</b><small>状态末时刻 {{time(issue?.stateTime)}}</small></div>
  <div class="fc-cell"><span>起报时刻</span><b>{{issue?.issueTime?issue.issueTime.slice(5,16).replace('T',' '):'—'}}</b><small>{{issue?.issueTime?.slice(0,4)}} 年 · {{issue?.advanceHours?'已用实况推进 '+issue.advanceHours+' 小时':'与连续状态衔接'}}</small></div>
  <div class="fc-cell"><span>起报湖位</span><b>{{fixed(issue?.stateLakeLevelM,2)}}<em>m</em></b><small>湖泊模型连续状态</small></div>
  <div class="fc-cell"><span>主站实测湖位</span><b>{{fixed(mainObservation?.value,2)}}<em>m</em></b><small>{{time(mainObservation?.time)}}</small></div>
  <div class="fc-cell"><span>杜加实测流量</span><b>{{number(issue?.latest?.DUJA?.value,1)}}<em>m³/s</em></b><small>{{shortDate(issue?.latest?.DUJA?.time)}}</small></div>
  <div class="fc-cell"><span>溃口外泄</span><b>{{number(outflowNow,1)}}<em>m³/s</em></b><small>槛高 {{fixed(outlet?.currentCrestM,2)}} m</small></div>
 </section>
 <nav class="workspace-tabs"><button v-for="[id,label] in tabs" :key="id" :class="{active:tab===id}" @click="tab=id">{{label}}</button></nav>
 <p v-if="error" class="message error" role="alert">{{error}}<button @click="error=''">×</button></p>
 <p v-if="message" class="message success" role="status">{{message}}<button @click="message=''">×</button></p>
 <div v-for="task in pending" :key="task.id" class="running-strip"><b>{{task.purpose==='weather'?'获取集合预报':'预报计算'}}</b><span>{{task.message||'排队中'}}</span><progress :value="task.progress||0" max="100"/></div>

 <ForecastWeather v-show="tab==='weather'" :issue="issue" :source-id="sourceId" :busy="working||busy" @saved="weatherSaved" @task="weatherTask" @navigate="emit('navigate',$event)" @error="error=$event"/>

 <section v-if="tab==='run'" class="panel">
  <div class="panel-title"><h3>预报计算</h3></div>
  <div class="fc-run">
   <div class="fc-step"><i>1</i><div><h4>未来气象</h4>
    <select v-model="inputId" aria-label="未来气象"><option value="">请选择</option><option v-for="v in usableInputs" :key="v.id" :value="v.id">{{v.name}} · {{v.memberCount>1?v.memberCount+'个成员':'单情景'}} · {{number(v.hours/24,0)}}天</option></select>
    <p v-if="chosenInput" class="fc-hint">{{time(chosenInput.start)}} 至 {{time(chosenInput.end)}}</p>
    <p v-else class="fc-hint">尚无与本次起报衔接的未来气象，<button class="link" @click="tab='weather'">去设定</button></p></div></div>
   <div class="fc-step"><i>2</i><div><h4>起报湖位</h4>
    <label class="fc-radio"><input type="radio" v-model="startLevel" value="model"><span><b>{{fixed(issue?.stateLakeLevelM,3)}} m</b>湖泊模型连续状态</span></label>
    <label class="fc-radio" :class="{disabled:!mainUsable}"><input type="radio" v-model="startLevel" value="observed" :disabled="!mainUsable"><span><b>{{fixed(mainObservation?.value,2)}} m</b>主站实测 {{time(mainObservation?.time)}}<template v-if="!mainUsable">（距起报超过6小时，不能作为起报湖位）</template></span></label></div></div>
   <div class="fc-step"><i>3</i><div><h4>计算</h4><button class="primary" :disabled="working||busy||!inputId||pending.some(t=>t.purpose==='forecast')" @click="predict">开始预报</button>
    <p class="fc-hint">逐小时推算各子流域产汇流、杜加站流量、入湖流量与湖位；有外泄时同时计算溃口出流。</p></div></div>
  </div>
  <h4 class="fc-subhead">近期预报</h4>
  <div class="fc-list"><button v-for="f in forecasts.slice(0,8)" :key="f.id" :class="{on:selected===f.id}" @click="selected=f.id;tab='results'"><b>{{f.name}}</b><span>起报 {{time(f.issueTime)}} · {{number(f.hours/24,0)}}天 · {{f.memberCount>1?f.memberCount+'成员':'单情景'}}</span><span>最高湖位 {{fixed(f.summary?.maximumMedianLevel,3)}} m · 杜加最大 {{number(f.summary?.peakDujaM3s,1)}} m³/s</span></button><p v-if="!forecasts.length" class="empty">尚无预报成果。</p></div>
 </section>

 <ForecastResult v-if="tab==='results'" :forecasts="forecasts" v-model:selected="selected" @warning="tab='warning'"/>
 <WarningPanel v-if="tab==='warning'" :forecasts="forecasts" v-model:selected="selected"/>
 <LakeCurvePanel v-if="tab==='curve'" :level="issue?.stateLakeLevelM"/>
</div>
</template>
<style scoped>
.fc-status{display:grid;grid-template-columns:1.4fr repeat(5,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:18px}
.fc-cell{background:#fff;padding:14px 16px;display:grid;gap:3px;min-width:0}
.fc-cell span{font-size:12px;color:var(--muted)}
.fc-cell b{font-size:20px;font-weight:600;color:#245e6f;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fc-cell.wide b{font-size:16px}
.fc-cell em{font-style:normal;font-size:12px;font-weight:400;color:var(--muted);margin-left:3px}
.fc-cell small{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.fc-cell select{min-height:34px;padding:4px 8px;font-size:14px}
.fc-run{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.fc-step{display:flex;gap:14px;padding:18px;border:1px solid var(--line);border-radius:8px;background:#fbfdfd}
.fc-step>i{flex:0 0 28px;height:28px;border-radius:50%;background:#e2f0f2;color:#1c7885;font-style:normal;font-weight:600;display:grid;place-items:center}
.fc-step>div{min-width:0;flex:1;display:grid;gap:10px;align-content:start}
.fc-step h4{margin:2px 0 0}
.fc-hint{margin:0;font-size:12.5px;color:var(--muted)}
.fc-radio{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;font-size:13px;color:var(--muted)}
.fc-radio:has(input:checked){border-color:#5aa6b2;background:#f0f8f9}
.fc-radio.disabled{opacity:.6;cursor:not-allowed}
.fc-radio input{margin-top:4px}
.fc-radio b{display:block;font-size:16px;color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.fc-subhead{margin:28px 0 12px}
.fc-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.fc-list>button{display:grid;justify-items:start;gap:4px;text-align:left;padding:14px 16px}
.fc-list>button.on{border-color:#4f9fae;background:#f0f8f9}
.fc-list span{font-size:12.5px;color:var(--muted)}
@media(max-width:1250px){.fc-status{grid-template-columns:repeat(3,minmax(0,1fr))}.fc-run{grid-template-columns:1fr}}
@media(max-width:700px){.fc-status{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
