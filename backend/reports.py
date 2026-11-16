"""Chinese PDF and machine-readable reports from saved operational records."""
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from .common import ROOT, now, sha_file, write_json
from . import operational_store as store


LOCAL = timezone(timedelta(hours=8))
TYPES = {'monitoring-daily': '监测日报', 'warning-weekly': '预警周报',
         'warning': '预警单', 'comparison': '应急方案对比报告'}


def catalog(db):
    return {'reports': store.list_records(db, 'reports'), 'types': TYPES}


def due_reports(db, time=None):
    current = time or datetime.now(LOCAL)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LOCAL)
    current = current.astimezone(LOCAL)
    if current.hour < 8:
        return []
    requests = [('monitoring-daily', current.strftime('%Y-%m-%d'))]
    if current.weekday() == 0:
        requests.append(('warning-weekly', current.strftime('%G-W%V')))
    completed = {(r.get('reportType'), r.get('periodKey')) for r in store.list_records(db, 'reports')}
    return [{'reportType': kind, 'periodKey': period} for kind, period in requests if (kind, period) not in completed]


def _font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    if 'SelincoChinese' in pdfmetrics.getRegisteredFontNames():
        return 'SelincoChinese'
    candidates = [Path('C:/Windows/Fonts/msyh.ttc'), Path('C:/Windows/Fonts/simsun.ttc'),
                  ROOT/'data/fonts/中文字体.ttf']
    for path in candidates:
        if path.is_file():
            try:
                pdfmetrics.registerFont(TTFont('SelincoChinese', str(path), subfontIndex=0))
                return 'SelincoChinese'
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    return 'STSong-Light'


def _data(db, request):
    kind = request.get('reportType', 'monitoring-daily')
    if kind not in TYPES:
        raise ValueError('报告类型无效')
    data = {'title': TYPES[kind], 'reportType': kind, 'createdAt': now(),
            'periodKey': request.get('periodKey'), 'sections': [], 'rows': []}
    if kind == 'monitoring-daily':
        time = datetime.now(LOCAL)
        day = request.get('date') or (time-timedelta(days=1)).strftime('%Y-%m-%d')
        start = day+'T00:00:00'
        end = day+'T23:59:59'
        rows = db.rows('''SELECT o.station_id,s.name,o.variable,o.step,o.unit,COUNT(o.value) AS count,
            MIN(o.value) AS minimum,MAX(o.value) AS maximum,AVG(o.value) AS mean,
            MIN(o.time) AS firstTime,MAX(o.time) AS lastTime FROM observations o
            JOIN stations s ON s.id=o.station_id
            JOIN sources src ON src.id=o.source_id
            WHERE o.time>=? AND o.time<=? AND o.value IS NOT NULL AND o.quality='valid'
            AND o.id=(SELECT o2.id FROM observations o2 JOIN sources src2 ON src2.id=o2.source_id
                WHERE o2.station_id=o.station_id AND o2.variable=o.variable AND o2.step=o.step AND o2.time=o.time
                ORDER BY src2.imported_at DESC,o2.id DESC LIMIT 1)
            GROUP BY o.station_id,s.name,o.variable,o.step,o.unit ORDER BY o.station_id,o.variable,o.step''', (start, end))
        data.update(date=day, rows=rows)
        data['sections'].append({'title': '观测摘要 · '+day,
            'lines': [r['name']+' / '+r['variable']+' ('+r['step']+')：有效记录'+str(r['count'])+'条，范围'+
                      _value(r['minimum'])+'～'+_value(r['maximum'])+' '+r['unit'] for r in rows]
                    or ['该日暂无有效观测记录。']})
        connections = store.list_records(db, 'data-connections')
        data['sections'].append({'title': '资料更新', 'lines': [str(c.get('name', c['id']))+'：'+
            str(c.get('lastSync', {}).get('completedAt') if c.get('lastSync', {}).get('status') == 'succeeded'
                else c.get('lastSuccessAt') or '尚无成功同步') for c in connections] or ['未配置外部连接。']})
    elif kind == 'warning-weekly':
        cutoff = datetime.now(timezone.utc)-timedelta(days=7)
        rows = [r for r in store.list_records(db, 'warnings')
                if datetime.fromisoformat(r['createdAt'].replace('Z', '+00:00')) >= cutoff]
        data['rows'] = rows
        data['sections'].append({'title': '近七天预警与情景提示', 'lines': [
            r['name']+'；状态：'+r['status']+'；有效至'+r['validUntil']+'；依据预测'+r['forecastId']
            for r in rows] or ['近七天无预警或情景提示记录。']})
    elif kind == 'warning':
        warning = store.get(db, 'warnings', request['warningId'])
        data['warning'] = warning
        match = warning['assessment']['selected']
        data['sections'].append({'title': warning['name'], 'lines': [
            '类别：'+('业务预警' if warning['mode'] == 'business' else '情景风险提示'),
            '状态：'+warning['status']+'；有效至：'+warning['validUntil'],
            '阈值：'+_value(match['levelM'])+' m；最大超限成员比例：'+_value(100*match['maximumProportion'])+'%',
            '首次达到判据：'+match['firstTime'], '预测编号：'+warning['forecastId'],
            '判据来源：'+(warning['ruleSnapshot'].get('source') or '用户情景配置'),
            '发布人：'+warning.get('operator', '待人工确认'), warning.get('notes', '')]})
    elif kind == 'comparison':
        risk = store.get(db, 'risk-results', request['riskResultId'])
        data['riskResultId'] = risk['id']
        data['rows'] = risk['comparisons']
        data['sections'].append({'title': '方案结果', 'lines': [
            r['name']+'：最高湖位'+_value(r['peakLevelM'])+' m；新增潜在淹没'+
            _value(r['inundatedAreaKm2'])+' km²；溢流体积'+_value(r['spillVolumeM3'])+
            ' m³；人工泄流体积'+_value(r.get('releaseVolumeM3', 0))+' m³；总出流'+
            _value(r.get('totalOutflowM3', r['spillVolumeM3']))+' m³；受影响对象'+str(r['effects']['count'])+'个；道路'+
            _value(r['effects']['roadLengthM'])+' m' for r in risk['comparisons']]})
        data['sections'].append({'title': '计算范围', 'lines': risk['limitations']})
    return data


