<script setup>
import {ref,computed,watch,onMounted,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import ParameterTransferAudit from './ParameterTransferAudit.vue'
import {api,number} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
import {liftDujaToShared,transferMode} from './parameter-transfer.mjs'
const props=defineProps({boot:Object,tasks:Array,detail:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','stop','select','state','refresh','simulate','export','deleted'])
const steps=['资料与时段','选择参数','开始率定','查看与采用']
const step=ref(0),schemeId=ref(''),draft=ref(null),saved=ref(''),group=ref('land'),working=ref(false),error=ref(''),message=ref(''),data=ref(null),observations=ref({flow:[],lake:[]}),progress=ref(null),autoAdopt=ref(true),manualBase=ref(null),seedsText=ref('42, 142, 242'),historyOpen=ref(false)
let timer,progressTicket=0,disposed=false
const copy=v=>JSON.parse(JSON.stringify(v))
const schemes=computed(()=>props.boot.schemes||[])
const dirty=computed(()=>draft.value&&JSON.stringify(draft.value)!==saved.value)
const locked=computed(()=>props.busy||working.value||props.active.length>0)
const selectedScheme=computed(()=>schemes.value.find(s=>s.id===schemeId.value))
const currentName=computed(()=>props.boot.currentBusiness?.name||schemes.value.find(s=>s.id===props.boot.homeSchemeId)?.name||'色林错模拟方案')
const records=computed(()=>(props.tasks||[]).filter(t=>!t.trashed&&['two-stage-calibration','calibration','trial'].includes(t.kind)))
const definitions=computed(()=>props.boot.parameterDefinitions||[])
const flowKeys=computed(()=>props.boot.calibrationConfig?.flowSelectableKeys||[])
const groups=[{id:'land',name:'陆面产流'},{id:'snow',name:'积雪与冰川'},{id:'routing',name:'汇流'},{id:'lake',name:'湖泊'}]
const shownDefinitions=computed(()=>definitions.value.filter(d=>d.group===group.value))
const workflow=computed(()=>props.detail?.result?.calibrationWorkflow)
const fitRows=computed(()=>workflow.value?.flowFits||[])
const finalParameters=computed(()=>props.detail?.result?.parameters||{})
const successful=computed(()=>props.detail?.status==='succeeded'&&props.detail?.result?.daily?.length)
const selectedCount=computed(()=>draft.value?.search.keys.filter(k=>flowKeys.value.includes(k)).length||0)
const totalBudget=computed(()=>Object.values(draft.value?.twoStage?.budgets||{}).reduce((a,b)=>a+Number(b||0),0))
const historyRows=computed(()=>props.detail?.result?.daily?.filter(r=>r.date>='2021-04-23')||[])
const traceRows=computed(()=>(progress.value?.trace||[]).map(r=>({...r,date:String(r.evaluation)})))
const adopted=computed(()=>props.detail?.id===props.boot.homeResultId)
const constrained=computed(()=>draft.value?.twoStage?.selectionMode==='flow-constrained-lake'&&draft.value?.twoStage?.mode==='both')
const selection=computed(()=>workflow.value?.selection)
const warmupDiagnostics=computed(()=>props.detail?.result?.warmupDiagnostics)
const actualWarmup=computed(()=>draft.value?.periods?.warmupStart||props.detail?.request?.start)
const modeName=computed(()=>constrained.value?'流量与湖位分阶段率定':({both:'两阶段连续率定',flow:'杜加流量率定',lake:'湖泊水位率定'})[draft.value?.twoStage?.mode]||'两阶段连续率定')
const status=t=>({succeeded:'已完成',running:'计算中',queued:'排队中',failed:'失败',cancelled:'已停止',interrupted:'已中断',stopping:'正在停止'})[t?.status]||t?.status
const initialValue=d=>draft.value?.parameters[d.key]
function warmupCase(name){
 const match=name.match(/^(\d{4}-\d{2}-\d{2})-(\d+(?:\.\d+)?)$/)
 return match?match[1]+'起算 · 土壤初始蓄水'+Math.round(Number(match[2])*100)+'%':name
}
function searchState(fit){
 if(fit.converged)return '达到搜索收敛条件'
 return ({'training-target-reached':'湖位筛选目标已达到','no-budget':'未追加联合细化','insufficient-population':'合格候选不足以组成搜索种群','constraint-proposal-limit':'已到物理约束提案上限'})[fit.termination]||'达到搜索预算'
}
function prepare(value){
 const v=copy(value)
 v.partitionParameters||={};v.search||={};v.search.bounds||={}
 for(const d of definitions.value)v.search.bounds[d.key]||=[d.min,d.max]
 v.search.keys||=[...flowKeys.value]
 v.twoStage||=copy(data.value?.defaults||{version:1,mode:'both',reverseCheck:true,finalRefit:true,seeds:[42,142,242],budgets:{runoff:6000,snow:6000,routing:3000,joint:3000},allowExtension:true,lakePeriods:{trainingStart:'2021-04-23T00:00:00',trainingEnd:'2025-01-01T00:00:00',evaluationStart:'2025-01-01T00:00:00',evaluationEnd:'2026-09-14T00:00:00'}})
 v.twoStage.selectionMode||='flow-only'
 v.twoStage.candidateSourceTaskId||=props.boot.currentBusiness?.candidateSourceTaskId||'JOB-4934a5d35c9344a59353'
 v.twoStage.flowConstraints||={minNse:0.70,maxAbsPbias:5}
 v.twoStage.candidateLimit??=256
 v.twoStage.jointBudget??=2000
 return v
}
function choose(id){
 const row=schemes.value.find(s=>s.id===id);if(!row)return
 draft.value=prepare(row.payload);schemeId.value=id;saved.value=JSON.stringify(draft.value);seedsText.value=draft.value.twoStage.seeds.join(', ');manualBase.value=null;error.value=''
}
function chooseFromUser(event){
 if(dirty.value&&!confirm('当前修改尚未保存，仍要切换方案吗？')){event.target.value=schemeId.value;return}
 choose(event.target.value)
}
watch(schemes,rows=>{
 if(!draft.value&&rows.length)choose(rows.find(r=>r.id===props.boot.currentBusiness?.schemeId)?.id||rows.find(r=>r.payload?.twoStage)?.id||rows[0].id)
},{immediate:true})
watch(()=>props.boot.currentBusiness?.schemeId,id=>{if(id&&!dirty.value&&schemes.value.some(r=>r.id===id))choose(id)})
watch(()=>props.detail?.id,()=>{progress.value=null;progressTicket++;if(props.detail)step.value=['running','queued'].includes(props.detail.status)?2:3})
watch(()=>props.detail?.status,value=>{if(value==='succeeded')step.value=3})
watch(()=>draft.value?.twoStage?.mode,mode=>{if(mode==='lake')group.value='lake'})
async function fresh(){
 working.value=true;error.value=''
 try{draft.value=prepare(await api('research-template'));draft.value.name='我的色林错方案';schemeId.value='';saved.value='';manualBase.value=null;step.value=0;seedsText.value=draft.value.twoStage.seeds.join(', ')}
 catch(e){error.value=e.message}finally{working.value=false}
}
function duplicate(){
 if(!draft.value)return
 draft.value=prepare(draft.value)
 if(transferMode(draft.value)==='legacy')draft.value=liftDujaToShared(draft.value,'duja-shared',{schemeId:schemeId.value,configurationSHA256:draft.value.configurationSHA256})
 draft.value.name+=' · 副本';schemeId.value='';saved.value=''
 for(const key of ['builtin','id','familyId','revision','configurationSHA256','adoptedParameterId'])delete draft.value[key]
}
function day(period,key){const d=period?.[key]?.slice(0,10)||'';return d&&key.endsWith('End')?shiftCalendarDay(d,-1):d}
function setDay(period,key,value){if(value)period[key]=(key.endsWith('End')?shiftCalendarDay(value,1):value)+'T00:00:00'}
function validCount(which,period,kind){
 if(!data.value)return '读取中'
 const start=period?.[kind+'Start'],end=period?.[kind+'End'];if(!start||!end)return 0
 return observations.value[which].filter(r=>r.value!=null&&r.time>=start&&r.time<end).length
}
function selectGroup(all){
 const allowed=shownDefinitions.value.filter(d=>flowKeys.value.includes(d.key)).map(d=>d.key)
 draft.value.search.keys=[...draft.value.search.keys.filter(k=>!allowed.includes(k)),...(all?allowed:[])]
}
function setParameter(key,event){draft.value.parameters[key]=event.target.value===''?null:Number(event.target.value)}
async function ensureSaved(){
 const value=copy(draft.value)
 value.twoStage.seeds=seedsText.value.split(/[,，\s]+/).filter(Boolean).map(Number)
 value.purpose='business';value.target='duja';value.search.scope='duja'
 value.calibrationInitialParameters=copy(value.parameters);value.calibrationInitialPartitionParameters=copy(value.partitionParameters||{})
 if(manualBase.value){
  value.parameterSources={...copy(manualBase.value.result.parameterSources||{}),sourceTaskId:manualBase.value.id}
  for(const [process,keys] of [['hydrology',Object.keys(value.parameters).filter(k=>k!=='lakeCoefficient')],['lake',['lakeCoefficient']]]){
   const changed=keys.filter(k=>value.parameters[k]!==manualBase.value.result.parameters[k])
   if(changed.length)value.parameterSources[process]={method:'manual-adjustment',sourceTaskId:manualBase.value.id,changedKeys:changed}
  }
 }
 if(transferMode(value)!=='duja-shared')throw Error('请新建方案，采用流域共享参数进行两阶段率定。')
 const builtin=selectedScheme.value?.builtin||value.builtin
 if(!schemeId.value||builtin||JSON.stringify(value)!==saved.value){
  for(const key of ['builtin','id','configurationSHA256','resultId'])delete value[key]
  const row=await api('schemes',{payload:value,...(schemeId.value&&!builtin?{parentId:schemeId.value}:{})})
  schemeId.value=row.id;draft.value=prepare(row.payload);saved.value=JSON.stringify(draft.value)
 }
 return schemeId.value
}
async function saveOnly(){working.value=true;error.value='';try{await ensureSaved();message.value='方案已保存。';emit('refresh')}catch(e){error.value=e.message}finally{working.value=false}}
async function run(kind='two-stage-calibration'){
 working.value=true;error.value='';message.value=''
 try{
  const id=await ensureSaved()
  const request={kind,researchSchemeId:id,name:draft.value.name+' · '+(kind==='trial'?'人工试算':modeName.value),clientRequestId:crypto.randomUUID()}
  if(kind==='two-stage-calibration')request.adoptOnSuccess=autoAdopt.value
  if(kind==='trial'){
   request.start=draft.value.historyPeriod?.start||draft.value.twoStage.lakePeriods.trainingStart
   request.end=manualBase.value?.result?.end||draft.value.historyPeriod?.end||(data.value?.forcing?.lastCompleteDay?shiftCalendarDay(data.value.forcing.lastCompleteDay,1)+'T00:00:00':draft.value.periods.evaluationEnd)
   if(manualBase.value&&Object.keys(draft.value.parameters).filter(k=>k!=='lakeCoefficient').every(k=>draft.value.parameters[k]===manualBase.value.result.parameters[k]))request.lakeOnlyBaseTaskId=manualBase.value.id
  }
  step.value=2;emit('submit',request)
 }catch(e){error.value=e.message}finally{working.value=false}
}
async function loadProgress(){
 const task=props.detail;if(!task||task.kind!=='two-stage-calibration'||!['running','queued','stopping'].includes(task.status))return
 const ticket=++progressTicket
 try{const next=await api('search-progress?'+new URLSearchParams({taskId:task.id,limit:120,after:0}));if(ticket===progressTicket&&!disposed)progress.value=next}catch{}
}
async function resume(){working.value=true;error.value='';try{await api('tasks/'+props.detail.id+'/resume',{});step.value=2;emit('refresh')}catch(e){error.value=e.message}finally{working.value=false}}
async function adopt(){working.value=true;error.value='';try{await api('business-adopt',{taskId:props.detail.id});message.value='已保存参数并采用成果，首页与历史模拟已同步。';emit('refresh')}catch(e){error.value=e.message}finally{working.value=false}}
function adjust(){
 if(!successful.value)return
 manualBase.value=copy(props.detail)
 const source=props.detail.result.scheme||selectedScheme.value?.payload||draft.value
 draft.value=prepare(source);draft.value.parameters=copy(finalParameters.value);draft.value.calibrationInitialParameters=copy(finalParameters.value);draft.value.partitionParameters={};draft.value.calibrationInitialPartitionParameters={}
 draft.value.name=(source.name||'色林错方案')+' · 人工调整';draft.value.initialization={mode:'automatic'}
 for(const key of ['builtin','id','familyId','revision','configurationSHA256','adoptedParameterId'])delete draft.value[key]
 schemeId.value='';saved.value='';step.value=1;group.value='land';message.value='修改参数后点击人工试算。仅调整湖泊系数时将复用已有来水。'
}
function openRecord(id){emit('select',id);step.value=3;historyOpen.value=false}
async function recycleScheme(){if(!schemeId.value)return;working.value=true;error.value='';try{await api('schemes/'+schemeId.value+'/metadata',{trashed:true});draft.value=null;schemeId.value='';emit('refresh')}catch(e){error.value=e.message}finally{working.value=false}}
onMounted(async()=>{
 try{
  const [coverage,flow,lake]=await Promise.all([api('calibration-data'),api('observation-series?station=DUJA&variable=flow&profile=quality-v2'),api('observation-series?station=SLC&variable=level&profile=quality-v2')])
  if(disposed)return
  data.value=coverage;observations.value={flow:flow.rows||[],lake:lake.rows||[]}
  if(!props.detail){const record=records.value.find(t=>['running','queued'].includes(t.status))||records.value.find(t=>t.id===props.boot.homeResultId);if(record)emit('select',record.id)}
 }catch(e){error.value=e.message}
 loadProgress();timer=setInterval(loadProgress,5000)
})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);progressTicket++})
</script>

