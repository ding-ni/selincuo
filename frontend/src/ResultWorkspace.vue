<script setup>
import {ref,computed,watch} from 'vue'
import {kindLabel} from './workflow.mjs'
const props=defineProps({tasks:Array,selectedId:String,busy:Boolean})
const emit=defineEmits(['select','metadata','cleanup','stop'])
const tab=ref('all'),search=ref(''),type=ref(''),collection=ref(''),page=ref(1)
const status=s=>({queued:'排队',running:'运行中',stopping:'停止中',succeeded:'已完成',failed:'失败',cancelled:'已停止',interrupted:'已中断'}[s]||s)
const filtered=computed(()=>props.tasks.filter(t=>(tab.value==='trash'?t.trashed:!t.trashed)&&(tab.value!=='saved'||t.favorite)&&(tab.value!=='failed'||['failed','interrupted','cancelled'].includes(t.status))&&(!type.value||t.kind===type.value)&&(!collection.value||t.collection===collection.value)&&[t.name,t.id,t.created_at,t.message].join(' ').toLowerCase().includes(search.value.trim().toLowerCase())))
const pages=computed(()=>Math.max(1,Math.ceil(filtered.value.length/12))),visible=computed(()=>filtered.value.slice((page.value-1)*12,page.value*12))
const collections=computed(()=>[...new Set(props.tasks.map(t=>t.collection).filter(Boolean))])
watch([tab,search,type,collection],()=>page.value=1)
watch(pages,n=>page.value=Math.min(page.value,n))
</script>
<template>
 <section class="panel results-tools"><nav class="workspace-tabs"><button v-for="t in [['all','全部记录'],['saved','保留成果'],['failed','未完成'],['trash','回收站']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav><div class="form-row"><input v-model="search" type="search" class="grow" aria-label="查找成果" placeholder="名称、编号或计算日期"><select v-model="type" aria-label="筛选计算类型"><option value="">全部类型</option><option v-for="kind in [...new Set(tasks.map(t=>t.kind))]" :key="kind" :value="kind">{{kindLabel(kind)}}</option></select><select v-model="collection" aria-label="筛选成果分组"><option value="">全部分组</option><option v-for="c in collections" :key="c">{{c}}</option></select></div><p v-if="tab==='trash'" class="note">平台回收站保留文件；清理前先预览，未被引用的文件再送入Windows回收站。</p></section>
 <div class="archive-workspace"><section class="panel record-list"><div class="record-list-heading">{{filtered.length}} 次计算 <span>选择记录查看详情</span></div><article v-for="t in visible" :key="t.id" :class="{selected:selectedId===t.id}"><button class="record-select" @click="emit('select',t.id)"><b>{{t.favorite?'★ ':''}}{{t.name||kindLabel(t.kind)}}</b><span>{{kindLabel(t.kind)}} · {{status(t.status)}}<template v-if="['running','queued','stopping'].includes(t.status)"> · {{Math.round(t.progress)}}%</template></span><small>{{t.created_at.slice(0,16).replace('T',' ')}}</small></button><div class="record-row-actions"><button v-if="['running','queued','stopping'].includes(t.status)" @click="emit('stop',t.id)">停止</button><template v-else><button @click="emit('metadata',t,{favorite:!t.favorite})">{{t.favorite?'取消保留':'保留'}}</button><button @click="emit('metadata',t,{trashed:!t.trashed})">{{t.trashed?'恢复':'移入回收站'}}</button><button v-if="t.trashed" @click="emit('cleanup',t)">清理预览</button></template></div></article><p v-if="!visible.length" class="empty">暂无匹配记录</p><div class="list-pagination"><button :disabled="page<=1" @click="page--">上一页</button><span>{{page}} / {{pages}}</span><button :disabled="page>=pages" @click="page++">下一页</button></div></section><div class="archive-detail"><slot/></div></div>
</template>
