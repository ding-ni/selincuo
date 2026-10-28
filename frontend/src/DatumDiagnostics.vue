<script setup>
import {ref,onMounted,onBeforeUnmount} from 'vue'
import Chart from './Chart.vue'
import {api,number} from './api.mjs'
const props=defineProps({start:String,end:String})
const data=ref(null),error=ref(''),working=ref(false),detail=ref(null),names={'SLC-N':'北岸','SLC-E':'东岸','SLC-S':'南岸','SLC-W':'西岸'}
let controller,ticket=0
const analysisController=new AbortController()
onMounted(async()=>{try{data.value=await api('datum-candidates',null,{signal:analysisController.signal})}catch(e){if(e.name!=='AbortError')error.value=e.message}})
async function showAdjusted(candidate){
 const n=++ticket;controller?.abort();controller=new AbortController();working.value=true;error.value=''
 const period={start:props.start||'',end:props.end||''}
 try{
  const result=await api('observation-series?'+new URLSearchParams({station:candidate.station,variable:'level',...period,profile:'candidate'}),null,{signal:controller.signal})
  if(n===ticket)detail.value={station:candidate.station,period,rows:result.rows,message:result.message}
 }catch(e){if(n===ticket&&e.name!=='AbortError')error.value=e.message}finally{if(n===ticket)working.value=false}
}
onBeforeUnmount(()=>{ticket++;controller?.abort();analysisController.abort()})
</script>
<template><section class="datum-diagnostics">
 <h4>四岸与主站差值分析</h4><p class="note">统计偏移只用于对照，不能代替水准测量，也不自动改变观测或模型输入。</p>
 <p v-if="error" class="message error" role="alert">{{error}}</p>
 <p v-if="!data&&!error" class="muted">正在分析同期记录…</p>
 <div class="table-wrap" v-if="data"><table><thead><tr><th>站点</th><th>估计期配对日</th><th>本站－主站 m</th><th>分析状态</th><th>分析详情</th></tr></thead><tbody>
  <tr v-for="c in data.candidates" :key="c.station"><td>{{names[c.station]}}</td><td>{{c.trainingDays}}</td><td>{{number(c.offsetM,3)}}</td><td>{{c.reason||'配对样本不足'}}</td><td><button v-if="c.correctionM!=null" :disabled="working" @click="showAdjusted(c)">查看平移对照</button><details><summary>估计、检查时段与异常</summary><p>估计 {{c.estimationPeriod?.start}} 至 {{c.estimationPeriod?.endExclusive}} 前；检查自 {{c.validationPeriod?.start}} 起。</p><p>配对 {{c.matchedPoints}} 次；接受合格及限定合格记录。</p><p v-for="m in c.validation" :key="m.month">{{m.month}} · {{m.days}}日 · 残差 {{number(m.medianResidualM,3)}} m</p><p v-if="c.withinDayDiagnostics">日内差值异常候选 {{c.withinDayDiagnostics.flaggedPoints}} 点，仅提示核查，未自动剔除。</p></details></td></tr>
 </tbody></table></div>
 <p class="muted">同期匹配容差15分钟，每条采样最多匹配一次，按日等权汇总；分析时段由资料规则决定，不随图表缩放改变。</p>
 <section v-if="detail"><h4>{{names[detail.station]}}统计平移对照</h4><p class="muted">{{detail.period.start}} — {{detail.period.end}}；原始日值保留，平移线仅作统计比较。</p><p v-if="detail.message" class="note">{{detail.message}}</p><Chart :rows="detail.rows" :series="[{key:'value',label:'本站日值'},{key:'adjustedValue',label:'统计平移对照'}]" unit="m" :context="'datum:'+detail.station"/></section>
 </section></template>
<style scoped>.datum-diagnostics{border-top:1px solid var(--line);margin-top:26px;padding-top:16px}.datum-diagnostics details{margin:4px 0}.datum-diagnostics table{min-width:750px}</style>
