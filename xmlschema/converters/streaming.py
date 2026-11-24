#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
"""
Streaming JSON sinks for lazy decoding.

A *sink* is a push-style JSON writer used by the lazy streaming decoder. It
receives, frame by frame, the same information that a converter's
:meth:`element_decode` receives (mapped child entries, decoded attributes,
text and xmlns declarations) but renders JSON incrementally, without ever
building the full decoded document.

Each sink reproduces exactly the key grouping, scalar/list and wrapper
conventions of its paired converter, so the JSON produced by a lazy
:func:`xmlschema.to_json` call parses to the same object obtained from a full
:func:`xmlschema.to_dict` followed by :func:`json.dumps`.

Rendering model
---------------

Two kinds of frames coexist and can only nest along a contiguous *fast
spine* starting at the document root:

* **fast-array frame** -- the element's content model is a single repeating
  child particle. Its children render their own JSON straight into the
  output stream inside a JSON array; peak memory is independent of the
  number of children. A frame can switch to fast mode only when every
  ancestor is itself a fast-array frame (or the root), because bytes are
  emitted in stream order;
* **buffered frame** -- the decoded values of its direct children are kept
  in memory until the element ends, then rendered once following the
  converter grouping rules. The number of buffered entries is bounded by
  the element's XSD content model, never by the total number of records.
"""
import json
from collections.abc import MutableMapping, MutableSequence
from typing import Any, IO, Optional, TYPE_CHECKING

from xmlschema.names import XSD_ANY_TYPE

if TYPE_CHECKING:
    from xmlschema.converters.base import ElementData, XMLSchemaConverter
    from xmlschema.validators import XsdElement, XsdType

__all__ = ('JsonFrame', 'JsonStreamSink', 'XMLSchemaJsonSink', 'ParkerJsonSink',
           'BadgerFishJsonSink', 'ColumnarJsonSink', 'JsonMLJsonSink',
           'get_json_sink')


class JsonFrame:
    """Per-element state of a streaming JSON sink."""

    __slots__ = ('xsd_element', 'xsd_type', 'level', 'data', 'children',
                 'fast_name', 'fast_count', 'marker', 'is_leaf', 'raw_text',
                 'self_value', 'parent_marker', 'cdata', 'ordered',
                 '_columnar_root_wrap')

    def __init__(self, xsd_element: 'XsdElement', xsd_type: 'XsdType',
                 level: int, data: 'ElementData') -> None:
        self.xsd_element = xsd_element
        self.xsd_type = xsd_type
        self.level = level
        # ElementData assembled by the engine (content filled lazily)
        self.data = data
        # buffered child entries (mapped_name, decoded_value, xsd_child)
        self.children: list[tuple[str, Any, Optional['XsdElement']]] = []
        # fast-array mode
        self.fast_name: Optional[str] = None
        self.fast_count = 0
        # decoded value produced at frame close, used by buffered parents
        self.marker: Any = None
        # set by the engine for leaf/simple elements
        self.is_leaf = False
        self.raw_text: Any = None
        # the value the frame renders about itself
        self.self_value: Any = None
        # the value a buffered parent must group (unwrapped leaf values)
        self.parent_marker: Any = None
        # mixed-content parts and the document-order child/cdata sequence
        self.cdata: list[tuple[int, Any]] = []
        self.ordered: list[tuple] = []
        # Columnar fast-array root wrapper flag
        self._columnar_root_wrap = False

    @property
    def fast_path(self) -> bool:
        return self.fast_name is not None


