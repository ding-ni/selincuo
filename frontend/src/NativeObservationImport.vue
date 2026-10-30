<script setup>
import {ref} from 'vue'
import {api} from './api.mjs'
const props=defineProps({disabled:Boolean,stations:{type:Array,default:()=>[]}})
const emit=defineEmits(['imported','error'])
const files=ref([]),preview=ref(null),working=ref(false),message=ref('')
const labels={SLC:'色林错主站',DUJA:'杜加',BLQ:'巴勒切','115DB':'115道班','SLC-E':'东岸','SLC-S':'南岸','SLC-W':'西岸','SLC-N':'北岸',precipitation:'降水',level:'水位',flow:'流量',day:'日值',irregular:'逐次',hour:'小时值'}
function choose(event){files.value=Array.from(event.target.files||[]).map(file=>({file,filename:'',station:''}));preview.value=null;message.value=''}
function request(){return {files:files.value.map(f=>({filename:f.filename,originalName:f.file.name,...(f.station?{station:f.station}:{})}))}}
async function inspect(){
 working.value=true;message.value='';preview.value=null
 try{
  for(const item of files.value){
   if(item.filename)continue
   const response=await fetch('/api/model/upload?'+new URLSearchParams({format:'exchange',filename:item.file.name}),{method:'PUT',body:item.file})
   const body=await response.json();if(!response.ok)throw Error(body.error||'上传失败');item.filename=body.filename
  }
  preview.value=await api('native-observation-preview',request())
 }catch(error){message.value=error.message}finally{working.value=false}
}
async function submit(){
 if(!preview.value)return
 working.value=true;message.value=''
 try{
  const result=await api('native-observation-import',{...request(),previewToken:preview.value.previewToken})
  message.value=result.status==='unchanged'?'资料已是最新，无需重复更新。':'更新完成：新增 '+result.new+' 条，更新 '+result.changed+' 条，相同 '+result.same+' 条。'
  preview.value=null;files.value=[];emit('imported')
 }catch(error){message.value=error.message;preview.value=null}finally{working.value=false}
}
</script>
<template>
 <div class="native-import">
  <p class="intro">选择从监测系统导出的水位、流量或雨量 Excel，支持一次选择多个文件。</p>
  <div class="import-actions"><label class="file-field">观测文件<input type="file" accept=".xls,.xlsx" multiple :disabled="disabled||working" @change="choose"></label><button class="primary" :disabled="disabled||working||!files.length" @click="inspect">{{working?'正在检查…':'检查资料'}}</button></div>
  <div v-if="files.length" class="files"><div v-for="item in files" :key="item.file.name" class="file-row"><span>{{item.file.name}}</span><label>站点 <select v-model="item.station" @change="preview=null"><option value="">自动识别</option><option v-for="s in stations.filter(s=>['SLC','DUJA','BLQ','115DB'].includes(s.id))" :key="s.id" :value="s.id">{{s.name}}</option></select></label></div></div>
  <template v-if="preview">
   <div class="counts"><div><strong>{{preview.counts.new}}</strong><span>新增</span></div><div><strong>{{preview.counts.changed}}</strong><span>更新</span></div><div><strong>{{preview.counts.same}}</strong><span>已有相同记录</span></div><div><strong>{{preview.counts.missing}}</strong><span>表内缺测</span></div></div>
   <div class="table-wrap"><table><thead><tr><th>文件</th><th>站点与要素</th><th>资料时段</th><th>记录</th></tr></thead><tbody><tr v-for="(row,i) in preview.summary" :key="i"><td>{{row.file}}</td><td>{{labels[row.station]||row.station}} · {{labels[row.variable]||row.variable}} · {{labels[row.step]||row.step}}</td><td>{{row.start.slice(0,10)}} — {{row.end.slice(0,10)}}</td><td>{{row.records}}</td></tr></tbody></table></div>
   <details v-if="preview.changes.length"><summary>查看已有时次的变化（最多30条）</summary><p v-for="r in preview.changes" :key="r.station+r.variable+r.time">{{labels[r.station]||r.station}} · {{labels[r.variable]||r.variable}} · {{r.time.replace('T',' ')}}：{{r.old??'缺测'}} → {{r.new??'缺测'}}</p></details>
   <details v-if="preview.issues.length"><summary>需要核对的观测值</summary><p v-for="r in preview.issues" :key="r.file+r.time">{{r.file}} · {{r.time.slice(0,10)}} · {{r.value}} mm：{{r.message}}</p></details>
   <p v-if="preview.counts.preserved" class="intro">{{preview.counts.preserved}} 个空白时次保留已有值。</p>
   <div class="confirm"><span>原始文件与历史值会保留。</span><button class="primary" :disabled="disabled||working" @click="submit">确认更新</button></div>
  </template>
  <p v-if="message" class="import-message" role="status">{{message}}</p>
 </div>
</template>
<style scoped>
.intro{color:var(--muted);margin:0 0 16px}.import-actions{display:flex;gap:14px;align-items:end}.file-field{flex:1;max-width:620px}.files{display:grid;gap:8px;margin-top:18px}.file-row{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:10px 14px;background:#f3f7f8;border-radius:6px}.file-row label{display:flex;align-items:center;gap:8px;white-space:nowrap}.counts{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.counts div{background:#f3f7f8;padding:14px;border-radius:6px;display:grid;gap:3px}.counts strong{font-size:21px;color:#145c67}.counts span,.confirm span{color:var(--muted);font-size:13px}.confirm{display:flex;align-items:center;justify-content:space-between;margin-top:20px}.import-message{margin:16px 0 0;color:#315e68}.native-import details{margin-top:16px}.native-import details p{font-size:13px}@media(max-width:700px){.counts{grid-template-columns:repeat(2,1fr)}.import-actions,.file-row{align-items:stretch;flex-direction:column}}
</style>
