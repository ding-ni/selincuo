<script setup>
import {ref,reactive,computed,onMounted,onBeforeUnmount,watch} from 'vue'
import Dashboard from './Dashboard.vue'
import MonitoringWorkspace from './MonitoringWorkspace.vue'
import CalibrationWorkspace from './CalibrationWorkspace.vue'
import HistoricalSimulation from './HistoricalSimulation.vue'
import HistoricalResult from './HistoricalResult.vue'
import ResultWorkspace from './ResultWorkspace.vue'
import ModelStates from './ModelStates.vue'
import ModelResources from './ModelResources.vue'
import SystemWorkspace from './SystemWorkspace.vue'
import HelpCenter from './HelpCenter.vue'
import {historyDefaults,historyIssue,historyRequest,readHistory} from './history-workflow.mjs'
import {api,number,resultName} from './api.mjs'
const route=new URLSearchParams(location.search)
const mode=ref(route.get('mode')==='expert'?'expert':'business'),selectedId=ref(route.get('result')||''),viewDay=ref(route.get('date')||''),context=ref(null),tasksOpen=ref(false)
const visiblePages=computed(()=>pages.filter(p=>mode.value==='expert'||!['calibration','process'].includes(p[0])))
const selectedRole=computed(()=>context.value?.selectedResult?.role)
const roleLabel=computed(()=>({'current-default':'当前默认','research-candidate':'研究候选','historical-release':'历史发布','user-result':'用户成果'})[selectedRole.value]||'成果')
const pages=[['overview','◈','湖情总览'],['monitor','≋','监测资料'],['calibration','⌘','模型方案与率定'],['simulation','▷','历史模拟'],['results','▥','成果中心'],['process','⊞','状态与空间诊断'],['model','◇','资料与更新'],['system','⚙','运行与维护']]
const page=ref(pages.some(p=>p[0]===route.get('page'))?route.get('page'):'overview'),boot=ref(null),tasks=ref([]),states=ref([]),system=ref(null),detail=ref(null),historyDetail=ref(null),calibrationDetail=ref(null),error=ref(''),notice=ref(''),busy=ref(false),collapsed=ref(window.innerWidth<1280),monitorInitial=ref(null),stateResultId=ref(''),continuation=ref(null),restorePreview=ref(null),restoreFilename=ref(''),backupUploadProgress=ref(null)
const savedWindow=readHistory(localStorage)
const historyRun=reactive({name:savedWindow.name||'',start:savedWindow.start,end:savedWindow.end})
const continuationSources=ref([]),historyPlan=ref(null),resultWindow=ref(null)
function continuationInput(){const selected=boot.value.inputs.find(i=>i.id===continuation.value.inputId);if(selected)continuation.value.end=selected.manifest.end.slice(0,16)}
const active=computed(()=>tasks.value.filter(t=>['queued','running','stopping'].includes(t.status))),resultRecords=computed(()=>tasks.value.filter(t=>['simulation','continuation','calibration'].includes(t.kind))),title=computed(()=>pages.find(p=>p[0]===page.value)?.[2]||'使用帮助')
let timer,historyTicket,contextTicket=0,historyInitialized=false,manualNavigation=false
function toggleNavigation(){manualNavigation=true;collapsed.value=!collapsed.value}
function resizeNavigation(){if(!manualNavigation)collapsed.value=window.innerWidth<1280}
function syncUrl(replace=false){const url=new URL(location.href);url.searchParams.set('page',page.value);url.searchParams.set('mode',mode.value);if(selectedId.value)url.searchParams.set('result',selectedId.value);else url.searchParams.delete('result');if(viewDay.value)url.searchParams.set('date',viewDay.value);else url.searchParams.delete('date');history[replace?'replaceState':'pushState']({},'',url)}
async function loadContext(){
 const ticket=++contextTicket
 try{
  const next=await api('business-context'+(selectedId.value?'?resultId='+encodeURIComponent(selectedId.value):''))
  if(ticket!==contextTicket)return
  context.value=next
  if(boot.value){
   const standard=next.defaultResult||(next.selectedResult.role==='current-default'?next.selectedResult:await api('result-summary?resultId='+encodeURIComponent(next.release.resultId)))
   if(ticket!==contextTicket)return
   boot.value.standardResultId=next.release.resultId
   boot.value.defaultResultInputId=standard.inputId
   const draft=historyInitialized?historyRun:{name:historyRun.name,start:historyRun.start,end:historyRun.end,inputId:standard.inputId}
   Object.assign(historyRun,historyDefaults(boot.value,draft));historyInitialized=true
  }
  if(!selectedId.value)selectedId.value=next.release.resultId
 }catch(e){if(ticket===contextTicket)error.value='业务上下文读取失败：'+e.message}
}
async function selectContext(id,day,urlMode='replace'){if(id&&id!==selectedId.value){selectedId.value=id;await loadContext()}if(day)viewDay.value=day;if(urlMode!=='none')syncUrl(urlMode!=='push')}
function changeMode(){if(mode.value==='business'&&['calibration','process'].includes(page.value))page.value='overview';syncUrl()}
async function restoreRoute(){const q=new URLSearchParams(location.search);page.value=pages.some(p=>p[0]===q.get('page'))?q.get('page'):'overview';mode.value=q.get('mode')==='expert'?'expert':'business';selectedId.value=q.get('result')||boot.value?.standardResultId||'';viewDay.value=q.get('date')||'';await loadContext();if(page.value==='results')await action(()=>openResult(selectedId.value));if(page.value==='process')stateResultId.value=selectedId.value}

