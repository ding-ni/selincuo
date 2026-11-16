"""合并 Copernicus GLO-90 瓦片，裁剪到色林错湖周范围，生成漫溢地形单文件。

输出：data/derived/spatial/湖周漫溢地形.tif
  - EPSG:4326，EGM2008 垂直基准（原瓦片基准，不转换）
  - 覆盖范围：88.0–90.0°E，31.0–32.5°N（含约 50 km 湖周缓冲）
  - 保留 GLO-90 原始分辨率（约 1 arcsecond / ~30 m）

用法：
    python scripts/构建湖周漫溢地形.py

依赖：rasterio >= 1.3，numpy。如使用 GDAL VRT 合并方式则需 osgeo.gdal 或 rasterio.merge。
"""
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / 'data/raw/spatial/公开连续地形-CopernicusGLO90-20261028'
OUT_PATH = ROOT / 'data/derived/spatial/湖周漫溢地形.tif'
# Clip bounds: lon_min, lat_min, lon_max, lat_max
CLIP_BOUNDS = (88.0, 31.0, 90.0, 32.5)


def find_tiles():
    tiles = sorted(SRC_DIR.glob('*/*_DEM.tif'))
    if not tiles:
        tiles = sorted(SRC_DIR.glob('*_DEM.tif'))
    return tiles


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build():
    if OUT_PATH.is_file():
        print(f'输出文件已存在，跳过：{OUT_PATH}')
        print('如需重新生成，请先手动删除该文件。')
        return

    tiles = find_tiles()
    if not tiles:
        print(f'错误：在 {SRC_DIR} 下未找到 *_DEM.tif 文件', file=sys.stderr)
        sys.exit(1)
    print(f'找到 {len(tiles)} 个 GLO-90 瓦片，开始合并并裁剪…')

    try:
        import rasterio
        from rasterio.merge import merge
        from rasterio.crs import CRS
    except ImportError:
        print('错误：需要 rasterio >= 1.3，请先安装：pip install rasterio', file=sys.stderr)
        sys.exit(1)

    # Open all tiles and select only those intersecting the clip bounds.
    lon_min, lat_min, lon_max, lat_max = CLIP_BOUNDS
    datasets = []
    skipped = 0
    for tile in tiles:
        try:
            ds = rasterio.open(tile)
            b = ds.bounds
            # Quick overlap check
            if b.right < lon_min or b.left > lon_max or b.top < lat_min or b.bottom > lat_max:
                ds.close()
                skipped += 1
                continue
            datasets.append(ds)
        except Exception as exc:
            print(f'  跳过 {tile.name}（读取错误：{exc}）', file=sys.stderr)

    if not datasets:
        print('错误：没有任何瓦片覆盖指定范围', file=sys.stderr)
        sys.exit(1)
    print(f'  覆盖目标范围的瓦片：{len(datasets)} 个，不相交已跳过：{skipped} 个')

    # Merge overlapping tiles within clip bounds.
    merged, transform = merge(
        datasets,
        bounds=(lon_min, lat_min, lon_max, lat_max),
        res=None,       # keep native resolution
        nodata=None,    # auto-detect from files
        method='first',
    )
    for ds in datasets:
        ds.close()

    crs = CRS.from_epsg(4326)
    profile = {
        'driver': 'GTiff',
        'dtype': merged.dtype,
        'width': merged.shape[2],
        'height': merged.shape[1],
        'count': merged.shape[0],
        'crs': crs,
        'transform': transform,
        'compress': 'lzw',
        'tiled': True,
        'blockxsize': 512,
        'blockysize': 512,
        'nodata': datasets[0].nodata if datasets else None,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUT_PATH.with_suffix('.tmp.tif')
    with rasterio.open(tmp_path, 'w', **profile) as dst:
        dst.write(merged)
        dst.update_tags(
            SOURCE='Copernicus DEM GLO-90',
            TILES=str(len(datasets)),
            CLIP_BOUNDS=f'{lon_min},{lat_min},{lon_max},{lat_max}',
            VERTICAL_DATUM='EGM2008（原瓦片基准，未转换）',
            RESOLUTION_ARCSEC='~1（约30m）',
            BUILT_AT=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        )
    tmp_path.rename(OUT_PATH)
    print(f'已写入：{OUT_PATH}')
    print(f'  大小：{OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB')
    print(f'  尺寸：{profile["width"]} × {profile["height"]} 像素')

    # Write a companion metadata JSON for traceability.
    meta = {
        'path': 'data/derived/spatial/湖周漫溢地形.tif',
        'source': 'Copernicus DEM GLO-90',
        'tileCount': len(datasets),
        'clipBounds': {'lonMin': lon_min, 'latMin': lat_min, 'lonMax': lon_max, 'latMax': lat_max},
        'verticalDatum': 'EGM2008（原瓦片基准，未转换）',
        'resolutionArcSec': 1,
        'crs': 'EPSG:4326',
        'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'sha256': sha256_file(OUT_PATH),
        'sizeBytes': OUT_PATH.stat().st_size,
    }
    meta_path = OUT_PATH.with_suffix('.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'  元数据：{meta_path}')
    print('完成。漫溢模块将自动使用该文件。')


if __name__ == '__main__':
    build()
