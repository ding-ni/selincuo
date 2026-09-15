"""验证真实资料合同和隔离的失败目录；所有测试输出保留。"""
import csv
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("service", ROOT / "后端/应用.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class SpatialContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(service.app)
        cls.evidence = ROOT / "成果/测试验证" / ("接口验证-" + uuid4().hex[:12])
        cls.evidence.mkdir(parents=True, exist_ok=False)

    def test_original_hashes(self):
        with (ROOT / "文档/核查记录/复制校验清单.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 134)
        for row in rows:
            self.assertEqual(hashlib.sha256((ROOT / row["目标"]).read_bytes()).hexdigest(), row["目标SHA256"])

    def test_objects_layers_and_sources(self):
        objects = self.client.get("/api/v1/objects").json()
        datasets = self.client.get("/api/v1/datasets").json()
        self.assertEqual(objects["total"], 10)
        self.assertEqual(datasets["total"], 7)
        ids = {o["object_id"] for o in objects["items"]}
        feature_ids = set()
        for ds in datasets["items"]:
            self.assertEqual(ds["source_type"], "待核实")
            self.assertIsNone(ds["time_range"])
            self.assertNotIn("layer_file", ds)
            self.assertTrue(all(len(f["sha256"]) == 64 for f in ds["source_files"]))
            response = self.client.get("/api/v1/layers/" + ds["dataset_id"])
            self.assertEqual(response.status_code, 200)
            layer = response.json()
            self.assertEqual({f["properties"]["object_id"] for f in layer["features"]}, set(ds["object_ids"]))
            for feature in layer["features"]:
                geometry = shape(feature["geometry"])
                self.assertTrue(geometry.is_valid)
                self.assertFalse(geometry.is_empty)
                self.assertTrue(87 < geometry.bounds[0] < 93)
                self.assertTrue(29 < geometry.bounds[1] < 34)
                feature_ids.add(feature["properties"]["object_id"])
        self.assertEqual(ids, feature_ids)
        for obj in objects["items"]:
            self.assertEqual(self.client.get("/api/v1/objects/" + obj["object_id"]).json(), obj)
            related = self.client.get("/api/v1/datasets", params={"object_id": obj["object_id"]}).json()["items"]
            self.assertEqual({d["dataset_id"] for d in related}, set(obj["dataset_ids"]))

    def test_filter_and_empty(self):
        self.assertEqual(self.client.get("/api/v1/objects?type=station").json()["total"], 4)
        self.assertEqual(self.client.get("/api/v1/objects", params={"q": "115"}).json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/objects", params={"q": "不存在"}).json(), {"items": [], "total": 0})
        self.assertEqual(self.client.get("/api/v1/objects?type=invalid").status_code, 422)

    def test_unknown_and_private_files(self):
        for url in ["/api/v1/objects/unknown", "/api/v1/layers/unknown", "/api/v1/datasets?object_id=unknown", "/数据/原始资料/湖泊边界/湖泊边界.shp", "/AGENTS.md", "/static/../后端/应用.py"]:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_missing_catalog(self):
        client = TestClient(service.create_app(self.evidence / "不存在目录"))
        self.assertEqual(client.get("/api/v1/objects").status_code, 503)
        self.assertEqual(client.get("/api/v1/health").status_code, 503)

    def test_corrupt_catalog(self):
        folder = self.evidence / "损坏目录"
        folder.mkdir()
        with (folder / "空间目录.json").open("x", encoding="utf-8") as stream:
            stream.write('{"schema_version":1,"objects":[{}],"datasets":[]}')
        client = TestClient(service.create_app(folder))
        self.assertEqual(client.get("/api/v1/objects").status_code, 503)
        self.assertEqual(client.get("/api/v1/health").status_code, 503)

    def test_tampered_layer(self):
        folder = self.evidence / "篡改图层"
        folder.mkdir()
        source = json.loads((service.DEFAULT_DATA / "空间目录.json").read_text(encoding="utf-8"))
        ds = source["datasets"][0]
        with (folder / "空间目录.json").open("x", encoding="utf-8") as stream:
            json.dump(source, stream)
        with (folder / ds["layer_file"]).open("x", encoding="utf-8") as stream:
            stream.write('{}')
        self.assertEqual(TestClient(service.create_app(folder)).get("/api/v1/layers/" + ds["dataset_id"]).status_code, 503)

    def test_geometry_report(self):
        report = json.loads((service.DEFAULT_DATA / "质量报告.json").read_text(encoding="utf-8"))
        self.assertTrue(report["original_hashes_unchanged"])
        self.assertEqual(len(report["inputs"]), 49)
        checks = [f for d in report["geometry_checks"] for f in d["features"]]
        self.assertEqual(sum(not f["valid_before"] for f in checks), 4)
        self.assertTrue(all(f["valid_after"] for f in checks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
