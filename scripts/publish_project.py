"""Copy the independent source/data project into a NEW explicit destination.

No files are moved or removed. Existing destination contents cause refusal.
Development tasks and caches are excluded; a clean data seed initializes the
new workspace. Publicly obtained original NetCDF files are retained separately.
"""
import argparse
import shutil
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, sha_file, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT):
        raise ValueError('独立目标须位于当前工程目录之外')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('目标目录不是空目录，拒绝覆盖既有资料')
    selected_seed = read_json(ROOT / 'seed/current.json', {'directory': 'workspace'})['directory']
    def include(p):
        relative = p.relative_to(ROOT)
        parts = relative.parts
        if parts[0] in ('.git', 'build', 'workspace', 'releases') or any(v in parts for v in ('__pycache__', 'node_modules', '.pytest_cache')):
            return False
        if p.suffix in ('.pyc', '.nbc', '.nbi', '.log') or p.name in ('.env', '.cdsapirc', 'package-lock.upstream.json', 'active-workspace.json'):
            return False
        if parts[0] == 'seed' and relative.as_posix() != 'seed/current.json' and not relative.as_posix().startswith('seed/' + selected_seed + '/'):
            return False
        return True
    files = [p for p in ROOT.rglob('*') if p.is_file() and include(p)]
    destination.mkdir(parents=True, exist_ok=True)
    for p in files:
        output = destination / p.relative_to(ROOT)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, output)
    raw = ROOT / 'workspace/downloads/era5-land'
    target = destination / 'workspace/downloads/era5-land'
    target.mkdir(parents=True, exist_ok=True)
    climate = []
    for p in sorted(raw.glob('*')):
        if p.is_file() and p.suffix in ('.nc', '.json'):
            shutil.copy2(p, target / p.name)
            if p.suffix == '.nc':
                climate.append({'path': p.name, 'bytes': p.stat().st_size, 'sha256': sha_file(p)})
    write_json(target / 'retained-source-manifest.json', {'createdAt': now(), 'source': 'Copernicus ERA5-Land', 'files': climate})
    for folder in ('releases', 'build'):
        (destination / folder).mkdir(exist_ok=True)
    write_json(destination / 'docs/independent-copy.json', {'createdAt': now(), 'projectId': 'selinco',
               'sourceFileCount': len(files), 'climateFileCount': len(climate), 'developmentTasksCopied': False,
               'originalFilesMoved': False})
    print(destination)


if __name__ == '__main__':
    main()