const resultTickets={detail:0,history:0,calibration:0}
async function action(fn){busy.value=true;error.value='';try{return await fn()}catch(e){error.value=e.message}finally{busy.value=false}}
async function refresh(){
 const responses=await Promise.allSettled([api('bootstrap'),api('tasks'),api('states')])
 const [b,t,s]=responses
 if(b.status==='fulfilled')boot.value=b.value;else if(!boot.value)throw b.reason
 if(t.status==='fulfilled')tasks.value=t.value.tasks
 if(s.status==='fulfilled')states.value=s.value.states
 const failed=responses.filter(r=>r.status==='rejected')
 if(failed.length)error.value='部分内容读取失败：'+failed.map(r=>r.reason.message).join('；')
 await loadContext()
 if(!historyDetail.value&&boot.value.standardResultId)await openResult(boot.value.standardResultId,'history')
 if(page.value==='system')system.value=await api('system')
}
async function openResult(id,target='detail'){if(!id)return;const ticket=++resultTickets[target];const value=await api(boot.value.publishedResults.some(r=>r.id===id)?'published-result?id='+id:'tasks/'+id);if(ticket!==resultTickets[target])return;if(target==='calibration')calibrationDetail.value=value;else if(target==='history')historyDetail.value=value;else{detail.value=value;if(value.status==='succeeded')await selectContext(id,undefined,id!==selectedId.value?'push':'replace')}return value}
async function navigate(p,id){
 error.value='';if(id)await selectContext(id,undefined,'none');page.value=p;if(['calibration','process'].includes(p))mode.value='expert';syncUrl()
 if(p==='results')await action(()=>openResult(id||selectedId.value||boot.value.standardResultId))
 if(p==='system')await action(async()=>{system.value=await api('system')})
 if(p==='process')stateResultId.value=id||selectedId.value
}
async function submit(request){
 if(busy.value)return
 await action(async()=>{const task=await api('tasks',request);if(['calibration-workflow','calibration','trial','sensitivity'].includes(request.kind))calibrationDetail.value=task;else if(request.kind==='continuation'){detail.value=task;notice.value='续算已开始'}else if(request.kind==='simulation')historyDetail.value=task;else notice.value='资料处理已提交';await refresh()})
}
async function submitHistory(){
 if(historyPlan.value?.action==='reuse'){resultWindow.value={id:historyPlan.value.resultId,start:historyRun.start.slice(0,10),end:new Date(Date.parse(historyRun.end+'Z')-86400000).toISOString().slice(0,10)};await navigate('results',historyPlan.value.resultId);return}
 if(!historyPlan.value||historyPlan.value.action==='blocked'){error.value='请先完成运行计划检查。';return}
 const issue=historyIssue(historyRun,boot.value,active.value.length>0);if(issue){error.value=issue;return}
 const name=historyRun.name.trim()||'色林错湖位与入湖径流模拟成果（'+historyRun.start.slice(0,10)+'—'+new Date(Date.parse(historyRun.end+'Z')-86400000).toISOString().slice(0,10)+'）'
 const request={...historyRequest(historyRun,''),name};const signature=JSON.stringify(request);if(historyTicket?.signature!==signature)historyTicket={signature,id:crypto.randomUUID()}
 request.clientRequestId=historyTicket.id;await submit(request);if(!error.value)historyTicket=null
}
async function stop(id){await action(async()=>{await api('tasks/'+id+'/stop',{});await refresh()})}
async function metadata(t,change){await action(async()=>{await api('tasks/'+t.id+'/metadata',change);await refresh()})}
async function saveParameters(t){const name=prompt('候选参数名称',t.name||'率定参数');if(!name?.trim())return;await action(async()=>{await api('parameters',{name:name.trim(),payload:{parameters:t.result.parameters,partitionParameters:t.result.partitionParameters||{},sourceTaskId:t.id,provenance:'率定候选'}});await refresh();notice.value='候选参数已保存'})}
function showState(t){if(!t)return;stateResultId.value=t.id;navigate('process',t.id)}
async function resume(s){await action(async()=>{const state=(await api('states/'+s.id)).state;const inputId=state.inputId||state.identity.inputId;const original=boot.value.inputs.find(i=>i.id===inputId);if(!original)throw Error('状态对应气象输入不存在');continuationSources.value=state.schemaVersion===3?boot.value.inputs.filter(i=>i.manifest.start<=s.valid_at&&i.manifest.end>s.valid_at&&i.manifest.transformationSHA256===state.identity.forcingTransformationSHA256).sort((a,b)=>b.manifest.end.localeCompare(a.manifest.end)):[original];const input=continuationSources.value[0]||original;continuation.value={kind:'continuation',name:'状态续算',stateId:s.id,inputId:input.id,parameters:state.parameters,partitionParameters:state.partitionParameters||{},routingMode:state.identity.routingMode,start:new Date(Date.parse(s.valid_at+'Z')+3600000).toISOString().slice(0,16),end:input.manifest.end.slice(0,16)}})}
async function renameState(s){const name=prompt('状态名称',s.name||'');if(name===null)return;await action(async()=>{await api('states/'+s.id,{name,favorite:true});await refresh()})}
async function backup(){await submit({kind:'backup',name:'工作区完整备份',clientRequestId:crypto.randomUUID()});if(!error.value){notice.value='备份已进入后台队列，完成后可在任务中心下载。';tasksOpen.value=true}}
async function exportResult(id,format){
 await submit({kind:'export-result',taskId:id,format,name:'成果导出 · '+format,clientRequestId:crypto.randomUUID()})
 if(!error.value){notice.value='导出任务已提交，完成文件可在任务中心下载。';tasksOpen.value=true}
}
const taskMessage=t=>t.status==='failed'?t.message:t.message?.startsWith('备份 ')?'正在归档工作区文件':t.message?.startsWith('压缩逐时变量')?'正在生成逐时成果文件':t.message?.startsWith('校验')?'正在核验文件完整性':t.message
const beijingTime=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—'
async function previewBackupTask(id){await action(async()=>{restoreFilename.value='';restorePreview.value=await api('restore-preview',{backupTaskId:id})})}
async function uploadBackup(file){await action(async()=>{
 backupUploadProgress.value=0
 try{
  const data=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('PUT','/api/model/upload?filename='+encodeURIComponent(file.name));xhr.upload.onprogress=e=>{if(e.lengthComputable)backupUploadProgress.value=e.loaded/e.total*100};xhr.onerror=()=>reject(Error('备份上传中断，请重试'));xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);xhr.status>=200&&xhr.status<300?resolve(data):reject(Error(data.error||'上传失败'))}catch{reject(Error('备份上传响应无法读取'))}};xhr.send(file)})
  restoreFilename.value=data.filename;restorePreview.value=await api('restore-preview',{filename:data.filename})
 }finally{backupUploadProgress.value=null}
})}
async function restore(){const preview=restorePreview.value;if(!preview)return;await submit({kind:'restore-workspace',name:'恢复备份至独立目录',token:preview.token,...(preview.backupTaskId?{backupTaskId:preview.backupTaskId}:{filename:preview.filename||restoreFilename.value}),clientRequestId:crypto.randomUUID()});if(!error.value){notice.value='恢复已进入后台队列；完整校验成功后才会切换工作区。';restorePreview.value=null;tasksOpen.value=true}}
function dashboardStation(id,day,comparison=false){monitorInitial.value={station:id,variable:boot.value.stations.find(s=>s.id===id)?.kind==='rain'?'precipitation':id==='DUJA'?'flow':'level',day,comparison};navigate('monitor')}
watch(historyRun,()=>{if(boot.value)localStorage.setItem('selinco:history:v1',JSON.stringify({name:historyRun.name,start:historyRun.start,end:historyRun.end}))},{deep:true})
watch(collapsed,v=>localStorage.setItem('selinco:nav-collapsed',String(v)))
onMounted(async()=>{window.addEventListener('popstate',restoreRoute);window.addEventListener('resize',resizeNavigation);await action(async()=>{await refresh();if(page.value==='results')await openResult(selectedId.value||boot.value.standardResultId);syncUrl(true)});timer=setInterval(async()=>{if(!active.value.length||busy.value)return;try{const ids=active.value.map(t=>t.id);tasks.value=(await api('tasks')).tasks;for(const [r,key] of [[historyDetail,'history'],[calibrationDetail,'calibration'],[detail,'detail']])if(r.value&&ids.includes(r.value.id))await openResult(r.value.id,key);if(ids.some(id=>!active.value.some(t=>t.id===id)))await refresh()}catch(e){error.value=e.message}},2500)})
onBeforeUnmount(()=>{clearInterval(timer);window.removeEventListener('popstate',restoreRoute);window.removeEventListener('resize',resizeNavigation)})
</script>
<template>
 <div class="app-shell" :class="{'dashboard-host':page==='overview','nav-collapsed':collapsed}">
 <aside class="sidebar"><div class="brand"><span class="brand-mark">◈</span><div>色林错<span>预警预报平台</span></div></div><button class="nav-collapse" :aria-label="collapsed?'展开导航':'收起导航'" @click="toggleNavigation">{{collapsed?'☰':'☰ 收起导航'}}</button>
 <nav aria-label="业务导航"><button v-for="p in visiblePages" :key="p[0]" :title="p[2]" :aria-label="p[2]" :class="{selected:page===p[0]}" @click="navigate(p[0])"><span>{{p[1]}}</span><b>{{p[2]}}</b></button></nav>
 <div class="support-navigation"><label class="mode-switch">工作模式<select v-model="mode" @change="changeMode"><option value="business">业务模式</option><option value="expert">专家模式</option></select></label><button @click="tasksOpen=true"><span>☷</span><b>任务中心 · {{active.length}}</b></button><button title="使用帮助" @click="navigate('help')"><span>?</span><b>使用帮助</b></button></div></aside>
 <main><header v-if="page!=='overview'"><div><span class="breadcrumb">色林错流域</span><h1>{{title}}</h1></div><div class="header-right"><span class="pill">{{boot?'服务已连接':'正在连接'}}</span><button @click="action(refresh)">刷新</button></div></header>
 <div class="content"><div v-if="error" class="message error" role="alert">{{error}}<button aria-label="关闭错误" @click="error=''">×</button></div><div v-if="notice" class="message success" role="status">{{notice}}<button aria-label="关闭提示" @click="notice=''">×</button></div>
 <div v-if="!boot" class="panel empty">正在读取工作区…</div><template v-else>
 <section v-if="context" class="business-context" :class="{'candidate-context':selectedRole==='research-candidate'}" aria-label="统一业务上下文"><div><span class="context-role">{{roleLabel}}</span><b>{{context.selectedResult.scheme?.name||context.selectedResult.name}}</b><small>修订 {{context.selectedResult.scheme?.revision||'—'}} · {{context.selectedResult.evaluation.label}}</small></div><div><span>成果覆盖</span><b>{{context.selectedResult.displayCoverage.start?.slice(0,10)}} — {{context.selectedResult.displayCoverage.end?.slice(0,10)}}</b></div><div><span>气象完整日 / 主站有效日</span><b>{{context.forcing.latest?.lastCompleteDay||'无'}} / {{context.observations.mainStation.lastValidDay||'无'}}</b></div><button v-if="selectedId!==context.release.resultId" @click="selectContext(context.release.resultId).then(()=>navigate(page))">返回默认成果</button></section>
 <div v-if="active.length&&page!=='overview'" class="running-strip"><span>{{active[0].name||'后台任务'}}</span><progress :value="active[0].progress" max="100"></progress><span>{{number(active[0].progress,1)}}%</span><button @click="stop(active[0].id)">停止</button></div>
 <Dashboard v-if="page==='overview'" :boot="boot" :tasks="tasks" :active="active" :selected-id="selectedId" :view-day="viewDay" :summary="context?.selectedResult" @context="selectContext" @navigate="navigate" @result="navigate('results',$event)" @station="dashboardStation"/>
 <MonitoringWorkspace v-if="page==='monitor'" :initial="monitorInitial" @navigate="navigate"/>
 <CalibrationWorkspace v-if="page==='calibration'" :boot="boot" :tasks="tasks" :detail="calibrationDetail" :busy="busy" :active="active" @refresh="action(refresh)" @submit="submit" @stop="stop" @select="action(()=>openResult($event,'calibration'))" @save-parameters="saveParameters" @state="showState(calibrationDetail)"/>
 <HistoricalSimulation v-if="page==='simulation'" :boot="boot" :run="historyRun" :expert="mode==='expert'" @plan="historyPlan=$event" :busy="busy" :active="active" @submit="submitHistory" @research="submit" @navigate="navigate"><HistoricalResult :detail="historyDetail" :busy="busy" @stop="stop" @export="exportResult" @state="showState(historyDetail)" @retry="submitHistory"/></HistoricalSimulation>
 <ResultWorkspace :evaluation-tasks="tasks" :evaluation-enabled="mode==='expert'&&context?.capabilities?.evaluationArchiveWriterAvailable" :release-id="context?.release?.id" @submit="submit" v-if="page==='results'" :tasks="resultRecords" :published="boot.publishedResults" :standard-id="boot.standardResultId" :selected-id="detail?.id" :busy="busy" @select="action(()=>openResult($event))" @metadata="metadata" @stop="stop"><HistoricalResult :detail="detail" :view-window="resultWindow?.id===detail?.id?resultWindow:null" :busy="busy" @stop="stop" @export="exportResult" @state="showState(detail)" @retry="navigate('simulation')"/></ResultWorkspace>
 <ModelStates v-if="page==='process'" :boot="boot" :tasks="tasks" :states="states" :initial-id="selectedId||boot.standardResultId" :view-day="viewDay" @context="selectContext" :busy="busy" @resume="resume" @rename="renameState"><div v-if="continuation" class="result-toolbar"><label class="grow">续算驱动（历史前缀须通过核验）<select v-model="continuation.inputId" @change="continuationInput"><option v-for="i in continuationSources" :key="i.id" :value="i.id">{{i.name}} · {{i.manifest.end.slice(0,16)}}</option></select></label><label>续算起点<input type="datetime-local" :value="continuation.start" readonly></label><label>结束时刻<input type="datetime-local" v-model="continuation.end"></label><button class="primary" :disabled="busy||active.length>0||continuation.end<continuation.start" @click="submit(continuation)">开始续算</button><p v-if="!continuationSources.length" class="note">暂无更晚的兼容驱动，请先更新气象资料。</p></div></ModelStates>
 <ModelResources v-if="page==='model'" :boot="boot" :active="active" :busy="busy" @navigate="navigate" @refresh="action(refresh)" @error="error=$event" @submit="submit" @station="dashboardStation"/>
 <SystemWorkspace v-if="page==='system'" :boot="boot" :system="system" :tasks="tasks" :busy="busy" :active="active" :upload-progress="backupUploadProgress" @preview-backup="previewBackupTask" :restore-preview="restorePreview" @navigate="navigate" @backup="backup" @upload-backup="uploadBackup" @restore="restore" @rebuild="submit({kind:'build-spatial'})" @shutdown="action(async()=>{await api('shutdown',{});notice='服务已退出'})"/>
 <HelpCenter v-if="page==='help'" :boot="boot" :system="system" @navigate="navigate"/>
 </template></div></main>
 <div v-if="tasksOpen" class="task-shade" @click.self="tasksOpen=false"><aside class="task-drawer" role="dialog" aria-modal="true" aria-label="全局任务中心"><div class="panel-title"><h2>任务中心</h2><button aria-label="关闭任务中心" @click="tasksOpen=false">×</button></div><p class="muted">后台任务执行时可继续查询资料和浏览已有成果。</p><article v-for="t in tasks.slice(0,30)" :key="t.id" class="task-item"><b>{{t.name||t.kind}}</b><span>{{({queued:'排队',running:'运行中',stopping:'正在停止',succeeded:'完成',failed:'失败',cancelled:'停止',interrupted:'中断'})[t.status]||t.status}} · {{number(t.progress,1)}}%</span><progress v-if="['queued','running','stopping'].includes(t.status)" :value="t.progress" max="100"></progress><p>{{taskMessage(t)}}</p><details v-if="taskMessage(t)!==t.message"><summary>处理详情</summary><p>{{t.message}}</p></details><small>更新 {{beijingTime(t.heartbeatAt||t.updated_at)}} · 北京时间</small><span v-if="t.termination" class="warning">{{({'completed':'任务完成','user-stopped':'用户停止','service-restarted':'服务重启后中断','failed':'执行失败'})[t.termination]||t.termination}}</span><a v-for="file in t.result?.artifacts||[]" :key="file.name" class="button" :href="file.downloadUrl">下载 {{file.name}} · {{number(file.bytes/1048576,1)}} MiB</a><p v-if="t.result?.restartRequired" class="note warning">恢复与摘要校验成功。退出并重新启动平台后使用新工作区，原工作区保留。</p><button v-if="['queued','running'].includes(t.status)" @click="stop(t.id)">停止任务</button><button v-if="t.status==='succeeded'&&['simulation','continuation','calibration'].includes(t.kind)" @click="navigate('results',t.id);tasksOpen=false">查看成果</button></article><p v-if="!tasks.length">暂无任务。</p></aside></div></div>
</template>
