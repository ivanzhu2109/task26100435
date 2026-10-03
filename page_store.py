"""Conditional JSON Patch (RFC 6902) component for page JSON trees.

In-memory page store for an offline page editor.  Documents and patches
are JSON text; every successful Patch yields a brand-new ETag.
Only the Python standard library is used.
"""

from __future__ import annotations

import copy
import json
import uuid


class PageStoreError(Exception):
    """Base class for all page store errors."""


class PageNotFoundError(PageStoreError):
    """Raised when a page_id does not exist."""


class ETagMismatchError(PageStoreError):
    """Raised when expected_etag does not match the current ETag."""


class PatchError(PageStoreError):
    """Raised when a patch cannot be applied (bad path, failed test, ...)."""


class InvalidDocumentError(PageStoreError):
    """Raised when a document is not a valid page tree."""


_MISSING = object()


def _unescape(segment: str) -> str:
    # Decode ~1 and ~0 exactly once, in RFC 6901 order.
    return segment.replace("~1", "/").replace("~0", "~")


def _parse_pointer(path) -> list:
    if not isinstance(path, str):
        raise PatchError("path must be a string")
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError("non-empty path must start with '/'")
    return [_unescape(segment) for segment in path.split("/")[1:]]


def _parse_index(segment: str) -> int:
    if not segment or not segment.isascii() or not segment.isdigit():
        raise PatchError(f"invalid array index: {segment!r}")
    if len(segment) > 1 and segment[0] == "0":
        raise PatchError(f"array index with leading zero: {segment!r}")
    return int(segment)


def _read_child(container, segment: str):
    if isinstance(container, dict):
        if segment not in container:
            raise PatchError(f"object member does not exist: {segment!r}")
        return container[segment]
    if isinstance(container, list):
        index = _parse_index(segment)
        if index >= len(container):
            raise PatchError(f"array index out of range: {segment!r}")
        return container[index]
    raise PatchError("cannot traverse into a scalar value")


def _resolve_parent(box: list, segments: list):
    current = box[0]
    if current is _MISSING:
        raise PatchError("document does not exist")
    for segment in segments[:-1]:
        current = _read_child(current, segment)
    return current


def _read_value(box: list, segments: list):
    if not segments:
        if box[0] is _MISSING:
            raise PatchError("document does not exist")
        return box[0]
    current = box[0]
    if current is _MISSING:
        raise PatchError("document does not exist")
    for segment in segments:
        current = _read_child(current, segment)
    return current


