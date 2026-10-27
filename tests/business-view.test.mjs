import test from 'node:test'
import assert from 'node:assert/strict'
import {businessTasks,qualityLabel,stepLabel,variableDefinitions} from '../frontend/src/business-view.mjs'

test('daily result list includes business simulations while research stays in maintenance',()=>{
 const tasks=[{id:'simulation',kind:'simulation'},{id:'continuation',kind:'continuation'},
  {id:'trial',kind:'trial'},{id:'calibration',kind:'calibration'},{id:'sensitivity',kind:'sensitivity'}]
 assert.deepEqual(businessTasks(tasks).map(t=>t.id),['simulation','continuation'])
 assert.equal(businessTasks(tasks,true).length,tasks.length)
})

test('monitoring copy uses readable status and step labels',()=>{
 assert.equal(qualityLabel('zero_needs_review'),'待核查')
 assert.equal(qualityLabel('ice_or_stopped'),'停测')
 assert.equal(stepLabel('irregular'),'时段记录')
 assert.ok(variableDefinitions.some(item=>item.id==='snowWaterEquivalent'&&item.unit==='mm'))
})
