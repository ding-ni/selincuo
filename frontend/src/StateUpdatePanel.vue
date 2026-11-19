<script setup>
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {api,number,fixed} from './api.mjs'
const emit=defineEmits(['updated','error','navigate'])
const METHODS=[
 ['station','站点实测','用观测库中的雨量站日雨量和主站水位；缺测日可在表中录入站点实测值，存入本地观测库'],
 ['reanalysis','再分析资料','用已下载的 ERA5-Land 再分析驱动，与率定期一致；需联网下载'],
 ['climatology','多年同期','无观测资料时，按多年同期平均降水、气温推进状态']]
const editing=ref(false),mode=ref('station'),plan=ref(null),loading=ref(false),task=ref(null),message=ref(''),planError=ref('')
let timer
const METHOD_CLASS={era5:'m-era',station:'m-sta',manual:'m-man',climatology:'m-clim'}
const latest=computed(()=>plan.value?.latest)
const target=computed(()=>plan.value?.target?.slice(0,10))
const today=new Date(Date.now()+8*3600000).toISOString().slice(0,10)
const lagDays=computed(()=>{const s=latest.value?.stateTime;return s?Math.max(0,Math.round((Date.parse(today)-Date.parse(s.slice(0,10)))/86400000)):null})
const days=computed(()=>plan.value?.days||[])
const LEVEL_STATION='SLC',LEVEL_HOUR=8
// Entries are station observations keyed by station and date; saved to SQLite before the update runs.
const entries=ref({}),saving=ref(false)
const key=(sid,date)=>sid+'|'+date
const pending=computed(()=>Object.entries(entries.value).filter(([,v])=>v!==''&&v!=null))
const counts=computed(()=>{const c={era5:0,station:0,manual:0,climatology:0};for(const d of days.value)c[d.method]++;return c})
const rainTotal=computed(()=>days.value.reduce((a,d)=>a+(d.basinPrecipitationMm||0),0))
const levelDays=computed(()=>days.value.filter(d=>d.lakeLevelM!=null).length)
const stations=computed(()=>plan.value?.stations||[])
const reported=computed(()=>stations.value.filter(st=>days.value.some(d=>d.stations[st.id]!=null)))
const missingCells=computed(()=>days.value.filter(d=>d.method!=='era5').reduce((n,d)=>n+stations.value.filter(st=>d.stations[st.id]==null).length+(d.lakeLevelM==null?1:0),0))
const t=s=>s?String(s).replace('T',' ').slice(0,16):'—'
async function load(){loading.value=true;message.value='';planError.value=''
 try{plan.value=await api('state-update/plan?'+new URLSearchParams({mode:mode.value}));entries.value={}}
 catch(e){plan.value=null;planError.value=e.message}finally{loading.value=false}}
watch(mode,()=>{editing.value=false;load()})
function records(){return pending.value.map(([k,v])=>{const [station,date]=k.split('|');return station===LEVEL_STATION
 ?{station,variable:'level',date,hour:LEVEL_HOUR,value:Number(v)}:{station,variable:'precipitation',date,value:Number(v)}})}
async function saveEntries(){saving.value=true
 try{const r=await api('state-update/station-entry',{records:records(),note:'实况更新页手工录入'});await load();editing.value=true
  message.value='已将 '+r.saved+' 项站点实测值存入本地观测库，逐日驱动已按新资料重算'}
 catch(e){emit('error',e.message)}finally{saving.value=false}}
async function submit(){if(pending.value.length){emit('error','还有 '+pending.value.length+' 项录入未保存，请先保存到观测库');return}
 try{task.value=await api('state-update',{mode:mode.value,end:plan.value.target,name:'实况更新至 '+target.value,clientRequestId:'su-'+Date.now()});timer=setInterval(poll,2500)}catch(e){emit('error',e.message)}}
async function poll(){if(!task.value)return;try{const x=await api('tasks/'+encodeURIComponent(task.value.id));task.value=x
 if(x.status==='succeeded'){clearInterval(timer);task.value=null;await load();message.value='模型状态已更新至 '+t(x.result?.end)+'，期末湖位 '+fixed(x.result?.finalLakeLevelM,3)+' m（'+x.result?.forcingLabel+'）';emit('updated',x.result)}
 else if(['failed','cancelled','interrupted'].includes(x.status)){clearInterval(timer);task.value=null;emit('error',x.message||'状态更新未完成')}}catch(e){emit('error',e.message)}}
