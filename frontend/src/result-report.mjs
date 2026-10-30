import {displayCoverageDates,displayAnnualMetrics} from './result-catalog.mjs'
const escape=value=>String(value??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c])
const value=(v,digits=4)=>v==null||!Number.isFinite(Number(v))?'无合格评价资料':Number(v).toFixed(digits)

function processSvg(rows){
 const values=rows.flatMap(r=>[r.lakeLevel,r.observedLevel]).filter(v=>v!=null&&Number.isFinite(v))
 if(!rows.length||!values.length)return '<p>无湖位过程资料。</p>'
 const lower=Math.floor((Math.min(...values)-.05)*10)/10,upper=Math.ceil((Math.max(...values)+.05)*10)/10
 const x=i=>70+i/Math.max(1,rows.length-1)*900,y=v=>270-(v-lower)/(upper-lower)*220
 const path=key=>{let open=false;return rows.map((r,i)=>{if(r[key]==null||!Number.isFinite(r[key])){open=false;return ''}const point=(open?'L':'M')+x(i).toFixed(2)+','+y(r[key]).toFixed(2);open=true;return point}).join(' ')}
 const grid=Array.from({length:6},(_,i)=>{const v=lower+(upper-lower)*i/5;return `<line x1="70" x2="970" y1="${y(v)}" y2="${y(v)}" stroke="#dbe5e8"/><text x="60" y="${y(v)+4}" text-anchor="end">${v.toFixed(2)}</text>`}).join('')
 return `<svg viewBox="0 0 1000 330" role="img" aria-label="完整冻结湖位过程"><g font-size="13" fill="#526d77">${grid}<text x="70" y="30">湖位 / m</text><text x="70" y="305">${escape(rows[0].date.slice(0,10))}</text><text x="970" y="305" text-anchor="end">${escape(rows.at(-1).date.slice(0,10))}</text></g><path d="${path('lakeLevel')}" fill="none" stroke="#168698" stroke-width="2"/><path d="${path('observedLevel')}" fill="none" stroke="#ce9041" stroke-width="2"/></svg><p class="legend">湖蓝：模拟湖位　橙色：主站实测；缺测保持断线。</p>`
}

export function resultReport(summary,result){
 const coverage=displayCoverageDates(summary.displayCoverage)
 const annual=displayAnnualMetrics(summary.evaluation.annual,summary.displayCoverage)
 summary={...summary,displayCoverage:{...summary.displayCoverage,...coverage}}
 result={...result,daily:(result.daily||[]).filter(row=>{const day=row.date?.slice(0,10);return day&&(!coverage.start||day>=coverage.start)&&(!coverage.end||day<=coverage.end)})}
 const metrics=Object.entries(annual).map(([year,m])=>`<tr><td>${escape(year)}</td><td>${value(m.lake?.rmse)}</td><td>${value(m.lake?.mae)}</td><td>${value(m.lake?.bias)}</td><td>${m.lake?.count||0}</td><td>${value(m.flow?.nse)}</td><td>${value(m.flow?.pbias,2)}</td><td>${m.flow?.count||0}</td></tr>`).join('')
 return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><title>${escape(summary.name)} · 成果摘要</title><style>body{font:15px/1.7 'Microsoft YaHei',sans-serif;color:#284854;max-width:1100px;padding:40px 32px;margin:auto}h1{font-size:26px;border-bottom:3px solid #168698;padding-bottom:15px}h2{font-size:19px;margin:28px 0 12px}p{margin:9px 0}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #dae4e8}th{background:#f0f6f7}.context{background:#f1f7f8;padding:16px}.warning{color:#97672b}.legend,small{color:#617b86;font-size:12px}svg{width:100%;height:auto}code{font-size:12px;overflow-wrap:anywhere}@media print{body{padding:0}h2{break-after:avoid}table,svg{break-inside:avoid}}</style><h1>${escape(summary.name)}</h1><div class="context"><p><b>${escape(({'current-default':'当前默认（受保护历史参考）','research-candidate':'研究候选','historical-release':'历史发布','user-result':'用户成果'})[summary.role]||'身份待核')}</b></p><p>${escape(summary.scheme?.name||'方案身份待核')} · 修订 ${escape(summary.scheme?.revision)} · ${escape(summary.evaluation.label)}</p><p>成果覆盖：${escape(summary.displayCoverage.start?.slice(0,10))}—${escape(summary.displayCoverage.end?.slice(0,10))}；${summary.unitCount} 个计算单元</p><p>本报告使用冻结完整成果。图表缩放不会修改年度评分。</p></div><h2>年度精度与样本</h2><table><thead><tr><th>年份</th><th>湖位RMSE / m</th><th>MAE / m</th><th>平均偏差 / m</th><th>湖位日数</th><th>杜加NSE</th><th>流量偏差 / %</th><th>流量日数</th></tr></thead><tbody>${metrics}</tbody></table><p class="warning">${summary.evaluation.mode==='window'?'本成果采用窗口初始化。窗口湖位误差不能作为固定锚点连续精度。':summary.evaluation.mode==='continuous'?'本成果采用固定锚点连续积分。进入默认发布仍须通过共同日期比较、初态稳定性和完整性检查。':'初始化口径未核验，不参与默认准入排序。'}</p><h2>完整湖位过程</h2>${processSvg(result.daily||[])}<h2>采用依据与适用限制</h2>${summary.limitations.map(s=>`<p>${escape(s)}</p>`).join('')}<p>当前默认保留受保护历史参考身份；新候选必须完成科学与软件准入后才可升级。</p><h2>冻结来源</h2><p>成果编号：<code>${escape(summary.id)}</code></p><p>输入版本：<code>${escape(summary.inputId)}</code></p><p>空间版本：<code>${escape(summary.spatialVersion)}</code></p><p>成果身份摘要：<code>${escape(summary.identitySHA256)}</code></p><p>观测规则：<code>${escape(summary.evaluation.qualityProfile)}</code></p><small>色林错预警预报平台 · 离线成果摘要；时间按北京时间解释。页面可直接打印为PDF。</small></html>`
}

export function downloadReport(summary,result){
 const url=URL.createObjectURL(new Blob([resultReport(summary,result)],{type:'text/html;charset=utf-8'}))
 const a=document.createElement('a');a.href=url;a.download='色林错成果摘要-'+summary.id+'.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