class JsonStreamSink:
    """
    Base push JSON writer. A sink owns the output stream and a JSON encoder.
    """

    def __init__(self, fp: IO[str], converter: 'XMLSchemaConverter',
                 json_options: Optional[dict[str, Any]] = None) -> None:
        self.fp = fp
        self.converter = converter
        json_options = dict(json_options or ())
        # The streaming sink replaces the encoder hooks used by the old lazy
        # generator-based path.
        json_options.pop('cls', None)
        self.encoder = json.JSONEncoder(**json_options)
        # Mirror the separator spacing chosen by json.dump/json.dumps so the
        # hand-written structural punctuation matches a full-load encoding.
        self.item_sep = self.encoder.item_separator
        self.key_sep = self.encoder.key_separator
        self.stack: list[JsonFrame] = []

    # -- low level helpers --------------------------------------------------

    def _write(self, fragment: str) -> None:
        self.fp.write(fragment)

    def _enc(self, value: Any) -> str:
        return self.encoder.encode(value)

    def _key(self, key: str, value: Any) -> str:
        """A ``"key": value`` pair honoring the configured key separator."""
        return f'{self._enc(key)}{self.key_sep}{self._enc(value)}'

    def _direct(self) -> bool:
        """
        A popped frame renders straight to the stream when it is the document
        root or its parent is a fast-array frame. Otherwise its rendering is
        captured as an in-memory marker for the buffered parent. A deferred
        (lookahead) parent has not emitted its head yet, so its children are
        buffered rather than written.
        """
        if not self.stack:
            return True
        parent = self.stack[-1]
        if parent.fast_path:
            return True
        return False

    # -- lifecycle ----------------------------------------------------------

    def start_element(self, xsd_element: 'XsdElement', xsd_type: 'XsdType',
                      level: int, data: 'ElementData') -> JsonFrame:
        frame = JsonFrame(xsd_element, xsd_type, level, data)
        self.stack.append(frame)
        return frame

    def begin_fast_array(self, frame: JsonFrame, mapped_name: str) -> None:
        """Switch *frame* to streaming mode for a repeating child."""
        raise NotImplementedError

    def fast_array_item(self, frame: JsonFrame, value: Any) -> None:
        """
        Append an already-decoded child value to an open fast array. Used
        when a deferred frame is promoted to streaming, to flush its small
        buffered lookahead. Separators are managed here.
        """
        if frame.fast_count:
            self._write(self.item_sep)
        self._write(self._enc(value))
        frame.fast_count += 1

    def _fast_separator(self) -> None:
        """
        Emit the array separator before a directly-rendered child frame.
        The closing frame is still on top of the stack, so its parent is the
        next frame down.
        """
        if len(self.stack) < 2:
            return
        parent = self.stack[-2]
        if parent.fast_path:
            if parent.fast_count:
                self._write(self.item_sep)
            parent.fast_count += 1

    def buffer_child(self, frame: JsonFrame, mapped_name: str, value: Any,
                     xsd_child: Optional['XsdElement']) -> None:
        frame.children.append((mapped_name, value, xsd_child))
    def end_element(self) -> Any:
        """Close the top frame and return its decoded marker value."""
        raise NotImplementedError

    def close(self) -> None:
        if self.stack:
            raise RuntimeError(
                f"unclosed frames at sink close: {len(self.stack)}"
            )
        flush = getattr(self.fp, 'flush', None)
        if flush is not None:
            flush()

    # -- shared helpers -----------------------------------------------------

    def _emit(self, frame: JsonFrame, fragment: str,
              self_value: Any, parent_marker: Any = None) -> Any:
        """
        Write *fragment* for direct frames. Sets the frame's *self_value*
        (what it renders) and *parent_marker* (what a buffered parent groups;
        defaults to *self_value*). Returns the marker that the engine hands
        to the parent.
        """
        if self._direct():
            self._write(fragment)
        frame.self_value = self_value
        frame.parent_marker = self_value if parent_marker is None \
            else parent_marker
        frame.marker = frame.parent_marker
        return frame.parent_marker

    @staticmethod
    def _entries_for(frame: JsonFrame, name: str) -> list[Any]:
        return [v for n, v, _ in frame.children if n == name]

    @staticmethod
    def _child_for(frame: JsonFrame, name: str) -> Optional['XsdElement']:
        for n, _v, child in frame.children:
            if n == name:
                return child
        return None

    @staticmethod
    def _ordered_names(frame: JsonFrame) -> list[str]:
        return list(dict.fromkeys(n for n, _, _ in frame.children))


