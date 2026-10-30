<script setup>
import {computed,getCurrentInstance,nextTick,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import * as echarts from 'echarts/core'
import {LineChart,BarChart} from 'echarts/charts'
import {GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'
import {chartState,saveChartState,rowKey,dateIndex,rangeContains,centeredZoom,isolatedObservation} from './chart-state.mjs'
echarts.use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps({rows:{default:()=>[]},series:{default:()=>[]},unit:{default:''},context:{default:''},compact:Boolean,dark:Boolean,day:String,zero:Boolean,hourly:Boolean,expanded:Boolean,axisLabel:{default:'日期'}})
const emit=defineEmits(['day'])
const root=ref(),plot=ref(),dialog=ref(),isExpanded=ref(false),revision=ref(0),selecting=ref(false),selectionBox=ref(null)
const fallbackKey='chart-instance:'+getCurrentInstance().uid
const memoryKey=()=>props.context||fallbackKey
const state=()=>chartState(memoryKey())
const selectedIndex=computed(()=>dateIndex(props.rows,props.day,props.hourly))
const selectedRow=computed(()=>selectedIndex.value<0?null:props.rows[selectedIndex.value])
const selectedValues=computed(()=>{revision.value;const visible=state().selected;return props.series.filter(s=>visible[s.label]!==false).map(s=>({key:s.key,label:s.label,value:selectedRow.value?.[s.key]}))})
const outside=computed(()=>{revision.value;return !!props.day&&selectedIndex.value>=0&&!rangeContains(selectedIndex.value,props.rows.length,state().zoom)})
const displayValue=v=>v==null||!Number.isFinite(Number(v))?'缺测':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3})+(props.unit?' '+props.unit:'')
let chart,observer,pixelRatio=1,ratioMedia,resizeFrame=0,dragging=false,selectionOrigin=null
function remember(){saveChartState(memoryKey(),state());revision.value++}
function render(){
 if(!chart)return
 const memory=state(),ink=props.dark?'#bdd0dc':'#516e7b',grid=props.dark?'#294450':'#e0e9ed'
 const short=props.compact&&chart.getHeight()<140,tickFont=short?10:12
 const selected=Object.fromEntries(props.series.map(s=>[s.label,memory.selected[s.label]!==false]))
 const axes=props.rows.map(r=>rowKey(r,props.hourly))
 const series=props.series.map(s=>{
  const observed=s.observed||s.key.startsWith('observed'),values=props.rows.map(r=>r[s.key]??null)
  return {id:s.id||s.key,name:s.label,type:s.type||'line',data:values,showSymbol:observed,symbol:'circle',symbolSize:(_,point)=>observed&&isolatedObservation(values,point.dataIndex)?4:0,connectNulls:false,lineStyle:{width:2,type:observed?'dashed':'solid'},barMaxWidth:16,...s.options}
 })
 // The invisible helper always owns the selected-day marker; hiding any legend item cannot hide it.
 if(selectedIndex.value>=0)series.push({id:'__selected_day__',name:'__selected_day__',type:'line',silent:true,showSymbol:false,tooltip:{show:false},data:props.rows.map(()=>null),markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:'#bc9251',type:'dashed',width:1.5},data:[{xAxis:axes[selectedIndex.value]}]}})
 chart.setOption({animation:false,color:['#208c96','#ce9041','#6486bd','#8da873','#ae81a6'],textStyle:{fontFamily:'Microsoft YaHei UI, Microsoft YaHei, sans-serif',fontSize:12,color:ink},
 tooltip:{trigger:'axis',confine:true,backgroundColor:props.dark?'#132d3a':'#fff',borderColor:grid,textStyle:{color:ink,fontSize:13},valueFormatter:v=>v==null?'缺测':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3})},
 legend:{data:props.series.map(s=>s.label),type:'scroll',top:2,left:'auto',right:8,itemWidth:16,itemHeight:8,textStyle:{color:ink,fontSize:tickFont},selected},
 grid:{left:12,right:30,top:short?24:props.compact?34:55,bottom:short?6:props.compact?12:50,containLabel:true},
 xAxis:{type:'category',boundaryGap:props.series.some(s=>s.type==='bar'),data:axes,axisLabel:{color:ink,fontSize:tickFont,hideOverlap:true,showMinLabel:true,showMaxLabel:true,alignMinLabel:'left',alignMaxLabel:'right',formatter:value=>props.compact&&/^\d{4}-\d{2}/.test(value)?value.slice(0,7):value},axisLine:{lineStyle:{color:grid}},axisTick:{show:false}},
 yAxis:{type:'value',min:memory.yRange?.[0]??null,max:memory.yRange?.[1]??null,name:props.unit,nameLocation:'end',nameGap:short?7:12,nameTextStyle:{color:ink,fontSize:tickFont,align:'left'},scale:!props.zero,axisLabel:{color:ink,fontSize:tickFont,hideOverlap:true,formatter:v=>Math.abs(v)>=1e8?(v/1e8)+'亿':Math.abs(v)>=1e4?(v/1e4)+'万':String(v)},splitNumber:short?2:props.compact?3:5,splitLine:{lineStyle:{color:grid,type:'dashed'}}},
 dataZoom:[{id:'x-inside',type:'inside',xAxisIndex:0,start:memory.zoom[0],end:memory.zoom[1],zoomOnMouseWheel:true,moveOnMouseMove:true},...(!props.compact?[{id:'x-slider',type:'slider',xAxisIndex:0,height:16,bottom:2,start:memory.zoom[0],end:memory.zoom[1],borderColor:grid}]:[])],series
 },{replaceMerge:['series','dataZoom','legend']})
}
function readZoom(){
 const memory=state(),zooms=chart?.getOption().dataZoom||[],x=zooms.find(z=>z.id==='x-inside')
 if(Number.isFinite(x?.start)&&Number.isFinite(x?.end))memory.zoom=[x.start,x.end]
 remember()
}
function initialise(){
 pixelRatio=window.devicePixelRatio||1
 chart=echarts.init(plot.value,null,{renderer:'canvas',devicePixelRatio:pixelRatio})
 const legend=e=>{Object.assign(state().selected,e.selected);remember()}
 for(const event of ['legendselectchanged','legendselected','legendunselected'])chart.on(event,legend)
 chart.on('datazoom',readZoom)
 chart.getZr().on('mousedown',()=>{dragging=false})
 chart.getZr().on('mousemove',e=>{if(e.event?.buttons)dragging=true})
 chart.on('click',e=>{if(!selecting.value&&!dragging&&e.name)emit('day',e.name.slice(0,props.hourly?16:10))})
 render()
}
function boxSelect(){selecting.value=!selecting.value;selectionBox.value=null}
function selectionPoint(event){const box=plot.value.getBoundingClientRect();return [event.clientX-box.left,event.clientY-box.top]}
function selectionStart(event){if(event.button!==0)return;selectionOrigin=selectionPoint(event);selectionBox.value={x:selectionOrigin[0],y:selectionOrigin[1],width:0,height:0};event.currentTarget.setPointerCapture(event.pointerId)}
function selectionMove(event){if(!selectionOrigin)return;const p=selectionPoint(event);selectionBox.value={x:Math.min(selectionOrigin[0],p[0]),y:Math.min(selectionOrigin[1],p[1]),width:Math.abs(p[0]-selectionOrigin[0]),height:Math.abs(p[1]-selectionOrigin[1])}}
function selectionEnd(event){
 if(!selectionOrigin)return
 const p=selectionPoint(event),first=selectionOrigin;selectionOrigin=null;selectionBox.value=null
 if(Math.abs(p[0]-first[0])<4||Math.abs(p[1]-first[1])<4)return
 const a=chart.convertFromPixel({gridIndex:0},first),b=chart.convertFromPixel({gridIndex:0},p)
 if(!Array.isArray(a)||!Array.isArray(b)||![...a,...b].every(Number.isFinite))return
 const low=Math.min(a[1],b[1]),high=Math.max(a[1],b[1]);if(high<=low)return
 state().yRange=[low,high]
 chart.dispatchAction({type:'dataZoom',dataZoomId:'x-inside',startValue:Math.min(a[0],b[0]),endValue:Math.max(a[0],b[0])})
 readZoom();selecting.value=false;render()
}
function resetZoom(){
 selecting.value=false;selectionBox.value=null
 const memory=state();memory.zoom=[0,100];memory.yRange=null;remember();render()
}
function locate(){const memory=state();memory.zoom=centeredZoom(selectedIndex.value,props.rows.length,memory.zoom);remember();render()}
function watchRatio(){ratioMedia?.removeEventListener('change',ratioChanged);ratioMedia=window.matchMedia(`(resolution: ${window.devicePixelRatio||1}dppx)`);ratioMedia.addEventListener('change',ratioChanged)}
function ratioChanged(){if(!chart)return;if((window.devicePixelRatio||1)!==pixelRatio){chart.dispose();initialise()}watchRatio()}
function resize(){cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{if(!chart)return;if((window.devicePixelRatio||1)!==pixelRatio)ratioChanged();else{chart.resize();render()}})}
function enlarge(){isExpanded.value=true;nextTick(()=>{dialog.value?.showModal();resize()})}
function closed(){isExpanded.value=false;revision.value++;render()}
function closeDialog(){dialog.value?.close();closed()}
onMounted(()=>{initialise();watchRatio();window.addEventListener('resize',resize);observer=new ResizeObserver(resize);observer.observe(plot.value)})
watch(()=>[props.rows,props.series,props.unit,props.context,props.day,props.dark,props.hourly],()=>{revision.value++;render()},{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();window.removeEventListener('resize',resize);ratioMedia?.removeEventListener('change',ratioChanged);cancelAnimationFrame(resizeFrame);chart?.dispose()})
</script>

