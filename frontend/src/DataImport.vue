<script setup>
import {ref,computed,onMounted} from 'vue'
import {api} from './api.mjs'
const props=defineProps({disabled:Boolean,stations:{type:Array,default:()=>[]}})
const emit=defineEmits(['imported','error'])
const file=ref(null),filename=ref(''),source=ref(''),sheet=ref(''),working=ref(false),message=ref(''),preview=ref(null),mapping=ref({}),defaults=ref({}),mode=ref('append'),duplicatePolicy=ref('reject'),contract=ref(null),checkedSignature=ref('')
const fields=[['station','站点'],['variable','要素'],['time','时间'],['value','数值'],['unit','单位'],['step','时间尺度'],['periodStart','时段起点'],['periodEnd','时段终点'],['quality','质量标记']]
const variableNames={precipitation:'降水',temperature:'气温',evaporation:'蒸发',flow:'流量',level:'水位',area:'面积',snowWaterEquivalent:'雪水当量'}
const clean=object=>Object.fromEntries(Object.entries(object).filter(([,value])=>value!==''&&value!=null))
const settings=computed(()=>({schemaVersion:1,projectId:'selinco',sourceKey:source.value.trim(),timeZone:'Asia/Shanghai',filename:filename.value,...(sheet.value?{sheet:sheet.value}:{}),mapping:clean(mapping.value),defaults:clean(defaults.value),mode:mode.value,duplicatePolicy:duplicatePolicy.value}))
const signature=computed(()=>JSON.stringify(settings.value))
const isChecked=computed(()=>!!preview.value&&checkedSignature.value===signature.value)
function choose(event){
 file.value=event.target.files[0]||null;filename.value='';sheet.value='';preview.value=null;mapping.value={};defaults.value={};checkedSignature.value='';message.value=''
 if(file.value)source.value=file.value.name.replace(/\.[^.]+$/,'')
}
async function inspect(){
 if(!file.value||!source.value.trim())return
 working.value=true;message.value=''
 try{
  if(!filename.value){
   const response=await fetch('/api/model/upload?format=exchange&filename='+encodeURIComponent(file.value.name),{method:'PUT',body:file.value})
   const uploaded=await response.json();if(!response.ok)throw Error(uploaded.error||'上传失败');filename.value=uploaded.filename
  }
  let data=await api('data-import-preview',settings.value)
  if(!preview.value&&Object.keys(mapping.value).length===0&&data.suggestedMapping){
   mapping.value={...data.suggestedMapping}
   data=await api('data-import-preview',settings.value)
  }
  preview.value=data;checkedSignature.value=signature.value
 }catch(e){emit('error',e.message)}finally{working.value=false}
}
function changeSheet(){mapping.value={};defaults.value={};preview.value=null;checkedSignature.value='';inspect()}
async function submit(){
 if(!isChecked.value||preview.value?.invalid>0||preview.value?.duplicates>0||(mode.value==='append'&&duplicatePolicy.value==='reject'&&preview.value?.conflicts>0))return
 working.value=true;message.value=''
 try{
  const data=await api('data-import-file',settings.value)
  message.value=data.status==='unchanged'?'资料已存在，无重复导入':`已导入 ${Number(data.records||0).toLocaleString()} 条记录。`
  emit('imported')
 }catch(e){emit('error',e.message)}finally{working.value=false}
}
onMounted(async()=>{try{contract.value=await api('data-contract')}catch(e){emit('error',e.message)}})
</script>
<template>
 <div class="standard-import">
  <div class="standard-import-title"><h4>导入观测表格</h4><a href="/api/model/data-template" class="link">下载 Excel 模板 ↓</a></div>
  <div class="form-row">
   <label class="grow">资料文件<input type="file" accept=".xlsx,.csv" :disabled="disabled||working" @change="choose"></label>
   <label class="grow">资料来源名称<input v-model="source" maxlength="160" :disabled="disabled||working" placeholder="例如：主站水位日表"></label>
   <button :disabled="disabled||working||!file||!source.trim()" @click="inspect">{{working?'检查中…':preview?'重新检查':'识别与检查'}}</button>
  </div>
  <template v-if="preview">
   <div class="form-row"><label v-if="preview.sheets?.length">工作表<select v-model="sheet" @change="changeSheet"><option value="">第一个工作表</option><option v-for="s in preview.sheets" :key="s" :value="s">{{s}}</option></select></label><label>更新方式<select v-model="mode"><option value="append">增量添加</option><option value="snapshot">替换该来源的整批记录</option></select></label><label v-if="mode==='append'">重复记录<select v-model="duplicatePolicy"><option value="reject">内容冲突时停止</option><option value="keep">保留已有记录</option><option value="replace">使用新记录</option></select></label></div>
   <details open><summary>字段对应</summary><p class="note">请核对表头。未提供的站点、要素、单位和时间尺度可在下方手动指定。</p><div class="mapping-grid"><label v-for="[key,label] in fields" :key="key">{{label}}<select v-model="mapping[key]"><option value="">未对应</option><option v-for="column in preview.columns" :key="column" :value="column">{{column}}</option></select></label></div>
    <div class="mapping-grid defaults-grid">
     <label v-if="!mapping.station">默认站点<select v-model="defaults.station"><option value="">请选择</option><option v-for="s in stations" :key="s.id" :value="s.id">{{s.name}}</option></select></label>
     <label v-if="!mapping.variable">默认要素<select v-model="defaults.variable"><option value="">请选择</option><option v-for="(_,key) in contract?.variables||{}" :key="key" :value="key">{{variableNames[key]||key}}</option></select></label>
     <label v-if="!mapping.unit">默认单位<input v-model="defaults.unit" placeholder="例如 m、mm、m3/s"></label>
     <label v-if="!mapping.step">默认时间尺度<select v-model="defaults.step"><option value="">请选择</option><option value="day">日值</option><option value="hour">小时值</option><option value="irregular">不规则采样</option></select></label>
    </div>
   </details>
   <div class="import-preview"><b>检查结果</b><span>记录 {{preview.records}} 条</span><span>有效 {{preview.valid}} 条</span><span>异常 {{preview.invalid}} 条</span><span>表内重复 {{preview.duplicates}} 条</span><span>已有时次 {{preview.existingDuplicates}} 条，其中内容冲突 {{preview.conflicts}} 条</span></div>
   <p v-if="!isChecked" class="note warning">设置已更改，请重新检查后再导入。</p>
   <p v-else-if="preview.duplicates>0" class="note warning">表内存在重复时次，请先修改源表再重新检查。</p><p v-else-if="mode==='append'&&duplicatePolicy==='reject'&&preview.conflicts>0" class="note warning">已有时次的内容有冲突，请选择处理方式后重新检查。</p>
   <details v-if="preview.issues?.length" open><summary>待处理记录（最多显示前20项）</summary><p v-for="issue in preview.issues" :key="issue.row+':'+issue.message" class="note warning">第 {{issue.row}} 行：{{issue.message}}</p></details>
   <div class="actions"><button :disabled="disabled||working||!isChecked||preview.invalid>0||preview.duplicates>0||(mode==='append'&&duplicatePolicy==='reject'&&preview.conflicts>0)" class="primary" @click="submit">确认导入</button></div>
  </template>
  <p v-if="message" role="status" class="import-status">{{message}}</p>
 </div>
</template>
<style scoped>
.standard-import{border-bottom:1px solid var(--line);padding:4px 0 22px;margin-bottom:22px}.standard-import-title{display:flex;justify-content:space-between;align-items:center;gap:12px}.standard-import-title h4{margin:0}.mapping-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}.defaults-grid{margin-top:18px}.import-preview{display:flex;gap:18px;flex-wrap:wrap;background:#f3f7f8;padding:14px;border-radius:5px;font-size:13px}.import-status{color:#38775c}
</style>