class XMLSchemaJsonSink(JsonStreamSink):
    """Streaming counterpart of :class:`xmlschema.XMLSchemaConverter`."""

    def begin_fast_array(self, frame, mapped_name):
        frame.fast_name = mapped_name
        head = self._head_fragment(frame)
        self._write(head + self._enc(mapped_name) + self.key_sep + '[')

    def _head_dict(self, frame: JsonFrame) -> dict[str, Any]:
        """Ordered xmlns/attribute head entries of *frame*."""
        converter = self.converter
        head: dict[str, Any] = {}
        xmlns = converter.get_effective_xmlns(
            frame.data.xmlns, frame.level, frame.xsd_element
        )
        if converter._use_namespaces and xmlns:
            for k, v in xmlns:
                key = f'{converter.ns_prefix}:{k}' if k else converter.ns_prefix
                head[key] = v
        if frame.data.attributes:
            for name, value in converter.map_attributes(frame.data.attributes):
                head[name] = value
        return head

    def _head_fragment(self, frame: JsonFrame) -> str:
        head = self._head_dict(frame)
        if not head:
            return '{'
        return '{' + self.item_sep.join(
            self._enc(k) + self.key_sep + self._enc(v) for k, v in head.items()
        ) + self.item_sep

    def _keep_result_dict(self, frame: JsonFrame) -> bool:
        converter = self.converter
        data = frame.data
        xmlns = converter.get_effective_xmlns(
            data.xmlns, frame.level, frame.xsd_element
        )
        if data.attributes or converter.force_dict and frame.xsd_type.is_complex():
            return True
        elif not xmlns or not converter._use_namespaces:
            return False
        from xmlschema.utils.qnames import get_namespace
        namespace = get_namespace(frame.xsd_element.name)
        if any(x[1] == namespace for x in xmlns):
            return True
        if frame.xsd_type.is_qname() and isinstance(data.text, str):
            try:
                prefix = data.text.split(':')[0]
            except IndexError:
                prefix = ''
            if any(x[0] == prefix for x in xmlns):
                return True
        return False

    def end_element(self) -> Any:
        self._fast_separator()
        frame = self.stack.pop()
        converter = self.converter
        data = frame.data

        if frame.fast_path:
            self._write(']}')
            frame.marker = None
            return frame.marker

        if frame.is_leaf:
            return self._end_leaf(frame)

        xsd_group = frame.xsd_type.model_group
        if xsd_group is None or not frame.children:
            # empty complex element
            keep_dict = self._keep_result_dict(frame)
            if not keep_dict:
                value = data.text
                frame.self_value = value
                return self._emit(frame, self._enc(value), value)
            body = self._head_dict(frame)
            if data.text is not None and converter.text_key is not None:
                body[converter.text_key] = data.text
            value = body or None
            frame.self_value = value
            return self._emit(frame, self._enc(value), value)

        result_dict = self._head_dict(frame)
        has_single_group = xsd_group.is_single()
        for name in self._ordered_names(frame):
            values = self._entries_for(frame, name)
            xsd_child = self._child_for(frame, name)
            result_dict[name] = self._merge(
                values, xsd_child, has_single_group, converter
            )
        value = result_dict or None
        frame.self_value = value
        return self._emit(frame, self._enc(value), value)

    def _end_leaf(self, frame: JsonFrame) -> Any:
        converter = self.converter
        data = frame.data
        # Mirror XMLSchemaConverter.element_decode's simple/empty branch.
        keep_dict = self._keep_result_dict(frame)
        if not keep_dict:
            if not frame.level and converter.preserve_root:
                self_value = converter.dict_class(
                    ((converter.map_qname(frame.xsd_element.name),
                      data.text),)
                )
                return self._emit(frame, self._enc(self_value), self_value,
                                  parent_marker=self_value)
            # bare scalar: self renders the scalar, parent groups the scalar
            return self._emit(frame, self._enc(data.text), data.text,
                              parent_marker=data.text)

        body = self._head_dict(frame)
        if data.text is not None and converter.text_key is not None:
            body[converter.text_key] = data.text
        if not frame.level and converter.preserve_root:
            self_value = converter.dict_class(
                ((converter.map_qname(frame.xsd_element.name),
                  body or None),)
            )
        else:
            self_value = body or None
        return self._emit(frame, self._enc(self_value), self_value,
                          parent_marker=self_value)

    def _merge(self, values, xsd_child, has_single_group, converter):
        result: Any = None
        for value in values:
            if result is None:
                if converter.force_list or not (
                        xsd_child is None
                        or has_single_group and xsd_child.is_single()):
                    result = converter.list_class((value,))
                else:
                    result = value
            elif not isinstance(result, MutableSequence) or not result:
                result = converter.list_class((result, value))
            elif isinstance(result[0], MutableSequence) or \
                    not isinstance(value, MutableSequence):
                result.append(value)
            else:
                result = converter.list_class((result, value))
        return result


