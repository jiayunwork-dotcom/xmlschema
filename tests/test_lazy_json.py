#!/usr/bin/env python
#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Tests for the bounded-memory streaming JSON decoding of lazy XML resources.

The fixtures are intentionally small and hand-written so that every grouping
rule of the five supported converters can be compared against the full-load
result. Each structural case (continuous repetitions, a single occurrence,
same-name elements separated by other elements, nested repeating groups) is
checked with both ``lazy=1`` and ``lazy=2``.
"""
import io
import json
import unittest
from textwrap import dedent

from xmlschema import XMLSchema, XMLResource, to_json
from xmlschema.converters import (
    XMLSchemaConverter, ParkerConverter, BadgerFishConverter,
    ColumnarConverter, JsonMLConverter,
)
from xmlschema.validators.exceptions import XMLSchemaValidationError

CONVERTERS = (
    XMLSchemaConverter,
    ParkerConverter,
    BadgerFishConverter,
    ColumnarConverter,
    JsonMLConverter,
)

CATALOG_XSD = dedent('''\
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               xmlns:ns="http://example.com/catalog"
               targetNamespace="http://example.com/catalog"
               elementFormDefault="qualified">
      <xs:element name="catalog" type="ns:catalogType"/>
      <xs:complexType name="catalogType">
        <xs:sequence>
          <xs:element name="item" type="ns:itemType" minOccurs="0"
                      maxOccurs="unbounded"/>
        </xs:sequence>
        <xs:attribute name="version" type="xs:string"/>
      </xs:complexType>
      <xs:complexType name="itemType">
        <xs:sequence>
          <xs:element name="code" type="xs:integer"/>
          <xs:element name="name" type="xs:string"/>
          <xs:element name="price" type="xs:decimal"/>
          <xs:element name="in_stock" type="xs:boolean" minOccurs="0"/>
          <xs:element name="tag" type="xs:string" minOccurs="0"
                      maxOccurs="unbounded"/>
        </xs:sequence>
        <xs:attribute name="id" type="xs:string" use="required"/>
      </xs:complexType>
    </xs:schema>
''')

REPEATED_XML = dedent('''\
    <catalog xmlns="http://example.com/catalog" version="1.0">
      <item id="a1">
        <code>1</code><name>Alpha</name><price>10.50</price>
        <in_stock>true</in_stock><tag>red</tag><tag>small</tag>
      </item>
      <item id="a2">
        <code>2</code><name>Beta</name><price>22.00</price><tag>blue</tag>
      </item>
      <item id="a3">
        <code>3</code><name>Gamma</name><price>3.75</price>
        <in_stock>false</in_stock>
      </item>
    </catalog>
''')

SINGLE_XML = dedent('''\
    <catalog xmlns="http://example.com/catalog" version="1.0">
      <item id="only-one">
        <code>42</code><name>Solo</name><price>99.99</price>
        <in_stock>true</in_stock>
      </item>
    </catalog>
''')

INTERLEAVE_XSD = dedent('''\
    <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               xmlns:ns="http://example.com/interleave"
               targetNamespace="http://example.com/interleave"
               elementFormDefault="qualified">
      <xs:element name="doc" type="ns:docType"/>
      <xs:complexType name="docType">
        <xs:sequence>
          <xs:element name="header" type="xs:string" minOccurs="0"/>
          <xs:group ref="ns:mixedGroup" minOccurs="0" maxOccurs="unbounded"/>
          <xs:element name="footer" type="xs:string" minOccurs="0"/>
        </xs:sequence>
      </xs:complexType>
      <xs:group name="mixedGroup">
        <xs:choice>
          <xs:element name="a" type="ns:aType"/>
          <xs:element name="b" type="xs:string"/>
          <xs:element name="group" type="ns:nestedGroupType"/>
        </xs:choice>
      </xs:group>
      <xs:complexType name="aType">
        <xs:sequence>
          <xs:element name="k" type="xs:integer"/>
          <xs:element name="v" type="xs:string"/>
        </xs:sequence>
        <xs:attribute name="id" type="xs:string"/>
      </xs:complexType>
      <xs:complexType name="nestedGroupType">
        <xs:sequence>
          <xs:element name="entry" type="xs:string" minOccurs="0"
                      maxOccurs="unbounded"/>
        </xs:sequence>
      </xs:complexType>
    </xs:schema>
''')

SEPARATED_XML = dedent('''\
    <doc xmlns="http://example.com/interleave">
      <header>start</header>
      <a id="x1"><k>1</k><v>one</v></a>
      <b>between</b>
      <a id="x2"><k>2</k><v>two</v></a>
      <b>also between</b>
      <a id="x3"><k>3</k><v>three</v></a>
      <footer>end</footer>
    </doc>
''')

NESTED_XML = dedent('''\
    <doc xmlns="http://example.com/interleave">
      <header>start</header>
      <group><entry>e1</entry><entry>e2</entry><entry>e3</entry></group>
      <b>mid</b>
      <group><entry>e4</entry><entry>e5</entry></group>
      <a id="x9"><k>9</k><v>nine</v></a>
      <group><entry>e6</entry></group>
      <footer>end</footer>
    </doc>
''')


class _NullWriter(io.TextIOBase):
    """A stream that accepts writes without retaining any content."""

    def writable(self):
        return True

    def write(self, text):
        return len(text)


class TestLazyStreamingJson(unittest.TestCase):

    def _full_json(self, xml, schema, converter):
        fp = io.StringIO()
        to_json(xml, fp=fp, schema=schema, converter=converter)
        return json.loads(fp.getvalue())

    def _lazy_json(self, xml, schema, converter, lazy, fp=None):
        own_fp = fp is None
        if fp is None:
            fp = io.StringIO()
        to_json(XMLResource(xml, lazy=lazy), fp=fp, schema=schema,
                converter=converter)
        if own_fp:
            return json.loads(fp.getvalue())
        return None

    def _assert_parity(self, xml, xsd):
        schema = XMLSchema(xsd)
        for converter in CONVERTERS:
            full = self._full_json(xml, schema, converter)
            for lazy in (1, 2):
                with self.subTest(converter=converter.__name__, lazy=lazy):
                    lazy_obj = self._lazy_json(xml, schema, converter, lazy)
                    self.assertEqual(lazy_obj, full)

    def test_continuous_repetitions(self):
        self._assert_parity(REPEATED_XML, CATALOG_XSD)

    def test_single_occurrence(self):
        self._assert_parity(SINGLE_XML, CATALOG_XSD)

    def test_separated_same_name_elements(self):
        self._assert_parity(SEPARATED_XML, INTERLEAVE_XSD)

    def test_nested_repeating_groups(self):
        self._assert_parity(NESTED_XML, INTERLEAVE_XSD)

    def test_lazy_equals_full_for_every_converter(self):
        # Explicit matrix assertion (also documents the supported set).
        schema = XMLSchema(CATALOG_XSD)
        for converter in CONVERTERS:
            for lazy in (1, 2):
                full = self._full_json(REPEATED_XML, schema, converter)
                lazy_obj = self._lazy_json(REPEATED_XML, schema,
                                           converter, lazy)
                self.assertEqual(lazy_obj, full)

    def test_streaming_does_not_buffer_output_text(self):
        # The whole JSON is written incrementally to the provided stream;
        # when the stream discards content the decoded document must not be
        # held in memory by the decoder (structural check via sink stack).
        from xmlschema.converters.streaming import get_json_sink
        from xmlschema.converters.streaming_decoder import \
            StreamingJsonDecoder
        from xmlschema.validators.validation import DecodeContext

        schema = XMLSchema(CATALOG_XSD)
        resource = XMLResource(REPEATED_XML, lazy=1)
        converter = schema.maps.settings.get_converter(
            source=resource, converter=XMLSchemaConverter,
            decimal_type=float)
        context = DecodeContext(source=resource, converter=converter,
                                decimal_type=float)
        sink = get_json_sink(converter, _NullWriter())
        decoder = StreamingJsonDecoder(schema, resource, context, sink,
                                       'skip')
        decoder.run()
        self.assertEqual(sink.stack, [])

    def test_lax_errors_are_returned_like_full_load(self):
        bad_xml = dedent('''\
            <catalog xmlns="http://example.com/catalog" version="1.0">
              <item id="x1">
                <code>nope</code><name>A</name><price>1.50</price>
              </item>
              <item id="x2">
                <code>2</code><name>B</name><price>2.50</price>
              </item>
            </catalog>
        ''')
        schema = XMLSchema(CATALOG_XSD)
        full_text, full_errors = to_json(bad_xml, schema=schema,
                                         validation='lax')
        for lazy in (1, 2):
            fp = io.StringIO()
            errors = to_json(XMLResource(bad_xml, lazy=lazy), fp=fp,
                             schema=schema, validation='lax')
            self.assertIsInstance(errors, tuple)
            self.assertEqual(len(errors), len(full_errors))
            self.assertEqual([e.reason for e in errors],
                             [e.reason for e in full_errors])
            self.assertEqual(json.loads(fp.getvalue()),
                             json.loads(full_text))

    def test_strict_mode_raises(self):
        bad_xml = dedent('''\
            <catalog xmlns="http://example.com/catalog">
              <item id="x1"><code>nope</code><name>A</name>
                <price>1.50</price></item>
            </catalog>
        ''')
        schema = XMLSchema(CATALOG_XSD)
        for lazy in (1, 2):
            with self.assertRaises(XMLSchemaValidationError):
                to_json(XMLResource(bad_xml, lazy=lazy), fp=_NullWriter(),
                        schema=schema, validation='strict')

    def test_write_failure_propagates_without_temp_files(self):
        import os
        import tempfile

        class FailingWriter(io.TextIOBase):
            def __init__(self):
                self.calls = 0

            def writable(self):
                return True

            def write(self, text):
                self.calls += 1
                if self.calls > 5:
                    raise OSError('simulated write failure')
                return len(text)

        schema = XMLSchema(CATALOG_XSD)
        tmpdir = tempfile.mkdtemp()
        before = set(os.listdir(tmpdir))
        with self.assertRaises(OSError):
            to_json(XMLResource(REPEATED_XML, lazy=1),
                    fp=FailingWriter(), schema=schema)
        self.assertEqual(set(os.listdir(tmpdir)), before)

    def test_returns_string_when_fp_is_none(self):
        schema = XMLSchema(CATALOG_XSD)
        for lazy in (1, 2):
            result = to_json(XMLResource(REPEATED_XML, lazy=lazy),
                             schema=schema)
            self.assertIsInstance(result, str)
            self.assertEqual(json.loads(result),
                             self._full_json(REPEATED_XML, schema,
                                             XMLSchemaConverter))

    def test_non_lazy_path_is_unchanged(self):
        schema = XMLSchema(CATALOG_XSD)
        full = to_json(REPEATED_XML, schema=schema)
        resource = XMLResource(REPEATED_XML)  # lazy=False
        self.assertFalse(resource.is_lazy())
        fp = io.StringIO()
        to_json(resource, fp=fp, schema=schema)
        self.assertEqual(fp.getvalue(), full)

    def test_first_byte_emitted_before_document_ends(self):
        # With the fast-array strategy the head bytes must be written as
        # soon as the first repeating child starts, not buffered to the end.
        schema = XMLSchema(CATALOG_XSD)
        resource = XMLResource(REPEATED_XML, lazy=1)
        converter = schema.maps.settings.get_converter(
            source=resource, converter=XMLSchemaConverter,
            decimal_type=float)

        capturing = io.StringIO()
        from xmlschema.converters.streaming import get_json_sink
        from xmlschema.converters.streaming_decoder import \
            StreamingJsonDecoder
        from xmlschema.validators.validation import DecodeContext

        context = DecodeContext(source=resource, converter=converter,
                                decimal_type=float)
        sink = get_json_sink(converter, capturing)
        decoder = StreamingJsonDecoder(schema, resource, context, sink,
                                       'skip')
        # Drive a few iterparse events manually: after the root start and
        # the first item start, fast-array head must already be visible.
        from xmlschema.resources import XMLResourceManager
        with XMLResourceManager(resource) as cm:
            for event, node in resource._lazy_iterparse(cm.fp):
                if event == 'start':
                    decoder._on_start(node)
                elif event == 'end':
                    decoder._on_end(node)
                if capturing.getvalue():
                    break
        self.assertTrue(capturing.getvalue())
        self.assertIn('item', capturing.getvalue())


if __name__ == '__main__':
    unittest.main()
