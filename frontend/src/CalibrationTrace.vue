<script setup>
import {ref,computed,watch,onBeforeUnmount} from 'vue'
import {api,number} from './api.mjs'
import Chart from './Chart.vue'
const props=defineProps({task:Object}),trace=ref([]),search=ref(null),error=ref('')
let timer,ticket=0
const rows=computed(()=>trace.value.map((r,i)=>({date:String(i+1),[r.stage]:r.bestObjective})))
async function load(){const id=props.task?.id;if(!id)return;const n=ticket;try{const r=await api('search-progress?'+new URLSearchParams({taskId:id,after:trace.value.length}));if(n!==ticket)return;trace.value.push(...r.trace);search.value=r}catch(e){error.value=e.message}}
watch(()=>props.task?.id,()=>{ticket++;trace.value=[];search.value=null;load();clearInterval(timer);timer=setInterval(()=>{if(['queued','running','stopping'].includes(props.task?.status))load()},2500)},{immediate:true})
watch(()=>props.task?.status,load)
onBeforeUnmount(()=>{ticket++;clearInterval(timer)})
</script>
<template><section v-if="trace.length" class="panel"><div class="panel-title"><h3>训练期实时最优目标</h3><span>已评价 {{trace.length}} 次</span></div><Chart :rows="rows" :series="[{key:'runoff',label:'产流目标'},{key:'routing',label:'汇流目标'},{key:'lake',label:'湖位RMSE'}]" unit="目标值" :context="task.id+':search'"/><p class="muted">评价期未参与搜索。停止后保留轨迹；预算耗尽不代表收敛。</p><div class="table-wrap"><table><thead><tr><th>阶段</th><th>次数 / 预算</th><th>初始指标（NSE或RMSE）</th><th>最佳目标</th><th>触边参数</th><th>停止原因</th></tr></thead><tbody><tr v-for="s in task?.result?.calibration?.stages||search?.stages||[]" :key="s.stage"><td>{{s.stage}}</td><td>{{s.evaluations}} / {{s.maxEvaluations}}</td><td>{{number(s.baselineMetrics?.nse??s.baselineMetrics?.rmse,4)}}</td><td>{{number(s.bestObjective,5)}}</td><td>{{s.boundaryParameters.join('、')||'无'}}</td><td>{{s.termination==='budget-exhausted'?'预算耗尽，未判收敛':s.termination==='converged'?'达到搜索停止条件':s.termination}}</td></tr></tbody></table></div></section></template>