class ParkerJsonSink(JsonStreamSink):
    """Streaming counterpart of :class:`xmlschema.ParkerConverter`."""

    def begin_fast_array(self, frame, mapped_name):
        frame.fast_name = mapped_name
        self._write('{' + self._enc(mapped_name) + self.key_sep + '[')

    def end_element(self) -> Any:
        self._fast_separator()
        frame = self.stack.pop()
        converter = self.converter
        data = frame.data

        if frame.fast_path:
            self._write(']}')
            frame.marker = None
            return frame.marker

        if frame.is_leaf:
            # Parker drops attributes and xmlns; leaves are the bare scalar.
            if converter.preserve_root and not frame.level:
                self_value = converter.dict_class(
                    ((converter.map_qname(frame.xsd_element.name),
                      data.text),)
                )
                frame.self_value = self_value
            else:
                self_value = data.text
                frame.self_value = self_value
            # parent grouping marker is always the bare text
            return self._emit(frame, self._enc(self_value), data.text)

        xsd_group = frame.xsd_type.model_group
        if xsd_group is None or not frame.children:
            if converter.preserve_root and not frame.level:
                self_value = converter.dict_class(
                    ((converter.map_qname(frame.xsd_element.name),
                      data.text),)
                )
            else:
                self_value = data.text
            frame.self_value = self_value
            return self._emit(frame, self._enc(self_value), self_value)

        # Mirror ParkerConverter.element_decode's KeyError/AttributeError
        # merge and the single-element list unwrap post-processing.
        result_dict: dict[str, Any] = {}
        for name, value, _xsd_child in converter.map_content(frame.children):
            try:
                result_dict[name].append(value)
            except KeyError:
                if isinstance(value, MutableSequence):
                    result_dict[name] = converter.list_class((value,))
                else:
                    result_dict[name] = value
            except AttributeError:
                result_dict[name] = converter.list_class(
                    (result_dict[name], value)
                )
        for k, v in result_dict.items():
            if isinstance(v, MutableSequence) and len(v) == 1:
                inner = v.pop()
                v.extend(inner)

        if converter.preserve_root and not frame.level:
            out = converter.dict_class(
                ((converter.map_qname(frame.xsd_element.name),
                  result_dict),)
            )
        else:
            out = result_dict if result_dict else None
        return self._emit(frame, self._enc(out), out)


