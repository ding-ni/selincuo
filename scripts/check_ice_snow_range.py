"""Limited 2023 warm-up / 2024 process sensitivity; never fit on 2025."""
import argparse
import sys
from copy import deepcopy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import write_json, now
from backend.operational_scheme import catalog, prepare_request
from backend.simulation import run_simulation, prepare
from backend.database import Database


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    db=Database();scheme=catalog(db)[0]
    base=prepare_request(db,{'schemeId':scheme['id'],'start':'2024-01-01','end':'2025-01-01'})
    source=prepare(db,base['inputId'])
    rows=[]
    for key,value in [('snowDdf',3.),('snowDdf',5.),('iceDdf',5.25),('iceDdf',8.75)]:
        request=deepcopy(base)
        request['parameters'][key]=value
        for region in request['partitionParameters'].values():region[key]=value
        result=run_simulation(db,request,prepared=source)
        diagnostics=result['modelDiagnostics'];cryosphere=diagnostics['cryosphere']
        rows.append({'parameter':key,'value':value,'metrics2024':result['metrics']['2024'],
                     'catchmentBalanceMaxErrorM3':diagnostics['catchmentBalanceMaxErrorM3'],
                     'unitBalanceMaxErrorMm':result['diagnostics']['landGlacierMaxBalanceErrorMm'],
                     'cryosphere':cryosphere})
        write_json(args.output,{'at':now(),'developmentYear':2024,'withheldYearUsed':False,
              'parametersActivated':False,'rows':rows,
              'decision':'Retain fixed priors; lack of independent snow/ice observations prevents process validation from lake fit alone.'})
        print(key,value,result['metrics']['2024']['lake']['rmse'],flush=True)


if __name__=='__main__':main()
