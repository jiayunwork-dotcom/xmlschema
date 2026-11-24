#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Push-style streaming decoder for lazy XML resources.

The standard decode path builds the whole decoded Python object (and thus the
whole JSON document) in memory. This module drives a single ``iterparse``
pass over a lazy :class:`xmlschema.XMLResource` and pushes decoded fragments
into a :class:`~xmlschema.converters.streaming.JsonStreamSink`.

The decoder reuses the existing XSD machinery for everything that can be done
on an element once its subtree is complete (attribute decoding, simple type
decoding, converter name mapping, identity counters and error collection),
while replacing the in-memory *grouping* step with incremental,
memory-bounded logic:

* elements whose content model is a single repeating child particle are
  decoded in *fast array* mode, emitting each child into a JSON array as
  soon as it ends -- peak memory is independent of the number of children;
* all other elements buffer only the decoded values of their direct
  children, whose distinct names are bounded by the XSD content model;
* elements without element children (leaf and simple-content elements) are
  decoded with the ordinary :meth:`XsdElement.raw_decode`, guaranteeing
  identical values and identical validation behavior.
"""
from typing import Any, Optional, TYPE_CHECKING

from xml.etree.ElementTree import Element

from xmlschema.converters import ElementData
from xmlschema.converters.streaming import JsonFrame, JsonStreamSink
import xmlschema.names as nm
from xmlschema.names import XSI_TYPE
from xmlschema.validators.validation import DecodeContext
from xmlschema.validators.exceptions import XMLSchemaValidationError

if TYPE_CHECKING:
    from xmlschema.resources import XMLResource
    from xmlschema.validators import XMLSchemaBase, XsdElement, XsdGroup, XsdType

__all__ = ('StreamingJsonDecoder',)


class _EngineFrame:
    """Engine-side state for one in-flight XML element."""

    __slots__ = ('elem', 'xsd_element', 'xsd_type', 'level', 'sink_frame',
                 'model', 'leaf', 'model_group', 'fast_name',
                 'deferred_name', 'deferred_mapped', 'pending_first',
                 'child_count', 'attributes', 'xmlns',
                 'skipped', 'cdata', 'cdata_index', 'ordered')

    def __init__(self, elem: Element, xsd_element: 'XsdElement',
                 xsd_type: 'XsdType', level: int,
                 sink_frame: JsonFrame, xmlns: Any) -> None:
        self.elem = elem
        self.xsd_element = xsd_element
        self.xsd_type = xsd_type
        self.level = level
        self.sink_frame = sink_frame
        self.model: Optional[Any] = None
        self.leaf = True
        self.model_group: Optional['XsdGroup'] = xsd_type.model_group
        # fast-path state
        self.fast_name: Optional[str] = None
        # deferred fast array: a repeating single-particle frame buffers its
        # first child and only starts streaming once a second child proves
        # the group is multi-valued (so single-record scalar rules survive).
        self.deferred_name: Optional[str] = None
        self.deferred_mapped: Optional[str] = None
        self.pending_first: Any = None
        self.child_count = 0
        self.attributes: Optional[list[tuple[str, Any]]] = None
        self.xmlns = xmlns
        self.skipped = False
        # ordered mixed-content character parts: (index, value)
        self.cdata: list[tuple[int, Any]] = []
        self.cdata_index = 1
        # document-order sequence of ('cdata', index, value) and
        # ('child', mapped_name, value, xsd_child) entries
        self.ordered: list[tuple] = []


class StreamingJsonDecoder:
    """
    Incremental decoder binding a lazy XML resource, a decode context, a
    schema and a streaming JSON sink.

    :param validation: the XSD validation mode, ``'strict'``, ``'lax'`` or
        ``'skip'``. In ``'lax'`` mode validation errors are collected into
        ``context.errors``; in ``'strict'`` mode the first error is raised.
    """

    def __init__(self, schema: 'XMLSchemaBase', resource: 'XMLResource',
                 context: DecodeContext, sink: JsonStreamSink,
                 validation: str = 'strict') -> None:
        self.schema = schema
        self.resource = resource
        self.context = context
        self.sink = sink
        self.validation = validation
        self.namespaces = context.namespaces
        self.frames: list[_EngineFrame] = []

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        resource = self.resource
        from xmlschema.resources import XMLResourceManager
        with XMLResourceManager(resource) as cm:
            for event, node in resource._lazy_iterparse(cm.fp):
                if event == 'start':
                    if not callable(node.tag):
                        self._on_start(node)
                elif event == 'end':
                    if not callable(node.tag):
                        self._on_end(node)
                # comment/PI events are ignored, matching XsdGroup.raw_decode
        self.sink.close()

    # ------------------------------------------------------------- events

    def _on_start(self, elem: Element) -> None:
        context = self.context
        parent_frame = self.frames[-1] if self.frames else None

        if parent_frame is None:
            xsd_element = self._match_root(elem)
        else:
            # Promote a deferred parent before the second child's frame is
            # created, so the child renders directly into the open array.
            self._maybe_promote(parent_frame, elem)
            xsd_element = self._match_child(parent_frame, elem)
            if not parent_frame.skipped:
                parent_frame.leaf = False
                parent_frame.child_count += 1

        if xsd_element is None:
            # Unknown element: standard path discards it unless keep_unknown.
            self._handle_unknown(elem, parent_frame)
            return

        xsd_type = self._resolve_type(xsd_element, elem)
        level = len(self.frames)
        context.elem = elem
        xmlns = context.converter.set_xmlns_context(elem, level)

        data = ElementData(elem.tag, None, None, None, xmlns)
        sink_frame = self.sink.start_element(xsd_element, xsd_type, level, data)
        frame = _EngineFrame(elem, xsd_element, xsd_type, level, sink_frame,
                             xmlns)
        self.frames.append(frame)

        for identity in xsd_element.identities:
            if identity in context.identities:
                context.identities[identity].reset(elem)
            else:
                context.identities[identity] = identity.get_counter(elem)

        if frame.model_group is not None:
            frame.model = frame.model_group.get_model_visitor()

        # Decode attributes at start so a fast-array head can include them
        # before any child bytes are emitted.
        if frame.model_group is not None:
            frame.attributes = self._decode_attributes(frame)
            frame.sink_frame.data = ElementData(
                elem.tag, None, None, frame.attributes, xmlns
            )

    def _on_end(self, elem: Element) -> None:
        if not self.frames or self.frames[-1].elem is not elem:
            # End of an unknown subtree whose opening could not be matched:
            # nothing to decode, just release it.
            self._clear_elem(elem)
            return

        frame = self.frames.pop()
        context = self.context
        context.elem = elem
        context.level = frame.level

        if frame.skipped:
            self._clear_elem(elem)
            return

        if frame.leaf or frame.model_group is None:
            self._decode_leaf(frame)
        else:
            self._decode_complex(frame)

        for identity in frame.xsd_element.identities:
            context.identities[identity].enabled = False

        self._clear_elem(elem)

    def _collect_tail(self, parent_frame: '_EngineFrame',
                      child: Element) -> None:
        """Append a child's tail as the next mixed-content cdata part."""
        group = parent_frame.model_group
        if group is None or not group.mixed:
            return
        tail = child.tail
        if tail is None:
            return
        if not self.context.preserve_mixed:
            tail = str(tail.strip())
        if not tail:
            return
        index = parent_frame.cdata_index
        parent_frame.cdata_index += 1
        parent_frame.cdata.append((index, tail))
        parent_frame.ordered.append(('cdata', index, tail))

    def _build_cdata(self, frame: '_EngineFrame',
                     text: Optional[str]) -> list[tuple[int, Any]]:
        """Assemble the final cdata list, leading text as part #1."""
        if text is None:
            return list(frame.cdata)
        parts = [(1, text)]
        # Tail parts were numbered starting at 1; shift them past the lead.
        parts.extend((idx + 1, value) for idx, value in frame.cdata)
        return parts

    def _build_ordered(self, frame: '_EngineFrame',
                       text: Optional[str]) -> list[tuple]:
        """Assemble the document-order sequence including the leading text."""
        if text is None:
            return list(frame.ordered)
        sequence: list[tuple] = [('cdata', 1, text)]
        cdata_offsets = {idx: idx + 1 for idx, _ in frame.cdata}
        for entry in frame.ordered:
            if entry[0] == 'cdata':
                _, idx, value = entry
                sequence.append(('cdata', cdata_offsets[idx], value))
            else:
                sequence.append(entry)
        return sequence

    # ----------------------------------------------------- element decode

    def _decode_leaf(self, frame: _EngineFrame) -> None:
        """
        Decode simple/empty/leaf elements.

        Validation runs through the same simple-type ``raw_decode`` (and the
        attribute group decoder) used by :meth:`XsdElement.raw_decode`, so
        validation errors, facet processing, fillers and value hooks behave
        exactly like the full-load path. The typed text is fed to the sink
        as a raw part so each sink applies its own leaf conventions.
        """
        attributes = frame.attributes
        if attributes is None:
            attributes = self._decode_attributes(frame)
            frame.attributes = attributes

        if frame.model_group is None:
            # Simple/simple-content element: decode the typed text through
            # the XSD type's own raw_decode (no converter shaping).
            text = self._decode_simple_text(frame)
        else:
            # empty complex element: no character content
            text = None

        frame.sink_frame.data = ElementData(
            frame.elem.tag, text, None, attributes, frame.xmlns
        )
        frame.sink_frame.raw_text = text
        frame.sink_frame.is_leaf = True

        rendered = self.sink.end_element()
        self._hand_up(frame, rendered)

    def _decode_simple_text(self, frame: _EngineFrame) -> Any:
        """
        Decode the character text of a simple or simple-content element
        through its XSD type, applying the same post-processing rules as
        :meth:`XsdElement.raw_decode`.
        """
        import decimal
        from elementpath.datatypes import AbstractDateTime, Duration

        context = self.context
        xsd_element = frame.xsd_element
        elem = frame.elem

        text = elem.text
        if not text and xsd_element.default is not None \
                and context.use_defaults:
            text = xsd_element.default

        # A simple-content complex type (including xsi:type overrides) needs
        # the element-level raw_decode to reach its embedded simple type; a
        # plain simple type decodes directly.
        if frame.xsd_type.is_complex():
            return self._decode_simple_content(frame, text)

        # Use the simple type's own raw_decode so validation errors and the
        # error->None (or filler) behavior match XsdElement.raw_decode.
        # Validation errors are intentionally not caught here (strict mode
        # must propagate; lax mode collects them into the context).
        result = xsd_element.type.raw_decode(text or '', self.validation,
                                             context)

        if result is None and context.filler is not None:
            return context.filler(xsd_element)
        if context.value_hook is not None:
            return context.value_hook(result, xsd_element.type)
        if isinstance(result, context.keep_datatypes) or result is None:
            return result
        if isinstance(result, str):
            if result[:1] == '{' and xsd_element.type.is_qname():
                return text
            return result
        if isinstance(result, decimal.Decimal):
            if context.decimal_type is not None:
                return context.decimal_type(result)
            return result
        if isinstance(result, (AbstractDateTime, Duration)):
            return str(result) if text is None else text.strip()
        if isinstance(result, bool):
            return result
        return str(result)

    def _decode_simple_content(self, frame: '_EngineFrame',
                               text: Optional[str]) -> Any:
        """
        Decode a simple-content complex element (including xsi:type
        overrides). The embedded simple type performs the actual decoding and
        validation once, matching :meth:`XsdElement.raw_decode` without
        double-counting errors.
        """
        context = self.context
        simple_type = frame.xsd_type.simple_type
        if simple_type is None:
            # Defensive fallback for unusual complex type arrangements.
            return text
        return simple_type.raw_decode(text or '', self.validation, context)

    def _decode_complex(self, frame: _EngineFrame) -> None:
        context = self.context
        elem = frame.elem

        if frame.model is not None:
            for particle, occurs, expected in frame.model.stop():
                context.children_validation_error(
                    self.validation, frame.model_group, elem,
                    frame.child_count, particle, occurs, expected
                )

        attributes = frame.attributes
        if attributes is None:
            attributes = self._decode_attributes(frame)
            frame.attributes = attributes

        # Leading text for mixed content (element-only models ignore it,
        # exactly as the converters do). It is cdata part #1 and precedes
        # every element child in the document-order sequence.
        text = None
        if frame.model_group is not None and frame.model_group.mixed \
                and elem.text is not None and elem.text.strip():
            text = str(elem.text.strip())

        frame.sink_frame.data = ElementData(
            elem.tag, text, None, attributes, frame.xmlns
        )
        frame.sink_frame.cdata = self._build_cdata(frame, text)
        frame.sink_frame.ordered = self._build_ordered(frame, text)

        # Children were already pushed (buffer or fast array). The sink now
        # renders the frame's JSON following its converter rules and returns
        # its in-memory marker for buffered parent grouping.
        value = self.sink.end_element()
        self._hand_up(frame, value)

    def _hand_up(self, frame: _EngineFrame, value: Any) -> None:
        if not self.frames:
            return
        parent = self.frames[-1]
        if parent.skipped:
            return
        mapped = self.context.converter.map_qname(frame.xsd_element.name)

        if parent.sink_frame.fast_path:
            # Streaming array: the child's bytes (and separator) were already
            # emitted by the sink's direct-render path. Only mixed-content
            # tail handling remains.
            self._collect_tail(parent, frame.elem)
            return

        if parent.deferred_name is not None and \
                mapped == parent.deferred_mapped and \
                parent.pending_first is None:
            # First occurrence of a repeating particle: buffer one value and
            # wait to see whether a second occurrence arrives.
            parent.pending_first = value
            self.sink.buffer_child(parent.sink_frame, mapped, value,
                                   frame.xsd_element)
            parent.ordered.append(
                ('child', mapped, value, frame.xsd_element)
            )
            self._collect_tail(parent, frame.elem)
            return

        self.sink.buffer_child(parent.sink_frame, mapped, value,
                               frame.xsd_element)
        parent.ordered.append(
            ('child', mapped, value, frame.xsd_element)
        )
        # The child's tail follows the child in document order.
        self._collect_tail(parent, frame.elem)

    # ------------------------------------------------------------ matching

    def _match_root(self, elem: Element) -> Optional['XsdElement']:
        xsd_element = self.schema.get_element(elem.tag, None, self.namespaces)
        if xsd_element is None and XSI_TYPE in elem.attrib:
            xsd_element = self.schema.builders.create_element(elem.tag,
                                                              self.schema)
        return xsd_element

    def _match_child(self, parent_frame: _EngineFrame,
                     elem: Element) -> Optional['XsdElement']:
        context = self.context
        model = parent_frame.model
        tag = elem.tag

        # Arm the fast array when the first element child starts.
        if parent_frame.child_count == 0 and not parent_frame.sink_frame.fast_path:
            self._arm_fast_path(parent_frame, elem)

        if model is not None and model.element is not None:
            while model.element is not None:
                xsd_element = model.match_element(tag)
                if xsd_element is None:
                    for particle, occurs, expected in model.advance(False):
                        context.children_validation_error(
                            self.validation, parent_frame.model_group,
                            parent_frame.elem, parent_frame.child_count,
                            particle, occurs, expected
                        )
                        model.clear()
                        xsd_element = parent_frame.model_group.match_element(tag)
                        break
                    else:
                        continue
                    break

                try:
                    parent_frame.model_group.check_dynamic_context(
                        elem, xsd_element, model.element, context.namespaces
                    )
                except (XMLSchemaValidationError, TypeError) as err:
                    context.validation_error(
                        self.validation, parent_frame.model_group, err,
                        parent_frame.elem
                    )

                for particle, occurs, expected in model.advance(True):
                    context.children_validation_error(
                        self.validation, parent_frame.model_group,
                        parent_frame.elem, parent_frame.child_count,
                        particle, occurs, expected
                    )
                break
            else:
                xsd_element = parent_frame.model_group.match_element(tag)
                if xsd_element is None:
                    context.children_validation_error(
                        self.validation, parent_frame.model_group,
                        parent_frame.elem, parent_frame.child_count,
                        parent_frame.model_group, 0, None
                    )
            return xsd_element

        if parent_frame.model_group is None:
            return None
        xsd_element = parent_frame.model_group.match_element(tag)
        if xsd_element is None:
            context.missing_element_error(
                self.validation, self.schema, elem, None, None
            )
        return xsd_element

    def _arm_fast_path(self, parent_frame: _EngineFrame,
                       first_child_elem: Element) -> None:
        """
        Arm a *deferred* fast array when the content model is a single
        repeating element particle.

        A repeating particle can yield either a scalar (a single occurrence)
        or a list (several occurrences), and several converters shape those
        two cases differently. The frame therefore buffers its first child
        only; when a second child arrives :meth:`_promote_fast_array` starts
        streaming and flushes the buffered first value. This keeps the peak
        extra memory at one record regardless of the total record count.

        Fast frames form a contiguous spine from the document root: a
        buffered frame has not emitted its head yet, so streaming a
        descendant's bytes directly would corrupt the output ordering.
        """
        # The fast spine starts at the document root and every ancestor must
        # already be a streaming fast frame. A deferred or buffered ancestor
        # has not emitted its head yet, so streaming a descendant's bytes
        # directly would corrupt the output ordering.
        if self.frames and any(
                not f.sink_frame.fast_path for f in self.frames[:-1]):
            return

        model_group = parent_frame.model_group
        if model_group is None:
            return
        # Mixed content interleaves character data between the children, so a
        # pure streaming array cannot represent it; keep such frames buffered.
        if model_group.mixed:
            return
        particles = [p for p in model_group.iter_elements()]
        if len(particles) != 1:
            return
        particle = particles[0]
        if particle.is_single():
            # maxOccurs == 1: the scalar-vs-list choice needs full-frame
            # knowledge, so keep buffering.
            return
        if particle.match(first_child_elem.tag) is None:
            return

        mapped = self.context.converter.map_qname(particle.name)
        parent_frame.deferred_name = particle.name
        parent_frame.deferred_mapped = mapped

    def _maybe_promote(self, parent_frame: '_EngineFrame',
                       elem: Element) -> None:
        """Promote a deferred frame when a second matching child starts."""
        if parent_frame.deferred_name is None:
            return
        mapped = parent_frame.deferred_mapped
        actual = self.context.converter.map_qname(elem.tag)
        if actual != mapped:
            return
        if parent_frame.pending_first is None:
            return
        self._promote_fast_array(parent_frame)

    def _promote_fast_array(self, parent_frame: '_EngineFrame') -> None:
        """Promote a deferred frame to streaming once a second child arrives."""
        mapped = parent_frame.deferred_mapped
        parent_frame.fast_name = parent_frame.deferred_name
        parent_frame.deferred_name = None
        parent_frame.deferred_mapped = None
        self.sink.begin_fast_array(parent_frame.sink_frame, mapped)
        # Flush the buffered first value as the initial array item.
        if parent_frame.pending_first is not None:
            self.sink.fast_array_item(
                parent_frame.sink_frame,
                self._unwrap_first(parent_frame.pending_first)
            )
            parent_frame.pending_first = None

    def _unwrap_first(self, value: Any) -> Any:
        """
        Adapt the buffered first marker to the shape the fast array expects.

        BadgerFish stores full ``{tag: inner}`` objects in its markers but
        streams the unwrapped inner object; other sinks stream the value
        unchanged.
        """
        from xmlschema.converters.streaming import BadgerFishJsonSink
        from collections.abc import MutableMapping
        if isinstance(self.sink, BadgerFishJsonSink) \
                and isinstance(value, MutableMapping) and len(value) == 1:
            return next(iter(value.values()))
        return value

    def _resolve_type(self, xsd_element: 'XsdElement',
                      elem: Element) -> 'XsdType':
        context = self.context
        if not xsd_element.alternatives:
            xsd_type = xsd_element.type
        else:
            xsd_type = xsd_element.get_alternative_type(elem,
                                                         context.inherited)

        if nm.XSI_TYPE in elem.attrib and \
                xsd_element.schema.meta_schema is not None:
            type_name = elem.attrib[nm.XSI_TYPE].strip()
            try:
                xsd_type = xsd_element.maps.get_instance_type(
                    type_name, xsd_type, context.namespaces
                )
            except (KeyError, TypeError) as err:
                context.validation_error(
                    self.validation, xsd_element, err, elem
                )
        return xsd_type

    def _handle_unknown(self, elem: Element,
                        parent_frame: Optional[_EngineFrame]) -> None:
        context = self.context
        if parent_frame is not None and context.keep_unknown:
            value = self.schema.maps.any_type.raw_decode(
                elem, self.validation, context
            )
            mapped = context.converter.map_qname(elem.tag)
            if not parent_frame.sink_frame.fast_path:
                self.sink.buffer_child(parent_frame.sink_frame, mapped, value,
                                       None)
            return
        context.missing_element_error(
            self.validation, self.schema, elem, None, None
        )

    # ------------------------------------------------------------ helpers

    def _decode_attributes(self, frame: _EngineFrame) \
            -> Optional[list[tuple[str, Any]]]:
        context = self.context
        attribute_group = frame.xsd_element.get_attributes(frame.xsd_type)
        context.level = frame.level
        return attribute_group.raw_decode(
            frame.elem.attrib, self.validation, context
        )

    def _clear_elem(self, elem: Element) -> None:
        """
        Release a completed subtree without retaining any per-node bookkeeping.

        The default :meth:`XMLResource._clear` keeps the element itself and
        only drops its children, while the resource's ``_nsmaps`` /
        ``_xmlns`` dictionaries would otherwise keep one entry per parsed
        node for the whole run (the observed record-count-scaled memory
        growth). Here the whole subtree, including *elem*, is removed from
        those maps and the children are detached.
        """
        resource = self.resource
        nsmaps = resource._nsmaps
        xmlns = resource._xmlns
        for node in elem.iter():
            if node in xmlns:
                del xmlns[node]
            if node in nsmaps:
                del nsmaps[node]
        del elem[:]

        # Detach completed siblings from their parent so the ElementTree
        # structure itself cannot accumulate parsed subtrees.
        parent = self._parent_of(elem)
        if parent is not None:
            for index, child in enumerate(list(parent)):
                if child is elem:
                    del parent[:index + 1]
                    break

    def _parent_of(self, elem: Element) -> Optional[Element]:
        for frame in reversed(self.frames):
            if frame.elem is not elem and len(frame.elem):
                # frame element is still an ancestor in the parsed tree
                return frame.elem
        return self.resource.root if self.resource.root is not elem else None
