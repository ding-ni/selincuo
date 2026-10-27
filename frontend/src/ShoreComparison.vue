<script setup>
import {ref,computed,watch,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import DatumDiagnostics from './DatumDiagnostics.vue'
import {api,number} from './api.mjs'
import {lakeStations,compareShoreLevels,commonLakeDatum} from './shore-comparison.mjs'
const props=defineProps({start:String,end:String,modelRows:{type:Array,default:()=>[]},context:{type:String,default:'monitor'}})
const start=ref(props.start||''),end=ref(props.end||''),mode=ref('relative'),profile=ref('quality-v2'),evaluationScope=ref('common'),data=ref({}),catalog=ref([]),working=ref(false),error=ref(''),capability=ref(null),evaluationError=ref('')
let controller,ticket=0
const comparison=computed(()=>compareShoreLevels(data.value,props.modelRows,capability.value?.lakeInitializationDays||[]))
const rows=computed(()=>comparison.value[mode.value==='relative'?'relative':mode.value==='difference'?'difference':mode.value==='residual'?'residual':'rows'])
const hasModel=computed(()=>props.modelRows.length>0)
const datumConfirmed=computed(()=>commonLakeDatum(catalog.value))
const evaluationReady=computed(()=>datumConfirmed.value&&capability.value?.id===props.context&&Array.isArray(capability.value?.lakeInitializationDays))
const evaluation=computed(()=>comparison.value[evaluationScope.value==='common'?'commonEvaluation':'evaluation'])
const series=computed(()=>[...lakeStations.map(s=>({key:s.id,label:mode.value==='residual'?'模拟－'+s.name:mode.value==='difference'?(s.id==='SLC'?'主站零线':s.name+'－主站'):s.name,options:{showSymbol:true,showAllSymbol:true,symbolSize:3}})),...(hasModel.value&&mode.value!=='residual'&&(datumConfirmed.value||mode.value==='relative')?[{key:'model',label:mode.value==='difference'?'模拟－主站':mode.value==='relative'?'模拟涨落':'模拟湖位',options:{lineStyle:{type:'dashed',width:2},itemStyle:{color:'#43586e'}}}]:[])])
const shoreRange=computed(()=>{const v=catalog.value.filter(s=>s.id!=='SLC'&&lakeStations.some(l=>l.id===s.id)).flatMap(s=>s.variables.filter(v=>v.id==='level'));return v.length?{start:v.map(v=>v.start.slice(0,10)).sort()[0],end:v.map(v=>v.end.slice(0,10)).sort().at(-1)}:null})
async function query(){
 const n=++ticket;controller?.abort();controller=new AbortController();working.value=true;error.value='';data.value={}
 try{
  if(!catalog.value.length)catalog.value=(await api('observation-catalog',null,{signal:controller.signal})).stations
  if(n!==ticket)return
  if(hasModel.value&&(capability.value?.id!==props.context||!Array.isArray(capability.value?.lakeInitializationDays))){
   try{capability.value=await api('result-capabilities?'+new URLSearchParams({resultId:props.context}),null,{signal:controller.signal});if(!Array.isArray(capability.value.lakeInitializationDays))throw Error('成果未提供初始化日目录');evaluationError.value=''}catch(e){if(e.name==='AbortError')throw e;evaluationError.value='无法核对成果初始化日，绝对湖位评价暂不可用：'+e.message}
  }
  if(n!==ticket)return
  if(!start.value||!end.value){start.value=shoreRange.value?.start||'';end.value=shoreRange.value?.end||''}
  if(!start.value||end.value<start.value)throw Error('请选择有效的湖位对照日期')
  const result=await Promise.all(lakeStations.map(async s=>[s.id,(await api('observation-series?'+new URLSearchParams({station:s.id,variable:'level',start:start.value,end:end.value,profile:s.id==='SLC'&&profile.value==='candidate'?'quality-v2':profile.value}),null,{signal:controller.signal})).rows]))
  if(n===ticket)data.value=Object.fromEntries(result)
 }catch(e){if(n===ticket&&e.name!=='AbortError')error.value=e.message}finally{if(n===ticket)working.value=false}
}
function shorePeriod(){if(!shoreRange.value)return;start.value=shoreRange.value.start;end.value=shoreRange.value.end;query()}
function download(text,name){const url=URL.createObjectURL(new Blob(['\ufeff'+text],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function exportRows(){download('口径,'+({raw:'原始水位',difference:'与主站同日差',relative:'相对共同参考日涨落',residual:'模拟减实测绝对湖位残差'}[mode.value])+',参考日,'+(comparison.value.anchor||'无')+'\r\n基准状态,'+(datumConfirmed.value?'用户确认同基准且零点已统一':'待确认')+'\r\n日期,'+series.value.map(s=>s.label+' (m)').join(',')+'\r\n'+rows.value.map(r=>r.date+','+series.value.map(s=>r[s.key]??'').join(',')).join('\r\n'),'色林错湖位多站对照.csv')}
function exportEvaluation(){download('成果,'+props.context+'\r\n评价口径,'+(evaluationScope.value==='common'?'五站共同有效日':'各站各自有效日')+'\r\n高程基准,用户确认同基准且零点已统一\r\n剔除湖位初始化日,'+(capability.value?.lakeInitializationDays||[]).join('；')+'\r\n误差方向,模拟减实测（未平移未订正）\r\n站点,配对日,起日,止日,偏差m,MAEm,RMSEm\r\n'+evaluation.value.map(s=>[s.name,s.count,s.start||'',s.end||'',s.bias??'',s.mae??'',s.rmse??''].join(',')).join('\r\n'),'色林错多站绝对湖位评价.csv')}
watch(()=>[props.start,props.end,props.context],()=>{start.value=props.start||'';end.value=props.end||'';query()},{immediate:true})
onBeforeUnmount(()=>{ticket++;controller?.abort()})
</script>
<template><div class="shore-comparison" :aria-busy="working">
 <form class="result-toolbar" @submit.prevent="query"><label>对照起日<input type="date" v-model="start" required></label><label>对照止日<input type="date" v-model="end" required></label><button class="primary">查询对照</button><button type="button" :disabled="!shoreRange" @click="shorePeriod">四岸资料时段</button><button type="button" class="link" :disabled="working||!rows.length" @click="exportRows">导出对照 ↓</button></form>
 <label>日值类型<select v-model="profile" @change="query"><option value="quality-v2">整编日值</option><option value="candidate">统计偏移分析</option></select></label><p class="note">主站采用工作基准；四岸基准待核。各站保留原值，相对涨落用于比较变化。</p>
 <p v-if="hasModel" class="muted">实测取当前资料目录，模拟取所选成果；该对照不重算冻结的评价指标与观测快照。点标记保留孤立有效日，缺测段不连线。</p>
 <p v-if="error" class="message error" role="alert">{{error}}</p>
 <p v-if="evaluationError" class="message error" role="alert">{{evaluationError}}</p>
 <nav class="workspace-tabs"><button v-for="v in [['relative','共同参考日涨落'],['raw','本站水位'],['difference','与主站同日差'],...(hasModel&&datumConfirmed?[['residual','绝对湖位残差']]:[])]" :key="v[0]" :class="{active:mode===v[0]}" @click="mode=v[0]">{{v[1]}}</button></nav>
 <p v-if="mode==='raw'&&!datumConfirmed" class="note warning">各站零点尚未统一，此图展示各站记录，不表示同一高程基准。</p>
 <p v-if="mode==='relative'&&comparison.anchor" class="muted">共同参考日 {{comparison.anchor}}，各站减去自身该日值；共同有效 {{comparison.commonDays}} 日，仅比较涨落，不订正原始高程。<span v-if="hasModel&&!comparison.modelAtAnchor">模拟在参考日无绝对湖位，本视图保持空缺。</span></p>
 <div v-if="working" class="empty">正在读取五站湖位…</div><div v-else-if="mode==='relative'&&!comparison.anchor" class="empty">所选时段没有五站共同有效日，不能建立共同参考日。</div>
 <Chart v-else :rows="rows" :series="series" unit="m" :context="context+':shore:'+mode"/>
 <div class="table-wrap"><table><thead><tr><th>站点</th><th>有效日</th><th>有效起止</th><th>与主站配对日</th><th>同日差中位数 m</th><th>同日差5%—95%分位 m</th></tr></thead><tbody><tr v-for="s in comparison.summary" :key="s.id"><td>{{s.name}}</td><td>{{s.days}}</td><td>{{s.start||'—'}} — {{s.end||'—'}}</td><td>{{s.matchedDays}}</td><td>{{number(s.median,3)}}</td><td>{{number(s.p05,3)}} — {{number(s.p95,3)}}</td></tr></tbody></table></div>
 <p class="muted">差值方向为本站减主站，仅使用双方有效日；缺测不补零。分位范围描述配对日差值分布，不是置信区间。涨落对照不参与标准参数率定或主站评价。</p>
 <section v-if="hasModel&&evaluationReady" class="shore-absolute-evaluation"><div class="panel-title"><h3>多站绝对湖位评价</h3><button class="link" :disabled="working||!evaluation.some(s=>s.count)" @click="exportEvaluation">导出绝对评价 ↓</button></div><div class="result-toolbar"><label>配对口径<select v-model="evaluationScope" aria-label="多站评价配对口径"><option value="common">五站共同有效日</option><option value="matched">各站各自有效日</option></select></label><span v-if="evaluationScope==='common'">五站及模拟共同有效 {{comparison.commonModelDays}} 日</span></div><div class="table-wrap"><table><thead><tr><th>站点</th><th>配对日</th><th>评价起止</th><th>偏差 m</th><th>MAE m</th><th>RMSE m</th></tr></thead><tbody><tr v-for="s in evaluation" :key="s.id"><td>{{s.name}}</td><td>{{s.count}}</td><td>{{s.start||'—'}} — {{s.end||'—'}}</td><td>{{number(s.bias,3)}}</td><td>{{number(s.mae,3)}}</td><td>{{number(s.rmse,3)}}</td></tr></tbody></table></div><p class="muted">误差为模拟减实测，未平移或订正，剔除湖位初始化日。五站共同有效日用于同样本比较；各站有效日口径的时段可能不同。此为对所选成果的补充核验，保留冻结主站评分与2024搜索/2025评价边界。单一湖位模型不区分各岸局地水面过程。</p></section>
 <details><summary>四岸基准分析详情</summary><DatumDiagnostics/></details>
</div></template>
<style scoped>.shore-absolute-evaluation{border-top:1px solid var(--line);margin-top:26px;padding-top:24px}.shore-comparison>.table-wrap table{min-width:820px}.shore-comparison>.table-wrap td:nth-child(3){white-space:nowrap}</style>
