"""Curated immutable final results, independent of development job history."""
from .common import ROOT, read_json, sha_file, within


def catalog():
    manifest = read_json(ROOT/'data/final-results/catalog.json', {'results':[]})
    return manifest['results']


def current_id():
    manifest = read_json(ROOT/'data/final-results/catalog.json', {'results': []})
    rows = manifest['results']
    if not rows:
        return None
    identifier = manifest.get('currentResultId')
    if identifier is None and len(rows) == 1:
        identifier = rows[0]['id']
    if not any(row['id'] == identifier for row in rows):
        raise ValueError('未指定可用的标准计算成果')
    return identifier


def result_file(identifier, name):
    item = next((item for item in catalog() if item['id']==identifier), None)
    if item is None or name not in item['files']:
        raise FileNotFoundError('内置成果或导出文件不存在')
    path = within(ROOT/'data/final-results', item['directory']+'/'+name)
    if not path.is_file() or sha_file(path) != item['files'][name]['sha256']:
        raise ValueError('内置成果文件校验失败')
    return path


def detail(identifier):
    return read_json(result_file(identifier,'result.json'))
