export const kindLabel = kind => ({simulation:'历史模拟',trial:'人工试调',continuation:'状态续算',calibration:'自动率定','download-forcing':'气象下载','register-inputs':'气象登记','build-spatial':'空间处理','import-observations':'资料入库'}[kind] || kind)
export const groups = [{key:'land',label:'新安江产流'},{key:'snow',label:'积雪与冰川'},{key:'routing',label:'子流域汇流'},{key:'lake',label:'湖泊水量平衡'}]
export function runIssue(run, inputs, mode, active = false) {
  if (active) return '已有任务正在执行，可查看进度或停止后再开始。'
  const source = inputs.find(i => i.id === run.inputId)
  if (!source) return '请先选择已登记的真实气象输入。'
  if (source.compatible === false) return '输入的空间版本与当前模型不一致，请重新登记。'
  if (!run.start || !run.end || !Number.isFinite(Date.parse(run.start)) || !Number.isFinite(Date.parse(run.end)) || Date.parse(run.end) < Date.parse(run.start)) return '请设置有效的起止时刻。'
  if (run.start.length < 16 || run.end.length < 16 || run.start.slice(14,16) !== '00' || run.end.slice(14,16) !== '00') return '模型按整小时演算，请将起止时间的分钟设为00。'
  if (run.start < source.manifest.start.slice(0,16) || run.end > source.manifest.end.slice(0,16)) return '计算时段超出所选输入的真实覆盖范围，请补齐资料或缩短时段。'
  if (mode === 'calibration') {
    if (run.start > '2023-01-01T00:00' || run.end < '2024-12-31T23:00') return '自动率定需要完整2023年预热和2024年评价驱动，当前条件尚未满足。'
    if (!Number.isInteger(run.maxEvaluations) || run.maxEvaluations < 10 || run.maxEvaluations > 2000) return '评价预算须为10—2000之间的整数。'
    if (run.stage === 'lake' && !run.baseTaskId) return '请选择同输入版本的成功成果，以冻结流域参数。'
  }
  return ''
}
export function parameterIssue(params, definitions) {
  for (const d of definitions) {
    const v = params[d.key]
    if (typeof v !== 'number' || !Number.isFinite(v) || v < d.min || v > d.max) return `${d.label}须在${d.min}—${d.max}之间。`
  }
  if (params.snowTemperature >= params.rainTemperature) return '全雪温度须低于全雨温度。'
  if (params.KI + params.KG >= 1) return '小时KI与KG之和须小于1。'
  return ''
}
export function compatibleComparison(current, other) {
  if (!current || !other) return '读取比较成果后可叠加共同日期。'
  if (current.spatialVersion !== other.spatialVersion) return '空间版本不同，不能直接叠加比较。'
  const dates = new Set((current.daily || []).map(r => r.date))
  if (!(other.daily || []).some(r => dates.has(r.date))) return '两次计算没有共同日期，未叠加。'
  if (current.inputId !== other.inputId) return '输入版本不同：叠加共同日期，仅作条件差异比较。'
  return ''
}
export function readDraft(storage) {
  try { const d = JSON.parse(storage.getItem('selinco:workbench:v2')); return d?.version === 2 ? d : null } catch { return null }
}
