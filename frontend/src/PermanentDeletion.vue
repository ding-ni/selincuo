<script setup>
import {computed, nextTick, ref, watch} from 'vue'
import {api, number} from './api.mjs'

const props = defineProps({objects: {type: Array, default: () => []}, busy: Boolean})
const emit = defineEmits(['changed', 'restore'])
const chosen = ref([]), plan = ref(null), dialog = ref(null), working = ref(false), error = ref(''), stale = ref(false)
const identity = object => object.kind + ':' + object.id
const labels = {task: '成果', scheme: '方案', parameter: '参数'}
const selected = computed(() => props.objects.filter(object => chosen.value.includes(identity(object))))
const related = computed(() => {
 if (!plan.value || plan.value.resume) return []
 const included = new Set(plan.value.objects.map(identity)), unique = new Map()
 for (const item of plan.value.items) for (const reason of item.blocked || []) {
  const object = reason.related
  if (object?.trashed && !included.has(identity(object))) unique.set(identity(object), object)
 }
 return [...unique.values()]
})
const isSelected = object => chosen.value.includes(identity(object))
function toggle(object) {
 const key = identity(object)
 chosen.value = isSelected(object) ? chosen.value.filter(value => value !== key) : [...chosen.value, key]
}
watch(() => props.objects, objects => {
 const available = new Set(objects.map(identity))
 chosen.value = chosen.value.filter(value => available.has(value))
}, {deep: true})
function size(value) {
 if (!value) return '0 B'
 if (value < 1024) return number(value, 0) + ' B'
 if (value < 1024 ** 2) return number(value / 1024, 1) + ' KiB'
 if (value < 1024 ** 3) return number(value / 1024 ** 2, 1) + ' MiB'
 return number(value / 1024 ** 3, 2) + ' GiB'
}
async function showPreview(objects) {
 if (!objects.length || working.value || props.busy) return
 working.value = true; error.value = ''; stale.value = false
 try {
  const request = objects.map(({kind, id}) => ({kind, id}))
  plan.value = await api('permanent-delete/preview', {objects: request})
  await nextTick()
  if (dialog.value && !dialog.value.open) dialog.value.showModal()
 } catch (e) { error.value = e.message }
 finally { working.value = false }
}
function close() {
 if (working.value) return
 dialog.value?.close()
 plan.value = null; stale.value = false
}
async function execute() {
 if (!plan.value || !plan.value.deletableCount || working.value || stale.value) return
 working.value = true; error.value = ''
 try {
  const result = await api('permanent-delete/execute', {objects: plan.value.objects, token: plan.value.token})
  if (result.status === 'partial') {
   plan.value = {...plan.value, ...result, resume: true}
   return
  }
  if (result.status !== 'deleted') throw Error('删除未完成，请重新预览')
  const removed = new Set(result.deletedObjects.map(identity))
  chosen.value = chosen.value.filter(key => !removed.has(key))
  dialog.value?.close(); plan.value = null
  emit('changed', result)
 } catch (e) {
  error.value = e.message
  stale.value = true
 } finally { working.value = false }
}
async function refreshPreview() {
 const objects = plan.value?.objects || []
 dialog.value?.close(); plan.value = null
 await showPreview(objects)
}
async function includeRelated() {
 const objects = [...plan.value.objects, ...related.value]
 dialog.value?.close(); plan.value = null
 await showPreview(objects)
}
</script>

