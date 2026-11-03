"""Curated immutable final results, independent of development job history."""
from .common import ROOT, digest, read_json, sha_file, within


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


def hourly_zarr(identifier, verify_files=False):
    """Return a published Zarr directory after checking its complete file index."""
    from .grid_storage import FORMAT
    item=next((row for row in catalog() if row['id']==identifier),None)
    if item is None:
        raise FileNotFoundError('内置成果不存在')
    manifest=read_json(result_file(identifier,'hourly-manifest.json'))
    task=detail(identifier)
    result=task.get('result') or {}
    artifact=result.get('hourlyArtifact') or {}
    files=manifest.get('files') if isinstance(manifest,dict) else None
    if (result.get('storageFormat')!='zarr-v2' or manifest.get('format')!=FORMAT or
            manifest.get('complete') is not True or artifact.get('format')!=FORMAT or
            artifact.get('complete') is not True or not isinstance(files,dict) or not files or
            artifact.get('files')!=files or manifest.get('sha256')!=digest(files) or
            artifact.get('sha256')!=manifest['sha256']):
        raise ValueError('内置逐时Zarr成果清单或任务身份不一致')
    folder=within(ROOT/'data/final-results',item['directory'])
    indexed=item['files']
    for name,expected in files.items():
        if (not isinstance(name,str) or not isinstance(expected,str) or len(expected)!=64 or
                any(ch not in '0123456789abcdef' for ch in expected)):
            raise ValueError('内置Zarr清单路径或摘要无效')
        relative='hourly.zarr/'+name
        row=indexed.get(relative)
        indexed_sha=row.get('sha256') if isinstance(row,dict) else row
        if indexed_sha!=expected:
            raise ValueError('内置Zarr分块与发布目录清单不一致：'+name)
        path=within(folder,relative)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError('内置Zarr分块缺失：'+name)
        if verify_files and sha_file(path)!=expected:
            raise ValueError('内置Zarr分块摘要校验失败：'+name)
    return folder/'hourly.zarr'


def available(identifier):
    """Fast integrity/index check used by bootstrap; data hashes are checked on open."""
    try:
        item=next((row for row in catalog() if row['id']==identifier),None)
        if item is None:return False
        for name in ('result.json','daily.csv','final-state.json'):
            result_file(identifier,name)
        if 'hourly-manifest.json' in item['files']:
            hourly_zarr(identifier,verify_files=False)
        elif 'hourly.npz' in item['files']:
            result_file(identifier,'hourly.npz')
        else:
            return False
        return True
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RuntimeError):
        return False


def detail(identifier):
    return read_json(result_file(identifier,'result.json'))


def calculation_record(identifier):
    """Return a compact list row for a frozen, adopted calculation."""
    item = next((row for row in catalog() if row['id'] == identifier), None)
    if item is None:
        return None
    request = item.get('requestSummary') or {}
    return {
        'id': identifier,
        'kind': item.get('kind') or 'simulation',
        'status': item.get('status') or 'succeeded',
        'created_at': item.get('created_at') or item.get('publishedAt') or '',
        'updated_at': item.get('updated_at') or item.get('created_at') or '',
        'progress': 100,
        'message': '当前采用的冻结计算成果',
        'trashed': False,
        'favorite': True,
        'userSaved': True,
        'name': item.get('name') or item.get('displayName') or identifier,
        'displayName': item.get('displayName') or item.get('name') or identifier,
        'request': {
            'schemeName': request.get('schemeName'),
            'inputId': item.get('inputId') or request.get('inputId'),
            'start': request.get('start'),
            'end': request.get('end'),
            'modelFamily': request.get('modelFamily'),
        },
        'result': None,
        'builtin': True,
    }