<template>
 <div ref="root" class="chart-container" :class="{compact,'chart-expanded':expanded}" :data-chart-context="context||undefined">
  <div class="chart-toolbar" role="group" aria-label="图表操作">
   <button type="button" class="chart-action" :class="{active:selecting}" :aria-pressed="selecting" data-chart-action="box" @click="boxSelect">框选</button>
   <button type="button" class="chart-action" data-chart-action="reset" @click="resetZoom">复原</button>
   <button v-if="day&&outside" type="button" class="chart-action" data-chart-action="locate" @click="locate">定位日期</button>
   <button v-if="!expanded" type="button" class="chart-action" data-chart-action="expand" @click="enlarge">放大</button>
   <span v-if="!compact" class="chart-hint">滚轮缩放横轴 · 拖动平移 · 框选横纵轴</span>
  </div>
  <div v-if="day" class="chart-day-values" :data-selected-day="day"><strong>{{axisLabel}} {{day}}</strong><span v-for="item in selectedValues" :key="item.key">{{item.label}} {{displayValue(item.value)}}</span><span v-if="outside" class="chart-day-outside">当前范围外</span></div>
  <div class="chart-plot"><div ref="plot" class="chart" role="img" :data-day="day||undefined" :aria-label="series.map(s=>s.label).join('、')+'过程图'+(day?' · '+day:'')"></div><div v-if="selecting" class="chart-selection-overlay" data-chart-action="selection-area" @pointerdown="selectionStart" @pointermove="selectionMove" @pointerup="selectionEnd" @pointercancel="selectionEnd"><i v-if="selectionBox" :style="{left:selectionBox.x+'px',top:selectionBox.y+'px',width:selectionBox.width+'px',height:selectionBox.height+'px'}"></i></div><span v-if="!rows.length" class="chart-empty">该时段无资料</span></div>
  <Teleport to="body"><dialog ref="dialog" class="chart-dialog" :class="{'chart-dialog-dark':dark}" @close="closed"><div class="chart-dialog-heading"><strong>{{series.map(s=>s.label).join(' / ')}}</strong><button type="button" class="chart-action" aria-label="关闭放大图" @click="closeDialog">关闭</button></div><Chart v-if="isExpanded" expanded :rows="rows" :series="series" :unit="unit" :day="day" :dark="dark" :zero="zero" :hourly="hourly" :axis-label="axisLabel" :context="context||fallbackKey" @day="emit('day',$event)"/></dialog></Teleport>
 </div>
