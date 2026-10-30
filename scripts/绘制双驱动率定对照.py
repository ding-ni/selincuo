"""绘制三套已完成成果的湖位与杜加日流量，不运行模型或变更采用关系。

图形合同：展示不同驱动完整率定后对湖位和流量的共同影响，原采用作为
参照。上图为2021年以来湖位，下图为2024—2025流量；实测缺测不插值。
三条模拟线都是各自全部资料正式拟合成果，不代表独立验证。
复用既有Python绘图脚本的字体及导出习惯，字段依据结果日表显式映射。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / 'build/grid-revision-20261026/workspace'
TASKS = {
    'previous': ('JOB-88b35b44b27d4e15840c', '原采用', '#777777', '--'),
    'baseline': ('JOB-6536f36e07cc46868255', '原驱动重率定', '#176b95', '-'),
    'warm': ('JOB-05c3f62282ce4239aed1', '暖雨订正重率定', '#bf6530', '-.'),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_results():
    database = WORKSPACE / 'database/hydrology.sqlite3'
    with sqlite3.connect('file:' + database.as_posix() + '?mode=ro', uri=True) as conn:
        statuses = dict(conn.execute('SELECT id,status FROM tasks WHERE id IN (?,?,?)',
                                    tuple(row[0] for row in TASKS.values())))
    results = {}
    for name, (task_id, _, _, _) in TASKS.items():
        if statuses.get(task_id) != 'succeeded':
            raise ValueError(f'{task_id} 尚未成功：{statuses.get(task_id, "missing")}')
        path = WORKSPACE / 'tasks' / task_id / 'result.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('taskId') != task_id:
            raise ValueError(f'{task_id} 成果身份不匹配')
        frame = pd.DataFrame(result['daily'])
        frame.index = pd.to_datetime(frame.pop('date')).dt.normalize()
        if frame.index.has_duplicates:
            raise ValueError(f'{task_id} 含重复日')
        for column in ('duja', 'observedFlow', 'lakeLevel', 'observedLevel'):
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
        results[name] = (path, result, frame.sort_index())
    return results


def compare_observations(results, field):
    reference = results['previous'][2][field]
    for name in ('baseline', 'warm'):
        other = results[name][2][field]
        pair = pd.concat([reference, other], axis=1).dropna()
        if pair.empty or not np.allclose(pair.iloc[:, 0], pair.iloc[:, 1], rtol=0, atol=1e-10):
            raise ValueError(f'{name} 的共同日期 {field} 观测不一致')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='尚不存在的独立输出目录')
    args = parser.parse_args()
    results = read_results()
    for field in ('observedFlow', 'observedLevel'):
        compare_observations(results, field)
    # 只使用三套成果共同存在的完整日，不把部分日或缺测补成观测。
    full_days = [frame.index[frame['forcingHours'] == 24] for _, _, frame in results.values()]
    days = full_days[0].intersection(full_days[1]).intersection(full_days[2]).sort_values()
    start = max(pd.Timestamp('2021-04-24'), days.min())
    end = days.max()
    days = days[(days >= start) & (days <= end)]
    data = pd.DataFrame(index=days)
    data.index.name = 'date'
    reference = results['previous'][2]
    for field in ('observedLevel', 'observedFlow'):
        data[field] = reference[field].reindex(days)
    for name in TASKS:
        frame = results[name][2]
        data[name + '_level'] = frame['lakeLevel'].reindex(days)
        data[name + '_flow'] = frame['duja'].reindex(days)
    if not len(data) or data['observedLevel'].notna().sum() == 0:
        raise ValueError('共同湖位日期或观测缺失')
    flow = data.loc['2024-01-01':'2025-12-31']
    if flow['observedFlow'].notna().sum() == 0:
        raise ValueError('2024—2025无共同流量观测')
    args.output.mkdir(parents=True, exist_ok=False)
    csv_path = args.output / '日过程对照.csv'
    data.to_csv(csv_path, encoding='utf-8-sig', float_format='%.10f', date_format='%Y-%m-%d')
    plt.rcParams.update({'font.family': ['Microsoft YaHei', 'Arial', 'DejaVu Sans'],
        'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
        'svg.fonttype': 'none', 'pdf.fonttype': 42, 'axes.unicode_minus': False})
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), layout='constrained',
                             gridspec_kw={'height_ratios': [1.05, 1]})
    for name, (_, label, color, style) in TASKS.items():
        axes[0].plot(data.index, data[name + '_level'], color=color, linestyle=style,
                     linewidth=1.3, label=label)
        axes[1].plot(flow.index, flow[name + '_flow'], color=color, linestyle=style,
                     linewidth=1.15, label=label)
    axes[0].plot(data.index, data['observedLevel'], color='#202020', linewidth=.85,
                 marker='.', markersize=2.3, zorder=5)
    axes[1].plot(flow.index, flow['observedFlow'], color='#202020', linewidth=.8,
                 marker='.', markersize=2.3, zorder=5)
    axes[0].set(xlim=(start, end), ylabel='湖位（m）')
    axes[0].set_title('a  色林错湖位', loc='left')
    axes[0].ticklabel_format(axis='y', style='plain', useOffset=False)
    axes[1].set(xlim=(pd.Timestamp('2024-01-01'), pd.Timestamp('2025-12-31')),
                ylabel='杜加流量（m³/s）', xlabel='日期')
    axes[1].set_title('b  杜加日均流量', loc='left')
    handles = [Line2D([], [], color='#202020', marker='.', linewidth=.8, label='实测')]
    handles += [Line2D([], [], color=color, linestyle=style, linewidth=1.3, label=label)
                for _, label, color, style in TASKS.values()]
    fig.legend(handles=handles, loc='outside upper center', ncol=4, frameon=False)
    outputs = {}
    png_path = args.output / '双驱动流量湖位对照.png'
    svg_path = args.output / '双驱动流量湖位对照.svg'
    pdf_path = args.output / '双驱动流量湖位对照.pdf'
    fig.savefig(png_path, dpi=600, facecolor='white')
    fig.savefig(svg_path, facecolor='white')
    fig.savefig(pdf_path, facecolor='white')
    for extension, path in [('png', png_path), ('svg', svg_path), ('pdf', pdf_path)]:
        outputs[extension] = {'file': path.name, 'sha256': sha(path)}
    plt.close(fig)
    counts = {}
    for field, suffix, source in [('湖位', '_level', data), ('流量', '_flow', flow)]:
        obs = 'observedLevel' if suffix == '_level' else 'observedFlow'
        paired = source[[obs] + [name + suffix for name in TASKS]].dropna()
        counts[field] = {'共同有效观测日': len(paired), '观测日': int(source[obs].notna().sum())}
    metadata = {'figureContract': '对照三套正式拟合日过程，展示流量与湖位的共同表现',
        'panelRoles': {'a': '2021年以来湖位过程', 'b': '2024—2025杜加流量过程'},
        'fieldMapping': {'lakeLevel': '日湖位 m', 'observedLevel': '主站观测湖位 m',
                         'duja': '杜加日均流量 m³/s', 'observedFlow': '杜加实测日均流量 m³/s'},
        'dateRange': [str(start.date()), str(end.date())], 'counts': counts,
        'missing': '缺测断线，无插值；仅展示三套共有完整驱动日',
        'evaluation': '各方案为全部资料正式拟合；2025—2026已参与开发和资料订正，不称独立验证。',
        'uncertainty': '确定性过程线，无置信区间；日值具有时间相关性。',
        'sources': {name: {'taskId': TASKS[name][0], 'file': str(path.relative_to(ROOT)),
                           'sha256': sha(path), 'inputId': result.get('inputId')}
                    for name, (path, result, _) in results.items()},
        'outputs': {**outputs, 'csv': {'file': csv_path.name, 'sha256': sha(csv_path)}}}
    meta_path = args.output / '双驱动流量湖位对照.figure.json'
    with meta_path.open('x', encoding='utf-8') as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'output': str(args.output), 'counts': counts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
