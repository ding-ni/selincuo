<script setup>
import {ref,reactive,computed,watch,onMounted,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import ShoreComparison from './ShoreComparison.vue'
import {api,number} from './api.mjs'
import {seriesType} from './chart-state.mjs'
import {qualityLabel,stepLabel,variableDefinitions} from './business-view.mjs'
import {stationDatum} from './shore-comparison.mjs'
const props=defineProps({initial:Object});const emit=defineEmits(['navigate'])
const stations=ref([]),q=reactive({station:props.initial?.station||'SLC',variable:props.initial?.variable||'level',step:'',start:'',end:'',profile:'quality-v2'}),snapshot=ref(null),daily=ref([]),records=ref({rows:[],total:0,page:1,pageSize:50}),tab=ref('process'),comparisonOpened=ref(false),working=ref(true),recordBusy=ref(false),error=ref(''),profileNote=ref(''),recordStep=ref(''),qualityDay=ref('')
let queryController,recordController,ticket=0,recordTicket=0
const selected=computed(()=>stations.value.find(s=>s.id===q.station))
const available=computed(()=>selected.value?.variables||[])
const availableSteps=computed(()=>available.value.find(v=>v.id===q.variable)?.steps||[])
const definition=id=>variableDefinitions.find(v=>v.id===id)||{id,label:id,unit:''}
const shown=computed(()=>definition(snapshot.value?.variable)),stationName=computed(()=>stations.value.find(s=>s.id===snapshot.value?.station)?.name)
const valid=computed(()=>daily.value.filter(r=>r.value!=null))
const shownStation=computed(()=>stations.value.find(s=>s.id===snapshot.value?.station))
const sourceCoverage=computed(()=>available.value.find(v=>v.id===q.variable))
const profileName=profile=>({'quality-v2':'整编日值','raw':'原始观测','candidate':'统计零点候选','legacy':'历史日值'})[profile]||profile
const qualityRows=computed(()=>qualityDay.value?daily.value.filter(r=>r.time.slice(0,10)===qualityDay.value):daily.value)
const datumInfo=computed(()=>{if(selected.value?.id==='SLC')return '主站工作基准';const registered=(stationDatum(selected.value)?.periods||[]).some(p=>p.status==='survey-verified'&&p.surveySource&&p.effectiveFrom);return registered||snapshot.value?.station===selected.value?.id&&daily.value.some(r=>r.referenceDatum?.available)?'已登记实测基准，按时段核对':'零点高程未登记'})
const dailySource=r=>r.reason||({'source-daily':'原始日表','time-weighted-level-v2-gap6h-cover18h':'按时段加权整编','legacy-variable-rule':'按要素整编'}[r.source]||'—')
function bounds(reset=false){const v=sourceCoverage.value;if(v){const lo=v.start.slice(0,10),hi=v.end.slice(0,10);if(reset){q.start=lo;q.end=hi}else{if(!q.start||q.start<lo||q.start>hi)q.start=lo;if(!q.end||q.end>hi||q.end<lo)q.end=hi}}if(!availableSteps.value.includes(q.step))q.step='';if(q.profile==='candidate'&&(q.variable!=='level'||!q.station.startsWith('SLC-')))q.profile='quality-v2'}
function invalidate(){ticket++;recordTicket++;working.value=false;recordBusy.value=false;queryController?.abort();recordController?.abort();tab.value='process'}
watch(()=>q.station,()=>{if(!available.value.some(v=>v.id===q.variable))q.variable=available.value[0]?.id||'';bounds(true)})
watch(()=>q.variable,()=>bounds(true))
// A station/element switch invalidates the old view immediately. Vue batches
// station-driven element changes, so only the final selection is queried.
watch(()=>[q.station,q.variable],()=>{
 invalidate();snapshot.value=null;daily.value=[];records.value={rows:[],total:0,page:1,pageSize:50};profileNote.value='';error.value=''
 if(stations.value.length&&q.variable)query()
},{flush:'post'})
function period(days){const v=sourceCoverage.value;if(!v)return;q.end=v.end.slice(0,10);q.start=days?[v.start.slice(0,10),new Date(Date.parse(q.end+'T00:00:00Z')-(days-1)*86400000).toISOString().slice(0,10)].sort().at(-1):v.start.slice(0,10);query()}
async function query(){
 const n=++ticket;queryController?.abort();recordController?.abort();recordTicket++;queryController=new AbortController();working.value=true;recordBusy.value=false;error.value='';tab.value='process';snapshot.value=null;daily.value=[];records.value={rows:[],total:0,page:1,pageSize:50};profileNote.value=''
 try{if(q.end<q.start)throw Error('结束日期须不早于开始日期');const chosen={...q,step:''};const data=await api('observation-series?'+new URLSearchParams(chosen),null,{signal:queryController.signal});if(n!==ticket)return;daily.value=data.rows;snapshot.value=chosen;records.value={rows:[],total:0,page:1,pageSize:50};recordStep.value='';qualityDay.value='';profileNote.value=data.message||''}
 catch(e){if(e.name!=='AbortError'&&n===ticket)error.value=e.message}finally{if(n===ticket)working.value=false}
}
async function loadRecords(page=1){
 if(!snapshot.value)return
 const n=++recordTicket;recordController?.abort();recordController=new AbortController();recordBusy.value=true;error.value=''
 try{const value=await api('observation-page?'+new URLSearchParams({...snapshot.value,step:recordStep.value,page,pageSize:50}),null,{signal:recordController.signal});if(n===recordTicket)records.value=value}catch(e){if(e.name!=='AbortError'&&n===recordTicket)error.value=e.message}finally{if(n===recordTicket)recordBusy.value=false}
}
watch(tab,t=>{if(t==='records')loadRecords(1);if(t==='comparison')comparisonOpened.value=true})
function exportDaily(){const csv='\ufeff时间,'+shown.value.label+' ('+shown.value.unit+'),资料视图,质量,覆盖小时,最大间隔小时,来源或缺测原因\r\n'+daily.value.map(r=>[r.time,r.value??'',profileName(snapshot.value.profile),r.quality||'',r.coverageHours??'',r.maximumGapHours??'',r.reason||r.source||''].map(value=>'"'+String(value).replaceAll('"','""')+'"').join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=stationName.value+'-'+shown.value.label+'-'+profileName(snapshot.value.profile)+'-'+snapshot.value.start+'至'+snapshot.value.end+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
onMounted(async()=>{try{stations.value=(await api('observation-catalog')).stations;await Promise.resolve();bounds(true);if(props.initial?.day)q.start=q.end=props.initial.day;await query();if(props.initial?.comparison)tab.value='comparison'}catch(e){error.value=e.message;working.value=false}})
onBeforeUnmount(()=>{queryController?.abort();recordController?.abort();ticket++;recordTicket++})
</script>
<template>
 <section class="panel"><div class="panel-title"><h3>监测资料查询</h3><button class="link" @click="emit('navigate','model')">资料接入 →</button></div>
 <form class="query-grid monitoring-query" @submit.prevent="query">
 <label>测站<select v-model="q.station" aria-label="测站"><option v-for="s in stations.filter(s=>s.variables.length)" :key="s.id" :value="s.id">{{s.name}}</option></select></label>
 <label>监测要素<select v-model="q.variable" aria-label="监测要素"><option v-for="v in available" :key="v.id" :value="v.id">{{definition(v.id).label}}</option></select></label>
 <label>资料类型<select v-model="q.profile"><option value="quality-v2">整编日值</option><option value="raw">原始观测</option></select></label>
 <label>开始日期<input v-model="q.start" type="date" required></label><label>结束日期<input v-model="q.end" type="date" required></label>
 <button class="primary" :disabled="!q.variable||!stations.length||working">{{working?'查询中…':'应用筛选'}}</button></form>
 <div class="monitoring-coverage" v-if="sourceCoverage"><span>目录覆盖 {{sourceCoverage.start.slice(0,10)}} — {{sourceCoverage.end.slice(0,10)}}</span><span>{{sourceCoverage.valid.toLocaleString()}} 条有值记录</span><button class="link" :disabled="working" @click="period(30)">最近30天</button><button class="link" :disabled="working" @click="period(365)">最近一年</button><button class="link" :disabled="working" @click="period(null)">完整时段</button></div>
 <details v-if="selected"><summary>站点详情</summary><dl><dt>站点</dt><dd>{{selected.name}}</dd><dt>位置</dt><dd>{{selected.lon!=null&&selected.lat!=null?number(selected.lon,6)+'°E，'+number(selected.lat,6)+'°N':'尚未登记'}}</dd><dt>监测要素</dt><dd>{{available.map(v=>definition(v.id).label+(definition(v.id).unit?'（'+definition(v.id).unit+'）':'')).join('、')}}</dd><template v-if="selected.kind==='lake'"><dt>水位基准</dt><dd>{{datumInfo}}</dd></template></dl></details>
 </section>
 <p v-if="error" class="message error" role="alert">{{error}}</p>
 <section class="panel" :aria-busy="working">
 <div class="panel-title"><h3 v-if="tab==='comparison'">色林错五站湖位对照</h3><h3 v-else>{{stationName||'资料过程'}}<span v-if="snapshot"> · {{shown.label}}</span></h3><button v-if="tab!=='comparison'" class="link" :disabled="!snapshot||working" @click="exportDaily">导出当前结果 ↓</button></div>
 <div class="result-context" v-if="snapshot&&tab!=='comparison'"><span>{{profileName(snapshot.profile)}} · {{snapshot.start}} — {{snapshot.end}}</span><span>有效 {{valid.length}} {{snapshot.profile==='raw'?'条':'日'}}<template v-if="snapshot.profile!=='raw'"> / 缺测 {{daily.length-valid.length}} 日</template></span><span v-if="valid.length">末次有效值 {{number(valid.at(-1).value,3)}} {{shown.unit}} · {{valid.at(-1).time.slice(0,snapshot.profile==='raw'?16:10).replace('T',' ')}}</span></div>
 <nav class="workspace-tabs"><button :class="{active:tab==='process'}" @click="tab='process'">{{snapshot?.profile==='raw'?'原始采样过程':'日过程'}}</button><button :disabled="!snapshot||working" :class="{active:tab==='records'}" @click="tab='records'">原始资料明细</button><button v-if="shownStation?.kind==='lake'&&snapshot?.variable==='level'" :class="{active:tab==='comparison'}" @click="tab='comparison'">湖位多站对照</button></nav>
 <div v-if="working" class="note" role="status">正在读取资料…</div>
 <template v-if="tab==='process'"><p v-if="profileNote" class="note warning">{{profileNote}}</p><p v-if="shownStation?.kind==='lake'&&shownStation?.id!=='SLC'&&(snapshot?.profile==='raw'||daily.some(r=>r.value!=null&&!r.referenceDatum?.available))" class="note warning">本站读数保留原基准；未核定时段可查看自身变化。</p><Chart v-if="snapshot" :rows="daily" :series="[{key:'value',label:shown.label,type:seriesType(snapshot?.variable)}]" :unit="shown.unit" :hourly="snapshot?.profile==='raw'" :zero="seriesType(snapshot?.variable)==='bar'" :day="qualityDay" :context="'monitor:'+snapshot?.station+':'+snapshot?.variable+':'+snapshot?.profile" @day="qualityDay=$event.slice(0,10)"/><p v-else-if="!working" class="empty">请选择资料并应用筛选。</p><details v-if="snapshot?.profile==='quality-v2'" :open="!!qualityDay"><summary>日值质量与来源{{qualityDay?' · '+qualityDay:''}}</summary><p class="muted" v-if="snapshot?.variable==='level'">原始合格日表优先；其他采样相邻间隔不超过6小时，覆盖至少18小时才生成日均。</p><button v-if="qualityDay" class="link" @click="qualityDay=''">查看全部查询日</button><div class="table-wrap"><table><thead><tr><th>日期</th><th>日值</th><th>覆盖小时</th><th>最大间隔 h</th><th>来源 / 缺测原因</th></tr></thead><tbody><tr v-for="r in qualityRows" :key="r.time"><td>{{r.time.slice(0,10)}}</td><td>{{number(r.value,3)}}</td><td>{{number(r.coverageHours,2)}}</td><td>{{number(r.maximumGapHours,2)}}</td><td>{{dailySource(r)}}</td></tr></tbody></table></div></details></template>
 <ShoreComparison v-if="comparisonOpened" v-show="tab==='comparison'" :start="snapshot?.start" :end="snapshot?.end"/>
 <template v-if="tab==='records'"><p class="muted">本表列出当前查询站点、要素与日期对应的原始记录。统计候选与质量日均不会覆盖原值。</p><div class="record-filters"><label>原始时间尺度<select v-model="recordStep" @change="loadRecords(1)"><option value="">全部原始尺度</option><option v-for="s in shownStation?.variables.find(v=>v.id===snapshot.variable)?.steps||[]" :key="s" :value="s">{{stepLabel(s)}}</option></select></label><span v-if="recordBusy">正在读取…</span></div>
 <div class="table-wrap"><table><thead><tr><th>观测时间</th><th>数值 / {{shown.unit}}</th><th>尺度</th><th>质量</th><th>原始记录</th></tr></thead><tbody><tr v-for="r in records.rows" :key="r.id"><td>{{r.time.replace('T',' ')}}</td><td>{{number(r.value,3)}}</td><td>{{stepLabel(r.step)}}</td><td>{{qualityLabel(r.quality)}}</td><td><details class="row-source"><summary>{{r.raw_text||'来源'}}</summary>{{r.sheet}} / {{r.cell}}</details></td></tr></tbody></table></div>
 <div class="pagination"><span>共 {{records.total.toLocaleString()}} 条</span><button :disabled="recordBusy||records.page===1" @click="loadRecords(records.page-1)">上一页</button><span>{{records.page}} / {{Math.max(1,Math.ceil(records.total/50))}}</span><button :disabled="recordBusy||records.page*50>=records.total" @click="loadRecords(records.page+1)">下一页</button></div></template>
 </section>
</template>
