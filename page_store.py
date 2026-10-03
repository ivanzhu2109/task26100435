"""In-memory page document store with RFC 6902 conditional patching."""

from __future__ import annotations

import copy
import json
import uuid


class ApiError(Exception):
    """Base error for store operations."""


class PageNotFoundError(ApiError):
    """Raised when page_id does not exist."""


class ETagMismatchError(ApiError):
    """Raised when expected_etag does not match the current ETag."""


class PatchError(ApiError):
    """Raised when a patch cannot be applied; document and ETag stay unchanged."""


class InvalidPageError(PatchError):
    """Raised when the resulting document is not a valid page tree."""


def validate_page(node):
    """Raise InvalidPageError unless node is a valid page tree."""
    _validate_node(node, set())


def _validate_node(node, seen_ids):
    if not isinstance(node, dict):
        raise InvalidPageError("page node must be an object")
    for key, expected in (("id", str), ("type", str), ("props", dict), ("children", list)):
        if key not in node:
            raise InvalidPageError("page node missing %r" % key)
        if not isinstance(node[key], expected):
            raise InvalidPageError("page node %r must be %s" % (key, expected.__name__))
    if node["id"] in seen_ids:
        raise InvalidPageError("duplicate node id %r" % node["id"])
    seen_ids.add(node["id"])
    for child in node["children"]:
        _validate_node(child, seen_ids)


_MISSING = object()


def _unescape(segment):
    return segment.replace("~1", "/").replace("~0", "~")


def _parse_pointer(path):
    if not isinstance(path, str):
        raise PatchError("path must be a string")
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError("non-empty path must start with '/'")
    return [_unescape(seg) for seg in path.split("/")[1:]]


def _parse_index(segment):
    if not segment or not segment.isascii() or not segment.isdigit():
        raise PatchError("invalid array index %r" % segment)
    if len(segment) > 1 and segment[0] == "0":
        raise PatchError("array index must not have leading zeros")
    return int(segment)


def _child(container, segment):
    if container is _MISSING:
        raise PatchError("document does not exist")
    if isinstance(container, list):
        index = _parse_index(segment)
        if index >= len(container):
            raise PatchError("array index out of range")
        return container[index]
    if isinstance(container, dict):
        if segment not in container:
            raise PatchError("object member %r does not exist" % segment)
        return container[segment]
    raise PatchError("cannot traverse into a scalar value")


def _get(doc, segments):
    current = doc
    for segment in segments:
        current = _child(current, segment)
    if current is _MISSING:
        raise PatchError("document does not exist")
    return current


def _add(doc, segments, value):
    if not segments:
        return value
    parent = _get(doc, segments[:-1])
    last = segments[-1]
    if isinstance(parent, list):
        if last == "-":
            parent.append(value)
        else:
            index = _parse_index(last)
            if index > len(parent):
                raise PatchError("array insert index out of range")
            parent.insert(index, value)
    elif isinstance(parent, dict):
        parent[last] = value
    else:
        raise PatchError("cannot add into a scalar value")
    return doc


def _remove(doc, segments):
    if not segments:
        if doc is _MISSING:
            raise PatchError("document does not exist")
        return _MISSING, doc
    parent = _get(doc, segments[:-1])
    last = segments[-1]
    if isinstance(parent, list):
        index = _parse_index(last)
        if index >= len(parent):
            raise PatchError("array index out of range")
        return doc, parent.pop(index)
    if isinstance(parent, dict):
        if last not in parent:
            raise PatchError("object member %r does not exist" % last)
        return doc, parent.pop(last)
    raise PatchError("cannot remove from a scalar value")


def _replace(doc, segments, value):
    if not segments:
        if doc is _MISSING:
            raise PatchError("document does not exist")
        return value
    parent = _get(doc, segments[:-1])
    last = segments[-1]
    if isinstance(parent, list):
        index = _parse_index(last)
        if index >= len(parent):
            raise PatchError("array index out of range")
        parent[index] = value
    elif isinstance(parent, dict):
        if last not in parent:
            raise PatchError("object member %r does not exist" % last)
        parent[last] = value
    else:
        raise PatchError("cannot replace inside a scalar value")
    return doc


def _move(doc, src, dst):
    if len(dst) > len(src) and dst[: len(src)] == src:
        raise PatchError("cannot move a value into its own descendant")
    _get(doc, src)
    if src == dst:
        return doc
    doc, value = _remove(doc, src)
    return _add(doc, dst, value)


def _copy(doc, src, dst):
    value = _get(doc, src)
    return _add(doc, dst, copy.deepcopy(value))


def _json_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return False


def _apply_patch(doc, patch):
    if not isinstance(patch, list):
        raise PatchError("patch document must be a JSON array")
    for op in patch:
        if not isinstance(op, dict):
            raise PatchError("patch operation must be an object")
        name = op.get("op")
        path = op.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            raise PatchError("operation requires string 'op' and 'path'")
        segments = _parse_pointer(path)
        if name == "add":
            if "value" not in op:
                raise PatchError("add requires 'value'")
            doc = _add(doc, segments, op["value"])
        elif name == "remove":
            doc, _ = _remove(doc, segments)
        elif name == "replace":
            if "value" not in op:
                raise PatchError("replace requires 'value'")
            doc = _replace(doc, segments, op["value"])
        elif name == "move":
            source = op.get("from")
            if not isinstance(source, str):
                raise PatchError("move requires string 'from'")
            doc = _move(doc, _parse_pointer(source), segments)
        elif name == "copy":
            source = op.get("from")
            if not isinstance(source, str):
                raise PatchError("copy requires string 'from'")
            doc = _copy(doc, _parse_pointer(source), segments)
        elif name == "test":
            if "value" not in op:
                raise PatchError("test requires 'value'")
            if not _json_equal(_get(doc, segments), op["value"]):
                raise PatchError("test operation failed")
        else:
            raise PatchError("unknown operation %r" % name)
    return doc


class PageStore:
    """In-memory store: Create / Read / Patch with ETag concurrency control."""

    def __init__(self):
        self._pages = {}

    @staticmethod
    def _make_etag(page_id, version):
        return '"%s-%d"' % (page_id, version)

    def create(self, document_json):
        """Create a page from JSON text. Returns (page_id, etag)."""
        try:
            document = json.loads(document_json)
        except json.JSONDecodeError as exc:
            raise ApiError("document is not valid JSON: %s" % exc) from exc
        validate_page(document)
        page_id = uuid.uuid4().hex
        etag = self._make_etag(page_id, 1)
        self._pages[page_id] = {"doc": document, "etag": etag, "version": 1}
        return page_id, etag

    def read(self, page_id):
        """Return (document_json, etag) for the current state."""
        entry = self._pages.get(page_id)
        if entry is None:
            raise PageNotFoundError("unknown page_id %r" % page_id)
        return json.dumps(entry["doc"]), entry["etag"]

    def patch(self, page_id, expected_etag, patch_json):
        """Atomically apply an RFC 6902 patch. Returns the new ETag."""
        entry = self._pages.get(page_id)
        if entry is None:
            raise PageNotFoundError("unknown page_id %r" % page_id)
        if entry["etag"] != expected_etag:
            raise ETagMismatchError("ETag mismatch")
        try:
            patch = json.loads(patch_json)
        except json.JSONDecodeError as exc:
            raise ApiError("patch is not valid JSON: %s" % exc) from exc
        new_doc = _apply_patch(copy.deepcopy(entry["doc"]), patch)
        validate_page(new_doc)
        entry["version"] += 1
        entry["etag"] = self._make_etag(page_id, entry["version"])
        entry["doc"] = new_doc
        return entry["etag"]
