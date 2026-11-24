============================
Lazy streaming JSON decoding
============================

.. versionadded:: 4.3

The :func:`xmlschema.to_json` function decodes a lazy
:class:`xmlschema.XMLResource` straight into the output stream with bounded
memory. The previous lazy implementation relied on generator placeholders
that were expanded by a custom :class:`json.JSONEncoder`; it still retained
data proportional to the number of records, could raise ``AssertionError``
with :class:`xmlschema.BadgerFishConverter`, and produced output that
differed from a full load when same-name elements were separated by other
elements.

How it works
============

A single ``iterparse`` pass drives a push decoder
(:class:`~xmlschema.converters.streaming_decoder.StreamingJsonDecoder`) that
reuses the existing XSD machinery for attribute decoding, simple type
decoding, name mapping, identity counters and validation errors. Two frame
strategies coexist:

* **fast array frame** -- an element whose content model is a single
  repeating, non-mixed element particle renders each child into a JSON array
  as soon as the child ends. Peak memory is independent of the number of
  children. A fast frame can only start a contiguous *fast spine* from the
  document root, because bytes are emitted in stream order;
* **buffered frame** -- the decoded values of the element's direct children
  are kept until the element ends and then grouped with the exact rules of
  the paired converter. The number of buffered entries is bounded by the
  element's XSD content model, never by the total number of records.

Each supported converter has a paired streaming sink
(:mod:`xmlschema.converters.streaming`) that reproduces its key grouping,
scalar/list and wrapper conventions, so the produced JSON parses to the same
object as a full :func:`xmlschema.to_dict` followed by :func:`json.dumps`.

Same-name elements separated by other elements
==============================================

When same-name siblings are interleaved with other elements (for example
``a``, ``b``, ``a``) the converter must regroup all the ``a`` values under a
single key, which is only possible once the enclosing element ends. The
decoder therefore keeps such an enclosing frame buffered. This is the
content-model-aware strategy:

* elements that are a single repeating particle stream immediately (the
  common catalog/log case);
* elements with several child names, choice groups, or mixed content buffer
  until they close. Memory stays bounded by the number of distinct child
  names of one frame, but the first JSON byte of such a frame appears only
  when it ends rather than at its first child.

Inputs that make buffering grow toward the document size are therefore:
deeply nested or widely interleaved structures with no single repeating
spine, and documents whose repeating element is a direct child of a
multi-particle content model. Plain sequences of the same record type are
the best case and stay flat regardless of the record count.

Validation
==========

``validation='lax'`` collects errors and returns them with the result exactly
as the full-load path does; ``validation='strict'`` raises the first
:class:`xmlschema.XMLSchemaValidationError`. Write errors from the output
file-like object propagate to the caller and the decoder never creates
temporary files. A custom ``cls`` or ``default`` entry in *json_options*
selects the legacy generator-based encoder, whose hook-based behavior some
callers depend on.

Memory measurements
===================

The measurement scripts live in the repository ``scratch/scripts`` directory
and each run executes in a fresh interpreter with :mod:`tracemalloc`, writing
to a null stream so the JSON text is never retained::

    python scratch/scripts/gen_data.py scratch/measure 100000
    python scratch/scripts/memory_matrix.py
    python scratch/scripts/measure.py interleave \
        --xml scratch/measure/catalog-interleaved-100k.xml \
        --xsd scratch/measure/catalog-interleave.xsd

For a catalog of continuous ``item`` records the difference between the
10.000 and 100.000 record peaks is below 8 MiB for every supported converter
and both ``lazy=1`` and ``lazy=2``; on the reference measurement machine the
measured difference is 0.0 MiB with absolute peaks between roughly 0.7 and
1.1 MiB regardless of the record count. The full report is written to
``scratch/measure/memory-report.json``.

For the interleaved 100.000 document (same-name elements separated by
another element) the enclosing frame must buffer until it closes, so the
peak grows with the document size (on the order of hundreds of MiB for
100.000 interleaved records). This is the inherent cost of regrouping
separated siblings and is the input class the streaming design cannot make
flat without changing the required output shape.
