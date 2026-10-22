# coding: utf-8
"""Research-only initial soil-water sensitivity, never a calibration input."""
import sys
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.common import read_json,write_json,now
from backend.database import Database
from backend.model_core import initial_states
from backend.simulation import run_simulation,observation_snapshot

def main():
    report=read_json(ROOT/'docs/routing-partition-v0.6.0.json')
    db=Database();snapshot=observation_snapshot(db)
    result={'createdAt':now(),'researchOnly':True,'defaultFraction':.5,'selectionApplied':False,
            'description':'Only initial three-layer tension water changes; all other inputs, routing, ice/snow and parameters frozen.', 'cases':{}}
    for key in ('reference','flow'):
        entry=report['cases'][key]['routing']
        rows=[]
        for fraction in (.25,.75):
            def initialize(units,p):
                state=initial_states(units,p)
                state[:,2:5]*=fraction/.5
                return state
            with patch('backend.simulation.initial_states',side_effect=initialize):
                r=run_simulation(db,{'inputId':'INPUT-967613234ca58f1c6cfb44e2','start':'2023-01-01',
                    'end':'2026-01-01','routingMode':'source-separated','parameters':entry['parameters'],'_observations':snapshot})
            difference={y:{variable:{metric:r['metrics'][y][variable][metric]-entry['metrics'][y][variable][metric]
                for metric in (['nse','logNse','pbias'] if variable=='flow' else ['rmse'])} for variable in ('flow','lake')} for y in ('2024','2025')}
            rows.append({'initialTensionFraction':fraction,'metrics':r['metrics'],'differenceFromHalfFull':difference,
                         'annualUnitStorageChangeM3':{y:sum(m['unitStorageChangeM3'] for m in r['modelDiagnostics']['monthly'] if m['month'].startswith(y)) for y in ('2023','2024','2025')}})
        result['cases'][key]=rows
    write_json(ROOT/'docs/initial-storage-v0.6.0.json',result)
    print('Initial storage sensitivity saved; no runtime defaults or parameters changed.')

if __name__=='__main__':main()
