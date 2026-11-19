<script setup>
import {computed,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({forecasts:{type:Array,default:()=>[]},selected:String})
const emit=defineEmits(['update:selected'])
const COLORS={blue:'#1677ff',yellow:'#f5c400',orange:'#ff7d00',red:'#f5222d'},NAMES={blue:'蓝色',yellow:'黄色',orange:'橙色',red:'红色'}
const STATUS={draft:'待确认',published:'已发布',withdrawn:'已撤回',expired:'已到期'}
const assessment=ref(null),records=ref([]),reports=ref([]),error=ref(''),notice=ref(''),busy=ref(false),pending=ref(null),editing=ref(false)
const operator=ref(localStorage.getItem('selinco:operator')||''),notes=ref('')
const validHours=ref(24)
const form=reactive({levels:[],mode:'scenario',source:''})
const chosen=computed({get:()=>props.selected,set:v=>emit('update:selected',v)})
const grade=computed(()=>assessment.value?.selected)
const documentType=mode=>mode==='business'?'业务预警单':'情景风险提示'
const levels=computed(()=>assessment.value?.rules?.levels||[])
const reference=computed(()=>assessment.value?.rules?.referenceLevelM)
const time=t=>t?String(t).replace('T',' ').slice(0,16):'—'
let timer
watch(operator,v=>localStorage.setItem('selinco:operator',v))
async function action(fn){busy.value=true;error.value='';notice.value='';try{await fn()}catch(e){error.value=e.message}finally{busy.value=false}}
async function load(){const [w,r]=await Promise.all([api('warnings'),api('reports')]);records.value=w.warnings||[];reports.value=(r.reports||r.items||[]).slice(0,12)
 assessment.value=props.selected?await api('warnings/assess?'+new URLSearchParams({forecastId:props.selected})):null
 form.levels=levels.value.map(l=>({...l}));form.mode=assessment.value?.rules?.mode||'scenario';form.source=assessment.value?.rules?.source||''}
watch(()=>props.selected,()=>action(load))
async function draft(){await action(async()=>{const until=new Date(Date.now()+validHours.value*3600000).toISOString();const record=await api('warnings',{forecastId:props.selected,operator:operator.value,validUntil:until,notes:notes.value});notice.value=documentType(record.mode)+'草稿已生成，确认后发布';await load()})}
async function publish(row){await action(async()=>{await api('warnings/'+row.id+'/publish',{operator:operator.value});notice.value='已发布';await load()})}
async function withdraw(row){const reason=prompt('撤回原因');if(!reason)return;await action(async()=>{await api('warnings/'+row.id+'/withdraw',{operator:operator.value,reason});await load()})}
async function report(kind,extra={}){await action(async()=>{pending.value=await api('reports',{reportType:kind,...extra});notice.value='正在生成报告'})}
async function saveRules(){await action(async()=>{await api('warnings/rules',{name:'色林错漫溢风险分级（调整）',mode:form.mode,source:form.source.trim(),levels:form.levels.map(l=>({color:l.color,levelM:Number(l.levelM),outflowM3s:l.outflowM3s===''?null:Number(l.outflowM3s),probability:Number(l.probability),meaning:l.meaning}))});editing.value=false;notice.value='等级标准已更新';await load()})}
async function poll(){if(!pending.value?.id)return;try{const t=await api('tasks/'+pending.value.id);if(t.status==='succeeded'){pending.value=null;notice.value='报告已生成';await load()}else if(['failed','cancelled','interrupted'].includes(t.status)){pending.value=null;error.value=t.message}}catch(e){error.value=e.message}}
onMounted(()=>{action(load);timer=setInterval(poll,2500)})
onBeforeUnmount(()=>clearInterval(timer))
</script>
<template>
<section class="panel wp">
 <div class="wp-head"><label>依据预报<select v-model="chosen"><option value="">请选择</option><option v-for="f in forecasts" :key="f.id" :value="f.id">{{f.name}} · 起报 {{time(f.issueTime)}}</option></select></label></div>
 <p v-if="error" class="message error">{{error}}<button @click="error=''">×</button></p>
 <p v-if="notice" class="message success">{{notice}}<button @click="notice=''">×</button></p>

 <div v-if="assessment" class="wp-status" :style="{'--g':grade?COLORS[grade.color]:'#7d969e'}">
  <div class="wp-badge"><b>{{grade?grade.grade:'—'}}</b><span>{{grade?NAMES[grade.color]:'未达判据'}} · {{documentType(assessment.mode)}}</span></div>
  <div class="wp-facts">
   <h3>{{grade?grade.meaning:'预见期内湖位与溃口出流均低于蓝色预警标准'}}</h3>
   <dl>
    <dt>预报最高湖位</dt><dd>{{fixed(assessment.peakMedianLevelM,2)}} m · {{time(assessment.peakMedianTime)}}<template v-if="reference"> · 距 2023 年溃决水位 {{number((assessment.peakMedianLevelM-reference)*100,0)}} cm</template></dd>
    <dt>预报最大出流</dt><dd>{{number(assessment.peakOutflowM3s,1)}} m³/s<template v-if="assessment.peakOutflowTime"> · {{time(assessment.peakOutflowTime)}}</template></dd>
    <template v-if="grade"><dt>判定依据</dt><dd>{{grade.basis==='outflow'?'溃口出流':grade.basis==='level+outflow'?'湖位与溃口出流':'湖位'}}达到标准 · 首达 {{time(grade.firstTime)}}<template v-if="assessment.memberCount>1"> · {{number(grade.maximumProportion*100,0)}}% 成员</template></dd></template>
   </dl>
  </div>
  <div class="wp-issue">
   <label>签发人<input v-model="operator" placeholder="姓名"></label>
   <label>有效期<select v-model.number="validHours"><option :value="12">12 小时</option><option :value="24">24 小时</option><option :value="48">48 小时</option><option :value="72">72 小时</option></select></label>
   <label class="wide">说明<input v-model="notes" placeholder="可选"></label>
   <button class="primary" :disabled="busy||!grade||!operator" @click="draft">生成{{documentType(assessment.mode)}}</button>
  </div>
 </div>

 <div class="wp-grid">
  <div>
   <div class="panel-title"><h4>等级标准</h4><button class="link" @click="editing=!editing">{{editing?'收起':'调整'}}</button></div>
   <table class="wp-table"><thead><tr><th>等级</th><th>湖位 ≥</th><th>出流 ≥</th><th>含义</th></tr></thead>
    <tbody><tr v-for="l in levels" :key="l.color" :class="{hit:grade?.color===l.color}"><td class="nw"><i :style="{background:COLORS[l.color]}"></i>{{l.grade}} {{NAMES[l.color]}}</td><td class="nw">{{fixed(l.levelM,2)}} m</td><td class="nw">{{l.outflowM3s==null?'—':number(l.outflowM3s,0)+' m³/s'}}</td><td>{{l.meaning}}</td></tr></tbody></table>
   <p v-if="assessment?.rules" class="wp-note">{{assessment.rules.mode==='business'?'业务判据':'情景判据'}} · {{assessment.rules.source||'用户设定'}}。各级按登记的成员比例判定。</p>
   <div v-if="editing" class="wp-edit"><label>判据类别<select v-model="form.mode" @change="form.source=''"><option value="scenario">情景判据</option><option value="business">业务判据</option></select></label><label>依据<input v-model="form.source" :placeholder="form.mode==='business'?'填写业务判据来源或批准文件':'填写情景依据'"></label></div>
   <div v-if="editing" class="wp-edit"><table class="wp-table"><thead><tr><th>等级</th><th>湖位 m</th><th>出流 m³/s</th><th>成员比例</th></tr></thead><tbody><tr v-for="l in form.levels" :key="l.color"><td><i :style="{background:COLORS[l.color]}"></i>{{l.grade}}</td><td><input type="number" step=".01" v-model="l.levelM"></td><td><input type="number" step="1" v-model="l.outflowM3s"></td><td><input type="number" step=".05" min="0" max="1" v-model="l.probability"></td></tr></tbody></table><button :disabled="busy" @click="saveRules">保存标准</button></div>
  </div>
  <div>
   <div class="panel-title"><h4>风险提示与预警记录</h4></div>
   <article v-for="r in records" :key="r.id" class="wp-record" :style="{'--g':COLORS[r.color]}">
    <header><i></i><b>{{documentType(r.mode)}} · {{r.assessment?.selected?.grade}}{{NAMES[r.color]}}</b><span :class="'st-'+r.status">{{STATUS[r.status]}}</span></header>
    <p>最高湖位 {{fixed(r.assessment?.peakMedianLevelM,2)}} m · 最大出流 {{number(r.assessment?.peakOutflowM3s,1)}} m³/s · 有效至 {{time(r.validUntil)}}</p>
    <div class="actions"><button v-if="r.status==='draft'" class="primary" :disabled="busy||!operator" @click="publish(r)">确认发布{{documentType(r.mode)}}</button><button v-if="r.status==='published'" :disabled="busy||!operator" @click="withdraw(r)">撤回</button><button :disabled="busy||!!pending" @click="report('warning',{warningId:r.id})">导出{{documentType(r.mode)}}</button></div>
   </article>
   <p v-if="!records.length" class="wp-none">暂无预警单。</p>
  </div>
 </div>

 <div class="wp-reports">
  <div class="panel-title"><h4>监测日报与预警周报</h4><button :disabled="busy||!!pending" @click="report('monitoring-daily')">生成今日日报</button><button :disabled="busy||!!pending" @click="report('warning-weekly')">生成本周周报</button></div>
  <div v-if="pending" class="running-strip">正在生成报告<progress :value="pending.progress||0" max="100"/></div>
  <ul><li v-for="r in reports" :key="r.id"><b>{{r.name}}</b><span>{{time(r.createdAt)}}</span><a v-for="a in r.artifacts" :key="a.name" :href="a.downloadUrl">{{a.name.split('.').pop().toUpperCase()}}</a></li><li v-if="!reports.length" class="wp-none">尚未生成报告。日报每日 08:00、周报每周一 08:00 自动生成。</li></ul>
 </div>
</section>
</template>
<style scoped>
.wp-head{margin-bottom:18px}.wp-head label{max-width:520px}
.wp-status{--g:#7d969e;display:grid;grid-template-columns:150px minmax(0,1fr) 320px;gap:24px;align-items:stretch;padding:20px;border:1px solid color-mix(in srgb,var(--g) 45%,#fff);border-left:6px solid var(--g);border-radius:10px;background:color-mix(in srgb,var(--g) 6%,#fff);margin-bottom:24px}
.wp-badge{display:grid;place-content:center;justify-items:center;gap:4px;border-radius:8px;background:var(--g);color:#fff;min-height:120px}
.wp-badge b{font-size:30px;line-height:1}.wp-badge span{font-size:15px}
.wp-facts h3{font-size:17px;margin:2px 0 12px;color:#26444f}
.wp-facts dl{grid-template-columns:110px 1fr;gap:8px 14px;margin:0;font-size:13.5px}
.wp-issue{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start}
.wp-issue .wide,.wp-issue button{grid-column:1/-1}
.wp-issue label{font-size:13px}.wp-issue input,.wp-issue select{min-height:36px;padding:5px 9px}
.wp-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:28px}
.wp-grid .panel-title{margin-bottom:10px}
.wp-table{font-size:13.5px}.wp-table th,.wp-table td{padding:9px 11px}
.wp-table i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;vertical-align:-1px}
.wp-table .nw{white-space:nowrap}
.wp-table tr.hit td{background:#fff7e6;font-weight:600}
.wp-table input{min-height:32px;padding:3px 7px;max-width:110px}
.wp-note{font-size:12.5px;color:var(--muted);margin:10px 0}
.wp-edit{margin-top:12px;display:grid;gap:10px;justify-items:start}
.wp-record{--g:#888;border:1px solid var(--line);border-left:4px solid var(--g);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.wp-record header{display:flex;gap:9px;align-items:center;padding:0;background:none;border:0}
.wp-record header i{width:10px;height:10px;border-radius:50%;background:var(--g)}
.wp-record header span{margin-left:auto;font-size:12px;padding:2px 8px;border-radius:10px;background:#eef3f5;color:#4f6b75}
.wp-record header .st-published{background:#e5f3ea;color:#2f7148}.wp-record header .st-draft{background:#fdf3df;color:#8a6417}
.wp-record p{margin:8px 0 0;font-size:13px;color:#4c6772}
.wp-record .actions{margin:10px 0 0}
.wp-none{color:var(--muted);font-size:13px}
.wp-reports{margin-top:26px;border-top:1px solid var(--line);padding-top:18px}
.wp-reports .panel-title{margin-bottom:10px}.wp-reports .panel-title>button:last-child{margin-left:0}.wp-reports .panel-title h4{margin-right:auto}
.wp-reports ul{list-style:none;padding:0;margin:0;display:grid;gap:6px}
.wp-reports li{display:flex;gap:14px;align-items:center;font-size:13.5px;padding:8px 0;border-bottom:1px solid #edf2f4}
.wp-reports li span{color:var(--muted);margin-right:auto}
@media(max-width:1250px){.wp-status{grid-template-columns:130px 1fr}.wp-issue{grid-column:1/-1;grid-template-columns:repeat(4,minmax(0,1fr))}.wp-grid{grid-template-columns:1fr}}
</style>