onMounted(load)
onBeforeUnmount(()=>clearInterval(timer))
</script>
<template>
<section class="panel su">
 <div class="su-status" :class="{lag:lagDays>0}">
  <div><span>模型状态</span><b>{{t(latest?.stateTime)}}</b></div>
  <div><span>距今日</span><b>{{lagDays==null?'—':lagDays===0?'已到今日':'滞后 '+lagDays+' 天'}}</b></div>
  <div><span>状态末湖位</span><b>{{fixed(plan?.base?.lakeLevelM,3)}} m</b></div>
  <p>预报从模型状态末时刻起报。先把状态更新到今日，预报才有现实意义。</p>
 </div>
 <p v-if="message" class="message success" role="status">{{message}}</p>

 <h4 class="su-step"><i>1</i>选择更新资料</h4>
 <div class="su-methods" role="tablist">
  <button v-for="[id,label,desc] in METHODS" :key="id" :class="{on:mode===id}" :disabled="!!task" @click="mode=id"><b>{{label}}</b><span>{{desc}}</span>
   <small v-if="id==='reanalysis'">{{plan?.reanalysisEnd?'已覆盖至 '+plan.reanalysisEnd.slice(0,10):'尚未下载新资料'}}</small></button>
 </div>

 <p v-if="loading" class="muted">正在读取逐日资料…</p>
 <div v-else-if="planError" class="su-blocked"><p>{{planError}}</p>
  <button v-if="mode==='reanalysis'" @click="emit('navigate','model:forcing')">前往资料更新下载 →</button></div>

 <template v-else-if="days.length">
  <h4 class="su-step"><i>2</i>核对逐日驱动 <small>{{days[0].date}} 至 {{days.at(-1).date}}，共 {{days.length}} 天</small></h4>
  <div v-if="mode==='station'" class="su-hint">已有的站点日雨量和主站水位取自本地观测库；某站缺测的日子不参与插值，全部雨量站缺测的日子按多年同期补齐。
   <template v-if="!reported.length">本时段观测库中没有雨量站资料。</template>
   <label class="su-edit"><input type="checkbox" v-model="editing">录入缺测日的站点实测值<small v-if="missingCells">（{{missingCells}} 处缺测）</small></label>
   <button class="link" @click="emit('navigate','model:observations')">批量导入 Excel 或接入数据库 →</button></div>
  <div v-else-if="mode==='climatology'" class="su-hint">全部日子按多年同期（前后 7 天）平均降水、气温和蒸发能力推进；已有主站水位的日子仍用实测水位校正。</div>
  <div v-else class="su-hint">再分析覆盖的日子直接使用 ERA5-Land 驱动，与率定期一致。</div>
  <div class="su-chips">
   <span v-if="counts.era5" class="m-era">再分析 {{counts.era5}} 天</span><span v-if="counts.station" class="m-sta">雨量站 {{counts.station}} 天</span>
<span v-if="counts.climatology" class="m-clim">多年同期 {{counts.climatology}} 天</span>
   <span class="su-sum">面雨量合计 {{number(rainTotal,1)}} mm · 水位校正 {{levelDays}} 天</span>
  </div>
  <div class="table-wrap su-table"><table>
   <thead><tr><th>日期</th><th>降水驱动</th>
    <th v-for="s in (mode==='station'?stations:reported)" :key="s.id">{{s.name}}<small>日雨量 mm</small></th>
    <th>主站水位 m<small>{{editing?'按 0'+LEVEL_HOUR+' 时观测录入':'实测'}}</small></th><th class="num">流域面雨量 mm<small>计算值</small></th></tr></thead>
   <tbody><tr v-for="d in days" :key="d.date">
    <td class="su-date">{{d.date.slice(5)}}</td>
    <td><i :class="METHOD_CLASS[d.method]">{{d.methodLabel}}</i></td>
    <td v-for="s in (mode==='station'?stations:reported)" :key="s.id" class="num" :class="{obs:d.stations[s.id]!=null}">
     <template v-if="d.stations[s.id]!=null">{{number(d.stations[s.id],1)}}</template>
     <input v-else-if="editing&&d.method!=='era5'" type="number" min="0" max="300" step="0.1" v-model="entries[key(s.id,d.date)]" placeholder="缺测" :aria-label="d.date+' '+s.name+'日雨量'">
     <span v-else class="miss">缺测</span></td>
    <td class="num" :class="{obs:d.lakeLevelM!=null}">
     <template v-if="d.lakeLevelM!=null"><span :title="d.lakeLevelTime?'实测 '+t(d.lakeLevelTime):''">{{fixed(d.lakeLevelM,2)}}</span></template>
     <input v-else-if="editing" type="number" min="4530" max="4560" step="0.01" v-model="entries[key(LEVEL_STATION,d.date)]" placeholder="缺测" :aria-label="d.date+'主站水位'">
     <span v-else class="miss">—</span></td>
    <td class="num">{{d.method==='era5'?'再分析':number(d.basinPrecipitationMm,1)}}</td>
   </tr></tbody>
  </table></div>
  <div v-if="editing" class="su-save">
   <button class="primary" :disabled="!pending.length||saving" @click="saveEntries">{{saving?'正在保存…':'保存到本地观测库（'+pending.length+' 项）'}}</button>
   <span class="muted">录入值按站点实测入库（质量“合格”，来源“站点手工录入”），之后监测资料、预报起报都会使用；同一站同一天再次录入以新值为准。</span>
  </div>

  <h4 class="su-step"><i>3</i>更新模型状态</h4>
  <div class="su-foot">
   <button class="primary" :disabled="!!task" @click="submit">{{task?'更新中 '+number(task.progress||0,0)+'%':'更新模型状态至 '+target+' 00:00'}}</button>
   <span class="muted">{{task?(task.message||'排队中'):'率定参数、湖泊方案不变；更新后预报自动从新状态起报。'}}</span>
  </div>
  <details class="su-notes"><summary>驱动资料与方法说明</summary><ul>
   <li>站点实测：反距离权重插值当日有资料的站点日雨量，再按多年同期（前后 7 天）降水空间分布展布到各计算单元（站点比例法），得到流域面雨量。手工录入的站点值与导入资料同样处理。</li>
   <li>站网暂无气温观测，气温与水面蒸发能力采用多年同期同一时刻的逐时平均值，并按高程递减率分配。</li>
   <li>主站水位取当日最接近 12 时的一次合格观测，在实测时刻校正湖位；手工录入的水位按 08 时观测入库。</li>
   <li>再分析资料以后到位，可再做一次“再分析资料”更新，替换站点及多年同期驱动段。</li>
  </ul></details>
 </template>
 <p v-else-if="plan" class="empty">模型状态已到{{mode==='reanalysis'?'再分析资料末日':'今日'}}，无需更新。</p>
