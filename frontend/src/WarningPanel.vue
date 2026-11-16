<script setup>
import {ref,reactive,onMounted,computed} from 'vue'
import {api,number} from './api.mjs'
const props=defineProps({forecastId:String})
const values=ref({rules:[],warnings:[],default:null}),error=ref(''),busy=ref(false),operator=ref(''),ruleId=ref(''),notice=ref('')
const expires=new Date(Date.now()+86400000)
const validUntil=ref(new Date(expires.getTime()-expires.getTimezoneOffset()*60000).toISOString().slice(0,16)),notes=ref('')
const COLORS=[['blue','蓝色','#2d8cce'],['yellow','黄色','#c8ab23'],['orange','橙色','#d37c25'],['red','红色','#b63948']]
const COLOR_DESC={blue:'接近历史高水位，注意监视',yellow:'超历史实测最高，启动预警',orange:'接近东侧低洼溢口高程，准备应急',red:'漫溢高风险，立即启动应急预案'}
const form=reactive({name:'湖位情景判据',mode:'scenario',source:'',levels:COLORS.map(([color])=>({color,levelM:'',probability:.5}))})
const hasRules=computed(()=>values.value.rules.length>0)
const defaultRules=computed(()=>values.value.default)
function colorHex(color){return COLORS.find(c=>c[0]===color)?.[2]||'#999'}
function applyDefault(){
  if(!defaultRules.value)return
  form.name=defaultRules.value.name
  form.mode=defaultRules.value.mode
  form.source=defaultRules.value.source
  defaultRules.value.levels.forEach(r=>{const item=form.levels.find(l=>l.color===r.color);if(item){item.levelM=r.levelM;item.probability=r.probability}})
  notice.value='已载入内置参考阈值，请核对后保存'
}
async function refresh(){
  const[w,r]=await Promise.all([api('warnings'),api('warnings/rules')])
  values.value={rules:r.items||[],warnings:w.warnings||[],default:r.default||null}
  if(!ruleId.value&&values.value.rules.length)ruleId.value=values.value.rules[0].id
}
async function action(fn){busy.value=true;error.value='';notice.value='';try{await fn();await refresh()}catch(e){error.value=e.message}finally{busy.value=false}}
async function save(){await action(async()=>{const value=await api('warnings/rules',{...form,levels:form.levels.filter(r=>r.levelM!=='')});ruleId.value=value.id;notice.value='判据已保存'})}
async function draft(){await action(async()=>{await api('warnings/draft',{forecastId:props.forecastId,ruleSetId:ruleId.value,operator:operator.value,validUntil:validUntil.value?new Date(validUntil.value).toISOString():undefined,notes:notes.value});notice.value='已生成待确认的提示草稿'})}
async function publish(row){await action(()=>api('warnings/'+row.id+'/publish',{operator:operator.value}))}
async function withdraw(row){const reason=prompt('撤回原因');if(!reason)return;await action(()=>api('warnings/'+row.id+'/withdraw',{operator:operator.value,reason}))}
async function report(row){await action(async()=>{await api('reports',{reportType:'warning',warningId:row.id});notice.value='预警单生成任务已提交，可在报告列表下载'})}
onMounted(()=>action(refresh))
</script>
<template><section class="warning-panel">
<div class="panel-title"><h3>判据与发布记录</h3><button :disabled="busy" @click="action(refresh)">刷新</button></div>
<p class="error" v-if="error">{{error}}</p>
<p class="note" v-if="notice">{{notice}}</p>

