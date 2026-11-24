"""Reproduce lazy to_json issues: compare full-load output vs lazy=1/2 for each converter."""
import io
import json
import os
import sys
import traceback

import xmlschema
from xmlschema import XMLResource, to_dict, to_json
from xmlschema.converters import (
    XMLSchemaConverter, ParkerConverter, BadgerFishConverter,
    ColumnarConverter, JsonMLConverter,
)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')

CONVERTERS = [
    ('XMLSchema', XMLSchemaConverter),
    ('Parker', ParkerConverter),
    ('BadgerFish', BadgerFishConverter),
    ('Columnar', ColumnarConverter),
    ('JsonML', JsonMLConverter),
]

CASES = [
    ('repeated', os.path.join(DATA, 'catalog.xsd'), os.path.join(DATA, 'repeated.xml')),
    ('single', os.path.join(DATA, 'catalog.xsd'), os.path.join(DATA, 'single.xml')),
    ('separated', os.path.join(DATA, 'interleave.xsd'), os.path.join(DATA, 'separated.xml')),
    ('nested', os.path.join(DATA, 'interleave.xsd'), os.path.join(DATA, 'nested.xml')),
]


def run():
    failures = 0
    for case_name, xsd_path, xml_path in CASES:
        schema = xmlschema.XMLSchema(xsd_path)
        print(f'\n===== case: {case_name} ({xml_path}) =====')
        for conv_name, conv_cls in CONVERTERS:
            # full load baseline through the same to_json entry point
            full_fp = io.StringIO()
            to_json(xml_path, fp=full_fp, schema=schema,
                    converter=conv_cls)
            full = json.loads(full_fp.getvalue())
            for lazy in (1, 2):
                try:
                    fp = io.StringIO()
                    result = to_json(
                        XMLResource(xml_path, lazy=lazy),
                        fp=fp,
                        schema=schema,
                        converter=conv_cls,
                    )
                    lazy_obj = json.loads(fp.getvalue())
                except Exception as exc:
                    failures += 1
                    print(f'  {conv_name:12s} lazy={lazy} EXC {type(exc).__name__}: {exc}')
                    tb = traceback.format_exc().splitlines()
                    print('    ' + tb[-2].strip())
                    continue

                if lazy_obj != full:
                    failures += 1
                    print(f'  {conv_name:12s} lazy={lazy} MISMATCH')
                    print(f'    full: {json.dumps(full, default=str)[:200]}')
                    print(f'    lazy: {json.dumps(lazy_obj, default=str)[:200]}')
                else:
                    print(f'  {conv_name:12s} lazy={lazy} ok')
    print(f'\n{failures} failure(s)')
    return failures


if __name__ == '__main__':
    sys.exit(1 if run() else 0)
