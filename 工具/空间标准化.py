"""只读原件，独占新建标准批次；不执行清理或覆盖。"""
import argparse
import csv
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pyproj
import shapefile
import shapely
import xlrd
from pyproj import CRS, Geod, Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, Point, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity

ROOT = Path(__file__).resolve().parents[1]
SPECS = [
    ("D01", "basin-main", "色林错流域候选范围", "basin", "流域总边界/basin1.shp"),
    ("D02-ZJ", "basin-zajia", "扎加藏布", "subbasin", "入湖子流域/扎加藏布边界.shp"),
    ("D02-ZG", "basin-zagen", "扎根藏布", "subbasin", "入湖子流域/扎根藏布边界.shp"),
    ("D02-BQ", "basin-boqu", "波曲藏布", "subbasin", "入湖子流域/波曲藏布边界.shp"),
    ("D02-AL", "basin-ali", "阿里藏布", "subbasin", "入湖子流域/阿里藏布边界.shp"),
    ("D03", "lake-selinco", "色林错", "lake", "湖泊边界/湖泊边界.shp"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def polygons(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for child in getattr(geometry, "geoms", []) for p in polygons(child)]


def area(geometry):
    # 每个面统一环方向后计算椭球面积；无效输入仅用作修复差异诊断。
    geod = Geod(ellps="WGS84")
    return sum(abs(geod.geometry_area_perimeter(orient(p, sign=1))[0]) for p in polygons(geometry)) / 1e6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    if not args.batch or any(c not in "0123456789-批次空间标准化" for c in args.batch):
        parser.error("批次仅允许中文前缀、数字和连字符")
    target = ROOT / "数据/标准数据" / args.batch
    target.mkdir(exist_ok=False)
    with (ROOT / "文档/核查记录/复制校验清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        baseline = {r["目标"].replace("\\", "/"): r["目标SHA256"] for r in csv.DictReader(stream)}
    catalog = {"schema_version": 1, "version": args.batch, "created_at": datetime.now(timezone.utc).isoformat(),
               "objects": [], "datasets": []}
    report = {"code_version": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "environment": {"python": platform.python_version(), "shapely": shapely.__version__, "pyproj": pyproj.__version__},
              "inputs": [], "geometry_checks": [], "topology_checks": [],
              "limitations": ["来源年代、边界含义、正式河湖拓扑及许可待核实", "几何有效不等于科学质量通过", "无效面修复前面积仅作诊断"]}
    geometries = {}

    def sources(path):
        files = sorted(path.parent.glob(path.stem + ".*")) if path.suffix == ".shp" else [path]
        result = []
        for file in files:
            relative = file.relative_to(ROOT).as_posix()
            sha = digest(file)
            if baseline.get(relative) != sha:
                raise ValueError("原件与复制基线不符：" + relative)
            report["inputs"].append({"path": relative, "sha256": sha})
            result.append({"name": file.name, "sha256": sha})
        return result

    def register(dataset_id, features, source_files, checks, source_crs):
        filename = dataset_id + "-空间图层.geojson"
        write_new(target / filename, {"type": "FeatureCollection", "features": features})
        catalog["datasets"].append({
            "dataset_id": dataset_id, "name": features[0]["properties"]["name"] if len(features) == 1 else "降水站点位置目录",
            "version": args.batch, "source_type": "待核实", "source_description": "旧项目空间资料，原始制图来源待核实",
            "source_files": source_files, "source_crs": source_crs, "crs": "EPSG:4326", "coordinate_unit": "degree",
            "vertical_datum": None, "time_range": None, "time_basis": None, "sampling_interval": None,
            "missing_value": None, "quality_status": "仅空间展示", "license_status": "待核实；仅限本机内部原型",
            "limitations": ["年代与来源待核实", "未接入观测时序", "不可直接用于正式模型或业务预警"],
            "geometry_checks": checks, "object_ids": [f["properties"]["object_id"] for f in features],
            "layer_file": filename, "sha256": digest(target / filename)})

    for dataset_id, object_id, name, kind, relative in SPECS:
        path = ROOT / "数据/原始资料" / relative
        source_files = sources(path)
        crs = CRS.from_wkt(path.with_suffix(".prj").read_text())
        converter = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        with shapefile.Reader(str(path), encoding="utf-8") as reader:
            originals = [transform(converter.transform, shape(s.__geo_interface__)) for s in reader.shapes()]
        fixed = []
        checks = []
        for index, original in enumerate(originals):
            repaired = make_valid(original) if not original.is_valid else original
            parts = polygons(repaired)
            if not parts:
                raise ValueError("边界未产生面几何")
            polygonal = unary_union(parts)
            if not polygonal.is_valid or polygonal.is_empty:
                raise ValueError("几何修复失败")
            fixed.append(polygonal)
            checks.append({"feature_index": index, "valid_before": original.is_valid,
                           "reason_before": explain_validity(original), "type_before": original.geom_type,
                           "type_after_make_valid": repaired.geom_type, "type_after": polygonal.geom_type,
                           "parts_before": len(polygons(original)), "parts_after": len(polygons(polygonal)),
                           "holes_before": sum(len(p.interiors) for p in polygons(original)),
                           "holes_after": sum(len(p.interiors) for p in polygons(polygonal)),
                           "discarded_non_polygon": repaired.geom_type == "GeometryCollection",
                           "area_before_km2_diagnostic": area(original), "area_after_km2": area(polygonal),
                           "area_change_km2": area(polygonal) - area(original), "valid_after": polygonal.is_valid})
        geometry = unary_union(fixed)
        geometries[object_id] = geometry
        obj = {"object_id": object_id, "name": name, "type": kind, "bbox": list(geometry.bounds),
               "dataset_ids": [dataset_id], "area_km2": area(geometry), "parent_id": None,
               "association_status": "流域归属及连通性待核实", "quality_status": "仅空间展示"}
        catalog["objects"].append(obj)
        feature = {"type": "Feature", "id": object_id, "properties": {k: obj[k] for k in ["object_id", "name", "type"]}, "geometry": mapping(geometry)}
        register(dataset_id, [feature], source_files, checks, crs.to_string())
        report["geometry_checks"].append({"dataset_id": dataset_id, "features": checks, "output_valid": geometry.is_valid})

    path = ROOT / "数据/原始资料/站点位置/降水_站点位置.xls"
    source_files = sources(path)
    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    features = []
    for row in range(1, sheet.nrows):
        name, lon, lat, association = sheet.row_values(row)
        if not name or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("站点位置行无效")
        object_id = f"station-{row:03d}"
        obj = {"object_id": object_id, "name": name, "type": "station", "bbox": [lon, lat, lon, lat],
               "dataset_ids": ["D06"], "parent_id": None, "source_association": association,
               "association_status": "原表归属待核实；坐标基准未声明，展示暂按经纬度WGS84解释", "quality_status": "仅位置目录"}
        catalog["objects"].append(obj)
        features.append({"type": "Feature", "id": object_id, "properties": {k: obj[k] for k in ["object_id", "name", "type"]}, "geometry": mapping(Point(lon, lat))})
    register("D06", features, source_files, [], "原表未声明；暂按WGS84经纬度解释，待核实")
    catalog["datasets"][-1]["limitations"].append("站点坐标基准未声明；不是正式遥感水文站或在线状态证明")
    main_basin = geometries["basin-main"]
    subbasins = [(key, geom) for key, geom in geometries.items() if key not in ["basin-main", "lake-selinco"]]
    for i, (key, geom) in enumerate(subbasins):
        report["topology_checks"].append({"object_id": key, "outside_candidate_basin_km2": area(geom.difference(main_basin))})
        for other, geometry in subbasins[i + 1:]:
            report["topology_checks"].append({"pair": [key, other], "overlap_km2": area(geom.intersection(geometry))})
    report["uncovered_candidate_basin_km2"] = area(main_basin.difference(unary_union([g for _, g in subbasins] + [geometries["lake-selinco"]])))
    for item in report["inputs"]:
        if digest(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("处理后原件哈希变化")
    report["original_hashes_unchanged"] = True
    write_new(target / "质量报告.json", report)
    write_new(target / "空间目录.json", catalog)
    print(json.dumps({"batch": args.batch, "objects": len(catalog["objects"]), "datasets": len(catalog["datasets"]), "source_files": len(report["inputs"])}, ensure_ascii=True))


if __name__ == "__main__":
    main()
