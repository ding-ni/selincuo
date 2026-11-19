<script setup>
import {computed, ref} from 'vue'
import {api, number} from './api.mjs'

const props=defineProps({tasks:{type:Array,default:()=>[]},busy:Boolean})
const emit=defineEmits(['refresh'])
const file=ref(null), preview=ref(null), pending=ref(''), submitted=ref(null), message=ref(''), error=ref('')
const record=computed(()=>props.tasks.find(t=>t.id===submitted.value?.id)||submitted.value)
const restored=computed(()=>record.value?.kind==='restore-workspace'&&record.value?.status==='succeeded')
const active=computed(()=>props.tasks.some(t=>['queued','running','stopping'].includes(t.status)))
const backups=computed(()=>props.tasks.filter(t=>t.kind==='backup'&&t.status==='succeeded'&&t.result?.artifacts?.length))
const blocked=computed(()=>props.busy||!!pending.value||active.value||restored.value)
function choose(event){file.value=event.target.files?.[0]||null;preview.value=null;message.value='';error.value=''}
async function perform(stage,operation){
 if(blocked.value)return
 pending.value=stage;message.value='';error.value=''
 try{await operation()}catch(e){error.value=e.message}finally{pending.value=''}
}
async function backup(){await perform('backup',async()=>{
 submitted.value=await api('tasks',{kind:'backup',name:'工作区备份'});emit('refresh')
})}
async function inspect(){await perform('upload',async()=>{
 if(!file.value)return
 preview.value=null
 const response=await fetch('/api/model/upload?'+new URLSearchParams({filename:file.value.name}),{method:'PUT',body:file.value})
 const uploaded=await response.json();if(!response.ok)throw Error(uploaded.error||'备份上传失败')
 preview.value=await api('restore-preview',{filename:uploaded.filename})
})}
async function restore(){await perform('restore',async()=>{
 if(!preview.value)return
 submitted.value=await api('restore',{filename:preview.value.filename,token:preview.value.token})
 preview.value=null;message.value='正在恢复到单独的工作区。';emit('refresh')
})}
</script>
<template>
 <section class="panel"><div class="panel-title"><h3>备份工作区</h3></div>
  <p>把本机的观测、方案、预报和报告保存为 ZIP 文件。换电脑时请一并携带相同版本的安装包。</p>
  <button class="primary" :disabled="blocked" @click="backup">创建工作区备份</button>
  <p v-for="t in backups" :key="t.id"><a :href="t.result.artifacts[0].downloadUrl">下载备份 ZIP</a> · {{t.name}} · {{new Date(t.created_at).toLocaleString('zh-CN')}} · {{number(t.result.artifacts[0].bytes/1048576,1)}} MiB</p>
 </section>
 <section class="panel"><div class="panel-title"><h3>恢复工作区</h3></div>
  <p>选择已有的工作区备份，先预览内容。确认后另建工作区；重启平台时切换，原工作区保留。</p>
  <div class="actions"><label>备份 ZIP <input type="file" accept=".zip" :disabled="blocked" @change="choose"></label>
   <button :disabled="blocked||!file" @click="inspect">{{pending==='upload'?'正在读取备份…':'预览备份'}}</button></div>
  <p v-if="preview" class="note">{{file?.name}} · {{number(preview.files,0)}} 个文件 · 解压后 {{number(preview.bytes/1048576,1)}} MiB
   <button :disabled="blocked" @click="restore">确认恢复并在重启后使用</button></p>
  <p v-if="error" class="note warning" role="alert">{{error}}</p>
  <p v-else-if="restored" class="note" role="status">恢复完成。退出平台服务，再从桌面快捷方式启动，即可进入恢复后的工作区。</p>
  <p v-else-if="record&&['queued','running','stopping'].includes(record.status)" class="note" role="status">正在处理：{{number(record.progress,0)}}% · {{record.name}}</p>
  <p v-else-if="record&&['failed','cancelled','interrupted'].includes(record.status)" class="note warning" role="alert">{{record.message}}</p>
  <p v-else-if="message" class="note" role="status">{{message}}</p>
 </section>
</template>
