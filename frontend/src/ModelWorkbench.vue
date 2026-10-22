<script setup>
import {ref,computed,watch} from 'vue'
import {groups,runIssue,parameterIssue,preferredHistoricalInput,historicalPeriod} from './workflow.mjs'
const props=defineProps({boot:Object,run:Object,params:Object,page:String,busy:Boolean,active:Array,completed:Array})
const emit=defineEmits(['submit','scheme','parameters','save-scheme','save-parameters','data','defaults','base'])
const method=ref('automatic'),tab=ref('result'),group=ref('land'),schemeId=ref(''),parameterId=ref(''),schemeName=ref(''),parameterName=ref('')
const mode=computed(()=>props.page==='calibration'&&method.value==='automatic'?'calibration':props.page==='calibration'?'trial':'simulation')
const issue=computed(()=>runIssue(props.run,props.boot.inputs,mode.value,props.active.length>0)||parameterIssue(props.params,props.boot.parameterDefinitions))
const input=computed(()=>props.boot.inputs.find(i=>i.id===props.run.inputId))
const definitions=computed(()=>props.boot.parameterDefinitions.filter(d=>d.group===group.value))
const baseTasks=computed(()=>props.completed.filter(t=>!t.trashed))
const searchKeys=computed(()=>props.run.stage==='lake'?['lakeCoefficient']:props.run.stage==='routing'?(props.boot.calibrationConfig?.routingKeys||[]):(props.boot.calibrationConfig?.runoffKeys||[]))
const automaticLocked=d=>mode.value==='calibration'&&!searchKeys.value.includes(d.key)
function selectInput(e){const i=props.boot.inputs.find(i=>i.id===e.target.value);props.run.inputId=i?.id||'';props.run.baseTaskId='';if(i){Object.assign(props.run,historicalPeriod(i.manifest))}}
function preset(year){props.run.start=(year===2024?'2023':year)+'-01-01T00:00';props.run.end=(year+1)+'-01-01T00:00'}
function completeHistory(){const i=preferredHistoricalInput(props.boot.inputs);if(i){props.run.inputId=i.id;props.run.baseTaskId='';Object.assign(props.run,historicalPeriod(i.manifest))}}
watch(()=>props.page,()=>{tab.value='result'})
watch(()=>props.run.stage,()=>{props.run.baseTaskId='';if(props.run.stage==='routing')props.run.routingMode='source-separated'})
</script>
<template>
 <section class="panel scheme-toolbar"><div><b>计算方案</b><small>保存与恢复条件 · 参数试调 · 成果留存</small></div><select v-model="schemeId" aria-label="打开已保存方案" @change="emit('scheme',schemeId)"><option value="">当前编辑条件</option><option v-for="s in boot.schemes" :key="s.id" :value="s.id">{{s.name}}</option></select><input v-model="schemeName" aria-label="新方案名称" placeholder="方案名称"><button :disabled="!schemeName.trim()||busy" @click="emit('save-scheme',schemeName)">另存方案</button></section>
 <div class="model-workbench">
  <aside class="conditions-column">
   <section class="panel"><div class="panel-title"><h3>计算条件</h3><span class="badge">连续时段</span></div>
    <div class="condition-fields"><label>采用参数<select v-model="parameterId" @change="emit('parameters',parameterId)"><option value="">当前编辑参数 / 未率定初值</option><option v-for="p in boot.parameters" :key="p.id" :value="p.id">{{p.name}}</option></select></label>
     <label>任务名称<input v-model="run.name" placeholder="未率定基线 / 参数试调"></label>
     <label>真实气象输入<select :value="run.inputId" @change="selectInput"><option value="">请选择已登记输入</option><option v-for="i in boot.inputs" :key="i.id" :value="i.id" :disabled="i.compatible===false">{{i.name}} · {{i.manifest.start.slice(0,10)}}—{{i.manifest.end.slice(0,10)}}</option></select></label>
     <p v-if="input" class="note coverage-hint">可用 {{input.manifest.start.slice(0,16).replace('T',' ')}}<br>至 {{input.manifest.end.slice(0,16).replace('T',' ')}} · 北京时间</p>
     <label>汇流结构<select v-model="run.routingMode" :disabled="mode==='calibration'&&['lake','routing'].includes(run.stage)"><option v-for="r in boot.routingModes" :key="r.id" :value="r.id">{{r.label}}</option></select></label><label>开始时间<input v-model="run.start" type="datetime-local" step="3600"></label><label>结束时间<input v-model="run.end" type="datetime-local" step="3600"></label>
     <div class="period-presets"><button :disabled="!boot.annualValidation.ready" @click="completeHistory">完整历史（含预热）</button><button @click="preset(2024)">2024率定</button><button @click="preset(2025)">2025单年回算</button><button @click="preset(2026)">2026检验</button></div>
     <template v-if="page==='calibration'"><div class="segmented" aria-label="调参方式"><button :class="{active:method==='automatic'}" @click="method='automatic'">自动率定</button><button :class="{active:method==='manual'}" @click="method='manual';tab='parameters'">人工试调</button></div><template v-if="method==='automatic'"><label>率定阶段<select v-model="run.stage"><option value="runoff">① 产汇流 · 杜加流量</option><option value="routing">② 汇流 · 冻结陆面与冰雪</option><option value="lake">③ 湖泊 · 主站湖位</option></select></label><label v-if="run.stage!=='lake'">流量目标<select v-model="run.objectiveProfile"><option v-for="(v,k) in boot.calibrationConfig?.profiles" :key="k" :value="k">{{v.label}}</option></select></label><label>评价次数预算<input type="number" min="10" max="2000" step="1" v-model.number="run.maxEvaluations"></label><label v-if="['lake','routing'].includes(run.stage)">冻结参数的基准成果<select v-model="run.baseTaskId" @change="emit('base',run.baseTaskId)"><option value="">选择成功成果（提交时核验同输入）</option><option v-for="t in baseTasks" :key="t.id" :value="t.id">{{t.name||t.id}}</option></select></label></template></template>
     <p v-if="issue" class="note warning" role="status">{{issue}}</p><button class="primary run-primary" :disabled="busy||!!issue" @click="tab='result';emit('submit',mode)">{{mode==='calibration'?'开始分步率定':mode==='trial'?'运行人工试调':'开始历史模拟'}}</button>
     <button v-if="page==='simulation'" :disabled="busy||!!issue" @click="tab='parameters'">调整参数 / 人工试调</button><button :disabled="busy||active.length>0||!!runIssue(run,boot.inputs,'sensitivity',false)||!!parameterIssue(params,boot.parameterDefinitions)" @click="tab='result';emit('submit','sensitivity')">运行局部敏感性诊断</button><button class="link" @click="emit('data')">查看资料覆盖与下载 →</button>
    </div>
   </section>
   <section class="panel compact-guide"><b>本次计算规则</b><p>小时演算 · 日尺度评价<br>2023预热 → 2024率定 → 2025验证<br>正式验证沿用预热后的连续状态。<br>湖面面积固定，可靠观测窗口起算。</p><span class="badge">冰雪全年连续</span><p class="note">汇流阶段仅搜索河道、壤中与地下调蓄时间，冻结陆面、冰雪及湖泊参数。对照原单库方案时，这两个参数不生效。</p></section>
  </aside>
  <div class="workbench-main"><nav class="workspace-tabs" aria-label="模型工作区"><button :class="{active:tab==='result'}" @click="tab='result'">过程与评价</button><button :class="{active:tab==='parameters'}" @click="tab='parameters'">参数试调</button><button :class="{active:tab==='readiness'}" @click="tab='readiness'">计算准备</button></nav>
   <div v-if="tab==='result'"><slot/><section v-if="!$slots.default" class="panel empty">完成计算后，在此查看过程、评价和状态。</section></div>
   <section v-if="tab==='parameters'" class="panel parameter-workspace"><div class="panel-title"><h3>模型参数</h3><button class="link" :disabled="busy||active.length>0" @click="emit('defaults')">恢复未率定初值</button></div><p class="note">人工试调修改当前参数；产汇流阶段固定冰雪参数，汇流阶段只调3个记忆时间，湖泊阶段只调蒸发系数。后两阶段恢复基准成果的完整条件，左侧显示实际搜索范围。</p>
    <div class="parameter-group-tabs"><button v-for="g in groups" :key="g.key" :class="{active:group===g.key}" @click="group=g.key">{{g.label}}</button></div>
    <table class="parameter-table"><thead><tr><th>参数</th><th>当前值</th><th>允许范围 / 单位</th><th>自动搜索</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td><b>{{d.label}}</b><small class="block">{{d.key}}</small></td><td><input :aria-label="d.label" type="number" :min="d.min" :max="d.max" :step="d.step" :disabled="busy||active.length>0||(mode==='calibration'&&['lake','routing'].includes(run.stage))" v-model.number="params[d.key]"></td><td>{{d.min}} — {{d.max}}<small class="block">{{d.unit||'无量纲'}}</small></td><td><span class="badge">{{mode!=='calibration'?'人工设定':automaticLocked(d)?'固定':'参与搜索'}}</span></td></tr></tbody></table>
    <div class="form-row"><label class="grow">命名当前参数<input v-model="parameterName" placeholder="例如：基线参数 / 试调02"></label><button :disabled="!parameterName.trim()||busy||!!parameterIssue(params,boot.parameterDefinitions)" @click="emit('save-parameters',parameterName)">保存到参数库</button></div><div v-if="page==='simulation'||method==='manual'" class="actions"><button class="primary" :disabled="busy||!!runIssue(run,boot.inputs,'trial',active.length>0)||!!parameterIssue(params,boot.parameterDefinitions)" @click="tab='result';emit('submit','trial')">运行本组参数试调</button></div><p class="note">保存参数后可在左侧“采用参数”中直接选择，用于其他时段模拟。</p>
   </section>
   <section v-if="tab==='readiness'" class="panel"><div class="panel-title"><h3>计算准备检查</h3></div><div class="readiness-row"><b>空间控制域</b><span>{{boot.spatial?.version||'未生成'}}</span></div><div class="readiness-row"><b>计算单元</b><span>{{boot.spatial?.units.length||0}} 个 · 500 m高程带 / 冰川分区</span></div><div class="readiness-row"><b>真实驱动</b><span>{{input?input.manifest.hours.toLocaleString()+' 小时':'未选择'}}</span></div><div class="readiness-row"><b>年度驱动</b><span>{{boot.annualValidation.message}}</span></div><div class="readiness-row"><b>当前条件</b><span :class="{warning:!!issue}">{{issue||'满足提交条件；后端继续核验数据及冻结版本。'}}</span></div><p class="note">参数默认未率定。无实测流量的河流使用参数移用，显示为未独立验证；2025和2026观测不参与2024参数搜索。</p><button @click="emit('data')">前往资料与格网下载</button></section>
  </div>
 </div>
</template>
