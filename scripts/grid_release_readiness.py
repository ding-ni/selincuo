# -*- coding: utf-8 -*-
"""Read-only resource selection audit; does not assemble, build, install or publish."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, read_json, within, write_json
from backend.published_results import catalog, current_id
from scripts.assemble_release import inspect_seed, selected_resources

seed = within(ROOT / 'seed', read_json(ROOT / 'seed/current.json')['directory'])
counts, scheme = inspect_seed(seed)
selected = [r for r in catalog() if r['id'] == current_id()]
assert len(selected) == 1
resources = selected_resources(scheme, selected)
relative = [p.relative_to(ROOT).as_posix() for p in resources]
assert not any('/GRID-SPATIAL-' in p or p.endswith('/grid-current.json') for p in relative)
evidence = {
    'mode': 'read-only-selection-audit', 'assembled': False, 'exeBuilt': False, 'published': False,
    'seed': str(seed), 'seedCounts': counts,
    'standardSchemeId': scheme['id'], 'standardSpatialVersion': scheme['spatialVersion'],
    'selectedResultId': selected[0]['id'],
    'researchSpatialResourcesExcluded': True,
    'resourceBytes': sum(p.stat().st_size for p in resources),
    'files': relative,
}
write_json(ROOT / 'build/grid-revision-20261026/release-readiness-evidence.json', evidence)
print({k: v for k, v in evidence.items() if k != 'files'}, flush=True)
