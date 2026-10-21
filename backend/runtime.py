"""Pin the matching HDF5 DLLs before xarray/GDAL plugin imports on Windows."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_HANDLES = []
_DLLS = []
_READY = False


def prepare_runtime():
    global _READY
    if _READY:
        return
    if os.name == 'nt':
        import ctypes
        spec = importlib.util.find_spec('h5py')
        if spec and spec.origin:
            folder = Path(spec.origin).parent
            hdf5 = folder / 'hdf5.dll'
            high = folder / 'hdf5_hl.dll'
            if hdf5.is_file() and high.is_file():
                _HANDLES.append(os.add_dll_directory(str(folder)))
                _DLLS.extend([ctypes.WinDLL(str(hdf5), winmode=0x1100), ctypes.WinDLL(str(high), winmode=0x1100)])
    import h5py
    expected = tuple(h5py.version.hdf5_built_version_tuple)
    actual = tuple(h5py.h5.get_libversion())
    if expected != actual:
        raise RuntimeError(f'HDF5版本不匹配：构建{expected}，运行{actual}')
    proj_spec = importlib.util.find_spec('pyproj')
    if proj_spec and proj_spec.origin:
        proj_data = Path(proj_spec.origin).parent / 'proj_dir/share/proj'
        if (proj_data / 'proj.db').is_file():
            os.environ['PROJ_DATA'] = str(proj_data)
            os.environ['PROJ_LIB'] = str(proj_data)
    raster_spec = importlib.util.find_spec('rasterio')
    if raster_spec and raster_spec.origin:
        gdal_data = Path(raster_spec.origin).parent / 'gdal_data'
        if gdal_data.is_dir():
            os.environ['GDAL_DATA'] = str(gdal_data)
    _READY = True
