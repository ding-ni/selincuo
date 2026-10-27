"""预检后独占写入新版本，原件只读；失败批次保留。"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from 后端.监测资料 import Product, read_products, sha256, strict_json, validate_objects


def ingest(source, spatial_directory, output_directory, allow_synthetic=False):
    source = Path(source).resolve()
    if source.stat().st_size > 30 * 1024 * 1024:
        raise ValueError("单个接入文件不得超过30 MiB")
    content = source.read_bytes()
    product = Product.model_validate(strict_json(content))
    if product.source_type == "合成演示" and not allow_synthetic:
        raise ValueError("合成数据只能通过显式测试参数导入隔离测试目录")
    output = Path(output_directory).resolve()
    if product.source_type == "合成演示" and not output.is_relative_to(ROOT / "成果/测试验证"):
        raise ValueError("合成数据目标必须位于成果/测试验证，禁止进入正式目录")
    catalog = strict_json((Path(spatial_directory) / "空间目录.json").read_bytes())
    validate_objects(product, catalog["objects"])
    existing = read_products(output, catalog["objects"])
    if any(p.dataset_id == product.dataset_id and p.version == product.version for p, _ in existing):
        raise ValueError("数据集版本已存在；请使用独立新版本，不覆盖旧资料")
    # 确定目录名也充当同一版本并发接入锁；残留目录需要新版本，不删除重试。
    target = output / ("监测产品-" + product.dataset_id + "@" + product.version)
    target.mkdir(parents=True, exist_ok=False)
    standard = json.dumps(product.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    with (target / "监测产品.json").open("xb") as stream:
        stream.write(standard)
    if source.read_bytes() != content:
        raise ValueError("读取期间源文件发生变化，批次不注册")
    receipt = {"schema_version": 1, "source_file": source.name, "source_sha256": sha256(content),
               "standard_sha256": sha256(standard), "imported_at": datetime.now(timezone.utc).isoformat(),
               "object_catalog_version": catalog["version"], "batch_id": uuid4().hex,
               "record_count": len(product.records)}
    with (target / "接入回执.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--spatial-dir", type=Path, default=ROOT / "数据/标准数据/空间标准化-20260916-02")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "数据/标准数据/监测接入")
    parser.add_argument("--allow-synthetic-test", action="store_true")
    args = parser.parse_args()
    try:
        print(ingest(args.source, args.spatial_dir, args.output_dir, args.allow_synthetic_test))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, "接入失败：" + str(exc) + "\n")
