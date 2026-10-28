<script setup>
import {ref,computed,watch,onBeforeUnmount} from 'vue'
import {api,number} from './api.mjs'
import Chart from './Chart.vue'
import {createTraceLoader} from './calibration-trace-loader.mjs'
const props=defineProps({task:Object,definitions:{type:Array,default:()=>[]}}),trace=ref([]),search=ref(null),error=ref(''),loading=ref(false)
let timer
const rows=computed(()=>trace.value.map((r,i)=>({date:String(i+1),[r.stage]:r.bestObjective})))
const stageName=value=>({runoff:'产流',snow:'雪冰',routing:'汇流',lake:'湖位',integrated:'综合'})[value]||value
const parameterName=key=>props.definitions.find(d=>d.key===key)?.label||key
const stopReason=value=>({'budget-exhausted':'达到设定次数，未判断收敛','converged':'达到率定停止条件','constraint-proposal-limit':'参数范围限制，已停止','proposal-limit':'参数范围限制，已停止','no-evaluable-target':'本期没有可评价目标，保留原参数','no-free-parameters':'没有可调整参数，保留原参数','no-responsive-parameters':'所选参数均无可搜索的局部响应','starts-completed':'各搜索起点已完成，未判断收敛','no-budget':'本起点没有分配评价次数'})[value]||value
const calibration=computed(()=>props.task?.result?.calibration),target=computed(()=>props.task?.result?.calibrationWorkflow?.target||props.task?.request?.target||'duja')
const evaluationCount=computed(()=>Number.isFinite(calibration.value?.evaluations)?calibration.value.evaluations:Number.isFinite(search.value?.count)?search.value.count:null)
const targets=computed(()=>calibration.value?.targets||search.value?.targets||{}),assessments=computed(()=>calibration.value?.assessments||{})
const stages=computed(()=>calibration.value?.stages||search.value?.stages||[])
const statusName=value=>({'responsive':'有局部响应','no-local-response':'该扰动无局部响应','not-tested':'未测试','constraint-blocked':'受约束未测','not-evaluable':'无法评价'})[value]||value||'未说明'
const years=value=>Object.entries(value||{}).map(([year,count])=>`${year}年 ${count}日`).join('、')||'无'
const loader=createTraceLoader({getTaskId:()=>props.task?.id,getOffset:()=>trace.value.length,
 fetchPage:(id,after)=>api('search-progress?'+new URLSearchParams({taskId:id,after})),
 onStart:()=>{loading.value=true;error.value=''},onSuccess:r=>{trace.value.push(...r.trace);search.value=r},
 onFailure:e=>{error.value=e.message||'请求失败'},onFinish:()=>{loading.value=false}})
