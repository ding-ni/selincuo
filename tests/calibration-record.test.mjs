import test from 'node:test'
import assert from 'node:assert/strict'
import {calibrationRecord,calibrationCounts,calibrationRecordObjective,recordDownload} from '../frontend/src/calibration-record.mjs'

const definitions=[{key:'KC',label:'蒸发系数'},{key:'iceDdf',label:'融冰因子'},
 {key:'iceSublimationFraction',label:'升华比例'},{key:'lakeCoefficient',label:'湖面系数'}]
const task={id:'history',kind:'two-stage-calibration',request:{parameters:{KC:.72,iceDdf:10.4,iceSublimationFraction:.25,lakeCoefficient:1.5},
 search:{keys:['KC','iceDdf'],bounds:{KC:[.2,1.5],iceDdf:[2,14]}},twoStage:{mode:'both',seeds:[42,142,242]}},
 result:{parameters:{KC:.714,iceDdf:11.06,iceSublimationFraction:.25,lakeCoefficient:1.497},
 parameterSources:{fixed:['iceSublimationFraction']},calibrationWorkflow:{flowParameterSelectionUsesLakeLevel:true,lake:{},
 flowFits:[{evaluations:34569}],selection:{totalRescored:32569,eligibleRescored:9861,selectedForBasin:256,
 basinCandidatesEvaluated:773,refinement:{evaluations:2000}}}}}

test('historical initial values and bounds stay tied to the selected request',()=>{
 const a=calibrationRecord(task,definitions)
 assert.equal(a[0].initial,.72);assert.equal(a[0].final,.714)
 assert.equal(a[0].lower,.2);assert.equal(a[1].selected,true)
 assert.equal(a[2].stage,'人工固定');assert.equal(a[3].stage,'湖泊阶段')
 const other=structuredClone(task);other.request.parameters.KC=.9;other.result.parameters.KC=1.1
 assert.equal(calibrationRecord(other,definitions)[0].initial,.9)
 assert.equal(a[0].initial,.72)
 assert.equal(calibrationRecord({...task,request:{},result:{}},definitions)[0].initial,undefined)
})

test('counts distinguish rescoring, proposals and actual full basin candidates',()=>{
 assert.deepEqual(calibrationCounts(task),{rescored:32569,eligible:9861,selectedForBasin:256,
 jointProposals:2000,basinCandidates:773,numericEvaluations:34569})
 const saved=recordDownload(task,definitions)
 assert.equal(saved.parameters[0].initial,.72)
 assert.deepEqual(saved.twoStage.seeds,[42,142,242])
 assert.equal(saved.counts.basinCandidates,773)
})

test('manual trials do not claim calibration and lake-only runs keep hydrology fixed',()=>{
 const trial=structuredClone(task);trial.kind='trial'
 assert.equal(calibrationRecord(trial,definitions)[0].selected,false)
 assert.equal(calibrationRecord(trial,definitions)[3].selected,false)
 const lake=structuredClone(task);lake.request.twoStage.mode='lake'
 assert.equal(calibrationRecord(lake,definitions)[0].selected,false)
 assert.equal(calibrationRecord(lake,definitions)[3].selected,true)
})

test('continuous calibration exports actual objective and fresh generation counts',()=>{
 const current=structuredClone(task)
 current.request.twoStage={mode:'both',selectionMode:'continuous-dual',objective:{weights:{flow:.6,lake:.4}}}
 current.request.calibrationConfigurationSHA256='frozen-configuration'
 current.result.calibrationWorkflow.selection={totalGenerated:36000,totalRescored:0,selectedForBasin:512,
  basinCandidatesEvaluated:4500,refinement:{evaluations:4000}}
 const exported=recordDownload(current,definitions)
 assert.deepEqual(exported.objectiveSpec,{weights:{flow:.6,lake:.4}})
 assert.equal(exported.calibrationConfigurationSHA256,'frozen-configuration')
 assert.equal(exported.counts.generated,36000)
 assert.equal(exported.counts.rescored,0)
 assert.equal(exported.counts.eligible,undefined)
})

test('frozen objective and actual initial conditions are exported without using current configuration',()=>{
 const objectiveSpec={flowWeights:{nse:.5,logNse:.25,volume:.25},
  lakeWeights:{rmse:.25,trend:.25,amplitude:.25,peakTiming:.25},
  lakeScales:{rmseM:.1,trendErrorM:.1,amplitudeErrorM:.1,peakTimingErrorDays:14}}
 const initialHydrology={soilFraction:.5}
 const initialization={time:'2021-04-23T12:00:00',level:4543.66,reference:'SLC-working-datum'}
 const record=structuredClone(task)
 record.request.initialHydrology=initialHydrology
 record.result.initialization=initialization
 const plan={sourceTaskId:'history',configuration:{objectiveSpec,initialHydrology:{soilFraction:.4},initialization:{level:999}}}
 assert.deepEqual(calibrationRecordObjective(record,plan),objectiveSpec)
 const saved=recordDownload(record,definitions,plan)
 assert.deepEqual(saved.objectiveSpec,objectiveSpec)
 assert.deepEqual(saved.initialHydrology,initialHydrology)
 assert.deepEqual(saved.initialization,initialization)
 // A stale plan for a different task cannot contaminate this record.
 assert.equal(calibrationRecordObjective(record,{...plan,sourceTaskId:'other'}),null)
 assert.equal(recordDownload({...record,result:{}},definitions,{...plan,sourceTaskId:'other'}).initialization,null)
})

test('replay source can use its matching frozen plan when record fields are absent',()=>{
 const objectiveSpec={flowWeights:{nse:.5,logNse:.25,volume:.25}}
 const configuration={objectiveSpec,initialHydrology:{soilFraction:.5},initialization:{level:4543.66}}
 const replay={id:'new-task',kind:'calibration-replay',request:{replay:{sourceTaskId:'history'}},result:{}}
 const plan={sourceTaskId:'history',configuration}
 assert.deepEqual(calibrationRecordObjective(replay,plan),objectiveSpec)
 const saved=recordDownload(replay,definitions,plan)
 assert.equal(saved.sourceTaskId,'history')
 assert.deepEqual(saved.initialHydrology,configuration.initialHydrology)
 assert.deepEqual(saved.initialization,configuration.initialization)
 // An objective explicitly preserved in the task has precedence over the plan.
 replay.result.calibration={objectiveSpec:{version:'recorded'}}
 replay.result.replay={configuration:{objectiveSpec:{version:'replay'}}}
 assert.deepEqual(calibrationRecordObjective(replay,plan),{version:'recorded'})
 // A partial replay configuration must not mask the remaining plan fields.
 assert.deepEqual(recordDownload(replay,definitions,plan).initialization,configuration.initialization)
 assert.equal(calibrationRecordObjective({request:{},result:{}},{configuration}),null)
})
