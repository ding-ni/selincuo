"""在独占 build 批次内执行旧 unittest，保留临时工作区并阻断文件删除。

默认覆盖评价归档、评价 HTTP、发布绑定、发布队列及业务注册合同。
此执行器只适配测试临时目录的保存方式，不改动旧测试或业务实现。
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid


DEFAULT_MODULES = (
    'test_evaluation_archive',
    'test_evaluation_api',
    'test_release_runtime',
    'test_release_runtime_jobs',
    'test_business_registry',
)
ROOT = Path(__file__).resolve().parents[1]


def sha_file(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def save_json(path, payload):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = call_name(node.value)
        return parent + '.' + node.attr if parent else node.attr
    return ''


def audit_sources(modules):
    """预审所选测试；业务代码中未执行的危险分支也保留位置供复核。"""
    findings, forbidden = [], []
    files = []
    interesting = {'TemporaryDirectory', 'cleanup', 'unlink', 'rmtree', 'rmdir',
                   'removedirs', 'remove', 'rename', 'replace', 'Popen', 'system'}
    for module in modules:
        source = ROOT / 'tests' / (module.split('.')[0] + '.py')
        if not source.is_file():
            raise FileNotFoundError('测试模块不存在：' + str(source))
        files.append(source)
        tree = ast.parse(source.read_text(encoding='utf-8-sig'), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            suffix = name.rsplit('.', 1)[-1]
            if suffix not in interesting:
                continue
            entry = {'file': str(source.relative_to(ROOT)).replace('\\', '/'),
                     'line': node.lineno, 'call': name}
            findings.append(entry)
            # cleanup 调用由保留型 TemporaryDirectory 接管，fixture 内的
            # rename/replace 只允许在本批次内；其他删除调用在导入前拒绝。
            if suffix in {'unlink', 'rmtree', 'rmdir', 'removedirs', 'Popen', 'system'} or name in {'os.remove', 'remove'}:
                forbidden.append(entry)
    backend_risks = []
    for source in sorted((ROOT / 'backend').rglob('*.py')):
        tree = ast.parse(source.read_text(encoding='utf-8-sig'), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = call_name(node.func)
                if name.rsplit('.', 1)[-1] in {'unlink', 'rmtree', 'rmdir', 'removedirs'} or name == 'os.remove':
                    backend_risks.append({'file': str(source.relative_to(ROOT)).replace('\\', '/'),
                                          'line': node.lineno, 'call': name,
                                          'policy': '执行时审计钩子一律阻断，不调用该清理分支'})
    return {'testFindings': findings, 'testForbiddenCalls': forbidden,
            'backendRiskLocations': backend_risks}, files


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []
        self.started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=None):
        row = {'test': test.id(), 'status': status,
               'seconds': round(time.monotonic() - self.started.get(test.id(), time.monotonic()), 6)}
        if detail is not None:
            row['detail'] = detail
        self.outcomes.append(row)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, 'failed', self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, 'error', self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, 'skipped', reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, 'expected-failure', self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, 'unexpected-success')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, 'failed-subtest', self._exc_info_to_string(err, test))


def install_preservation(batch):
    fixtures = batch / '保留临时工作区'
    fixtures.mkdir()
    retained, blocked = [], []
    original_mkdtemp = tempfile.mkdtemp

    class RetainedTemporaryDirectory:
        def __init__(self, suffix=None, prefix=None, dir=None,
                     ignore_cleanup_errors=False, *, delete=True):
            # 不继承 TemporaryDirectory，不注册 weakref finalizer。
            # 旧测试传入的 BASE 或系统目录只记录，不作为实际写入目录。
            self.name = original_mkdtemp(prefix='保留工作区-', dir=str(fixtures))
            self.entry = {'path': self.name, 'requestedDirectory': str(dir) if dir is not None else None,
                          'requestedPrefix': prefix, 'requestedSuffix': suffix, 'cleanupCalls': 0}
            retained.append(self.entry)

        def __enter__(self):
            return self.name

        def __exit__(self, exc_type, exc, traceback):
            self.cleanup()
            return False

        def cleanup(self):
            self.entry['cleanupCalls'] += 1

    def contained(value):
        if isinstance(value, int):
            return True  # 文件描述符已在 open 事件核验。
        path = Path(os.fsdecode(value)).resolve()
        return path == batch or batch in path.parents

    def deny(event, args, reason):
        blocked.append({'event': event, 'arguments': [repr(value) for value in args], 'reason': reason})
        raise PermissionError('无删除回归执行器拒绝操作：' + reason)

    def audit(event, args):
        if event in {'os.remove', 'os.rmdir', 'shutil.rmtree'}:
            deny(event, args, '任何文件删除均被禁止')
        if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.spawn'}:
            deny(event, args, '此回归不允许派生外部程序')
        if event == 'open':
            path, mode, flags = args
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
            if writing and not contained(path):
                deny(event, args, '写入目标位于本批次外')
        elif event == 'os.mkdir' and not contained(args[0]):
            deny(event, args, '新目录位于本批次外')
        elif event == 'os.rename' and not all(contained(value) for value in args[:2]):
            deny(event, args, '移动或替换涉及本批次外文件')
        elif event == 'sqlite3.connect':
            database = os.fsdecode(args[0])
            if database != ':memory:' and 'mode=ro' not in database and not contained(database):
                deny(event, args, '可写数据库位于本批次外')

    tempfile.TemporaryDirectory = RetainedTemporaryDirectory
    tempfile.tempdir = str(fixtures)
    sys.addaudithook(audit)
    return retained, blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('modules', nargs='*', help='tests 下 unittest 模块，可附类或方法名')
    parser.add_argument('--audit-only', action='store_true', help='只生成预审记录，不导入或执行测试')
    arguments = parser.parse_args()
    modules = arguments.modules or list(DEFAULT_MODULES)
    if any(not value.startswith('test_') or not all(part.isidentifier() for part in value.split('.'))
           for value in modules):
        parser.error('只接受 tests 下的 test_ 开头模块或其类/方法名')
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    batch = ROOT / 'build' / ('无删除回归-' + stamp + '-' + uuid.uuid4().hex[:10])
    batch.mkdir(parents=True, exist_ok=False)
    audit_record, selected_files = audit_sources(modules)
    snapshot_files = selected_files + sorted((ROOT / 'backend').rglob('*.py')) + [Path(__file__).resolve()]
    audit_record.update({'batch': str(batch), 'modules': modules,
                         'sourceSHA256': {str(path.relative_to(ROOT)).replace('\\', '/'): sha_file(path)
                                          for path in snapshot_files},
                         'policy': '旧测试不修改；临时工作区保留；删除、批次外写入及外部程序派生均阻断'})
    save_json(batch / '源码预审.json', audit_record)
    print('独占批次：' + str(batch), flush=True)
    if audit_record['testForbiddenCalls']:
        print('预审发现额外删除或外部程序调用；未执行测试。', flush=True)
        return 2
    if arguments.audit_only:
        print('源码预审完成；本次未执行测试。', flush=True)
        return 0

    retained, blocked = install_preservation(batch)
    os.environ['SELINCO_APP_ROOT'] = str(ROOT)
    os.environ['SELINCO_TEST_APP_ROOT'] = str(ROOT)
    os.environ['SELINCO_WORKSPACE'] = str(batch / '未显式指定的隔离工作区')
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'tests'))
    started = time.monotonic()
    with (batch / '回归日志.log').open('x', encoding='utf-8', newline='\n') as log:
        tee = Tee(sys.stdout, log)
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(stream=tee, verbosity=2, resultclass=RecordedResult).run(suite)
        report = {'batch': str(batch), 'finishedAt': datetime.now(timezone.utc).isoformat(),
                  'modules': modules, 'testsRun': result.testsRun,
                  'failures': len(result.failures), 'errors': len(result.errors),
                  'skipped': len(result.skipped), 'expectedFailures': len(result.expectedFailures),
                  'unexpectedSuccesses': len(result.unexpectedSuccesses),
                  'seconds': round(time.monotonic() - started, 6),
                  'testSuiteSuccessful': result.wasSuccessful(),
                  'passed': result.wasSuccessful() and not blocked,
                  'retainedWorkspaces': retained, 'blockedOperations': blocked,
                  'outcomes': result.outcomes,
                  'limitations': ['这是旧接口与工程回归，不认证水文精度、科学准入或安装发行。',
                                  '文件删除审计针对 Python 文件操作；SQLite 内部日志生命周期维持原实现。']}
        save_json(batch / '回归结果.json', report)
        tee.write('结果记录：' + str(batch / '回归结果.json') + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
