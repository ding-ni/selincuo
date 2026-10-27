# -*- coding: utf-8 -*-
"""Check live preview contracts against saved real observations and result chunks."""
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, digest, read_json, sha_file, write_json
from backend.database import Database
from backend.grid_storage import open_arrays
from backend.forcing_latest import acquire_latest
import numpy as np
import pandas as pd
import requests

out = ROOT / 'build/grid-revision-20261026'
db = Database(out / 'workspace')
extension = read_json(out / 'extension-evidence.json')
identifier = extension['fullResultTaskId']
base = 'http://127.0.0.1:8882/api/model/'


def get(path, **params):
    response = requests.get(base + path, params=params, timeout=90)
    response.raise_for_status()
    return response.json()


started = time.monotonic()
candidate = get('datum-candidates')
assert digest(candidate) == digest(get('datum-candidates'))
archived = db.workspace / 'reports/observation-candidates' / (candidate['id'] + '.json')
assert digest(read_json(archived)) == digest(candidate)
meta = get('result-capabilities', resultId=identifier)
assert len(meta['units']) == 1597 and meta['canContinue'] and not meta['inflowComplete']
standard_id = read_json(ROOT / 'data/final-results/catalog.json')['currentResultId']
old_state = get('result-checkpoint', resultId=standard_id)
assert len(old_state['units']) == 30 and len(old_state['variables']) == 8
states = get('states')['states']
state_id = next(s['id'] for s in states if s['task_id'] == identifier)
own_state = get('states/' + state_id)
assert [u['id'] for u in own_state['units']] == own_state['state']['unitIds']
assert own_state['spatialVersion'] == meta['spatialVersion']

arrays = open_arrays(db.workspace / 'tasks' / identifier)
times = pd.DatetimeIndex(arrays['time'][:].astype('datetime64[ns]'))
index = times.get_loc(pd.Timestamp('2025-06-02T00:00:00'))
checks = []
for variable, expected in (
    ('swe', arrays['unitStates'][index, :, 0]),
    ('RS', arrays['unitDiagnostics'][index-23:index+1, :, 0].sum(axis=0)),
    ('flow', arrays['flow'][index-23:index+1].mean(axis=0)),
):
    frame = get('result-frame', resultId=identifier, variable=variable,
                time='2025-06-02T00:00:00', step='day')
    np.testing.assert_array_equal([v['value'] for v in frame['values']], expected)
    series = get('result-series', resultId=identifier, variable=variable,
                 unit=0, start='2025-06-01', end='2025-06-02', step='day')
    np.testing.assert_allclose(series['rows'][0]['value'], expected[0], atol=1e-10, rtol=0)
    checks.append({'variable': variable, 'aggregation': frame['aggregation'], 'actualChunksEqual': True})

coverage = get('forcing-coverage')
assert coverage['update']['inputId'] == extension['inputId']
manifest = read_json(db.workspace / 'inputs/products' / extension['inputId'] / 'manifest.json')
from backend.forcing_latest import provenance_summary
provenance = provenance_summary(db, manifest)
assert provenance['hasProvisional']
with patch('cdsapi.Client', side_effect=AssertionError('同目录已覆盖时不应重复下载')):
    latest = acquire_latest(db)
assert latest['status'] == 'up-to-date' and latest['inputId'] == extension['inputId']

preparation = read_json(out / 'preparation.json')
assert read_json(ROOT / 'data/final-results/catalog.json') == preparation['publishedCatalog']
assert sha_file(ROOT / 'config/operational/historical-standard-v1.json') == preparation['standardConfigurationSHA256']
for filename, info in preparation['publishedCatalog']['results'][0]['files'].items():
    assert sha_file(ROOT / 'data/final-results' / preparation['publishedCatalog']['results'][0]['directory'] / filename) == info['sha256']
original = Database(ROOT / 'workspace')
tables = {}
for table in ('observations', 'sources'):
    tables[table] = digest(original.rows('SELECT * FROM ' + table + ' ORDER BY id')) == digest(db.rows('SELECT * FROM ' + table + ' ORDER BY id'))
assert all(tables.values())
tasks = get('tasks')['tasks']
assert not any(t['id'] in ('JOB-ccca7c64a9b74275b590', 'JOB-248e3bd470854d9cb2b6') for t in tasks)

evidence = {
    'seconds': time.monotonic() - started,
    'datumArchiveDeterministic': True, 'candidateId': candidate['id'],
    'gridStateUnits': len(own_state['units']), 'legacyStateOwnUnits': len(old_state['units']),
    'stateColumns': len(own_state['variables']), 'canContinue': meta['canContinue'],
    'dailyFrames': checks, 'sameCatalogUpdate': latest['status'],
    'latestInputId': latest['inputId'], 'provisionalDataExplicit': provenance['hasProvisional'],
    'sourceTablesUnchanged': tables, 'allPublishedFileDigestsUnchanged': True,
    'standardConfigurationAndEntryUnchanged': True, 'acceptanceTasksAbsentFromBusiness': True,
}
write_json(out / 'api-evidence.json', evidence)
print(evidence, flush=True)
