<script setup>
import {ref} from 'vue'
const props=defineProps({disabled:Boolean})
const emit=defineEmits(['imported','error'])
const file=ref(null),source=ref(''),working=ref(false),message=ref('')
function choose(event){file.value=event.target.files[0]||null;if(file.value)source.value=file.value.name.replace(/\.[^.]+$/,'')}
async function submit(){
 if(!file.value||!source.value.trim())return
 working.value=true;message.value=''
 try{
  let response=await fetch('/api/model/upload?format=exchange&filename='+encodeURIComponent(file.value.name),{method:'PUT',body:file.value})
  let data=await response.json();if(!response.ok)throw Error(data.error)
  response=await fetch('/api/model/data-import-file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schemaVersion:1,projectId:'selinco',sourceKey:source.value.trim(),timeZone:'Asia/Shanghai',filename:data.filename,sheet:'data'})})
  data=await response.json();if(!response.ok)throw Error(data.error)
  message.value=data.status==='unchanged'?(data.active?'资料已存在，无重复导入':'此文件为旧版本，现用资料保持最新版本'):`已导入 ${data.records.toLocaleString()} 条记录`
  emit('imported')
 }catch(error){emit('error',error.message)}finally{working.value=false}
}
</script>
<template>
 <div class="standard-import">
  <div class="standard-import-title"><h4>标准表格导入</h4><a href="/api/model/data-template" class="link">下载 Excel 模板 ↓</a></div>
  <form class="form-row" @submit.prevent="submit">
   <label class="grow">资料文件<input type="file" accept=".xlsx,.csv" :disabled="disabled||working" @change="choose"></label>
   <label>来源名称<input v-model="source" maxlength="160" required :disabled="disabled||working" placeholder="例如：主站水位日表"></label>
   <button class="primary" :disabled="disabled||working||!file||!source.trim()">{{working?'正在导入':'导入资料'}}</button>
  </form>
  <p v-if="message" role="status" class="import-status">{{message}}</p>
 </div>
</template>
<style scoped>
.standard-import{border-bottom:1px solid #e1e9e3;padding:4px 0 22px;margin-bottom:22px}.standard-import-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:15px}.standard-import-title h4{margin:0;font-size:15px;font-weight:550;color:#416950}.standard-import-title a{font-size:11px}.import-status{margin-bottom:0;color:#38775c;font-size:12px}
</style>
