# -*- coding: utf-8 -*-
"""Stream and verify every value of the full latest grid result without loading it all."""
import json
import sys
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import ROOT, read_json, sha_file, write_json
from backend.grid_storage import export_npz, open_arrays
import numpy as np
import psutil

out = ROOT / 'build/grid-revision-20261026'
identifier = read_json(out / 'extension-evidence.json')['fullResultTaskId']
folder = out / 'workspace/tasks' / identifier
arrays = open_arrays(folder)
done = threading.Event()
peak = [psutil.Process().memory_info().rss]


def sample_memory():
    while not done.wait(.2):
        peak[0] = max(peak[0], psutil.Process().memory_info().rss)


threading.Thread(target=sample_memory, daemon=True).start()
started = time.monotonic()
try:
    exported = export_npz(folder)
    export_seconds = time.monotonic() - started
    checks = []
    verify_start = time.monotonic()
    with zipfile.ZipFile(exported) as archive:
        assert set(archive.namelist()) == {key + '.npy' for key in arrays.array_keys()}
        for key in arrays.array_keys():
            data = arrays[key]
            with archive.open(key + '.npy') as stream:
                assert np.lib.format.read_magic(stream) == (1, 0)
                shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
                assert shape == data.shape and dtype == data.dtype and not fortran
                trailing = int(np.prod(shape[1:], dtype=np.int64))
                for offset in range(0, shape[0], 168):
                    count = min(168, shape[0] - offset)
                    raw = stream.read(count * trailing * dtype.itemsize)
                    actual = np.frombuffer(raw, dtype=dtype).reshape((count,) + shape[1:])
                    np.testing.assert_array_equal(actual, data[offset:offset + count])
                assert not stream.read(1)
            checks.append({'variable': key, 'shape': shape, 'dtype': str(dtype), 'exactlyEqual': True})
            print('verified ' + key, flush=True)
    evidence = {
        'taskId': identifier, 'hours': arrays['time'].shape[0],
        'units': arrays['unitStates'].shape[1], 'allValuesExactlyEqual': True,
        'exportSeconds': export_seconds, 'verifySeconds': time.monotonic() - verify_start,
        'peakSampledRssMiB': peak[0] / 1024 ** 2,
        'exportBytes': exported.stat().st_size, 'exportSHA256': sha_file(exported),
        'zarrBytes': sum(p.stat().st_size for p in (folder / 'hourly.zarr').rglob('*') if p.is_file()),
        'variables': checks,
    }
    write_json(out / 'full-export-evidence.json', evidence)
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
finally:
    done.set()
