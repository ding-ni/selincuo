"""监测时序接入合同与只读存储；不插值、不猜测单位、不自动提升质量。"""
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

VARIABLES = {
    "water_level": {"name": "水位", "unit": "m", "types": ["lake", "station"], "datum_required": True},
    "discharge": {"name": "流量", "unit": "m3/s", "types": ["station"], "datum_required": False},
    "precipitation": {"name": "累计降水", "unit": "mm", "types": ["station", "subbasin", "basin"], "datum_required": False},
    "air_temperature": {"name": "气温", "unit": "degC", "types": ["station", "subbasin", "basin"], "datum_required": False},
    "water_area": {"name": "水域面积", "unit": "km2", "types": ["lake"], "datum_required": False},
}
SourceType = Literal["原始观测", "遥感反演", "再分析", "气象预报", "模型模拟", "合成演示", "待核实"]
Channel = Literal["ground", "satellite", "uav", "other"]
Quality = Literal["unreviewed", "accepted", "suspect", "missing", "rejected"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Record(Contract):
    time: AwareDatetime
    value: Optional[FiniteFloat] = None
    quality: Quality
    quality_note: str = Field(default="", max_length=500)

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("监测值必须为JSON数值或null")
        return value

    @field_validator("time", mode="before")
    @classmethod
    def explicit_time(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("时间必须为带时区的ISO 8601字符串")
        return value

    @model_validator(mode="after")
    def check_missing(self):
        if (self.value is None) != (self.quality == "missing"):
            raise ValueError("缺测必须同时使用null值和missing标志，不能用0或缺测码代替")
        if self.quality in ("accepted", "suspect", "rejected") and not self.quality_note:
            raise ValueError("已审核或异常质量标志必须附审核/判定依据")
        self.time = self.time.astimezone(timezone.utc)
        return self


class Product(Contract):
    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    object_id: str = Field(min_length=1, max_length=100)
    variable: Literal["water_level", "discharge", "precipitation", "air_temperature", "water_area"]
    unit: str
    source_type: SourceType
    channel: Channel
    source_description: str = Field(min_length=1, max_length=2000)
    source_version: str = Field(min_length=1, max_length=160)
    license: str = Field(min_length=1, max_length=500)
    vertical_datum: Optional[str] = Field(default=None, min_length=1, max_length=160)
    sampling_interval_seconds: Optional[int] = Field(default=None, gt=0, strict=True)
    accumulation_seconds: Optional[int] = Field(default=None, gt=0, strict=True)
    records: List[Record] = Field(min_length=1, max_length=100000)

    @model_validator(mode="after")
    def check_contract(self):
        if self.unit != VARIABLES[self.variable]["unit"]:
            raise ValueError("单位不符合变量合同，须先显式转换并记录来源")
        if self.variable == "water_level" and not self.vertical_datum:
            raise ValueError("水位必须声明垂直基准，不能混用椭球高和水准高")
        if self.variable == "precipitation" and not self.accumulation_seconds:
            raise ValueError("累计降水必须声明累计秒数，记录时间为累计结束时刻")
        if self.variable != "precipitation" and self.accumulation_seconds is not None:
            raise ValueError("非降水变量不能设置累计时长")
        if self.source_type in ("气象预报", "模型模拟"):
            raise ValueError("预报及模型结果需独立起报/运行合同，不可作为监测时序导入")
        times = [r.time for r in self.records]
        if len(set(times)) != len(times):
            raise ValueError("同一对象变量版本存在重复时刻（已按UTC比较）")
        if self.variable in ("precipitation", "water_area") and any(r.value is not None and r.value < 0 for r in self.records):
            raise ValueError("降水和水域面积不能为负值；请先处理原始缺测码")
        self.records.sort(key=lambda row: row.time)
        return self


def validate_objects(product, objects):
    obj = next((o for o in objects if o["object_id"] == product.object_id), None)
    if obj is None:
        raise ValueError("对象ID未注册")
    if obj["type"] not in VARIABLES[product.variable]["types"]:
        raise ValueError("变量与对象类型不兼容")


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def strict_json(content):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("JSON包含重复字段：" + key)
            value[key] = item
        return value
    return json.loads(content, object_pairs_hook=pairs)


def product_summary(product, receipt):
    rows = product.records
    gaps = []
    if product.sampling_interval_seconds:
        for left, right in zip(rows, rows[1:]):
            seconds = (right.time - left.time).total_seconds()
            if seconds > product.sampling_interval_seconds:
                gaps.append({"start": left.time.isoformat(), "end": right.time.isoformat(), "seconds": seconds})
    result = product.model_dump(mode="json", exclude={"records"})
    result.update({"product_id": product.dataset_id + "@" + product.version,
                   "record_count": len(rows), "time_range": [rows[0].time.isoformat(), rows[-1].time.isoformat()],
                   "quality_counts": dict(Counter(r.quality for r in rows)), "gap_count": len(gaps),
                   "gaps": gaps[:50], "gap_list_truncated": len(gaps) > 50,
                   "coverage_status": "按声明间隔检查" if product.sampling_interval_seconds else "采样间隔未声明，无法判断缺口",
                   "source_file": receipt["source_file"], "source_sha256": receipt["source_sha256"],
                   "standard_sha256": receipt["standard_sha256"], "imported_at": receipt["imported_at"],
                   "quality_scope": "质量标志来自供数声明；入库校验不构成独立科学认证"})
    return result


def read_products(root, objects):
    root = Path(root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("监测目录不是文件夹")
    result = []
    ids = set()
    # 回执最后独占写入。未完成批次保留但不对外注册。
    for receipt_path in sorted(root.glob("*/接入回执.json")):
        receipt = strict_json(receipt_path.read_bytes())
        standard = receipt_path.parent / "监测产品.json"
        content = standard.read_bytes()
        if sha256(content) != receipt["standard_sha256"]:
            raise ValueError("监测产品哈希不符")
        for key in ("source_sha256", "standard_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
                raise ValueError("回执哈希不合法")
        if not isinstance(receipt["source_file"], str) or any(c in receipt["source_file"] for c in ("/", "\\", ":")):
            raise ValueError("回执文件名不合法")
        imported = datetime.fromisoformat(receipt["imported_at"])
        if imported.tzinfo is None:
            raise ValueError("回执时间缺少时区")
        product = Product.model_validate(strict_json(content))
        validate_objects(product, objects)
        summary = product_summary(product, receipt)
        if summary["product_id"] in ids:
            raise ValueError("存在重复监测产品版本")
        ids.add(summary["product_id"])
        result.append((product, summary))
    return result
