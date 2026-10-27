<script setup>
import {ref,computed,watch} from 'vue'
import HistoricalResult from './HistoricalResult.vue'
import ResearchSchemes from './ResearchSchemes.vue'
import CalibrationTrace from './CalibrationTrace.vue'
import {api,number,standardName} from './api.mjs'
const props=defineProps({boot:Object,tasks:Array,detail:Object,busy:Boolean,active:Array})
const emit=defineEmits(['submit','stop','select','save-parameters','state','refresh'])
const tab=ref('standard'),scope=ref('duja'),group=ref('land')
const standard=computed(()=>props.boot.operationalSchemes[0]),groups=computed(()=>[...new Set(props.boot.parameterDefinitions.map(d=>d.group))])
const labels={land:'陆面产流',snow:'冰雪过程',glacier:'冰川过程',routing:'汇流参数',lake:'湖泊参数'}
const records=computed(()=>props.tasks.filter(t=>!t.trashed&&['calibration-workflow','calibration','trial','sensitivity'].includes(t.kind)))
const definitions=computed(()=>props.boot.parameterDefinitions.filter(d=>d.group===group.value))
const baseline=computed(()=>({...standard.value.parameters,...(scope.value==='duja'?standard.value.partitionParameters?.duja:{})}))
const locked=computed(()=>props.busy||props.active.length>0)
const result=computed(()=>props.detail?.result)
const summary=ref(null),summaryError=ref('');let summaryTicket=0
watch(()=>props.boot.standardResultId,async id=>{const ticket=++summaryTicket;summary.value=null;summaryError.value='';try{const value=await api('result-summary?resultId='+encodeURIComponent(id));if(ticket===summaryTicket)summary.value=value}catch(e){if(ticket===summaryTicket)summaryError.value=e.message}},{immediate:true})
const annual=computed(()=>summary.value?.evaluation?.annual||Object.fromEntries([2024,2025].map(y=>[y,standard.value.evidence?.['metrics'+y]||{}])))
</script>
<template>
 <section class="panel">
 <div class="panel-title"><h3>模型方案与率定</h3><span class="badge">已发布方案 · 修订 {{standard.revision}}</span><button class="primary" @click="tab='calibrate'">新建研究候选</button></div>
 <div class="scheme-heading">{{tab==='standard'?standardName:tab==='calibrate'?'我的研究方案':detail?.request?.schemeSnapshot?.name||'率定过程与成果'}}</div>
 <nav class="workspace-tabs"><button :class="{active:tab==='standard'}" @click="tab='standard'">标准方案</button><button :class="{active:tab==='calibrate'}" @click="tab='calibrate'">我的方案</button><button :class="{active:tab==='records'}" @click="tab='records'">率定过程与成果</button></nav>
 <template v-if="tab==='standard'"><div class="scheme-overview"><h4>当前默认成果的评价口径</h4><p>{{summary?.evaluation?.label||'窗口初始化评价'}}。湖位在各观测窗口起点初始化；这些指标不能作为固定锚点连续模拟的精度。</p><p>业务默认保留原历史成果。逐格网连续候选尚未通过共同日期比较与初态稳定性准入；空间更细不等于精度更高。</p></div><p v-if="summaryError" class="note warning">冻结成果摘要读取失败：{{summaryError}}。下表暂列方案归档证据，请刷新重试。</p><div class="table-wrap standard-evaluation"><table><thead><tr><th>评价年</th><th>湖位 RMSE / m</th><th>湖位偏差 / m</th><th>湖位有效日</th><th>杜加 NSE</th><th>流量偏差 / %</th><th>流量有效日</th></tr></thead><tbody><tr v-for="(m,y) in annual" :key="y"><td>{{y}}年</td><td>{{number(m.lake?.rmse,4)}}</td><td>{{number(m.lake?.bias,4)}}</td><td>{{m.lake?.count??'—'}}</td><td>{{number(m.flow?.nse,4)}}</td><td>{{number(m.flow?.pbias,2)}}</td><td>{{m.flow?.count??'无合格评价资料'}}</td></tr></tbody></table></div><details class="scheme-admission"><summary>候选发布条件与资料限制</summary><p>2024、2025共同有效日期下，连续湖位RMSE与偏差绝对值、杜加NSE与总量偏差绝对值均不得退步；至少一年湖位RMSE下降10%或杜加NSE提高0.03，并通过真实预热、干湿初态、完整成果和严格续算核验。</p><p>2025、2026资料已用于开发诊断，不能称未触碰的外部验证。四岸零点与未核实排水区继续保留不确定性，保存参数和研究候选不会修改默认发布。</p></details><div class="result-toolbar"><label>参数区域<select v-model="scope"><option value="duja">杜加控制区 · 测流约束</option><option value="basin">流域背景参数</option></select></label><span class="muted">{{scope==='duja'?'杜加区域值覆盖背景值，作用范围仅限杜加控制区。':'未测分区保留背景参数及来源，不逐格自由率定。'}}</span></div>
 <div class="parameter-group-tabs"><button v-for="g in groups" :key="g" :class="{primary:group===g}" @click="group=g">{{labels[g]||g}}</button></div>
 <div class="table-wrap"><table><thead><tr><th>参数</th><th>标准值</th><th>单位</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td>{{d.label}}</td><td>{{number(baseline[d.key],6)}}</td><td>{{d.unit||'—'}}</td></tr></tbody></table></div>
 </template>
 <ResearchSchemes v-if="tab==='calibrate'" :boot="boot" :locked="locked" @refresh="emit('refresh')" @submit="r=>{emit('submit',r);tab='records'}"/>
 <template v-if="tab==='records'"><p class="note">任务完成表示计算结束；候选精度与发布准入须单独评价。预算耗尽不代表算法收敛。</p><div class="table-wrap"><table><thead><tr><th>名称</th><th>时间</th><th>状态</th></tr></thead><tbody><tr v-for="t in records" :key="t.id"><td><button class="link" @click="emit('select',t.id)">{{t.name||'参数率定'}}</button></td><td>{{t.created_at.slice(0,16).replace('T',' ')}}</td><td>{{t.status==='succeeded'?'计算完成 · 研究候选':t.message}}</td></tr></tbody></table></div><p v-if="!records.length" class="empty">尚无研究任务。可复制已发布方案建立候选。</p></template>
 </section>
 <template v-if="tab==='records'&&detail">
 <CalibrationTrace :task="detail"/>
 <section v-if="result?.parameters" class="panel"><div class="panel-title"><h3>率定成果参数</h3><button :disabled="busy" @click="emit('save-parameters',detail)">保存候选参数</button></div><div class="table-wrap"><table><thead><tr><th>参数</th><th>标准值</th><th>本次值</th></tr></thead><tbody><tr v-for="d in boot.parameterDefinitions.filter(d=>['KC','WM','B','SM','KI','KG','tauHours','interflowTauHours','baseflowTauHours','lakeCoefficient'].includes(d.key))" :key="d.key"><td>{{d.label}}</td><td>{{number({...standard.parameters,...standard.partitionParameters?.duja}[d.key],5)}}</td><td>{{number({...result.parameters,...result.partitionParameters?.duja}[d.key],5)}}</td></tr></tbody></table></div></section>
 <HistoricalResult :detail="detail" :busy="busy" @stop="emit('stop',$event)" @state="emit('state')" @retry="tab='calibrate'"/>
 <details v-if="result?.calibration" class="panel"><summary>搜索诊断</summary><dl><dt>评价次数</dt><dd>{{result.calibration.evaluations}}</dd><dt>搜索目标</dt><dd>{{result.calibration.objectiveProfile}}</dd><dt>搜索阶段</dt><dd>{{result.calibrationWorkflow?.stages.map(s=>({runoff:'产流',routing:'汇流',lake:'湖泊'})[s.stage]).join(' → ')||result.calibration.stage}}</dd></dl></details>
 </template>
</template>