class BadgerFishJsonSink(JsonStreamSink):
    """Streaming counterpart of :class:`xmlschema.BadgerFishConverter`."""

    def begin_fast_array(self, frame, mapped_name):
        frame.fast_name = mapped_name
        # BadgerFish wraps every element as {tag: inner}; emit the outer
        # wrapper opening plus the head (attrs/xmlns) of the inner object.
        outer_tag = self.converter.map_qname(frame.xsd_element.name)
        head = self._head_fragment(frame)
        self._write('{' + self._enc(outer_tag) + self.key_sep + head +
                    self._enc(mapped_name) + self.key_sep + '[')

    def _head_dict(self, frame: JsonFrame) -> dict[str, Any]:
        converter = self.converter
        head: dict[str, Any] = {}
        if frame.data.attributes:
            for name, value in converter.map_attributes(frame.data.attributes):
                head[name] = value
        xmlns = converter.get_effective_xmlns(
            frame.data.xmlns, frame.level, frame.xsd_element
        )
        if converter._use_namespaces and xmlns:
            head['@xmlns'] = converter.dict_class(
                (k or '$', v) for k, v in xmlns
            )
        return head

    def _head_fragment(self, frame: JsonFrame) -> str:
        head = self._head_dict(frame)
        if not head:
            return '{'
        return '{' + self.item_sep.join(
            self._enc(k) + self.key_sep + self._enc(v) for k, v in head.items()
        ) + self.item_sep

    def _parent_is_fast(self) -> bool:
        return bool(self.stack and self.stack[-1].fast_path)

    def _merge(self, entries, xsd_child, has_single_group, frame, converter):
        # Child markers are full BadgerFish {tag: inner} objects; the parent
        # grouping keeps the unwrapped inner dict, as element_decode does
        # with ``item = item[name]``.
        items = []
        for entry in entries:
            if isinstance(entry, MutableMapping):
                inner = entry[next(iter(entry))]
            else:
                inner = entry
            items.append(inner)

        result: Any = None
        any_type = frame.xsd_type.name == XSD_ANY_TYPE
        for item in items:
            if result is None:
                if any_type or has_single_group and xsd_child is not None \
                        and xsd_child.is_single():
                    result = item
                else:
                    result = converter.list_class((item,))
            elif not isinstance(result, MutableSequence) or not result:
                result = converter.list_class((result, item))
            elif isinstance(result[0], MutableSequence) or \
                    not isinstance(item, MutableSequence):
                result.append(item)
            else:
                result = converter.list_class((result, item))
        return result

    def end_element(self) -> Any:
        self._fast_separator()
        frame = self.stack.pop()
        converter = self.converter
        data = frame.data
        tag = converter.map_qname(frame.xsd_element.name)

        if frame.fast_path:
            # close the inner object and the outer {tag: ...} wrapper
            self._write(']}' + '}')
            self_value = converter.dict_class(((tag, None),))
            frame.marker = self_value
            return self_value

        result_dict = self._head_dict(frame)

        if frame.is_leaf:
            if data.text is not None:
                result_dict['$'] = data.text
            self_value = converter.dict_class(((tag, result_dict),))
            # A direct child of a fast-array frame renders only its inner
            # object; the array grouping re-wraps it by the child name.
            if self._parent_is_fast():
                return self._emit(frame, self._enc(result_dict),
                                  self_value, parent_marker=self_value)
            return self._emit(frame, self._enc(self_value), self_value,
                              parent_marker=self_value)

        # Mixed-content character data is emitted through frame.ordered as
        # numbered parts ('$1', ...). The plain '$' key is only used when
        # there is no interleaved cdata (simple/simple-content elements).
        if data.text is not None and not frame.cdata:
            result_dict['$'] = data.text

        xsd_group = frame.xsd_type.model_group
        has_single_group = xsd_group is not None and xsd_group.is_single()
        any_type = frame.xsd_type.name == XSD_ANY_TYPE
        # Walk the document-order sequence so mixed-content cdata parts keep
        # their position relative to the element children. For element-only
        # frames (no interleaved cdata) the sequence is reconstructed from
        # the buffered child entries.
        ordered_entries = frame.ordered or [
            ('child', name, value, xsd_child)
            for name, value, xsd_child in frame.children
        ]
        for entry in ordered_entries:
            if entry[0] == 'cdata':
                _, index, value = entry
                result_dict[f'${index}'] = value
                continue
            _, name, value, xsd_child = entry
            inner = value[next(iter(value))] \
                if isinstance(value, MutableMapping) else value
            if name not in result_dict:
                if any_type or has_single_group and xsd_child is not None \
                        and xsd_child.is_single():
                    result_dict[name] = inner
                else:
                    result_dict[name] = converter.list_class((inner,))
            else:
                current = result_dict[name]
                if not isinstance(current, MutableSequence) or not current:
                    result_dict[name] = converter.list_class((current, inner))
                elif isinstance(current[0], MutableSequence) or \
                        not isinstance(inner, MutableSequence):
                    current.append(inner)
                else:
                    result_dict[name] = converter.list_class((current, inner))

        self_value = converter.dict_class(((tag, result_dict),))
        if self._parent_is_fast():
            # direct child of a fast-array frame: render the inner object only
            return self._emit(frame, self._enc(result_dict), self_value,
                              parent_marker=self_value)
        return self._emit(frame, self._enc(self_value), self_value,
                          parent_marker=self_value)


