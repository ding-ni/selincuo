import os
from PyInstaller.utils.hooks import collect_submodules, copy_metadata, collect_data_files
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
hidden = ['h5py','h5netcdf','xarray','openpyxl','xlrd','numba','llvmlite','geopandas','rasterio','scipy.ndimage','pyogrio','pyproj','shapely','fiona']
hidden += collect_submodules('shapely', filter=lambda n: '.tests' not in n)
hidden += collect_submodules('rasterio', filter=lambda n: '.tests' not in n and not n.startswith('rasterio.rio'))
hidden += collect_submodules('fiona', filter=lambda n: '.tests' not in n)
datas = copy_metadata('h5netcdf') + copy_metadata('h5py')
datas += collect_data_files('pyproj', includes=['proj_dir/**'])
datas += collect_data_files('rasterio', includes=['gdal_data/**', 'proj_data/**'])
datas += collect_data_files('fiona', includes=['gdal_data/**', 'proj_data/**'])
a = Analysis([os.path.join(ROOT, 'app_entry.py')], pathex=[ROOT], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=['torch','tensorflow','IPython','pytest','matplotlib','PyQt5','PyQt6','PySide6','tkinter','cfgrib','netCDF4'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SelincoPlatform', console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='SelincoPlatform', upx=False)
