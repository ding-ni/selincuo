"""记录同一Python解释器的依赖及运行服务身份，不修改服务或计算任务。"""
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
from urllib.request import urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
TASKS = ['JOB-6536f36e07cc46868255', 'JOB-05c3f62282ce4239aed1']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    with urlopen('http://127.0.0.1:8882/api/health', timeout=30) as response:
        health = json.load(response)
    packages = {}
    for name in ('numpy', 'pandas', 'scipy', 'numba', 'llvmlite', 'zarr', 'numcodecs',
                 'openpyxl', 'xlrd', 'shapely', 'pyproj'):
        packages[name] = importlib.metadata.version(name)
    binaries = {}
    for name in ('numpy.core._multiarray_umath', 'numba._dispatcher', 'numcodecs.blosc'):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        binaries[name] = {'path': str(path), 'sha256': sha(path)}
    from datetime import datetime
    result = {'recordedAt': datetime.now().astimezone().isoformat(), 'tasks': TASKS,
        'service': health, 'probe': {'python': platform.python_version(),
        'pythonVersion': sys.version, 'executable': str(Path(sys.executable).resolve()),
        'executableSHA256': sha(sys.executable), 'platform': platform.platform(),
        'packages': packages, 'binaryModules': binaries},
        'scope': '同一解释器启动的独立只读探测进程；服务PID及模块列表另存。'
                 '不把探测进程的系统版本报告当作模型进程的直接报告。',
        'requirementsLockSHA256': sha(ROOT / 'requirements-lock.windows.txt')}
    path = ROOT / 'docs/evidence' / ('双驱动率定环境-' + uuid.uuid4().hex[:10] + '.json')
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')
    print(json.dumps({'path': str(path), 'python': result['probe']['python'],
                      'packages': packages}, ensure_ascii=False))


if __name__ == '__main__':
    main()