</template>

<style scoped>
.chart-container{display:flex;flex-direction:column;min-height:0}.chart-plot{position:relative;flex:1 1 300px;min-height:190px}.chart-container .chart{position:absolute;inset:0;width:100%;height:auto;min-height:0}.chart-selection-overlay{position:absolute;inset:0;z-index:2;cursor:crosshair;touch-action:none}.chart-selection-overlay i{position:absolute;display:block;border:1px solid #65acac;background:#65acac33;pointer-events:none}.chart-toolbar{display:flex;align-items:center;gap:5px;flex-wrap:wrap;min-height:27px}.chart-container .chart-action,.chart-dialog .chart-action{font:inherit;font-size:11px;line-height:1.2;min-height:24px;padding:3px 8px;border:1px solid var(--sc-line,#cedde2);border-radius:4px;background:var(--sc-panel,#fff);color:var(--sc-ink,#315766);cursor:pointer}.chart-action.active{background:#d9eee9;color:#215e5a;border-color:#50978d}.chart-hint{font-size:11px;color:var(--sc-muted,#708992);margin-left:5px}.chart-day-values{display:flex;align-items:center;gap:4px 10px;flex-wrap:wrap;font-size:11px;line-height:1.35;color:var(--sc-muted,#607d88);font-variant-numeric:tabular-nums}.chart-day-values strong{color:var(--sc-ink,#315766);font-weight:600}.chart-day-outside{color:#b07d3d}.compact .chart-plot{flex:1 1 0;min-height:0}.compact .chart-toolbar{min-height:22px}.compact .chart-action{font-size:10px;min-height:21px;padding:2px 6px}.compact .chart-day-values{font-size:10px}.chart-dialog{width:min(1280px,94vw);height:min(760px,90vh);max-height:none;max-width:none;border:1px solid #b8ced4;border-radius:8px;padding:16px;background:#fff;color:#315766;box-shadow:0 12px 50px #0006}.chart-dialog::backdrop{background:#05111cb8}.chart-dialog-dark{--sc-panel:#122a39;--sc-line:#294554;--sc-ink:#e1ecf1;--sc-muted:#9bb4c2;background:var(--sc-panel);color:var(--sc-ink)}.chart-dialog-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.chart-dialog .chart-expanded{height:calc(100% - 40px)}.chart-dialog .chart-expanded .chart-plot{min-height:0}
</style>
