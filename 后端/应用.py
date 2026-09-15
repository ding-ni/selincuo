"""本机只读GIS目录服务；仅公开白名单字段及前端资源。"""
import hashlib
import json
import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "数据/标准数据/空间标准化-20260916-02"


def create_app(data_dir=None):
    directory = Path(data_dir or os.environ.get("SELINCO_DATA_DIR", DEFAULT_DATA)).resolve()
    app = FastAPI(title="色林错空间资料服务", version="0.1.0")

    def catalog():
        try:
            value = json.loads((directory / "空间目录.json").read_text(encoding="utf-8"))
            if value["schema_version"] != 1 or not isinstance(value["version"], str) or not value["version"] or not isinstance(value["objects"], list) or not isinstance(value["datasets"], list):
                raise ValueError("目录格式不支持")
            # 目录完整性先于提供任何业务响应；损坏目录不得显示成空资料。
            for obj in value["objects"]:
                for field in ("object_id", "name", "type", "bbox", "dataset_ids"):
                    obj[field]
            for ds in value["datasets"]:
                for field in ("dataset_id", "object_ids", "layer_file", "sha256"):
                    ds[field]
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "标准空间目录未就绪或已损坏，请核查标准批次")

    @app.get("/api/v1/health")
    def health():
        value = catalog()
        return {"status": "ok", "mode": "本机只读原型", "data_version": value["version"]}

    @app.get("/api/v1/objects")
    def objects(type: Optional[Literal["basin", "subbasin", "lake", "station"]] = None,
                q: str = Query(default="", max_length=100)):
        items = [o for o in catalog()["objects"] if (type is None or o["type"] == type) and q.casefold() in o["name"].casefold()]
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/objects/{object_id}")
    def object_detail(object_id: str):
        item = next((o for o in catalog()["objects"] if o["object_id"] == object_id), None)
        if item is None:
            raise HTTPException(404, "空间对象不存在")
        return item

    @app.get("/api/v1/datasets")
    def datasets(object_id: Optional[str] = None):
        value = catalog()
        if object_id is not None and not any(o["object_id"] == object_id for o in value["objects"]):
            raise HTTPException(404, "空间对象不存在")
        items = [{k: v for k, v in d.items() if k != "layer_file"} for d in value["datasets"]
                 if object_id is None or object_id in d["object_ids"]]
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/layers/{dataset_id}")
    def layer(dataset_id: str):
        ds = next((d for d in catalog()["datasets"] if d["dataset_id"] == dataset_id), None)
        if ds is None:
            raise HTTPException(404, "空间图层不存在")
        path = (directory / ds["layer_file"]).resolve()
        if path.parent != directory or path.suffix != ".geojson":
            raise HTTPException(503, "图层注册路径不合法")
        try:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != ds["sha256"]:
                raise ValueError("图层哈希不符")
            value = json.loads(content)
            if value["type"] != "FeatureCollection" or not isinstance(value["features"], list):
                raise ValueError("图层结构不符")
            return value
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "标准图层缺失或校验失败")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(ROOT / "前端/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "前端/页面"), name="frontend")
    app.mount("/vendor", StaticFiles(directory=ROOT / "前端/依赖", check_dir=False), name="vendor")
    return app


app = create_app()
