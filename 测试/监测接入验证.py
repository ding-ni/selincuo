"""隔离合成资料验证；所有批次保留，不删除、不修改已有文件。"""
import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from 后端.应用 import create_app
from 后端.监测资料 import Product, sha256, strict_json
from 工具.监测资料接入 import ingest

SPATIAL = ROOT / "数据/标准数据/空间标准化-20260916-02"


def fixture():
    start = datetime(2025, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    rows = []
    for index in range(62):
        quality = ["missing", "suspect", "rejected", "accepted", "unreviewed"][index % 5]
        rows.append({"time": (start + timedelta(hours=index + (index >= 30))).isoformat(),
                     "value": None if quality == "missing" else 4500 + index / 100,
                     "quality": quality, "quality_note": "合成测试标志，不是科学审核"})
    return {"schema_version": 1, "dataset_id": "test-lake", "version": "v1", "name": "隔离合成湖位",
            "object_id": "lake-selinco", "variable": "water_level", "unit": "m", "source_type": "合成演示",
            "channel": "ground", "source_description": "仅用于接口和界面测试，无实际观测含义",
            "source_version": "test-v1", "license": "仅限隔离测试", "vertical_datum": "合成测试基准",
            "sampling_interval_seconds": 3600, "records": list(reversed(rows))}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def prepare_batch():
    batch = ROOT / "成果/测试验证" / ("监测验证-" + uuid4().hex[:12])
    batch.mkdir(parents=True, exist_ok=False)
    source = batch / "合成源资料.json"
    write_json(source, fixture())
    ingest(source, SPATIAL, batch / "接入资料", allow_synthetic=True)
    return batch


class MonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = prepare_batch()
        cls.source = cls.batch / "合成源资料.json"
        cls.output = cls.batch / "接入资料"
        cls.client = TestClient(create_app(monitoring_dir=cls.output))
        print("验证批次：", cls.batch)

    def test_roundtrip_and_provenance(self):
        response = self.client.get("/api/v1/monitoring/products")
        self.assertEqual(response.status_code, 200)
        product = response.json()["items"][0]
        self.assertEqual(product["source_sha256"], sha256(self.source.read_bytes()))
        self.assertEqual(product["record_count"], 62)
        self.assertEqual(product["gap_count"], 1)
        self.assertEqual(product["gaps"][0]["seconds"], 7200)
        self.assertEqual(product["time_range"][0], "2025-01-01T00:00:00+00:00")
        self.assertEqual(product["quality_counts"]["missing"], 13)
        rows = self.client.get("/api/v1/observations", params={"product_id": "test-lake@v1", "limit": 50}).json()
        self.assertEqual(len(rows["items"]), 50)
        self.assertIsNone(rows["items"][0]["value"])
        remaining = self.client.get("/api/v1/observations", params={"product_id": "test-lake@v1", "offset": 50}).json()
        self.assertEqual(len(remaining["items"]), 12)
        self.assertEqual(rows["total"], remaining["total"])

    def test_filters_and_time_contract(self):
        for params in ({"object_id": "station-001"}, {"variable": "precipitation"}, {"channel": "uav"}):
            data = self.client.get("/api/v1/monitoring/products", params=params).json()
            self.assertEqual(data["total"], 0)
            self.assertEqual(data["registered_total"], 1)
            self.assertEqual(data["status"], "available")
        params = {"product_id": "test-lake@v1", "start": "2025-01-01T08:00:00+08:00", "end": "2025-01-01T08:00:00+08:00"}
        self.assertEqual(self.client.get("/api/v1/observations", params=params).json()["total"], 1)
        rows = self.client.get("/api/v1/observations", params={"product_id": "test-lake@v1", "quality": "missing"}).json()
        self.assertEqual(rows["total"], 13)
        self.assertTrue(all(r["value"] is None for r in rows["items"]))
        for change in ({"start": "2025-01-01T08:00:00"}, {"start": "2026-01-01T08:00:00Z"}, {"limit": 501}, {"offset": -1}, {"quality": "good"}):
            self.assertEqual(self.client.get("/api/v1/observations", params={**params, **change}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/observations", params={"product_id": "test-lake@v2"}).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/monitoring/products?object_id=unknown").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/monitoring/products?variable=unknown").status_code, 422)

    def test_invalid_contracts(self):
        for changes in ({"unit": "cm"}, {"vertical_datum": None}, {"source_type": "模型模拟"}, {"source_type": "气象预报"}, {"sampling_interval_seconds": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Product.model_validate({**fixture(), **changes})
        for changes in ({"time": "2025-01-01T00:00:00"}, {"time": 1234}, {"value": True}, {"value": "4500"}, {"value": float("nan")}, {"value": float("inf")}, {"value": None, "quality": "accepted"}, {"value": 0, "quality": "missing"}, {"quality": "accepted", "quality_note": ""}):
            value = fixture()
            value["records"] = [{**value["records"][0], **changes}]
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Product.model_validate(value)
        value = fixture()
        value["records"] = [value["records"][-1], {**value["records"][-1], "time": "2025-01-01T00:00:00Z"}]
        with self.assertRaises(ValueError):
            Product.model_validate(value)
        with self.assertRaises(ValueError):
            strict_json('{"value":1,"value":2}')
        value = {**fixture(), "variable": "precipitation", "unit": "mm", "object_id": "station-001"}
        with self.assertRaises(ValueError):
            Product.model_validate(value)
        value["accumulation_seconds"] = 3600
        value["records"] = [{"time": "2025-01-01T00:00:00Z", "value": -9999, "quality": "unreviewed"}]
        with self.assertRaises(ValueError):
            Product.model_validate(value)

    def test_no_overwrite_and_synthetic_isolation(self):
        before = {p: sha256(p.read_bytes()) for p in self.output.rglob("*.json")}
        with self.assertRaises(ValueError):
            ingest(self.source, SPATIAL, self.output, True)
        self.assertEqual(before, {p: sha256(p.read_bytes()) for p in before})
        with self.assertRaises(ValueError):
            ingest(self.source, SPATIAL, self.batch / "禁止隐式测试")
        with self.assertRaises(ValueError):
            ingest(self.source, SPATIAL, ROOT / "数据/标准数据/监测接入", True)
        self.assertFalse((self.batch / "禁止隐式测试").exists())
        for object_id, variable, unit in [("unknown", "water_level", "m"), ("lake-selinco", "discharge", "m3/s")]:
            source = self.batch / ("无效关联-" + uuid4().hex + ".json")
            write_json(source, {**fixture(), "object_id": object_id, "variable": variable, "unit": unit})
            with self.assertRaises(ValueError):
                ingest(source, SPATIAL, self.batch / "无效关联", True)
        self.assertFalse((self.batch / "无效关联").exists())

    def test_empty_incomplete_and_corrupt_store(self):
        missing = TestClient(create_app(monitoring_dir=self.batch / "不存在"))
        self.assertEqual(missing.get("/api/v1/monitoring/products").json()["status"], "not_connected")
        incomplete = self.batch / "未完成" / "批次"
        write_json(incomplete / "监测产品.json", fixture())
        self.assertEqual(TestClient(create_app(monitoring_dir=incomplete.parent)).get("/api/v1/monitoring/products").json()["total"], 0)
        for label, invalid_receipt in [("坏回执", []), ("哈希不符", {"standard_sha256": "0" * 64})]:
            folder = self.batch / label / "批次"
            write_json(folder / "监测产品.json", fixture())
            write_json(folder / "接入回执.json", invalid_receipt)
            client = TestClient(create_app(monitoring_dir=folder.parent))
            self.assertEqual(client.get("/api/v1/monitoring/products").status_code, 503)

    def test_version_separation(self):
        output = self.batch / "版本验证"
        ingest(self.source, SPATIAL, output, True)
        changed = copy.deepcopy(fixture())
        changed["version"] = "v2"
        changed["records"] = [changed["records"][0]]
        source = self.batch / "合成第二版.json"
        write_json(source, changed)
        ingest(source, SPATIAL, output, True)
        client = TestClient(create_app(monitoring_dir=output))
        self.assertEqual(client.get("/api/v1/monitoring/products").json()["total"], 2)
        self.assertEqual(client.get("/api/v1/observations?product_id=test-lake@v1").json()["total"], 62)
        self.assertEqual(client.get("/api/v1/observations?product_id=test-lake@v2").json()["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
