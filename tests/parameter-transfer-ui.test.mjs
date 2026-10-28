import test from 'node:test'
import assert from 'node:assert/strict'
import {liftDujaToShared,transferMode,parameterValueSource,calibrationParameterScope,parseCalibrationSeeds} from '../frontend/src/parameter-transfer.mjs'

test('explicit transfer uses Duja values, retains global lake coefficient and records all prior inputs',()=>{
 const original={parameters:{KC:.8,WM:150,lakeCoefficient:.9},partitionParameters:{duja:{KC:.7,WM:120},zagen:{KC:1}},
  calibrationInitialParameters:{KC:.6,WM:160,lakeCoefficient:1},calibrationInitialPartitionParameters:{duja:{KC:.5}},adoptedParameterId:'old'}
 const result=liftDujaToShared(original,'duja-area-routing',{schemeId:'old-scheme',configurationSHA256:'abc'})
 assert.deepEqual(result.parameters,{KC:.7,WM:120,lakeCoefficient:.9})
 assert.deepEqual(result.calibrationInitialParameters,{KC:.5,WM:160,lakeCoefficient:1})
 assert.deepEqual(result.partitionParameters,{})
 assert.deepEqual(result.calibrationInitialPartitionParameters,{})
 assert.deepEqual(result.sourceTransferSnapshot.partitionParameters,original.partitionParameters)
 assert.equal(result.sourceTransferSnapshot.schemeId,'old-scheme')
 assert.equal(original.partitionParameters.duja.KC,.7)
 assert.equal(result.adoptedParameterId,undefined)
 assert.equal(transferMode(result),'duja-area-routing')
 assert.equal(parameterValueSource(result,'background','KC'),'杜加共享基准')
 assert.equal(parameterValueSource(original,'zagen','KC'),'该区域单独设置')
 assert.equal(parameterValueSource(original,'zagen','WM'),'沿用流域共用值')
 assert.equal(parameterValueSource(result,'background','lakeCoefficient'),'全湖参数')
})
test('search starts are one to four distinct unsigned 32 bit seeds',()=>{
 assert.deepEqual(parseCalibrationSeeds('42, 101，65535'),[42,101,65535])
 for(const value of ['', '-1','1,1','1,2,3,4,5','4294967296','1.5'])assert.throws(()=>parseCalibrationSeeds(value))
})
test('calibration scope distinguishes shared flow-constrained parameters and the lake coefficient',()=>{
 assert.equal(calibrationParameterScope({},'KC'),'杜加上游')
 for(const mode of ['duja-shared','duja-area-routing']){
  const scheme={parameterTransfer:{mode}}
  assert.equal(calibrationParameterScope(scheme,'KC'),'流域共享基准（杜加流量约束）')
  assert.equal(calibrationParameterScope(scheme,'lakeCoefficient'),'全湖')
 }
})
