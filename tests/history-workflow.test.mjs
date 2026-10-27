import test from 'node:test'
import assert from 'node:assert/strict'
import {historyDefaults,historyIssue,historyRequest,readHistory,shiftCalendarDay} from '../frontend/src/history-workflow.mjs'
const scheme={id:'standard',revision:1,available:true,inputId:'real',start:'2024-01-01T00:00:00',end:'2026-01-01T00:00:00',initialization:{origin:'2023-01-01T00:00:00'}}
const boot={operationalSchemes:[scheme],inputs:[{id:'real',compatible:true,manifest:{start:'2022-12-01T09:00:00',end:'2026-01-01T07:00:00'}}]}
test('daily run works from packaged defaults and never adopts calibration values',()=>{
 const run=historyDefaults(boot)
 assert.equal(historyIssue(run,boot),'')
 const request=historyRequest({...run,parameters:{WM:40},stage:'runoff',partitionParameters:{duja:{KC:1}}},'unique')
 assert.deepEqual(Object.keys(request).sort(),['kind','schemeId','schemeRevision','inputId','start','end','name','clientRequestId'].sort())
 assert.equal(request.schemeId,'standard')
 assert.equal(run.end,'2026-01-01T00:00')
})
test('inclusive dates handle year boundaries and leap days without local timezone conversion',()=>{
 assert.equal(shiftCalendarDay('2025-12-31',1),'2026-01-01')
 assert.equal(shiftCalendarDay('2024-02-28',1),'2024-02-29')
 assert.equal(shiftCalendarDay('2026-01-01',-1),'2025-12-31')
})
test('missing inputs and out of coverage stop before submitting a task',()=>{
 const run=historyDefaults(boot)
 assert.match(historyIssue({...run,end:'2026-02-01T00:00'},boot),/覆盖/)
 assert.match(historyIssue(run,{...boot,operationalSchemes:[{...scheme,available:false,message:'资料缺失'}]}),/资料缺失/)
 assert.match(historyIssue({...run,start:'2023-01-01T00:00'},boot),/2024/)
 assert.match(historyIssue(run,boot,true),/任务/)
 assert.deepEqual(readHistory({getItem:()=>'{bad'}),{})
})
test('a revised scheme replaces a stored uncorrected input and rejects submitting it',()=>{
 const revised={...scheme,revision:2,inputId:'corrected',inputRequirement:{transformationSHA256:'lake-rain-v1'}}
 const context={...boot,operationalSchemes:[revised],inputs:[...boot.inputs,{...boot.inputs[0],id:'corrected',manifest:{...boot.inputs[0].manifest,transformationSHA256:'lake-rain-v1'}}]}
 const run=historyDefaults(context,{inputId:'real'})
 assert.equal(run.inputId,'corrected')
 assert.equal(run.schemeRevision,2)
 assert.equal(historyIssue(run,context),'')
 assert.match(historyIssue({...run,inputId:'real'},context),/标准驱动/)
})
