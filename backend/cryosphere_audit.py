"""Independent accounting of the existing snow/ice kernel, not new physics.

Reconstruct phase and snow transfers from forcing and saved states. Every term
is checked against the state transition. Ice mass is explicitly external because
the first model has neither a finite ice-volume state nor snow-to-firn conversion.
"""
import numpy as np
import pandas as pd


def cryosphere_report(times, precipitation, temperature, evaporation, diagnostics,
                      history, initial, units, parameters, regions=None):
    rain_total = np.asarray(precipitation, dtype=float)
    temperature = np.asarray(temperature, dtype=float)
    evaporation = np.asarray(evaporation, dtype=float)
    state = np.asarray(history, dtype=float)
    initial = np.asarray(initial, dtype=float)
    n, count = rain_total.shape
    if temperature.shape != (n, count) or evaporation.shape != (n, count):
        raise ValueError('冰雪审查输入维度不一致')
    if state.shape != (n, count, 8) or initial.shape != (count, 8) or len(units) != count:
        raise ValueError('冰雪审查状态维度不一致')
    ice = np.array([u['kind'] == 'glacier' for u in units])
    area = np.array([u['areaKm2'] for u in units], dtype=float)
    def value(key):
        return np.array([(regions or {}).get(u.get('partition'), parameters)[key] for u in units])[None, :]
    old_solid = np.vstack([initial[None, :, 0], state[:-1, :, 0]])
    old_liquid = np.vstack([initial[None, :, 1], state[:-1, :, 1]])
    fraction = np.clip((value('rainTemperature') - temperature) /
                       (value('rainTemperature') - value('snowTemperature')), 0, 1)
    snowfall = rain_total * fraction
    rain = rain_total - snowfall
    rain_on_snow = np.where((old_solid > 1e-10) | (snowfall > 1e-10), rain, 0.)
    snowmelt = diagnostics[:, :, 4]
    ice_melt = diagnostics[:, :, 5]
    ice_sublimation = diagnostics[:, :, 8]
    refreeze = np.minimum(old_liquid + rain_on_snow + snowmelt,
                          value('refreezeFactor') * np.maximum(-temperature, 0) / 24)
    solid_before_sublimation = old_solid + snowfall - snowmelt + refreeze
    pet = evaporation * np.where(ice[None, :], 1., value('KC'))
    snow_sublimation = np.minimum(solid_before_sublimation, pet * value('snowSublimation'))
    drainage = np.maximum(0, old_liquid + rain_on_snow + snowmelt - refreeze -
                          value('liquidCapacity') * state[:, :, 0])
    solid_error = old_solid + snowfall + refreeze - snowmelt - snow_sublimation - state[:, :, 0]
    liquid_error = old_liquid + rain_on_snow + snowmelt - refreeze - drainage - state[:, :, 1]
    transfer_error = rain_total - snow_sublimation - drainage - (rain-rain_on_snow) - (
        state[:, :, 0] + state[:, :, 1] - old_solid - old_liquid)
    terms = {
        'precipitation': rain_total, 'rainfall': rain, 'snowfall': snowfall,
        'rainOnSnow': rain_on_snow, 'snowmelt': snowmelt, 'refreeze': refreeze,
        'snowSublimation': snow_sublimation, 'snowDrainage': drainage,
        'directRain': rain-rain_on_snow, 'icemelt': ice_melt,
        'iceSublimation': ice_sublimation,
        'snowStorageChange': state[:, :, 0]+state[:, :, 1]-old_solid-old_liquid,
        'solidSnowStorageChange': state[:, :, 0]-old_solid,
    }
    # These are interval-end times; midnight belongs to the preceding date.
    year = (pd.DatetimeIndex(times)-pd.Timedelta(nanoseconds=1)).year
    rows = []
    for kind in ('land', 'glacier'):
        ids = np.flatnonzero(ice if kind == 'glacier' else ~ice)
        if not len(ids):
            continue
        scale = area[ids] * 1000.
        group_area = float(area[ids].sum())
        for y in sorted(set(year)):
            hours = np.flatnonzero(year == y)
            row = {'year': int(y), 'kind': kind, 'areaKm2': group_area, 'hours': len(hours),
                   'complete': len(hours) == (8784 if pd.Timestamp(int(y), 12, 31).is_leap_year else 8760)}
            for key, values in terms.items():
                volume = float((values[np.ix_(hours, ids)] @ scale).sum())
                row[key+'M3'] = volume
                row[key+'Mm'] = volume / (group_area*1000)
            first, last = hours[0], hours[-1]
            row['startSnowStorageMm'] = float(((old_solid[first, ids]+old_liquid[first, ids]) @ scale)/(group_area*1000))
            row['endSnowStorageMm'] = float(((state[last, ids, 0]+state[last, ids, 1]) @ scale)/(group_area*1000))
            row['snowfallFraction'] = row['snowfallM3']/row['precipitationM3'] if row['precipitationM3'] else None
            if kind == 'glacier':
                row['externalIceLossM3'] = row['icemeltM3']+row['iceSublimationM3']
                row['externalIceLossMm'] = row['externalIceLossM3']/(group_area*1000)
                row['modelledSolidMassChangeMm'] = row['solidSnowStorageChangeMm']-row['externalIceLossMm']
            rows.append(row)
    return {
        'schemaVersion': 1, 'annual': rows,
        'snowSolidMaxErrorMm': float(np.max(np.abs(solid_error))),
        'snowLiquidMaxErrorMm': float(np.max(np.abs(liquid_error))),
        'snowTransferMaxErrorMm': float(np.max(np.abs(transfer_error))),
        'finiteIceVolumeModelled': False, 'snowToFirnConversionModelled': False,
        'independentSnowObservationValidated': False, 'independentGlacierMassBalanceValidated': False,
        'definitions': {
            'snowmelt': 'Internal snow-to-liquid transfer; refreezing can cause repeated melt. Do not add to runoff.',
            'externalIceLoss': 'Bare-ice melt plus ice sublimation supplied by an unmodelled ice inventory.',
            'modelledSolidMassChange': 'Solid-snow storage change minus external ice loss; model diagnostic, not observed glacier mass balance.',
        },
    }
