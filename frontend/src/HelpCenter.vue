<script setup>
import {ref,onMounted} from 'vue'
import {api} from './api.mjs'
const props=defineProps({boot:Object})
const emit=defineEmits(['navigate'])
const tab=ref('guide'),context=ref(null),error=ref('')
onMounted(async()=>{try{context.value=await api('business-context')}catch(e){error.value=e.message}})
</script>
<template>
 <nav class="workspace-tabs" aria-label="帮助分类"><button v-for="t in [['guide','快速使用'],['science','方案与评价'],['about','平台信息']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <section v-if="tab==='guide'" class="panel help-content">
  <div class="panel-title"><h3>业务流程</h3><span class="badge">打开即可查看默认已有成果</span></div><p>业务模式提供湖情、监测、历史模拟、成果和资料更新。专家模式增加模型方案与率定、状态与空间诊断；工作模式与任务中心位于左侧导航下方。</p>
  <div class="help-steps">
   <article><b>01</b><div><h4>查看湖情与成果身份</h4><p>先核对当前默认或研究候选、成果覆盖与日期，再看湖位、四河来水。浅色用于日常操作，深色用于汇报；历史回放不是今日实况。</p><button class="link" @click="emit('navigate','overview')">打开湖情总览 →</button></div></article>
   <article><b>02</b><div><h4>查询真实资料</h4><p>默认最近一年，可快捷查看最近30天或完整时段。应用筛选后联查过程、质量、原始明细与导出。请求失败时保留并标明上次成功结果，缺测不补零。</p><button class="link" @click="emit('navigate','monitor')">打开监测资料 →</button></div></article>
   <article><b>03</b><div><h4>选择日期，先看运行计划</h4><p>历史模拟采用默认已发布方案。日期为成果查看窗口，实际预热由方案决定；同身份且已覆盖时可复用，其他情况显示重算或阻塞原因。</p><button class="link" @click="emit('navigate','simulation')">打开历史模拟 →</button></div></article>
   <article><b>04</b><div><h4>从质量判断成果</h4><p>成果中心先展示评价口径、年度精度、样本数与限制。缩放曲线不会改变年度评分；比较时须核对初始化、输入和共同日期。</p><button class="link" @click="emit('navigate','results')">打开成果中心 →</button></div></article>
   <article><b>05</b><div><h4>更新资料与专家研究</h4><p>资料更新建立新版本。研究候选与业务默认分开，保存参数不发布；预算耗尽不等于收敛。状态帧用于查看，续算需要完整检查点和兼容核验。</p><button class="link" @click="emit('navigate','model')">打开资料与更新 →</button></div></article>
   <article><b>06</b><div><h4>后台任务、导出与恢复</h4><p>大成果导出和备份提交后台任务，在全局任务中心查看进度、失败原因及成功文件下载。运行中可切页查询。恢复先读取清单，再在独立目录校验，成功后切换并重启，原工作区保留。</p><button class="link" @click="emit('navigate','system')">打开运行与维护 →</button></div></article>
  </div>
 </section>
 <section v-if="tab==='science'" class="panel help-content"><div class="panel-title"><h3>当前方案与科学评价</h3><span class="badge">研究候选须通过准入</span></div><h4>受保护默认成果</h4><p>原历史方案修订2及其2024—2025成果为当前受保护默认入口。湖位采用观测窗口初始化，2024、2025湖位RMSE约0.06925、0.06523米，分别231、255个有效日；这些分数不能代表固定锚点连续模拟精度。</p><h4>连续候选仍处研究阶段</h4><p>1597单元连续候选在已完成对照中湖位、杜加流量与初态稳定性尚未达到升级条件。程序完成或空间更细不会自动升级默认，候选不会因切页变成标准。</p><h4>升级条件</h4><p>2024、2025共同日期下的连续湖位RMSE、偏差绝对值、杜加NSE和总量偏差绝对值均不得退步。至少一年湖位RMSE下降10%或杜加NSE提高0.03，同时通过初态稳定、物理记账、参数范围、来源、完整成果与严格续算核验。缺少合格观测显示缺项。</p><h4>资料边界</h4><p>2025、2026资料已用于开发诊断，不能称未触碰的外部验证。主站仅作工作基准，四岸零点待核；统计候选不等于测量校正。未核实排水区独立产流，研究成果不将其计入已核实入湖量。固定湖面、固定冰川及裸冰外部水源等简化仍须保留说明。</p><button class="link" @click="emit('navigate','calibration')">查看模型方案与研究入口 →</button></section>
 <section v-if="tab==='about'" class="panel help-content">
  <div class="panel-title"><h3>色林错预警预报平台</h3><span class="badge">v{{boot.version}}</span></div>
  <p v-if="error" class="note warning">当前覆盖读取失败：{{error}}。请在资料与更新页重试。</p><dl><dt>业务功能</dt><dd>湖情展示 · 资料查询 · 历史模拟 · 成果与空间诊断</dd><dt>默认发布成果</dt><dd>{{context?.release?.resultId||boot.standardResultId}}</dd><dt>模型方案</dt><dd>{{boot.operationalSchemes?.[0]?.name||'色林错内置方案'}} · 修订 {{boot.operationalSchemes?.[0]?.revision||'—'}}</dd><dt>最新气象末时</dt><dd>{{context?.forcing?.latest?.end?.slice(0,16).replace('T',' ')||'尚未取得'}} · 北京时间</dd><dt>完整业务日</dt><dd>{{context?.forcing?.latest?.lastCompleteDay||'尚未取得'}}</dd><dt>主站合格质量日</dt><dd>{{context?.observations?.mainStation?.lastValidDay||'尚未取得'}}</dd><dt>默认成果覆盖</dt><dd>{{context?.selectedResult?.displayCoverage?.start?.slice(0,10)||'—'}} — {{context?.selectedResult?.displayCoverage?.end?.slice(0,10)||'—'}}</dd><dt>计算尺度</dt><dd>逐小时计算 · 逐日成果；500米为垂直高程分带</dd><dt>资料管理</dt><dd>本机SQLite与版本化文件；最新资料末时不代表已有成果已计算到该日</dd></dl><p class="note">当前为整改版，科学评价、页面业务验收与安装发行分别记录。实时未来预报与预警阈值尚未作为已交付业务启用。</p>
 </section>
</template>
<style scoped>
.help-content{max-width:1120px;padding:28px}.help-content>p{font-size:14px;line-height:1.8}.help-content>h4{font-size:16px;margin-top:26px}.help-steps{display:grid;grid-template-columns:1fr 1fr;gap:28px 40px}.help-steps article{display:flex;gap:20px;border-top:1px solid #dce8ec;padding-top:20px}.help-steps article>b{font-size:26px;font-weight:400;color:#6f9ba8}.help-steps h4{font-size:15px;color:#244f60;font-weight:550;margin:0 0 8px}.help-steps p{font-size:14px;line-height:1.8;color:#536f7c;max-width:420px;margin:0 0 12px}.help-steps .link{font-size:13px}.help-content dl{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px 20px;font-size:14px;line-height:1.7}.help-content dt{color:#607d89}.help-content dd{margin:0;color:#284f60;overflow-wrap:anywhere}@media(max-width:800px){.help-steps{grid-template-columns:1fr}.help-content{padding:20px}.help-content dl{grid-template-columns:1fr;gap:5px}.help-content dd{margin-bottom:12px}}
</style>
