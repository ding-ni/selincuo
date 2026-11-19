<script setup>
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import ForecastWeather from './ForecastWeather.vue'
import ForecastResult from './ForecastResult.vue'
import WarningPanel from './WarningPanel.vue'
import LakeCurvePanel from './LakeCurvePanel.vue'
import StateUpdatePanel from './StateUpdatePanel.vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({boot:Object,busy:Boolean})
const emit=defineEmits(['refresh','navigate','forecast'])
const tabs=[['state','实况更新'],['weather','未来气象'],['run','预报计算'],['results','预报成果'],['warning','预警发布'],['curve','水位库容']]
const tab=ref('state'),sources=ref([]),sourceId=ref(''),issue=ref(null),inputs=ref([]),forecasts=ref([]),inputId=ref(''),startLevel=ref('model'),selected=ref(''),error=ref(''),message=ref(''),pending=ref([]),working=ref(false)
let timer,disposed=false
const source=computed(()=>sources.value.find(s=>s.id===sourceId.value))
const weatherOpened=ref(false)
watch(tab,v=>{if(v==='weather')weatherOpened.value=true})
const stateAge=computed(()=>!issue.value?(error.value?'状态读取失败':working.value?'正在读取模型状态…':'暂无可用状态'):issue.value.lagDays>0?'滞后今日 '+issue.value.lagDays+' 天':'已到今日')
const usableInputs=computed(()=>inputs.value.filter(v=>v.sourceResultId===sourceId.value&&v.issueTime===issue.value?.issueTime))
const chosenInput=computed(()=>inputs.value.find(v=>v.id===inputId.value))
const GRADE_COLORS={blue:'#1677ff',yellow:'#f5c400',orange:'#ff7d00',red:'#f5222d'}
const time=t=>t?String(t).replace('T',' ').slice(0,16):'—'
const shortDate=t=>t?String(t).slice(0,10):'—'
// The latest main-station level can start a forecast only when it was observed within 6 h before issue.
const mainObservation=computed(()=>issue.value?.latest?.SLC)
const mainUsable=computed(()=>{const o=mainObservation.value,t=issue.value?.issueTime;if(!o||!t)return false;const gap=(Date.parse(t)-Date.parse(o.time))/3600000;return gap>=0&&gap<=6})
const outlet=computed(()=>issue.value?.lakeModel?.outlet)
const headNow=computed(()=>{const o=outlet.value,h=issue.value?.stateLakeLevelM;return o&&h!=null?h-o.currentCrestM:null})
const outflowNow=computed(()=>{const o=outlet.value,h=issue.value?.stateLakeLevelM;if(!o||h==null)return null;const head=h-o.currentCrestM;return head>0?2/3*o.coefficient*o.currentWidthM*Math.sqrt(2*9.80665)*head**1.5:0})
async function run(fn){working.value=true;error.value='';message.value='';try{return await fn()}catch(e){error.value=e.message}finally{working.value=false}}
async function loadSources(){const s=await api('forecasts/sources');sources.value=s.items||[];const newest=sources.value.find(v=>v.latest)?.id||sources.value[0]?.id||'';if(sourceId.value!==newest)sourceId.value=newest}
async function loadIssue(){issue.value=sourceId.value?await api('forecasts/issue?'+new URLSearchParams({sourceResultId:sourceId.value})):null}
async function loadLists(){const [i,f]=await Promise.all([api('forecast-inputs'),api('forecasts')]);inputs.value=(i.items||[]).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)));forecasts.value=(f.items||[]).sort((a,b)=>String(b.createdAt||b.issueTime).localeCompare(String(a.createdAt||a.issueTime)));if(!usableInputs.value.some(v=>v.id===inputId.value))inputId.value=usableInputs.value[0]?.id||'';if(!selected.value)selected.value=forecasts.value[0]?.id||''}
async function load(){await loadSources();await Promise.all([loadIssue(),loadLists(),loadTrashed()]);if(!usableInputs.value.some(v=>v.id===inputId.value))inputId.value=usableInputs.value[0]?.id||'';if(issue.value&&issue.value.lagDays===0&&tab.value==='state')tab.value='weather'}
function stateUpdated(){run(load).then(()=>{tab.value='weather';message.value='模型状态已更新，可准备未来气象'})}
function weatherSaved(id){inputId.value=id;run(loadLists);tab.value='run';message.value='未来气象已保存，可开始预报'}
const trashed=ref([])
async function loadTrashed(){try{trashed.value=(await api('forecasts?trashed=1')).items||[]}catch{trashed.value=[]}}
async function recycle(f){if(!confirm('回收“'+f.name+'”？记录和文件保留，可在“已回收”中恢复。'))return;await run(async()=>{await api('forecasts/'+encodeURIComponent(f.id)+'/trash',{reason:'用户回收'});if(selected.value===f.id)selected.value='';await loadLists();await loadTrashed();message.value='已回收，可恢复'})}
async function restore(f){await run(async()=>{await api('forecasts/'+encodeURIComponent(f.id)+'/restore',{});await loadLists();await loadTrashed();message.value='已恢复'})}
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
  <div class="fc-cell wide" :class="{lag:issue?.lagDays>0}"><span>模型状态</span>
   <b>{{time(issue?.stateTime)}}</b>
   <small>{{stateAge}}<template v-if="issue?.stateForcing"> · {{issue.stateForcing}}</template></small></div>
  <div class="fc-cell"><span>起报时刻</span><b>{{issue?.issueTime?issue.issueTime.slice(5,16).replace('T',' '):'—'}}</b><small>{{issue?.issueTime?.slice(0,4)}} 年 · 状态末时刻起报</small></div>
  <div class="fc-cell"><span>起报湖位</span><b>{{fixed(issue?.stateLakeLevelM,2)}}<em>m</em></b><small>模型连续状态</small></div>
  <div class="fc-cell"><span>主站实测湖位</span><b>{{fixed(mainObservation?.value,2)}}<em>m</em></b><small>{{time(mainObservation?.time)}}</small></div>
  <div class="fc-cell"><span>杜加实测流量</span><b>{{number(issue?.latest?.DUJA?.value,1)}}<em>m³/s</em></b><small>{{shortDate(issue?.latest?.DUJA?.time)}}</small></div>
  <div class="fc-cell"><span>溃口出流</span><b>{{number(outflowNow,1)}}<em>m³/s</em></b><small>堰顶 {{fixed(outlet?.currentCrestM,2)}} m · 水头 {{headNow==null?'—':fixed(headNow,2)+' m'}}</small></div>
 </section>
 <div v-if="issue?.lagDays>0&&tab!=='state'" class="fc-gap-badge">模型状态滞后今日 {{issue.lagDays}} 天，预报将从 {{time(issue?.stateTime)}} 起报。<button class="link" @click="tab='state'">先做实况更新 →</button></div>
 <nav class="workspace-tabs"><button v-for="[id,label] in tabs" :key="id" :class="{active:tab===id}" @click="tab=id">{{label}}</button></nav>
 <p v-if="error" class="message error" role="alert">{{error}}<button @click="error=''">×</button></p>
 <p v-if="message" class="message success" role="status">{{message}}<button @click="message=''">×</button></p>
 <div v-for="task in pending" :key="task.id" class="running-strip"><b>{{task.purpose==='weather'?'获取集合预报':'预报计算'}}</b><span>{{task.message||'排队中'}}</span><progress :value="task.progress||0" max="100"/></div>

 <StateUpdatePanel v-if="tab==='state'&&issue" @updated="stateUpdated" @error="error=$event" @navigate="v=>emit('navigate',v.split(':')[0])"/>
 <ForecastWeather v-if="weatherOpened" v-show="tab==='weather'" :issue="issue" :source-id="sourceId" :busy="working||busy" @saved="weatherSaved" @task="weatherTask" @navigate="emit('navigate',$event)" @error="error=$event"/>

 <section v-if="tab==='run'" class="panel fc-sheet">
  <div class="fc-form">
   <div class="fc-row"><span>起报时刻</span><div><b>{{time(issue?.issueTime)}}</b><small>模型状态末时刻{{issue?.lagDays>0?' · 滞后今日 '+issue.lagDays+' 天':''}}</small></div></div>
   <div class="fc-row"><span>未来气象</span><div>
    <select v-model="inputId" aria-label="未来气象"><option value="">请选择</option><option v-for="v in usableInputs" :key="v.id" :value="v.id">{{v.name}} · {{v.memberCount>1?v.memberCount+'个成员':'单情景'}} · {{number(v.hours/24,0)}}天</option></select>
    <small v-if="chosenInput">{{time(chosenInput.start)}} 至 {{time(chosenInput.end)}} · 预见期 {{number(chosenInput.hours/24,0)}} 天</small>
    <button v-else class="link" @click="tab='weather'">尚无与本次起报衔接的未来气象，去准备 →</button></div></div>
   <div class="fc-row"><span>起报湖位</span><div>
    <div class="seg fc-seg"><button :class="{on:startLevel==='model'}" @click="startLevel='model'">模型状态 {{fixed(issue?.stateLakeLevelM,3)}} m</button><button :class="{on:startLevel==='observed'}" :disabled="!mainUsable" @click="startLevel='observed'">主站实测 {{mainObservation?.value!=null?fixed(mainObservation.value,2)+' m':'—'}}</button></div>
    <small>{{mainUsable?'主站实测 '+time(mainObservation?.time)+'，以实测湖位起报':'主站最近实测 '+time(mainObservation?.time)+'，距起报超过 6 小时，采用模型状态湖位'}}</small></div></div>
   <div class="fc-row"><span>计算内容</span><div><small class="fc-chain">产汇流（31 个计算单元）→ 杜加站流量 → 各河入湖流量 → 湖泊水量平衡与溃口出流 → 色林错湖位</small></div></div>
   <div class="fc-row fc-go"><span></span><div><button class="primary" :disabled="working||busy||!inputId||pending.some(t=>t.purpose==='forecast')" @click="predict">开始预报</button><small>逐小时计算，一般 1—3 分钟；完成后自动打开预报成果。</small></div></div>
  </div>
 </section>
 <section v-if="tab==='run'" class="panel">
  <div class="panel-title"><h3>近期预报</h3></div>
  <div v-if="forecasts.length" class="table-wrap"><table class="fc-table">
   <thead><tr><th></th><th>预报名称</th><th>起报时刻</th><th>预见期</th><th>最高湖位 m</th><th>杜加洪峰 m³/s</th><th>溃口出流总量 万m³</th><th></th></tr></thead>
   <tbody><tr v-for="f in forecasts.slice(0,10)" :key="f.id" :class="{on:selected===f.id}">
    <td><i class="fc-grade-dot" :style="{background:f.warning?.color?GRADE_COLORS[f.warning.color]:'#c8d8dc'}"></i></td>
    <td>{{f.name}}<small v-if="f.memberCount>1"> · {{f.memberCount}} 成员</small></td><td class="num">{{time(f.issueTime)}}</td><td class="num">{{number(f.hours/24,0)}} 天</td>
    <td class="num">{{fixed(f.summary?.maximumMedianLevel,3)}}</td><td class="num">{{number(f.summary?.peakDujaM3s,1)}}</td><td class="num">{{number((f.summary?.outflowVolumeM3||0)/1e4,0)}}</td>
    <td class="fc-acts"><button class="link" @click="selected=f.id;tab='results'">查看成果</button><button class="link" @click="emit('navigate','risk',f.id)">推演漫溢</button><button class="link muted-link" @click="recycle(f)">回收</button></td>
   </tr></tbody></table></div>
  <p v-else class="empty">尚无预报成果。先做实况更新，再准备未来气象。</p>
  <details v-if="trashed.length" class="fc-trash"><summary>已回收 {{trashed.length}} 条（可恢复）</summary>
   <div v-for="f in trashed" :key="f.id" class="fc-trash-row"><span>{{f.name}} · 起报 {{time(f.issueTime)}}</span><button class="link" @click="restore(f)">恢复</button></div></details>
 </section>

 <ForecastResult v-if="tab==='results'" :forecasts="forecasts" v-model:selected="selected" @warning="tab='warning'" @risk="emit('navigate','risk',$event)"/>
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
.fc-cell.lag{background:#fdf8ec}.fc-cell.lag b{color:#8a5a12}
.fc-cell em{font-style:normal;font-size:12px;font-weight:400;color:var(--muted);margin-left:3px}
.fc-cell small{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.fc-cell select{min-height:34px;padding:4px 8px;font-size:14px}
.fc-sheet{padding-top:6px}
.fc-form{display:grid;max-width:980px}
.fc-row{display:grid;grid-template-columns:110px minmax(0,1fr);gap:18px;align-items:start;padding:14px 0;border-bottom:1px solid #edf2f4}
.fc-row:last-child{border-bottom:0}
.fc-row>span{font-size:13.5px;color:#4b6874;padding-top:9px;font-weight:600}
.fc-row>div{display:grid;gap:6px;justify-items:start;min-width:0}
.fc-row b{font-size:18px;font-weight:600;color:#1f4e5c;font-variant-numeric:tabular-nums;padding-top:6px}
.fc-row small{font-size:12.5px;color:var(--muted)}
.fc-row select{min-width:420px;max-width:100%}
.fc-seg{display:inline-flex;border:1px solid #cddce3;border-radius:6px;overflow:hidden}
.fc-seg button{border:0;border-radius:0;min-height:38px;padding:6px 16px;background:#fff;font-variant-numeric:tabular-nums}
.fc-seg button+button{border-left:1px solid #cddce3}.fc-seg button.on{background:#1c7885;color:#fff}.fc-seg button:disabled{color:#a7b8be;background:#f6f8f9}
.fc-chain{color:#3f6572!important;padding-top:9px}
.fc-go button{min-width:180px}
.fc-table td,.fc-table th{white-space:nowrap}.fc-table td.num{font-variant-numeric:tabular-nums;text-align:right}
.fc-table tr.on td{background:#f0f8f9}.fc-table small{color:var(--muted)}
.fc-acts{display:flex;gap:12px}.fc-acts .muted-link{color:var(--muted)}
.fc-trash{margin-top:14px;font-size:13px}.fc-trash summary{cursor:pointer;color:var(--muted)}.fc-trash-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #edf2f4}
.fc-grade-dot{display:inline-block;width:9px;height:9px;border-radius:50%}
.fc-gap-badge{padding:8px 14px;background:#fdf6e9;border:1px solid #ecd9ae;border-radius:6px;color:#7a5a1d;font-size:13px;margin-bottom:14px}
@media(max-width:1250px){.fc-status{grid-template-columns:repeat(3,minmax(0,1fr))}.fc-run{grid-template-columns:1fr}}
@media(max-width:700px){.fc-status{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
