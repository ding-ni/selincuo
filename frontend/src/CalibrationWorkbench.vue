<script setup>
import {ref,computed,watch,onMounted,onBeforeUnmount,nextTick} from 'vue'
import Chart from './Chart.vue'
import ParameterTransferAudit from './ParameterTransferAudit.vue'
import SchemeManager from './SchemeManager.vue'
import {api,number} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
import {liftDujaToShared,transferMode} from './parameter-transfer.mjs'
import {calibrationRecord,calibrationCounts,calibrationRecordObjective,recordDownload} from './calibration-record.mjs'
const props=defineProps({boot:Object,tasks:Array,detail:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','stop','select','state','refresh','simulate','export','deleted','replay'])
const steps=['资料与时段','参数与范围','运行率定','结果评价']
const executable=ref(null),allHistory=ref(null),lakeOnlyInitial=ref(null)
const step=ref(0),schemeId=ref(''),draft=ref(null),saved=ref(''),group=ref('land'),working=ref(false),error=ref(''),message=ref(''),data=ref(null),observations=ref({flow:[],lake:[]}),progress=ref(null),autoAdopt=ref(false),manualBase=ref(null),seedsText=ref('42, 142, 242'),historyOpen=ref(false),replayPlan=ref(null),replayLoading=ref(false)
let timer,progressTicket=0,replayTicket=0,recordTicket=0,disposed=false
const replayDialog=ref(null)
const editingInitial=ref('')
const managerOpen=ref(false),managedSchemes=ref([])
const recordedPlan=ref(null)
const replayHistory=ref([]),replayHistoryOpen=ref(false),replayHistoryLoading=ref(false)
const copy=v=>JSON.parse(JSON.stringify(v))
const schemes=computed(()=>[...new Map([...managedSchemes.value,...(props.boot.schemes||[])].map(row=>[row.id,row])).values()])
const dirty=computed(()=>draft.value&&(JSON.stringify(draft.value)!==saved.value||JSON.stringify(seedsText.value.split(/[,，\s]+/).filter(Boolean).map(Number))!==JSON.stringify(draft.value.twoStage.seeds)||(draft.value.twoStage.mode==='lake'&&lakeOnlyInitial.value!==draft.value.parameters.lakeCoefficient)))
const locked=computed(()=>props.busy||working.value)
const selectedScheme=computed(()=>schemes.value.find(s=>s.id===schemeId.value))
const currentName=computed(()=>props.boot.currentBusiness?.name||schemes.value.find(s=>s.id===props.boot.homeSchemeId)?.name||'色林错模拟方案')
const records=computed(()=>(allHistory.value||props.tasks||[]).filter(t=>!t.trashed&&['two-stage-calibration','calibration','trial','calibration-replay'].includes(t.kind)))
const definitions=computed(()=>props.boot.parameterDefinitions||[])
const flowKeys=computed(()=>props.boot.calibrationConfig?.flowSelectableKeys||[])
const groups=[{id:'land',name:'陆面产流'},{id:'snow',name:'积雪与冰川'},{id:'routing',name:'汇流'},{id:'lake',name:'湖泊'}]
const shownDefinitions=computed(()=>definitions.value.filter(d=>d.group===(draft.value?.twoStage?.mode==='lake'?'lake':group.value)&&(flowKeys.value.includes(d.key)||d.key==='lakeCoefficient')))
const workflow=computed(()=>props.detail?.result?.calibrationWorkflow)
const fitRows=computed(()=>workflow.value?.flowFits||[])
const finalParameters=computed(()=>props.detail?.result?.parameters||{})
const recordedParameters=computed(()=>calibrationRecord(props.detail,definitions.value))
const recordedCounts=computed(()=>calibrationCounts(props.detail))
const isReplay=computed(()=>props.detail?.kind==='calibration-replay')
const canReplay=computed(()=>props.detail?.kind==='two-stage-calibration'&&recordedPlan.value?.sourceTaskId===props.detail.id)
const replayComparison=computed(()=>props.detail?.result?.replay?.comparison)
const seedCount=computed(()=>seedsText.value.split(/[,，\s]+/).filter(Boolean).length)
const recordOptions=computed(()=>props.detail?.request?.twoStage||{})
const recordSearch=computed(()=>props.detail?.request?.search||{})
const recordSeeds=computed(()=>recordOptions.value.seeds||recordSearch.value.seeds||[])
const recordObjective=computed(()=>calibrationRecordObjective(props.detail,recordedPlan.value||replayPlan.value))
const recordInitialization=computed(()=>props.detail?.result?.initialization||props.detail?.result?.replay?.configuration?.initialization||recordedPlan.value?.configuration?.initialization)
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
const initialValue=d=>draft.value?.twoStage?.mode==='lake'&&d.key==='lakeCoefficient'?lakeOnlyInitial.value:draft.value?.calibrationInitialParameters?.[d.key]
function initialDisplay(d){
 const value=initialValue(d)
 return editingInitial.value===d.key||typeof value!=='number'||!Number.isFinite(value)?value:Number(value.toPrecision(8))
}
const currentResults=computed(()=>props.detail?.request?.calibrationConfigurationSHA256&&props.detail.request.calibrationConfigurationSHA256===executable.value?.configurationSHA256?finalParameters.value:selectedScheme.value?.payload?.parameters||{})
const continuous=computed(()=>draft.value?.twoStage?.selectionMode==='continuous-dual')
async function showHistory(){historyOpen.value=!historyOpen.value;if(historyOpen.value){try{allHistory.value=(await api('tasks?includeExperiments=true')).tasks}catch(e){error.value=e.message}}}
function useFinalAsInitial(){
 if(draft.value.twoStage.mode==='lake'){
  const value=currentResults.value.lakeCoefficient??draft.value.parameters.lakeCoefficient
  if(typeof value!=='number'||!Number.isFinite(value)){error.value='没有可用的湖泊参数结果。';return}
  lakeOnlyInitial.value=value
  message.value='已将上次湖泊结果设为本次初值。'
  return
 }
 draft.value.calibrationInitialParameters=copy(currentResults.value);message.value='已将上次结果设为本次初值，开始率定时自动保存。'
}
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
 v.calibrationInitialParameters=copy(v.calibrationInitialParameters||v.parameters)
 return v
}
async function choose(id){
 const row=schemes.value.find(s=>s.id===id);if(!row)return
 draft.value=prepare(row.payload);schemeId.value=id;saved.value=JSON.stringify(draft.value);seedsText.value=draft.value.twoStage.seeds.join(', ');manualBase.value=null;error.value=''
 executable.value=null
 try{const config=await api('schemes/'+id+'/calibration-configuration');if(schemeId.value===id)executable.value=config}catch(e){if(schemeId.value===id)error.value=e.message}
}
async function loadTaskSettings(){
 const id=props.detail?.request?.researchSchemeId;if(!id)return
 working.value=true;error.value=''
 try{const config=await api('schemes/'+id+'/calibration-configuration');draft.value=prepare(config.form);schemeId.value=id;executable.value=config;saved.value=JSON.stringify(draft.value);seedsText.value=draft.value.twoStage.seeds.join(', ');step.value=0;manualBase.value=null}
 catch(e){error.value=e.message}finally{working.value=false}
}
function chooseFromUser(event){
 if(dirty.value&&!confirm('当前修改尚未保存，仍要切换方案吗？')){event.target.value=schemeId.value;return}
 choose(event.target.value)
}
watch(schemes,rows=>{
 if(!draft.value&&rows.length)choose(rows.find(r=>r.id===props.boot.currentBusiness?.schemeId)?.id||rows.find(r=>r.payload?.twoStage)?.id||rows[0].id)
},{immediate:true})
watch(()=>props.boot.currentBusiness?.schemeId,id=>{if(id&&!dirty.value&&schemes.value.some(r=>r.id===id))choose(id)})
watch(()=>props.detail?.id,()=>{progress.value=null;progressTicket++;replayPlan.value=null;recordedPlan.value=null;replayLoading.value=false;replayHistoryOpen.value=false;replayTicket++;recordTicket++;if(props.detail)step.value=['running','queued'].includes(props.detail.status)?2:3;loadFrozenRecord()})
watch(()=>props.detail?.status,value=>{if(value==='succeeded')step.value=3})
watch(()=>[draft.value,draft.value?.twoStage?.mode],([value,mode])=>{
 if(!value)return
 if(mode==='lake'){group.value='lake';lakeOnlyInitial.value=value.parameters.lakeCoefficient}
 else if(mode==='flow'&&group.value==='lake')group.value='land'
})
async function fresh(){
 if(dirty.value&&!confirm('当前设置尚未保存，仍要新建方案吗？'))return
 working.value=true;error.value=''
 try{draft.value=prepare(await api('research-template'));draft.value.name='我的色林错方案';schemeId.value='';saved.value='';executable.value=null;manualBase.value=null;step.value=0;seedsText.value=draft.value.twoStage.seeds.join(', ');autoAdopt.value=false;emit('select','');message.value='新方案已建立，填写名称后保存。'}
 catch(e){error.value=e.message}finally{working.value=false}
}
function duplicate(){
 if(!draft.value)return
 const source={schemeId:schemeId.value||null,configurationSHA256:executable.value?.configurationSHA256||draft.value.configurationSHA256||null}
 draft.value=prepare(draft.value)
 if(transferMode(draft.value)==='legacy')draft.value=liftDujaToShared(draft.value,'duja-shared',{schemeId:schemeId.value,configurationSHA256:draft.value.configurationSHA256})
 draft.value.name+=' · 副本';draft.value.purpose='business';draft.value.origin='user';draft.value.sourceCopy=source
 schemeId.value='';saved.value='';executable.value=null;manualBase.value=null;step.value=0;autoAdopt.value=false
 for(const key of ['builtin','id','familyId','revision','parentRevisionId','configurationSHA256','adoptedParameterId','resultId'])delete draft.value[key]
 emit('select','');message.value='副本已建立，修改名称后保存为独立方案。'
}
async function openManaged(row,asCopy=false){
 if(dirty.value&&!confirm('当前设置尚未保存，仍要切换方案吗？'))return
 managedSchemes.value=[...managedSchemes.value.filter(item=>item.id!==row.id),row]
 await choose(row.id);managerOpen.value=false;step.value=0;emit('select','')
 if(asCopy)duplicate()
}
async function schemesChanged(){
 try{
  const catalog=await api('scheme-management');managedSchemes.value=catalog.schemes.filter(row=>!row.trashed)
  if(schemeId.value&&!managedSchemes.value.some(row=>row.id===schemeId.value)){
   draft.value=null;schemeId.value='';executable.value=null;emit('select','')
   const row=catalog.schemes.find(item=>item.current&&!item.trashed)
   if(row){managedSchemes.value.push(row);await choose(row.id);step.value=0}
  }
  emit('refresh')
 }catch(e){error.value=e.message}
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
function setParameter(key,event){
 const value=event.target.value===''?null:Number(event.target.value)
 if(draft.value.twoStage.mode==='lake'&&key==='lakeCoefficient'){lakeOnlyInitial.value=value;return}
 draft.value.calibrationInitialParameters[key]=value;if(manualBase.value)draft.value.parameters[key]=value
}
async function ensureSaved(){
 const value=copy(draft.value)
 value.twoStage.seeds=seedsText.value.split(/[,，\s]+/).filter(Boolean).map(Number)
 if(value.twoStage.mode==='lake'){
  value.parameters.lakeCoefficient=lakeOnlyInitial.value
  value.calibrationInitialParameters.lakeCoefficient=lakeOnlyInitial.value
 }
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
 executable.value=await api('schemes/'+schemeId.value+'/calibration-configuration')
 return schemeId.value
}
async function saveOnly(){working.value=true;error.value='';try{await ensureSaved();message.value='方案已保存。';emit('refresh')}catch(e){error.value=e.message}finally{working.value=false}}
async function run(kind='two-stage-calibration'){
 working.value=true;error.value='';message.value=''
 try{
  if(kind==='trial'){
   if(draft.value.twoStage.mode==='lake')draft.value.parameters.lakeCoefficient=lakeOnlyInitial.value
   else draft.value.parameters=copy(draft.value.calibrationInitialParameters)
  }
  const id=await ensureSaved()
  const request={kind,researchSchemeId:id,name:draft.value.name+' · '+(kind==='trial'?'人工试算':modeName.value),clientRequestId:crypto.randomUUID()}
  if(kind==='two-stage-calibration'){
   if(!executable.value?.configurationSHA256)throw Error('请先保存绑定驱动的率定配置。')
   request.calibrationConfigurationSHA256=executable.value.configurationSHA256
   request.adoptOnSuccess=autoAdopt.value
  }
  if(kind==='trial'){
   request.start=draft.value.historyPeriod?.start||draft.value.twoStage.lakePeriods.trainingStart
   request.end=manualBase.value?.result?.end||draft.value.historyPeriod?.end||(data.value?.forcing?.lastCompleteDay?shiftCalendarDay(data.value.forcing.lastCompleteDay,1)+'T00:00:00':draft.value.periods.evaluationEnd)
   if(manualBase.value&&Object.keys(draft.value.parameters).filter(k=>k!=='lakeCoefficient').every(k=>draft.value.parameters[k]===manualBase.value.result.parameters[k]))request.lakeOnlyBaseTaskId=manualBase.value.id
  }
  step.value=2;emit('submit',request)
 }catch(e){error.value=e.message}finally{working.value=false}
}
async function loadProgress(){
 const task=props.detail;if(!task||!['two-stage-calibration','calibration-replay'].includes(task.kind)||!['running','queued','stopping'].includes(task.status))return
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
async function showReplayHistory(){
 replayHistoryOpen.value=!replayHistoryOpen.value
 if(!replayHistoryOpen.value)return
 replayHistoryLoading.value=true;error.value=''
 const source=props.detail?.request?.replay?.sourceTaskId||props.detail?.id
 try{const all=await api('tasks?includeExperiments=true');replayHistory.value=(all.tasks||[]).filter(t=>t.kind==='calibration-replay'&&(t.request?.replay?.sourceTaskId||t.request?.replaySourceTaskId)===source&&!t.trashed)}
 catch(e){error.value=e.message}finally{replayHistoryLoading.value=false}
}
async function openReplay(){
 const task=props.detail;if(!task||task.kind!=='two-stage-calibration')return
 const ticket=++replayTicket;replayLoading.value=true;error.value=''
 try{const value=await api('tasks/'+task.id+'/replay-plan');if(ticket===replayTicket&&!disposed){recordedPlan.value=value;replayPlan.value=value;await nextTick();replayDialog.value?.focus()}}
 catch(e){if(ticket===replayTicket)error.value=e.message}finally{if(ticket===replayTicket)replayLoading.value=false}
}
async function loadFrozenRecord(){
 const task=props.detail;if(!['two-stage-calibration','calibration-replay'].includes(task?.kind)||!task.result)return
 if(task.request?.twoStage?.selectionMode==='continuous-dual')return
 const source=task.request?.replay?.sourceTaskId||task.result?.replay?.sourceTaskId||task.id
 const ticket=++recordTicket
 try{const plan=await api('tasks/'+source+'/replay-plan');if(ticket===recordTicket&&!disposed&&props.detail?.id===task.id)recordedPlan.value=plan}catch{}
}
async function startReplay(){
 const plan=replayPlan.value;if(!plan?.available)return
 working.value=true;error.value=''
 try{const task=await api('tasks/'+plan.sourceTaskId+'/replay',{planSHA256:plan.planSHA256,clientRequestId:crypto.randomUUID()});replayPlan.value=null;message.value='复现已开始，成果将单独保存。';emit('replay',task)}
 catch(e){error.value=e.message}finally{working.value=false}
}
function downloadSettings(){
 if(!props.detail)return
 const url=URL.createObjectURL(new Blob([JSON.stringify(recordDownload(props.detail,definitions.value,recordedPlan.value||replayPlan.value),null,2)],{type:'application/json;charset=utf-8'}))
 const link=document.createElement('a');link.href=url;link.download='本次率定记录-'+props.detail.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
onMounted(async()=>{
 try{
  const [coverage,flow,lake]=await Promise.all([api('calibration-data'),api('observation-series?station=DUJA&variable=flow&profile=quality-v2'),api('observation-series?station=SLC&variable=level&profile=quality-v2')])
  if(disposed)return
  data.value=coverage;observations.value={flow:flow.rows||[],lake:lake.rows||[]}
  if(!props.detail){const record=records.value.find(t=>['running','queued'].includes(t.status))||records.value.find(t=>t.id===props.boot.homeResultId);if(record)emit('select',record.id)}
 }catch(e){error.value=e.message}
 loadProgress();loadFrozenRecord();timer=setInterval(loadProgress,5000)
})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);progressTicket++;replayTicket++;recordTicket++})
</script>

<template>
 <section class="panel calibration-workbench" :data-coverage-ready="!!data">
  <div class="calibration-heading">
   <div class="calibration-heading-main"><div class="calibration-heading-meta"><span>模型方案与率定</span><span class="scheme-status" :class="{adopted:schemeId===boot.currentBusiness?.schemeId}">{{!schemeId?'新方案':schemeId===boot.currentBusiness?.schemeId?'当前采用':'我的方案'}}</span></div><h2>{{draft?.name||currentName}}</h2><p>杜加流量率定 <span>→</span> 流量与湖位联合优选 <span>→</span> 连续模拟</p></div>
   <div class="calibration-save"><span :class="{unsaved:dirty}">{{dirty?'有未保存的修改':'设置已保存'}}</span><button class="primary" :disabled="locked||!dirty" @click="saveOnly">保存方案</button></div>
  </div>
  <div class="calibration-toolbar"><div class="scheme-tools"><button :disabled="locked" @click="fresh">＋ 新建方案</button><button :disabled="locked||!draft" @click="duplicate">复制方案</button><button :disabled="locked" @click="managerOpen=true">管理方案</button></div><button class="link" @click="showHistory">{{historyOpen?'收起计算记录':'计算记录'}} <span aria-hidden="true">↗</span></button></div>
  <SchemeManager v-if="managerOpen" @close="managerOpen=false" @open="openManaged($event)" @copy="openManaged($event,true)" @changed="schemesChanged"/>
  <div v-if="historyOpen" class="calibration-history"><button v-for="t in records" :key="t.id" :class="{selected:t.id===detail?.id}" @click="openRecord(t.id)"><span>{{t.name}}</span><small>{{status(t)}} · {{(t.created_at||'').slice(0,10)}}</small></button><p v-if="!records.length" class="muted">暂无计算记录。</p></div>
  <nav class="calibration-steps" aria-label="率定步骤"><button v-for="(label,i) in steps" :key="i" :class="{active:step===i,passed:step>i}" :aria-current="step===i?'step':undefined" @click="step=i"><span>{{i+1}}</span><b>{{label}}</b></button></nav>
  <p v-if="error" class="message error" role="alert">{{error}}</p><p v-if="message" class="message success" role="status">{{message}}</p>
  <template v-if="draft">
   <div v-if="step===0" class="calibration-step-body">
    <div class="query-grid"><label class="grow">方案名称<input v-model="draft.name" :disabled="locked" maxlength="120"></label><label>预热起日<input type="date" :value="draft.periods.warmupStart.slice(0,10)" :disabled="locked" @input="setDay(draft.periods,'warmupStart',$event.target.value)"></label></div>
    <div class="workflow-phases"><b>分期率定与检验</b><span>→</span><b>全部资料正式定参</b></div><details class="calibration-details"><summary>单独重做一个阶段</summary><div class="calibration-mode-options"><button v-for="(label,key) in {both:'完整率定',flow:'仅重做水文率定',lake:'仅重做湖泊率定'}" :key="key" :class="{selected:draft.twoStage.mode===key}" :disabled="locked" @click="draft.twoStage.mode=key">{{label}}</button></div></details>
    <section v-if="draft.twoStage.mode!=='lake'" class="calibration-period-block"><div class="calibration-section-heading"><h3><span>01</span> 杜加流量</h3><span>{{data?.flow?.days||'—'}}个有效日</span></div><div class="calibration-period-grid"><label v-for="(label,key) in {trainingStart:'率定起日',trainingEnd:'率定止日',evaluationStart:'检验起日',evaluationEnd:'检验止日'}" :key="key">{{label}}<input type="date" :aria-label="'流量'+label" :value="day(draft.periods,key)" :disabled="locked" @input="setDay(draft.periods,key,$event.target.value)"></label></div><p class="calibration-period-count">率定期 {{validCount('flow',draft.periods,'training')}}日 · 检验期 {{validCount('flow',draft.periods,'evaluation')}}日</p></section>
    <section v-if="draft.twoStage.mode!=='flow'" class="calibration-period-block"><div class="calibration-section-heading"><h3><span>02</span> 主站湖位</h3><span>{{data?.lake?.days||'—'}}个有效日 · {{data?.lake?.start?.slice(0,10)}}—{{data?.lake?.end?.slice(0,10)}}</span></div><div class="calibration-period-grid"><label v-for="(label,key) in {trainingStart:'率定起日',trainingEnd:'率定止日',evaluationStart:'检验起日',evaluationEnd:'检验止日'}" :key="key">{{label}}<input type="date" :aria-label="'湖位'+label" :value="day(draft.twoStage.lakePeriods,key)" :disabled="locked" @input="setDay(draft.twoStage.lakePeriods,key,$event.target.value)"></label></div><p class="calibration-period-count">率定期 {{validCount('lake',draft.twoStage.lakePeriods,'training')}}日 · 检验期 {{validCount('lake',draft.twoStage.lakePeriods,'evaluation')}}日</p></section>
    <p class="calibration-data-line">驱动：{{boot.inputs?.find(i=>i.id===draft.inputId)?.name||'方案已绑定气象资料'}} · 湖位起算 {{executable?.initialization?.time||draft.initialization?.initialLakeTime||'首个有效实测'}} <template v-if="draft.initialization?.initialLakeLevel!=null"> · {{draft.initialization.initialLakeLevel}} m</template></p>
    <div class="actions"><button class="primary" @click="step=1">下一步：选择参数 →</button></div>
   </div>
   <div v-if="step===1" class="calibration-step-body">
    <div class="calibration-section-heading"><h3>参数与搜索范围</h3><span>{{draft.twoStage.mode==='lake'?'水文参数保持固定':'水文参选 '+selectedCount+' / '+flowKeys.length+' 项'}}</span><button class="link" :disabled="locked" @click="useFinalAsInitial">以上次结果为初值</button></div>
    <nav class="parameter-group-tabs"><button v-for="g in groups.filter(g=>draft.twoStage.mode==='lake'?g.id==='lake':draft.twoStage.mode==='flow'?g.id!=='lake':true)" :key="g.id" :class="{primary:group===g.id}" @click="group=g.id">{{g.name}}</button></nav>
    <div class="calibration-table-toolbar"><span>{{draft.twoStage.mode==='lake'||group==='lake'?'固定水文参数和入湖来水后，调整湖面蒸发转换系数。':group==='snow'?'融雪、融冰、温度与雪层过程参数。':'各入湖区域采用同一组参数。'}}</span><div v-if="draft.twoStage.mode!=='lake'&&group!=='lake'"><button class="link" :disabled="locked" @click="selectGroup(true)">全选本组</button><button class="link" :disabled="locked" @click="selectGroup(false)">取消本组</button></div></div>
    <div class="table-wrap calibration-parameter-table"><table><thead><tr><th>参与率定</th><th>参数</th><th>初值</th><th>下界</th><th>上界</th><th>结果值</th></tr></thead><tbody><tr v-for="d in shownDefinitions" :key="d.key"><td><input v-if="flowKeys.includes(d.key)" type="checkbox" v-model="draft.search.keys" :value="d.key" :aria-label="'参与率定 '+d.label" :disabled="locked||draft.twoStage.mode==='lake'"><span v-else class="muted">{{d.group==='lake'?'湖泊阶段':'人工设置'}}</span></td><td><b>{{d.label}}</b><small>{{d.key}}<template v-if="d.unit"> · {{d.unit}}</template></small></td><td><input type="number" :aria-label="d.label+'初值'" :step="d.step" :min="d.min" :max="d.max" :value="initialDisplay(d)" :title="String(initialValue(d)??'')" :disabled="locked" @focus="editingInitial=d.key" @blur="editingInitial=''" @input="setParameter(d.key,$event)"></td><td><input type="number" :aria-label="d.label+'下界'" :step="d.step" v-model.number="draft.search.bounds[d.key][0]" :disabled="locked||(!flowKeys.includes(d.key)&&d.group!=='lake')"></td><td><input type="number" :aria-label="d.label+'上界'" :step="d.step" v-model.number="draft.search.bounds[d.key][1]" :disabled="locked||(!flowKeys.includes(d.key)&&d.group!=='lake')"></td><td class="parameter-result" :title="String(currentResults[d.key]??'')">{{number(currentResults[d.key],6)}}</td></tr></tbody></table></div>
    <div class="actions"><button @click="step=0">← 时段</button><button v-if="manualBase" :disabled="locked" @click="run('trial')">人工试算</button><button class="primary" @click="step=2">下一步：开始率定 →</button></div>
   </div>
   <div v-if="step===2" class="calibration-step-body">
    <template v-if="!isReplay||!['running','queued','stopping'].includes(detail?.status)">
    <div class="calibration-run-summary"><div><small>计算方式</small><b>{{draft.twoStage.mode==='both'?'流量与湖位完整率定':modeName}}</b></div><div><small>水文参数</small><b>{{draft.twoStage.mode==='lake'?'固定现有参数':selectedCount+'项 · 流域共享'}}</b></div><template v-if="draft.twoStage.mode!=='lake'"><div><small>每阶段基础搜索</small><b>{{number(totalBudget,0)}}次</b></div><div><small>搜索起点</small><b>{{seedCount}}组</b></div></template><template v-else><div><small>入湖来水</small><b>按固定水文参数计算</b></div><div><small>率定目标</small><b>湖位过程误差</b></div></template></div>
    <p class="calibration-period-count">实际预热起点 {{actualWarmup?.slice(0,10)}} · 湖位起算 {{draft.initialization?.initialLakeTime?.slice(0,10)||data?.lake?.start?.slice(0,10)||'首个有效观测'}}</p>
    <section v-if="continuous&&draft.twoStage.mode==='both'" class="objective-settings"><h3>联合目标权重</h3><div class="query-grid"><label>杜加流量<input aria-label="杜加流量目标权重" type="number" min="0.01" max="0.99" step="0.05" v-model.number="draft.twoStage.objective.weights.flow" :disabled="locked"></label><label>主站湖位<input aria-label="主站湖位目标权重" type="number" min="0.01" max="0.99" step="0.05" v-model.number="draft.twoStage.objective.weights.lake" :disabled="locked"></label></div></section>
    <details v-if="draft.twoStage.mode!=='lake'" class="calibration-details" open><summary>搜索规模</summary><div class="query-grid"><label v-for="(label,key) in {runoff:'陆面产流',snow:'积雪与冰川',routing:'汇流',joint:'水文联合细化'}" :key="key">{{label}} · 次<input type="number" min="1" max="12000" :aria-label="label+'搜索次数'" v-model.number="draft.twoStage.budgets[key]" :disabled="locked"></label></div><div v-if="draft.twoStage.mode==='both'" class="query-grid"><label>全流域代表候选 · 组<input type="number" min="1" max="256" v-model.number="draft.twoStage.candidateLimit" :disabled="locked"></label><label>流量—湖位联合评价 · 次<input type="number" min="0" max="2000" v-model.number="draft.twoStage.jointBudget" :disabled="locked"></label></div><label class="inline-option"><input type="checkbox" v-model="draft.twoStage.allowExtension" :disabled="locked">搜索仍改善时追加一轮，累计最多36000次</label></details>
    <details v-if="continuous&&draft.twoStage.mode!=='lake'" class="calibration-details"><summary>目标函数分项与搜索编号</summary><p>各组权重合计为1。搜索按数值收敛或预算结束。</p><div class="query-grid"><label v-for="(label,key) in {nse:'流量过程',logNse:'低流量过程',volume:'径流总量'}" :key="key">{{label}}权重<input type="number" min="0" max="1" step="0.05" v-model.number="draft.twoStage.objective.flowWeights[key]" :disabled="locked"></label></div><div class="query-grid"><label v-for="(label,key) in {rmse:'湖位过程',trend:'累计趋势',amplitude:'季节涨幅',peakTiming:'高点日期'}" :key="key">{{label}}权重<input type="number" min="0" max="1" step="0.05" v-model.number="draft.twoStage.objective.lakeWeights[key]" :disabled="locked"></label></div><div class="query-grid"><label v-for="(label,key) in {rmseM:'湖位误差尺度 · m',trendErrorM:'趋势误差尺度 · m',amplitudeErrorM:'涨幅误差尺度 · m',peakTimingErrorDays:'高点误差尺度 · 天'}" :key="key">{{label}}<input type="number" min="0.001" step="any" v-model.number="draft.twoStage.objective.lakeScales[key]" :disabled="locked"></label></div><label>多起点随机编号<input v-model="seedsText" :disabled="locked"></label></details>
    <div class="calibration-start-row"><button class="primary" :disabled="locked||(!selectedCount&&draft.twoStage.mode!=='lake')" @click="run()">开始率定</button><button :disabled="locked" @click="run('trial')">按当前参数试算</button><label><input type="checkbox" v-model="autoAdopt" :disabled="locked">完成后采用</label></div>
    </template><div v-else class="calibration-record-heading"><div><b>正在复现本次率定</b><p class="muted">使用原记录的资料、参数范围和搜索设置，结果单独保存。</p></div></div>
    <div v-if="detail&&['running','queued','stopping'].includes(detail.status)" class="calibration-progress" role="status"><div><b>{{detail.name}}</b><span>{{number(detail.progress,1)}}%</span></div><progress :value="detail.progress" max="100"></progress><p>{{detail.message||'正在准备资料…'}}</p><div v-if="progress?.evaluations" class="calibration-progress-numbers"><span>已评价 {{number(progress.evaluations,0)}}次</span><span v-if="progress.eligible!=null">通过流量门限 {{number(progress.eligible,0)}}组</span><span v-if="progress.bestObjective!=null">当前最优目标 {{number(progress.bestObjective,6)}}</span></div><button @click="emit('stop',detail.id)">停止并保存进度</button><details v-if="traceRows.length"><summary>搜索过程</summary><Chart :rows="traceRows" :series="[{key:'bestObjective',label:'最优目标'}]" unit="目标值" :context="detail.id+':'+progress.fit"/></details></div>
    <p v-else class="muted">完成分期率定与时间检验后，另存全部资料正式定参结果。计算期间可继续浏览平台。</p>
   </div>
   <div v-if="step===3" class="calibration-step-body">
    <template v-if="detail"><div class="calibration-section-heading"><h3>{{detail.name||'计算成果'}}</h3><span>{{status(detail)}}</span><button :disabled="working" @click="loadTaskSettings">本次设置</button></div>
     <p v-if="detail.status==='failed'" class="message error">{{detail.message}}</p><div v-if="['interrupted','cancelled','failed'].includes(detail.status)&&['two-stage-calibration','calibration-replay'].includes(detail.kind)" class="actions"><button class="primary" :disabled="locked" @click="resume">{{isReplay?'继续本次复现':'继续已保存的率定'}}</button></div><div v-if="['running','queued','stopping'].includes(detail.status)"><progress :value="detail.progress" max="100"></progress><p>{{detail.message}}</p><button @click="step=2">查看运行进度</button></div>
     <template v-if="successful">
      <div class="calibration-record-heading"><div><b>本次率定记录 · 只读</b><p class="calibration-period-count">预热 {{detail.request?.start?.slice(0,10)}} 起 · {{workflow?.flowParameterSelectionUsesLakeLevel?'杜加流量与湖位分阶段筛选':'杜加流量确定共享参数'}}</p></div><button :disabled="locked" @click="loadTaskSettings">读取本次率定设置</button></div>
      <p v-if="detail.result.adoptionSkipped" class="muted">{{detail.result.adoptionSkipped.message}}</p>
      <div class="calibration-result-actions"><span v-if="adopted" class="calibration-adopted">✓ 当前正式成果</span><button class="primary" :disabled="locked||adopted" @click="adopt">{{adopted?'已采用':'保存并采用'}}</button><button :disabled="locked" @click="adjust">人工微调并试算</button><button @click="downloadSettings">导出设置与记录</button><button @click="emit('export',detail.id,'daily.csv')">导出日成果</button></div>
      <div v-if="replayHistoryOpen" class="calibration-history"><p v-if="replayHistoryLoading" class="muted">正在读取复现记录…</p><button v-for="t in replayHistory" :key="t.id" @click="openRecord(t.id)"><span>{{t.name||'历史率定复现'}}</span><small>{{status(t)}} · {{t.created_at?.slice(0,10)}}</small></button><p v-if="!replayHistoryLoading&&!replayHistory.length" class="muted">暂无复现记录。</p></div>
      <div v-if="replayComparison" class="calibration-replay-comparison" :class="{passed:replayComparison.passes}"><b>{{replayComparison.passes?'复现结果与原记录一致':'复现完成，存在差异'}}</b><span>逐日流量最大差 {{number(replayComparison.flowMaxAbsoluteM3s,10)}} m³/s</span><span>湖位最大差 {{number(replayComparison.lakeMaxAbsoluteM,10)}} m</span><p>候选顺序、最终参数、阶段次数和过程数值分别核对，完整差异随本次设置导出。</p></div>
      <div v-if="fitRows.length" class="table-wrap calibration-fit-table"><table><thead><tr><th>流量率定</th><th>有效日</th><th>率定NSE</th><th>检验NSE</th><th>KGE</th><th>RMSE · m³/s</th><th>水量偏差</th></tr></thead><tbody><tr v-for="f in fitRows" :key="f.fit"><td>{{f.fit}}</td><td>{{f.training.count}}</td><td>{{number(f.training.nse,3)}}</td><td>{{number(f.validation?.nse,3)}}</td><td>{{number(f.training.kge,3)}}</td><td>{{number(f.training.rmse,2)}}</td><td>{{number(f.training.pbias,2)}}%</td></tr></tbody></table></div>
      <div v-if="workflow?.lake" class="calibration-run-summary lake-result-summary"><div><small>湖面蒸发系数</small><b>{{number(workflow.lake.final.value,4)}}</b></div><div><small>湖位率定RMSE</small><b>{{number(workflow.lake.split.training.rmse,3)}} m</b></div><div><small>湖位时间检验RMSE</small><b>{{number(workflow.lake.split.validation.rmse,3)}} m</b></div><div><small>{{workflow.finalRefit?'全部资料RMSE':'所选参数率定RMSE'}}</small><b>{{number(workflow.lake.final.training.rmse,3)}} m</b></div></div>
      <div v-if="selection" class="calibration-run-summary lake-result-summary"><div><small>{{selection.selectionMode==='continuous-dual'?'实际水文搜索':'历史参数复评'}}</small><b>{{number(recordedCounts.generated??recordedCounts.rescored,0)}}次</b></div><div><small>流量—湖位联合评价</small><b>{{number(recordedCounts.jointProposals,0)}}次</b></div><div><small>实际全流域计算</small><b>{{number(recordedCounts.basinCandidates,0)}}组</b></div><div><small>代表候选</small><b>{{number(recordedCounts.selectedForBasin,0)}}组</b></div></div>
      <details v-if="warmupDiagnostics" class="calibration-details"><summary>预热效果核验</summary><p>参考条件：{{warmupCase(warmupDiagnostics.baselineCase||'')}}。对照不同起算时间和土壤初始蓄水，比较湖位起算后首年过程。流量差以1%、湖位差以1厘米为核验标准。</p><div class="table-wrap"><table><thead><tr><th>对照条件</th><th>流量过程差异</th><th>最大湖位差</th><th>核验</th></tr></thead><tbody><tr v-for="(row,name) in warmupDiagnostics.comparisons" :key="name"><td>{{warmupCase(name)}}</td><td>{{number(row.flowNrmsePercent,3)}}%</td><td>{{number(row.lakeLevelMaxAbsoluteDifferenceM*100,3)}} cm</td><td>{{row.passes?'达到标准':'仍有初态影响'}}</td></tr></tbody></table></div></details>
      <div class="calibration-result-charts"><section><h4>杜加流量过程</h4><Chart :rows="historyRows" :series="[{key:'duja',label:'模拟流量'},{key:'observedFlow',label:'杜加实测',observed:true}]" unit="m³/s" :context="detail.id+':cal-flow'"/></section><section><h4>湖泊水位过程</h4><Chart :rows="historyRows" :series="[{key:'lakeLevel',label:'模拟湖位'},{key:'observedLevel',label:'主站实测',observed:true}]" unit="m" :context="detail.id+':cal-lake'"/></section></div>
      <details open class="calibration-details"><summary>本次参数 · 真实初值、范围与结果</summary><div class="table-wrap calibration-record-table"><table><thead><tr><th>阶段</th><th>参数</th><th>真实初值</th><th>下界</th><th>上界</th><th>结果值</th><th>结果来源</th></tr></thead><tbody><tr v-for="d in recordedParameters" :key="d.key" :data-parameter="d.key"><td>{{d.stage}}</td><td><b>{{d.label}}</b><small>{{d.key}}{{d.unit?' · '+d.unit:''}}</small></td><td :title="String(d.initial??'未记录')">{{number(d.initial,8)}}</td><td>{{number(d.lower,6)}}</td><td>{{number(d.upper,6)}}</td><td :title="String(d.final??'未记录')">{{number(d.final,8)}}</td><td>{{d.source}}</td></tr></tbody></table></div><p class="muted">显示保留适量小数；导出与复现使用本次记录的原始精度。</p></details>
      <details class="calibration-details"><summary>目标函数、资料分期与实际搜索记录</summary><p>流量目标函数：{{recordSearch.objectiveProfile||'未记录'}}。</p><p v-if="recordObjective?.flowWeights">本次冻结权重：流量NSE {{recordObjective.flowWeights.nse}}，低流量对数NSE {{recordObjective.flowWeights.logNse}}，径流总量偏差 {{recordObjective.flowWeights.volume}}。</p><p v-else>该记录未保存完整目标权重。</p><p v-if="recordObjective?.lakeWeights">湖位评分权重：日误差 {{recordObjective.lakeWeights.rmse}}，趋势 {{recordObjective.lakeWeights.trend}}，季节涨幅 {{recordObjective.lakeWeights.amplitude}}，高点日期 {{recordObjective.lakeWeights.peakTiming}}。</p><p>预热从 {{detail.request?.start?.slice(0,10)||'未记录'}} 连续计算，土壤初始蓄水比例 {{detail.request?.initialHydrology?.soilFraction??'未记录'}}。</p><p v-if="recordInitialization?.time">湖位起算 {{recordInitialization.time}} · {{number(recordInitialization.level,3)}} m；{{recordInitialization.reference}} · {{recordInitialization.method}}。</p><p v-if="selection&&recordOptions.selectionMode==='flow-constrained-lake'">候选筛选条件：NSE ≥ {{recordOptions.flowConstraints?.minNse}}，径流总量偏差绝对值 ≤ {{recordOptions.flowConstraints?.maxAbsPbias}}%。湖位四项等权评分与连续流量目标分别保存。</p><p v-if="selection">首轮候选预算 {{number(recordOptions.candidateLimit,0)}}组，实际选入 {{number(recordedCounts.selectedForBasin,0)}}组；联合细化预算 {{number(recordOptions.jointBudget,0)}}次，实际追加候选评价 {{number(recordedCounts.jointProposals,0)}}次。</p><p>多起点搜索 {{recordSeeds.length}}个；可复现随机编号 {{recordSeeds.join('、')||'未记录'}}。</p><p v-for="f in fitRows" :key="f.fit">{{f.fit}}：数值评价 {{number(f.evaluations,0)}}次，{{searchState(f)}}；实际使用 {{f.trainingPeriod?.start?.slice(0,10)||'—'}} 至 {{f.trainingPeriod?.end?shiftCalendarDay(f.trainingPeriod.end.slice(0,10),-1):'—'}}，{{f.training?.count||0}}个有效日。</p><p v-if="recordOptions.lakePeriods">湖位率定 {{day(recordOptions.lakePeriods,'trainingStart')}}—{{day(recordOptions.lakePeriods,'trainingEnd')}}；时间检验 {{day(recordOptions.lakePeriods,'evaluationStart')}}—{{day(recordOptions.lakePeriods,'evaluationEnd')}}。</p><p>{{selection?'先按所记湖位率定期筛选，再报告时间检验，最后单独拟合全部有效湖位。分期率定与检验结果分别保存。':'分年检验与全部资料重率定分别保存；全部资料拟合后的分数作为拟合评价。'}}</p><ParameterTransferAudit v-if="detail.result.parameterTransferAudit" :audit="detail.result.parameterTransferAudit" :definitions="definitions"/></details>
     </template>
    </template><p v-else class="empty">开始一次率定，或从“我的计算记录”选择成果。</p>
   </div>
  </template>
 </section>
 <Teleport to="body"><div v-if="replayPlan" class="calibration-replay-shade" @click.self="!working&&(replayPlan=null)"><section ref="replayDialog" class="calibration-replay-dialog" role="dialog" aria-modal="true" aria-labelledby="replay-title" tabindex="-1" @keydown.esc="!working&&(replayPlan=null)">
  <div class="calibration-section-heading"><div><span class="calibration-eyebrow">本次实际运行条件</span><h2 id="replay-title">复现本次率定</h2></div><button aria-label="关闭复现设置" :disabled="working" @click="replayPlan=null">×</button></div>
  <p>重放历史候选复评、湖位筛选和湖泊拟合，输出一份独立成果。当前采用成果保持不变。</p>
  <div class="calibration-run-summary"><div><small>预热起点</small><b>{{replayPlan.configuration?.start?.slice(0,10)}}</b></div><div><small>水文参与参数</small><b>{{replayPlan.configuration?.selectedKeys?.length||0}}项</b></div><div><small>搜索起点</small><b>{{replayPlan.configuration?.twoStage?.seeds?.join('、')}}</b></div><div><small>原全流域计算</small><b>{{number(replayPlan.counts?.basinCandidates,0)}}组</b></div></div>
  <p class="muted">原记录：复评 {{number(replayPlan.counts?.rescored,0)}}组，追加候选评价 {{number(replayPlan.counts?.jointProposals,0)}}次。更早生成候选的搜索不重新执行。</p>
  <div v-if="!replayPlan.available" class="message error" role="alert"><b>复现资料尚不完整</b><p v-for="(item,i) in replayPlan.missing" :key="i">{{typeof item==='string'?item:(item.name||item.asset||'资料')+'：'+(item.reason||item.message||'缺失或摘要不符')}}</p></div>
  <details open class="calibration-details"><summary>冻结的初值、范围和结果</summary><div class="table-wrap calibration-record-table"><table><thead><tr><th>参数</th><th>参与阶段</th><th>初值</th><th>下界</th><th>上界</th><th>原结果</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td>{{d.label}}<small>{{d.key}}</small></td><td>{{d.key==='lakeCoefficient'?'湖泊阶段':replayPlan.configuration?.selectedKeys?.includes(d.key)?'水文阶段':'人工固定'}}</td><td>{{number(replayPlan.configuration?.parameters?.[d.key],8)}}</td><td>{{number(replayPlan.configuration?.bounds?.[d.key]?.[0],6)}}</td><td>{{number(replayPlan.configuration?.bounds?.[d.key]?.[1],6)}}</td><td>{{number(replayPlan.configuration?.results?.[d.key],8)}}</td></tr></tbody></table></div></details>
  <details class="calibration-details"><summary>资料、计算版本与完整设置</summary><p>输入 {{replayPlan.configuration?.inputId}} · 空间 {{replayPlan.configuration?.spatialVersion}}</p><p>来源任务 {{replayPlan.sourceTaskId}} · 复现设置摘要 {{replayPlan.planSHA256}}</p><p>计算环境 Python {{replayPlan.environment?.python}}；依赖差异 {{Object.keys(replayPlan.environment?.lockMismatches||{}).length}}项，按实际重放数值核验。</p><pre>{{JSON.stringify({periods:replayPlan.configuration?.periods,twoStage:replayPlan.configuration?.twoStage,initialHydrology:replayPlan.configuration?.initialHydrology,initialization:replayPlan.configuration?.initialization,objectiveProfile:replayPlan.configuration?.objectiveProfile,objectiveSpec:replayPlan.configuration?.objectiveSpec},null,2)}}</pre><p v-for="asset in replayPlan.assets" :key="asset.name">{{asset.name}} · {{number(asset.bytes,0)}}字节 · {{asset.sha256}}</p></details>
  <div class="actions"><button :disabled="working" @click="replayPlan=null">返回</button><button class="primary" :disabled="locked||!replayPlan.available" @click="startReplay">{{working?'正在提交…':'开始独立复现'}}</button></div>
 </section></div></Teleport>
</template>

<style scoped>
.calibration-heading{display:flex;justify-content:space-between;gap:32px;align-items:center;padding:5px 0 25px;border-bottom:1px solid var(--line)}.calibration-heading-main{min-width:0}.calibration-heading-meta{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--muted)}.calibration-heading h2{font-size:25px;line-height:1.4;font-weight:600;letter-spacing:.2px;margin:12px 0 10px;overflow-wrap:anywhere}.scheme-status{border:1px solid var(--line);border-radius:4px;padding:3px 8px;font-size:11px}.scheme-status.adopted{border-color:#bbd9d5;color:#26716e;background:#f0f8f6}.calibration-heading p{font-size:13px;color:var(--muted);margin:0;line-height:1.8}.calibration-heading p>span{padding:0 8px;color:#9bb5ba}.calibration-save{display:flex;flex-direction:column;align-items:flex-end;gap:10px;flex-shrink:0}.calibration-save>span{font-size:12px;color:var(--muted)}.calibration-save>.unsaved{color:#91642d}.calibration-save>button{min-width:112px;padding:11px 22px}.calibration-toolbar{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:17px 0 0}.scheme-tools{display:flex;gap:8px;flex-wrap:wrap}.scheme-tools>button{font-size:13px;padding:8px 14px;border-color:transparent;background:#f2f6f6;color:#315c67}.scheme-tools>button:hover:not(:disabled){background:#e5f0ef;border-color:#bed6d2}.calibration-toolbar>.link{font-size:12px;white-space:nowrap}.calibration-toolbar>.link>span{margin-left:5px}@media(max-width:760px){.calibration-heading{align-items:flex-start;gap:14px}.calibration-heading h2{font-size:21px}.calibration-heading p{font-size:12px}.calibration-save>button{min-width:90px;padding:10px 12px}.calibration-toolbar{align-items:flex-start;flex-wrap:wrap}}
.workflow-phases{display:flex;gap:20px;align-items:center;margin-top:24px;padding:18px 22px;background:#edf5f4;color:#276e76;font-size:14px}.objective-settings{margin-top:24px}.objective-settings h3{font-size:16px}.objective-settings .query-grid{max-width:600px}.calibration-workbench input[type=number],.calibration-workbench input[type=date]{min-height:38px}.calibration-scheme-bar{align-items:center}.inline-option{display:flex;align-items:center;gap:8px}.calibration-details input:not([type=checkbox]){max-width:280px}
.calibration-record-heading{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:15px 18px;background:#edf5f4;border-left:3px solid var(--accent);margin:16px 0}.calibration-record-heading p{margin:7px 0 0}.calibration-record-table{max-height:430px;overflow:auto}.calibration-record-table th{position:sticky;top:0;background:#edf4f5;z-index:1}.calibration-record-table small{display:block;color:var(--muted);font-size:11px}.calibration-record-table td{font-variant-numeric:tabular-nums;white-space:nowrap}.calibration-replay-comparison{display:flex;gap:14px;flex-wrap:wrap;padding:16px;border:1px solid #d9bd8d;background:#fff9ef;margin:16px 0}.calibration-replay-comparison.passed{border-color:#a7c9bb;background:#f0f8f4}.calibration-replay-comparison p{width:100%;margin:0;color:var(--muted);font-size:12px}.calibration-replay-shade{position:fixed;inset:0;background:#071e2ca6;z-index:150;display:flex;align-items:center;justify-content:center;padding:24px}.calibration-replay-dialog{width:min(1120px,100%);max-height:calc(100dvh - 48px);overflow:auto;background:var(--panel,#fff);border-radius:10px;padding:26px 30px;box-shadow:0 20px 70px #001a354d;outline:none}.calibration-replay-dialog h2{margin:6px 0}.calibration-replay-dialog pre{max-height:280px;overflow:auto;background:#f0f5f6;padding:12px;font-size:12px}.calibration-replay-dialog .calibration-details p{overflow-wrap:anywhere}.calibration-replay-dialog .actions{position:sticky;bottom:-26px;background:var(--panel,#fff);padding:14px 0 4px;margin-top:16px}.calibration-replay-dialog>.calibration-run-summary{grid-template-columns:repeat(4,minmax(0,1fr))}@media(max-width:760px){.calibration-replay-shade{padding:10px}.calibration-replay-dialog{padding:18px;max-height:calc(100dvh - 20px)}.calibration-replay-dialog>.calibration-run-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.calibration-record-heading{align-items:flex-start;flex-direction:column}}
.calibration-selection-mode{max-width:620px;display:flex;flex-direction:column;gap:8px;margin:20px 0;font-size:13px}.calibration-selection-mode select{width:100%}
.calibration-workbench{padding:28px 32px;max-width:1480px;margin:0 auto}.calibration-top{display:flex;justify-content:space-between;gap:32px;align-items:center;margin-bottom:24px}.calibration-eyebrow{color:var(--muted);font-size:12px;letter-spacing:1.5px}.calibration-top h2{font-size:25px;margin:7px 0 10px;letter-spacing:1px}.calibration-top p{margin:0;color:var(--muted);font-size:14px}.calibration-current{display:flex;flex-direction:column;gap:5px;padding:14px 20px;border-left:3px solid var(--accent,#277f89);background:#edf5f4;min-width:270px}.calibration-current small,.calibration-current span{color:var(--muted);font-size:12px}.calibration-current b{font-size:15px}.calibration-scheme-bar{display:flex;gap:10px;align-items:flex-end}.calibration-scheme-bar>label{flex:1;max-width:620px}.calibration-scheme-bar>label select{width:100%}.calibration-scheme-bar>.link{margin-left:auto}.calibration-steps{display:grid;grid-template-columns:repeat(4,1fr);margin:26px 0 0;border-bottom:1px solid var(--line);gap:10px}.calibration-steps button{display:flex;align-items:center;gap:10px;padding:16px 8px;border:0;border-radius:0;border-bottom:3px solid transparent;background:none;color:var(--muted);justify-content:flex-start}.calibration-steps button>span{width:25px;height:25px;border-radius:50%;background:#edf2f3;display:grid;place-items:center;font-size:12px}.calibration-steps button.active{border-bottom-color:#277f89;color:#226f78}.calibration-steps button.active>span{background:#277f89;color:white}.calibration-steps button.passed>span{background:#e0efec;color:#277f89}.calibration-step-body{padding:26px 0 0;min-height:400px}.calibration-mode-options{display:flex;gap:9px;margin:20px 0}.calibration-mode-options button{padding:10px 18px}.calibration-mode-options .selected{border-color:#277f89;color:#226f78;background:#edf7f5}.calibration-section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:15px}.calibration-section-heading h3{font-size:16px;margin:0}.calibration-section-heading h3>span{font-size:12px;color:#277f89;letter-spacing:1px;margin-right:9px}.calibration-section-heading>span{font-size:12px;color:var(--muted)}.calibration-period-block{padding:22px 0;border-bottom:1px solid var(--line)}.calibration-period-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.calibration-period-count{color:#277f89;font-size:12px;margin:13px 0}.calibration-inline-options{display:flex;gap:26px;flex-wrap:wrap}.calibration-inline-options label,.calibration-start-row>label{display:flex;gap:8px;align-items:center;font-size:13px}.calibration-data-line{font-size:12px;margin:20px 0}.calibration-table-toolbar{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--muted);margin:15px 0}.calibration-parameter-table table{min-width:850px}.calibration-parameter-table td small{display:block;color:var(--muted);font-size:11px;margin-top:4px}.calibration-parameter-table td>b{font-size:13px;font-weight:500}.calibration-parameter-table input[type=number]{width:120px}.calibration-parameter-table input[type=checkbox]{width:17px;height:17px;accent-color:#277f89}.parameter-result{color:#277f89;font-variant-numeric:tabular-nums}.calibration-details{margin:22px 0;font-size:13px}.calibration-details summary{color:var(--muted);cursor:pointer}.calibration-details>p{color:var(--muted);line-height:1.8}.calibration-details .query-grid{margin:18px 0}.calibration-run-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));background:#f0f6f5;border:1px solid #dceae6}.calibration-run-summary>div{display:flex;flex-direction:column;gap:11px;padding:23px 20px;border-right:1px solid #dceae6}.calibration-run-summary>div:last-child{border:0}.calibration-run-summary small{color:var(--muted);font-size:12px}.calibration-run-summary b{font-size:17px;font-weight:500}.calibration-start-row{display:flex;align-items:center;gap:14px;margin:30px 0}.calibration-start-row>label{margin-right:auto}.calibration-start-row .primary{padding:12px 24px}.calibration-progress{border-top:1px solid var(--line);padding-top:24px;margin-top:24px}.calibration-progress>div:first-child{display:flex;justify-content:space-between;margin-bottom:10px}.calibration-progress progress{width:100%;height:8px;accent-color:#277f89}.calibration-progress p{color:var(--muted);font-size:13px}.calibration-progress-numbers{display:flex;gap:24px;margin:14px 0;font-size:13px;color:#277f89}.calibration-history{border:1px solid var(--line);margin-top:12px;max-height:260px;overflow:auto}.calibration-history>button{display:flex;justify-content:space-between;text-align:left;width:100%;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:12px 16px}.calibration-history small{color:var(--muted)}.calibration-history .selected{background:#edf7f5}.calibration-result-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:22px 0}.calibration-adopted{color:#277f89;margin-right:auto}.calibration-fit-table{margin:20px 0}.lake-result-summary{margin:22px 0}.calibration-result-charts{display:grid;grid-template-columns:1fr 1fr;gap:22px}.calibration-result-charts>section{min-width:0}.calibration-result-charts h4{font-size:14px;margin:18px 0 0}.calibration-result-charts :deep(.chart){height:290px}.calibration-secondary{border-top:1px solid var(--line);margin-top:25px;padding-top:15px}.actions{margin-top:24px}.calibration-workbench input[type=checkbox]{accent-color:#277f89}
@media(max-width:1100px){.calibration-workbench{padding:22px}.calibration-top{align-items:flex-start}.calibration-current{min-width:220px}.calibration-run-summary b{font-size:14px}.calibration-run-summary>div{padding:18px 13px}.calibration-result-charts{grid-template-columns:1fr}}
@media(max-width:760px){.calibration-top{flex-direction:column;gap:16px}.calibration-current{width:100%}.calibration-scheme-bar{flex-wrap:wrap}.calibration-scheme-bar>label{flex-basis:100%}.calibration-steps{gap:0}.calibration-steps button{flex-direction:column;font-size:12px;padding:10px 2px}.calibration-period-grid,.calibration-run-summary{grid-template-columns:1fr 1fr}.calibration-section-heading{align-items:flex-start;flex-direction:column;gap:8px}.calibration-start-row{flex-wrap:wrap}.calibration-start-row>label{width:100%;margin-bottom:10px}.calibration-mode-options{flex-wrap:wrap}.calibration-top h2{font-size:22px}}
</style>
