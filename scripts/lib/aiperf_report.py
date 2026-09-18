"""Export AIPerf request records without changing the original artifacts."""

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def write_csv(path, rows):
    if not rows:
        return
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def container_status(pod, name):
    status = pod.get('status', {})
    containers = status.get('initContainerStatuses', []) + status.get('containerStatuses', [])
    return next((item for item in containers if item['name'] == name), {})


def export_case(case, variant, concurrency, pod, expected, previous):
    raw = case / 'raw'
    summary = json.loads((raw / 'profile_export_aiperf.json').read_text())
    records = [json.loads(line) for line in (raw / 'profile_export.jsonl').read_text().splitlines() if line.strip()]
    inputs = json.loads((raw / 'inputs.json').read_text())
    targets = {item['session_id']: item['payloads'][0]['max_completion_tokens'] for item in inputs['data']}
    measured = [r for r in records if r['metadata']['phase_kind'] == 'profiling']
    warmup = [r for r in records if r['metadata']['phase_kind'] == 'warmup']
    dataset_hash = sha256(raw / 'inputs.json')
    api, client = container_status(pod, 'api'), container_status(pod, 'aiperf')

    def stat(name, statistic='avg'):
        return summary.get(name, {}).get(statistic)

    rows = []
    for record in records:
        metadata, metrics = record['metadata'], record.get('metrics', {})

        def value(name):
            return metrics.get(name, {}).get('value')

        rows.append({
            'variant': variant, 'concurrency': concurrency,
            'phase': metadata['phase_kind'], 'session_num': metadata.get('session_num'),
            'conversation_id': metadata.get('conversation_id'),
            'request_id': metadata.get('x_request_id'),
            'request_start_ns': metadata.get('request_start_ns'),
            'request_end_ns': metadata.get('request_end_ns'),
            'input_tokens': value('usage_prompt_tokens'),
            'target_output_tokens': targets.get(metadata.get('conversation_id')),
            'output_tokens': value('usage_completion_tokens'),
            'ttft_ms': value('time_to_first_token'), 'itl_ms': value('inter_token_latency'),
            'latency_ms': value('request_latency'), 'decode_duration_ms': value('decode_duration'),
            'inter_chunk_latency_ms': json.dumps(value('inter_chunk_latency')),
        })
    write_csv(case / 'requests.csv', rows)
    checks = {
        'summary_request_count': stat('request_count') == expected['requests'],
        'measured_record_count': len(measured) == expected['requests'],
        'warmup_record_count': len(warmup) == expected['warmup'],
        'no_errors': not summary.get('error_summary'),
        'not_cancelled': summary.get('was_cancelled') is False,
        'api_never_restarted': api.get('restartCount') == 0,
        'client_exit_zero': client.get('state', {}).get('terminated', {}).get('exitCode') == 0,
        'server_terminated': 'terminated' in api.get('state', {}),
        'per_request_output_matches_target': bool(rows) and all(
            row['target_output_tokens'] is not None and row['output_tokens'] == row['target_output_tokens']
            for row in rows
        ),
        'request_metrics_present': all(
            row[key] is not None for row in rows for key in ('ttft_ms', 'itl_ms', 'latency_ms')
        ),
        'input_dataset_identical': all(r['dataset_sha256'] == dataset_hash for r in previous),
        'same_node': all(r['node'] == pod['spec']['nodeName'] for r in previous),
        'fresh_server_pod': all(r['pod_uid'] != pod['metadata']['uid'] for r in previous),
        'same_api_image_per_variant': all(
            r['api_image_id'] == api.get('imageID') for r in previous if r['variant'] == variant
        ),
        'same_client_image': all(r['aiperf_image_id'] == client.get('imageID') for r in previous),
    }
    save_json(case / 'validation.json', checks)
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Invalid measurement in {case}: {', '.join(failed)}")
    result = {
        'variant': variant, 'concurrency': concurrency, 'status': 'passed',
        'measured_requests': len(measured), 'warmup_requests': len(warmup), 'errors': 0,
    }
    for output, source, stats in (
        ('ttft', 'time_to_first_token', ('avg', 'p50', 'p95', 'p99')),
        ('itl', 'inter_token_latency', ('avg', 'p95')),
        ('latency', 'request_latency', ('avg', 'p50', 'p95', 'p99')),
    ):
        for statistic in stats:
            result[f'{output}_{statistic}_ms'] = stat(source, statistic)
    result.update({
        'request_throughput_rps': stat('request_throughput'),
        'output_token_throughput_tps': stat('output_token_throughput'),
        'benchmark_duration_s': stat('benchmark_duration'),
        'input_tokens_avg': stat('input_sequence_length'), 'output_tokens_avg': stat('output_sequence_length'),
        'dataset_sha256': dataset_hash, 'pod': pod['metadata']['name'], 'pod_uid': pod['metadata']['uid'],
        'node': pod['spec']['nodeName'], 'api_image_id': api['imageID'], 'aiperf_image_id': client['imageID'],
    })
    save_json(case / 'summary.json', result)
    return result


def quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def write_summary(report, results, total):
    save_json(report / 'summary.json', results)
    write_csv(report / 'summary.csv', results)
    requests, groups = [], []
    for result in results:
        case = report / result['variant'] / f"concurrency-{result['concurrency']}"
        with (case / 'requests.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        requests.extend(rows)
        lengths = defaultdict(list)
        for row in rows:
            if row['phase'] == 'profiling':
                lengths[int(row['target_output_tokens'])].append(row)
        for target, subset in sorted(lengths.items()):
            group = {
                'variant': result['variant'], 'concurrency': result['concurrency'],
                'target_output_tokens': target, 'requests': len(subset),
                'input_tokens_min': min(int(r['input_tokens']) for r in subset),
                'input_tokens_max': max(int(r['input_tokens']) for r in subset),
            }
            for metric in ('ttft', 'itl', 'latency'):
                values = [float(r[metric + '_ms']) for r in subset]
                group[metric + '_avg_ms'] = mean(values)
                for label, fraction in (('p50', .5), ('p95', .95), ('p99', .99)):
                    group[f'{metric}_{label}_ms'] = quantile(values, fraction)
            groups.append(group)
    write_csv(report / 'requests.csv', requests)
    write_csv(report / 'by-length.csv', groups)
    save_json(report / 'by-length.json', groups)
    comparison = []
    for concurrency in sorted({r['concurrency'] for r in results}):
        pair = {r['variant']: r for r in results if r['concurrency'] == concurrency}
        if set(pair) == {'base', 'enhanced'}:
            base, enhanced = pair['base'], pair['enhanced']
            row = {'concurrency': concurrency}
            for metric in ('output_token_throughput_tps', 'ttft_avg_ms', 'itl_avg_ms', 'latency_avg_ms'):
                for variant, result in pair.items():
                    row[f'{variant}_{metric}'] = result[metric]
            row['throughput_ratio'] = enhanced['output_token_throughput_tps'] / base['output_token_throughput_tps']
            comparison.append(row)
    write_csv(report / 'comparison.csv', comparison)
    lines = [
        '# 측정 결과', '', f'완료된 케이스: {len(results)}/{total}. warmup은 요약에서 제외합니다.', '',
        '| 구현 | Concurrency | 요청 수 | TTFT 평균 (ms) | ITL 평균 (ms) | Latency 평균 (ms) | Latency p95 (ms) | 출력 tokens/s |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for row in results:
        values = [row['variant'], str(row['concurrency']), str(row['measured_requests'])]
        values.extend(f'{row[key]:,.3f}' for key in (
            'ttft_avg_ms', 'itl_avg_ms', 'latency_avg_ms', 'latency_p95_ms', 'output_token_throughput_tps'
        ))
        lines.append('| ' + ' | '.join(values) + ' |')
    lines.extend(['', '원본 요약: [summary.csv](summary.csv), 요청별 지표: [requests.csv](requests.csv), 출력 길이별 통계: [by-length.csv](by-length.csv).', ''])
    (report / 'summary.md').write_text('\n'.join(lines))
