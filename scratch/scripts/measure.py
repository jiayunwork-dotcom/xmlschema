"""
Bounded-memory measurement for lazy streaming to_json.

Each measurement runs in a fresh Python process (this script re-executes
itself with ``--worker``) so allocations from one run cannot affect the
next. The output file-like object discards everything written to it, so
the JSON text itself never accumulates in memory, and no temporary files
are created.

Usage:
    python measure.py bench <xml> <xsd> [--converter NAME] [--lazy N]
    python measure.py timing <xml> <xsd> [--converter NAME] [--lazy N]
    python measure.py interleave <xml> <xsd>
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..'))

import xmlschema  # noqa: E402
from xmlschema import XMLResource, to_json  # noqa: E402

CONVERTERS = {
    'XMLSchema': xmlschema.XMLSchemaConverter,
    'Parker': xmlschema.ParkerConverter,
    'BadgerFish': xmlschema.BadgerFishConverter,
    'Columnar': xmlschema.ColumnarConverter,
    'JsonML': xmlschema.JsonMLConverter,
}


class NullWriter(io.TextIOBase):
    """A text stream that accepts writes but never retains the content."""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def run_worker(xml_path: str, xsd_path: str, converter: str,
               lazy: int) -> tuple[int, float]:
    schema = xmlschema.XMLSchema(xsd_path)
    conv_cls = CONVERTERS[converter]
    resource = XMLResource(xml_path, lazy=lazy)
    fp = NullWriter()

    tracemalloc.start()
    t0 = time.perf_counter()
    to_json(resource, fp=fp, schema=schema, converter=conv_cls)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak, elapsed


def main_worker(args: argparse.Namespace) -> None:
    peak, elapsed = run_worker(args.xml, args.xsd, args.converter, args.lazy)
    print(json.dumps({'peak_bytes': peak, 'elapsed_s': elapsed,
                      'pid': os.getpid()}))


def spawn(xml: str, xsd: str, converter: str, lazy: int) -> dict:
    """Run one measurement in a brand-new interpreter process."""
    cmd = [sys.executable, '-P', os.path.abspath(__file__), 'worker',
           '--xml', xml, '--xsd', xsd, '--converter', converter,
           '--lazy', str(lazy)]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..')
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd='/tmp')
    if proc.returncode != 0:
        raise RuntimeError(f'worker failed:\n{proc.stderr}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


def mib(num_bytes: int) -> float:
    return num_bytes / 1024 / 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('bench', 'timing', 'interleave',
                                         'worker'))
    parser.add_argument('--xml')
    parser.add_argument('--xsd')
    parser.add_argument('--converter', default='XMLSchema')
    parser.add_argument('--lazy', type=int, default=1)
    args = parser.parse_args()

    if args.mode == 'worker':
        main_worker(args)
        return

    if args.mode == 'interleave':
        # Interleaved same-name elements: the bounded strategy must buffer
        # until groups can be assembled; measure all five converters.
        rows = []
        for name in CONVERTERS:
            r = spawn(args.xml, args.xsd, name, args.lazy)
            rows.append((name, r))
        print(f"interleaved document: {args.xml}")
        for name, r in rows:
            print(f"  {name:10s} peak={mib(r['peak_bytes']):7.2f} MiB "
                  f"time={r['elapsed_s']:6.2f}s")
        return

    r = spawn(args.xml, args.xsd, args.converter, args.lazy)
    print(json.dumps({
        'mode': args.mode,
        'xml': os.path.basename(args.xml),
        'converter': args.converter,
        'lazy': args.lazy,
        'peak_mib': round(mib(r['peak_bytes']), 3),
        'elapsed_s': round(r['elapsed_s'], 3),
        'pid': r['pid'],
    }, indent=2))


if __name__ == '__main__':
    main()