class ColumnarJsonSink(JsonStreamSink):
    """Streaming counterpart of :class:`xmlschema.ColumnarConverter`."""

    def begin_fast_array(self, frame, mapped_name):
        # Reached only via deferred promotion, so the repeating child is
        # multi-valued and Columnar groups it under the child local name as
        # a plain list. At the document root Columnar adds an extra
        # {root_local: ...} wrapper; nested fast frames do not. Columnar uses
        # unqualified local names, never the converter's qname mapping.
        if frame.fast_name is not None:
            return
        frame.fast_name = mapped_name
        frame._columnar_root_wrap = frame.level == 0
        if frame._columnar_root_wrap:
            outer = frame.xsd_element.local_name
            self._write('{' + self._enc(outer) + self.key_sep)
        head = self._head_fragment(frame)
        local_child = self._local_name(frame.fast_name)
        self._write(head + self._enc(local_child) + self.key_sep + '[')

    @staticmethod
    def _local_name(qname_or_tag: str) -> str:
        return qname_or_tag.split('}', 1)[-1] if '}' in qname_or_tag \
            else qname_or_tag.split(':', 1)[-1]

    def fast_array_item(self, frame, value):
        if frame.fast_count:
            self._write(self.item_sep)
        self._write(self._enc(value))
        frame.fast_count += 1

    def _head_fragment(self, frame: JsonFrame) -> str:
        converter = self.converter
        xsd_element = frame.xsd_element
        parts: list[str] = []
        if frame.data.attributes:
            if converter.attr_prefix:
                pfx = xsd_element.local_name + converter.attr_prefix
            else:
                pfx = xsd_element.local_name
            for k, v in frame.data.attributes:
                parts.append(self._enc(pfx + converter.map_qname(k))
                             + self.key_sep + self._enc(v))
        if not parts:
            return '{'
        return '{' + self.item_sep.join(parts) + self.item_sep

    def end_element(self) -> Any:
        self._fast_separator()
        frame = self.stack.pop()
        converter = self.converter
        xsd_element = frame.xsd_element
        xsd_type = frame.xsd_type
        data = frame.data

        if frame.fast_path:
            self._write(']}')
            if getattr(frame, '_columnar_root_wrap', False):
                self._write('}')
            frame.marker = None
            return frame.marker

        result_dict: Any = converter.dict_class()
        if data.attributes:
            if converter.attr_prefix:
                pfx = xsd_element.local_name + converter.attr_prefix
            else:
                pfx = xsd_element.local_name
            for k, v in data.attributes:
                result_dict[pfx + converter.map_qname(k)] = v

        if xsd_type.simple_type is not None:
            result_dict[xsd_element.local_name] = data.text

        if frame.children:
            result_dict = self._build_content(frame, result_dict)

        if frame.is_leaf and xsd_type.simple_type is not None:
            # result_dict is the inner {local: text}; at the document root
            # Columnar wraps it once more as {root_local: inner}.
            if frame.level == 0:
                self_value = converter.dict_class(
                    [(xsd_element.local_name, result_dict)]
                )
                return self._emit(frame, self._enc(self_value), self_value,
                                  parent_marker=self_value)
            return self._emit(frame, self._enc(result_dict), result_dict,
                              parent_marker=result_dict)

        if frame.level == 0:
            self_value = converter.dict_class(
                [(xsd_element.local_name, result_dict)]
            )
            return self._emit(frame, self._enc(self_value), self_value,
                              parent_marker=self_value)

        # level > 0: the frame renders the {local: ...} wrapper about itself,
        # but a buffered parent groups the bare inner dict/list, matching the
        # full-load converter's level>0 return value.
        return self._emit(frame, self._enc(result_dict), result_dict,
                          parent_marker=result_dict)

    def _build_content(self, frame: JsonFrame, result_dict: Any) -> Any:
        converter = self.converter
        for name, value, xsd_child in converter.map_content(frame.children):
            if not value:
                continue
            elif xsd_child.local_name:
                name = xsd_child.local_name
            else:
                name = name[2 + len(xsd_child.namespace):]

            if xsd_child.is_single():
                if xsd_child.type is not None and \
                        xsd_child.type.simple_type is not None:
                    for k in value:
                        result_dict[k] = value[k]
                else:
                    result_dict[name] = value
            else:
                if xsd_child.type is not None and \
                        xsd_child.type.simple_type is not None \
                        and not xsd_child.attributes:
                    # Streaming-safe replacement of the converter's DOM
                    # based xsd_element.findall('*') length check.
                    only_child = self._is_only_element_particle(frame,
                                                                 xsd_child)
                    if only_child:
                        try:
                            result_dict.append(list(value.values())[0])
                        except AttributeError:
                            result_dict = converter.list_class(value.values())
                    else:
                        try:
                            result_dict[name].append(list(value.values())[0])
                        except KeyError:
                            result_dict[name] = converter.list_class(
                                value.values()
                            )
                        except AttributeError:
                            result_dict[name] = converter.list_class(
                                value.values()
                            )
                else:
                    try:
                        result_dict[name].append(value)
                    except KeyError:
                        result_dict[name] = converter.list_class([value])
                    except AttributeError:
                        result_dict[name] = converter.list_class([value])
        return result_dict

    @staticmethod
    def _is_only_element_particle(frame: JsonFrame,
                                  xsd_child: 'XsdElement') -> bool:
        group = frame.xsd_type.model_group
        if group is None:
            return False
        elements = [e for e in group.iter_elements()]
        return len(elements) == 1 and elements[0] is xsd_child