<template>
 <section class="panel calibration-workbench" :data-coverage-ready="!!data">
  <div class="calibration-top"><div><span class="calibration-eyebrow">色林错流域 · 参数与计算</span><h2>模型方案与率定</h2><p>{{constrained?'杜加率定 → 湖位筛选共享参数 → 湖泊率定':'杜加流量率定水文参数，形成全流域来水，再率定湖泊水位。'}}</p></div><div class="calibration-current"><small>当前采用</small><b>{{currentName}}</b><span>陆域共用水文参数 · 湖泊参数单独率定</span></div></div>
  <div class="calibration-scheme-bar"><label>方案<select :value="schemeId" aria-label="率定方案" :disabled="locked" @change="chooseFromUser"><option v-if="!schemeId" value="">未保存的新方案</option><option v-for="s in schemes" :key="s.id" :value="s.id">{{s.name}}{{s.id===boot.homeSchemeId?' · 当前采用':''}}</option></select></label><button :disabled="locked" @click="fresh">新建</button><button :disabled="locked||!draft" @click="duplicate">复制</button><button :disabled="locked||!draft" @click="saveOnly">保存方案</button><button class="link" @click="historyOpen=!historyOpen">{{historyOpen?'收起记录':'我的计算记录'}}</button></div>
  <div v-if="historyOpen" class="calibration-history"><button v-for="t in records" :key="t.id" :class="{selected:t.id===detail?.id}" @click="openRecord(t.id)"><span>{{t.name}}</span><small>{{status(t)}} · {{(t.created_at||'').slice(0,10)}}</small></button><p v-if="!records.length" class="muted">暂无计算记录。</p></div>
  <nav class="calibration-steps" aria-label="率定步骤"><button v-for="(label,i) in steps" :key="i" :class="{active:step===i,passed:step>i}" :aria-current="step===i?'step':undefined" @click="step=i"><span>{{i+1}}</span><b>{{label}}</b></button></nav>
  <p v-if="error" class="message error" role="alert">{{error}}</p><p v-if="message" class="message success" role="status">{{message}}</p>
  <template v-if="draft">
   <div v-if="step===0" class="calibration-step-body">
    <div class="query-grid"><label class="grow">方案名称<input v-model="draft.name" :disabled="locked" maxlength="120"></label><label>预热起日<input type="date" :value="draft.periods.warmupStart.slice(0,10)" :disabled="locked" @input="setDay(draft.periods,'warmupStart',$event.target.value)"></label></div>
    <div class="calibration-mode-options"><button v-for="(label,key) in {both:'两阶段连续率定',flow:'仅率定杜加流量',lake:'仅率定湖泊水位'}" :key="key" :class="{selected:draft.twoStage.mode===key}" :disabled="locked" @click="draft.twoStage.mode=key">{{label}}</button></div>
    <label v-if="draft.twoStage.mode==='both'" class="calibration-selection-mode">共享参数的选择方式<select v-model="draft.twoStage.selectionMode" :disabled="locked"><option value="flow-constrained-lake">杜加流量约束下，用湖位筛选参数</option><option value="flow-only">仅由杜加流量确定参数</option></select></label>
    <p v-if="constrained" class="calibration-period-count">杜加 NSE ≥ {{draft.twoStage.flowConstraints.minNse}} · 水量偏差 ≤ {{draft.twoStage.flowConstraints.maxAbsPbias}}% · 湖位目标 0.10 m</p>
    <section v-if="draft.twoStage.mode!=='lake'" class="calibration-period-block"><div class="calibration-section-heading"><h3><span>01</span> 杜加流量</h3><span>{{data?.flow?.days||'—'}}个有效日 · {{data?.flow?.start?.slice(0,10)}}—{{data?.flow?.end?.slice(0,10)}}</span></div><div class="calibration-period-grid"><label v-for="(label,key) in {trainingStart:'率定起日',trainingEnd:'率定止日',evaluationStart:'检验起日',evaluationEnd:'检验止日'}" :key="key">{{label}}<input type="date" :aria-label="'流量'+label" :value="day(draft.periods,key)" :disabled="locked" @input="setDay(draft.periods,key,$event.target.value)"></label></div><p class="calibration-period-count">率定期 {{validCount('flow',draft.periods,'training')}}日 · 检验期 {{validCount('flow',draft.periods,'evaluation')}}日</p><div class="calibration-inline-options"><label><input type="checkbox" v-model="draft.twoStage.reverseCheck" :disabled="locked">交换两段资料再检验一次</label><label><input type="checkbox" v-model="draft.twoStage.finalRefit" :disabled="locked">检验后使用全部有效资料率定正式参数</label></div></section>
    <section v-if="draft.twoStage.mode!=='flow'" class="calibration-period-block"><div class="calibration-section-heading"><h3><span>02</span> 主站湖位</h3><span>{{data?.lake?.days||'—'}}个有效日 · {{data?.lake?.start?.slice(0,10)}}—{{data?.lake?.end?.slice(0,10)}}</span></div><div class="calibration-period-grid"><label v-for="(label,key) in {trainingStart:'率定起日',trainingEnd:'率定止日',evaluationStart:'检验起日',evaluationEnd:'检验止日'}" :key="key">{{label}}<input type="date" :aria-label="'湖位'+label" :value="day(draft.twoStage.lakePeriods,key)" :disabled="locked" @input="setDay(draft.twoStage.lakePeriods,key,$event.target.value)"></label></div><p class="calibration-period-count">率定期 {{validCount('lake',draft.twoStage.lakePeriods,'training')}}日 · 检验期 {{validCount('lake',draft.twoStage.lakePeriods,'evaluation')}}日</p></section>
    <p class="muted calibration-data-line">气象完整日：{{data?.forcing?.firstCompleteDay||'读取中'}} 至 {{data?.forcing?.lastCompleteDay||'—'}}。缺测日不参与评价，模拟保持连续。</p>
    <div class="actions"><button class="primary" @click="step=1">下一步：选择参数 →</button></div>
   </div>
   <div v-if="step===1" class="calibration-step-body">
    <div class="calibration-section-heading"><h3>参与率定的参数</h3><span>水文参数已选 {{selectedCount}} / {{flowKeys.length}} 项 · 湖面蒸发系数单独率定</span></div>
    <nav class="parameter-group-tabs"><button v-for="g in groups" :key="g.id" :class="{primary:group===g.id}" @click="group=g.id">{{g.name}}</button></nav>
    <div class="calibration-table-toolbar"><span>{{group==='lake'?'固定水文参数和入湖来水后，调整湖面蒸发转换系数。':group==='snow'?'融雪、融冰、温度与雪层过程参数。':'各入湖区域采用同一组参数。'}}</span><div v-if="group!=='lake'"><button class="link" :disabled="locked" @click="selectGroup(true)">全选本组</button><button class="link" :disabled="locked" @click="selectGroup(false)">取消本组</button></div></div>
    <div class="table-wrap calibration-parameter-table"><table><thead><tr><th>参与率定</th><th>参数</th><th>初值 / 人工设值</th><th>下界</th><th>上界</th><th>结果值</th></tr></thead><tbody><tr v-for="d in shownDefinitions" :key="d.key"><td><input v-if="flowKeys.includes(d.key)" type="checkbox" v-model="draft.search.keys" :value="d.key" :aria-label="'参与率定 '+d.label" :disabled="locked||draft.twoStage.mode==='lake'"><span v-else class="muted">{{d.group==='lake'?'第二阶段':'人工设置'}}</span></td><td><b>{{d.label}}</b><small>{{d.key}}<template v-if="d.unit"> · {{d.unit}}</template></small></td><td><input type="number" :aria-label="d.label+'初值'" :step="d.step" :min="d.min" :max="d.max" :value="initialValue(d)" :disabled="locked" @input="setParameter(d.key,$event)"></td><td><input type="number" :aria-label="d.label+'下界'" :step="d.step" v-model.number="draft.search.bounds[d.key][0]" :disabled="locked||(!flowKeys.includes(d.key)&&d.group!=='lake')"></td><td><input type="number" :aria-label="d.label+'上界'" :step="d.step" v-model.number="draft.search.bounds[d.key][1]" :disabled="locked||(!flowKeys.includes(d.key)&&d.group!=='lake')"></td><td class="parameter-result">{{number(finalParameters[d.key],6)}}</td></tr></tbody></table></div>
    <details class="calibration-details"><summary>参数来源与固定项说明</summary><p>水文参数由杜加流量率定后共享至计算陆域；各单元保留自己的气象、高程、面积和冰川覆盖。裸冰升华两项在当前模型中不改变流量，保留人工设置。</p><p v-for="d in shownDefinitions" :key="d.key"><b>{{d.label}}：</b>{{d.parameterBasis}}</p></details>
    <div class="actions"><button @click="step=0">← 时段</button><button v-if="manualBase" :disabled="locked" @click="run('trial')">人工试算</button><button class="primary" @click="step=2">下一步：开始率定 →</button></div>
   </div>
   <div v-if="step===2" class="calibration-step-body">
    <div class="calibration-run-summary"><div><small>计算方式</small><b>{{modeName}}</b></div><div><small>水文参数</small><b>{{selectedCount}}项 · 流域共享</b></div><div><small>搜索安排</small><b>{{constrained?'已有候选 → 湖位筛选':'分组交替 → 联合细化'}}</b></div><div><small>{{constrained?'候选 / 联合细化':'每次拟合预算'}}</small><b>{{constrained?number(draft.twoStage.candidateLimit,0)+' / '+number(draft.twoStage.jointBudget,0)+'次':number(totalBudget,0)+'次'}}</b></div></div>
    <p class="calibration-period-count">实际预热起点 {{actualWarmup?.slice(0,10)}} · 湖位起算 {{draft.initialization?.initialLakeTime?.slice(0,10)||data?.lake?.start?.slice(0,10)||'首个有效观测'}}</p>
    <details class="calibration-details"><summary>调整搜索次数与起点</summary><div v-if="constrained" class="query-grid"><label>首轮候选数<input type="number" min="1" max="256" v-model.number="draft.twoStage.candidateLimit" :disabled="locked"></label><label>联合细化次数<input type="number" min="0" max="2000" v-model.number="draft.twoStage.jointBudget" :disabled="locked"></label><label>杜加最低NSE<input type="number" min="0" max="1" step="0.01" v-model.number="draft.twoStage.flowConstraints.minNse" :disabled="locked"></label><label>最大水量偏差 · %<input type="number" min="0" max="100" step="0.5" v-model.number="draft.twoStage.flowConstraints.maxAbsPbias" :disabled="locked"></label></div><div v-else class="query-grid"><label v-for="(label,key) in {runoff:'产流次数',snow:'雪冰次数',routing:'汇流次数',joint:'联合细化次数'}" :key="key">{{label}}<input type="number" min="1" max="12000" v-model.number="draft.twoStage.budgets[key]" :disabled="locked"></label></div><div class="query-grid"><label>随机起点种子<input v-model="seedsText" :disabled="locked"></label></div><label v-if="!constrained"><input type="checkbox" v-model="draft.twoStage.allowExtension" :disabled="locked">搜索仍在改善时允许追加一轮，最多36000次</label></details>
    <div class="calibration-start-row"><label><input type="checkbox" v-model="autoAdopt" :disabled="locked">{{constrained?'结果改善后保存并采用':'完成后保存并采用，更新首页成果'}}</label><button class="primary" :disabled="locked||(!selectedCount&&draft.twoStage.mode!=='lake')" @click="run()">{{draft.twoStage.mode==='both'?'一键完成分阶段率定':'开始率定'}}</button><button :disabled="locked" @click="run('trial')">按当前参数试算</button></div>
    <div v-if="detail&&['running','queued','stopping'].includes(detail.status)" class="calibration-progress" role="status"><div><b>{{detail.name}}</b><span>{{number(detail.progress,1)}}%</span></div><progress :value="detail.progress" max="100"></progress><p>{{detail.message||'正在准备资料…'}}</p><div v-if="progress?.evaluations" class="calibration-progress-numbers"><span>已评价 {{number(progress.evaluations,0)}}次</span><span v-if="progress.eligible!=null">通过流量门限 {{number(progress.eligible,0)}}组</span><span v-if="progress.bestObjective!=null">当前最优目标 {{number(progress.bestObjective,6)}}</span></div><button @click="emit('stop',detail.id)">停止并保存进度</button><details v-if="traceRows.length"><summary>搜索过程</summary><Chart :rows="traceRows" :series="[{key:'bestObjective',label:'最优目标'}]" unit="目标值" :context="detail.id+':'+progress.fit"/></details></div>
    <p v-else class="muted">{{constrained?'复核候选流量 → 全流域来水与湖位筛选 → 湖泊参数率定。':'流量分年检验、全部资料率定和湖泊率定会依次执行。'}}你可以继续查看已有成果。</p>
   </div>
   <div v-if="step===3" class="calibration-step-body">
    <template v-if="detail"><div class="calibration-section-heading"><h3>{{detail.name||'计算成果'}}</h3><span>{{status(detail)}}</span></div>
     <p v-if="detail.status==='failed'" class="message error">{{detail.message}}</p><div v-if="['interrupted','cancelled','failed'].includes(detail.status)&&detail.kind==='two-stage-calibration'" class="actions"><button class="primary" :disabled="locked" @click="resume">继续已保存的率定</button></div><div v-if="['running','queued','stopping'].includes(detail.status)"><progress :value="detail.progress" max="100"></progress><p>{{detail.message}}</p><button @click="step=2">查看运行进度</button></div>
     <template v-if="successful">
      <p class="calibration-period-count">预热 {{detail.request?.start?.slice(0,10)}} 起 · {{workflow?.flowParameterSelectionUsesLakeLevel?'杜加流量与湖位共同筛选共享参数':'杜加流量确定共享参数'}}</p>
      <p v-if="detail.result.adoptionSkipped" class="muted">{{detail.result.adoptionSkipped.message}}</p>
      <div class="calibration-result-actions"><span v-if="adopted" class="calibration-adopted">✓ 当前采用成果</span><button class="primary" :disabled="locked||adopted" @click="adopt">{{adopted?'已保存并采用':'保存并采用'}}</button><button :disabled="locked" @click="adjust">人工微调并试算</button><button @click="emit('export',detail.id,'daily.csv')">导出日成果</button><button @click="emit('state')">查看过程与状态</button></div>
      <div v-if="fitRows.length" class="table-wrap calibration-fit-table"><table><thead><tr><th>流量率定</th><th>有效日</th><th>率定NSE</th><th>检验NSE</th><th>KGE</th><th>RMSE · m³/s</th><th>水量偏差</th></tr></thead><tbody><tr v-for="f in fitRows" :key="f.fit"><td>{{f.fit}}</td><td>{{f.training.count}}</td><td>{{number(f.training.nse,3)}}</td><td>{{number(f.validation?.nse,3)}}</td><td>{{number(f.training.kge,3)}}</td><td>{{number(f.training.rmse,2)}}</td><td>{{number(f.training.pbias,2)}}%</td></tr></tbody></table></div>
      <div v-if="workflow?.lake" class="calibration-run-summary lake-result-summary"><div><small>湖面蒸发系数</small><b>{{number(workflow.lake.final.value,4)}}</b></div><div><small>湖位率定RMSE</small><b>{{number(workflow.lake.split.training.rmse,3)}} m</b></div><div><small>湖位时间检验RMSE</small><b>{{number(workflow.lake.split.validation.rmse,3)}} m</b></div><div><small>{{workflow.finalRefit?'全部资料RMSE':'所选参数率定RMSE'}}</small><b>{{number(workflow.lake.final.training.rmse,3)}} m</b></div></div>
      <div v-if="selection" class="calibration-run-summary lake-result-summary"><div><small>杜加复核候选</small><b>{{number(selection.totalRescored,0)}}组</b></div><div><small>全流域候选</small><b>{{number(selection.basinCandidatesEvaluated,0)}}组</b></div><div><small>最终杜加NSE</small><b>{{number(selection.flowMetrics?.all?.nse,3)}}</b></div><div><small>水量偏差</small><b>{{number(selection.flowMetrics?.all?.pbias,2)}}%</b></div></div>
      <details v-if="warmupDiagnostics" class="calibration-details"><summary>预热效果核验</summary><p>参考条件：{{warmupCase(warmupDiagnostics.baselineCase||'')}}。对照不同起算时间和土壤初始蓄水，比较湖位起算后首年过程。流量差以1%、湖位差以1厘米为核验标准。</p><div class="table-wrap"><table><thead><tr><th>对照条件</th><th>流量过程差异</th><th>最大湖位差</th><th>核验</th></tr></thead><tbody><tr v-for="(row,name) in warmupDiagnostics.comparisons" :key="name"><td>{{warmupCase(name)}}</td><td>{{number(row.flowNrmsePercent,3)}}%</td><td>{{number(row.lakeLevelMaxAbsoluteDifferenceM*100,3)}} cm</td><td>{{row.passes?'达到标准':'仍有初态影响'}}</td></tr></tbody></table></div></details>
      <div class="calibration-result-charts"><section><h4>杜加流量过程</h4><Chart :rows="historyRows" :series="[{key:'duja',label:'模拟流量'},{key:'observedFlow',label:'杜加实测',observed:true}]" unit="m³/s" :context="detail.id+':cal-flow'"/></section><section><h4>湖泊水位过程</h4><Chart :rows="historyRows" :series="[{key:'lakeLevel',label:'模拟湖位'},{key:'observedLevel',label:'主站实测',observed:true}]" unit="m" :context="detail.id+':cal-lake'"/></section></div>
      <details class="calibration-details"><summary>完整参数与搜索记录</summary><div class="table-wrap"><table><thead><tr><th>参数</th><th>初值</th><th>结果</th><th>单位</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td>{{d.label}}</td><td>{{number(detail.request?.parameters?.[d.key],6)}}</td><td>{{number(finalParameters[d.key],6)}}</td><td>{{d.unit||'—'}}</td></tr></tbody></table></div><p v-for="f in fitRows" :key="f.fit">{{f.fit}}：{{number(f.evaluations,0)}}次，{{searchState(f)}}；种子 {{(f.seeds||detail.request?.twoStage?.seeds||[]).join('、')}}。轨迹与种群已保存。</p><p>{{selection?'本轮先按2021—2024年湖位筛选，再报告2025—2026年时间检验，最后单独拟合全部有效湖位。原流量分年率定记录保留原条件。':'分年检验记录与全部资料率定分别保存；全部资料拟合后的分数作为拟合评价。'}}</p><ParameterTransferAudit v-if="detail.result.parameterTransferAudit" :audit="detail.result.parameterTransferAudit" :definitions="definitions"/></details>
     </template>
    </template><p v-else class="empty">开始一次率定，或从“我的计算记录”选择成果。</p>
   </div>
  </template>
  <div v-if="step===0&&schemeId&&schemeId!==boot.homeSchemeId" class="calibration-secondary"><button class="link" :disabled="locked" @click="recycleScheme">将该方案移入回收站</button></div>
 </section>
