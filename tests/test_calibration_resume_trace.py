"""Targeted interruption recovery checks; all artifacts stay in an isolated build folder."""
import json
import unittest
import uuid
from copy import deepcopy
from pathlib import Path

import numpy as np

from backend.calibration_search import run_fit
from backend.common import ROOT, read_json, sha_file, write_json
from backend.dual_calibration import generated_candidates, select_candidates
from backend.jobs import prepare_calibration_resume_trace
from backend.model_core import DEFAULT_PARAMETERS
from backend.two_stage_calibration import defaults


class TraceResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / 'build' / ('接续尾行定向验收-' + uuid.uuid4().hex[:10])
        cls.root.mkdir()

    def fixture(self, suffix, content, fit='joint/training/flow', complete=False):
        task = self.root / suffix
        folder = task / fit
        folder.mkdir(parents=True)
        write_json(task / 'pipeline-state.json', {'fit': fit})
        trace = folder / 'search-trace.jsonl'
        trace.write_bytes(content)
        if complete:
            write_json(folder / 'fit-result.json', {'traceSHA256': sha_file(trace)})
        return task, trace

    def test_normal_completed_line_is_unchanged(self):
        task, trace = self.fixture('normal', b'{"evaluation":1}\n')
        before = sha_file(trace)
        self.assertIsNone(prepare_calibration_resume_trace(task))
        self.assertEqual(before, sha_file(trace))

    def test_complete_json_without_separator_gets_only_newline(self):
        content = '{"fit":"全部资料","evaluation":1}'.encode('utf-8')
        task, trace = self.fixture('no-separator', content)
        repair = prepare_calibration_resume_trace(task)
        self.assertEqual(trace.read_bytes(), content + b'\n')
        self.assertEqual(repair['originalBytes'], len(content))
        self.assertEqual(repair['traceFile'], 'joint/training/flow/search-trace.jsonl')
        self.assertIsNone(prepare_calibration_resume_trace(task))

    def test_legacy_fit_path_and_partial_json_keep_original_bytes(self):
        content = b'{"evaluation":1}\n{"evaluation":'
        task, trace = self.fixture('legacy', content, fit='forward')
        prepare_calibration_resume_trace(task)
        self.assertEqual(trace.read_bytes(), content + b'\n')
        self.assertEqual(json.loads(trace.read_bytes().splitlines()[0])['evaluation'], 1)

    def test_completed_fit_keeps_recorded_digest(self):
        task, trace = self.fixture('finished', b'{"evaluation":1}', complete=True)
        before = sha_file(trace)
        self.assertIsNone(prepare_calibration_resume_trace(task))
        self.assertEqual(before, sha_file(trace))
        self.assertEqual(read_json(trace.parent / 'fit-result.json')['traceSHA256'], before)

    def test_truncated_utf8_is_reported_without_mutating_file(self):
        content = b'{"evaluation":1}\n{"fit":"' + '全部'.encode('utf-8')[:-1]
        task, trace = self.fixture('utf8-fragment', content)
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            prepare_calibration_resume_trace(task)
        self.assertEqual(trace.read_bytes(), content)

    def test_missing_and_empty_trace_are_noops(self):
        folder = self.root / 'no-state'
        folder.mkdir()
        self.assertIsNone(prepare_calibration_resume_trace(folder))
        task, trace = self.fixture('empty', b'', fit='joint/all-data/flow')
        self.assertIsNone(prepare_calibration_resume_trace(task))
        self.assertEqual(trace.read_bytes(), b'')

    def test_search_resume_preserves_candidates_and_representatives(self):
        class Evaluator:
            profile = 'balanced'

            def mask(self, start, end):
                return np.ones(40, dtype=bool)

            def evidence(self, mask):
                return {'trainingPairs': 40, 'fixedTrainingDates': ['training'] * 40,
                        'fixedTrainingObservationSHA256': 'isolated-synthetic'}

            def objective(self, parameters, mask):
                score = ((parameters['WM'] - 190) / 100) ** 2 + ((parameters['KC'] - .8) / .5) ** 2 + .1
                return score, {'count': 40, 'nse': 1 - score}

            def assess(self, parameters, start, end):
                return {'count': 40, 'nse': 1 - ((parameters['WM'] - 190) / 100) ** 2 - .1}

        options = defaults()
        options.update(budgets={'runoff': 120, 'snow': 1, 'routing': 1, 'joint': 120},
                       allowExtension=False)
        keys, bounds = ['WM', 'KC'], {'WM': [40., 300.], 'KC': [.2, 1.5]}
        request = {'twoStage': options, 'search': {'keys': keys, 'bounds': bounds},
                   'schemeSnapshot': {'configurationSHA256': 'isolated-test'},
                   '_modelIdentity': 'isolated-test', 'inputId': 'synthetic'}

        def run(folder, progress=lambda *_: None):
            return run_fit(Evaluator(), request, folder, 'synthetic', '2024', '2025',
                           '2025', '2026', deepcopy(DEFAULT_PARAMETERS), progress, lambda: False)

        baseline_dir = self.root / 'integration-reference'
        baseline = run(baseline_dir)
        reference_lines = (baseline_dir / 'search-trace.jsonl').read_text(encoding='utf-8').splitlines()
        reference = generated_candidates(baseline_dir / 'search-trace.jsonl', keys, bounds)
        reference_chosen = select_candidates(reference, keys, bounds, DEFAULT_PARAMETERS, 80)
        ids = lambda rows: [row['candidateId'] for row in rows]
        for fit in ('joint/training/flow', 'all-data'):
            task = self.root / ('integration-' + fit.replace('/', '-'))
            folder = task / fit
            counter = [0]

            def stop(percent, message):
                counter[0] += 1
                if counter[0] == 35:
                    raise InterruptedError('isolated interruption')

            with self.assertRaises(InterruptedError):
                run(folder, stop)
            write_json(task / 'pipeline-state.json', {'fit': fit})
            checkpoint = read_json(folder / 'optimizer-state.json')
            trace = folder / 'search-trace.jsonl'
            with trace.open('ab') as stream:
                stream.write(reference_lines[checkpoint['evaluations']][:100].encode('utf-8'))
            original = trace.read_bytes()
            prepare_calibration_resume_trace(task)
            self.assertEqual(trace.read_bytes(), original + b'\n')
            resumed = run(folder)
            candidates = generated_candidates(trace, keys, bounds)
            selected = select_candidates(candidates, keys, bounds, DEFAULT_PARAMETERS, 80)
            self.assertEqual(resumed['parameters'], baseline['parameters'])
            self.assertEqual(resumed['evaluations'], baseline['evaluations'])
            self.assertEqual(ids(candidates), ids(reference))
            self.assertEqual(ids(selected), ids(reference_chosen))


if __name__ == '__main__':
    unittest.main()