watch(()=>props.task?.id,()=>{loader.reset();trace.value=[];search.value=null;error.value='';loading.value=false;loader.load();clearInterval(timer);timer=setInterval(()=>{if(['queued','running','stopping'].includes(props.task?.status))loader.load()},2500)},{immediate:true})
watch(()=>props.task?.status,()=>loader.load())
onBeforeUnmount(()=>{loader.reset();clearInterval(timer)})
</script>
<template><section v-if="task" class="panel" :data-trace-count="trace.length"><div class="panel-title"><h3>率定期评价</h3><span>{{evaluationCount==null?'评价次数待获取':'已评价 '+evaluationCount+' 次'}}</span></div>
 <p v-if="loading" class="muted" role="status">正在读取率定过程…</p><p v-if="error" class="message error" role="alert">率定过程读取失败：{{error}} <button @click="loader.load()">重试</button></p>
 <Chart v-if="trace.length" :rows="rows" :series="[{key:'integrated',label:'综合目标'},{key:'runoff',label:'产流目标'},{key:'snow',label:'雪冰目标'},{key:'routing',label:'汇流目标'},{key:'lake',label:'湖位RMSE'}]" unit="目标值" :context="task.id+':search'"/>
 <p class="muted">检验期未参与本次率定。2025—2026年资料曾用于平台开发与方案研究，涉及该时期的检验不是完全未触碰的独立验证。停止后保留评价记录；达到设定次数不代表参数已收敛。</p>
 <template v-if="Object.keys(targets).length||Object.keys(assessments).length"><p class="note">{{target==='duja'?'杜加流量参与参数排序；湖位仅作评价，不参与本次参数排序。':target==='integrated'?'流量与湖位均参与综合目标；各自使用率定期有效日期和固定尺度。':'主站湖位参与本次率定。'}} 雪冰参数仅受流量条件约束，不能替代独立雪冰观测。</p>
  <div v-if="Object.keys(targets).length" class="table-wrap"><table><thead><tr><th>观测</th><th>有效日</th><th>年度覆盖</th><th>用途</th></tr></thead><tbody><tr v-for="(name,key) in {flow:'杜加流量',lake:'主站湖位'}" :key="key"><td>{{name}}</td><td>{{targets[key]?.count??'—'}}</td><td>{{years(targets[key]?.countByYear)}}</td><td>{{targets[key]?.available?(targets[key]?.usedForObjective===false?'仅作评价':'参与率定'):targets[key]?.reason||'等待检查'}}</td></tr></tbody></table></div>
  <div v-if="Object.keys(assessments).length" class="table-wrap"><table><thead><tr><th>时段</th><th>流量有效日</th><th>流量 NSE / 水量偏差 %</th><th>湖位有效日</th><th>湖位 RMSE / 平均偏差 m</th><th>湖位首末误差变化 m</th></tr></thead><tbody><tr v-for="(label,key) in {training:'率定期',validation:'检验期'}" :key="key"><td>{{label}}</td><td>{{assessments[key]?.flow?.count??0}}</td><td>{{number(assessments[key]?.flow?.nse)}} / {{number(assessments[key]?.flow?.pbias)}}</td><td>{{assessments[key]?.lake?.count??0}}</td><td>{{number(assessments[key]?.lake?.rmse,3)}} / {{number(assessments[key]?.lake?.bias,3)}}</td><td>{{number(assessments[key]?.lake?.firstToLastErrorChangeM,3)}}</td></tr></tbody></table></div>
 </template>
 <div class="table-wrap"><table><thead><tr><th>阶段</th><th>评价次数 / 上限</th><th>最优目标值</th><th>达到 / 接近边界</th><th>停止原因</th></tr></thead><tbody><tr v-for="s in stages" :key="s.stage"><td>{{stageName(s.stage)}}</td><td>{{s.evaluations}} / {{s.maxEvaluations}}</td><td>{{number(s.bestObjective,5)}}</td><td>达到：{{(s.boundaryParameters||[]).map(parameterName).join('、')||'无'}}<br>接近：{{(s.nearBoundaryParameters||[]).map(parameterName).join('、')||'无'}}</td><td>{{stopReason(s.termination)}}</td></tr></tbody></table></div>
 <details v-for="s in stages" :key="'details-'+s.stage"><summary>{{stageName(s.stage)}}：参数变化、局部响应与搜索起点</summary>
  <div class="table-wrap"><table><thead><tr><th>参数</th><th>初值</th><th>结果值</th><th>搜索范围</th><th>训练期局部响应</th></tr></thead><tbody><tr v-for="key in s.selectedParameters||s.keys||[]" :key="key"><td>{{parameterName(key)}}</td><td>{{number(s.initialParameters?.[key],6)}}</td><td>{{number(s.bestParameters?.[key],6)}}</td><td>{{number(s.bounds?.[key]?.[0],6)}} — {{number(s.bounds?.[key]?.[1],6)}}</td><td>{{statusName(s.diagnostics?.parameters?.[key]?.status)}}<small v-if="s.diagnostics?.parameters?.[key]?.reason"> · {{s.diagnostics.parameters[key].reason}}</small></td></tr></tbody></table></div>
  <p v-if="s.diagnostics?.svd" class="note">局部响应矩阵秩 {{s.diagnostics.svd.rank??'—'}}；{{s.diagnostics.svd.interpretation||'仅反映该初值附近的条件响应，不表示全局可辨识。'}}</p>
  <div v-if="s.starts?.length" class="table-wrap"><table><thead><tr><th>起点种子</th><th>评价次数 / 分配上限</th><th>最优目标</th><th>停止原因</th></tr></thead><tbody><tr v-for="start in s.starts" :key="start.seed"><td>{{start.seed}}</td><td>{{start.evaluations}} / {{start.maxEvaluations}}</td><td>{{number(start.bestObjective,5)}}</td><td>{{stopReason(start.termination)}}</td></tr></tbody></table></div>
 </details>
</section></template>