</section>
</template>
<style scoped>
.su-status{display:flex;flex-wrap:wrap;gap:10px 28px;align-items:baseline;padding:14px 18px;border:1px solid var(--line);border-radius:8px;background:#f6fafb;margin-bottom:6px}
.su-status.lag{background:#fdf8ec;border-color:#ecd9ae}
.su-status div{display:grid;gap:2px}.su-status span{font-size:12px;color:var(--muted)}
.su-status b{font-size:18px;font-weight:600;color:#1f4e5c;font-variant-numeric:tabular-nums}.su-status.lag div:nth-child(2) b{color:#8a5a12}
.su-status p{flex:1 1 260px;margin:0;font-size:12.5px;color:#5b7680;text-align:right}
.su-step{display:flex;align-items:center;gap:9px;margin:22px 0 10px;font-size:15px}
.su-step i{font-style:normal;width:22px;height:22px;border-radius:50%;background:#1c7885;color:#fff;font-size:12.5px;display:grid;place-items:center}
.su-step small{font-weight:400;color:var(--muted);font-size:12.5px}
.su-methods{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.su-methods>button{display:grid;justify-items:start;gap:4px;text-align:left;padding:13px 15px;border-radius:8px}
.su-methods>button span{font-size:12.5px;color:var(--muted)}.su-methods>button small{font-size:12px;color:#245a86}
.su-methods>button.on{border-color:#2f8d9b;background:#eef7f8;box-shadow:inset 0 -3px 0 #2f8d9b}
.su-blocked{margin-top:12px;padding:12px 14px;border:1px solid #ecd9ae;background:#fdf6e9;border-radius:6px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}.su-blocked p{margin:0;color:#7a5a1d;font-size:13px}
.su-hint{font-size:13px;color:#4c6772;margin-bottom:10px}
.su-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.su-chips>span{font-size:12.5px;padding:3px 10px;border-radius:12px}
.su-sum{margin-left:auto;color:#2a5260;background:none!important}
.m-era{background:#e6f0f8;color:#245a86}.m-sta{background:#e5f4ea;color:#256a3c}.m-man{background:#f3ecf8;color:#64408a}.m-clim{background:#f1f1ee;color:#6a6a5e}
.su-table{max-height:420px}
.su-table th{white-space:nowrap;font-weight:600}.su-table th small{display:block;font-weight:400;font-size:11px;color:var(--muted)}
.su-table td{padding:6px 10px;font-variant-numeric:tabular-nums}.su-table td.num{text-align:right}.su-table th{text-align:left}
.su-edit{display:inline-flex!important;flex-direction:row;align-items:center;gap:6px;margin-left:10px;font-size:13px;color:#2b5866;cursor:pointer}.su-edit input{width:auto;min-height:0}
.su-table td.obs{color:#1d5f3a;font-weight:500}.su-table .miss{color:#a0a8ab;font-size:12px}
.su-save{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:10px 12px;background:#f5f9fa;border:1px dashed #b9d3da;border-radius:8px}
.su-table i{font-style:normal;font-size:12px;padding:2px 8px;border-radius:10px;white-space:nowrap}
.su-table input{min-height:30px;padding:2px 6px;width:84px;text-align:right}
.su-date{font-variant-numeric:tabular-nums;color:#3e6170;white-space:nowrap}
.su-foot{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.su-notes{margin-top:16px;font-size:13px;color:#4c6772}.su-notes summary{cursor:pointer;color:var(--muted)}.su-notes ul{margin:8px 0 0;padding-left:20px;line-height:1.8}
@media(max-width:1000px){.su-methods{grid-template-columns:1fr}}
</style>
