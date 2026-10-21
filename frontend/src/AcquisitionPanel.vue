<script setup>
import {computed,ref} from 'vue'
const props=defineProps({report:Object,busy:Boolean,active:Array})
const emit=defineEmits(['refresh','download','select-files'])
const direct=ref(true)
const labels={'account-disabled':'CDS账户停用',authentication:'认证失败','license-required':'待接受许可','queue-limit':'请求配额已满',running:'下载中',downloaded:'已完成',incomplete:'下载未完成',cancelled:'已停止','not-started':'尚未开始'}
const missing=computed(()=>props.report?.months.filter(m=>!m.complete)||[])
const years=computed(()=>[...new Set((props.report?.months||[]).map(m=>m.month.slice(0,4)))])
</script>
<template>
 <section class="panel acquisition-panel"><div class="panel-title"><h3>历史格网下载与覆盖</h3><span class="badge">ERA5-Land · 0.1°</span><button class="link" :disabled="busy" @click="emit('refresh')">核验本地文件 ↻</button></div>
 <template v-if="report"><div class="acquisition-summary"><div><b>{{report.completeMonths}}<small> / {{report.requiredMonths}} 月</small></b><span>降水、气温、蒸发能力均完整</span></div><div><strong>{{labels[report.download.status]||report.download.status}}</strong><span>{{missing.length?'尚缺 '+missing.length+' 个月':'所需月份已齐备，可登记连续输入'}}</span></div></div>
 <p v-if="report.download.message" class="message error">{{report.download.message}}</p><p v-else-if="report.download.status==='account-disabled'" class="message error">CDS返回账户已停用；请联系数据服务方恢复账户，或在本机配置另一个已授权账户。</p>
 <div v-for="year in years" :key="year" class="coverage-year"><b>{{year}}<small>{{year==='2022'?'起算缓冲':year==='2023'?'预热':year==='2024'?'率定':'独立验证'}}</small></b><div class="month-grid"><button v-for="m in report.months.filter(m=>m.month.startsWith(year))" :key="m.month" :class="{complete:m.complete}" :aria-label="m.month+(m.complete?'三个变量完整':'资料缺失')" :title="m.month+'：'+Object.entries(m.variables).map(([k,v])=>k+' '+(v.length?'完整':'缺失')).join('；')" @click="m.complete&&emit('select-files',[...new Set(Object.values(m.variables).flat())])"><span>{{Number(m.month.slice(5))}}月</span><small>{{m.complete?'✓ 完整':'待补齐'}}</small></button></div></div>
 <details v-if="missing.length"><summary>查看缺少的月份（{{missing.length}}）</summary><p class="note">{{missing.map(m=>m.month).join('、')}}</p></details><p class="note">按原始UTC月份核验三个变量的每小时记录；2022年12月用于差分与时区缓冲。点击完整月份可选择对应文件，登记时仍检查连续性、数值和空间权重。</p>
 <div class="actions"><label class="inline"><input type="checkbox" v-model="direct">本次任务直连</label><button class="primary" :disabled="busy||active.length>0||!missing.length" @click="emit('download',direct)">继续补齐缺失月份</button><span class="note">只提交一个CDS任务；可停止，保留断点。</span></div><details><summary>账户与资料恢复说明</summary><p class="note">认证从本机用户目录 .cdsapirc 读取，平台不保存或展示密钥。账户停用须由CDS支持解除；恢复后再点击继续。也可上传已获授权的原始NetCDF文件并核验。不会用演示文件或其他产品替换ERA5-Land。</p><a href="https://support.ecmwf.int/" target="_blank" rel="noopener">打开ECMWF支持门户 ↗</a></details>
 <details v-if="report.issues.length"><summary>未通过核验的文件（{{report.issues.length}}）</summary><p v-for="i in report.issues" :key="i.path" class="note warning">{{i.path}}：{{i.error}}</p></details>
 </template><p v-else class="empty">正在核验本地真实格网文件…</p>
 </section>
</template>
