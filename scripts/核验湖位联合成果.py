"""只读核验流量约束下湖位联合率定的完整交付。

该脚本只读取任务数据库、率定工件和显式指定的同条件对照，不创建任务、不
实例化 Jobs、不重算模型，也不会覆盖已有证据。联合任务尚未成功时默认只
输出状态预检；只有提供新旧结果和输出路径，才写正式
交付证据。
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.common import ROOT, WORKSPACE, canonical, digest, read_json, sha_file, within
from backend.fast_calibration import array_metrics
from backend.grid_storage import open_arrays
from backend.lake_calibration import assess as lake_assess, joint_assess
from backend.model_core import normalize_initial_hydrology
from backend.simulation import RIVERS


OLD_DEFAULT = "JOB-4934a5d35c9344a59353"
DEFAULT_RECORD = "build/湖位联合率定-20261029/启动记录.json"
DEFAULT_DB = WORKSPACE / "database" / "hydrology.sqlite3"
FLOW_YEARS = (2024, 2025)
FLOW_KEYS = ("duja", "zajia", "zagen", "boqu", "ali", "fringe", "total")
PERIODIC_KEYS = ("lakeInflowM3", "lakePrecipitationM3", "lakeEvaporationM3", "lakeDeltaVolumeM3")


def _path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _task(connection: sqlite3.Connection, identifier: str) -> dict:
    row = connection.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise ValueError(f"任务不存在：{identifier}")
    names = [item[1] for item in connection.execute("PRAGMA table_info(tasks)")]
    value = dict(zip(names, row))
    for key in ("request", "result"):
        if value.get(key):
            value[key] = json.loads(value[key])
    return value


def _task_result(connection: sqlite3.Connection, identifier: str) -> tuple[dict, Path]:
    value = _task(connection, identifier)
    if value["status"] != "succeeded" or not isinstance(value.get("result"), dict):
        raise ValueError(f"任务尚未成功完成：{identifier}（{value['status']}）")
    folder = within(WORKSPACE / "tasks", identifier)
    if not (folder / "result.json").is_file():
        raise ValueError(f"任务缺少 result.json：{identifier}")
    return value["result"], folder


def _frame(result: dict) -> pd.DataFrame:
    values = pd.DataFrame(result.get("daily", []))
    if values.empty or "date" not in values:
        raise ValueError("结果缺少日过程")
    values.index = pd.to_datetime(values.pop("date"))
    if values.index.has_duplicates:
        raise ValueError("日过程包含重复日期")
    return values.sort_index()


def _same(a, b) -> bool:
    return canonical(a) == canonical(b)


def _finite_mask(values: pd.Series | np.ndarray) -> np.ndarray:
    return np.isfinite(np.asarray(values, dtype=float))


def _flow_metric(frame: pd.DataFrame, start: str, end: str) -> dict | None:
    selected = frame.loc[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    if "forcingHours" in selected:
        selected = selected.loc[selected.forcingHours == 24]
    selected = selected[["observedFlow", "duja"]].dropna()
    selected = selected.loc[_finite_mask(selected.observedFlow) & _finite_mask(selected.duja)]
    if selected.empty:
        return None
    result = array_metrics(selected.observedFlow.to_numpy(), selected.duja.to_numpy())
    result["countByYear"] = {str(int(year)): int(len(group))
                             for year, group in selected.groupby(selected.index.year)}
    return result


def _lake_report(frame: pd.DataFrame, window: dict, coefficient: float,
                 baseline_coefficient: float) -> dict:
    copy = frame.copy()
    copy["baseLevel"] = copy["lakeLevel"]
    copy["slope"] = 0.0
    return joint_assess(copy, coefficient, baseline_coefficient, window)


def _yearly_metrics(frame: pd.DataFrame, window: dict, anchor: dict) -> dict:
    frozen = pd.DatetimeIndex(pd.to_datetime(window["dates"]))
    data = frame.reindex(frozen).copy()
    data["baseLevel"] = data["lakeLevel"]
    data["slope"] = 0.0
    result = {}
    for year in sorted(set(frozen.year) | set(frame.index.year)):
        start, end = f"{year}-01-01", f"{year + 1}-01-01"
        flow = _flow_metric(frame, start, end)
        lake = lake_assess(data, 0.0, 0.0, start, end, anchor) if year in frozen.year else None
        if flow or (lake and lake.get("count")):
            result[str(year)] = {"flow": flow, "lake": lake}
    return result


def _audit_hourly(folder: Path, frame: pd.DataFrame, result: dict) -> dict:
    manifest_path = folder / "hourly-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("缺少 hourly-manifest.json")
    manifest = read_json(manifest_path)
    hourly_folder = folder / "hourly.zarr"
    if not hourly_folder.is_dir() or not manifest.get("complete"):
        raise ValueError("逐时工件未完整写入")
    files = {path.relative_to(hourly_folder).as_posix(): sha_file(path)
             for path in sorted(hourly_folder.rglob("*")) if path.is_file()}
    if files != manifest.get("files") or digest(files) != manifest.get("sha256"):
        missing = sorted(set(manifest.get("files", {})) - set(files))
        extra = sorted(set(files) - set(manifest.get("files", {})))
        changed = sorted(key for key in set(files) & set(manifest.get("files", {}))
                         if files[key] != manifest["files"][key])
        raise ValueError(f"逐时清单不一致：missing={len(missing)} extra={len(extra)} changed={len(changed)}")
    arrays = open_arrays(folder)
    if len(arrays["time"]) != int(result.get("hours", len(arrays["time"]))):
        raise ValueError("逐时长度与结果不一致")
    sums = np.zeros(len(RIVERS), dtype=float)
    max_sum_error = 0.0
    max_excluded = 0.0
    flow_index = {name: RIVERS.index(name) for name in RIVERS}
    for start in range(0, len(arrays["time"]), 168):
        values = np.asarray(arrays["flow"][start:start + 168], dtype=float)
        sums += values.sum(axis=0) * 3600.0
        expected = sum(values[:, flow_index[name]] for name in ("zajia", "zagen", "boqu", "ali", "fringe"))
        max_sum_error = max(max_sum_error, float(np.max(np.abs(values[:, flow_index["total"]] - expected))))
        max_excluded = max(max_excluded, float(np.max(np.abs(np.asarray(
            arrays["excludedRunoff"][start:start + 168], dtype=float)))))
    return {"manifestSHA256": sha_file(manifest_path), "manifestFiles": len(files),
            "hourlyHours": int(len(arrays["time"])), "hourlyArtifactsVerified": True,
            "maximumTotalFlowSummationDifferenceM3s": max_sum_error,
            "excludedBackgroundRunoffM3s": max_excluded,
            "flowVolumesM3": {name: float(sums[index]) for name, index in flow_index.items()}}


def _aggregation_audit(result: dict, hourly: dict) -> dict:
    diagnostics = result.get("diagnostics", {})
    partitions = diagnostics.get("partitions", {})
    if "duja" not in partitions or "zajia_rest" not in partitions:
        raise ValueError("缺少杜加和扎加其余分区汇总")
    station_sum = sum(float(partitions[name].get("routedM3", 0.)) for name in ("duja", "zajia_rest"))
    station_error = float(hourly["flowVolumesM3"]["zajia"] - station_sum)
    if abs(station_error) > .05:
        raise ValueError(f"杜加控制区重复计入或汇总不一致：{station_error} m3")
    if hourly["maximumTotalFlowSummationDifferenceM3s"] > 1e-8:
        raise ValueError("四河与总入湖流量的逐时汇总不一致")
    if hourly["excludedBackgroundRunoffM3s"] > 1e-8:
        raise ValueError("未确定去向的背景区域进入了新方案汇总")
    return {"dujaIncludedOnceVolumeErrorM3": station_error,
            "partitionRoutedM3": {key: float(value.get("routedM3", 0.))
                                  for key, value in partitions.items()},
            "backgroundRunoffExcluded": True}


def _water_budget(frame: pd.DataFrame, anchor: dict) -> dict:
    start = pd.Timestamp(anchor["time"]).normalize() + pd.Timedelta(days=1)
    full = frame.loc[frame["forcingHours"] == 24]
    end = full.index.max().normalize()
    expected = pd.date_range(start, end, freq="D")
    data = frame.loc[(frame.index >= start) & (frame.index <= end)].copy()
    data = data.loc[data.get("forcingHours", 24) == 24]
    missing = expected.difference(data.index)
    if len(missing):
        raise ValueError(f"起算日后缺少完整模拟日：{missing[0].date()}")
    for key in PERIODIC_KEYS:
        if key not in data or not _finite_mask(data[key]).all():
            raise ValueError(f"水量收支缺少有效字段：{key}")
    data["month"] = data.index.to_period("M")
    data["year"] = data.index.year
    def grouped(column: str) -> list[dict]:
        rows = []
        for period, values in data.groupby(column):
            row = {key: float(values[key].sum()) for key in PERIODIC_KEYS}
            row["completeDays"] = int(len(values))
            row["period"] = str(period)
            rows.append(row)
        return rows
    total = {key: float(data[key].sum()) for key in PERIODIC_KEYS}
    total["completeDays"] = int(len(data))
    return {"anchorDayExcluded": pd.Timestamp(anchor["time"]).date().isoformat(),
            "firstCompleteDay": start.date().isoformat(), "lastCompleteDay": end.date().isoformat(),
            "completeDays": len(data), "total": total, "monthly": grouped("month"),
            "yearly": grouped("year")}


def _periods(request: dict) -> tuple[str, str, str, str]:
    value = request.get("periods", {})
    flow_start = value.get("trainingStart")
    flow_end = value.get("trainingEnd")
    evaluation_start = value.get("evaluationStart")
    evaluation_end = value.get("evaluationEnd")
    if not all((flow_start, flow_end, evaluation_start, evaluation_end)):
        raise ValueError("任务请求缺少杜加率定和检验时段")
    return flow_start, flow_end, evaluation_start, evaluation_end


def _formal_search(folder: Path, selection: dict, request: dict) -> dict:
    # The constrained pass deliberately keeps its large candidate arrays outside
    # Git.  Only counts, hashes and score summaries are copied into the evidence.
    source_trace = within(WORKSPACE / "tasks" / selection["candidateSourceTaskId"],
                          "all-data/search-trace.jsonl")
    if sha_file(source_trace) != selection.get("candidateSourceTraceSHA256"):
        raise ValueError("候选源轨迹 SHA-256 不一致")
    trace_names = {"duja-rescore.jsonl": "rescore", "lake-candidates.jsonl": "lakeCandidates",
                   "search-trace.jsonl": "refinement", "all-lake-fits.jsonl": "allLakeFits"}
    details = {}
    for filename, label in trace_names.items():
        path = folder / filename
        if not path.is_file():
            continue
        count = 0
        evaluations = set()
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                if "evaluation" in row:
                    evaluations.add(row["evaluation"])
        details[label] = {"file": str(path.relative_to(ROOT)).replace("\\", "/"),
                          "lines": count, "uniqueEvaluations": len(evaluations), "sha256": sha_file(path)}
    if selection.get("totalRescored") != details.get("rescore", {}).get("lines"):
        raise ValueError("杜加候选复评数量与记录不一致")
    if selection.get("basinCandidatesEvaluated") != details.get("lakeCandidates", {}).get("lines"):
        raise ValueError("全流域候选数量与记录不一致")
    refinement = selection.get("refinement", {})
    if details.get("refinement", {}).get("uniqueEvaluations", 0) != refinement.get("evaluations", 0):
        raise ValueError("联合细化评价数量与记录不一致")
    return {"candidateSourceTaskId": selection["candidateSourceTaskId"],
            "candidateSourceTraceSHA256": selection["candidateSourceTraceSHA256"],
            "flowConstraints": selection.get("flowConstraints"),
            "candidateLimit": request.get("twoStage", {}).get("candidateLimit"),
            "totalRescored": selection.get("totalRescored"),
            "eligibleRescored": selection.get("eligibleRescored"),
            "selectedForBasin": selection.get("selectedForBasin"),
            "basinCandidatesEvaluated": selection.get("basinCandidatesEvaluated"),
            "jointBudget": refinement.get("budget", request.get("twoStage", {}).get("jointBudget")),
            "refinement": {**{key: refinement.get(key) for key in
                              ("evaluations", "converged", "termination", "bestCandidateId")},
                           "starts": [{key: row.get(key) for key in
                                       ("seed", "evaluations", "budget", "converged", "termination", "generations")}
                                      for row in refinement.get("starts", [])],
                           "stateSHA256": sha_file(folder / "refinement-state.json")
                           if (folder / "refinement-state.json").is_file() else None},
            "seeds": selection.get("seeds"), "traceFiles": details,
            "initialParameters": selection.get("initialParameters"),
            "bounds": selection.get("bounds"), "selectedParameters": selection.get("selectedParameters"),
            "scoreVersion": selection.get("scoreVersion")}


def _lake_fit_summary(fit: dict) -> dict:
    result = {key: deepcopy(fit.get(key)) for key in
              ("algorithm", "value", "initial", "bounds", "boundary", "trainingPeriod", "validationPeriod",
               "training", "validation", "jointScore", "converged", "termination",
               "continuousOptimumGuaranteed", "fixedTrainingObservationSHA256")}
    ordinary = fit.get("rmseOnlyFit") or {}
    result["rmseOnlyFit"] = {key: deepcopy(ordinary.get(key)) for key in
                             ("algorithm", "value", "unconstrainedValue", "boundary", "training", "termination")}
    result["fixedTrainingDays"] = len(fit.get("fixedTrainingDates", []))
    return result


def _preflight(connection: sqlite3.Connection, identifier: str) -> dict:
    result, folder = _task_result(connection, identifier)
    frame = _frame(result)
    flow = {str(year): _flow_metric(frame, f"{year}-01-01", f"{year + 1}-01-01") for year in FLOW_YEARS}
    hourly = _audit_hourly(folder, frame, result)
    aggregation = _aggregation_audit(result, hourly)
    return {"taskId": identifier, "status": "succeeded", "resultSHA256": sha_file(folder / "result.json"),
            "inputId": result.get("inputId"), "spatialVersion": result.get("spatialVersion"),
            "initialization": result.get("initialization"), "flow": flow,
            "checks": {**hourly, **aggregation,
                       "cumulativeLakeBalanceErrorM3": float(sum(
                           row.get("lakeBalanceErrorM3") or 0. for row in result.get("daily", []))),
                       "dailyRows": len(frame), "hourlyManifestVerified": True}}


def _comparison(connection: sqlite3.Connection, identifier: str, reference: pd.DataFrame,
                periods: tuple[str, str, str, str], lake_windows: dict,
                baseline_coefficient: float) -> dict:
    result, folder = _task_result(connection, identifier)
    frame = _frame(result)
    flow_start, flow_end, evaluation_start, evaluation_end = periods
    # Use the current task's frozen observations for every model. This avoids
    # silently comparing two observation snapshots while preserving the original
    # model outputs and their own result identities.
    for key in ("observedFlow", "observedLevel"):
        if key in reference:
            frame[key] = reference[key].reindex(frame.index)
    flow = {"training": _flow_metric(frame, flow_start, flow_end),
            "evaluation": _flow_metric(frame, evaluation_start, evaluation_end),
            "all": _flow_metric(frame, min(flow_start, evaluation_start), max(flow_end, evaluation_end))}
    lake = {}
    for label, window in lake_windows.items():
        coeff = float(result.get("parameters", {}).get("lakeCoefficient", baseline_coefficient))
        lake[label] = _lake_report(frame, window, coeff, baseline_coefficient)
    return {"taskId": identifier, "resultSHA256": sha_file(folder / "result.json"),
            "inputId": result.get("inputId"), "spatialVersion": result.get("spatialVersion"),
            "initialization": result.get("initialization"), "parameters": result.get("parameters"),
            "partitionParameters": result.get("partitionParameters", {}), "flow": flow, "lake": lake,
            "yearly": _yearly_metrics(frame, lake_windows["allAvailable"], result["initialization"])}


def formal(connection: sqlite3.Connection, identifier: str, old_identifier: str,
           baseline_path: Path, output: Path) -> dict:
    new, new_folder = _task_result(connection, identifier)
    request = json.loads((new_folder / "request.json").read_text(encoding="utf-8-sig"))
    if new.get("calibrationWorkflow", {}).get("selectionMode") != "flow-constrained-lake":
        raise ValueError("指定任务不是流量约束下的湖位联合率定")
    selection = new["calibrationWorkflow"].get("selection")
    if not selection:
        raise ValueError("联合率定结果缺少 selection 记录")
    if not baseline_path.is_file():
        raise ValueError(f"同条件参数对照尚未生成：{baseline_path}")
    baseline = read_json(baseline_path)
    if not isinstance(baseline, dict) or not baseline.get("daily"):
        raise ValueError("同条件参数对照不是完整结果 JSON")
    old, old_folder = _task_result(connection, old_identifier)
    new_frame = _frame(new)
    if not _same(new.get("inputId"), baseline.get("inputId")) or not _same(new.get("spatialVersion"), baseline.get("spatialVersion")):
        raise ValueError("新成果与同条件对照的输入或空间不一致")
    if new.get("start") != baseline.get("start") or new.get("end") != baseline.get("end"):
        raise ValueError("新成果与同条件对照的起止时间不一致")
    if new.get("initialization") != baseline.get("initialization"):
        raise ValueError("新成果与同条件对照的湖位起算不一致")
    if not _same(normalize_initial_hydrology(new.get("initialHydrology")), normalize_initial_hydrology(baseline.get("initialHydrology"))):
        raise ValueError("新成果与同条件对照的初始蓄水状态不一致")
    if new.get("routingMode") != baseline.get("routingMode"):
        raise ValueError("新成果与同条件对照的汇流模式不一致")
    if new.get("identity", {}).get("modelImplementation") != baseline.get("identity", {}).get("modelImplementation"):
        raise ValueError("新成果与同条件对照的模型代码身份不一致")
    if not _same(baseline.get("parameters"), old.get("parameters")):
        raise ValueError("同条件对照未使用旧默认参数")
    state = read_json(new_folder / "pipeline-state.json", {})
    land_folder = within(new_folder, state.get("baselineFolder", "land-baseline"))
    land_baseline = read_json(land_folder / "result.json")
    if not land_baseline:
        raise ValueError("缺少湖泊率定前固定来水基准")
    hydrology = {key: value for key, value in new["parameters"].items() if key != "lakeCoefficient"}
    land_hydrology = {key: value for key, value in land_baseline["parameters"].items() if key != "lakeCoefficient"}
    if not _same(hydrology, land_hydrology):
        raise ValueError("湖泊阶段未保持同一组陆域水文参数")
    land_frame = _frame(land_baseline)
    if not new_frame[list(FLOW_KEYS)].equals(land_frame[list(FLOW_KEYS)]):
        raise ValueError("湖泊阶段前后的陆域来水过程不一致")
    if new.get("partitionParameters"):
        raise ValueError("新成果仍含独立分区参数覆盖")
    if new.get("parameterTransfer", {}).get("mode") != "duja-shared":
        raise ValueError("新成果未使用共享参数策略")
    effective = new.get("parameterTransferAudit", {}).get("effective", {}).get("regions", {})
    if not effective or any(not _same(row.get("parameters"), new["parameters"]) for row in effective.values()):
        raise ValueError("存在区域未使用共享参数")
    if set(unit.get("partition") for unit in new.get("units", [])) & {"external", "unresolved"}:
        raise ValueError("待确认背景范围仍属于模型计算单元")
    if not _same(selection.get("parameters"), new.get("parameters")):
        raise ValueError("筛选参数和正式成果参数不一致")
    if not selection.get("trainingUsesValidationLake") is False or not selection.get("finalUsesAllLakeObservations"):
        raise ValueError("湖位训练/全部资料阶段标记不正确")
    if selection.get("flowParameterSelectionUsesLakeLevel") is not True:
        raise ValueError("联合选择没有记录湖位约束")
    identity = read_json(new_folder / "joint" / "identity.json", {})
    if identity.get("identity") != selection.get("identity"):
        raise ValueError("候选身份与冻结窗口身份不一致")
    for key in ("trainingLake", "testingLake", "allLake"):
        if not identity.get(key, {}).get("dates"):
            raise ValueError(f"缺少冻结湖位窗口：{key}")
    if set(identity["trainingLake"]["dates"]) & set(identity["testingLake"]["dates"]):
        raise ValueError("湖位率定期与检验期发生泄漏")
    if not (set(identity["trainingLake"]["dates"]) | set(identity["testingLake"]["dates"])) <= set(identity["allLake"]["dates"]):
        raise ValueError("全部湖位窗口不是训练期和检验期的并集")
    # The new result carries the frozen observations for its own evaluation.
    reference = new_frame[[key for key in ("observedFlow", "observedLevel") if key in new_frame]]
    periods = _periods(request)
    lake_windows = {"training2021_2024": identity["trainingLake"],
                    "timeCheck2025_2026": identity["testingLake"],
                    "allAvailable": identity["allLake"]}
    old_comparison = _comparison(connection, old_identifier, reference, periods, lake_windows,
                                 float(baseline["parameters"]["lakeCoefficient"]))
    reference_comparison = _comparison_from_json(baseline, baseline_path, reference, periods,
                                                 lake_windows, float(baseline["parameters"]["lakeCoefficient"]))
    new_comparison = _comparison(connection, identifier, reference, periods, lake_windows,
                                 float(baseline["parameters"]["lakeCoefficient"]))
    hourly = _audit_hourly(new_folder, new_frame, new)
    aggregation = _aggregation_audit(new, hourly)
    budget = read_json(new_folder / "joint" / selection.get("waterBudgetFile", "water-budget.json"))
    if budget is None:
        raise ValueError("缺少湖泊水量收支表")
    computed_budget = _water_budget(new_frame, new["initialization"])
    # Stored sums are compared with the model-derived daily values; a small
    # absolute allowance covers JSON rounding and fast/full floating-point order.
    budget_differences = {}
    for grouping in ("monthly", "yearly"):
        stored = {row["period"]: row for row in budget.get(grouping, [])}
        calculated = {row["period"]: row for row in computed_budget[grouping]}
        if set(stored) != set(calculated):
            raise ValueError(f"湖泊{grouping}收支时段与完整日过程不一致")
        maxima = {key: 0.0 for key in PERIODIC_KEYS}
        for period, row in calculated.items():
            if stored[period]["completeDays"] != row["completeDays"]:
                raise ValueError(f"湖泊收支完整天数不一致：{period}")
            for key in PERIODIC_KEYS:
                difference = abs(float(stored[period][key]) - row[key])
                maxima[key] = max(maxima[key], difference)
                if difference > max(0.1, abs(row[key]) * 1e-8):
                    raise ValueError(f"湖泊收支不一致：{period} {key} {difference}")
        budget_differences[grouping] = maxima
    workflow = new.get("calibrationWorkflow", {})
    compact_flow = {key: deepcopy(value) for key, value in workflow.get("flowFits", [{}])[-1].items()
                    if key not in ("parameters",)}
    formal_search = _formal_search(new_folder / "joint", selection, request)
    warmup_file = baseline_path.parent / "summary.json"
    warmup = None
    if warmup_file.is_file():
        reference_warmup = read_json(warmup_file)
        if reference_warmup.get("parametersSHA256") != digest(baseline["parameters"]):
            raise ValueError("旧参数预热核验的参数摘要与同条件对照不一致")
        if reference_warmup.get("inputId") != baseline.get("inputId") or reference_warmup.get("spatialVersion") != baseline.get("spatialVersion"):
            raise ValueError("旧参数预热核验的输入或空间与同条件对照不一致")
        warmup = {"file": str(warmup_file.relative_to(ROOT)).replace("\\", "/"),
                  "sha256": sha_file(warmup_file), "result": reference_warmup}
    else:
        raise ValueError("缺少旧参数同条件预热核验证据")
    selected_warmup_file = new_folder / "warmup-diagnostics" / "summary.json"
    if not selected_warmup_file.is_file() or not new.get("warmupDiagnostics"):
        raise ValueError("缺少最终参数预热核验证据")
    selected_warmup = read_json(selected_warmup_file)
    if not _same(selected_warmup, new["warmupDiagnostics"]):
        raise ValueError("最终成果与预热核验摘要不一致")
    if selected_warmup.get("parametersSHA256") != digest(new["parameters"]):
        raise ValueError("最终参数预热核验的参数摘要与正式成果不一致")
    if selected_warmup.get("inputId") != new.get("inputId") or selected_warmup.get("spatialVersion") != new.get("spatialVersion"):
        raise ValueError("最终参数预热核验的输入或空间与正式成果不一致")
    if selected_warmup.get("referenceStart") != new.get("start"):
        raise ValueError("最终参数预热核验的起点与正式成果不一致")
    selected_warmup = {"file": str(selected_warmup_file.relative_to(ROOT)).replace("\\", "/"),
                       "sha256": sha_file(selected_warmup_file), "result": selected_warmup}
    setting = connection.execute("SELECT value FROM settings WHERE key=?", ("business-adopted-scheme-v1",)).fetchone()
    adopted = json.loads(setting[0]) if setting else {}
    flow_all = new_comparison["flow"]["all"] or {}
    thresholds = selection["flowConstraints"]
    passes_flow = (flow_all.get("nse") is not None and flow_all.get("pbias") is not None and
                   flow_all["nse"] >= thresholds["minNse"] and abs(flow_all["pbias"]) <= thresholds["maxAbsPbias"])
    lake_rmse = new_comparison["lake"]["allAvailable"]["rmse"]
    result = {"schemaVersion": 2, "taskId": identifier, "resultSHA256": sha_file(new_folder / "result.json"),
              "requestSHA256": sha_file(new_folder / "request.json"),
              "oldDefaultTaskId": old_identifier, "oldDefaultResultSHA256": sha_file(old_folder / "result.json"),
              "sameConditionBaselineFile": str(baseline_path.relative_to(ROOT)).replace("\\", "/"),
              "sameConditionBaselineSHA256": sha_file(baseline_path),
              "inputId": new.get("inputId"), "spatialVersion": new.get("spatialVersion"),
              "initialization": new.get("initialization"), "warmupStart": request.get("start"),
              "parameters": new.get("parameters"), "parameterSources": new.get("parameterSources"),
              "adoption": {"currentSetting": adopted, "taskAdoption": new.get("adoption"),
                           "adoptionSkipped": new.get("adoptionSkipped"),
                           "isCurrentResult": adopted.get("resultId") == identifier},
              "flowFit": compact_flow, "selection": {key: deepcopy(selection.get(key)) for key in (
                  "trainingCandidateId", "finalCandidateId", "flowMetrics", "flowObjective",
                  "adoptionComparison", "fastParity", "formalParity", "trainingUsesValidationLake",
                  "finalUsesAllLakeObservations", "flowParameterSelectionUsesLakeLevel")},
              "lakeFits": {"trainingAndTimeCheck": _lake_fit_summary(selection["trainingFit"]),
                           "allObservationsFinal": _lake_fit_summary(selection["finalFit"])},
              "frozenLakeWindows": {key: {field: deepcopy(identity[key].get(field)) for field in
                      ("start", "end", "observationSHA256", "seasonYears", "seasonMonths",
                       "minimumSeasonDays", "minimumDaysPerMonth", "anchorDayExcluded")}
                    | {"observationDays": len(identity[key]["dates"])}
                    for key in ("trainingLake", "testingLake", "allLake")},
              "search": formal_search,
              "warmupSensitivity": warmup,
              "selectedWarmup": selected_warmup,
              "comparisons": {"oldAdopted": old_comparison, "sameConditionBaseline": reference_comparison,
                              "newJoint": new_comparison},
              "waterBudget": {"storedFile": str((new_folder / "joint" / selection["waterBudgetFile"]).relative_to(ROOT)).replace("\\", "/"),
                              "storedSHA256": sha_file(new_folder / "joint" / selection["waterBudgetFile"]),
                              "computed": computed_budget, "maximumStoredAbsoluteDifferencesM3": budget_differences},
              "checks": {**hourly, **aggregation,
                         "parameterLakeStageFlowFixed": True,
                         "flowSelectionLakeLevelUsed": True,
                         "trainingValidationLakeMasksDisjoint": True,
                         "finalLakeWindowUsesAllFrozenObservations": bool(selection.get("finalUsesAllLakeObservations")),
                         "cumulativeLakeBalanceErrorM3": float(sum(row.get("lakeBalanceErrorM3") or 0.
                                                                    for row in new.get("daily", []))),
                         "firstCompleteLakeDay": (pd.Timestamp(new["initialization"]["time"]).normalize() + pd.Timedelta(days=1)).date().isoformat(),
                         "allDataFlowDays": (new_comparison["flow"]["all"] or {}).get("count"),
                         "flowConstraintsMet": passes_flow,
                         "lakeRmseTargetM": 0.1, "lakeRmseTargetMet": lake_rmse <= 0.1,
                         "sharedParameterRegions": len(effective),
                         "selectedParameterWarmupIdentityVerified": True,
                         "oldDefaultComparisonIsNotSingleFactor": True},
              "interpretation": "2025—2026作为时间检验窗口报告，不称为未参与开发的独立验证；旧采用成果、同条件原参数和本轮联合成果包含不同模型历史，仅作可复现对照。",
              "formalArtifactNote": "逐时数组、完整搜索轨迹和种群保留在任务工作区；本证据只保存摘要、计数和 SHA-256。"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"output": str(output.relative_to(ROOT)).replace("\\", "/"), "taskId": identifier,
            "checks": result["checks"]}


def _comparison_from_json(result: dict, path: Path, reference: pd.DataFrame,
                          periods: tuple[str, str, str, str], lake_windows: dict,
                          baseline_coefficient: float) -> dict:
    frame = _frame(result)
    for key in ("observedFlow", "observedLevel"):
        if key in reference:
            frame[key] = reference[key].reindex(frame.index)
    flow_start, flow_end, evaluation_start, evaluation_end = periods
    flow = {"training": _flow_metric(frame, flow_start, flow_end),
            "evaluation": _flow_metric(frame, evaluation_start, evaluation_end),
            "all": _flow_metric(frame, min(flow_start, evaluation_start), max(flow_end, evaluation_end))}
    coeff = float(result.get("parameters", {}).get("lakeCoefficient", baseline_coefficient))
    lake = {key: _lake_report(frame, window, coeff, baseline_coefficient)
            for key, window in lake_windows.items()}
    return {"taskId": "same-condition-baseline", "resultSHA256": sha_file(path),
            "inputId": result.get("inputId"), "spatialVersion": result.get("spatialVersion"),
            "initialization": result.get("initialization"), "parameters": result.get("parameters"),
            "partitionParameters": result.get("partitionParameters", {}), "flow": flow, "lake": lake,
            "yearly": _yearly_metrics(frame, lake_windows["allAvailable"], result["initialization"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--record", default=DEFAULT_RECORD)
    parser.add_argument("--old-task-id", default=OLD_DEFAULT)
    parser.add_argument("--baseline-file", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--preflight", action="store_true", help="只读核验已完成成果，不写证据")
    args = parser.parse_args()
    record = read_json(_path(args.record), {}) or {}
    identifier = args.task_id or record.get("taskId")
    if not identifier:
        print(json.dumps({"status": "pending", "message": "联合任务尚未创建，未生成正式成果证据。"}, ensure_ascii=False))
        return
    connection = sqlite3.connect("file:" + str(DEFAULT_DB) + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        state = _task(connection, identifier)
        if state["status"] != "succeeded" or not state.get("result"):
            print(json.dumps({"status": state["status"], "taskId": identifier, "message": state.get("message"),
                              "progress": state.get("progress")}, ensure_ascii=False))
            return
        if args.preflight:
            print(json.dumps(_preflight(connection, identifier), ensure_ascii=False, indent=2))
            return
        if not args.baseline_file or not args.output:
            raise ValueError("正式交付必须同时提供 --baseline-file 和 --output；任务未完成时请使用 --preflight")
        print(json.dumps(formal(connection, identifier, args.old_task_id, _path(args.baseline_file),
                                 _path(args.output)), ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
