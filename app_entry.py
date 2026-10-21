"""Independent source/frozen launcher; no upstream services or paths."""
import os
import sys
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
os.environ['SELINCO_APP_ROOT'] = str(ROOT)


def main():
    import json
    import shutil
    import urllib.request
    import webbrowser
    import argparse
    from backend.common import WORKSPACE, read_json, within
    from backend.database import Database
    from backend.server import main as serve
    if '--self-check' in sys.argv:
        import h5py
        from backend.model_core import run_kernel, initial_states, parameters, PARAMETER_NAMES
        import numpy as np
        p = parameters()
        d, h, s = run_kernel(np.zeros((2, 1)), np.zeros((2, 1)), np.zeros((2, 1)), np.array([False]),
                             initial_states([{'kind': 'land'}], p), np.array([p[k] for k in PARAMETER_NAMES]))
        print(json.dumps({'projectId': 'selinco', 'hdf5': h5py.version.hdf5_version,
                          'modelCheck': float(abs(d[:, :, 7]).max()), 'root': str(ROOT)}, ensure_ascii=False), flush=True)
        return
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument('--workspace', type=Path)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()
    target_workspace = (bootstrap_args.workspace or WORKSPACE).resolve()
    seed_info = read_json(ROOT / 'seed/current.json', {})
    seed = within(ROOT / 'seed', seed_info.get('directory', 'workspace'))
    if not (target_workspace / 'workspace-identity.json').is_file() and not (target_workspace / 'database/hydrology.sqlite3').exists() and seed.is_dir():
        shutil.copytree(seed, target_workspace, dirs_exist_ok=True)
    # Open only a verified Selinco instance when port already belongs to us.
    if '--port' not in sys.argv:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8879/api/health', timeout=1) as response:
                health = json.load(response)
            if health.get('projectId') == 'selinco' and Path(health.get('workspace', '')).resolve() == target_workspace:
                if '--no-browser' not in sys.argv:
                    webbrowser.open('http://127.0.0.1:8879/')
                print('色林错服务已运行。')
                return
        except Exception:
            pass
    try:
        serve()
    except OSError as exc:
        print(f'启动失败：{exc}；请检查8879端口，或使用 --port 其他端口。', flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
