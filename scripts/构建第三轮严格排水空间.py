"""Derive only raw-D8 known receivers, leaving every pit/water unresolved.

The earlier 2 m candidate generator remains byte-for-byte unchanged. A
negative selection threshold intentionally excludes even zero-depth pits.
"""
import json
from pathlib import Path

from backend.common import ROOT, sha_file
from scripts import 构建第三轮连续地形空间 as candidate


def main():
    candidate.THRESHOLD_M = -1.
    candidate.main()
    # Each fine manifest already records the generator digest and threshold.
    # A sidecar records this wrapper identity without modifying the manifest.
    root = ROOT / 'data/derived/spatial/versions'
    matches = []
    for path in root.glob('GRID-SPATIAL-*/manifest.json'):
        try:
            manifest = json.loads(path.read_text(encoding='utf-8-sig'))
            derivation = manifest.get('continuousTerrainDerivation', {})
            if (manifest.get('parentSpatialVersion') == candidate.PARENT and
                    derivation.get('pitThresholdM') == -1. and
                    derivation.get('generatorSHA256') == sha_file(Path(candidate.__file__))):
                matches.append((path, manifest))
        except (OSError, ValueError, KeyError):
            continue
    if len(matches) != 1:
        raise ValueError('严格候选空间身份不唯一，请检查版本目录')
    path, manifest = matches[0]
    receipt = {'spatialVersion': manifest['version'], 'generatorSHA256': sha_file(Path(candidate.__file__)),
               'wrapperSHA256': sha_file(Path(__file__)), 'pitThresholdM': -1.,
               'meaning': 'Only raw strictly downhill/flat-resolved known receivers; no pit is accepted.',
               'currentPointerModified': False}
    target = path.parent / 'strict-terrain-wrapper-receipt.json'
    candidate.write_new(target, candidate.data_bytes(receipt, indent=2))
    print(json.dumps(receipt, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
