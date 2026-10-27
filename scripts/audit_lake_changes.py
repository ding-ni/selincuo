"""Historical lake-change skill, explicitly not a forecast verification.

Use exact-date observation pairs inside one lake initialization window. The
constant-level reference predicts zero change. Perfect historical forcing and
window-conditioned simulations do not establish future forecast performance.
"""
import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.common import read_json, write_json, digest, now


def changes(result, days, year):
    rows = {r['date'][:10]: r for r in result['daily']}
    windows = result['diagnostics']['lakeWindows']
    pairs = []
    for date, last in rows.items():
        end = pd.Timestamp(date)
        if end.year != year:
            continue
        start = end - pd.Timedelta(days=days)
        first = rows.get(start.date().isoformat())
        if not first or any(r.get(k) is None for r in (first,last) for k in ('lakeLevel','observedLevel')):
            continue
        if not any(start > pd.Timestamp(w['start']).normalize() and end <= pd.Timestamp(w['end']).normalize() for w in windows):
            continue
        observed = last['observedLevel'] - first['observedLevel']
        simulated = last['lakeLevel'] - first['lakeLevel']
        pairs.append({'start':str(start.date()),'end':date,'observedChangeM':observed,'simulatedChangeM':simulated,
                      'errorM':simulated-observed})
    if not pairs:
        return {'year':year,'days':days,'count':0,'pairs':[]}
    observed = np.array([p['observedChangeM'] for p in pairs])
    simulated = np.array([p['simulatedChangeM'] for p in pairs])
    error = simulated-observed
    baseline = float(np.mean(observed**2))
    signal = np.abs(observed) >= .01
    return {'year':year,'days':days,'count':len(pairs),'rmseM':float(np.sqrt(np.mean(error**2))),
            'maeM':float(np.mean(np.abs(error))),'biasM':float(error.mean()),
            'constantLevelRmseM':float(np.sqrt(baseline)),
            'skillVsConstantLevel':float(1-np.mean(error**2)/baseline) if baseline > 1e-12 else None,
            'directionSamplesAbove1cm':int(signal.sum()),
            'directionAccuracyAbove1cm':float(np.mean(np.sign(simulated[signal])==np.sign(observed[signal]))) if signal.any() else None,
            'pairs':pairs}


def audit(result):
    years=sorted({int(r['date'][:4]) for r in result['daily'] if int(r['date'][:4]) >= 2024})
    monthly=[]
    for month in sorted({r['date'][:7] for r in result['daily']}):
        matched=[r for r in result['daily'] if r['date'].startswith(month) and r.get('observedLevel') is not None and r.get('lakeLevel') is not None]
        if not matched:
            continue
        error=np.array([r['lakeLevel']-r['observedLevel'] for r in matched])
        monthly.append({'month':month,'count':len(matched),'biasM':float(error.mean()),
                        'rmseM':float(np.sqrt(np.mean(error**2)))})
    return {'at':now(),'kind':'historical-conditional-lake-change-audit','futureForecastSkillClaimed':False,
            'resultIdentity':result['identity'],'resultDigest':digest(result),
            'monthlyLevelError':monthly,
            'changeSkill':[changes(result,days,year) for year in years for days in (1,7,15)],
            'definitions':['Historical observed daily-mean lake level pairs; not instantaneous levels.',
                           'Pairs require both exact dates and the same observation-anchored simulation window.',
                           'Initialization day is excluded; gaps are not interpolated.',
                           'The baseline is zero water-level change over the same period.',
                           'Historical meteorological forcing is known; these scores are not 7-15 day forecast validation.']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--result',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=audit(read_json(args.result))
    write_json(args.output,report)
    print([{k:v for k,v in row.items() if k!='pairs'} for row in report['changeSkill']])


if __name__=='__main__':
    main()
