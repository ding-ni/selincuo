<script setup>
import {ref,computed,watch,onMounted} from 'vue'
import {api,number,resultName} from './api.mjs'
const props=defineProps({records:Array,tasks:Array,releaseId:String,defaultId:String,busy:Boolean})
const emit=defineEmits(['submit'])
const baseline=ref(props.defaultId||''),candidate=ref(''),archives=ref([]),selected=ref(null),error=ref(''),loading=ref(false)
const choices=computed(()=>(props.records||[]).filter(t=>t.status==='succeeded'&&!t.trashed))
const label=id=>{const record=choices.value.find(t=>t.id===id);return record?resultName(record):id}
const canSubmit=computed(()=>props.releaseId&&baseline.value&&candidate.value&&baseline.value!==candidate.value&&!props.busy)
const pending=computed(()=>(props.tasks||[]).filter(t=>t.kind==='evaluation'&&['queued','running','stopping'].includes(t.status)))
let ticket=0
async function refresh(){try{archives.value=(await api('evaluations')).evaluations}catch(e){error.value='评价归档读取失败：'+e.message}}
async function open(id){const current=++ticket;loading.value=true;error.value='';try{const value=await api('evaluation?id='+encodeURIComponent(id));if(current===ticket)selected.value=value}catch(e){if(current===ticket)error.value=e.message}finally{if(current===ticket)loading.value=false}}
function submit(){if(canSubmit.value)emit('submit',{kind:'evaluation',name:'共同日期评价 · '+label(candidate.value),clientRequestId:crypto.randomUUID(),protocolId:'selinco-admission-v1',baselineReleaseId:props.releaseId,baselineResultId:baseline.value,candidateResultId:candidate.value})}
watch(()=>props.defaultId,id=>{if(!baseline.value)baseline.value=id})
watch(()=>(props.tasks||[]).filter(t=>t.kind==='evaluation').map(t=>t.id+':'+t.status).join('|'),refresh)
onMounted(refresh)
</script>
<template>
 <section class="panel evaluation-archive" aria-label="共同日期评价归档">
  <div class="panel-title"><h3>共同日期评价归档</h3><button :disabled="loading" @click="refresh">刷新归档</button></div>
  <p class="muted">用两套完整成果的共同有效日期重新计算。窗口与连续口径、观测冲突和缺少诊断会在报告中保留；归档不会改变默认方案。</p>
  <div class="result-toolbar">
   <label class="grow">比较基准<select v-model="baseline"><option value="">选择基准成果</option><option v-for="t in choices" :key="t.id" :value="t.id">{{resultName(t)}}{{t.id===defaultId?' · 当前默认':''}}</option></select></label>
   <label class="grow">研究候选<select v-model="candidate"><option value="">选择候选成果</option><option v-for="t in choices.filter(t=>t.id!==baseline)" :key="t.id" :value="t.id">{{resultName(t)}}</option></select></label>
   <button class="primary" :disabled="!canSubmit" @click="submit">提交共同日期评价</button>
  </div>
  <p v-if="pending.length" role="status">{{pending.length}} 项评价正在后台处理，可在任务中心查看进度。</p>
  <p v-if="error" class="note warning" role="alert">{{error}}</p>
  <div v-if="archives.length" class="table-wrap"><table><thead><tr><th>候选 / 基准</th><th>共同评价</th><th>发布检查</th><th>归档时间</th><th></th></tr></thead><tbody><tr v-for="record in archives" :key="record.id"><td>{{label(record.candidateResultId)}}<small class="muted">基准：{{label(record.baselineResultId)}}</small></td><td>{{record.comparisonStatus==='comparable'?'共同口径可比':'尚不可公平比较'}}</td><td>{{record.admission?.passed?'达到数值检查条件':'未通过'}}</td><td>{{record.createdAt?new Date(record.createdAt).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'历史记录'}}</td><td><button @click="open(record.id)">查看报告</button></td></tr></tbody></table></div>
  <p v-else class="muted">暂无正式评价归档。页面上的存量年度分数不会自动成为发布证据。</p>
  <p v-if="loading" role="status">正在读取冻结评价报告…</p>
  <article v-if="selected" class="evaluation-details">
   <h4>{{label(selected.candidateResultId)}} · 共同日期评价</h4>
   <p class="note" :class="{warning:!selected.admission?.passed}">{{selected.comparisonStatus==='comparable'?'共同口径可比':'尚不可公平比较'}} · {{selected.admission?.passed?'数值检查达到要求，发布仍需独立验收':'发布检查未通过，当前默认保持不变'}}</p>
   <p v-if="!selected.initializationExclusions" class="note warning">这份历史归档没有记录初始化日排除规则，不能用于发布。重新提交评价可按现行规则归档，历史报告继续保留。</p>
   <ul v-if="selected.comparisonReasons?.length"><li v-for="reason in selected.comparisonReasons" :key="reason">{{reason}}</li></ul>
   <div class="table-wrap"><table><thead><tr><th>年度 / 要素</th><th>共同日数</th><th>基准 RMSE</th><th>候选 RMSE</th><th>基准偏差</th><th>候选偏差</th><th>基准 NSE</th><th>候选 NSE</th><th>基准总量偏差 / %</th><th>候选总量偏差 / %</th></tr></thead><tbody><template v-for="(year,y) in selected.years" :key="y"><tr v-for="(value,scope) in year" :key="y+scope"><td>{{y}} · {{scope==='lake'?'湖位 / m':'杜加 / m³/s'}}</td><td>{{value.commonCount}}</td><td>{{number(value.baseline?.rmse,4)}}</td><td>{{number(value.candidate?.rmse,4)}}</td><td>{{number(value.baseline?.bias,4)}}</td><td>{{number(value.candidate?.bias,4)}}</td><td>{{scope==='flow'?number(value.baseline?.nse,4):'—'}}</td><td>{{scope==='flow'?number(value.candidate?.nse,4):'—'}}</td><td>{{scope==='flow'?number(value.baseline?.pbias,2):'—'}}</td><td>{{scope==='flow'?number(value.candidate?.pbias,2):'—'}}</td></tr></template></tbody></table></div>
   <details v-if="selected.initializationExclusions"><summary>初始化日排除与共同样本</summary><p>湖位评价排除两套成果冻结初始化日期的并集，起算当天不参与自身精度评分。流量样本继续按各自共同有效日计算。</p><p :class="{warning:!selected.initializationExclusions.lakeSamplesMetadataVerified}">{{selected.initializationExclusions.lakeSamplesMetadataVerified?'初始化元数据已核验':'初始化元数据缺失或冲突，未生成合格湖位样本'}}</p><p>本次共同日期中排除：{{selected.initializationExclusions.excludedCommonDates.join('、')||'无'}}</p><div v-for="row in selected.initializationExclusions.perResult" :key="row.resultId"><b>{{label(row.resultId)}}</b><p class="muted">{{row.dates.join('、')||'无有效初始化日期'}} · {{row.metadataVerified?'已核验':'未核验'}}</p></div></details>
   <details><summary>未通过条件、诊断与限制</summary><ul><li v-for="reason in selected.admission?.failures||[]" :key="reason">{{reason}}</li><li v-for="item in selected.limitations||[]" :key="item">{{item}}</li></ul></details>
   <details><summary>冻结身份与完整文件</summary><dl><dt>评价编号</dt><dd>{{selected.id}}</dd><dt>基准发布</dt><dd>{{selected.baselineReleaseId}}</dd><dt>协议摘要</dt><dd>{{selected.protocolSHA256}}</dd></dl><p class="muted">配对 CSV、报告 JSON 可从对应评价任务下载，文件包含独立摘要。图表缩放不会修改本报告。</p></details>
  </article>
 </section>
</template>