<details open>
  <summary>配置水位与集合超限判据</summary>
  <div v-if="!hasRules&&defaultRules" class="default-hint">
    <span>⚠ 尚未保存判据。内置参考阈值基于2023年漫溢事件，须主管人员核实后启用。</span>
    <button @click="applyDefault" :disabled="busy">载入参考阈值</button>
  </div>
  <div class="warning-form">
    <label>名称<input v-model="form.name"></label>
    <label>类别<select v-model="form.mode"><option value="scenario">情景判据</option><option value="business">有来源的业务判据</option></select></label>
    <label class="wide">依据<input v-model="form.source" placeholder="情景假设或批准文件来源"></label>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>级别</th><th>水位 m</th><th>超限成员比例</th><th>含义</th></tr></thead>
      <tbody>
        <tr v-for="(r,i) in form.levels" :key="r.color">
          <td><span class="level-dot" :style="{background:COLORS[i][2]}"></span>{{COLORS[i][1]}}</td>
          <td><input type="number" step=".01" v-model.number="r.levelM" placeholder="留空不启用"></td>
          <td><input type="number" min="0" max="1" step=".05" v-model.number="r.probability"></td>
          <td class="level-desc">{{COLOR_DESC[r.color]}}</td>
        </tr>
      </tbody>
    </table>
  </div>
  <button :disabled="busy" @click="save">保存判据</button>
</details>

<div class="warning-form publish-form">
  <label>采用判据<select v-model="ruleId"><option value="">请选择</option><option v-for="r in values.rules" :key="r.id" :value="r.id">{{r.name}}</option></select></label>
  <label>确认人<input v-model="operator" placeholder="本机操作人"></label>
  <label>有效至<input type="datetime-local" v-model="validUntil"></label>
  <label>发布说明<input v-model="notes" placeholder="本次判断或需要说明的情况"></label>
  <button class="primary" :disabled="busy||!props.forecastId||!ruleId||!validUntil" @click="draft">从当前预测生成草稿</button>
</div>

<div class="warning-records">
  <article v-for="row in values.warnings" :key="row.id">
    <div>
      <span class="level-dot lg" :style="{background:colorHex(row.color)}"></span>
      <strong>{{row.name}}</strong>
      <span class="status">{{({draft:'待确认',published:'已发布',withdrawn:'已撤回',expired:'已到期'})[row.status]}}</span>
    </div>
    <small>有效至 {{row.validUntil}} · 预测 {{row.forecastId}}</small>
    <p>阈值 {{number(row.assessment.selected.levelM,2)}} m / 最大超限成员比例 {{number(row.assessment.selected.maximumProportion*100,1)}}%</p>
    <div class="actions">
      <button v-if="row.status==='draft'" :disabled="busy||!operator" @click="publish(row)">人工确认发布</button>
      <button v-if="row.status==='published'" :disabled="busy||!operator" @click="withdraw(row)">撤回</button>
      <button :disabled="busy" @click="report(row)">导出预警单</button>
    </div>
  </article>
  <p v-if="!values.warnings.length" class="empty">暂无预警或情景提示记录。</p>
</div>
</section></template>
<style scoped>
.warning-panel{margin-top:22px}
.warning-form{display:grid;grid-template-columns:repeat(2,minmax(150px,1fr));gap:12px;margin:16px 0}
.warning-form label{display:flex;flex-direction:column;gap:6px}
.warning-form .wide{grid-column:1/-1}
.publish-form{grid-template-columns:repeat(2,minmax(150px,1fr));align-items:end}
.publish-form>button{justify-self:start}
.warning-panel input,.warning-panel select{width:100%}
.warning-panel td input{max-width:140px}
.level-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;flex-shrink:0;vertical-align:middle}
.level-dot.lg{width:12px;height:12px}
.level-desc{color:var(--muted,#61767b);font-size:11px;max-width:160px}
.warning-records{display:grid;gap:10px;margin-top:16px}
.warning-records article{padding:15px;border:1px solid var(--border,#d4dfe0);border-radius:8px}
.warning-records small{display:block;margin-top:8px;color:var(--muted,#61767b);overflow-wrap:anywhere}
.status{font-size:12px;margin-left:12px}
.warning-records p{margin:8px 0;font-size:13px}
.default-hint{display:flex;align-items:center;gap:14px;padding:10px 14px;background:#fdf5e4;border:1px solid #e8d495;border-radius:6px;margin-bottom:14px;font-size:13px;color:#735c18}
.default-hint button{flex-shrink:0}
@media(max-width:800px){.publish-form{grid-template-columns:1fr}.warning-form{grid-template-columns:1fr}}
</style>
