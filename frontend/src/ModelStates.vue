<script setup>
import {ref,computed,watch,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import ScreenMap from './ScreenMap.vue'
import {api,number,resultName} from './api.mjs'
import {scaledLayer} from './map-scale.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
const props=defineProps({boot:Object,tasks:Array,states:Array,initialId:String,busy:Boolean,viewDay:String})
const emit=defineEmits(['resume','rename','context'])
const unitSearch=ref(''),partitionFilter=ref('')
const scaleMode=ref('auto'),scaleLow=ref(0),scaleHigh=ref(1),scaleError=ref('')
const tab=ref('space'),id=ref(props.initialId||props.boot.homeResultId||''),meta=ref(null),variable=ref('swe'),unit=ref(0),start=ref(''),end=ref(''),time=ref(''),step=ref('day'),rows=ref([]),frame=ref(null),error=ref(''),working=ref(false),geometry=ref([]),selectedState=ref(null),stateName=ref(''),showAllStates=ref(false)
let sequence=0,controller
const choices=computed(()=>[...props.boot.publishedResults.map(t=>({...t,builtin:true})),...props.tasks.filter(t=>t.status==='succeeded'&&!t.trashed&&(['simulation','continuation'].includes(t.kind)||t.id===props.initialId))])
const library=computed(()=>props.states.filter(s=>showAllStates.value||s.name||s.favorite||props.tasks.some(t=>t.id===s.task_id&&['simulation','continuation'].includes(t.kind))))
const definition=computed(()=>meta.value?.variables.find(v=>v.id===variable.value))
const labels={duja:'杜加控制区',zajia_rest:'扎加下游区',zajia:'扎加藏布',zagen:'扎根藏布',boqu:'波曲藏布',ali:'阿里藏布',fringe:'其他入湖区（旧版为残余区）',unresolved:'排水归属未核实区',external:'其他闭合或外排区',total:'已计入入湖合计',lake:'色林错'}
const units=computed(()=>definition.value?.scope==='unit'?(meta.value?.units||[]).map(u=>({...u,label:(u.grid?u.grid+' · ':'')+(labels[u.partition]||u.partition)+' · '+u.elevationBandMin+'—'+u.elevationBandMax+'m · '+(u.kind==='glacier'?'冰川':'非冰川')})):(definition.value?.scope==='river'?meta.value?.rivers:definition.value?.scope==='partition'?meta.value?.partitions:['lake'])?.map(id=>({id,label:labels[id]||id}))||[])
const partitionChoices=computed(()=>[...new Set(units.value.map(u=>u.partition).filter(Boolean))])
const filteredUnits=computed(()=>units.value.map((u,index)=>({...u,index})).filter(u=>(!partitionFilter.value||u.partition===partitionFilter.value)&&(!unitSearch.value||[u.label,u.id,u.grid,u.partition,u.elevationBandMin,u.elevationBandMax,u.kind].join(' ').toLowerCase().includes(unitSearch.value.trim().toLowerCase()))))
const variableGroups=computed(()=>[['土壤', ['WU','WL','WD','freeWater','FR']],['雪冰',['swe','snowLiquid','glacierStorage','snowmelt','icemelt','iceSublimation','glacierRunoff']],['产流与水量',['RS','RI','RG','actualEt','balanceError']],['汇流',['flow','routingStorage','interflowStorage','baseflowStorage']],['湖泊',['lakeLevel','volumeChange','continuousLakeVolumeChange','lakeFlux']]].map(([label,ids])=>({label,variables:(meta.value?.variables||[]).filter(v=>ids.includes(v.id))})))
const frameMap=computed(()=>new Map((frame.value?.values||[]).map(v=>[v.id,v.value])))
const numeric=computed(()=>(frame.value?.values||[]).map(v=>v.value).filter(v=>v!=null))
const colorLayer=computed(()=>scaledLayer({id:variable.value,label:definition.value?.label||'状态',unit:definition.value?.unit||'',colors:['#e7f1ea','#c1dfc5','#8fc8b8','#4da8aa','#377f9c','#3e5888'],stops:[]},numeric.value,null,scaleMode.value==='fixed'&&Number.isFinite(Number(scaleLow.value))&&Number.isFinite(Number(scaleHigh.value))&&Number(scaleHigh.value)>Number(scaleLow.value)?[Number(scaleLow.value),Number(scaleHigh.value)]:null))
function lockScale(){if(!numeric.value.length){scaleError.value='所选时刻没有有效数值，无法锁定颜色范围';return}const stops=colorLayer.value.stops;scaleLow.value=stops[0];scaleHigh.value=stops.at(-1);scaleMode.value='fixed';scaleError.value=''}
const mapUnits=computed(()=>(meta.value?.units||[]).map(u=>({id:u.id,value:frameMap.value.get(definition.value?.scope==='unit'?u.id:definition.value?.scope==='partition'?u.partition:u.basin)})))
const resultContext=computed(()=>id.value+':'+variable.value+':'+unit.value)
const stateColumns=computed(()=>selectedState.value?.variables||[])
const stateDay=computed(()=>time.value?shiftCalendarDay(time.value,-1):'')
function setStateDay(day){if(day)time.value=shiftCalendarDay(day,1)+'T00:00'}
function stateUnitLabel(i){const u=selectedState.value?.units?.[i];return u?((u.grid?u.grid+' · ':'')+(labels[u.partition]||u.partition)+' · '+u.elevationBandMin+'—'+u.elevationBandMax+'m · '+(u.kind==='glacier'?'冰川':'非冰川')):(selectedState.value?.state?.unitIds?.[i]||'单元 '+(i+1))}
async function select(){
 const n=++sequence;controller?.abort();controller=new AbortController();working.value=true;error.value='';meta.value=null;rows.value=[];frame.value=null
 if(!id.value){working.value=false;return}
 try{const data=await api('result-capabilities?'+new URLSearchParams({resultId:id.value}),null,{signal:controller.signal});if(n!==sequence)return;meta.value=data;start.value=data.start.slice(0,16);end.value=data.end.slice(0,16);const requested=props.viewDay?new Date(Date.parse(props.viewDay+'T00:00:00Z')+86400000).toISOString().slice(0,16):'';time.value=requested&&requested>=start.value&&requested<=end.value?requested:end.value;partitionFilter.value='';unit.value=0;emit('context',id.value);if(time.value.slice(11,16)!=='00:00')step.value='hour';await query()}catch(e){if(e.name!=='AbortError')error.value=e.message}finally{if(n===sequence)working.value=false}
}
async function query(){
 if(!meta.value)return
 const n=++sequence;controller?.abort();controller=new AbortController();working.value=true;error.value=''
 try{const q={resultId:id.value,variable:variable.value,unit:unit.value,start:start.value,end:end.value,step:step.value}
 const [s,f]=await Promise.all([api('result-series?'+new URLSearchParams(q),null,{signal:controller.signal}),api('result-frame?'+new URLSearchParams({resultId:id.value,variable:variable.value,time:time.value,step:step.value}),null,{signal:controller.signal})]);if(n!==sequence)return;rows.value=s.rows;frame.value=f;const frameDay=step.value==='day'?new Date(Date.parse(time.value+'Z')-86400000).toISOString().slice(0,10):time.value.slice(0,10);emit('context',id.value,frameDay)}
 catch(e){if(e.name!=='AbortError')error.value=e.message}finally{if(n===sequence)working.value=false}
}
function fromDay(day){time.value=day.includes('T')?day:new Date(Date.parse(day+'T00:00:00Z')+86400000).toISOString().slice(0,16);query()}
function all(){start.value=meta.value.start.slice(0,16);end.value=meta.value.end.slice(0,16);query()}
watch(()=>props.initialId,value=>{if(value&&value!==id.value)id.value=value});watch(id,select,{immediate:true});watch(variable,()=>{unit.value=0;partitionFilter.value='';scaleMode.value='auto';scaleError.value='';query()});watch(unit,query)
function selectUnit(key){const index=units.value.findIndex(u=>u.id===key);if(index>=0)unit.value=index}
function exportSeries(){const csv='\ufeff时间,'+(definition.value?.label||'数值')+' ('+(definition.value?.unit||'')+')\r\n'+rows.value.map(r=>r.time+','+(r.value??'')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download='模型过程.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function builtinState(){try{selectedState.value=await api('result-checkpoint?resultId='+encodeURIComponent(id.value));stateName.value='所选成果末状态'}catch(e){error.value=e.message}}
function downloadState(){const blob=new Blob([JSON.stringify(selectedState.value.state,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='model-final-state.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function inspectState(s){try{selectedState.value=await api('states/'+s.id);stateName.value=s.name||''}catch(e){error.value=e.message}}
onBeforeUnmount(()=>{sequence++;controller?.abort()})
</script>
<template>
 <section class="panel"><div class="panel-title"><h3>过程与状态</h3><span v-if="meta" class="badge">{{meta.units.length}} 个计算单元</span></div><nav class="workspace-tabs"><button v-for="t in [['space','状态分布'],['process','单元过程'],['library','完整状态库']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <div v-if="tab!=='library'" class="result-toolbar"><label class="grow">计算成果<select v-model="id"><option value="">请选择成果</option><option v-for="t in choices" :key="t.id" :value="t.id">{{resultName(t)}}</option></select></label><label>状态或过程<select v-model="variable"><optgroup v-for="g in variableGroups" :key="g.label" :label="g.label"><option v-for="v in g.variables" :key="v.id" :value="v.id">{{v.label}} · {{v.unit||'无量纲'}}</option></optgroup></select></label></div>
 <p v-if="error" class="message error" role="alert">{{error}}</p>
 <template v-if="meta&&tab!=='library'">
 <p class="note">分布图用于查看模型状态；续算请使用状态库中的完整末状态。{{step==='day'?definition?.aggregation:'逐时保存值'}}按北京时间显示；500米表示垂直高程分带。</p>
 <div class="result-toolbar state-filter"><label class="grow">格网 / 单元检索<input type="search" v-model="unitSearch" placeholder="输入格网、编号、高程或分区"></label><label v-if="partitionChoices.length">分区<select v-model="partitionFilter"><option value="">全部分区</option><option v-for="p in partitionChoices" :key="p" :value="p">{{labels[p]||p}}</option></select></label><span>{{filteredUnits.length}} / {{units.length}} 个单元</span></div>
 <div class="result-toolbar"><label>开始时刻<input v-model="start" type="datetime-local" step="3600"></label><label>结束时刻<input v-model="end" type="datetime-local" step="3600"></label><label>查询尺度<select v-model="step"><option value="day">逐日 · {{definition?.aggregation}}</option><option value="hour">逐小时</option></select></label><button :disabled="working" @click="query">查询</button><button @click="all">完整时段（含预热）</button><button :disabled="!rows.length" @click="exportSeries">导出过程</button></div>
 <template v-if="tab==='process'"><div class="result-toolbar"><label class="grow">查询单元<select v-model="unit"><option v-for="u in filteredUnits" :key="u.id" :value="u.index">{{u.label}}</option></select></label><span class="muted">{{step==='day'?definition?.aggregation:'逐时保存值'}} · {{definition?.unit}}</span></div><p v-if="!filteredUnits.length" class="note">没有匹配单元，请调整检索条件。</p><Chart :rows="rows" :series="[{key:'value',label:definition?.label,type:definition?.kind==='flux'?'bar':'line'}]" :unit="definition?.unit" :context="resultContext" :hourly="step==='hour'" @day="fromDay"/></template>
 <template v-else><div class="result-toolbar"><label>状态时刻<input v-if="step==='day'" type="date" :value="stateDay" :min="meta.start.slice(0,10)" :max="shiftCalendarDay(meta.end,-1)" @input="setStateDay($event.target.value)"><input v-else v-model="time" type="datetime-local" step="3600" :min="meta.start.slice(0,16)" :max="meta.end.slice(0,16)"></label><span v-if="step==='day'" class="muted">{{stateDay}} 日末</span><button :disabled="working" @click="query">查看分布</button><span class="muted">{{frame?.aggregation||'状态值'}} · {{definition?.unit}}</span></div>
 <div class="result-toolbar state-color-controls"><label>颜色范围<select v-model="scaleMode"><option value="auto">随所选时刻自动调整</option><option value="fixed">固定范围 · 跨时段锁定</option></select></label><button :disabled="!numeric.length" @click="lockScale">锁定当前范围</button><template v-if="scaleMode==='fixed'"><label>下限<input type="number" v-model.number="scaleLow" step="any"></label><label>上限<input type="number" v-model.number="scaleHigh" step="any"></label><span v-if="!(Number(scaleHigh)>Number(scaleLow))" class="note warning">上限须大于下限，当前回退自动范围。</span></template><span>缺测不参与分级</span><span v-if="scaleError" class="note warning">{{scaleError}}</span></div>
 <div class="state-spatial"><ScreenMap :spatial-base="meta.spatialBase" :stations="[]" variable="snow" :snow="{complete:true,units:mapUnits}" :state-layer="colorLayer" :lake-state="definition?.scope==='lake'?frame?.values?.[0]:null" :selected-unit="units[unit]?.id" show-rivers show-names show-ice @unit="selectUnit"/><div><p class="muted">点击地图或表格定位，再查看单元过程。缺测显示“—”。</p><div class="table-wrap"><table><thead><tr><th>空间单元</th><th>{{definition?.label}} / {{definition?.unit}}</th></tr></thead><tbody><tr v-for="u in filteredUnits" :key="u.id" :class="{selected:units[unit]?.id===u.id}" @click="selectUnit(u.id)"><td><button class="link" @click.stop="selectUnit(u.id)">{{u.label}} · {{u.grid||u.id}}</button></td><td>{{number(frameMap.get(u.id),4)}}</td></tr></tbody></table></div><p v-if="!filteredUnits.length" class="note">没有匹配单元。</p><button :disabled="!units[unit]" @click="tab='process'">查看选中单元过程</button></div></div></template>
 </template>
 <p v-else-if="tab!=='library'" class="empty">{{working?'正在读取模型状态…':id?'模型状态暂不可用。':'请选择成果后查看状态。'}}</p>
 <template v-if="tab==='library'"><div class="result-toolbar"><button :disabled="!id" @click="builtinState">查看所选成果末状态</button><label class="inline-check"><input type="checkbox" v-model="showAllStates">显示其他计算状态</label></div><div class="table-wrap"><table><thead><tr><th>有效时刻</th><th>状态名称</th><th>操作</th></tr></thead><tbody><tr v-for="s in library" :key="s.id"><td>{{s.valid_at.replace('T',' ')}}</td><td>{{s.name||tasks.find(t=>t.id===s.task_id)?.name||'完整末状态'}}</td><td><button @click="inspectState(s)">查看</button><button @click="emit('rename',s)">命名</button><button :disabled="busy||!s.canContinue" :title="s.canContinue?'从下一小时开始':'与当前模型版本不兼容'" @click="emit('resume',s)">状态续算</button></td></tr></tbody></table></div>
 <div v-if="selectedState" class="state-inspector"><h4>{{stateName||'完整末状态'}} · {{selectedState.state.validAt}}</h4><p class="muted">空间版本：{{selectedState.spatialVersion||'旧版未标识'}} · {{selectedState.state.unitStates.length}} 个单元</p><button @click="downloadState">导出完整末状态</button><div class="table-wrap"><table><thead><tr><th>单元</th><th v-for="v in stateColumns" :key="v.id">{{v.label}} {{v.unit}}</th></tr></thead><tbody><tr v-for="(u,i) in selectedState.state.unitStates" :key="i"><td>{{stateUnitLabel(i)}}</td><td v-for="(v,j) in u" :key="j">{{number(v,4)}}</td></tr></tbody></table></div></div>
 <p v-if="!library.length" class="empty">尚无用户计算保存的完整末状态</p><slot/>
 </template>
 </section>
</template>
