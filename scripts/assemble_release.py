import sys
import shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import write_json, sha_file, now, read_json, within

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=ROOT / 'build/verified-dist/SelincoPlatform')
    args = parser.parse_args()
    dest = args.destination.resolve()
    if not dest.is_relative_to(ROOT / 'build'):
        raise ValueError('发布组装仅允许本项目build目录')
    for name in ['backend', 'config', 'frontend/dist', 'data/raw', 'data/derived', 'docs', 'scripts']:
        origin = ROOT / name
        if origin.is_dir():
            shutil.copytree(origin, dest / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.nbi', '*.nbc', 'active-workspace.json'))
    seed = within(ROOT / 'seed', read_json(ROOT / 'seed/current.json', {}).get('directory', 'workspace'))
    if not seed.is_dir():
        raise ValueError('请先创建干净种子')
    shutil.copytree(seed, dest / 'seed/workspace', dirs_exist_ok=True)
    for name in ['README.md', 'requirements.txt']:
        shutil.copy2(ROOT / name, dest / name)
    def included(path):
        relative = path.relative_to(dest)
        return relative.parts[0] not in ('workspace', 'restored-workspaces') and relative.as_posix() != 'config/active-workspace.json'
    write_json(ROOT / 'releases/build-manifest.json', {'createdAt': now(), 'projectId': 'selinco',
                'files': [{'path': p.relative_to(dest).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha_file(p)}
                          for p in sorted(dest.rglob('*')) if p.is_file() and included(p)]})
    print(dest)

if __name__ == '__main__':
    main()
