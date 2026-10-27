"""Create a source snapshot and a clean preview by copying the real workspace."""
import json,sqlite3,sys,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import sha_file,write_json,now,read_json

def main():
    folder=ROOT/'build/grid-revision-20261026'
    if (folder/'preparation.json').exists():
        print((folder/'workspace').resolve());return
    folder.mkdir(parents=True,exist_ok=True)
    snapshot=folder/'before-source'
    for name in ('backend','frontend/src','config','scripts','tests','docs'):
        shutil.copytree(ROOT/name,snapshot/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.nbi','*.nbc'),dirs_exist_ok=True)
    for name in ('requirements.txt','README.md'):shutil.copy2(ROOT/name,snapshot/name)
    source=ROOT/'workspace';destination=folder/'workspace'
    if destination.exists():raise ValueError('新的业务预览目录已存在，拒绝覆盖')
    destination.mkdir()
    shutil.copy2(source/'workspace-identity.json',destination/'workspace-identity.json')
    (destination/'database').mkdir()
    with sqlite3.connect((source/'database/hydrology.sqlite3').as_uri()+'?mode=ro',uri=True) as a,sqlite3.connect(destination/'database/hydrology.sqlite3') as b:a.backup(b)
    for name in ('inputs','observation-sources','downloads','reports','tasks'):
        if (source/name).is_dir():shutil.copytree(source/name,destination/name)
    write_json(folder/'preparation.json',{'at':now(),'source':str(source),'destination':str(destination),'sourceDatabaseSHA256':sha_file(source/'database/hydrology.sqlite3'),'backupDatabaseSHA256':sha_file(destination/'database/hydrology.sqlite3'),'standardConfigurationSHA256':sha_file(ROOT/'config/operational/historical-standard-v1.json'),'publishedCatalog':read_json(ROOT/'data/final-results/catalog.json')})
    print(destination)

if __name__=='__main__':main()
