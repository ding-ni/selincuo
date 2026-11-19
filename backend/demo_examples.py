"""Read the demonstration snapshots shipped with the offline installation."""
from .common import ROOT, read_json, within


DIRECTORY = 'data/derived/spatial/离线演示'
DEMO_ROOT = ROOT / DIRECTORY


def index():
    return read_json(DEMO_ROOT / 'demo.json')


def catalog():
    bundle = index()
    if bundle is None:
        return None
    return {
        'forecast': {k: bundle['forecast'][k] for k in ('id', 'name', 'issueTime', 'hours')},
        'scenarios': [{k: row[k] for k in ('id', 'name', 'resultId', 'peakFrame')}
                      for row in bundle['scenarios']],
    }


def forecast(identifier, history=False):
    bundle = index()
    if not bundle or bundle['forecast']['id'] != identifier:
        raise FileNotFoundError('离线演示预报不存在')
    return bundle['history' if history else 'forecast']


def risk_result(identifier):
    result = read_json(within(DEMO_ROOT / 'results', identifier) / '结果.json')
    if not result or result['id'] != identifier:
        raise FileNotFoundError('离线漫溢结果不存在')
    return result
