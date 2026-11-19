<script setup>
import {computed,onMounted,ref} from 'vue'
import Chart from './Chart.vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({level:Number})
const curve=ref(null),model=ref(null),probe=ref(4544),error=ref(''),showFormula=ref(false)
const p=computed(()=>curve.value?.coefficients||{a:.066557,b:2.283942,c:34.7485,referenceLevel:4540})
const V=h=>{const x=h-p.value.referenceLevel;return p.value.a*x*x+p.value.b*x+p.value.c}
const A=h=>(2*p.value.a*(h-p.value.referenceLevel)+p.value.b)*1000
const lo=computed(()=>curve.value?.minLevel??4537),hi=computed(()=>curve.value?.maxLevel??4545)
const rows=computed(()=>{const out=[];for(let h=lo.value;h<=hi.value+1e-9;h+=.05)out.push({time:h.toFixed(2),volume:V(h),area:A(h)});return out})
const marker=computed(()=>props.level==null?null:(Math.round(props.level*10)/10).toFixed(2))
onMounted(async()=>{try{const [c,m]=await Promise.all([api('lake-curves'),api('lake-models')]);curve.value=(c.items||[]).find(i=>i.id===(m.active?.curveId||c.defaultCurveId))||c.items?.[0];model.value=m.active;if(props.level!=null)probe.value=Number(props.level.toFixed(2))}catch(e){error.value=e.message}})
</script>
<template>
<section class="panel lc">
 <p v-if="error" class="message error">{{error}}</p>
 <div class="lc-body">
  <div class="lc-chart-wrap">
   <div class="lc-title"><h3>色林错水位—库容关系</h3><button class="link" @click="showFormula=!showFormula">{{showFormula?'收起公式':'查看库容公式'}}</button></div>
   <div v-if="showFormula" class="lc-formula">
    <p class="eq">V(H) = {{p.a}}·x² + {{p.b}}·x + {{p.c}}</p>
    <p class="eq sub">A(H) = dV/dH = {{fixed(2*p.a*1000,3)}}·x + {{fixed(p.b*1000,3)}}</p>
    <p class="lc-legend">x = H − {{p.referenceLevel}}；H 为湖位（m），V 为库容（km³），A 为湖面面积（km²）。适用范围 {{lo}}—{{hi}} m。入湖水量计入库容后由 V(H) 反算湖位，湖面降水、蒸发按 A(H) 计算。</p>
   </div>
   <Chart :rows="rows" :series="[{key:'volume',label:'库容 V'}]" unit="km³" axis-label="湖位 m" :day="marker" context="lc-v"/>
  </div>
  <div class="lc-right">
   <div class="lc-calc">
    <label>输入湖位<input type="number" step=".01" :min="lo" :max="hi" v-model.number="probe"> m</label>
    <div class="lc-kpi"><span>库容</span><b>{{number(V(probe),3)}}</b><em>km³</em></div>
    <div class="lc-kpi"><span>湖面面积</span><b>{{fixed(A(probe),0)}}</b><em>km²</em></div>
    <div v-if="level!=null" class="lc-kpi"><span>较当前{{(V(probe)-V(level))>=0?'多':'少'}}</span><b>{{number(Math.abs(V(probe)-V(level))*1000,0)}}</b><em>百万 m³</em></div>
   </div>
   <div v-if="model" class="lc-model">
    <div class="lc-model-row"><span>湖面系数</span><b>{{number(model.lakeCoefficient,3)}}</b></div>
    <div class="lc-model-row"><span>溃口槛高</span><b>{{fixed(model.outlet.currentCrestM,2)}} m</b></div>
    <div class="lc-model-row"><span>等效过流宽</span><b>{{number(model.outlet.currentWidthM,0)}} m</b></div>
    <div class="lc-model-row" v-if="model.metrics?.all?.revised"><span>湖位误差</span><b>RMSE {{number(model.metrics.all.revised.rmseM,3)}} m</b></div>
   </div>
  </div>
 </div>
</section>
</template>
<style scoped>
.lc-body{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:24px;align-items:start}
.lc-chart-wrap h3{margin:0;font-size:16px}
.lc-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.lc-formula{padding:12px 16px;margin-bottom:12px;background:#f5f9fa;border:1px solid #dbe8ec;border-radius:8px}
.eq{font-family:"Cambria Math","Times New Roman",serif;font-size:19px;margin:0 0 4px;color:#1f4e5c}
.eq.sub{font-size:16px;color:#46707d}
.lc-legend{font-size:12.5px;color:var(--muted);margin:8px 0 0;line-height:1.7}
.lc-chart-wrap :deep(.chart){height:340px}
.lc-right{display:grid;gap:16px}
.lc-calc{border:1px solid var(--line);border-radius:8px;padding:16px;display:grid;gap:12px;background:#fbfdfd}
.lc-calc label{display:grid;gap:6px;font-size:13px;color:var(--muted)}
.lc-calc input{min-height:36px;padding:4px 10px}
.lc-kpi{display:flex;align-items:baseline;gap:6px}
.lc-kpi span{font-size:12px;color:var(--muted);min-width:56px}
.lc-kpi b{font-size:18px;font-weight:600;color:#1a4e5c;font-variant-numeric:tabular-nums}
.lc-kpi em{font-style:normal;font-size:12px;color:var(--muted)}
.lc-model{border:1px solid var(--line);border-radius:8px;padding:14px;display:grid;gap:8px;background:#f7fbfc}
.lc-model-row{display:flex;justify-content:space-between;font-size:13px}
.lc-model-row span{color:var(--muted)}
.lc-model-row b{font-variant-numeric:tabular-nums;color:#2a5260}
@media(max-width:900px){.lc-body{grid-template-columns:1fr}}
</style>