</template>

<style scoped>
.calibration-selection-mode{max-width:620px;display:flex;flex-direction:column;gap:8px;margin:20px 0;font-size:13px}.calibration-selection-mode select{width:100%}
.calibration-workbench{padding:28px 32px;max-width:1480px;margin:0 auto}.calibration-top{display:flex;justify-content:space-between;gap:32px;align-items:center;margin-bottom:24px}.calibration-eyebrow{color:var(--muted);font-size:12px;letter-spacing:1.5px}.calibration-top h2{font-size:25px;margin:7px 0 10px;letter-spacing:1px}.calibration-top p{margin:0;color:var(--muted);font-size:14px}.calibration-current{display:flex;flex-direction:column;gap:5px;padding:14px 20px;border-left:3px solid var(--accent,#277f89);background:#edf5f4;min-width:270px}.calibration-current small,.calibration-current span{color:var(--muted);font-size:12px}.calibration-current b{font-size:15px}.calibration-scheme-bar{display:flex;gap:10px;align-items:flex-end}.calibration-scheme-bar>label{flex:1;max-width:620px}.calibration-scheme-bar>label select{width:100%}.calibration-scheme-bar>.link{margin-left:auto}.calibration-steps{display:grid;grid-template-columns:repeat(4,1fr);margin:26px 0 0;border-bottom:1px solid var(--line);gap:10px}.calibration-steps button{display:flex;align-items:center;gap:10px;padding:16px 8px;border:0;border-radius:0;border-bottom:3px solid transparent;background:none;color:var(--muted);justify-content:flex-start}.calibration-steps button>span{width:25px;height:25px;border-radius:50%;background:#edf2f3;display:grid;place-items:center;font-size:12px}.calibration-steps button.active{border-bottom-color:#277f89;color:#226f78}.calibration-steps button.active>span{background:#277f89;color:white}.calibration-steps button.passed>span{background:#e0efec;color:#277f89}.calibration-step-body{padding:26px 0 0;min-height:400px}.calibration-mode-options{display:flex;gap:9px;margin:20px 0}.calibration-mode-options button{padding:10px 18px}.calibration-mode-options .selected{border-color:#277f89;color:#226f78;background:#edf7f5}.calibration-section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:15px}.calibration-section-heading h3{font-size:16px;margin:0}.calibration-section-heading h3>span{font-size:12px;color:#277f89;letter-spacing:1px;margin-right:9px}.calibration-section-heading>span{font-size:12px;color:var(--muted)}.calibration-period-block{padding:22px 0;border-bottom:1px solid var(--line)}.calibration-period-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.calibration-period-count{color:#277f89;font-size:12px;margin:13px 0}.calibration-inline-options{display:flex;gap:26px;flex-wrap:wrap}.calibration-inline-options label,.calibration-start-row>label{display:flex;gap:8px;align-items:center;font-size:13px}.calibration-data-line{font-size:12px;margin:20px 0}.calibration-table-toolbar{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--muted);margin:15px 0}.calibration-parameter-table table{min-width:850px}.calibration-parameter-table td small{display:block;color:var(--muted);font-size:11px;margin-top:4px}.calibration-parameter-table td>b{font-size:13px;font-weight:500}.calibration-parameter-table input[type=number]{width:120px}.calibration-parameter-table input[type=checkbox]{width:17px;height:17px;accent-color:#277f89}.parameter-result{color:#277f89;font-variant-numeric:tabular-nums}.calibration-details{margin:22px 0;font-size:13px}.calibration-details summary{color:var(--muted);cursor:pointer}.calibration-details>p{color:var(--muted);line-height:1.8}.calibration-details .query-grid{margin:18px 0}.calibration-run-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:#f0f6f5;border:1px solid #dceae6}.calibration-run-summary>div{display:flex;flex-direction:column;gap:11px;padding:23px 20px;border-right:1px solid #dceae6}.calibration-run-summary>div:last-child{border:0}.calibration-run-summary small{color:var(--muted);font-size:12px}.calibration-run-summary b{font-size:17px;font-weight:500}.calibration-start-row{display:flex;align-items:center;gap:14px;margin:30px 0}.calibration-start-row>label{margin-right:auto}.calibration-start-row .primary{padding:12px 24px}.calibration-progress{border-top:1px solid var(--line);padding-top:24px;margin-top:24px}.calibration-progress>div:first-child{display:flex;justify-content:space-between;margin-bottom:10px}.calibration-progress progress{width:100%;height:8px;accent-color:#277f89}.calibration-progress p{color:var(--muted);font-size:13px}.calibration-progress-numbers{display:flex;gap:24px;margin:14px 0;font-size:13px;color:#277f89}.calibration-history{border:1px solid var(--line);margin-top:12px;max-height:260px;overflow:auto}.calibration-history>button{display:flex;justify-content:space-between;text-align:left;width:100%;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:12px 16px}.calibration-history small{color:var(--muted)}.calibration-history .selected{background:#edf7f5}.calibration-result-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:22px 0}.calibration-adopted{color:#277f89;margin-right:auto}.calibration-fit-table{margin:20px 0}.lake-result-summary{margin:22px 0}.calibration-result-charts{display:grid;grid-template-columns:1fr 1fr;gap:22px}.calibration-result-charts>section{min-width:0}.calibration-result-charts h4{font-size:14px;margin:18px 0 0}.calibration-result-charts :deep(.chart){height:290px}.calibration-secondary{border-top:1px solid var(--line);margin-top:25px;padding-top:15px}.actions{margin-top:24px}.calibration-workbench input[type=checkbox]{accent-color:#277f89}
@media(max-width:1100px){.calibration-workbench{padding:22px}.calibration-top{align-items:flex-start}.calibration-current{min-width:220px}.calibration-run-summary b{font-size:14px}.calibration-run-summary>div{padding:18px 13px}.calibration-result-charts{grid-template-columns:1fr}}
@media(max-width:760px){.calibration-top{flex-direction:column;gap:16px}.calibration-current{width:100%}.calibration-scheme-bar{flex-wrap:wrap}.calibration-scheme-bar>label{flex-basis:100%}.calibration-steps{gap:0}.calibration-steps button{flex-direction:column;font-size:12px;padding:10px 2px}.calibration-period-grid,.calibration-run-summary{grid-template-columns:1fr 1fr}.calibration-section-heading{align-items:flex-start;flex-direction:column;gap:8px}.calibration-start-row{flex-wrap:wrap}.calibration-start-row>label{width:100%;margin-bottom:10px}.calibration-mode-options{flex-wrap:wrap}.calibration-top h2{font-size:22px}}
</style>
