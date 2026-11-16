<script setup>
import {computed,onMounted,ref} from 'vue'
import Chart from './Chart.vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({level:Number})
const curve=ref(null),model=ref(null),probe=ref(4544),error=ref('')
const p=computed(()=>curve.value?.coefficients||{a:.066557,b:2.283942,c:34.7485,referenceLevel:4540})
const V=h=>{const x=h-p.value.referenceLevel;return p.value.a*x*x+p.value.b*x+p.value.c}
const A=h=>(2*p.value.a*(h-p.value.referenceLevel)+p.value.b)*1000
const lo=computed(()=>curve.value?.minLevel??4537),hi=computed(()=>curve.value?.maxLevel??4545)
const rows=computed(()=>{const out=[];for(let h=lo.value;h<=hi.value+1e-9;h+=.1)out.push({time:h.toFixed(2),volume:V(h),area:A(h)});return out})
const table=computed(()=>{const out=[];for(let h=lo.value;h<=hi.value+1e-9;h+=.5)out.push({h,v:V(h),a:A(h)});return out})
const marker=computed(()=>props.level==null?null:(Math.round(props.level*10)/10).toFixed(2))
const nearest=computed(()=>props.level==null?null:table.value.reduce((b,r)=>Math.abs(r.h-props.level)<Math.abs(b.h-props.level)?r:b,table.value[0]).h)
const metrics=computed(()=>model.value?.metrics?.all)
onMounted(async()=>{try{const [c,m]=await Promise.all([api('lake-curves'),api('lake-models')]);curve.value=(c.items||[]).find(i=>i.id===(m.active?.curveId||c.defaultCurveId))||c.items?.[0];model.value=m.active;if(props.level!=null)probe.value=Number(props.level.toFixed(2))}catch(e){error.value=e.message}})
</script>
<template>
<section class="panel lc">
 <p v-if="error" class="message error">{{error}}</p>
 <div class="lc-top">
  <div class="lc-formula">
   <h3>色林错水位—库容关系</h3>
   <p class="eq">V(H) = {{p.a}} x² + {{p.b}} x + {{p.c}}</p>
   <p class="eq sub">A(H) = dV/dH = {{fixed(2*p.a*1000,3)}} x + {{fixed(p.b*1000,3)}}</p>
   <p class="lc-legend">x = H − {{p.referenceLevel}}，H 为湖位（m），V 为库容（km³），A 为湖面面积（km²）。湖面降水、蒸发按 A(H) 计算，入湖水量计入库容后由 V(H) 反算湖位。适用 {{lo}}—{{hi}} m。</p>
  </div>
  <div class="lc-calc">
   <label>湖位 H（m）<input type="number" step=".01" :min="lo" :max="hi" v-model.number="probe"></label>
   <div><span>库容</span><b>{{number(V(probe),3)}}<em>km³</em></b></div>
   <div><span>湖面面积</span><b>{{fixed(A(probe),1)}}<em>km²</em></b></div>
   <div v-if="level!=null"><span>相对当前湖位</span><b>{{(V(probe)-V(level))>=0?'+':''}}{{number((V(probe)-V(level))*1000,0)}}<em>百万m³</em></b></div>
  </div>
  <div v-if="model" class="lc-model">
   <h4>湖泊方案</h4>
   <dl><dt>湖面系数</dt><dd>{{number(model.lakeCoefficient,3)}}</dd><dt>溃口槛高</dt><dd>{{fixed(model.outlet.currentCrestM,2)}} m（2023 年起溢时 {{fixed(model.onsetLevelM,2)}} m）</dd><dt>等效过流宽度</dt><dd>{{number(model.outlet.currentWidthM,0)}} m</dd><dt>湖位误差</dt><dd>RMSE {{number(metrics?.revised?.rmseM,3)}} m（{{metrics?.revised?.days}} 天）</dd></dl>
  </div>
 </div>
 <div class="lc-charts">
  <div><h4>水位—库容</h4><Chart :rows="rows" :series="[{key:'volume',label:'库容 V'}]" unit="km³" axis-label="湖位 m" :day="marker" :context="'lc-v'"/></div>
  <div><h4>水位—面积</h4><Chart :rows="rows" :series="[{key:'area',label:'面积 A = dV/dH'}]" unit="km²" axis-label="湖位 m" :day="marker" :context="'lc-a'"/></div>
 </div>
 <div class="table-wrap lc-table"><table><thead><tr><th>湖位 m</th><th v-for="r in table" :key="r.h" :class="{now:r.h===nearest}">{{r.h.toFixed(1)}}</th></tr></thead><tbody><tr><th>库容 km³</th><td v-for="r in table" :key="r.h" :class="{now:r.h===nearest}">{{number(r.v,2)}}</td></tr><tr><th>面积 km²</th><td v-for="r in table" :key="r.h" :class="{now:r.h===nearest}">{{fixed(r.a,0)}}</td></tr></tbody></table></div>
</section>
</template>
<style scoped>
.lc-top{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(220px,.8fr) minmax(240px,.9fr);gap:22px;margin-bottom:22px}
.lc-formula h3{margin:0 0 12px}
.eq{font-family:"Cambria Math","Times New Roman",serif;font-size:21px;margin:0 0 6px;color:#1f4e5c;font-variant-numeric:tabular-nums}
.eq.sub{font-size:17px;color:#46707d}
.lc-legend{font-size:13px;color:var(--muted);margin:12px 0 0;line-height:1.75}
.lc-calc,.lc-model{border:1px solid var(--line);border-radius:8px;padding:14px 16px;background:#fbfdfd;display:grid;gap:10px;align-content:start}
.lc-calc span{font-size:12px;color:var(--muted);display:block}
.lc-calc b{font-size:20px;color:#245e6f;font-variant-numeric:tabular-nums}
.lc-calc em{font-style:normal;font-size:12px;color:var(--muted);margin-left:4px;font-weight:400}
.lc-model h4{margin:0}
.lc-model dl{grid-template-columns:90px 1fr;gap:6px 10px;margin:0;font-size:13px}
.lc-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin-bottom:18px}
.lc-charts h4{margin:0 0 4px}
.lc-table th,.lc-table td{text-align:center;padding:8px 10px;font-variant-numeric:tabular-nums}
.lc-table .now{background:#fff4dc;font-weight:600}
@media(max-width:1200px){.lc-top{grid-template-columns:1fr 1fr}.lc-formula{grid-column:1/-1}.lc-charts{grid-template-columns:1fr}}
</style>
