"""Sequential official CDS downloads, shared with the desktop acquisition task."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.acquisition import acquire

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--years', nargs='+', type=int, default=[2023, 2024, 2025])
    p.add_argument('--months', nargs='+', type=int, default=list(range(1, 13)))
    p.add_argument('--halo', action='store_true')
    p.add_argument('--direct', action='store_true')
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--output', type=Path, default=ROOT/'workspace/downloads/era5-land')
    p.add_argument('--workers', type=int, default=1)
    p.add_argument('--transfer-workers', type=int, default=2)
    p.add_argument('--group-months', type=int, default=1)
    a = p.parse_args()
    if a.output.name != 'era5-land' or a.output.parent.name != 'downloads':
        p.error('--output 必须为工作区/downloads/era5-land')
    try:
        result = acquire(a.output.parent.parent, a.years, a.months, a.halo, a.direct, a.prepare_only,
                         progress=lambda n,s: print(json.dumps({'progress':round(n,1),'message':s},ensure_ascii=False),flush=True))
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, KeyboardInterrupt) as exc:
        print(json.dumps({'status':'stopped','message':str(exc)},ensure_ascii=False))
        raise SystemExit(2)
if __name__ == '__main__':
    main()
