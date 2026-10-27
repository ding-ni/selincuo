# -*- coding: utf-8 -*-
"""Freeze the reviewed source/evidence index without altering model artifacts."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, read_json, write_json, sha_file, now
from backend.database import Database
from backend.grid_simulation import model_identity

folder=ROOT/'build/grid-revision-20261026'
db=Database(folder/'workspace')
latest=read_json(folder/'extension-evidence.json')['fullResultTaskId']
task=db.task(latest)
assert task['status']=='succeeded'
assert task['result']['identity']['modelImplementation']==model_identity()
preparation=read_json(folder/'preparation.json')
assert read_json(ROOT/'data/final-results/catalog.json')==preparation['publishedCatalog']
assert sha_file(ROOT/'config/operational/historical-standard-v1.json')==preparation['standardConfigurationSHA256']
names=['preparation','spatial-audit','geometry-evidence','station-evidence',
       'baseline-evidence','comparison-evidence','calibration-evidence',
       'lake-calibration-evidence','extension-evidence','acceptance-evidence',
       'performance-evidence','exchange-evidence','full-export-evidence',
       'api-evidence','ui-evidence','latest-ui-evidence','release-readiness-evidence']
evidence={name+'.json':sha_file(folder/(name+'.json')) for name in names}
assert not read_json(folder/'ui-evidence.json')['errors']
assert not read_json(folder/'latest-ui-evidence.json')['errors']
files=[]
for directory in ('backend','frontend/src','config','scripts','tests'):
    files += [p for p in (ROOT/directory).rglob('*') if p.is_file()
              and p.suffix in ('.py','.vue','.css','.mjs','.js','.json','.ps1')
              and '__pycache__' not in p.parts]
files += list((ROOT/'frontend/dist').rglob('*'))
files += [ROOT/'requirements.txt',ROOT/'requirements-lock.windows.txt',
          ROOT/'docs/逐格网连续模拟修订与验收-20261026.md']
sources={p.relative_to(ROOT).as_posix():sha_file(p) for p in sorted(set(files)) if p.is_file()}
value={
    'at':now(),'status':'candidate-implemented-and-verified-standard-not-promoted',
    'workspace':str(db.workspace),'previewURL':'http://127.0.0.1:5175/?result='+latest,
    'report':'docs/逐格网连续模拟修订与验收-20261026.md',
    'standardResultId':preparation['publishedCatalog']['currentResultId'],
    'candidateResultId':latest,'numericModelIdentity':model_identity(),
    'verification':{'backendTests':89,'backendSeconds':30.700,'frontendTests':28,
                    'productionFrontendBuild':'passed','responsiveWidths':[1920,1440,1366,768],
                    'fullExportAllValuesEqual':True,'fullVsSplitAndExtensionEqual':True,
                    'sourceObservationsAndFormalArtifactsUnchanged':True,
                    'releaseResourceSelection':'read-only-passed'},
    'notPerformed':['standard-entry-promotion','EXE-build','installation','publication'],
    'unresolved':['2340.743 km2 drainage assignment','surveyed lake-station zero datum',
                  'new-model accuracy and boundary parameters','initial-state sensitivity',
                  'finite glacier mass and dynamic lake hypsometry'],
    'evidenceSHA256':evidence,'sourceAndFrontendSHA256':sources,
}
write_json(folder/'delivery-manifest.json',value)
print({k:v for k,v in value.items() if k not in ('evidenceSHA256','sourceAndFrontendSHA256')},flush=True)
