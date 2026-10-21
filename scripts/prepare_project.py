"""Build reproducible raw-source index, observation archive and display layer."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import write_json, sha_file, now, read_json
from backend.database import Database
from backend.observations import import_observations


def main():
    from shapely.geometry import shape, mapping
    folder = ROOT / 'data/derived/spatial'
    geo = read_json(folder / 'map.geojson')
    if geo:
        display = {**geo, 'features': [{**f, 'geometry': mapping(shape(f['geometry']).simplify(.001, preserve_topology=True))} for f in geo['features']]}
        write_json(folder / 'display.geojson', display)
    report = import_observations(Database())
    sources = [{'path': p.relative_to(ROOT).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha_file(p)}
               for p in sorted((ROOT / 'data/raw').rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
    write_json(ROOT / 'docs/source-manifest.json', {'projectId': 'selinco', 'createdAt': now(), 'files': sources,
                                                   'originalLocation': '用户提供的原始色林错资料；复制保留原件'})
    print(f'Prepared {len(sources)} raw files; {len(report["files"])} observation sources.')


if __name__ == '__main__':
    main()