def _value(value):
    return '缺测' if value is None else format(float(value), '.5g')


def render_pdf(path, data):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    font = _font()
    title = ParagraphStyle('TitleChinese', fontName=font, fontSize=20, leading=28, textColor=colors.HexColor('#17394a'))
    heading = ParagraphStyle('HeadingChinese', fontName=font, fontSize=12, leading=20,
                             spaceBefore=16, textColor=colors.HexColor('#234f60'))
    text = ParagraphStyle('TextChinese', fontName=font, fontSize=10, leading=17, wordWrap='CJK')
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=45, leftMargin=45, topMargin=45, bottomMargin=45)
    story = [Paragraph(escape(data['title']), title), Spacer(1, 12),
             Paragraph('编制时间：'+escape(data['createdAt']), text)]
    for section in data['sections']:
        story.append(Paragraph(escape(section['title']), heading))
        for line in section['lines']:
            if line:
                story.append(Paragraph(escape(str(line)), text))
    doc.build(story)


def run(db, request, folder=None, progress=None, cancelled=None):
    if cancelled and cancelled():
        raise InterruptedError('已停止报告生成')
    data = _data(db, request)
    folder = Path(folder) if folder else db.workspace/'reports'/datetime.now(LOCAL).strftime('%Y%m%d%H%M%S%f')
    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder/(data['title']+'.pdf')
    js = folder/(data['title']+'.json')
    render_pdf(pdf, data)
    write_json(js, data)
    artifacts = []
    files = [pdf, js]
    if data['rows']:
        csv_path = folder/(data['title']+'.csv')
        rows = data['rows']
        keys = list(dict.fromkeys(key for row in rows for key in row))
        with csv_path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows([{k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                              for k, v in r.items()} for r in rows])
        files.append(csv_path)
    task_id = folder.name
    for path in files:
        artifacts.append({'name': path.name, 'filename': path.name, 'bytes': path.stat().st_size,
            'storage': 'task', 'ownerTaskId': task_id, 'complete': True,
            'mtimeNs': path.stat().st_mtime_ns, 'sha256': sha_file(path),
            'downloadUrl': '/api/model/task-artifact?'+urlencode({'taskId': task_id, 'name': path.name})})
    result = store.save(db, 'reports', {k: v for k, v in data.items() if k != 'sections'} | {
        'name': data['title'], 'artifacts': artifacts, 'taskId': task_id, 'status': 'succeeded'}, prefix='REPORT-')
    write_json(folder/'result.json', result)
    if progress:
        progress(100, data['title']+'已生成')
    return result