<template>
 <div class="permanent-deletion">
  <slot name="rows" :is-selected="isSelected" :toggle="toggle">
   <ul v-if="objects.length" class="recycle-list">
    <li v-for="object in objects" :key="identity(object)">
     <label><input type="checkbox" :checked="isSelected(object)" :disabled="busy||working" @change="toggle(object)">
      <span>{{object.name}} <small>{{labels[object.kind]}}</small></span></label>
     <button type="button" :disabled="busy||working" @click="emit('restore',object)">恢复</button>
    </li>
   </ul>
   <p v-else class="muted">回收站为空。</p>
  </slot>
  <div v-if="objects.length" class="recycle-actions">
   <span>已选择 {{selected.length}} 项</span>
   <button type="button" :disabled="!selected.length||busy||working" @click="showPreview(selected)">选中后彻底删除</button>
   <button type="button" :disabled="busy||working" @click="showPreview(objects)">清空回收站</button>
  </div>
  <p v-if="error&&!plan" class="message error" role="alert">{{error}}</p>
  <Teleport to="body">
   <dialog v-if="plan" ref="dialog" class="deletion-dialog" aria-labelledby="deletion-title" @cancel.prevent="close">
    <div class="deletion-head"><h3 id="deletion-title">{{plan.resume?'继续彻底删除':'确认彻底删除'}}</h3><button type="button" aria-label="关闭" :disabled="working" @click="close">×</button></div>
    <p class="deletion-warning">彻底删除后无法恢复。原始观测、共享气象和空间资料不会随这些对象删除。</p>
    <p>将删除 {{plan.deletableCount}} 项<span v-if="plan.blockedCount">；{{plan.blockedCount}} 项因保护或依赖保留</span>。</p>
    <ul class="deletion-objects">
     <li v-for="item in plan.items" :key="item.kind+':'+item.id">
      <strong>{{item.name}}</strong><span class="deletion-kind">{{labels[item.kind]}}</span>
      <span v-if="item.deletable" class="deletion-status">可删除</span>
      <div v-else class="deletion-blocked"><span>保留</span><div v-for="reason in item.blocked" :key="reason.message">{{reason.message}}<template v-if="reason.action">；{{reason.action}}</template></div></div>
     </li>
    </ul>
    <p v-if="related.length" class="related-choice">{{related.length}} 项关联对象已在回收站，可以一同选择并重新预览。 <button type="button" :disabled="working" @click="includeRelated">一并选择关联对象</button></p>
    <div class="deletion-files"><strong>独占文件范围</strong>
     <p v-if="plan.fileScopes.length">{{plan.fileCount}} 个文件，预计释放 {{size(plan.estimatedReclaimBytes)}}<template v-if="plan.ownedExportCount">，含 {{plan.ownedExportCount}} 项关联导出</template>。</p>
     <p v-else>仅移除所选方案或参数的记录，无独占成果文件。</p>
     <ul v-if="plan.fileScopes.length"><li v-for="scope in plan.fileScopes" :key="scope"><code>{{scope}}</code></li></ul>
     <small>共享硬链接按其他引用保留；释放空间为估计值。</small>
    </div>
    <p v-if="plan.resume" class="deletion-warning" role="status">上次删除未完成<span v-if="plan.deletedFileCount!=null">：已删除 {{plan.deletedFileCount}} 个文件，剩余 {{plan.remainingFileCount}} 个</span>。对象索引仍保留，请处理原因后重试。</p>
    <p v-if="plan.error" class="message error" role="alert">{{plan.error}}</p>
    <p v-if="error" class="message error" role="alert">{{error}}</p>
    <div class="deletion-actions"><button type="button" :disabled="working" @click="close">取消</button>
     <button v-if="stale" type="button" :disabled="working" @click="refreshPreview">重新预览</button>
     <button v-else type="button" class="danger" :disabled="working||!plan.deletableCount" @click="execute">{{working?'正在处理…':plan.resume?'重试彻底删除':'彻底删除 '+plan.deletableCount+' 项'}}</button>
    </div>
   </dialog>
  </Teleport>
 </div>
</template>

<style scoped>
.recycle-list{list-style:none;margin:12px 0;padding:0;border-top:1px solid var(--line)}
.recycle-list li{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid var(--line)}
.recycle-list label{display:flex;align-items:center;gap:10px;min-width:0}.recycle-list small,.deletion-kind{color:var(--muted,#66737d);margin-left:8px}
.recycle-actions,.deletion-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:14px 0}.recycle-actions span{margin-right:auto;color:var(--muted,#66737d)}
.deletion-dialog{width:min(680px,calc(100vw - 32px));max-height:min(85vh,850px);overflow:auto;border:1px solid var(--line,#d1d9dc);border-radius:8px;background:var(--panel,#fff);color:var(--ink,#182a33);padding:22px;box-shadow:0 18px 56px #0005}
.deletion-dialog::backdrop{background:#10242db3}.deletion-head{display:flex;align-items:center;justify-content:space-between;gap:20px}.deletion-head h3{margin:0}.deletion-warning{padding:10px 12px;border-left:3px solid #bd6343;background:#fff2eb;color:#6e2b19}
.deletion-objects{list-style:none;padding:0;margin:12px 0;border-top:1px solid var(--line,#d1d9dc)}.deletion-objects li{padding:9px 0;border-bottom:1px solid var(--line,#d1d9dc)}.deletion-status{float:right;color:#24715b}.deletion-blocked{color:#935129;margin-top:6px}.deletion-blocked span{font-weight:700;margin-right:8px}.deletion-blocked div{margin-top:3px}
.related-choice{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.deletion-files{padding:12px 14px;background:var(--surface,#f3f6f5);border-radius:5px}.deletion-files p{margin:6px 0}.deletion-files ul{margin:6px 0;padding-left:20px;overflow-wrap:anywhere}.deletion-actions{justify-content:flex-end;margin-bottom:0}.danger{background:#a43d2b;color:white;border-color:#a43d2b}
</style>
