<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue'
import {api,parameterCompatible} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
const props=defineProps({boot:Object,selection:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','navigate'])
const schemeId=ref(''),parameterId=ref(''),start=ref(''),end=ref(''),name=ref(''),coverage=ref(null),coverageError=ref('')
const plan=ref(null),planError=ref(''),planNotice=ref(''),planning=ref(false)
let planTicket=0,coverageTicket=0,planTimer,planController
const schemes=computed(()=>(props.boot.schemes||[]).filter(s=>s.payload?.modelFamily==='grid-continuous-v1'))
const selectedScheme=computed(()=>schemes.value.find(s=>s.id===schemeId.value))
const firstDay=computed(()=>coverage.value?.complete&&coverage.value.firstCompleteDay?shiftCalendarDay(coverage.value.firstCompleteDay,365):'')
const lastDay=computed(()=>coverage.value?.complete?coverage.value.lastCompleteDay:'')
const parameters=computed(()=>(props.boot.parameters||[]).filter(p=>p.payload?.sourceTaskId&&parameterCompatible(p,selectedScheme.value?.payload)))
const issue=computed(()=>{
 if(!selectedScheme.value)return '请选择可用方案。'
 if(coverageError.value)return '气象资料读取失败：'+coverageError.value
 if(!coverage.value)return '正在检查模型气象资料。'
 if(!coverage.value.complete)return coverage.value.message||'没有完整的气象资料，请先更新资料。'
 if(!start.value||!end.value||end.value<start.value)return '请选择有效的开始和结束日期。'
 if(firstDay.value&&start.value<firstDay.value)return '该日期之前没有完整的一年预热气象资料，请更新资料或调整开始日期。'
 if(lastDay.value&&end.value>lastDay.value)return '结束日期超出气象资料的完整日范围。'
 return ''
})
function pickScheme(id,force=false){
 const s=schemes.value.find(x=>x.id===id);if(!s)return
 const selected=force?(props.selection?.parameterId||''):''
 schemeId.value=id;parameterId.value=(props.boot.parameters||[]).some(p=>p.id===selected&&p.payload?.sourceTaskId&&parameterCompatible(p,s.payload))?selected:''
 const limit=lastDay.value
 start.value=s.payload.periods?.trainingStart?.slice(0,10)||s.payload.periods?.warmupStart?.slice(0,10)||''
 const preferred=s.payload.periods?.evaluationEnd?shiftCalendarDay(s.payload.periods.evaluationEnd,-1):limit
 end.value=limit&&preferred>limit?limit:preferred
}
watch(schemes,rows=>{if(!rows.length)return;if(!rows.some(s=>s.id===schemeId.value)){const s=rows.find(x=>x.id===props.selection?.schemeId)||rows.find(x=>x.builtin||x.payload?.builtin)||rows[0];pickScheme(s.id,!!props.selection)}},{immediate:true})
watch(()=>props.selection,value=>{if(value?.schemeId)pickScheme(value.schemeId,true)},{deep:true})
watch(parameters,rows=>{if(parameterId.value&&!rows.some(p=>p.id===parameterId.value))parameterId.value=''},{immediate:true})
watch(()=>props.boot,async()=>{const ticket=++coverageTicket;coverageError.value='';try{const data=await api('forcing-coverage');if(ticket===coverageTicket){coverage.value=data.current;if(lastDay.value&&end.value>lastDay.value)end.value=lastDay.value}}catch(e){if(ticket===coverageTicket)coverageError.value=e.message}},{immediate:true})
function planRequest(){return {researchSchemeId:schemeId.value,start:start.value+'T00:00:00',end:shiftCalendarDay(end.value,1)+'T00:00:00',...(parameterId.value?{candidateParameterId:parameterId.value}:{})}}
watch(()=>[schemeId.value,parameterId.value,start.value,end.value,issue.value,coverage.value],()=>{
 const ticket=++planTicket;clearTimeout(planTimer);planController?.abort();plan.value=null;planError.value='';planNotice.value=''
 if(issue.value){planning.value=false;return}
 planning.value=true;planController=new AbortController()
 planTimer=setTimeout(async()=>{try{const data=await api('simulation-plan?'+new URLSearchParams(planRequest()),null,{signal:planController.signal});if(ticket===planTicket)plan.value=data}catch(e){if(e.name!=='AbortError'&&ticket===planTicket)planError.value=e.message}finally{if(ticket===planTicket)planning.value=false}},250)
},{immediate:true})
onBeforeUnmount(()=>{planTicket++;coverageTicket++;clearTimeout(planTimer);planController?.abort()})
async function run(force=false){
 if(issue.value||planError.value||planning.value||!plan.value||props.busy||props.active.length)return
 if(!force&&plan.value.action==='reuse'){
  const request=planRequest(),saved=plan.value;planning.value=true
  try{const latest=await api('simulation-plan?'+new URLSearchParams(request));if(JSON.stringify(request)!==JSON.stringify(planRequest()))return;if(latest.planToken!==saved.planToken||latest.action!=='reuse'||latest.resultId!==saved.resultId){plan.value=latest;planNotice.value='资料或计算条件已变化，请重新确认。';return}emit('navigate','results',latest.resultId,{id:latest.resultId,start:start.value,end:end.value})}
  catch(e){planError.value=e.message}finally{planning.value=false}
  return
 }
 if(plan.value.action==='blocked')return
 emit('submit',{kind:'simulation',...planRequest(),expectedPlanToken:plan.value.planToken,name:name.value.trim()||selectedScheme.value.name+' · '+start.value+'—'+end.value})
}
</script>
<template>
 <section class="panel simulation-form">
  <div class="panel-title"><h3>历史模拟</h3><button class="link" @click="emit('navigate','results')">查看已有成果 →</button></div>
  <p v-if="boot.builtinResultAvailable===false" class="note warning">{{boot.builtinResultMessage}}</p>
  <p class="muted">选择方案、参数和时段；计算所需的预热与资料检查由平台处理。</p>
  <form @submit.prevent="run(false)">
   <div class="query-grid">
    <label class="grow">模型方案<select :value="schemeId" @change="pickScheme($event.target.value)"><option value="" disabled>选择方案</option><option v-for="s in schemes" :key="s.id" :value="s.id">{{s.name}}{{s.builtin||s.payload?.builtin?' · 内置':''}}</option></select></label>
    <label class="grow">参数<select v-model="parameterId"><option value="">方案采用的参数</option><option v-for="p in parameters" :key="p.id" :value="p.id">{{p.name}}</option></select></label>
    <label>开始日期<input v-model="start" type="date" :min="firstDay" :max="lastDay" required></label>
    <label>结束日期<input v-model="end" type="date" :min="firstDay" :max="lastDay" required></label>
   </div>
   <div class="query-grid simulation-details">
    <label class="grow">成果名称<input v-model="name" placeholder="可留空，按方案和时段命名"></label>
    <button class="primary" :disabled="busy||active.length>0||!!issue||planning||!!planError||!plan||plan.action==='blocked'">{{planning?'检查资料…':plan?.action==='reuse'?'查看已有成果':'开始模拟'}}</button><button v-if="plan?.action==='reuse'" type="button" :disabled="busy||active.length>0" @click="run(true)">重新计算</button>
   </div>
  </form>
  <div class="simulation-input-status" v-if="coverage?.complete"><span>气象完整日：{{coverage.firstCompleteDay}} 至 {{lastDay}}</span><span>最早可选择 {{firstDay}}（含一年预热，湖位起算另行核查）</span><button class="link" @click="emit('navigate','model')">更新资料 →</button></div>
  <p v-if="plan?.initialization" class="note">陆面从 {{plan.calculationStart.slice(0,10)}} 开始预热；湖位从 {{plan.initialization.time.replace('T',' ').slice(0,16)}}、{{plan.initialization.level}} m 起算<template v-if="plan.initialization.method==='first-quality-daily-level-at-noon'||plan.initialization.source?.includes('日值')">（日值按12时代表，并非已核实的瞬时观测）</template>，首个完整湖位日为 {{plan.earliestCompleteLakeDay}}。</p>
  <p v-if="issue" class="note warning" role="status">{{issue}}</p>
  <p v-if="planNotice" class="note warning" role="status">{{planNotice}}</p>
  <p v-if="planError" class="note warning" role="alert">计算前检查失败：{{planError}}</p><p v-if="plan?.action==='blocked'" class="note warning">{{plan.reasons?.join('；')||'当前资料或起算条件不满足计算要求。'}}</p><p v-else-if="plan?.action==='reuse'" class="note">已有条件一致的完整成果，可直接查看，也可选择重新计算。</p>
 </section>
 <slot/>
</template>
<style scoped>.simulation-details{margin-top:18px}.simulation-input-status{display:flex;flex-wrap:wrap;align-items:center;gap:20px;margin-top:18px;color:var(--muted);font-size:13px}</style>
