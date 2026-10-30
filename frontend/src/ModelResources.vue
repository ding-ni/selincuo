<script setup>
import {ref,computed,watch,onMounted} from 'vue'
import DataImport from './DataImport.vue'
import {api,number} from './api.mjs'
import {variableDefinitions} from './business-view.mjs'
const props=defineProps({boot:Object,busy:Boolean,active:Array})
const emit=defineEmits(['navigate','refresh','error','submit','station'])
const tab=ref('forcing'),catalog=ref(null),coverage=ref(null),files=ref([]),station=ref('SLC'),working=ref(false),message=ref(''),startDate=ref(''),endDate=ref('')
const activeUpdate=computed(()=>(props.active||[]).find(t=>t.kind==='update-forcing'))
const label=v=>variableDefinitions.find(x=>x.id===v)?.label||v
async function imported(){
 emit('refresh')
 try{catalog.value=await api('observation-catalog')}catch(e){emit('error',e.message)}
}
async function checkCoverage(){
 working.value=true
 try{coverage.value=await api('forcing-coverage?check=true')}catch(e){emit('error',e.message)}finally{working.value=false}
}
function update(range=false){
 message.value=''
 if(range&&(!startDate.value||!endDate.value||endDate.value<startDate.value)){message.value='请选择有效的起止日期。';return}
 emit('submit',{kind:'update-forcing',...(range?{startDate:startDate.value,endDate:endDate.value}:{})})
}
async function upload(){
 working.value=true;message.value=''
 try{
  const uploaded=[]
  for(const file of files.value){
   const response=await fetch('/api/model/upload?'+new URLSearchParams({filename:file.name,station:station.value}),{method:'PUT',body:file})
   const data=await response.json();if(!response.ok)throw Error(data.error);uploaded.push('incoming/'+data.filename)
  }
  emit('submit',{kind:'import-observations'})
  message.value='资料已上传，正在校验和登记。';files.value=[]
 }catch(e){emit('error',e.message)}finally{working.value=false}
}
onMounted(async()=>{
 const results=await Promise.allSettled([api('observation-catalog'),api('forcing-coverage')])
 if(results[0].status==='fulfilled')catalog.value=results[0].value
 if(results[1].status==='fulfilled')coverage.value=results[1].value
})
watch(()=>props.boot,async()=>{try{coverage.value=await api('forcing-coverage')}catch(e){emit('error',e.message)}})
</script>
<template>
 <nav class="workspace-tabs"><button v-for="t in [['forcing','模型气象资料'],['observations','观测资料']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <template v-if="tab==='forcing'">
  <section class="panel">
   <div class="panel-title"><h3>更新气象资料</h3><span class="badge">ERA5-Land</span><button :disabled="working||busy" @click="checkCoverage">{{working?'检查中…':'检查官方可用范围'}}</button></div>
   <div class="resource-summary"><div><span>当前可用的完整日</span><b>{{coverage?.current?.complete?coverage.current.firstCompleteDay+' 至 '+coverage.current.lastCompleteDay:'尚未确认'}}</b><small>{{coverage?.current?.complete?'降水、气温和蒸发能力完整':'请检查或补充气象资料'}}</small></div><div><span>官方目录</span><b>{{coverage?.catalog?.availableEndUTC?.slice(0,10)||'尚未检查'}}</b><small>以三变量实际覆盖校验为准</small></div></div>
   <p v-if="coverage?.current&&!coverage.current.complete" class="note warning">{{coverage.current.message}}</p>
   <p class="muted">按需下载、续传并登记气象资料；已有成果继续使用各自的输入。</p>
   <div class="query-grid"><label>补充起日<input type="date" v-model="startDate"></label><label>补充止日<input type="date" v-model="endDate"></label><button :disabled="working||busy||active.length>0" @click="update(true)">更新所选时段</button><button class="primary" :disabled="working||busy||active.length>0" @click="update(false)">更新至可用最新日期</button></div>
   <p v-if="message" class="note warning" role="status">{{message}}</p>
   <p v-if="activeUpdate" class="note">{{activeUpdate.message||'正在更新气象资料'}} · {{number(activeUpdate.progress,1)}}%</p>
   <p v-if="coverage?.update?.status&&coverage.update.status!=='succeeded'" class="note">上次更新：{{coverage.update.message||coverage.update.status}}</p>
   <p class="note">如提示 CDS 账户或许可问题，可查看 <a href="/api/model/cds-help">账户配置说明</a>。</p>
   <div class="actions"><button @click="emit('navigate','simulation')">前往历史模拟 →</button><button @click="emit('navigate','calibration')">前往方案与率定 →</button></div>
  </section>
 </template>
 <template v-if="tab==='observations'">
  <section class="panel"><div class="panel-title"><h3>更新观测资料</h3><button class="link" @click="emit('navigate','monitor')">查询监测资料 →</button></div>
   <DataImport :stations="boot.stations" :disabled="busy||working||active.length>0" @imported="imported" @error="emit('error',$event)"/>
   <details><summary>导入原格式观测表</summary><div class="result-toolbar"><label>所属站点<select v-model="station"><option v-for="s in boot.stations.filter(s=>['SLC','DUJA','BLQ','115DB'].includes(s.id))" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label class="grow">Excel 文件<input type="file" multiple accept=".xls,.xlsx" @change="files=Array.from($event.target.files)"></label><button :disabled="working||busy||active.length>0||!files.length" @click="upload()">导入</button></div></details>
   <details><summary>站点降水的用途</summary><p class="note">当前采用成果保留原驱动：主站2024年降水用于湖面降水比例订正。本站雨量同时用于陆域暖时降雨订正对照，水文及湖泊参数保持不变。</p><template v-if="coverage?.precipitationCorrection"><p class="note">独立订正对照已生成；主站、杜加、巴勒切用于具备足够样本的季节估计，115道班参与资料对照。冬季降水保持原驱动。</p><p>对照输入 {{coverage.precipitationCorrection.inputId}} · {{coverage.precipitationCorrection.createdAt?.slice(0,10)}}</p><a v-if="coverage.precipitationCorrection.reportUrl" class="button" :href="coverage.precipitationCorrection.reportUrl" download>下载降水订正对照</a><button v-if="coverage.precipitationCorrection.comparisonTaskId" class="link" @click="emit('navigate','results',coverage.precipitationCorrection.comparisonTaskId)">查看固定参数试算 →</button></template><p v-else class="muted">订正输入和对照成果另存，完成登记后在此提供。</p></details>
   <div class="actions"><button @click="emit('navigate','simulation')">前往历史模拟 →</button><button @click="emit('navigate','calibration')">前往方案与率定 →</button></div>
  </section>
  <section class="panel"><div class="panel-title"><h3>站点资料范围</h3></div><div class="table-wrap"><table><thead><tr><th>站点 / 要素</th><th>资料时段</th><th>有值记录</th><th>状态</th></tr></thead><tbody><template v-for="s in catalog?.stations||[]" :key="s.id"><tr v-for="v in s.variables" :key="v.id"><td><button class="link" @click="emit('station',s.id)">{{s.name}} · {{label(v.id)}}</button></td><td>{{v.start.slice(0,10)}} — {{v.end.slice(0,10)}}</td><td>{{v.valid.toLocaleString()}}</td><td>{{s.kind==='lake'?(s.id==='SLC'?'主站工作基准':'基准待核'):'观测资料'}}</td></tr></template></tbody></table></div></section>
 </template>
</template>
<style scoped>.resource-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:18px 0}.resource-summary>div{display:grid;gap:4px;padding:16px;background:#f3f7f8;border-radius:6px}.resource-summary span,.resource-summary small{font-size:12px;color:var(--muted)}.resource-summary b{font-size:19px;font-variant-numeric:tabular-nums}@media(max-width:700px){.resource-summary{grid-template-columns:1fr}}</style>
