"""只读核验本轮正式采用重启后的配置、历史成果及数值身份。"""
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, WORKSPACE, now, read_json, sha_file
from backend.calibration_configuration import implementation


def get(path):
    with urlopen('http://127.0.0.1:8882/' + path, timeout=90) as response:
        return json.load(response)


def main():
    before = read_json(ROOT / 'build/双驱动采用重启前-20261031.json')
    health = get('api/health')
    bootstrap = get('api/model/bootstrap')
    current = bootstrap['currentBusiness']
    config = get('api/model/schemes/' + current['schemeId'] + '/calibration-configuration')
    with sqlite3.connect('file:' + (WORKSPACE / 'database/hydrology.sqlite3').as_posix() + '?mode=ro',
                         uri=True, timeout=30) as connection:
        active = connection.execute('SELECT id FROM tasks WHERE status IN (?,?,?)',
                                    ('running', 'queued', 'stopping')).fetchall()
        historic = connection.execute('SELECT id,status,trashed FROM tasks WHERE id IN (?,?,?)',
            ('JOB-88b35b44b27d4e15840c', 'JOB-6536f36e07cc46868255', 'JOB-05c3f62282ce4239aed1')).fetchall()
    comparison_path = ROOT / 'docs/evidence/双驱动完整率定对比-20261030-e35e1a5019f0.json'
    comparison = read_json(comparison_path)
    before_current = before['currentBusiness']
    adoption_time_equal = (datetime.fromisoformat(before_current['adoptedAt'])
                           == datetime.fromisoformat(current['adoptedAt']))
    adoption_fields_equal = ({key: value for key, value in before_current.items() if key != 'adoptedAt'}
                             == {key: value for key, value in current.items() if key != 'adoptedAt'})
    result_checks = {name: sha_file(ROOT / row['resultPath']) == row['resultSHA256']
                     for name, row in comparison['tasks'].items()}
    checks = {
        'newServiceInstance': before['health']['instanceId'] != health['instanceId'],
        'sameWorkspace': before['health']['workspace'] == health['workspace'],
        'adoptionPersisted': adoption_fields_equal and adoption_time_equal,
        'sameExecutableConfiguration': before['configurationSHA256'] == config['configurationSHA256'],
        'frozenNumericalImplementation': before['implementation'] == config['implementation'] == implementation(),
        'configurationStillExecutable': config['matchesCurrentImplementation'],
        'noActiveTasks': not active,
        'allHistoricResultsRetained': len(historic) == 3 and all(row[1:] == ('succeeded', 0) for row in historic),
        'originalResultBytesUnchanged': all(result_checks.values()),
        'warmRainSelected': current['resultId'] == 'JOB-05c3f62282ce4239aed1',
    }
    if not all(checks.values()):
        raise ValueError('采用重启核对失败：' + json.dumps(checks, ensure_ascii=False))
    report = {'createdAt': now(), 'before': before, 'after': {'health': health, 'currentBusiness': current,
        'configurationSHA256': config['configurationSHA256']}, 'checks': checks,
        'historicalTasks': historic, 'resultBytesChecks': result_checks,
        'adoptionComparison': {'sameFieldsExceptTimestamp': adoption_fields_equal,
            'sameTimestampInstant': adoption_time_equal,
            'timestampRepresentation': '原始前后记录均保留；采用时刻按带时区的同一时刻比较，允许北京时间与UTC表示。'},
        'comparison': {'path': comparison_path.relative_to(ROOT).as_posix(), 'sha256': sha_file(comparison_path)},
        'adoptionReason': '暖雨方案通过最终预热和干湿初态检查，湖面与陆域液态暖雨处理一致；原驱动联合分数虽低，最终参数预热未通过并有11项边界参数，保留为对照。',
        'reportedLimitations': ['暖雨正式杜加PBIAS为−21.9188%，流量拟合弱于旧采用。',
            '分期暖雨2025流量NSE为−0.136674，2025—2026湖位时间检验RMSE为0.392511m。',
            '两套原冻结目标和优化最优结果均保留，采用判断未改写优化评分。'],
        'resumeRepairChecks': {'testFile': 'tests/test_calibration_resume_trace.py',
            'testFileSHA256': sha_file(ROOT / 'tests/test_calibration_resume_trace.py'),
            'command': 'python -B -X utf8 -m unittest discover -s tests -p test_calibration_resume_trace.py -v',
            'passedTests': 7, 'scope': '已实际运行的隔离检查；不重新运行生产率定。'}}
    path = ROOT / 'docs/evidence' / ('双驱动采用与重启-20261031-' + uuid.uuid4().hex[:8] + '.json')
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'path': str(path), 'sha256': sha_file(path), 'checks': checks}, ensure_ascii=False))


if __name__ == '__main__':
    main()
