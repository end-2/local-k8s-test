#!/usr/bin/env python3
"""Validate benchmark range selection, raw exports and failure cleanup."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aiperf_runner', ROOT / 'scripts/run-transformers-api-aiperf.py')
runner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_module)
from aiperf_report import export_case, save_json, write_summary


def completed_pod():
    return {
        'metadata': {'name': 'case-pod', 'uid': 'pod-1'}, 'spec': {'nodeName': 'node-1'},
        'status': {
            'initContainerStatuses': [{'name': 'api', 'restartCount': 0, 'imageID': 'api-image',
                                      'state': {'terminated': {'exitCode': 0}}}],
            'containerStatuses': [{'name': 'aiperf', 'imageID': 'client-image',
                                   'state': {'terminated': {'exitCode': 0}}}],
        },
    }


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.report = Path(self.temporary.name)
        self.case = self.report / 'base/concurrency-1'
        self.raw = self.case / 'raw'
        self.raw.mkdir(parents=True)
        self.pod = completed_pod()
        self.expected = {'requests': 2, 'warmup': 1}
        save_json(self.raw / 'inputs.json', {'data': [
            {'session_id': 's1', 'payloads': [{'max_completion_tokens': 64}]},
            {'session_id': 's2', 'payloads': [{'max_completion_tokens': 128}]},
        ]})
        self.summary = {'error_summary': [], 'was_cancelled': False, 'request_count': {'avg': 2}}
        for key in ('time_to_first_token', 'inter_token_latency', 'request_latency', 'request_throughput',
                    'output_token_throughput', 'benchmark_duration', 'input_sequence_length', 'output_sequence_length'):
            self.summary[key] = {'avg': 20, 'p50': 20, 'p95': 30, 'p99': 40}
        save_json(self.raw / 'profile_export_aiperf.json', self.summary)
        self.records = []
        for i, (phase, session, target, ttft) in enumerate((('warmup', 's1', 64, 999), ('profiling', 's1', 64, 10), ('profiling', 's2', 128, 30))):
            self.records.append({
                'metadata': {'phase_kind': phase, 'session_num': i, 'conversation_id': session, 'x_request_id': str(i)},
                'metrics': {k: {'value': v} for k, v in {
                    'usage_prompt_tokens': 140 if target == 64 else 524, 'usage_completion_tokens': target,
                    'time_to_first_token': ttft, 'inter_token_latency': 3, 'request_latency': 100,
                    'decode_duration': 90, 'inter_chunk_latency': [2, 3, 4],
                }.items()},
            })
        self.write_records()

    def write_records(self):
        (self.raw / 'profile_export.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def export(self, previous=()):
        return export_case(self.case, 'base', 1, self.pod, self.expected, previous)

    def test_raw_is_unchanged_and_warmup_excluded_from_length_summary(self):
        original = (self.raw / 'profile_export.jsonl').read_bytes()
        result = self.export()
        write_summary(self.report, [result], 4)
        self.assertEqual(original, (self.raw / 'profile_export.jsonl').read_bytes())
        groups = json.loads((self.report / 'by-length.json').read_text())
        self.assertEqual([(g['requests'], g['ttft_avg_ms']) for g in groups], [(1, 10), (1, 30)])
        self.assertEqual(len((self.report / 'requests.csv').read_text().splitlines()), 4)

    def test_short_output_fails_but_preserves_request_export(self):
        self.records[-1]['metrics']['usage_completion_tokens']['value'] = 127
        self.write_records()
        with self.assertRaisesRegex(RuntimeError, 'per_request_output_matches_target'):
            self.export()
        self.assertTrue((self.case / 'requests.csv').exists())
        self.assertFalse((self.case / 'summary.json').exists())

    def test_missing_record_fails(self):
        self.records.pop()
        self.write_records()
        with self.assertRaisesRegex(RuntimeError, 'measured_record_count'):
            self.export()

    def test_missing_timing_metric_fails(self):
        del self.records[-1]['metrics']['time_to_first_token']
        self.write_records()
        with self.assertRaisesRegex(RuntimeError, 'request_metrics_present'):
            self.export()

    def test_api_restart_fails(self):
        self.pod['status']['initContainerStatuses'][0]['restartCount'] = 1
        with self.assertRaisesRegex(RuntimeError, 'api_never_restarted'):
            self.export()

    def test_client_error_or_cancellation_fails(self):
        for field, value in (('error_summary', [{'error': 'timeout'}]), ('was_cancelled', True)):
            with self.subTest(field=field):
                summary = {**self.summary, field: value}
                save_json(self.raw / 'profile_export_aiperf.json', summary)
                with self.assertRaises(RuntimeError):
                    self.export()

    def test_cross_case_dataset_node_and_image_invariants(self):
        previous = self.export()
        previous['pod_uid'] = 'older-pod'
        for field, value, failure in (
            ('dataset_sha256', 'changed', 'input_dataset_identical'),
            ('node', 'other-node', 'same_node'),
            ('api_image_id', 'changed', 'same_api_image_per_variant'),
            ('aiperf_image_id', 'changed', 'same_client_image'),
            ('pod_uid', 'pod-1', 'fresh_server_pod'),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, failure):
                    self.export([{**previous, field: value}])

    def test_other_variant_may_use_different_api_image(self):
        previous = self.export()
        previous.update(variant='enhanced', api_image_id='enhanced-image', pod_uid='previous-pod')
        self.export([previous])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.args = runner_module.parse_args(['--variant', 'base', '--max-concurrency', '2', '--cooldown-seconds', '0',
                                             '--output-dir', str(Path(self.temporary.name) / 'report')])
        self.runner = runner_module.Runner(self.args)
        self.runner.report.mkdir()
        self.runner.environment.mkdir()
        self.runner.node = 'node-1'
        self.runner.runtime = {'base': {'image_id': 'api-image'}, 'aiperf': {'image_id': 'client-image'}}
        self.runner.expected = {'requests': 2, 'warmup': 1}
        self.runner.manifests['base', 1] = {
            'metadata': {'name': 'case'},
            'spec': {'activeDeadlineSeconds': 7200, 'template': {'spec': {}}},
        }

    def test_range_is_inclusive_powers_of_two(self):
        self.assertEqual(runner_module.build_plan('enhanced', 8), [('enhanced', c) for c in (1, 2, 4, 8)])
        self.assertEqual(runner_module.build_plan('both', 2), [('base', 1), ('enhanced', 1), ('base', 2), ('enhanced', 2)])
        self.assertEqual(len(runner_module.build_plan('both', 128)), 16)

    def test_rejects_invalid_concurrency(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner_module.parse_args(['--variant', 'base', '--max-concurrency', '3'])

    def test_existing_report_is_never_overwritten(self):
        marker = self.runner.report / 'progress.json'
        marker.write_text('existing results')
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = runner_module.main(['--variant', 'base', '--max-concurrency', '2', '--output-dir', str(self.runner.report)])
        self.assertEqual(code, 1)
        self.assertEqual(marker.read_text(), 'existing results')

    def test_wrong_overlay_concurrency_stops_before_creating_resources(self):
        args = runner_module.parse_args(['--variant', 'base', '--max-concurrency', '2',
                                         '--output-dir', str(Path(self.temporary.name) / 'wrong-overlay')])
        runner = runner_module.Runner(args)
        manifest = {'metadata': {'name': 'case'}, 'spec': {'template': {'spec': {
            'containers': [{'name': 'aiperf', 'env': [{'name': 'CONCURRENCY', 'value': '1'}]}],
        }}}}
        with patch.object(runner, 'kube', return_value=json.dumps(manifest)) as kube, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'Overlay concurrency mismatch'):
                runner.prepare()
            self.assertTrue(all('--dry-run=client' in call.args for call in kube.call_args_list))

    def run_terminal_case(self, terminal='Complete', collection_errors=(), interrupt=False):
        calls = []

        def kube(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('get', 'job'):
                if interrupt:
                    raise KeyboardInterrupt()
                return json.dumps({'status': {'conditions': [{'type': terminal, 'status': 'True'}]}})
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [completed_pod()]})
            return ''

        with patch.object(self.runner, 'kube', side_effect=kube), \
                patch.object(self.runner, 'collect', return_value=(completed_pod(), collection_errors)) as collect, \
                patch.object(self.runner, 'progress'), patch.object(runner_module, 'export_case', return_value={'status': 'passed'}) as export:
            try:
                self.runner.run_case('base', 1)
            finally:
                self.assertEqual(collect.call_count, 1)
                self.assertTrue(any(call[:2] == ('delete', 'job') for call in calls))
                self.assertFalse(any(call[:2] == ('delete', 'namespace') for call in calls))
                self.assertEqual(export.call_count, 0 if terminal == 'Failed' or collection_errors or interrupt else 1)

    def test_success_collects_before_releasing_job(self):
        self.run_terminal_case()

    def test_failed_job_stops_and_releases_gpu(self):
        with self.assertRaisesRegex(RuntimeError, 'case failed'):
            self.run_terminal_case(terminal='Failed')

    def test_collection_failure_preserves_storage_and_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Result collection failed'):
            self.run_terminal_case(collection_errors=['raw artifacts: copy failed'])

    def test_interruption_collects_and_releases_gpu(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_terminal_case(interrupt=True)

    def test_foreign_namespace_is_never_deleted(self):
        self.runner.owns_namespace = True
        with patch.object(self.runner, 'kube', return_value=json.dumps({'metadata': {'labels': {'aiperf-run': 'foreign'}}})) as kube:
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                self.runner.cleanup()
            self.assertEqual(kube.call_count, 1)


if __name__ == '__main__':
    unittest.main()
