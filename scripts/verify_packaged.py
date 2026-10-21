"""API smoke verification against a disposable packaged/installed workspace."""
import argparse
import io
import json
import time
import sys
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from backend.common import write_json, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8882')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/packaged-verification.json')
    args = parser.parse_args()
    def api(path, data=None):
        request = urllib.request.Request(args.url + '/api/model/' + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    def task(payload):
        row = api('tasks', payload)
        deadline = time.time() + 300
        while time.time() < deadline:
            row = api('tasks/' + row['id'])
            if row['status'] in ('succeeded', 'failed', 'cancelled', 'interrupted'):
                if row['status'] != 'succeeded':
                    raise RuntimeError(row['message'])
                return row
            time.sleep(.5)
        raise RuntimeError('打包验证任务超过300秒')
    def data(identifier):
        url = args.url + '/api/model/export?taskId=' + identifier + '&format=hourly.npz'
        with urllib.request.urlopen(url, timeout=120) as response:
            content = response.read()
        with np.load(io.BytesIO(content), allow_pickle=False) as arrays:
            return {k: arrays[k].copy() for k in arrays.files}
    boot = api('bootstrap')
    assert boot['projectId'] == 'selinco' and not boot['forecast']['enabled']
    assert {s['id'] for s in boot['stations']} == {'SLC', 'DUJA', 'BLQ', '115DB', 'SLC-E', 'SLC-S', 'SLC-W', 'SLC-N'}
    product = next(i for i in boot['inputs'] if i['manifest']['start'] <= '2023-01-01T00:00:00' and i['manifest']['end'] >= '2023-05-01T00:00:00' and i.get('compatible', True))
    request = {'inputId': product['id'], 'start': '2023-01-01T00:00:00', 'end': '2023-05-01T00:00:00'}
    full = task({**request, 'kind': 'simulation', 'name': '打包环境真实历史闭环验证'})
    part = task({**request, 'kind': 'simulation', 'end': '2023-04-25T12:00:00', 'name': '打包环境分段起算验证'})
    tail = task({**request, 'kind': 'continuation', 'start': '2023-04-25T13:00:00', 'stateId': part['result']['stateId'], 'parameters': part['result']['parameters'], 'name': '打包环境续算验证'})
    f, p, t = data(full['id']), data(part['id']), data(tail['id'])
    checks = {}
    for key in ('flow', 'unitStates', 'unitDiagnostics', 'routingStorage', 'lakeLevel', 'volumeChange'):
        combined = np.concatenate([p[key], t[key]])
        np.testing.assert_allclose(f[key], combined, atol=1e-8, rtol=1e-12, equal_nan=True)
        difference = np.abs(f[key] - combined)
        checks[key] = float(np.nanmax(difference)) if np.isfinite(difference).any() else None
    assert full['result']['metrics']['2023']['lake']['count'] >= 1
    backup = api('backup', {})
    archive = urllib.request.urlopen(args.url + '/api/model/download?filename=' + backup['filename'], timeout=120).read()
    request_upload = urllib.request.Request(args.url + '/api/model/upload?filename=verify-backup.zip', data=archive, method='PUT')
    uploaded = json.load(urllib.request.urlopen(request_upload, timeout=120))
    preview = api('restore-preview', {'filename': uploaded['filename']})
    restore = api('restore', {'filename': uploaded['filename'], 'token': preview['token']})
    report = {'at': now(), 'projectId': 'selinco', 'url': args.url, 'inputId': product['id'],
              'sourceKind': 'genuine ERA5-Land, user observations', 'wholeTaskId': full['id'], 'splitTaskIds': [part['id'], tail['id']],
              'continuationMaxDifference': checks, 'metrics': full['result']['metrics'], 'diagnostics': full['result']['diagnostics'],
              'backup': backup, 'restore': restore, 'freshCountsBeforeVerification': boot['database']['counts'],
              'annualCalibrationCompleted': False}
    write_json(args.output, report)
    print('Packaged runtime, real simulation, continuation, backup/restore verified:', args.output, flush=True)


if __name__ == '__main__':
    main()
