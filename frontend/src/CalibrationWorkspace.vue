<script setup>
import {ref,computed,watch} from 'vue'
import HistoricalResult from './HistoricalResult.vue'
import ResearchSchemes from './ResearchSchemes.vue'
import CalibrationTrace from './CalibrationTrace.vue'
import {api,number,parameterCompatible} from './api.mjs'
const props=defineProps({boot:Object,tasks:Array,detail:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','stop','select','state','refresh','simulate','export'])
const tab=ref('schemes'),schemeId=ref(''),parameterId=ref(''),resultRegion=ref('background'),working=ref(false),error=ref(''),message=ref('')
const locked=computed(()=>props.busy||working.value||props.active.length>0)
const records=computed(()=>(props.tasks||[]).filter(t=>!t.trashed&&['calibration-workflow','calibration','trial'].includes(t.kind)))
const selectedScheme=computed(()=>(props.boot.schemes||[]).find(s=>s.id===schemeId.value))
const parameters=computed(()=>(props.boot.parameters||[]).filter(p=>p.payload?.sourceTaskId&&parameterCompatible(p,selectedScheme.value?.payload)))
const resultRegions=computed(()=>Object.keys(props.detail?.result?.partitionParameters||{}))
const resultValues=computed(()=>({...props.detail?.result?.parameters,...(resultRegion.value==='background'?{}:props.detail?.result?.partitionParameters?.[resultRegion.value])}))
const regionNames={duja:'杜加控制区',zajia_rest:'扎加下游区',zagen:'扎根藏布',boqu:'波曲藏布',ali:'阿里藏布',fringe:'湖周补充区',unresolved:'排水归属待核区'}
const status=t=>({succeeded:'已完成',running:'计算中',queued:'排队中',failed:'失败',cancelled:'已停止',interrupted:'已中断',stopping:'正在停止'})[t.status]||t.status
async function saveParameters(){
 if(props.detail?.status!=='succeeded')return
 const name=prompt('参数组名称',props.detail.name||'计算参数');if(!name?.trim())return
 working.value=true;error.value='';message.value=''
 try{const saved=await api('parameters/from-task',{taskId:props.detail.id,name:name.trim()});parameterId.value=saved.id;message.value='已保存本次计算实际使用的完整参数。';emit('refresh')}
 catch(e){error.value=e.message}finally{working.value=false}
}
async function adopt(){
 if(!schemeId.value||!parameterId.value)return
 working.value=true;error.value='';message.value=''
 try{const row=await api('schemes/'+encodeURIComponent(schemeId.value)+'/adopt-parameters',{parameterId:parameterId.value});schemeId.value=row.id;message.value='方案已采用所选参数；率定初值保持原设置。';emit('refresh')}
 catch(e){error.value=e.message}finally{working.value=false}
}
function run(request){tab.value='records';message.value='';emit('submit',request)}
watch(()=>props.detail?.id,()=>{parameterId.value='';resultRegion.value='background'})
watch(parameters,rows=>{if(parameterId.value&&!rows.some(p=>p.id===parameterId.value))parameterId.value=''})
</script>
<template>
 <section class="panel">
  <div class="panel-title"><h3>模型方案与率定</h3></div>
  <nav class="workspace-tabs"><button :class="{active:tab==='schemes'}" @click="tab='schemes'">方案与参数</button><button :class="{active:tab==='records'}" @click="tab='records'">试算与率定结果</button></nav>
  <ResearchSchemes v-show="tab==='schemes'" :boot="boot" :locked="locked" :selected-id="schemeId" @selected="schemeId=$event" @refresh="emit('refresh')" @submit="run" @simulate="emit('simulate',$event)"/>
  <template v-if="tab==='records'">
   <p class="muted">选择一次计算结果，查看过程和指标，再保存实际使用的参数。</p>
   <div class="table-wrap"><table><thead><tr><th>名称</th><th>类型</th><th>时间</th><th>状态</th></tr></thead><tbody><tr v-for="t in records" :key="t.id" :class="{selected:t.id===detail?.id}"><td><button class="link" @click="emit('select',t.id)">{{t.name||'计算结果'}}</button></td><td>{{t.kind==='trial'?'试算':'率定'}}</td><td>{{(t.created_at||'').slice(0,16).replace('T',' ')}}</td><td>{{status(t)}}</td></tr></tbody></table></div>
   <p v-if="!records.length" class="empty">还没有试算或率定记录。</p>
  </template>
 </section>
 <template v-if="tab==='records'&&detail">
  <p v-if="error" class="message error" role="alert">{{error}}</p><p v-if="message" class="message success" role="status">{{message}}</p>
  <CalibrationTrace v-if="detail.result?.calibration" :task="detail"/>
  <section v-if="detail.status==='succeeded'&&detail.result?.parameters" class="panel">
   <div class="panel-title"><h3>结果参数</h3><button :disabled="locked" @click="saveParameters">保存参数</button></div>
   <p class="muted">参数从这次已完成的计算结果读取，不使用页面中尚未运行的编辑值。</p>
   <div class="result-toolbar"><label class="grow">方案<select v-model="schemeId"><option value="">选择方案</option><option v-for="s in boot.schemes" :key="s.id" :value="s.id">{{s.name}}{{s.builtin||s.payload?.builtin?' · 内置':''}}</option></select></label><label class="grow">已保存参数<select v-model="parameterId"><option value="">选择参数组</option><option v-for="p in parameters" :key="p.id" :value="p.id">{{p.name}}</option></select></label><button :disabled="locked||!schemeId||!parameterId" @click="adopt">方案采用此参数</button><button class="primary" :disabled="!schemeId||!parameterId" @click="emit('simulate',{schemeId,parameterId})">用此参数模拟 →</button></div>
   <p v-if="selectedScheme" class="note">当前方案：{{selectedScheme.name}}。采用参数不会改写已有成果，也不会改变率定初值。</p>
   <details><summary>查看本次完整参数</summary><div class="result-toolbar"><label>参数适用范围<select v-model="resultRegion"><option value="background">其他分区共用</option><option v-for="r in resultRegions" :key="r" :value="r">{{regionNames[r]||r}}</option></select></label></div><div class="table-wrap"><table><thead><tr><th>参数</th><th>本次计算值</th><th>单位</th></tr></thead><tbody><tr v-for="d in boot.parameterDefinitions" :key="d.key"><td>{{d.label}}</td><td>{{number(resultValues[d.key],6)}}</td><td>{{d.unit||'无量纲'}}</td></tr></tbody></table></div></details>
  </section>
  <HistoricalResult :detail="detail" :busy="busy" @stop="emit('stop',$event)" @state="emit('state')" @export="(id,format)=>emit('export',id,format)" @retry="tab='schemes'"/>
 </template>
</template>
