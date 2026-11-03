<script setup>
import {ref,computed,watch} from 'vue'
import Chart from './Chart.vue'
import ShoreComparison from './ShoreComparison.vue'
import {downloadReport} from './result-report.mjs'
import {api,number,resultName} from './api.mjs'
import {displayCoverageDates,displayAnnualMetrics,calculationProcess} from './result-catalog.mjs'
const props=defineProps({detail:Object,busy:Boolean,viewWindow:Object})
const emit=defineEmits(['stop','state','retry','results','export'])
const summary=ref(null),summaryError=ref('');let summaryTicket=0
const summaryCoverage=computed(()=>displayCoverageDates(summary.value?.displayCoverage))
const summaryAnnual=computed(()=>displayAnnualMetrics(summary.value?.evaluation?.annual,summary.value?.displayCoverage))
watch(()=>[props.detail?.id,props.detail?.status],async ([id,status])=>{const ticket=++summaryTicket;summary.value=null;summaryError.value='';if(!id||status!=='succeeded')return;try{const data=await api('result-summary?resultId='+encodeURIComponent(id));if(ticket===summaryTicket)summary.value=data}catch(e){if(ticket===summaryTicket)summaryError.value=e.message}},{immediate:true})
const tab=ref('lake'),year=ref(''),start=ref(''),end=ref('')
const result=computed(()=>props.detail?.result),all=computed(()=>{
 const coverage=result.value?.displayCoverage,start=coverage?.start?.slice(0,10),end=coverage?.endExclusive?.slice(0,10)
 return (result.value?.daily||[]).filter(r=>(!start||r.date.slice(0,10)>=start)&&(!end||r.date.slice(0,10)<end))
})
const process=computed(()=>calculationProcess(props.detail))
const totalLabel=computed(()=>result.value?.inflowComplete===false?'已判定入湖合计':result.value?.modelFamily==='grid-continuous-v1'?'入湖流量':'历史口径入湖合计')
const fringeLabel=computed(()=>result.value?.modelFamily==='grid-continuous-v1'?'湖周直接来水':'残余集水区（旧版）')
const years=computed(()=>[...Object.keys(result.value?.periodEvaluation?.periods||{}),...Object.keys(result.value?.metrics||{})]),metric=computed(()=>result.value?.periodEvaluation?.periods?.[year.value]||result.value?.metrics?.[year.value])
const periodLabel=y=>result.value?.periodEvaluation?.periods?.[y]?.label||y+'年'
const rows=computed(()=>all.value.filter(r=>(!start.value||r.date.slice(0,10)>=start.value)&&(!end.value||r.date.slice(0,10)<=end.value)))
const running=computed(()=>['queued','running','stopping'].includes(props.detail?.status))
const href=f=>props.detail.builtin?'/api/model/published-result?'+new URLSearchParams({id:props.detail.id,file:f}):'/api/model/export?'+new URLSearchParams({taskId:props.detail.id,format:f})
function whole(){start.value=all.value[0]?.date.slice(0,10)||'';end.value=all.value.at(-1)?.date.slice(0,10)||''}
watch(()=>[props.detail?.id,props.viewWindow],()=>{tab.value='lake';whole();if(props.viewWindow){start.value=props.viewWindow.start;end.value=props.viewWindow.end}year.value=years.value[0]||''},{immediate:true})
watch(()=>all.value.length,()=>{if(!start.value)whole();if(!year.value)year.value=years.value[0]||''})
</script>
<template>
 <section v-if="!detail" class="panel empty">选择时段开始模拟，或查看已有成果。</section>
 <section v-else class="panel result-view">
 <div class="panel-title"><h3>{{resultName(detail)}}</h3><span v-if="summary?.evaluation?.label" class="badge">{{summary.evaluation.label}}</span></div>
 <div v-if="!result?.daily" class="task-progress"><b>{{detail.message||'准备计算'}}</b><progress :value="detail.progress" max="100"></progress><span>{{number(detail.progress,1)}}%</span><button v-if="running" :disabled="detail.status==='stopping'" @click="emit('stop',detail.id)">停止计算</button><button v-else @click="emit('retry')">重新计算</button></div>
 <template v-else>
 <div v-if="summary" class="result-evidence"><div class="evidence-heading"><b>模拟评价</b><span>{{summary.unitCount}} 个计算单元 · {{summaryCoverage.start}} — {{summaryCoverage.end}}</span></div><div class="table-wrap"><table><thead><tr><th>年份</th><th>湖位 RMSE / m</th><th>湖位偏差 / m</th><th>湖位有效日</th><th>杜加 NSE</th><th>流量偏差 / %</th><th>流量有效日</th></tr></thead><tbody><tr v-for="(m,y) in summaryAnnual" :key="y"><td>{{y}}</td><td>{{number(m.lake?.rmse,4)}}</td><td>{{number(m.lake?.bias,4)}}</td><td>{{m.lake?.count||0}}</td><td>{{number(m.flow?.nse,4)}}</td><td>{{number(m.flow?.pbias,2)}}</td><td>{{m.flow?.count||'无合格评价资料'}}</td></tr></tbody></table></div><p class="note">{{summary.evaluation.mode==='window'?'该历史成果按观测窗口初始化，指标不能作为连续模拟精度。':summary.evaluation.mode==='continuous'?'按固定锚点连续计算；评价仅覆盖有效观测日期。':'评价口径待核实。'}}</p><details v-if="summary.limitations?.length"><summary>资料与计算说明</summary><p v-for="item in summary.limitations" :key="item">{{item}}</p></details></div><p v-if="summaryError" class="note warning">评价摘要读取失败：{{summaryError}}</p>
 <p v-if="result.inflowComplete===false&&!result.drainage?.calculationDomainComplete" class="note">入湖合计采用四河及已判定湖周范围；其余 {{number(result.drainage?.unresolvedAreaKm2,1)}} km² 单列记账，去向依据见资料与计算说明。</p>
 <div class="result-toolbar"><label>开始日期<input type="date" v-model="start"></label><label>结束日期<input type="date" v-model="end"></label><button @click="whole">完整时段</button><a class="button" :href="href('daily.csv')">导出日过程</a><button :disabled="!summary" @click="downloadReport(summary,result)">成果摘要报告</button><button @click="emit('state')">查看模型状态</button></div>
 <p v-if="viewWindow" class="note">过程图显示所选时段；年度评价仍按成果记录的评价时段统计。</p>
 <nav class="workspace-tabs"><button v-for="t in [['lake','湖位过程'],['flow','入湖径流'],['balance','水量收支'],['evaluation','模拟评价'],['shores','四岸核对']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <ShoreComparison v-if="tab==='shores'" :start="start" :end="end" :model-rows="all" :context="detail.id"/>
 <Chart v-if="tab==='lake'" :rows="rows" :series="[{key:'lakeLevel',label:'模拟湖位'},{key:'observedLevel',label:'主站实测'}]" unit="m" :context="detail.id+':lake'"/>
 <template v-if="tab==='flow'"><Chart :rows="rows" :series="[{key:'total',label:totalLabel},{key:'duja',label:'杜加模拟'},{key:'observedFlow',label:'杜加实测'}]" unit="m³/s" :context="detail.id+':flow'"/><Chart :rows="rows" :series="[{key:'zajia',label:'扎加藏布'},{key:'zagen',label:'扎根藏布'},{key:'boqu',label:'波曲藏布'},{key:'ali',label:'阿里藏布'},{key:'fringe',label:fringeLabel}]" unit="m³/s" :context="detail.id+':rivers'"/></template>
 <p v-if="tab==='balance'&&summary?.evaluation.mode==='window'" class="note warning">旧成果湖泊收支按观测窗口活动小时记账，窗口外缺项不代表无来水。这些存档量不能直接与连续模型全年水量相减或用于全年闭合判断。</p>
 <Chart v-if="tab==='balance'" :rows="rows" :series="[{key:'lakeInflowM3',label:'入湖水量',type:'bar'},{key:'lakePrecipitationM3',label:'湖面降水',type:'bar'},{key:'lakeEvaporationM3',label:'湖面蒸发',type:'bar'},{key:'lakeDeltaVolumeM3',label:'净蓄水变化'}]" unit="m³/d" :context="detail.id+':balance'"/>
 <template v-if="tab==='evaluation'">
  <div class="result-toolbar"><label>评价时段<select v-model="year"><option v-for="y in years" :key="y" :value="y">{{periodLabel(y)}}</option></select></label><span v-if="metric?.start">{{metric.start.slice(0,10)}} — {{metric.endExclusive.slice(0,10)}}（止日不含）</span></div>
  <div class="metric-grid"><div><span>湖位 RMSE</span><b>{{number(metric?.lake?.rmse,3)}} <small>m</small></b></div><div><span>湖位 MAE</span><b>{{number(metric?.lake?.mae,3)}} <small>m</small></b></div><div><span>杜加流量 NSE</span><b>{{number(metric?.flow?.nse,3)}}</b></div><div><span>径流量偏差</span><b>{{number(metric?.flow?.pbias,1)}} <small>%</small></b></div></div>
  <p class="muted">配对日数：湖位 {{metric?.lake?.count||0}} 日，流量 {{metric?.flow?.count||0}} 日。过程日期筛选与本处评价选择独立。</p>
 </template>
 <details class="technical-details calculation-process"><summary>计算过程与参数记录</summary>
  <dl><dt>模型</dt><dd>{{result.modelFamily==='grid-continuous-v1'?'格网连续新安江与分区汇流':result.modelFamily||'模型版本见计算身份'}}</dd>
   <dt>模拟时段</dt><dd>{{result.start?.slice(0,10)}} — {{result.end?.slice(0,10)}}</dd>
   <dt>预热起点</dt><dd>{{detail.request?.periods?.warmupStart?.slice(0,10)||detail.request?.start?.slice(0,10)||'记录未单列'}}</dd>
   <dt>驱动与空间</dt><dd>{{result.inputId}} · {{result.spatialVersion}}</dd>
   <dt>搜索起点</dt><dd>{{process.seeds.join('、')||'无'}}</dd>
   <dt>实际评价次数</dt><dd>{{number(process.evaluations,0)}} · {{process.converged?'已收敛':process.stages.some(stage=>stage.termination==='budget-exhausted')?'预算用尽':'未提供收敛记录'}}</dd>
  </dl>
  <div v-if="process.stages.length" class="table-wrap"><table><thead><tr><th>阶段</th><th>实际评价</th><th>状态</th></tr></thead><tbody><tr v-for="stage in process.stages" :key="stage.stage"><td>{{stage.stage}}</td><td>{{number(stage.evaluations,0)}}</td><td>{{stage.converged?'已收敛':stage.termination==='budget-exhausted'?'预算用尽':'已完成'}}</td></tr></tbody></table></div>
  <div v-if="process.parameters.length" class="table-wrap"><table><thead><tr><th>参数</th><th>使用方式</th><th>初值</th><th>搜索范围</th><th>最终值</th></tr></thead><tbody><tr v-for="row in process.parameters" :key="row.key"><td>{{row.key}}</td><td>{{({flow:'水文率定',lake:'湖泊率定',fixed:'固定',unknown:'未记录'})[row.phase]}}</td><td>{{number(row.initial,6)}}</td><td>{{['flow','lake'].includes(row.phase)?number(row.bounds[0],6)+' — '+number(row.bounds[1],6):'—'}}</td><td>{{number(row.value,6)}}</td></tr></tbody></table></div>
 </details>
 <details class="technical-details"><summary>成果文件</summary><dl><dt>方案</dt><dd>{{result.scheme?.name||'用户计算'}} · {{result.scheme?.revision||'—'}}</dd><dt>成果编号</dt><dd>{{detail.id}}</dd></dl><div class="actions"><a class="button" :href="href(detail.builtin?'final-state.json':'checkpoint.json')">导出末状态</a><button @click="emit('export',detail.id,'hourly.npz')">后台导出逐时数据</button></div></details>
 </template></section>
</template>
