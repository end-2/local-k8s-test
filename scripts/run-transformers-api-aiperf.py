#!/usr/bin/env python3
"""Run isolated Transformers API/AIPerf Jobs and retain their raw results."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from aiperf_report import container_status, export_case, save_json, sha256, write_summary

ROOT = Path(__file__).resolve().parents[1]
CONCURRENCIES = (1, 2, 4, 8, 16, 32, 64, 128)
GPU_FIELDS = 'timestamp,uuid,name,driver_version,pstate,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit,clocks.current.sm,clocks.current.memory'
INSPECT_RUNTIME = '''import hashlib, importlib.metadata as m, json, pathlib, platform, subprocess
versions = {}
for name in ('torch', 'transformers', 'tokenizers', 'fastapi', 'uvicorn', 'pydantic', 'aiperf'):
    try: versions[name] = m.version(name)
    except m.PackageNotFoundError: pass
sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/app').glob('*.py')}
requirements = pathlib.Path('/app/api-requirements.txt')
if requirements.exists(): sources['requirements.txt'] = hashlib.sha256(requirements.read_bytes()).hexdigest()
cuda = None
if 'torch' in versions:
    import torch
    cuda = torch.version.cuda
check = subprocess.run(['python', '-m', 'pip', 'check'], capture_output=True, text=True)
print(json.dumps({'python': platform.python_version(), 'packages': versions, 'torch_cuda': cuda,
                 'source_sha256': sources, 'pip_check': check.stdout + check.stderr,
                 'pip_check_exit_code': check.returncode}))
raise SystemExit(check.returncode)
'''


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(args, data=None, timeout=90):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}")
    return result.stdout


def option(args, name):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f'Missing Job argument: {name}') from exc


def workload(manifest):
    client = next(c for c in manifest['spec']['template']['spec']['containers'] if c['name'] == 'aiperf')
    return {
        'requests': int(option(client['args'], '--request-count')),
        'warmup': int(option(client['args'], '--warmup-request-count')),
        'client_args': client['args'], 'client_image': client['image'],
        'client_environment': [e for e in client.get('env', []) if e['name'] not in ('CONCURRENCY', 'POD_NAME')],
        'client_resources': client.get('resources', {}),
    }


def build_plan(variant, maximum):
    variants = ('base', 'enhanced') if variant == 'both' else (variant,)
    return [(v, c) for c in CONCURRENCIES if c <= maximum for v in variants]


class Runner:
    def __init__(self, args):
        self.args = args
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.run_id = stamp.lower() + '-' + uuid.uuid4().hex[:6]
        default = ROOT / 'reports' / f'{stamp}-transformers-api-aiperf-{args.variant}-c{args.max_concurrency}'
        self.report = (args.output_dir or default).resolve()
        self.environment = self.report / 'environment'
        self.namespace = 'aiperf-' + self.run_id
        self.kube_command = [str(ROOT / 'scripts/local-k8s.sh'), 'kubectl', '--request-timeout=30s']
        self.plan = build_plan(args.variant, args.max_concurrency)
        self.manifests = {}
        self.results = []
        self.owns_namespace = False
        self.report_created = False
        self.runtime = {}
        self.node = None

    def kube(self, *args, data=None, timeout=90):
        return command(self.kube_command + ['-n', self.namespace] + list(args), data, timeout)

    def progress(self, status, **details):
        value = {'status': status, 'updated_at': now(), 'namespace': self.namespace,
                 'completed_cases': len(self.results), 'total_cases': len(self.plan), **details}
        save_json(self.report / 'progress.json', value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def gpu(self):
        if not shutil.which('nvidia-smi'):
            return ''
        try:
            return command(['nvidia-smi', '--query-gpu=' + GPU_FIELDS, '--format=csv,noheader,nounits'], timeout=15).strip()
        except (RuntimeError, subprocess.TimeoutExpired):
            return ''

    def prepare(self):
        # A new directory prevents accidental mixing or overwriting of measurements.
        self.report.mkdir(parents=True, exist_ok=False)
        self.report_created = True
        self.environment.mkdir()
        print(f'Report: {self.report}', flush=True)
        for variant, concurrency in self.plan:
            path = ROOT / f'config/benchmarks/transformers-api-aiperf/{variant}/concurrency-{concurrency}'
            manifest = json.loads(self.kube('create', '--dry-run=client', '-k', str(path), '-o', 'json'))
            manifest['metadata'].pop('namespace', None)
            client = next(c for c in manifest['spec']['template']['spec']['containers'] if c['name'] == 'aiperf')
            actual = next((e.get('value') for e in client.get('env', []) if e['name'] == 'CONCURRENCY'), None)
            if actual != str(concurrency):
                raise RuntimeError(f'Overlay concurrency mismatch: {variant}/concurrency-{concurrency} has {actual}')
            self.manifests[variant, concurrency] = manifest
            save_json(self.environment / 'manifests' / f'{variant}-c{concurrency}.json', manifest)
        workloads = [workload(m) for m in self.manifests.values()]
        if any(w != workloads[0] for w in workloads[1:]):
            raise RuntimeError('All cases must share the same client workload, image and resources')
        if workloads[0]['requests'] < 100:
            raise RuntimeError('The workload must contain at least 100 measured requests')
        self.expected = workloads[0]
        for variant in dict(self.plan):
            specs = [m['spec']['template']['spec'] for (v, c), m in self.manifests.items() if v == variant]
            reference = specs[0]
            for spec in specs[1:]:
                for key in ('initContainers', 'volumes', 'nodeSelector', 'securityContext'):
                    if spec.get(key) != reference.get(key):
                        raise RuntimeError(f'{variant} server environment differs between concurrency cases: {key}')
        save_json(self.environment / 'protocol.json', {
            'created_at': now(), 'namespace': self.namespace, 'variant': self.args.variant,
            'max_concurrency': self.args.max_concurrency,
            'order': [{'variant': v, 'concurrency': c} for v, c in self.plan],
            'workload': self.expected, 'pre_case_idle_seconds': self.args.cooldown_seconds,
            'gpu_sampling_seconds': 30, 'dry_run': self.args.dry_run,
        })
        shutil.copytree(ROOT / 'config/benchmarks/transformers-api-aiperf', self.environment / 'configuration')
        for variant in dict(self.plan):
            destination = self.environment / 'source' / variant
            destination.mkdir(parents=True)
            for source in (ROOT / f'inference/transformers-api-{variant}').iterdir():
                if source.is_file():
                    shutil.copy2(source, destination / source.name)
        shutil.copy2(Path(__file__), self.environment / 'run-transformers-api-aiperf.py')
        shutil.copy2(ROOT / 'scripts/lib/aiperf_report.py', self.environment / 'aiperf_report.py')
        (self.report / 'README.md').write_text(
            '# Transformers API와 AIPerf 측정\n\n'
            '진행 상태: [progress.json](progress.json), 측정 요약: [summary.md](summary.md).\n\n'
            '`<구현>/concurrency-<값>/raw/`에 원본 결과를 보관합니다. '
            '각 케이스의 `requests.csv`는 warmup과 본 측정의 요청별 TTFT, ITL, latency와 토큰 수를 포함합니다. '
            '시간 단위는 ms이며 `inter_chunk_latency_ms`는 스트리밍 청크 간 시간 배열입니다.\n\n'
            '`environment/`에는 실행 순서, 설정, 소스와 런타임 환경을 보관합니다. '
            '케이스별 `pod.json`, `environment.json`, `gpu.csv`에서 실제 이미지 ID, 노드, 종료 상태와 호스트 GPU 표본을 확인합니다. '
            '각 케이스는 새 서버에서 한 번 실행하며 GPU 온도와 호스트 파일 캐시는 달라질 수 있습니다.\n'
        )
        write_summary(self.report, [], len(self.plan))
        if self.args.dry_run:
            self.progress('dry_run')
            return
        save_json(self.environment / 'host.json', {
            'collected_at': now(), 'platform': platform.platform(), 'kernel': platform.release(),
            'cpu_count': os.cpu_count(),
            'cpu': command(['lscpu', '--json']) if shutil.which('lscpu') else None,
            'git_commit': command(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip(),
            'git_status': command(['git', '-C', str(ROOT), 'status', '--short']),
        })
        save_json(self.environment / 'nodes.json', json.loads(self.kube('get', 'nodes', '-o', 'json')))
        save_json(self.environment / 'kubernetes-version.json', json.loads(self.kube('version', '-o', 'json')))
        (self.environment / 'gpu-start.csv').write_text(GPU_FIELDS + '\n' + self.gpu() + '\n')
        self.kube('create', '-f', '-', data=json.dumps({
            'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': self.namespace, 'labels': {'aiperf-run': self.run_id}},
        }))
        self.owns_namespace = True
        self.kube('apply', '-f', str(ROOT / 'config/benchmarks/transformers-api-aiperf/results.yaml'))
        self.kube('wait', '--for=condition=Ready', 'pod/aiperf-results', '--timeout=120s', timeout=150)
        reader = json.loads(self.kube('get', 'pod', 'aiperf-results', '-o', 'json'))
        self.node = reader['spec']['nodeName']
        save_json(self.environment / 'results-pod.json', reader)
        self.inspect_images()

    def inspect_images(self):
        images = {}
        for variant, concurrency in self.plan:
            spec = self.manifests[variant, concurrency]['spec']['template']['spec']
            api = next(c for c in spec['initContainers'] if c['name'] == 'api')
            client = next(c for c in spec['containers'] if c['name'] == 'aiperf')
            for name, container in ((variant, api), ('aiperf', client)):
                if name in images and images[name] != container['image']:
                    raise RuntimeError(f'Image varies between cases: {name}')
                images[name] = container['image']
        checksums = ROOT / 'config/models/qwen3-0.6b.sha256'
        expected_model = dict(line.split(maxsplit=1)[::-1] for line in checksums.read_text().splitlines())
        model_script = '''
model_hashes = {}
for filename in MODEL_FILES:
    digest = hashlib.sha256()
    with (pathlib.Path('/model') / filename).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    model_hashes[filename] = digest.hexdigest()
print(json.dumps({'model_sha256': model_hashes}))
'''
        for name, image in images.items():
            self.progress('preflight', image=image)
            pod_name = 'inspect-' + name
            manifest = {
                'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': pod_name},
                'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                         'nodeSelector': {'kubernetes.io/hostname': self.node},
                         'containers': [{'name': 'inspect', 'image': image, 'imagePullPolicy': 'Never',
                                         'command': ['python', '-c', INSPECT_RUNTIME]}]},
            }
            if name != 'aiperf':
                source_spec = self.manifests[name, 1]['spec']['template']['spec']
                volume = next(v for v in source_spec['volumes'] if v['name'] == 'model')
                manifest['spec']['volumes'] = [volume]
                inspect = manifest['spec']['containers'][0]
                inspect['volumeMounts'] = [{'name': 'model', 'mountPath': '/model', 'readOnly': True}]
                inspect['command'][-1] = INSPECT_RUNTIME.replace('raise SystemExit(check.returncode)', '') + '\nMODEL_FILES = ' + repr(list(expected_model)) + model_script + '\nraise SystemExit(check.returncode)\n'
            self.kube('create', '-f', '-', data=json.dumps(manifest))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                pod = json.loads(self.kube('get', 'pod', pod_name, '-o', 'json'))
                phase = pod.get('status', {}).get('phase')
                if phase in ('Succeeded', 'Failed'):
                    break
                time.sleep(2)
            else:
                save_json(self.environment / f'{name}-preflight-pod.json', pod)
                raise TimeoutError(f'Image preflight timed out: {image}')
            output = self.kube('logs', pod_name)
            (self.environment / f'{name}-preflight.log').write_text(output)
            save_json(self.environment / f'{name}-preflight-pod.json', pod)
            if phase != 'Succeeded':
                raise RuntimeError(f'Image preflight failed: {image}; see {name}-preflight.log')
            lines = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
            runtime = lines[0]
            if name != 'aiperf':
                model_hashes = lines[-1]['model_sha256']
                save_json(self.environment / f'{name}-model.json', {'model': 'Qwen/Qwen3-0.6B', 'node': self.node, 'sha256': model_hashes})
                if model_hashes != expected_model:
                    raise RuntimeError('Mounted model checksums differ from config/models/qwen3-0.6b.sha256')
            runtime.update(image=image, image_id=container_status(pod, 'inspect')['imageID'], node=self.node)
            save_json(self.environment / f'{name}-runtime.json', runtime)
            self.runtime[name] = runtime
            if name != 'aiperf':
                sources = self.environment / 'source' / name
                expected = {p.name: sha256(p) for p in sources.iterdir() if p.suffix == '.py' or p.name == 'requirements.txt'}
                if runtime['source_sha256'] != expected:
                    raise RuntimeError(f'{name} image sources differ from this checkout. Rebuild and load the image.')
            self.kube('delete', 'pod', pod_name, '--wait=true', '--timeout=60s')

    def sample(self, case, variant, concurrency, pod):
        sample = self.gpu()
        if sample:
            with (case / 'gpu.csv').open('a') as stream:
                stream.write(sample + '\n')
        count = 0
        if pod:
            path = '/results/' + pod['metadata']['name'] + '/profile_export.jsonl'
            try:
                output = self.kube('exec', 'aiperf-results', '--', 'sh', '-c',
                                   'test ! -f "$1" || wc -l < "$1"', 'sh', path)
                count = int(output.strip() or '0')
            except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                pass
        self.progress('running', variant=variant, concurrency=concurrency,
                      exported_records_including_warmup=count,
                      pod_phase=pod.get('status', {}).get('phase') if pod else None)

    def collect(self, case, job, pod, started, variant, concurrency):
        errors = []

        def capture(label, action):
            try:
                return action()
            except Exception as exc:
                errors.append(f'{label}: {exc}')
                return None

        info = capture('Job', lambda: json.loads(self.kube('get', 'job', job, '-o', 'json')))
        if info:
            save_json(case / 'job.finished.json', info)
        if not pod:
            pods = capture('Pods', lambda: json.loads(self.kube('get', 'pods', '-l', f'batch.kubernetes.io/job-name={job}', '-o', 'json')))
            if pods and pods['items']:
                pod = pods['items'][0]
        if pod:
            pod = capture('Pod', lambda: json.loads(self.kube('get', 'pod', pod['metadata']['name'], '-o', 'json'))) or pod
            save_json(case / 'pod.json', pod)
            name = pod['metadata']['name']
            events = capture('events', lambda: json.loads(self.kube('get', 'events', '--field-selector', 'involvedObject.name=' + name, '-o', 'json')))
            if events:
                save_json(case / 'events.json', events)
            for container in ('api', 'aiperf'):
                output = capture(container + ' log', lambda: self.kube('logs', name, '-c', container, '--timestamps=true'))
                if output is not None:
                    (case / (container + '.log')).write_text(output)
            for attempt in range(3):
                try:
                    temporary = case / 'raw.partial'
                    shutil.rmtree(temporary, ignore_errors=True)
                    self.kube('cp', 'aiperf-results:/results/' + name, str(temporary), timeout=180)
                    temporary.rename(case / 'raw')
                    break
                except Exception as exc:
                    if attempt == 2:
                        errors.append('raw artifacts: ' + str(exc))
                    else:
                        time.sleep(2)
            save_json(case / 'environment.json', {
                'started_at': started, 'finished_at': now(), 'variant': variant, 'concurrency': concurrency,
                'node': pod['spec'].get('nodeName'), 'api': container_status(pod, 'api'),
                'client': container_status(pod, 'aiperf'), 'host_gpu_after': self.gpu(),
                'runtime_environment': f'../../environment/{variant}-runtime.json',
            })
        else:
            errors.append('Job did not create a Pod')
        save_json(case / 'collection.json', {'errors': errors})
        return pod, errors

    def run_case(self, variant, concurrency):
        case = self.report / variant / f'concurrency-{concurrency}'
        case.mkdir(parents=True)
        self.progress('cooldown', variant=variant, concurrency=concurrency)
        time.sleep(self.args.cooldown_seconds)
        manifest = self.manifests[variant, concurrency]
        # Keep all cases on the node holding the reader's ReadWriteOnce PVC.
        manifest['spec']['template']['spec'].setdefault('nodeSelector', {})['kubernetes.io/hostname'] = self.node
        save_json(case / 'job.request.json', manifest)
        job = manifest['metadata']['name']
        started = now()
        (case / 'gpu.csv').write_text(GPU_FIELDS + '\n')
        pod, failure = None, None
        try:
            self.kube('create', '-f', '-', data=json.dumps(manifest))
            deadline = time.monotonic() + manifest['spec']['activeDeadlineSeconds'] + 190
            last_sample = 0
            while time.monotonic() < deadline:
                info = json.loads(self.kube('get', 'job', job, '-o', 'json'))
                pods = json.loads(self.kube('get', 'pods', '-l', f'batch.kubernetes.io/job-name={job}', '-o', 'json'))['items']
                if len(pods) > 1:
                    raise RuntimeError('Multiple Pods for one case invalidate the measurement')
                if pods:
                    pod = pods[0]
                    if container_status(pod, 'api').get('restartCount', 0):
                        raise RuntimeError('API restarted during measurement')
                    for name in ('api', 'aiperf'):
                        status = container_status(pod, name)
                        if status.get('imageID') and status['imageID'] != self.runtime[variant if name == 'api' else name]['image_id']:
                            raise RuntimeError(f'{name} image changed after preflight')
                terminal = next((c['type'] for c in info.get('status', {}).get('conditions', [])
                                 if c['type'] in ('Complete', 'Failed') and c['status'] == 'True'), None)
                if terminal == 'Failed':
                    raise RuntimeError(f'{job} failed: {info["status"]}')
                if terminal == 'Complete':
                    break
                if time.monotonic() - last_sample >= 30:
                    self.sample(case, variant, concurrency, pod)
                    last_sample = time.monotonic()
                time.sleep(5)
            else:
                raise TimeoutError(f'{job} exceeded its deadline')
        except BaseException as exc:
            failure = exc
        finally:
            try:
                pod, errors = self.collect(case, job, pod, started, variant, concurrency)
                if errors and failure is None:
                    failure = RuntimeError('Result collection failed: ' + '; '.join(errors))
            except BaseException as exc:
                failure = failure or exc
            # Release the GPU even on interruption; retain the PVC for failed collections.
            try:
                self.kube('delete', 'job', job, '--ignore-not-found', '--cascade=foreground', '--wait=true', '--timeout=90s', timeout=120)
            except BaseException as exc:
                failure = failure or exc
        if failure:
            save_json(case / 'failure.json', {'error': str(failure), 'at': now()})
            raise failure
        return export_case(case, variant, concurrency, pod, self.expected, self.results)

    def cleanup(self):
        if self.owns_namespace:
            namespace = json.loads(self.kube('get', 'namespace', self.namespace, '-o', 'json'))
            if namespace['metadata'].get('labels', {}).get('aiperf-run') != self.run_id:
                raise RuntimeError('Namespace ownership changed; refusing cleanup')
            self.kube('delete', 'namespace', self.namespace, '--wait=true', '--timeout=120s', timeout=150)
            self.owns_namespace = False

    def run(self):
        self.prepare()
        if self.args.dry_run:
            return
        for variant, concurrency in self.plan:
            self.results.append(self.run_case(variant, concurrency))
            write_summary(self.report, self.results, len(self.plan))
            self.progress('case_complete', variant=variant, concurrency=concurrency)
        self.cleanup()
        self.progress('complete', measured_requests=sum(r['measured_requests'] for r in self.results),
                      warmup_requests=sum(r['warmup_requests'] for r in self.results))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('base', 'enhanced', 'both'), required=True,
                        help='API implementation; both alternates base/enhanced at each concurrency')
    parser.add_argument('--max-concurrency', type=int, choices=CONCURRENCIES, required=True,
                        help='Run powers of two from 1 through this value')
    parser.add_argument('--output-dir', type=Path, help='New report directory (default: reports/<UTC timestamp>-...)')
    parser.add_argument('--cooldown-seconds', type=int, default=20, help='Idle time before each new server (default: 20)')
    parser.add_argument('--dry-run', action='store_true', help='Save the plan and rendered Jobs without creating Kubernetes resources')
    args = parser.parse_args(argv)
    if args.cooldown_seconds < 0:
        parser.error('--cooldown-seconds must not be negative')
    return args


def main(argv=None):
    runner = Runner(parse_args(argv))

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Interrupted by signal {signum}')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        runner.run()
    except BaseException as exc:
        if runner.report_created:
            runner.progress('interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed', error=str(exc))
        print(f'Error: {exc}', file=sys.stderr)
        if runner.owns_namespace:
            print(f'Results storage retained in namespace {runner.namespace}. See {runner.report}/progress.json.', file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
