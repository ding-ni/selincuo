<script setup>
import {ref,computed,watch} from 'vue'
import {api,number} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
import {transferMode,transferNames,liftDujaToShared,parameterValueSource,calibrationParameterScope,parseCalibrationSeeds} from './parameter-transfer.mjs'
import PermanentDeletion from './PermanentDeletion.vue'
import ParameterTransferAudit from './ParameterTransferAudit.vue'
const props=defineProps({boot:Object,locked:Boolean,busy:Boolean,selectedId:String})
const emit=defineEmits(['refresh','submit','selected','simulate','deleted'])
const draft=ref(null),parent=ref(''),snapshot=ref(''),error=ref(''),message=ref(''),working=ref(false),group=ref('land'),scope=ref('background'),trash=ref(null),parameterTrashRows=ref([]),settingsOpen=ref(false),plan=ref(null),planError=ref(''),coverage=ref(null),transferChoice=ref('duja-shared'),seedsText=ref('42')
let planTicket=0
const copyValue=value=>JSON.parse(JSON.stringify(value))
const rows=computed(()=>(props.boot.schemes||[]).filter(s=>s.payload?.modelFamily==='grid-continuous-v1'))
const builtin=computed(()=>!!(rows.value.find(s=>s.id===parent.value)?.builtin||draft.value?.builtin))
const dirty=computed(()=>!!draft.value&&JSON.stringify(draft.value)!==snapshot.value)
const displayGroup=d=>d.group==='snow'?'cryosphere':d.group
const groups=computed(()=>['land','cryosphere','routing','lake'].filter(g=>(props.boot.parameterDefinitions||[]).some(d=>displayGroup(d)===g)))
const labels={land:'陆面产流',cryosphere:'积雪与冰川',routing:'河道汇流',lake:'湖泊蒸发与蓄水'}
const regionNames={duja:'杜加站上游参数',zajia:'扎加藏布',zajia_rest:'扎加下游区',zagen:'扎根藏布',boqu:'波曲藏布',ali:'阿里藏布',fringe:'湖周区',unresolved:'其他区域'}
const mode=computed(()=>transferMode(draft.value)),regions=computed(()=>mode.value==='legacy'?Object.keys(draft.value?.partitionParameters||{}):[])
const definitions=computed(()=>(props.boot.parameterDefinitions||[]).filter(d=>displayGroup(d)===group.value))
const defaultFlowKeys=computed(()=>[...(props.boot.calibrationConfig?.runoffKeys||[]),...(props.boot.calibrationConfig?.routingKeys||[])])
const flowKeys=computed(()=>props.boot.calibrationConfig?.flowSelectableKeys||defaultFlowKeys.value)
const allowedKeys=computed(()=>draft.value?.target==='lake'?['lakeCoefficient']:draft.value?.target==='integrated'?[...defaultFlowKeys.value,'lakeCoefficient']:flowKeys.value)
const selectedSnow=computed(()=>(draft.value?.search?.keys||[]).some(key=>(props.boot.calibrationConfig?.snowKeys||[]).includes(key)))
const trashObjects=computed(()=>[...(trash.value||[]).map(s=>({kind:'scheme',id:s.id,name:s.name})),...parameterTrashRows.value.filter(p=>p.trashed).map(p=>({kind:'parameter',id:p.id,name:p.name}))])
const activeParameterRows=computed(()=>parameterTrashRows.value.filter(p=>!p.trashed))
function prepare(value){
 const data=copyValue(value)
 data.partitionParameters||={}
 data.initialization||={}
 data.initialization.mode||='legacy'
 data.calibrationInitialParameters||=copyValue(data.parameters)
 data.calibrationInitialPartitionParameters||=copyValue(data.partitionParameters)
 data.search.budgets.snow??=120
 return data
}
function choose(id,clearMessage=true){
 const row=rows.value.find(s=>s.id===id)
 if(!row)return
 draft.value=prepare(row.payload);parent.value=row.id;snapshot.value=JSON.stringify(draft.value);scope.value='background';transferChoice.value=mode.value==='legacy'?'duja-shared':mode.value;seedsText.value=(draft.value.search.seeds||[draft.value.search.seed??42]).join(', ');if(clearMessage)message.value='';error.value='';emit('selected',row.id)
}
function chooseFromUser(event){
 const id=event.target.value
 if(id!==parent.value&&dirty.value&&!confirm('当前方案有未保存的修改。切换后这些修改将丢失，继续切换吗？')){event.target.value=parent.value;return}
 choose(id)
}
watch(rows,value=>{if(props.selectedId&&props.selectedId!==parent.value&&value.some(r=>r.id===props.selectedId)){choose(props.selectedId,false);return}if(!draft.value&&value.length){const row=value.find(r=>r.id===props.selectedId)||value.find(r=>r.builtin||r.payload?.builtin)||value[0];choose(row.id,false)}},{immediate:true})
watch(()=>props.selectedId,id=>{if(id&&id!==parent.value&&rows.value.some(s=>s.id===id))choose(id,false)})
watch(()=>props.boot,async()=>{try{coverage.value=(await api('forcing-coverage')).current}catch{coverage.value=null}},{immediate:true})
watch(()=>[parent.value,snapshot.value,props.boot],async()=>{
 const ticket=++planTicket;plan.value=null;planError.value=''
 if(!parent.value||!draft.value)return
 const periods=draft.value.periods
 try{const result=await api('simulation-plan?'+new URLSearchParams({researchSchemeId:parent.value,start:periods.trainingStart,end:periods.evaluationEnd}));if(ticket===planTicket)plan.value=result}
 catch(e){if(ticket===planTicket)planError.value=e.message}
},{immediate:true})
async function create(){
 if(dirty.value&&!confirm('当前方案有未保存的修改。新建方案后这些修改将丢失，继续新建吗？'))return
 working.value=true;error.value=''
 try{draft.value=prepare(await api('research-template'));draft.value.name='新方案';draft.value.initialization.mode='automatic';delete draft.value.builtin;parent.value='';snapshot.value='';scope.value='background';transferChoice.value=mode.value==='legacy'?'duja-shared':mode.value;seedsText.value=(draft.value.search.seeds||[draft.value.search.seed??42]).join(', ');message.value='';emit('selected','')}
 catch(e){error.value=e.message}finally{working.value=false}
}
function copy(){
 if(!draft.value)return
 const source={schemeId:parent.value||draft.value.sourceTransferSnapshot?.schemeId||null,configurationSHA256:draft.value.configurationSHA256||draft.value.sourceTransferSnapshot?.configurationSHA256||null}
 draft.value=prepare(draft.value);draft.value.name=draft.value.name+' · 副本';draft.value.sourceCopy=source
 draft.value.initialization.mode='automatic'
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
 return mode.value==='legacy'&&s==='duja'&&key!=='lakeCoefficient'?draft.value.calibrationInitialPartitionParameters.duja?.[key]??draft.value.calibrationInitialParameters[key]:draft.value.calibrationInitialParameters[key]
}
function setInitial(key,event){
 const n=event.target.value===''?NaN:Number(event.target.value)
 if(mode.value==='legacy'&&draft.value.search.scope==='duja'&&key!=='lakeCoefficient'){draft.value.calibrationInitialPartitionParameters.duja||={};draft.value.calibrationInitialPartitionParameters.duja[key]=n}
 else draft.value.calibrationInitialParameters[key]=n
}
function convertTransfer(){
 if(!draft.value||builtin.value||props.locked)return
 if(mode.value==='legacy'){
  const source=draft.value.sourceCopy||{schemeId:parent.value,configurationSHA256:draft.value.configurationSHA256}
  draft.value=liftDujaToShared(draft.value,transferChoice.value,source);scope.value='background'
  message.value='已用杜加参数建立共享基准，原分区设置保存在转换记录；保存方案后可预览各区实际采用值。'
 }else{
  draft.value.parameterTransfer={...draft.value.parameterTransfer,mode:transferChoice.value}
  message.value='已修改参数传递方式，保存方案后可预览各区实际采用值。'
 }
}
function target(){
 draft.value.search.keys=draft.value.target==='lake'?['lakeCoefficient']:draft.value.target==='integrated'?[...defaultFlowKeys.value,'lakeCoefficient']:[...defaultFlowKeys.value]
 draft.value.search.scope=draft.value.target==='lake'?'background':'duja'
 draft.value.search.budgets.integrated??=120
 if(draft.value.target==='integrated')draft.value.search.joint={method:'rmse-over-training-std-v1',weights:{flow:.5,lake:.5},minimumPairs:30,minimumScale:1e-10}
}
function lakeInitializationIssue(value){
 const initial=value.initialization||{},level=initial.initialLakeLevel,time=String(initial.initialLakeTime||'')
 if(initial.mode!=='manual')return ''
 if(level==null||level===''||!time)return '请填写湖位起算时间和初始湖位。'
 if(!Number.isFinite(Number(level))||Number(level)<4000||Number(level)>6500)return '初始湖位须在4000—6500米之间。'
 if(!/^\d{4}-\d{2}-\d{2}T\d{2}:00(?::00)?$/.test(time))return '湖位起算时间须为北京时间整小时。'
 const parsed=new Date(time.slice(0,16)+':00Z')
 if(!Number.isFinite(parsed.getTime())||parsed.toISOString().slice(0,16)!==time.slice(0,16))return '请填写有效的湖位起算时间。'
 return ''
}
function displayDay(key){const day=draft.value.periods[key]?.slice(0,10)||'';return day&&['trainingEnd','evaluationEnd'].includes(key)?shiftCalendarDay(day,-1):day}
function setDay(key,value){if(!value)return;draft.value.periods[key]=(['trainingEnd','evaluationEnd'].includes(key)?shiftCalendarDay(value,1):value)+'T00:00:00'}
function seedsChanged(event){seedsText.value=event.target.value;try{draft.value.search.seeds=parseCalibrationSeeds(seedsText.value)}catch{}}
async function save(kind){
 if(!draft.value||working.value||props.locked)return
 working.value=true;error.value='';message.value=''
 try{
  const issue=lakeInitializationIssue(draft.value);if(issue)throw Error(issue)
  const seeds=parseCalibrationSeeds(seedsText.value)
  if(builtin.value&&JSON.stringify(draft.value.search.seeds||[draft.value.search.seed??42])!==JSON.stringify(seeds))throw Error('内置方案请先复制再调整搜索起点。')
  if(!builtin.value&&JSON.stringify(draft.value.search.seeds||[draft.value.search.seed??42])!==JSON.stringify(seeds))draft.value.search.seeds=seeds
  let id=parent.value
  if(!id||dirty.value){
   if(builtin.value)throw Error('内置方案请先复制，再修改和保存。')
   const payload=copyValue(draft.value)
   delete payload.sourceCopy
   if(payload.initialization.mode==='manual')payload.initialization.initialLakeSource=String(payload.initialization.initialLakeSource||'').trim()||'用户手动输入（未注明详细来源）'
   if(Object.hasOwn(payload.initialization,'origin'))payload.initialization.origin=payload.periods.warmupStart
   const row=await api('schemes',{payload,...(id?{parentId:id}:{})})
   draft.value=prepare(row.payload);parent.value=row.id;snapshot.value=JSON.stringify(draft.value);id=row.id;emit('selected',id)
  }
  if(kind){
   emit('submit',{kind,researchSchemeId:id,name:draft.value.name+(kind==='calibration'?' · 率定':' · 试算')})
  }else{message.value='方案已保存到本机，重新打开平台后仍可使用。';emit('refresh')}
 }catch(e){error.value=e.message}finally{working.value=false}
}
async function showTrash(){
 error.value=''
 try{
  const [schemes,parameters]=await Promise.all([api('schemes?includeTrashed=true'),api('parameters?includeTrashed=true')])
  trash.value=schemes.schemes.filter(s=>s.trashed||s.payload?.trashed)
  parameterTrashRows.value=parameters.parameters
 }
 catch(e){error.value=e.message}
}
function toggleTrash(){if(trash.value===null)showTrash();else trash.value=null}
async function moveToTrash(){
 if(!parent.value||dirty.value||working.value||props.busy)return
 working.value=true;error.value=''
 try{await api('schemes/'+encodeURIComponent(parent.value)+'/metadata',{trashed:true});draft.value=null;parent.value='';snapshot.value='';message.value='方案已移入回收站，已有成果保留。';emit('selected','');emit('refresh');if(trash.value!==null)await showTrash()}
 catch(e){error.value=e.message}finally{working.value=false}
}
async function restore(id){
 working.value=true;error.value=''
 try{await api('schemes/'+encodeURIComponent(id)+'/metadata',{trashed:false});trash.value=trash.value.filter(s=>s.id!==id);message.value='方案已恢复。';emit('refresh')}
 catch(e){error.value=e.message}finally{working.value=false}
}
async function setParameterTrash(id,trashed){
 if(working.value||props.busy)return
 working.value=true;error.value=''
 try{
  await api('parameters/'+encodeURIComponent(id)+'/metadata',{trashed})
  await showTrash();message.value=trashed?'参数已移入回收站，已有方案和成果保留。':'参数已恢复。';emit('refresh')
 }catch(e){error.value=e.message}finally{working.value=false}
}
async function afterPermanentDeletion(result){await showTrash();emit('deleted',result)}
function restoreObject(item){if(item.kind==='scheme')restore(item.id);else setParameterTrash(item.id,false)}
</script>
<template>
 <div class="scheme-editor">
  <div class="result-toolbar">
   <label class="grow">方案<select :value="parent" :disabled="working||busy" @change="chooseFromUser"><option v-if="!parent" value="">新建方案</option><option v-for="s in rows" :key="s.id" :value="s.id">{{s.name}}{{s.builtin||s.payload?.builtin?' · 内置':''}}</option></select></label>
   <button :disabled="working||locked" @click="create">新建方案</button><button :disabled="!draft||working||locked" @click="copy">复制方案</button>
   <button :disabled="!parent||dirty||working||busy" :title="dirty?'请先保存方案修改':''" @click="moveToTrash">移入回收站</button><button @click="toggleTrash">{{trash===null?'方案与参数回收站':'关闭回收站'}}</button>
  </div>
  <div v-if="trash!==null" class="scheme-trash">
   <b>方案与参数回收站</b><p class="muted">回收站内的方案和参数可恢复，也可在查看依赖后彻底删除。</p>
   <PermanentDeletion :objects="trashObjects" :busy="working||busy" @changed="afterPermanentDeletion" @restore="restoreObject"/>
   <b>已保存参数</b><p class="muted">移入回收站后，已有方案和成果仍保留原参数。</p><p v-if="!activeParameterRows.length" class="muted">暂无可回收的已保存参数。</p><div v-for="p in activeParameterRows" :key="p.id" class="trash-row"><span>{{p.name}}</span><button :disabled="working||busy" @click="setParameterTrash(p.id,true)">移入回收站</button></div>
  </div>
  <p v-if="error" class="message error" role="alert">{{error}}</p><p v-if="message" class="message success" role="status">{{message}}</p>
  <template v-if="draft">
   <div class="scheme-heading-row"><div><h3>{{draft.name}}</h3><span class="muted">{{builtin?'内置方案 · 复制后可修改':dirty?'有未保存的修改':'方案设置已保存'}}</span></div><button class="primary" :disabled="builtin||working||locked||!draft.name?.trim()" @click="save()">保存方案</button></div>
   <div class="query-grid">
    <label class="grow">方案名称<input v-model="draft.name" maxlength="120" :disabled="builtin||locked"></label>
    <p class="muted">气象资料由平台自动匹配。<template v-if="coverage?.complete">完整日 {{coverage.firstCompleteDay}} 至 {{coverage.lastCompleteDay}}。</template></p>
   </div>
   <fieldset class="scheme-periods"><legend>计算与检验时段（包含止日）</legend><label v-for="(label,key) in {warmupStart:'预热起日',trainingStart:'率定起日',trainingEnd:'率定止日',evaluationStart:'检验起日',evaluationEnd:'检验止日'}" :key="key">{{label}}<input type="date" :disabled="builtin||locked" :value="displayDay(key)" @input="setDay(key,$event.target.value)"></label></fieldset>
   <p v-if="plan?.initialization&&!dirty" class="note">湖位从 {{plan.initialization.time.replace('T',' ').slice(0,16)}} 起算，起算湖位 {{number(plan.initialization.level,2)}} m；<template v-if="plan.earliestCompleteLakeDay">首个完整湖位日为 {{plan.earliestCompleteLakeDay}}。</template><template v-else>所选时段没有完整湖位日。</template><template v-if="plan.initialization.method==='first-quality-daily-level-at-noon'||plan.initialization.source?.includes('日值')">日值按12时代表，非已核实的瞬时观测。</template></p>
   <p v-else-if="plan&&!plan.initialization&&!dirty" class="note">当前时段没有可用湖位起算资料，可计算流量；补充湖位依据后可计算绝对湖位。</p>
   <p v-else-if="planError&&!dirty" class="note warning">起算资料检查：{{planError}}</p>
   <p v-else-if="dirty" class="note">保存后可查看本方案的实际起算日期和湖位。</p>
   <details class="lake-start"><summary>高级计算设置</summary>
    <div class="query-grid"><label>湖位起算方式<select v-model="draft.initialization.mode" :disabled="builtin||locked"><option value="automatic">自动确定</option><option v-if="draft.initialization.mode==='legacy'" value="legacy">沿用已有起算依据</option><option value="manual">指定起算水位</option></select></label></div>
    <div v-if="draft.initialization.mode==='manual'" class="query-grid"><label>起算时间（北京时间）<input type="datetime-local" step="3600" v-model="draft.initialization.initialLakeTime" :disabled="builtin||locked"></label><label>初始湖位（m）<input type="number" min="4000" max="6500" step="any" v-model.number="draft.initialization.initialLakeLevel" :disabled="builtin||locked"></label><label class="grow">依据说明（选填）<input v-model="draft.initialization.initialLakeSource" :disabled="builtin||locked" placeholder="例如站点、日期或资料来源"></label></div>
    <p class="note">自定湖位请核对资料依据。计算时会检查起算前的预热资料；起算前可计算流量。日值按12时代表，不视作瞬时测量。</p>
   </details>
   <section class="transfer-settings">
    <h4>参数在流域内的使用</h4>
    <p class="note">当前：{{transferNames[mode]}}。{{mode==='legacy'?'各区可沿用已有单独设置，未单独设置时使用共用值。':'各区从同一组杜加共享参数起算，尚未率定的参数仍为先验值。'}}</p>
    <p v-if="mode==='duja-area-routing'" class="note">子流域调蓄时间与传播滞时按面积平方根缩放，是等形、等流速假设；尚缺独立验证。湖周与未判定等范围不参与缩放。</p>
    <div class="result-toolbar"><label>选择传递方式<select v-model="transferChoice" :disabled="builtin||locked||!!parent"><option value="duja-shared">杜加参数共享</option><option value="duja-area-routing">杜加共享与汇流面积缩放</option></select></label><button v-if="mode==='legacy'" :disabled="builtin||locked||!!parent" @click="convertTransfer">以杜加参数建立共享基准</button><button v-else :disabled="builtin||locked||!!parent||transferChoice===mode" @click="convertTransfer">应用传递方式</button></div>
    <p v-if="parent" class="note">要更改传递方式，请先复制方案；原方案和历史成果保持原设置。</p>
    <p v-else-if="mode==='legacy'" class="note">建立共享基准会将杜加的已有区域值并入共用参数，清除原分区单独设置；全湖参数保持原值，转换前设置保存在方案记录中。</p>
    <p v-if="draft.lakeParameterSource?.method==='fixed-potential-open-water-proxy'" class="note">湖面系数采用1.0，作为潜在水面蒸发的近似；它并非直接湖面蒸发观测，也未用湖位率定。</p>
   </section>
   <div class="result-toolbar"><label>参数适用范围<select v-model="scope"><option value="background">流域共用参数</option><option v-for="r in regions" :key="r" :value="r">{{regionNames[r]||r}}</option></select></label><span class="muted">{{scope==='background'?'未单独设置的区域采用共用值。':'该范围未单独设置的参数使用共用值。'}}</span></div>
   <div class="parameter-group-tabs"><button v-for="g in groups" :key="g" :class="{primary:g===group}" @click="group=g">{{labels[g]||g}}</button></div>
   <div class="table-wrap scheme-parameters"><table><thead><tr><th>参数 / 依据</th><th>当前值</th><th>单位</th><th>允许范围</th><th>当前值来源</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td>{{d.label}}<small class="parameter-basis">{{d.parameterBasis||'依据待核实'}}</small></td><td><input type="number" :min="d.min" :max="d.max" :step="d.step" :value="valueFor(d.key)" :disabled="builtin||locked" :aria-label="d.label" @input="setValue(d.key,$event)"></td><td>{{d.unit||'无量纲'}}</td><td>{{number(d.min,6)}} — {{number(d.max,6)}}</td><td>{{parameterValueSource(draft,scope,d.key)}}</td></tr></tbody></table></div>
   <details v-if="plan?.parameterTransferAudit&&!dirty"><summary>查看本方案各区实际采用值</summary><ParameterTransferAudit :audit="plan.parameterTransferAudit" :definitions="boot.parameterDefinitions"/></details>
   <div v-show="settingsOpen" id="calibration-settings" class="calibration-settings">
    <p class="note">{{draft.target==='duja'?'杜加流量用于率定，主站湖位仅作评价，不参与本次参数排序。':draft.target==='lake'?'主站湖位参与本次参数率定。':'杜加流量与主站湖位均参与本次综合率定；目前综合研究仅开放默认产汇流参数与湖面系数。'}} 雪冰参数如果参与流量率定，仅受流量条件约束，不能当成独立雪冰观测验证。</p>
    <div class="query-grid"><label>率定方式<select v-model="draft.target" :disabled="builtin||locked" @change="target"><option value="duja">杜加流量率定，湖位仅评价</option><option value="lake">主站湖位率定（研究）</option><option value="integrated">流量与湖位综合率定（研究）</option></select></label><label v-if="draft.target==='integrated'">综合评价次数<input type="number" min="1" max="2000" v-model.number="draft.search.budgets.integrated" :disabled="builtin||locked"></label><template v-else><label v-if="draft.target==='duja'">流量目标<select v-model="draft.search.objectiveProfile" :disabled="builtin||locked"><option value="balanced">洪峰、低流量与水量平衡</option><option value="peaks">偏重洪峰</option></select></label><label v-for="(label,key) in (draft.target==='lake'?{lake:'湖位评价次数'}:{runoff:'产流评价次数',...(selectedSnow?{snow:'雪冰评价次数'}:{}),routing:'汇流评价次数'})" :key="key">{{label}}<input type="number" min="1" max="2000" v-model.number="draft.search.budgets[key]" :disabled="builtin||locked"></label></template><label>搜索起点种子（1—4个）<input :value="seedsText" :disabled="builtin||locked" placeholder="例如 42, 101" @input="seedsChanged"></label></div>
    <p class="note">每阶段评价次数包含基线、单参数扰动和所有搜索起点；额外起点不会增加总次数上限。</p>
    <div class="result-toolbar"><button :disabled="builtin||locked" @click="setInitialFromCurrent">将当前参数设为率定初值</button><span class="muted">采用参数和率定初值分别保存。</span></div>
    <div class="table-wrap scheme-parameters"><table><thead><tr><th>参与率定</th><th>参数与作用范围</th><th>率定初值</th><th>下界</th><th>上界</th><th>说明</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td><input type="checkbox" v-model="draft.search.keys" :value="d.key" :disabled="builtin||locked||!allowedKeys.includes(d.key)" :aria-label="'率定'+d.label"></td><td>{{d.label}} · {{d.unit||'无量纲'}}<span v-if="allowedKeys.includes(d.key)" class="muted"> · {{calibrationParameterScope(draft,d.key)}}</span></td><td><input type="number" :step="d.step" :value="initialFor(d.key)" :disabled="builtin||locked||!allowedKeys.includes(d.key)" @input="setInitial(d.key,$event)"></td><td><input type="number" :step="d.step" v-model.number="draft.search.bounds[d.key][0]" :disabled="builtin||locked||!allowedKeys.includes(d.key)"></td><td><input type="number" :step="d.step" v-model.number="draft.search.bounds[d.key][1]" :disabled="builtin||locked||!allowedKeys.includes(d.key)"></td><td>{{d.key==='lakeCoefficient'&&['lake','integrated'].includes(draft.target)?'湖位进入本次率定，湖面蒸发仍缺直接观测。':d.flowCalibration?.condition||(!allowedKeys.includes(d.key)?'当前率定方式不调整此参数':'仅所选观测约束')}}</td></tr></tbody></table></div>
   </div>
   <div class="actions scheme-actions"><button type="button" :aria-expanded="settingsOpen" aria-controls="calibration-settings" @click="settingsOpen=!settingsOpen">{{settingsOpen?'收起率定设置':'率定设置'}}</button><button :disabled="working||locked" @click="save('trial')">试算</button><button class="primary" :disabled="working||locked" @click="save('calibration')">开始率定</button><button :disabled="!parent||dirty" @click="emit('simulate',{schemeId:parent})">运行模拟</button></div>
   <p class="muted">流域产流、汇流形成入湖流量，与湖面降水和蒸发共同影响湖泊蓄水及水位。</p>
   <p v-if="dirty" class="note">试算或率定会先保存当前方案设置。运行完成后，可从结果保存实际使用的参数。</p>
  </template>
  <p v-else class="empty">请选择或新建方案。</p>
 </div>
</template>
<style scoped>
.scheme-heading-row{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:22px 0}.scheme-heading-row h3{margin-bottom:3px}.scheme-periods{display:flex;flex-wrap:wrap;gap:16px;border:1px solid var(--line);padding:16px;margin:22px 0}.scheme-periods label{flex:1 1 140px}.scheme-parameters input[type=number]{min-width:100px;max-width:150px}.scheme-parameters table{min-width:850px}.parameter-basis{display:block;max-width:330px;margin-top:4px;line-height:1.5}.transfer-settings,.calibration-settings{margin:20px 0;padding:16px;border:1px solid var(--line);border-radius:6px}.transfer-settings h4{margin:0 0 10px}.scheme-actions{justify-content:flex-end}.scheme-trash{padding:16px;border:1px solid var(--line);border-radius:6px;margin:16px 0}.trash-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0}
</style>
