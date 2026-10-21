"""Create a clean Selinco seed: data/input versions only, no task/state/result."""
import sys
import shutil
import sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import now, read_json, write_json, sha_file
from backend.database import Database
from backend.observations import import_observations


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'seed/workspace')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if (args.output / 'database/hydrology.sqlite3').exists():
        raise ValueError('种子目标已存在，请指定新的空目录，不覆盖既有资料')
    db = Database(args.output)
    import_observations(db)
    source = Database()
    spatial_version = read_json(ROOT / 'data/derived/spatial/manifest.json')['version']
    for item in source.items('input_products'):
        if item['manifest']['spatialVersion'] != spatial_version:
            continue
        identifier = item['id']
        origin = source.workspace / 'inputs/products' / identifier
        shutil.copytree(origin, db.workspace / 'inputs/products' / identifier)
        report = source.workspace / 'reports' / (identifier + '-station-qc.json')
        if report.is_file():
            (db.workspace / 'reports').mkdir(exist_ok=True)
            shutil.copy2(report, db.workspace / 'reports' / report.name)
        with db.connect() as conn:
            conn.execute('INSERT INTO input_products VALUES(?,?,?,?)', (identifier, item['name'], item['created_at'], __import__('json').dumps(item['manifest'], ensure_ascii=False)))
    summary = db.summary()
    assert summary['counts']['tasks'] == summary['counts']['states'] == summary['counts']['parameter_versions'] == 0
    with db.connect() as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    write_json(db.workspace / 'seed-report.json', {'createdAt': now(), 'projectId': 'selinco', 'counts': summary['counts'],
               'databaseSHA256': sha_file(db.path), 'spatialVersion': read_json(ROOT / 'data/derived/spatial/manifest.json')['version']})
    if args.activate:
        write_json(ROOT / 'seed/current.json', {'directory': db.workspace.relative_to(ROOT / 'seed').as_posix()})
    print(db.workspace)


if __name__ == '__main__':
    main()