class JsonMLJsonSink(JsonStreamSink):
    """Streaming counterpart of :class:`xmlschema.JsonMLConverter`."""

    def begin_fast_array(self, frame, mapped_name):
        # Reached only via deferred promotion: the repeating child is
        # multi-valued, so each child JsonML list streams into the enclosing
        # list. Emit [tag, {attrs}? prefix without the closing ']'.
        if frame.fast_name is not None:
            return
        frame.fast_name = mapped_name
        converter = self.converter
        head: list[Any] = [converter.map_qname(frame.xsd_element.name)]
        attributes = converter.dict_class(
            converter.map_attributes(frame.data.attributes or ())
        )
        xmlns = converter.get_effective_xmlns(
            frame.data.xmlns, frame.level, frame.xsd_element
        )
        if xmlns and converter._use_namespaces:
            attributes.update(
                (f'{converter.ns_prefix}:{k}' if k else converter.ns_prefix, v)
                for k, v in xmlns
            )
        if attributes:
            head.append(attributes)
        prefix = self.encoder.encode(head)
        # prefix ends with ']' for the head list; drop it and keep the item
        # separator so the first streamed child is properly delimited.
        self._write(prefix[:-1] + self.item_sep)

    def fast_array_item(self, frame, value):
        if frame.fast_count:
            self._write(self.item_sep)
        self._write(self._enc(value))
        frame.fast_count += 1

    def end_element(self) -> Any:
        self._fast_separator()
        frame = self.stack.pop()
        converter = self.converter

        if frame.fast_path:
            self._write(']')
            frame.marker = None
            return frame.marker

        data = frame.data
        result_list: list[Any] = [] if converter.list_class is list \
            else converter.list_class()
        result_list.append(converter.map_qname(frame.xsd_element.name))

        attributes = converter.dict_class(
            converter.map_attributes(data.attributes or ())
        )
        xmlns = converter.get_effective_xmlns(
            data.xmlns, frame.level, frame.xsd_element
        )
        if xmlns and converter._use_namespaces:
            attributes.update(
                (f'{converter.ns_prefix}:{k}' if k else converter.ns_prefix, v)
                for k, v in xmlns
            )
        if attributes:
            result_list.append(attributes)

        if frame.is_leaf:
            if data.text is not None:
                result_list.append(data.text)
            # parent grouping consumes the JsonML list as-is
            return self._emit(frame, self._enc(result_list), result_list,
                              parent_marker=result_list)

        if frame.xsd_type.model_group is not None:
            # Honor document order, interleaving mixed-content cdata strings
            # with the child JsonML lists.
            for entry in frame.ordered:
                if entry[0] == 'cdata':
                    result_list.append(entry[2])
                else:
                    _, _name, value, _child = entry
                    result_list.append(
                        value if value is not None
                        else converter.list_class((entry[1],))
                    )

        frame.self_value = result_list
        return self._emit(frame, self._enc(result_list), result_list)


def get_json_sink(converter: 'XMLSchemaConverter', fp: IO[str],
                  json_options: Optional[dict[str, Any]] = None) \
        -> JsonStreamSink:
    """Return the streaming sink paired with *converter*'s class."""
    from xmlschema.converters.parker import ParkerConverter
    from xmlschema.converters.badgerfish import BadgerFishConverter
    from xmlschema.converters.columnar import ColumnarConverter
    from xmlschema.converters.jsonml import JsonMLConverter
    from xmlschema.converters.base import XMLSchemaConverter

    if isinstance(converter, ParkerConverter):
        return ParkerJsonSink(fp, converter, json_options)
    if isinstance(converter, BadgerFishConverter):
        return BadgerFishJsonSink(fp, converter, json_options)
    if isinstance(converter, ColumnarConverter):
        return ColumnarJsonSink(fp, converter, json_options)
    if isinstance(converter, JsonMLConverter):
        return JsonMLJsonSink(fp, converter, json_options)
    if isinstance(converter, XMLSchemaConverter):
        return XMLSchemaJsonSink(fp, converter, json_options)
    raise TypeError(
        f"no streaming JSON sink available for converter {type(converter)!r}"
    )
