import test from 'node:test'
import assert from 'node:assert/strict'
import {runIssue,parameterIssue,compatibleComparison,readDraft} from '../frontend/src/workflow.mjs'
const inputs=[{id:'real',compatible:true,manifest:{start:'2023-01-01T00:00:00',end:'2024-12-31T23:00:00'}}]
const run={inputId:'real',start:'2023-01-01T00:00',end:'2024-12-31T23:00',stage:'runoff',maxEvaluations:120}
test('partial forcing cannot be used for annual calibration or out-of-coverage simulation',()=>{
  assert.equal(runIssue(run,inputs,'calibration'),'')
  assert.match(runIssue({...run,end:'2023-04-30T23:00'},inputs,'calibration'),/2024/)
  assert.match(runIssue({...run,end:'2025-01-01T00:00'},inputs,'simulation'),/覆盖/)
  assert.match(runIssue({...run,start:'2023-01-01T00:15'},inputs,'simulation'),/整小时/)
  assert.match(runIssue({...run,stage:'lake'},inputs,'calibration'),/冻结/)
  assert.match(runIssue(run,[{...inputs[0],compatible:false}],'simulation'),/空间/)
})
test('comparison does not silently overlay unrelated spaces or dates',()=>{
  const a={spatialVersion:'v1',inputId:'i1',daily:[{date:'2024-01-01'}]}
  assert.match(compatibleComparison(a,{...a,spatialVersion:'v2'}),/空间/)
  assert.match(compatibleComparison(a,{...a,daily:[{date:'2025-01-01'}]}),/共同日期/)
  assert.match(compatibleComparison(a,{...a,inputId:'i2'}),/输入版本不同/)
  assert.equal(compatibleComparison(a,a),'')
})
test('invalid parameters and corrupt remembered drafts fail clearly',()=>{
  assert.match(parameterIssue({WM:NaN},[{key:'WM',label:'容量',min:1,max:200}]),/容量/)
  assert.equal(readDraft({getItem:()=>'{bad'}),null)
  assert.equal(readDraft({getItem:()=>JSON.stringify({version:1})}),null)
})