def _op_add(box: list, segments: list, value) -> None:
    if not segments:
        box[0] = value
        return
    parent = _resolve_parent(box, segments)
    last = segments[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        if last == "-":
            parent.append(value)
        else:
            index = _parse_index(last)
            if index > len(parent):
                raise PatchError(f"add index out of range: {last!r}")
            parent.insert(index, value)
    else:
        raise PatchError("cannot add into a scalar value")


def _op_remove(box: list, segments: list):
    if not segments:
        if box[0] is _MISSING:
            raise PatchError("document does not exist")
        value = box[0]
        box[0] = _MISSING
        return value
    parent = _resolve_parent(box, segments)
    last = segments[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PatchError(f"object member does not exist: {last!r}")
        return parent.pop(last)
    if isinstance(parent, list):
        index = _parse_index(last)
        if index >= len(parent):
            raise PatchError(f"array index out of range: {last!r}")
        return parent.pop(index)
    raise PatchError("cannot remove from a scalar value")


def _op_replace(box: list, segments: list, value) -> None:
    if not segments:
        if box[0] is _MISSING:
            raise PatchError("document does not exist")
        box[0] = value
        return
    parent = _resolve_parent(box, segments)
    last = segments[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise PatchError(f"object member does not exist: {last!r}")
        parent[last] = value
    elif isinstance(parent, list):
        index = _parse_index(last)
        if index >= len(parent):
            raise PatchError(f"array index out of range: {last!r}")
        parent[index] = value
    else:
        raise PatchError("cannot replace inside a scalar value")


def _op_move(box: list, from_segments: list, to_segments: list) -> None:
    if from_segments == to_segments:
        _read_value(box, from_segments)  # no-op, but the source must exist
        return
    if (
        len(to_segments) > len(from_segments)
        and to_segments[: len(from_segments)] == from_segments
    ):
        raise PatchError("cannot move a value into its own descendant")
    value = _op_remove(box, from_segments)
    _op_add(box, to_segments, value)


def _op_copy(box: list, from_segments: list, to_segments: list) -> None:
    value = copy.deepcopy(_read_value(box, from_segments))
    _op_add(box, to_segments, value)


def _json_equal(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return False


def _required_member(operation: dict, key: str):
    if key not in operation:
        raise PatchError(f"operation is missing member {key!r}")
    return operation[key]


_OP_NAMES = {"add", "remove", "replace", "move", "copy", "test"}


def _apply_operation(box: list, operation) -> None:
    if not isinstance(operation, dict):
        raise PatchError("patch operation must be an object")
    op = operation.get("op")
    if op not in _OP_NAMES:
        raise PatchError(f"unsupported operation: {op!r}")
    path = _parse_pointer(_required_member(operation, "path"))
    if op == "add":
        _op_add(box, path, _required_member(operation, "value"))
    elif op == "remove":
        _op_remove(box, path)
    elif op == "replace":
        _op_replace(box, path, _required_member(operation, "value"))
    elif op == "test":
        expected = _required_member(operation, "value")
        if not _json_equal(_read_value(box, path), expected):
            raise PatchError(f"test operation failed at {operation['path']!r}")
    else:
        from_path = _required_member(operation, "from")
        if not isinstance(from_path, str):
            raise PatchError("'from' must be a string")
        from_segments = _parse_pointer(from_path)
        if op == "move":
            _op_move(box, from_segments, path)
        else:
            _op_copy(box, from_segments, path)


def _reject_constant(value: str):
    raise PageStoreError(f"invalid JSON constant: {value}")


def _parse_json(text) -> object:
    if not isinstance(text, str):
        raise PageStoreError("expected JSON text input")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise PageStoreError(f"invalid JSON: {exc}") from exc


def _validate_page_tree(root) -> None:
    if root is _MISSING:
        raise InvalidDocumentError("document must be a page node")
    seen_ids = set()

    def visit(node) -> None:
        if not isinstance(node, dict):
            raise InvalidDocumentError("page node must be an object")
        if not isinstance(node.get("id"), str):
            raise InvalidDocumentError("page node requires a string id")
        if not isinstance(node.get("type"), str):
            raise InvalidDocumentError("page node requires a string type")
        if not isinstance(node.get("props"), dict):
            raise InvalidDocumentError("page node requires an object props")
        children = node.get("children")
        if not isinstance(children, list):
            raise InvalidDocumentError("page node requires a children array")
        if node["id"] in seen_ids:
            raise InvalidDocumentError(f"duplicate node id: {node['id']!r}")
        seen_ids.add(node["id"])
        for child in children:
            visit(child)

    visit(root)


class PageStore:
    """In-memory store of page documents with ETag-guarded patching."""

    def __init__(self) -> None:
        self._documents = {}
        self._etags = {}
        self._etag_counters = {}

    def _next_etag(self, page_id: str) -> str:
        counter = self._etag_counters.get(page_id, 0) + 1
        self._etag_counters[page_id] = counter
        return f'"{page_id}-{counter}"'

    def create(self, document_json: str) -> tuple[str, str]:
        """Create a page from JSON text; return (page_id, etag)."""
        document = _parse_json(document_json)
        _validate_page_tree(document)
        page_id = uuid.uuid4().hex
        self._documents[page_id] = document
        etag = self._next_etag(page_id)
        self._etags[page_id] = etag
        return page_id, etag

    def read(self, page_id: str) -> tuple[str, str]:
        """Return (document_json, etag) for the page."""
        if page_id not in self._documents:
            raise PageNotFoundError(f"unknown page_id: {page_id!r}")
        document_json = json.dumps(self._documents[page_id], ensure_ascii=False)
        return document_json, self._etags[page_id]

    def patch(self, page_id: str, expected_etag: str, patch_json: str) -> str:
        """Atomically apply an RFC 6902 patch; return the new ETag."""
        if page_id not in self._documents:
            raise PageNotFoundError(f"unknown page_id: {page_id!r}")
        if expected_etag != self._etags[page_id]:
            raise ETagMismatchError(
                f"expected ETag {expected_etag!r} does not match {self._etags[page_id]!r}"
            )
        operations = _parse_json(patch_json)
        if not isinstance(operations, list):
            raise PatchError("patch must be a JSON array of operations")
        box = [copy.deepcopy(self._documents[page_id])]
        for operation in operations:
            _apply_operation(box, operation)
        _validate_page_tree(box[0])
        self._documents[page_id] = box[0]
        etag = self._next_etag(page_id)
        self._etags[page_id] = etag
        return etag
