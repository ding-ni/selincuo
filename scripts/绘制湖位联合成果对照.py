"""从已经完成的成果绘制湖位对照；不执行模型或修改采用关系。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd


SERIES = (
    ("observedLevel", "主站实测", "#252525", "-", 1.1),
    ("oldDefault", "原采用方案", "#8c8c8c", "--", 1.35),
    ("sameCondition", "同条件原参数", "#d89031", ":", 1.6),
    ("selected", "本轮优选参数", "#176b95", "-", 1.7),
)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frame(result: dict) -> pd.DataFrame:
    data = pd.DataFrame(result["daily"])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop("date"))).normalize()
    if data.index.has_duplicates:
        raise ValueError("日过程包含重复日期")
    return data.sort_index()


def save(fig, stem: Path) -> dict:
    outputs = {}
    for extension in ("svg", "pdf", "png"):
        path = stem.with_suffix("." + extension)
        fig.savefig(path, dpi=200, facecolor="white", pad_inches=.12)
        outputs[extension] = {"file": path.name, "sha256": sha(path)}
    plt.close(fig)
    return outputs


def plot(new_path: Path, old_path: Path, reference_path: Path, output: Path) -> dict:
    results = [read(path) for path in (new_path, old_path, reference_path)]
    new, old, reference = [frame(result) for result in results]
    start = pd.Timestamp(results[0]["initialization"]["time"]).normalize() + pd.Timedelta(days=1)
    end = new.loc[new.forcingHours == 24].index.max()
    days = pd.date_range(start, end, freq="D")
    data = pd.DataFrame(index=days)
    data.index.name = "date"
    data["observedLevel"] = new.observedLevel.reindex(days)
    data["oldDefault"] = old.lakeLevel.reindex(days)
    data["sameCondition"] = reference.lakeLevel.reindex(days)
    data["selected"] = new.lakeLevel.reindex(days)
    paired = data.dropna()
    counts = {key: int(data[key].notna().sum()) for key, *_ in SERIES}
    rmse = {key: float(np.sqrt(np.mean((paired[key] - paired.observedLevel) ** 2)))
            for key in ("oldDefault", "sameCondition", "selected")}
    # An independent folder prevents a rerun from replacing a previous figure.
    output.mkdir(parents=True, exist_ok=False)
    csv = output / "日湖位对照.csv"
    data.to_csv(csv, encoding="utf-8-sig", float_format="%.10f", date_format="%Y-%m-%d")

    plt.rcParams.update({
        "font.family": ["Arial", "Microsoft YaHei", "DejaVu Sans"],
        "font.size": 13, "axes.labelsize": 16,
        "xtick.labelsize": 13, "ytick.labelsize": 13, "legend.fontsize": 13,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": .7, "axes.grid": False,
        "svg.fonttype": "none", "pdf.fonttype": 42, "ps.fonttype": 42,
        "axes.unicode_minus": False, "savefig.bbox": "tight",
    })
    handles = [Line2D([], [], color=color, linestyle=style, linewidth=width)
               for _, _, color, style, width in SERIES]
    labels = [label for _, label, *_ in SERIES]
    fig, ax = plt.subplots(figsize=(14, 4.5), layout="constrained")
    for key, label, color, style, width in SERIES[1:]:
        ax.plot(data.index, data[key], label=label, color=color, linestyle=style, linewidth=width)
    ax.plot(data.index, data.observedLevel, color=SERIES[0][2], linewidth=1.1,
            marker=".", markersize=2.5, label=SERIES[0][1], zorder=5)
    ax.axvline(pd.Timestamp("2025-01-01"), color="#b7b7b7", linestyle="-.", linewidth=.8)
    ax.set(xlim=(start, end), xlabel="日期", ylabel="湖泊水位（m）")
    ticks = [start] + list(pd.date_range(f"{start.year + 1}-01-01", end, freq="YS"))
    ax.set_xticks(ticks)
    ax.set_xticklabels([start.strftime("%Y-%m")] + [date.strftime("%Y") for date in ticks[1:]])
    ax.ticklabel_format(axis="y", style="plain", useOffset=False)
    all_values = data.to_numpy(dtype=float)
    lo, hi = float(np.nanmin(all_values)), float(np.nanmax(all_values))
    pad = max((hi - lo) * .07, .05)
    ax.set_ylim(lo - pad, hi + pad)
    ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(.5, 1.18),
              ncol=4, frameon=False, handlelength=2.7)
    main_outputs = save(fig, output / "湖位过程对照")

    legend_fig = plt.figure(figsize=(11, .55))
    legend_fig.legend(handles, labels, loc="center", ncol=4, frameon=False, handlelength=2.7)
    legend_outputs = save(legend_fig, output / "图例_湖位过程对照")
    metadata = {
        "comparison_family": "色林错湖位联合优选", "variable": "日湖泊水位", "units": "m",
        "statistic": "各方案确定性模拟日值与主站整编实测日值；RMSE仅用共同有效观测日",
        "sample_size_n": len(paired),
        "unit_of_independence": "共同有效观测日；日值具有时间相关性，不视为独立重复",
        "uncertainty_definition": "not applicable; deterministic runs and observed daily series, no confidence intervals",
        "data_kind": "categorical",
        "counts": counts, "rmseM": rmse, "dateRange": [str(start.date()), str(end.date())],
        "palette": {label: color for _, label, color, *_ in SERIES},
        "normalization": "categorical colors plus distinct line styles; no colorbar",
        "limits": {"y": [lo - pad, hi + pad]}, "vmin": lo - pad, "vmax": hi + pad,
        "final_width_mm": 355.6, "minimum_font_pt": 13, "font_size_pt": 13,
        "legend_file": "图例_湖位过程对照.svg",
        "sources": [{"file": str(path.resolve()), "sha256": sha(path)}
                    for path in (new_path, old_path, reference_path)],
        "outputs": {"main": main_outputs, "legend": legend_outputs,
                    "csv": {"file": csv.name, "sha256": sha(csv)}},
        "caption": (
            f"色林错{start.date()}至{end.date()}日湖位。实测共{counts['observedLevel']}日，"
            f"三方案共同有效观测日n={len(paired)}。缺测断线；无置信区间。"
            "竖线为2025年1月1日的湖位训练/时间检验分界；2025—2026参与开发，"
            "不是未触碰独立验证。本轮正式结果另使用全部有效湖位拟合。"
            "原采用方案与新成果的预热和输入不同；同条件原参数对照用于分离参数变更。"
        ),
    }
    with (output / "湖位过程对照.figure.json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"folder": str(output), "pairedDays": len(paired), "rmseM": rmse}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-result", type=Path, required=True)
    parser.add_argument("--old-result", type=Path, required=True)
    parser.add_argument("--baseline-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(plot(args.new_result, args.old_result, args.baseline_file, args.output),
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
