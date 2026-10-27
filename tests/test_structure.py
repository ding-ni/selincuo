import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backend.routing import route_partition
from backend.model_core import parameters, route_series, PARAMETER_NAMES, run_kernel, initial_states
from backend.calibration_config import flow_objective
from backend.calibration import calibrate
from backend.model_diagnostics import diagnose


class StructureTests(unittest.TestCase):
    def test_source_reservoirs_conserve_and_resume_while_legacy_is_exact(self):
        p = parameters({'tauHours': 12, 'lagHours': 2.3})
        sources = np.random.default_rng(6).random((900, 4))*1000
        full, history, final, memory, errors = route_partition(sources, {}, p, 'source-separated')
        a, ah, state, am, _ = route_partition(sources[:371], {}, p, 'source-separated')
        b, bh, state, bm, _ = route_partition(sources[371:], state, p, 'source-separated')
        np.testing.assert_array_equal(full, np.r_[a, b])
        np.testing.assert_array_equal(memory, np.r_[am, bm])
        self.assertEqual(final, state)
        self.assertLess(abs(sources.sum()-full.sum()*3600-history[-1]), 1e-6)
        legacy, stored, _, _, _ = route_partition(sources, {}, p, 'lumped')
        expected, expected_stored, _, _ = route_series(sources.sum(axis=1), 0., np.zeros(4), 12, 2.3)
        np.testing.assert_allclose(legacy*3600, expected, rtol=1e-12, atol=1e-8)
        np.testing.assert_allclose(stored, expected_stored, rtol=1e-12, atol=1e-8)

    def test_slow_source_retains_more_than_fast_source_and_ice_mass_is_explicit(self):
        p = parameters()
        for col in (1, 2):
            sources = np.zeros((24, 4)); sources[0, col] = 1000
            _, _, final, _, _ = route_partition(sources, {}, p, 'source-separated')
            key = 'interflow' if col == 1 else 'baseflow'
            self.assertAlmostEqual(final['sources'][key], 1000*p['interflowTauHours' if col == 1 else 'baseflowTauHours']*(np.exp(-23/p['interflowTauHours' if col == 1 else 'baseflowTauHours'])-np.exp(-24/p['interflowTauHours' if col == 1 else 'baseflowTauHours'])))
        state = initial_states([{'kind':'glacier'}], p)
        d, h, _ = run_kernel(np.zeros((24,1)), np.full((24,1),6.), np.full((24,1),.4), np.array([True]), state, np.array([p[k] for k in PARAMETER_NAMES]))
        self.assertEqual(d.shape[-1],9)
        self.assertGreater(d[:,:,8].sum(),0)
        self.assertAlmostEqual(d[:,:,5].sum()+d[:,:,8].sum()-d[:,:,3].sum()-d[:,:,6].sum(),h[-1,0,[0,1,7]].sum(),places=9)

    def test_balanced_objective_penalizes_volume_and_low_flow_and_does_not_see_future(self):
        base = {'nse':.7,'logNse':.8,'pbias':0}
        a, _ = flow_objective(base)
        b, _ = flow_objective({**base,'pbias':40})
        c, _ = flow_objective({**base,'logNse':.1})
        self.assertGreater(b,a); self.assertGreater(c,a)
        self.assertAlmostEqual(flow_objective({**base,'pbias':40},'peaks')[0],.34)
        with self.assertRaises(ValueError):flow_objective({'nse':.5,'pbias':0})

    def test_midyear_warmup_or_incomplete_last_day_is_rejected(self):
        source = {'times':pd.DatetimeIndex(['2022-12-01','2026-01-01'])}
        with tempfile.TemporaryDirectory() as temp, patch('backend.calibration.prepare',return_value=source):
            for request in ({'start':'2023-07-01','end':'2026-01-01'},{'start':'2023-01-01','end':'2024-12-31T23:00'}):
                with self.assertRaises(ValueError):calibrate(None,{'inputId':'unit-test',**request},Path(temp))

    def test_sensitivity_rank_and_candidates_ignore_validation_values(self):
        from backend.sensitivity import analyze
        def run(future):
            def simulation(db, request, **kwargs):
                p=request['parameters'];loss=(p['KC']-.8)**2
                return {'parameters':p,'metrics':{'2024':{'flow':{'count':100,'nse':.8-loss,'logNse':.9-loss,'pbias':loss*10},'lake':{'count':100,'rmse':abs(p['lakeCoefficient']-.7)}},'2025':{'flow':{'nse':future}}}}
            with tempfile.TemporaryDirectory() as tmp, patch('backend.sensitivity.prepare',return_value={}), patch('backend.sensitivity.run_simulation',side_effect=simulation):
                return analyze(None,{'inputId':'unit-test'},Path(tmp))
        a,b=run(-100),run(100)
        self.assertEqual(a['sensitivity'],b['sensitivity'])
        self.assertEqual(a['parameters'],parameters())
        self.assertFalse(a['sensitivity']['parametersApplied'])

    def test_month_end_hour_and_daily_mean_window_residual(self):
        times=pd.date_range('2024-01-31T01:00',periods=48,freq='h')
        source={'precipitation':np.ones((48,1)),'lakeRain':np.full(48,1.),'lakeEvaporation':np.full(48,.2),
                'temperature':np.full((48,1),10.),'evaporation':np.zeros((48,1)),'deltaElevation':np.zeros(1)}
        diag=np.zeros((48,1,9));diag[:,:,0]=1
        states=np.zeros((48,1,8));initial=np.zeros((1,8))
        q={'total':np.full(48,1000/3600)}
        spatial={'units':[{'areaKm2':1.,'kind':'land'}],'lakeAreaKm2':1.}
        # One day of net inflow 1000 + lake P 1000 - E 200 = 1800 m3/h.
        levels=np.r_[np.full(24,100.),np.full(24,100.0432)]
        window={'id':'w','start':times[0],'end':times[-1],'method':'unit-test'}
        out=diagnose(times,source,np.arange(48),diag,states,initial,q,{'p':np.zeros(48)},np.full(48,1800),levels,np.arange(48)*1800,[window],parameters({'lakeCoefficient':1}),spatial,'source-separated',{'level':{'2024-01-31':100.,'2024-02-01':100.0432}})
        self.assertEqual([(m['month'],m['hours']) for m in out['monthly']],[('2024-01',24),('2024-02',24)])
        self.assertLess(out['catchmentBalanceMaxErrorM3'],1e-10)
        self.assertAlmostEqual(out['lakeWindows'][0]['residual']['unexplainedNetInputM3'],0.,places=6)


if __name__ == '__main__':unittest.main()
