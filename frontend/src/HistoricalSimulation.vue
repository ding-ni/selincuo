<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue'
import {api,parameterCompatible} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
const props=defineProps({boot:Object,selection:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','navigate'])
const schemeId=ref(''),parameterId=ref(''),inputId=ref(''),start=ref(''),end=ref(''),name=ref('')
const plan=ref(null),planError=ref(''),planning=ref(false)
let planTicket=0,planTimer,planController
const schemes=computed(()=>(props.boot.schemes||[]).filter(s=>s.payload?.modelFamily==='grid-continuous-v1'))
const selectedScheme=computed(()=>schemes.value.find(s=>s.id===schemeId.value))
const inputs=computed(()=>(props.boot.inputs||[]).filter(i=>{
 if(!i.manifest)return false
 const scheme=selectedScheme.value?.payload;if(!scheme)return true
 const required=scheme.inputRequirement?.transformationSHA256
 return i.manifest.start<=scheme.periods.warmupStart&&(!required||i.manifest.transformationSHA256===required)
}))
const selectedInput=computed(()=>inputs.value.find(i=>i.id===inputId.value))
const firstDay=computed(()=>selectedScheme.value?.payload?.periods?.warmupStart?.slice(0,10)||'')
const lakeAnchor=computed(()=>selectedScheme.value?.payload?.initialization?.initialLakeTime||'')
const lakeAnchorSource=computed(()=>selectedScheme.value?.payload?.initialization?.initialLakeSource||'')
const firstLakeDay=computed(()=>!lakeAnchor.value?'':lakeAnchor.value.slice(11,19)==='00:00:00'?lakeAnchor.value.slice(0,10):shiftCalendarDay(lakeAnchor.value,1))
const lastDay=computed(()=>selectedInput.value?.manifest?.end?shiftCalendarDay(selectedInput.value.manifest.end,-1):'')
const parameters=computed(()=>(props.boot.parameters||[]).filter(p=>p.payload?.sourceTaskId&&parameterCompatible(p,selectedScheme.value?.payload)))
const issue=computed(()=>{
 if(!selectedScheme.value)return '请选择可用方案。'
 if(!selectedInput.value)return '没有可用的气象资料，请先更新资料。'
 if(!start.value||!end.value||end.value<start.value)return '请选择有效的开始和结束日期。'
 if(firstDay.value&&start.value<firstDay.value)return '开始日期早于该方案的起算时间。'
 if(selectedInput.value.manifest.start>selectedScheme.value.payload.periods.warmupStart)return '气象资料未覆盖方案预热起点，请更新资料或调整方案。'
 if(lastDay.value&&end.value>lastDay.value)return '结束日期超出气象资料的完整日范围。'
 return ''
})
function pickScheme(id,force=false){
 const s=schemes.value.find(x=>x.id===id);if(!s)return
 schemeId.value=id;parameterId.value=force?(props.selection?.parameterId||''):''
 const compatible=inputs.value.slice().sort((a,b)=>b.manifest.end.localeCompare(a.manifest.end))
 inputId.value=compatible[0]?.id||''
 const source=inputs.value.find(i=>i.id===inputId.value)
 const limit=source?.manifest?.end?shiftCalendarDay(source.manifest.end,-1):''
 start.value=s.payload.periods?.trainingStart?.slice(0,10)||s.payload.periods?.warmupStart?.slice(0,10)||''
 const preferred=s.payload.periods?.evaluationEnd?shiftCalendarDay(s.payload.periods.evaluationEnd,-1):limit
 end.value=limit&&preferred>limit?limit:preferred
}
watch(schemes,rows=>{if(!rows.length)return;if(!rows.some(s=>s.id===schemeId.value)){const s=rows.find(x=>x.id===props.selection?.schemeId)||rows.find(x=>x.builtin||x.payload?.builtin)||rows[0];pickScheme(s.id,!!props.selection)}},{immediate:true})
watch(()=>props.selection,value=>{if(value?.schemeId)pickScheme(value.schemeId,true)},{deep:true})
function planRequest(){return {researchSchemeId:schemeId.value,inputId:inputId.value,start:start.value+'T00:00:00',end:shiftCalendarDay(end.value,1)+'T00:00:00',...(parameterId.value?{candidateParameterId:parameterId.value}:{})}}
watch(()=>[schemeId.value,parameterId.value,inputId.value,start.value,end.value,issue.value],()=>{
 const ticket=++planTicket;clearTimeout(planTimer);planController?.abort();plan.value=null;planError.value=''
 if(issue.value){planning.value=false;return}
 planning.value=true;planController=new AbortController()
 planTimer=setTimeout(async()=>{try{const data=await api('simulation-plan?'+new URLSearchParams(planRequest()),null,{signal:planController.signal});if(ticket===planTicket)plan.value=data}catch(e){if(e.name!=='AbortError'&&ticket===planTicket)planError.value=e.message}finally{if(ticket===planTicket)planning.value=false}},250)
},{immediate:true})
onBeforeUnmount(()=>{planTicket++;clearTimeout(planTimer);planController?.abort()})
function run(force=false){
 if(issue.value||planError.value||planning.value||!plan.value||props.busy||props.active.length)return
 if(!force&&plan.value.action==='reuse'){emit('navigate','results',plan.value.resultId,{id:plan.value.resultId,start:start.value,end:end.value});return}
 if(plan.value.action==='blocked')return
 emit('submit',{kind:'simulation',...planRequest(),name:name.value.trim()||selectedScheme.value.name+' · '+start.value+'—'+end.value})
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
    <label class="grow">气象资料<select v-model="inputId"><option value="" disabled>选择已登记资料</option><option v-for="i in inputs" :key="i.id" :value="i.id">{{i.name}} · {{i.manifest.start.slice(0,10)}} — {{i.manifest.end.slice(0,10)}}</option></select></label>
    <label class="grow">成果名称<input v-model="name" placeholder="可留空，按方案和时段命名"></label>
    <button class="primary" :disabled="busy||active.length>0||!!issue||planning||!!planError||!plan||plan.action==='blocked'">{{planning?'检查资料…':plan?.action==='reuse'?'查看已有成果':'开始模拟'}}</button><button v-if="plan?.action==='reuse'" type="button" :disabled="busy||active.length>0" @click="run(true)">重新计算</button>
   </div>
  </form>
  <div class="simulation-input-status" v-if="selectedInput"><span>气象资料：{{selectedInput.manifest.start.slice(0,10)}} 起，完整日至 {{lastDay}}</span><span>方案预热起点：{{firstDay}}</span><button class="link" @click="emit('navigate','model')">更新资料 →</button></div>
  <p v-if="firstLakeDay" class="note">陆面自预热起点计算；湖位锚点为 {{lakeAnchor.replace('T',' ').slice(0,16)}}<template v-if="lakeAnchorSource.includes('日值')">（日值代表时刻假设）</template>，完整湖位日从 {{firstLakeDay}} 开始。</p>
  <p v-if="issue" class="note warning" role="status">{{issue}}</p>
  <p v-if="planError" class="note warning" role="alert">计算前检查失败：{{planError}}</p><p v-if="plan?.action==='blocked'" class="note warning">{{plan.reasons?.join('；')||'当前资料或起算条件不满足计算要求。'}}</p><p v-else-if="plan?.action==='reuse'" class="note">已有条件一致的完整成果，可直接查看，也可选择重新计算。</p>
 </section>
 <slot/>
</template>
<style scoped>.simulation-details{margin-top:18px}.simulation-input-status{display:flex;flex-wrap:wrap;align-items:center;gap:20px;margin-top:18px;color:var(--muted);font-size:13px}</style>
