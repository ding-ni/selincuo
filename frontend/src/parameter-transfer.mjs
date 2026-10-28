// Display-only labels and an explicit draft conversion. Calculated regional
// values always come from the backend's frozen audit, never a browser formula.
export const transferMode=scheme=>scheme?.parameterTransfer?.mode||'legacy'
export const transferNames={legacy:'沿用已有分区参数', 'duja-shared':'杜加参数共享', 'duja-area-routing':'杜加参数共享与汇流面积缩放'}
export function calibrationParameterScope(scheme,key){
 if(key==='lakeCoefficient')return '全湖'
 return transferMode(scheme)==='legacy'?'杜加上游':'流域共享基准（杜加流量约束）'
}
const clone=value=>JSON.parse(JSON.stringify(value??{}))
export function liftDujaToShared(scheme,mode,source={}){
 if(!['duja-shared','duja-area-routing'].includes(mode))throw new Error('请选择共享参数方式')
 const value=clone(scheme),parameters=clone(value.parameters),partitions=clone(value.partitionParameters)
 const initial=clone(value.calibrationInitialParameters||parameters),initialPartitions=clone(value.calibrationInitialPartitionParameters||partitions)
 value.sourceTransferSnapshot={method:'explicit-duja-lift',schemeId:source.schemeId||null,configurationSHA256:source.configurationSHA256||null,
  parameters,partitionParameters:partitions,calibrationInitialParameters:initial,calibrationInitialPartitionParameters:initialPartitions}
 const lift=(base,duja)=>{const result={...base,...(duja||{})};result.lakeCoefficient=base.lakeCoefficient;return result}
 value.parameters=lift(parameters,partitions.duja)
 value.calibrationInitialParameters=lift(initial,initialPartitions.duja)
 value.partitionParameters={};value.calibrationInitialPartitionParameters={}
 value.parameterTransfer={version:1,mode,donor:'duja',areaExponent:.5}
 delete value.adoptedParameterId
 return value
}
export function parameterValueSource(scheme,scope,key){
 if(key==='lakeCoefficient')return '全湖参数'
 if(transferMode(scheme)!=='legacy')return '杜加共享基准'
 if(scope!=='background'&&Object.hasOwn(scheme?.partitionParameters?.[scope]||{},key))return '该区域单独设置'
 return scope==='background'?'流域共用参数':'沿用流域共用值'
}
export function parseCalibrationSeeds(value){
 const text=String(value||'').trim(),items=text?text.split(/[\s,，]+/):[]
 if(items.length<1||items.length>4||items.some(x=>!/^\d+$/.test(x)))throw new Error('请填写1—4个整数起点，用逗号分隔。')
 const values=items.map(Number)
 if(values.some(x=>!Number.isSafeInteger(x)||x<0||x>=2**32)||new Set(values).size!==values.length)throw new Error('起点须为不重复的0—4294967295整数。')
 return values
}
