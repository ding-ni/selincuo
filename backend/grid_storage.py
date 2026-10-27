"""Bounded float64 Zarr v2 artifacts with legacy NPZ export."""
import zipfile
from pathlib import Path
from threading import Lock
import numpy as np
from .common import read_json,write_json,sha_file,digest

FORMAT='zarr-v2-grid-hourly-1'
_export_guard=Lock()
_export_locks={}

class Writer:
    def __init__(self,folder,hours,units,partitions):
        import zarr
        from numcodecs import Blosc
        self.path=folder/'hourly.zarr';self.root=zarr.open_group(str(self.path),mode='w')
        self.root.attrs.update(format=FORMAT,complete=False)
        compressor=Blosc(cname='zstd',clevel=3,shuffle=Blosc.BITSHUFFLE)
        shapes={'time':(hours,),'flow':(hours,7),'lakeLevel':(hours,),'volumeChange':(hours,),'continuousLakeVolumeChange':(hours,),'lakeFlux':(hours,),
                'unitStates':(hours,units,8),'unitDiagnostics':(hours,units,9),'routingStorage':(hours,partitions),'sourceRoutingStorage':(hours,partitions,2),'excludedRunoff':(hours,)}
        for key,shape in shapes.items():
            chunks=(min(168,max(1,hours)),)+tuple(min(64,n) if i==0 else n for i,n in enumerate(shape[1:]))
            self.root.create_dataset(key,shape=shape,chunks=chunks,dtype='i8' if key=='time' else 'f8',compressor=compressor,fill_value=0 if key=='time' else np.nan)
    def put(self,a,b,values):
        for key,value in values.items():self.root[key][a:b]=value
    def finish(self):
        self.root.attrs['complete']=True
        files={p.relative_to(self.path).as_posix():sha_file(p) for p in sorted(self.path.rglob('*')) if p.is_file()}
        value={'format':FORMAT,'files':files,'sha256':digest(files),'complete':True}
        write_json(self.path.parent/'hourly-manifest.json',value)
        return value

def open_arrays(folder):
    import zarr
    root=zarr.open_group(str(folder/'hourly.zarr'),mode='r')
    if not root.attrs.get('complete'):raise ValueError('逐时分块成果未完整写入')
    return root

def export_npz(folder, destination=None, progress=None, cancelled=None):
    folder=Path(folder).resolve()
    destination=Path(destination).resolve() if destination else folder
    with _export_guard:lock=_export_locks.setdefault(str(destination),Lock())
    with lock:return _export_npz(folder,destination,progress,cancelled)


def _export_npz(folder,destination=None,progress=None,cancelled=None):
    destination=destination or folder
    destination.mkdir(parents=True,exist_ok=True)
    target=destination/'hourly.npz'
    if target.is_file():return target
    arrays=open_arrays(folder);temporary=destination/'hourly-export.partial'
    keys=sorted(arrays.array_keys())
    total=sum(int(np.prod(arrays[key].shape))*arrays[key].dtype.itemsize for key in keys)
    completed=0
    with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as archive:
        for key in keys:
            if cancelled and cancelled():raise InterruptedError('用户停止导出')
            a=arrays[key]
            with archive.open(key+'.npy','w',force_zip64=True) as stream:
                np.lib.format.write_array_header_1_0(stream,{'descr':np.lib.format.dtype_to_descr(a.dtype),'fortran_order':False,'shape':a.shape})
                for offset in range(0,a.shape[0],168):
                    if cancelled and cancelled():raise InterruptedError('用户停止导出')
                    block=np.ascontiguousarray(a[offset:offset+168]).tobytes()
                    stream.write(block);completed+=len(block)
                    if progress:progress(completed/max(1,total)*90,f'压缩逐时变量 {key} · {completed}/{total} 字节')
    if cancelled and cancelled():raise InterruptedError('用户停止导出')
    temporary.replace(target)
    return target
