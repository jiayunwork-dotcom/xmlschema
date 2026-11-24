"""Run the full memory matrix required by the task and emit a JSON report."""
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MEASURE = os.path.join(HERE, '..', 'measure')
REPORT = os.path.join(MEASURE, 'memory-report.json')

CONVERTERS = ['XMLSchema', 'Parker', 'BadgerFish', 'Columnar', 'JsonML']
SIZES = {'10k': 'catalog-10k.xml', '100k': 'catalog-100k.xml'}
XSD = 'catalog.xsd'
REPEATS = 1


def spawn(xml: str, xsd: str, converter: str, lazy: int) -> dict:
    cmd = [sys.executable, '-P', os.path.join(HERE, 'measure.py'),
           'worker', '--xml', xml, '--xsd', xsd,
           '--converter', converter, '--lazy', str(lazy)]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.path.join(HERE, '..', '..')
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd='/tmp')
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    report: dict = {'runs': [], 'deltas': {}}
    for lazy in (1, 2):
        for converter in CONVERTERS:
            peaks = {}
            times = {}
            for label, xml_name in SIZES.items():
                runs = []
                for _ in range(REPEATS):
                    r = spawn(os.path.join(MEASURE, xml_name),
                              os.path.join(MEASURE, XSD), converter, lazy)
                    runs.append(r)
                peaks[label] = statistics.median(
                    r['peak_bytes'] / 1024 / 1024 for r in runs)
                times[label] = statistics.median(
                    r['elapsed_s'] for r in runs)
                report['runs'].extend(
                    {'lazy': lazy, 'converter': converter, 'size': label,
                     **r} for r in runs)
            delta = peaks['100k'] - peaks['10k']
            report['deltas'][f'{converter}-lazy{lazy}'] = {
                'peak_10k_mib': round(peaks['10k'], 3),
                'peak_100k_mib': round(peaks['100k'], 3),
                'delta_mib': round(delta, 3),
                'time_10k_s': round(times['10k'], 3),
                'time_100k_s': round(times['100k'], 3),
                'within_8_mib': delta <= 8.0,
            }
    with open(REPORT, 'w', encoding='utf-8') as fp:
        json.dump(report, fp, indent=2)
    print(json.dumps(report['deltas'], indent=2))
    print('report written to', REPORT)


if __name__ == '__main__':
    main()
