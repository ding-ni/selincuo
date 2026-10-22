"""Public search definitions shared by the service, diagnostics and UI."""
RUNOFF_KEYS = ['KC', 'WM', 'B', 'SM', 'KI', 'KG', 'tauHours']
PROFILES = {
    'balanced': {'label': '洪峰、低流量与水量平衡', 'weights': {'nse': .5, 'logNse': .25, 'volume': .25}},
    'peaks': {'label': '原方案：洪峰优先', 'weights': {'nse': 1., 'logNse': 0., 'volume': .1}},
}


def flow_objective(metric, profile='balanced'):
    if profile not in PROFILES:
        raise ValueError('未知率定目标')
    weights = PROFILES[profile]['weights']
    if metric.get('nse') is None or metric.get('pbias') is None:
        raise ValueError('流量率定需要有效NSE和径流量偏差')
    if weights['logNse'] and metric.get('logNse') is None:
        raise ValueError('平衡目标需要有效低流量对数NSE')
    components = {'nse': 1-metric['nse'], 'logNse': 1-(metric.get('logNse') or 0),
                  'volume': abs(metric['pbias'])/100}
    return sum(weights[key]*value for key, value in components.items()), components


def public_config():
    return {'runoffKeys': RUNOFF_KEYS, 'lakeKeys': ['lakeCoefficient'], 'profiles': PROFILES,
            'defaultProfile': 'balanced', 'calibrationYear': 2024, 'warmupYear': 2023,
            'excludedYears': [2025, 2026], 'sensitivity': {'fraction': .2, 'method': 'one-at-a-time',
            'keys': ['KC', 'snowDdf', 'iceDdf', 'tauHours', 'interflowTauHours', 'baseflowTauHours', 'lakeCoefficient']}}
