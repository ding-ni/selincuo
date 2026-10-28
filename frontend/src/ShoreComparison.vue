<script setup>
import {ref,computed,watch,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import DatumDiagnostics from './DatumDiagnostics.vue'
import {api,number} from './api.mjs'
import {lakeStations,compareShoreLevels} from './shore-comparison.mjs'
const props=defineProps({start:String,end:String,modelRows:{type:Array,default:()=>[]},context:{type:String,default:'monitor'}})
const start=ref(props.start||''),end=ref(props.end||''),mode=ref('raw'),evaluationScope=ref('matched'),responses=ref({}),catalog=ref([]),working=ref(false),error=ref(''),capability=ref(null),evaluationError=ref(''),analysisOpen=ref(false),analysisVisited=ref(false),snapshot=ref(null),stationErrors=ref({})
let controller,ticket=0
const data=computed(()=>Object.fromEntries(Object.entries(responses.value).map(([id,result])=>[id,result.rows||[]])))
const comparison=computed(()=>compareShoreLevels(data.value,props.modelRows,capability.value?.lakeInitializationDays||[]))
const rows=computed(()=>comparison.value[mode.value==='relative'?'relative':mode.value==='difference'?'difference':mode.value==='residual'?'residual':'rows'])
const hasModel=computed(()=>props.modelRows.length>0)
const evaluationReady=computed(()=>comparison.value.referenceStations.length>0&&capability.value?.id===props.context&&Array.isArray(capability.value?.lakeInitializationDays))
const evaluation=computed(()=>comparison.value[evaluationScope.value==='common'?'commonEvaluation':'evaluation'])
const series=computed(()=>[...lakeStations.filter(s=>mode.value!=='residual'||comparison.value.referenceStations.includes(s.id)).map(s=>({key:s.id,label:mode.value==='residual'?'模拟－'+s.name:mode.value==='difference'?(s.id==='SLC'?'主站零线':s.name+'－主站'):s.name,options:{showSymbol:true,showAllSymbol:true,symbolSize:3}})),...(hasModel.value&&(mode.value==='relative'||mode.value==='difference')?[{key:'model',label:mode.value==='difference'?'模拟－主站':'模拟涨落',options:{lineStyle:{type:'dashed',width:2},itemStyle:{color:'#43586e'}}}]:[])])
function stationStatus(id){
 if(stationErrors.value[id])return '读取失败'
 const result=responses.value[id];if(!result||!snapshot.value)return '待查询'
 const c=result.coverage||{},first=c.start?.slice(0,10),last=c.end?.slice(0,10)
 if(!first)return '无记录'
 if(snapshot.value.end<first)return '早于资料起点'
 if(snapshot.value.start>last)return '所选日期无记录'
 if(result.rows.some(r=>r.value!=null))return '有效日值'
 return result.rows.length?'日均资料不足':'所选日期无记录'
}
const shoreRange=computed(()=>{const v=catalog.value.filter(s=>s.id!=='SLC'&&lakeStations.some(l=>l.id===s.id)).flatMap(s=>s.variables.filter(v=>v.id==='level'));return v.length?{start:v.map(v=>v.start.slice(0,10)).sort()[0],end:v.map(v=>v.end.slice(0,10)).sort().at(-1)}:null})
async function query(){
 const n=++ticket;controller?.abort();controller=new AbortController();working.value=true;error.value=''
 try{
  if(!catalog.value.length)catalog.value=(await api('observation-catalog',null,{signal:controller.signal})).stations
  if(n!==ticket)return
  if(hasModel.value&&(capability.value?.id!==props.context||!Array.isArray(capability.value?.lakeInitializationDays))){
   try{capability.value=await api('result-capabilities?'+new URLSearchParams({resultId:props.context}),null,{signal:controller.signal});if(!Array.isArray(capability.value.lakeInitializationDays))throw Error('成果未提供初始化日目录');evaluationError.value=''}catch(e){if(e.name==='AbortError')throw e;evaluationError.value='无法核对成果初始化日，绝对湖位评价暂不可用：'+e.message}
  }
  if(n!==ticket)return
  if(!start.value||!end.value){start.value=shoreRange.value?.start||'';end.value=shoreRange.value?.end||''}
  if(!start.value||end.value<start.value)throw Error('请选择有效的湖位对照日期')
  const period={start:start.value,end:end.value}
  const result=await Promise.allSettled(lakeStations.map(async s=>api('observation-series?'+new URLSearchParams({station:s.id,variable:'level',...period,profile:'quality-v2'}),null,{signal:controller.signal})))
  if(n===ticket){
   const next={},failures={}
   result.forEach((r,i)=>{const id=lakeStations[i].id;if(r.status==='fulfilled')next[id]=r.value;else{next[id]={rows:[]};failures[id]=r.reason?.message||'读取失败'}})
   responses.value=next;stationErrors.value=failures;snapshot.value=period
   if(mode.value==='residual'&&!comparison.value.referenceStations.length)mode.value='raw'
  }
 }catch(e){if(n===ticket&&e.name!=='AbortError')error.value=e.message}finally{if(n===ticket)working.value=false}
}
function shorePeriod(){if(!shoreRange.value)return;start.value=shoreRange.value.start;end.value=shoreRange.value.end;query()}
function toggleAnalysis(event){analysisOpen.value=event.target.open;if(analysisOpen.value)analysisVisited.value=true}
function download(text,name){const url=URL.createObjectURL(new Blob(['\ufeff'+text],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function exportRows(){download('口径,'+({raw:'原始水位',difference:'与主站同日差',relative:'相对共同参考日涨落',residual:'模拟减实测绝对湖位残差'}[mode.value])+',参考日,'+(comparison.value.anchor||'无')+'\r\n基准状态,原始读数保留各站基准；绝对残差仅使用已登记的有效基准\r\n日期,'+series.value.map(s=>s.label+' (m)').join(',')+'\r\n'+rows.value.map(r=>r.date+','+series.value.map(s=>r[s.key]??'').join(',')).join('\r\n'),'色林错湖位多站对照.csv')}
function exportEvaluation(){download('成果,'+props.context+'\r\n评价口径,'+(evaluationScope.value==='common'?'五站共同有效日':'各站各自有效日')+'\r\n高程基准,按站点及日期采用已登记的实测基准关系\r\n剔除湖位初始化日,'+(capability.value?.lakeInitializationDays||[]).join('；')+'\r\n误差方向,模拟减实测\r\n站点,配对日,起日,止日,偏差m,MAEm,RMSEm\r\n'+evaluation.value.map(s=>[s.name,s.count,s.start||'',s.end||'',s.bias??'',s.mae??'',s.rmse??''].join(',')).join('\r\n'),'色林错多站绝对湖位评价.csv')}
watch(()=>[props.start,props.end,props.context],()=>{start.value=props.start||'';end.value=props.end||'';query()},{immediate:true})
onBeforeUnmount(()=>{ticket++;controller?.abort()})
</script>
<template><div class="shore-comparison" :aria-busy="working">
 <form class="result-toolbar" @submit.prevent="query"><label>对照起日<input type="date" v-model="start" required></label><label>对照止日<input type="date" v-model="end" required></label><button class="primary">查询对照</button><button type="button" :disabled="!shoreRange" @click="shorePeriod">四岸资料时段</button><button type="button" class="link" :disabled="working||!rows.length" @click="exportRows">导出对照 ↓</button></form>
 <p class="note">主站采用工作基准；四岸保留各站原基准。统计偏移分析单独查看。</p>
 <p v-if="snapshot" class="muted">当前显示 {{snapshot.start}} — {{snapshot.end}} 的整编日值。<span v-if="working">正在读取新时段，暂保留当前图。</span></p>
 <p v-for="(message,id) in stationErrors" :key="id" class="note warning">{{lakeStations.find(s=>s.id===id)?.name||id}}读取失败：{{message}}；其他站点不受影响。</p>
 <p v-if="hasModel" class="muted">实测取当前监测资料，模拟取所选成果；本次对照不会改变成果生成时保存的评价指标和观测资料。孤立有效日以点显示，缺测段不连线。</p>
 <p v-if="error" class="message error" role="alert">{{error}}</p>
 <p v-if="evaluationError" class="message error" role="alert">{{evaluationError}}</p>
 <nav class="workspace-tabs"><button v-for="v in [['relative','共同参考日涨落'],['raw','本站水位'],['difference','与主站同日差'],...(hasModel&&evaluationReady?[['residual','绝对湖位残差']]:[])]" :key="v[0]" :class="{active:mode===v[0]}" @click="mode=v[0]">{{v[1]}}</button></nav>
 <p v-if="mode==='raw'" class="note warning">各站保留原记录基准，四岸零点未核定的时段不用于绝对湖位评价。</p>
 <p v-if="mode==='relative'&&comparison.anchor" class="muted">共同参考日 {{comparison.anchor}}，各站减去自身该日值；共同有效 {{comparison.commonDays}} 日，仅比较涨落，不订正原始高程。<span v-if="hasModel&&!comparison.modelAtAnchor">模拟在参考日无绝对湖位，本视图保持空缺。</span></p>
 <p v-if="working&&!snapshot" class="note" role="status">正在读取各站湖位…</p><p v-if="mode==='relative'&&!comparison.anchor&&snapshot" class="empty">所选时段没有五站共同有效日，可切换到本站水位或与主站同日差。</p>
 <Chart v-else-if="snapshot" :rows="rows" :series="series" unit="m" :context="context+':shore:'+mode"/>
 <div class="table-wrap"><table><thead><tr><th>站点</th><th>资料状态</th><th>有效日</th><th>有效起止</th><th>与主站配对日</th><th>同日差中位数 m</th><th>同日差5%—95%分位 m</th></tr></thead><tbody><tr v-for="s in comparison.summary" :key="s.id"><td>{{s.name}}</td><td>{{stationStatus(s.id)}}</td><td>{{s.days}}</td><td>{{s.start||'—'}} — {{s.end||'—'}}</td><td>{{s.matchedDays}}</td><td>{{number(s.median,3)}}</td><td>{{number(s.p05,3)}} — {{number(s.p95,3)}}</td></tr></tbody></table></div>
 <p class="muted">差值方向为本站减主站，仅使用双方有效日；缺测不补零。分位范围描述配对日差值分布，不是置信区间。涨落对照不参与标准参数率定或主站评价。</p>
 <section v-if="hasModel&&evaluationReady" class="shore-absolute-evaluation"><div class="panel-title"><h3>绝对湖位评价</h3><button class="link" :disabled="working||!evaluation.some(s=>s.count)" @click="exportEvaluation">导出绝对评价 ↓</button></div><div class="result-toolbar"><label>配对口径<select v-model="evaluationScope" aria-label="多站评价配对口径"><option value="matched">各站各自有效日</option><option value="common">五站共同有效日</option></select></label><span v-if="evaluationScope==='common'">五站及模拟共同有效 {{comparison.commonModelDays}} 日</span></div><div class="table-wrap"><table><thead><tr><th>站点</th><th>配对日</th><th>评价起止</th><th>偏差 m</th><th>MAE m</th><th>RMSE m</th></tr></thead><tbody><tr v-for="s in evaluation" :key="s.id"><td>{{s.name}}</td><td>{{s.count}}</td><td>{{s.start||'—'}} — {{s.end||'—'}}</td><td>{{number(s.bias,3)}}</td><td>{{number(s.mae,3)}}</td><td>{{number(s.rmse,3)}}</td></tr></tbody></table></div><p class="muted">误差为模拟减实测，仅使用各站已核定基准的有效时段，剔除湖位初始化日。各站评价时段可能不同；五站共同有效日用于同样本比较。单一湖位模型不区分各岸局地水面过程。</p></section>
 <details @toggle="toggleAnalysis"><summary>四岸基准分析详情</summary><DatumDiagnostics v-if="analysisVisited" v-show="analysisOpen" :start="snapshot?.start" :end="snapshot?.end"/></details>
</div></template>
<style scoped>.shore-absolute-evaluation{border-top:1px solid var(--line);margin-top:26px;padding-top:24px}.shore-comparison>.table-wrap table{min-width:820px}.shore-comparison>.table-wrap td:nth-child(3){white-space:nowrap}</style>
