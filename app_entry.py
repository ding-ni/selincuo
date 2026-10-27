"""Independent source/frozen launcher; no upstream services or paths."""
import os
import sys
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
os.environ['SELINCO_APP_ROOT'] = str(ROOT)


def running_instance(port, workspace, version):
    import json
    from urllib.request import urlopen
    from urllib.error import URLError
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1) as response:
            health=json.load(response)
    except URLError as exc:
        if getattr(exc,'code',None):raise ValueError('指定端口已有服务但不能核验身份，请先核查端口')
        return False
    if health.get('projectId')!='selinco' or Path(health.get('workspace','')).resolve()!=workspace.resolve():
        raise ValueError('指定端口已被其他服务或工作区使用，请选择其他端口')
    if health.get('version')!=version:
        raise ValueError('该工作区仍运行其他版本服务，请先退出原服务再升级；不会自动关闭服务或打开旧版页面')
    return True


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    import json
    import shutil
    import urllib.request
    import webbrowser
    import argparse
    from backend.common import WORKSPACE, VERSION, read_json, within
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
    bootstrap_parser.add_argument('--port',type=int,default=8882)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()
    target_workspace = (bootstrap_args.workspace or WORKSPACE).resolve()
    port=bootstrap_args.port
    # Probe before copying/upgrading anything. An existing same-version service
    # owns the workspace; launchers never modify its files or stop it.
    if running_instance(port,target_workspace,VERSION):
        if '--no-browser' not in sys.argv:webbrowser.open(f'http://127.0.0.1:{port}/')
        print('色林错同版本服务已运行。')
        return
    seed_info = read_json(ROOT / 'seed/current.json', {})
    seed = within(ROOT / 'seed', seed_info.get('directory', 'workspace'))
    identity_exists=(target_workspace/'workspace-identity.json').is_file()
    database_exists=(target_workspace/'database/hydrology.sqlite3').is_file()
    if not identity_exists and not database_exists:
        if target_workspace.exists() and any(target_workspace.iterdir()):
            raise ValueError('新工作区目录非空且缺少色林错身份，拒绝覆盖')
        if not seed.is_dir():raise ValueError('发行种子不存在，无法创建可用工作区')
        shutil.copytree(seed,target_workspace,dirs_exist_ok=True)
    elif identity_exists and database_exists:
        from backend.workspace_upgrade import idle_workspace,supplement_default_input
        with idle_workspace(target_workspace):
            upgrade=supplement_default_input(target_workspace,seed,ROOT)
        if upgrade['changed']:print('已校验并补齐发行默认冻结输入；原观测、任务、方案和发布记录保留。')
    else:raise ValueError('工作区身份或数据库不完整，请先恢复完整备份，拒绝覆盖')
    try:
        serve()
    except OSError as exc:
        print(f'启动失败：{exc}；请检查{port}端口，或使用 --port 其他端口。', flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
