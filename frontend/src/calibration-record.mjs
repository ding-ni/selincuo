// Read historical settings only from the selected task, never from its adopted
// scheme or the currently editable draft. Missing history stays missing.
export function calibrationRecord(task, definitions = []) {
 const configuration = task?.result?.replay?.configuration || task?.request?.replayConfiguration || {}
 const request = task?.request || {}
 const initial = request.parameters || configuration.parameters || {}
 const result = task?.result?.parameters || configuration.results || {}
 const bounds = request.search?.bounds || configuration.bounds || {}
 const selected = new Set(request.search?.keys || configuration.selectedKeys || [])
 const sources = task?.result?.parameterSources || {}
 const mode = request.twoStage?.mode || configuration.twoStage?.mode
 const searched = task?.kind !== 'trial' && !!(task?.result?.calibrationWorkflow || task?.result?.calibration)
 const selectedWithLake = task?.result?.calibrationWorkflow?.flowParameterSelectionUsesLakeLevel === true
 const fixed = new Set(sources.fixed || [])
 return definitions.map(d => {
  const lake = d.key === 'lakeCoefficient'
  const optimized = searched && (lake ? mode !== 'flow' && !!task?.result?.calibrationWorkflow?.lake : mode !== 'lake' && selected.has(d.key))
  const stage = lake ? (optimized ? '湖泊阶段' : '固定') : optimized ? '水文阶段' : '人工固定'
  let source = '本次固定设置'
  if (fixed.has(d.key)) source = '人工固定'
  else if (lake && optimized) source = '固定来水后拟合湖位'
  else if (optimized) source = selectedWithLake ? '杜加流量与湖位分阶段筛选' : '杜加流量率定'
  return { ...d, initial: initial[d.key], final: result[d.key], lower: bounds[d.key]?.[0],
   upper: bounds[d.key]?.[1], selected: optimized, stage, source }
 })
}

export function calibrationCounts(task) {
 const workflow = task?.result?.calibrationWorkflow || {}
 const selection = workflow.selection
 const fits = workflow.flowFits || []
 if (selection) return {
  rescored: selection.totalRescored,
  eligible: selection.eligibleRescored,
  selectedForBasin: selection.selectedForBasin,
  jointProposals: selection.refinement?.evaluations,
  basinCandidates: selection.basinCandidatesEvaluated,
  numericEvaluations: fits.reduce((n, f) => n + Number(f.evaluations || 0), 0)
 }
 return { numericEvaluations: fits.reduce((n, f) => n + Number(f.evaluations || 0), 0) }
}

// A replay preview is relevant only to its source. A pending response from a
// different selection must never be used as the currently displayed history.
function replayConfiguration(task, replayPlan) {
 const sourceTaskId = task?.request?.replay?.sourceTaskId || task?.request?.replaySourceTaskId || task?.result?.replay?.sourceTaskId || task?.id
 return sourceTaskId && replayPlan?.sourceTaskId === sourceTaskId ? replayPlan?.configuration : undefined
}

export function calibrationRecordObjective(task, replayPlan) {
 const frozen = task?.result?.objectiveSpec ?? task?.result?.calibration?.objectiveSpec ??
  task?.result?.calibrationWorkflow?.objectiveSpec ?? task?.result?.replay?.configuration?.objectiveSpec ??
  task?.request?.replayConfiguration?.objectiveSpec ?? replayConfiguration(task,replayPlan)?.objectiveSpec
 // Older records may contain only a profile name. Current profile settings are
 // deliberately not substituted for the historical definition.
 return frozen ?? null
}

export function recordDownload(task, definitions = [], replayPlan) {
 const request = task?.request || {}
 const replayResult = task?.result?.replay?.configuration || {}
 const replayRequest = request.replayConfiguration || {}
 const plan = replayConfiguration(task,replayPlan) || {}
 return { taskId: task?.id, kind: task?.kind, sourceTaskId: request.replay?.sourceTaskId || request.replaySourceTaskId || task?.result?.replay?.sourceTaskId,
  parameters: calibrationRecord(task, definitions), periods: request.periods,
  start: request.start, end: request.end,
  initialHydrology: task?.result?.initialHydrology ?? request.initialHydrology ?? replayResult.initialHydrology ?? replayRequest.initialHydrology ?? plan.initialHydrology ?? null,
  initialization: task?.result?.initialization ?? replayResult.initialization ?? replayRequest.initialization ?? plan.initialization ?? request.initialization ?? null,
  objectiveSpec: calibrationRecordObjective(task,replayPlan),
  inputId: request.inputId, spatialVersion: request.spatialVersion, search: request.search,
  twoStage: request.twoStage, counts: calibrationCounts(task),
  parameterSources: task?.result?.parameterSources,
  workflow: task?.result?.calibrationWorkflow, replay: task?.result?.replay }
}
