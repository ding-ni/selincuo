"""Conservative, optional source reservoirs ahead of the channel reservoir."""
import numpy as np
from .model_core import route_series

MODES = ('source-separated', 'lumped')


def route_partition(sources, old, params, mode):
    if mode not in MODES:
        raise ValueError('未知汇流结构')
    arrivals = sources[:, 0] + sources[:, 3]
    histories, errors, final_sources = [], {}, {}
    if mode == 'source-separated':
        for index, name, key in ((1, 'interflow', 'interflowTauHours'), (2, 'baseflow', 'baseflowTauHours')):
            storage = float(old.get('sources', {}).get(name, 0.))
            out, history, end, queue = route_series(sources[:, index], storage, np.zeros(2), params[key], 0.)
            arrivals = arrivals + out
            final_sources[name] = float(end)
            histories.append(history)
            errors[name] = float(sources[:, index].sum() + storage - out.sum() - end)
    else:
        arrivals = arrivals + sources[:, 1] + sources[:, 2]
        histories = [np.zeros(len(sources)), np.zeros(len(sources))]
    channel = old.get('channel', {})
    storage = float(channel.get('storage', 0.))
    queue = np.array(channel.get('queue', [0.] * (int(params['lagHours']) + 2)), dtype=float)
    if len(queue) != int(params['lagHours']) + 2:
        raise ValueError('传播状态长度与滞时参数不一致')
    before = storage + queue.sum()
    out, history, end, queue = route_series(arrivals, storage, queue, params['tauHours'], params['lagHours'])
    errors['channel'] = float(arrivals.sum() + before - out.sum() - history[-1])
    for name, error in errors.items():
        if abs(error) > max(1e-4, sources.sum() * 1e-10):
            raise ValueError(name + '调蓄水量不闭合')
    return out / 3600, history + histories[0] + histories[1], {
        'sources': final_sources, 'channel': {'storage': float(end), 'queue': queue.tolist()}
    }, np.stack(histories, axis=1), errors
