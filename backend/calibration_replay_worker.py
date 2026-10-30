"""Isolated launcher for an archived backend; it has no model formulas."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sys
import time
import traceback
from pathlib import Path


def message(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def _sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def record_worker_environment(folder, token):
    """Record what this child actually runs, before numerical computation."""
    runtime = folder / 'replay-runtime'
    expected = json.loads((runtime / ('worker-expected-environment-' + token + '.json'))
                          .read_text(encoding='utf-8-sig'))
    packages = {}
    evidence = {}
    important = ('numpy', 'pandas', 'numba', 'llvmlite', 'scipy', 'zarr', 'numcodecs')
    for name in expected['packages']:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
        if name not in important or packages[name] is None:
            continue
        distribution = importlib.metadata.distribution(name)
        selected = []
        for item in distribution.files or ():
            label = str(item).replace('\\', '/')
            if (label.endswith('.dist-info/RECORD') or
                    any(part in label for part in ('_multiarray_umath.', '_umath_linalg.',
                        'libllvmlite.', 'llvmlite.dll', '_dispatcher.', '_ccallback_c.', 'numcodecs/blosc.'))):
                path = distribution.locate_file(item)
                if path.is_file():
                    selected.append({'file': label, 'sha256': _sha256(path)})
        evidence[name] = sorted(selected, key=lambda row: row['file'])
    actual = {'python': platform.python_version(), 'pythonVersion': sys.version,
              'executable': str(Path(sys.executable).resolve()), 'platform': platform.platform(),
              'packages': packages, 'executableSHA256': _sha256(sys.executable),
              'numericPackageFiles': evidence}
    differences = {key: {'expected': expected.get(key), 'actual': actual.get(key)}
                   for key in ('python', 'pythonVersion', 'executable', 'platform', 'packages')
                   if expected.get(key) != actual.get(key)}
    record = {'schemaVersion': 1, 'expected': expected, 'actual': actual,
              'differences': differences,
              'note': '历史环境没有数值包文件摘要；本次摘要供后续核验，不能反证历史文件相同。'}
    path = runtime / ('worker-environment-' + token + '.json')
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    allowed_platform = ('Windows-10-10.0.26100-SP0', 'Windows-10-10.0.26200-SP0')
    if any(key != 'platform' for key in differences) or (
            'platform' in differences and (expected['platform'], actual['platform']) != allowed_platform):
        raise ValueError('重放工作进程的Python、包版本或运行环境不符合冻结条件')
    return path


class WorkerControl:
    """Treat a brief control-file read failure as a retry, not a stop."""
    def __init__(self, control, token, *, timeout=20., monotonic=None, wall_time=None):
        self.control, self.token, self.timeout = control, token, timeout
        self.monotonic = monotonic or time.monotonic
        self.wall_time = wall_time or time.time
        self.last_fresh = self.monotonic()
        self.last_heartbeat = None
        self.last_error = None
        self.reason = None

    def _stop(self, reason):
        if self.reason is None:
            self.reason = reason
            record = {'reason': reason, 'atEpoch': self.wall_time(),
                      'lastHeartbeat': self.last_heartbeat, 'lastReadError': self.last_error}
            path = self.control.parent / ('control-reason-' + self.token + '.json')
            try:
                with path.open('x', encoding='utf-8', newline='\n') as stream:
                    json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                    stream.write('\n')
            except OSError:
                pass
            print('历史重放控制结束：' + json.dumps(record, ensure_ascii=False),
                  file=sys.stderr, flush=True)
        return True

    def cancelled(self):
        if self.reason is not None:
            return True
        now = self.monotonic()
        try:
            record = json.loads(self.control.read_text(encoding='utf-8-sig'))
            if not isinstance(record, dict) or not isinstance(record.get('token'), str):
                raise ValueError('控制记录缺少有效标识')
            if record['token'] != self.token:
                return self._stop('token-changed')
            if record.get('cancel') is True:
                return self._stop('explicit-cancel')
            if record.get('cancel') is not False or isinstance(record.get('heartbeat'), bool):
                raise ValueError('控制记录缺少有效心跳或停止状态')
            heartbeat = float(record['heartbeat'])
            if not math.isfinite(heartbeat):
                raise ValueError('控制心跳不是有限数值')
            if self.wall_time() - heartbeat > self.timeout:
                return self._stop('heartbeat-stale')
            if self.last_heartbeat != heartbeat:
                self.last_heartbeat, self.last_fresh = heartbeat, now
            self.last_error = None
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.last_error = type(error).__name__ + ': ' + str(error)[:300]
        if now - self.last_fresh > self.timeout:
            return self._stop('control-unreadable' if self.last_error else 'heartbeat-stale')
        return False


def main(folder_name, source_workspace, token):
    folder = Path(folder_name).resolve()
    app = Path(os.environ['SELINCO_APP_ROOT']).resolve()
    control = folder / 'replay-runtime/control.json'

    cancelled = WorkerControl(control, token).cancelled

    def progress(value, text):
        if cancelled():
            raise InterruptedError('历史重放已停止')
        message({'type': 'progress', 'value': value, 'message': text})

    # A previous worker may briefly survive a service restart. The per-task
    # lock and changing token prevent two writers from resuming the same run.
    lock_path = folder / 'replay-runtime/worker.lock'
    with lock_path.open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        acquired = False
        deadline = time.monotonic() + 45.
        while not acquired:
            if cancelled():
                raise InterruptedError('历史重放已停止')
            lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('上一次重放仍在退出，请稍后继续')
                time.sleep(.25)
        sys.path.insert(0, str(app))
        import backend
        if Path(backend.__file__).resolve().parent != app / 'backend':
            raise ValueError('历史重放加载的代码目录不正确')
        from backend.common import digest, sha_file
        from backend.grid_simulation import model_identity
        from backend.two_stage_calibration import run
        from backend.common import model_identity as backend_identity
        if backend_identity() != 'd209aa6b038b278e913aeffce300494de3c9556f76037c6115f191837b2bfd5f' or \
                model_identity() != '6abbc3e802baa94c00c3f4489e72847086eac3123ba1ead576d8379cdee2ee7d':
            raise ValueError('历史重放代码摘要不一致')
        snapshot = folder / 'replay-snapshot'
        request_path = snapshot / 'sourceRequest.json'
        if sha_file(request_path) != 'b3acc5a7ae1d80ce0725d86389c62edb3ba13138e1b473703f1cc921bfea8581':
            raise ValueError('历史请求摘要不一致')
        for name, expected in (
                ('baselineRequest.json', '7d61a07c122148774acc1aa72b2c07db67989c84685654140086c686cc77f966'),
                ('baselineResult.json', '35acc88c3a97f2f68a35032ec63a43eb83cc1e52786867c151f24f734bcf2f50')):
            if sha_file(snapshot / name) != expected:
                raise ValueError('历史比较基准摘要不一致')
        request = json.loads(request_path.read_text(encoding='utf-8-sig'))
        baseline = {'id': 'JOB-4934a5d35c9344a59353', 'kind': 'two-stage-calibration',
                    'status': 'succeeded',
                    'request': json.loads((snapshot / 'baselineRequest.json').read_text(encoding='utf-8-sig')),
                    'result': json.loads((snapshot / 'baselineResult.json').read_text(encoding='utf-8-sig'))}

        class ReadOnlyHistory:
            workspace = Path(source_workspace).resolve()

            def task(self, identifier):
                if identifier != baseline['id']:
                    raise ValueError('历史重放不能读取未冻结的其他任务')
                return baseline

        if cancelled():
            raise InterruptedError('历史重放已停止')
        record_worker_environment(folder, token)
        run(ReadOnlyHistory(), request, folder, progress, cancelled)
        message({'type': 'completed'})


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except InterruptedError:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
