import {createApp,h,ref} from 'vue'
import Chart from './src/Chart.vue'
import MonitoringTrend from './src/MonitoringTrend.vue'
import ScreenMap from './src/ScreenMap.vue'
import './src/style.css'
import './src/dashboard.css'
const rows=Array.from({length:100},(_,i)=>({date:new Date(Date.UTC(2024,0,1+i)).toISOString().slice(0,10),level:4540+i*.01,obs:i%7===0?4540+i*.01:null}))
const station={id:'TEST',name:'测试站',daily:rows.map(r=>({date:r.date,value:r.obs}))}
const cells=Array.from({length:9},(_,i)=>({id:'grid-'+i,lon:88.9+(i%3)*.1,lat:31.7+Math.floor(i/3)*.1,value:i+1}))
createApp({setup(){const day=ref(rows[20].date);return()=>h('div',{style:'display:grid;grid-template-columns:1fr 1fr;gap:12px'},[
 h(Chart,{rows,series:[{key:'level',label:'模拟'},{key:'obs',label:'实测',observed:true}],unit:'m',day:day.value,context:'test:chart-a',onDay:value=>day.value=value}),
 h(Chart,{rows,series:[{key:'level',label:'流量'}],unit:'m³/s',day:day.value,context:'test:chart-b',onDay:value=>day.value=value}),
 h('section',{class:'lake-screen',style:'height:240px;display:block'},[h(MonitoringTrend,{station,day:day.value,days:90,style:'height:180px'})]),
 h('section',{class:'lake-screen',style:'height:500px;display:block'},[h(ScreenMap,{variable:'precipitation',frame:{variable:'precipitation',complete:true,cells},stations:[],showNames:true,showRivers:false,showBoundaries:true,onFeature:value=>{document.querySelector('#map-selection').textContent=value.id+':'+value.value}})]),
 h('output',{id:'map-selection'})])}}).mount('#app')
