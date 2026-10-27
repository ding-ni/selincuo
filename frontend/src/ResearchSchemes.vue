<script setup>
import {ref,computed,watch} from 'vue'
import {api,number} from './api.mjs'
const props=defineProps({boot:Object,locked:Boolean,selectedId:String})
const emit=defineEmits(['refresh','submit','selected','simulate'])
const draft=ref(null),parent=ref(''),snapshot=ref(''),error=ref(''),message=ref(''),working=ref(false),group=ref('land'),scope=ref('background'),trash=ref(null),spatialRegions=ref([])
let spatialTicket=0
const copyValue=value=>JSON.parse(JSON.stringify(value))
const rows=computed(()=>(props.boot.schemes||[]).filter(s=>s.payload?.modelFamily==='grid-continuous-v1'))
const builtin=computed(()=>!!(rows.value.find(s=>s.id===parent.value)?.builtin||draft.value?.builtin))
const dirty=computed(()=>!!draft.value&&JSON.stringify(draft.value)!==snapshot.value)
const groups=computed(()=>[...new Set((props.boot.parameterDefinitions||[]).map(d=>d.group))])
const labels={land:'产流',snow:'积雪',glacier:'冰川',routing:'汇流',lake:'湖泊水量平衡'}
const regionNames={duja:'杜加控制区',zajia:'扎加藏布',zajia_rest:'扎加下游区',zagen:'扎根藏布',boqu:'波曲藏布',ali:'阿里藏布',fringe:'湖周补充区',unresolved:'排水归属待核区'}
const regions=computed(()=>[...new Set([...spatialRegions.value,...Object.keys(draft.value?.partitionParameters||{})])])
const definitions=computed(()=>(props.boot.parameterDefinitions||[]).filter(d=>d.group===group.value))
function prepare(value){
 const data=copyValue(value)
 data.partitionParameters||={}
 data.calibrationInitialParameters||=copyValue(data.parameters)
 data.calibrationInitialPartitionParameters||=copyValue(data.partitionParameters)
 return data
}
function choose(id){
 const row=rows.value.find(s=>s.id===id)
 if(!row)return
 draft.value=prepare(row.payload);parent.value=row.id;snapshot.value=JSON.stringify(draft.value);scope.value='background';message.value='';error.value='';emit('selected',row.id)
}
watch(rows,value=>{if(props.selectedId&&props.selectedId!==parent.value&&value.some(r=>r.id===props.selectedId)){choose(props.selectedId);return}if(!draft.value&&value.length){const row=value.find(r=>r.id===props.selectedId)||value.find(r=>r.builtin||r.payload?.builtin)||value[0];choose(row.id)}},{immediate:true})
watch(()=>props.selectedId,id=>{if(id&&id!==parent.value&&rows.value.some(s=>s.id===id))choose(id)})
watch(()=>draft.value?.spatialVersion,async version=>{
 const ticket=++spatialTicket;spatialRegions.value=[];if(!version)return
 try{const spatial=await api('spatial-version?'+new URLSearchParams({version}));if(ticket===spatialTicket)spatialRegions.value=[...new Set((spatial.units||[]).map(u=>u.partition).filter(Boolean))]}
 catch(e){if(ticket===spatialTicket)error.value='空间分区读取失败：'+e.message}
},{immediate:true})
async function create(){
 working.value=true;error.value=''
 try{draft.value=prepare(await api('research-template'));draft.value.name='新方案';delete draft.value.builtin;parent.value='';snapshot.value='';scope.value='background';message.value='';emit('selected','')}
 catch(e){error.value=e.message}finally{working.value=false}
}
function copy(){
 if(!draft.value)return
 draft.value=prepare(draft.value);draft.value.name=draft.value.name+' · 副本'
 for(const key of ['builtin','id','revision','familyId','parentRevisionId','configurationSHA256'])delete draft.value[key]
 parent.value='';snapshot.value='';message.value='';emit('selected','')
}
function valueFor(key){
 if(scope.value!=='background'&&key!=='lakeCoefficient')return draft.value.partitionParameters[scope.value]?.[key]??draft.value.parameters[key]
 return draft.value.parameters[key]
}
function setValue(key,event){
 const n=event.target.value===''?NaN:Number(event.target.value)
 if(scope.value!=='background'&&key!=='lakeCoefficient'){draft.value.partitionParameters[scope.value]||={};draft.value.partitionParameters[scope.value][key]=n}
 else draft.value.parameters[key]=n
}
function setInitialFromCurrent(){
 draft.value.calibrationInitialParameters=copyValue(draft.value.parameters)
 draft.value.calibrationInitialPartitionParameters=copyValue(draft.value.partitionParameters)
 message.value='已将当前编辑参数设为率定初值；保存方案后生效。'
}
function initialFor(key){
 const s=draft.value.search.scope
 return s==='duja'&&key!=='lakeCoefficient'?draft.value.calibrationInitialPartitionParameters.duja?.[key]??draft.value.calibrationInitialParameters[key]:draft.value.calibrationInitialParameters[key]
}
function setInitial(key,event){
 const n=event.target.value===''?NaN:Number(event.target.value)
 if(draft.value.search.scope==='duja'&&key!=='lakeCoefficient'){draft.value.calibrationInitialPartitionParameters.duja||={};draft.value.calibrationInitialPartitionParameters.duja[key]=n}
 else draft.value.calibrationInitialParameters[key]=n
}
function target(){
 draft.value.search.keys=draft.value.target==='lake'?['lakeCoefficient']:['KC','WM','B','SM','KI','KG','tauHours','interflowTauHours','baseflowTauHours']
 draft.value.search.scope=draft.value.target==='lake'?'background':'duja'
}
async function save(kind){
 if(!draft.value||working.value||props.locked)return
 working.value=true;error.value='';message.value=''
 try{
  let id=parent.value
  if(!id||dirty.value){
   if(builtin.value)throw Error('内置方案请先复制，再修改和保存。')
   const row=await api('schemes',{payload:draft.value,...(id?{parentId:id}:{})})
   draft.value=prepare(row.payload);parent.value=row.id;snapshot.value=JSON.stringify(draft.value);id=row.id;emit('selected',id)
  }
  if(kind){
   emit('submit',{kind,researchSchemeId:id,name:draft.value.name+(kind==='calibration'?' · 率定':' · 试算')})
  }else{message.value='方案已保存。';emit('refresh')}
 }catch(e){error.value=e.message}finally{working.value=false}
}
async function showTrash(){
 error.value=''
 try{trash.value=(await api('schemes?includeTrashed=true')).schemes.filter(s=>s.trashed||s.payload?.trashed)}
 catch(e){error.value=e.message}
}
async function moveToTrash(){
 if(!parent.value||builtin.value)return
 working.value=true;error.value=''
 try{await api('schemes/'+encodeURIComponent(parent.value)+'/metadata',{trashed:true});draft.value=null;parent.value='';snapshot.value='';message.value='方案已移入回收站。';emit('selected','');emit('refresh')}
 catch(e){error.value=e.message}finally{working.value=false}
}
async function restore(id){
 working.value=true;error.value=''
 try{await api('schemes/'+encodeURIComponent(id)+'/metadata',{trashed:false});trash.value=trash.value.filter(s=>s.id!==id);message.value='方案已恢复。';emit('refresh')}
 catch(e){error.value=e.message}finally{working.value=false}
}
</script>
<template>
 <div class="scheme-editor">
  <div class="result-toolbar">
   <label class="grow">方案<select :value="parent" @change="choose($event.target.value)"><option v-if="!parent" value="">新建方案</option><option v-for="s in rows" :key="s.id" :value="s.id">{{s.name}}{{s.builtin||s.payload?.builtin?' · 内置':''}}</option></select></label>
   <button :disabled="working||locked" @click="create">新建方案</button><button :disabled="!draft||working||locked" @click="copy">复制方案</button>
   <button :disabled="!parent||builtin||working||locked" @click="moveToTrash">移入回收站</button><button @click="trash===null?showTrash():trash=null">{{trash===null?'方案回收站':'关闭回收站'}}</button>
  </div>
  <div v-if="trash!==null" class="scheme-trash"><b>方案回收站</b><p v-if="!trash.length" class="muted">暂无已移入回收站的方案。</p><div v-for="s in trash" :key="s.id" class="trash-row"><span>{{s.name}}</span><button :disabled="working" @click="restore(s.id)">恢复</button></div></div>
  <p v-if="error" class="message error" role="alert">{{error}}</p><p v-if="message" class="message success" role="status">{{message}}</p>
  <template v-if="draft">
   <div class="scheme-heading-row"><div><h3>{{draft.name}}</h3><span class="muted">{{builtin?'内置方案 · 复制后可修改':dirty?'有未保存的修改':'方案设置已保存'}}</span></div><button class="primary" :disabled="builtin||working||locked||!draft.name?.trim()" @click="save()">保存方案</button></div>
   <div class="query-grid">
    <label class="grow">方案名称<input v-model="draft.name" maxlength="120" :disabled="builtin||locked"></label>
    <label class="grow">气象资料<select v-model="draft.inputId" :disabled="builtin||locked"><option v-for="i in boot.inputs" :key="i.id" :value="i.id">{{i.name}} · {{i.manifest.start.slice(0,10)}} — {{i.manifest.end.slice(0,10)}}</option></select></label>
   </div>
   <fieldset class="scheme-periods"><legend>计算与评价时段</legend><label v-for="(label,key) in {warmupStart:'预热起日',trainingStart:'率定起日',trainingEnd:'率定止日（不含）',evaluationStart:'评价起日',evaluationEnd:'评价止日（不含）'}" :key="key">{{label}}<input type="date" :disabled="builtin||locked" :value="draft.periods[key].slice(0,10)" @input="draft.periods[key]=$event.target.value+'T00:00:00'"></label></fieldset>
   <div class="result-toolbar"><label>参数适用范围<select v-model="scope"><option value="background">其他分区共用</option><option v-for="r in regions" :key="r" :value="r">{{regionNames[r]||r}}</option></select></label><span class="muted">分区参数单独保存，未单独设置的分区使用共用值。</span></div>
   <div class="parameter-group-tabs"><button v-for="g in groups" :key="g" :class="{primary:g===group}" @click="group=g">{{labels[g]||g}}</button></div>
   <div class="table-wrap scheme-parameters"><table><thead><tr><th>参数</th><th>当前值</th><th>单位</th><th>允许范围</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td :title="d.parameterBasis">{{d.label}}</td><td><input type="number" :min="d.min" :max="d.max" :step="d.step" :value="valueFor(d.key)" :disabled="builtin||locked" :aria-label="d.label" @input="setValue(d.key,$event)"></td><td>{{d.unit||'无量纲'}}</td><td>{{number(d.min,6)}} — {{number(d.max,6)}}</td></tr></tbody></table></div>
   <details><summary>率定设置</summary>
    <div class="query-grid"><label>评价对象<select v-model="draft.target" :disabled="builtin||locked" @change="target"><option value="duja">杜加站流量</option><option value="lake">主站湖位</option></select></label><label>目标<select v-model="draft.search.objectiveProfile" :disabled="builtin||locked"><option value="balanced">综合误差</option><option value="peaks">偏重洪峰</option></select></label><label v-for="(label,key) in {runoff:'产流评价次数',routing:'汇流评价次数',lake:'湖泊评价次数'}" :key="key">{{label}}<input type="number" min="1" max="2000" v-model.number="draft.search.budgets[key]" :disabled="builtin||locked"></label></div><p class="note">{{draft.target==='duja'?'杜加站流量率定作用于杜加控制区。':'湖位率定作用于全湖。'}}</p>
    <div class="result-toolbar"><button :disabled="builtin||locked" @click="setInitialFromCurrent">将当前参数设为率定初值</button><span class="muted">采用参数和率定初值分别保存。</span></div>
    <div class="table-wrap scheme-parameters"><table><thead><tr><th>参与率定</th><th>参数</th><th>率定初值</th><th>下界</th><th>上界</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td><input type="checkbox" v-model="draft.search.keys" :value="d.key" :disabled="builtin||locked||(draft.target==='lake'?d.key!=='lakeCoefficient':d.key==='lakeCoefficient')" :aria-label="'率定'+d.label"></td><td>{{d.label}} · {{d.unit||'无量纲'}}</td><td><input type="number" :step="d.step" :value="initialFor(d.key)" :disabled="builtin||locked" @input="setInitial(d.key,$event)"></td><td><input type="number" :step="d.step" v-model.number="draft.search.bounds[d.key][0]" :disabled="builtin||locked"></td><td><input type="number" :step="d.step" v-model.number="draft.search.bounds[d.key][1]" :disabled="builtin||locked"></td></tr></tbody></table></div>
   </details>
   <div class="actions scheme-actions"><button :disabled="working||locked" @click="save('trial')">试算</button><button class="primary" :disabled="working||locked" @click="save('calibration')">开始率定</button><button :disabled="!parent||dirty" @click="emit('simulate',{schemeId:parent})">用此方案模拟 →</button></div>
   <p v-if="dirty" class="note">试算或率定会先保存当前方案设置。运行完成后，可从结果保存实际使用的参数。</p>
  </template>
  <p v-else class="empty">请选择或新建方案。</p>
 </div>
</template>
<style scoped>
.scheme-heading-row{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:22px 0}.scheme-heading-row h3{margin-bottom:3px}.scheme-periods{display:flex;flex-wrap:wrap;gap:16px;border:1px solid var(--line);padding:16px;margin:22px 0}.scheme-periods label{flex:1 1 140px}.scheme-parameters input[type=number]{min-width:100px;max-width:150px}.scheme-parameters table{min-width:650px}.scheme-actions{justify-content:flex-end}.scheme-trash{padding:16px;border:1px solid var(--line);border-radius:6px;margin:16px 0}.trash-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0}
</style>
