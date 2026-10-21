"""Run genuine baseline, sequential 2024 calibration and frozen validation."""
import argparse
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import write_json, now
from backend.database import Database
from backend.forcing import register_era5
from backend.jobs import Jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runoff-budget', type=int, default=120)
    parser.add_argument('--lake-budget', type=int, default=30)
    parser.add_argument('--end', default='2026-01-01T00:00:00')
    args = parser.parse_args()
    db = Database()
    paths = sorted((db.workspace / 'downloads/era5-land').glob('*.nc'))
    product = register_era5(paths, db)
    if product['start'] > '2023-01-01T01:00:00' or product['end'] < args.end:
        raise ValueError('真实驱动尚未覆盖完整2023预热、2024率定与2025独立验证，不能形成正式年度报告')
    jobs = Jobs(db)
    def wait(task):
        while True:
            record = db.task(task['id'])
            if record['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
                if record['status'] != 'succeeded':
                    raise RuntimeError(record['message'])
                return record
            time.sleep(1)
    try:
        request = {'inputId': product['id'], 'start': '2023-01-01T00:00:00', 'end': args.end}
        baseline = wait(jobs.submit('simulation', {**request, 'name': '真实历史未率定基线'}))
        runoff = wait(jobs.submit('calibration', {**request, 'name': '2024杜加产汇流率定', 'stage': 'runoff', 'maxEvaluations': args.runoff_budget}))
        lake = wait(jobs.submit('calibration', {**request, 'name': '2024主站湖泊率定及独立验证', 'stage': 'lake', 'baseTaskId': runoff['id'], 'maxEvaluations': args.lake_budget}))
        db.save_item('parameter_versions', 'PARAM-HISTORICAL-' + lake['id'][4:], '2024率定冻结参数', {'sourceTaskId': lake['id'], 'parameters': lake['result']['parameters'], 'calibrationYear': 2024})
        write_json(ROOT / 'docs/historical-results.json', {'createdAt': now(), 'input': product,
                    'baseline': baseline['result'], 'runoff': runoff['result'], 'lake': lake['result']})
        print('Historical workflow complete:', lake['id'], flush=True)
    finally:
        jobs.close()


if __name__ == '__main__':
    main()
