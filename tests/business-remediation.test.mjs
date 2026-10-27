import test from 'node:test'
import assert from 'node:assert/strict'
import {historyDefaults,historyRequest,shiftCalendarDay} from '../frontend/src/history-workflow.mjs'
import {resultReport} from '../frontend/src/result-report.mjs'

const scheme={id:'standard',revision:2,available:true,inputId:'latest',start:'2024-01-01T00:00:00',end:'2026-10-21T00:00:00',initialization:{origin:'2023-01-01T00:00:00'},inputRequirement:{transformationSHA256:'lake-v1'}}
const boot={operationalSchemes:[scheme],defaultResultInputId:'frozen',inputs:['frozen','latest'].map((id,i)=>({id,compatible:true,manifest:{start:'2022-12-01T09:00:00',end:i?'2026-10-21T07:00:00':'2026-01-01T07:00:00',transformationSHA256:'lake-v1'}}))}
test('new business session selects frozen release input and exclusive full-day end',()=>{
 const run=historyDefaults(boot)
 assert.equal(run.inputId,'frozen')
 assert.equal(run.end,'2026-01-01T00:00')
 assert.equal(shiftCalendarDay(run.end,-1),'2025-12-31')
 assert.equal(historyRequest({...run,parameters:{bad:1}},'retry').parameters,undefined)
})
test('date windows survive refresh without replacing frozen identity',()=>{
 const run=historyDefaults(boot,{start:'2025-01-01T00:00',end:'2026-01-01T00:00'})
 assert.equal(run.inputId,'frozen')
 assert.equal(run.start,'2025-01-01T00:00')
 assert.equal(shiftCalendarDay('2024-02-28',1),'2024-02-29')
})
test('offline report uses complete frozen annual metrics and escapes untrusted text',()=>{
 const summary={id:'id',name:'<script>alert(1)</script>',scheme:{name:'default',revision:2},unitCount:30,displayCoverage:{start:'2024-01-01',end:'2025-12-31'},evaluation:{mode:'window',label:'窗口初始化',annual:{2025:{lake:{rmse:.0652344,count:255},flow:{nse:.72537,count:211}}},qualityProfile:'quality'},limitations:['<img src=x onerror=alert(1)>']}
 const html=resultReport(summary,{daily:[{date:'2025-01-01',lakeLevel:4544,observedLevel:4544.1},{date:'2025-01-02',lakeLevel:null,observedLevel:null},{date:'2025-01-03',lakeLevel:4544.2}]})
 assert.match(html,/0\.0652/)
 assert.match(html,/255/)
 assert.match(html,/Content-Security-Policy/)
 assert.ok(!html.includes('<script>'))
 assert.ok(!html.includes('<img src='))
 assert.match(html,/M70\.00/)
 assert.match(html,/M970\.00/)
 assert.match(html,/窗口湖位误差不能作为固定锚点连续精度/)
})
