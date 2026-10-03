"""Tests for the page store conditional patch component.

Run with:  python3 -m unittest test_page_store -v
"""

import json
import unittest

from page_store import (
    ETagMismatchError,
    InvalidDocumentError,
    PageStore,
    PatchError,
)


def node(node_id, node_type="text", props=None, children=None):
    return {
        "id": node_id,
        "type": node_type,
        "props": props if props is not None else {},
        "children": children if children is not None else [],
    }


def make_page():
    return {
        "id": "root",
        "type": "page",
        "props": {"title": "Demo"},
        "children": [
            {
                "id": "a",
                "type": "section",
                "props": {"~tilde": 1, "": "empty-key", "0": "zero"},
                "children": [node("a0"), node("a1"), node("a2")],
            },
            {
                "id": "b",
                "type": "section",
                "props": {"x~y": {"nested": [1, 2, 3]}, "x/y": "slash"},
                "children": [],
            },
        ],
    }


class MoveCopyAndKeyTests(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        self.page_id, self.etag = self.store.create(json.dumps(make_page()))

    def read_doc(self):
        document_json, etag = self.store.read(self.page_id)
        return json.loads(document_json), etag

    def test_move_copy_with_escaped_empty_and_numeric_keys(self):
        patch = [
            # "~0" escape: key "~tilde"; numbers compare by value (1 == 1.0).
            {"op": "test", "path": "/children/0/props/~0tilde", "value": 1.0},
            # Trailing "/" addresses the empty-string key.
            {"op": "test", "path": "/children/0/props/", "value": "empty-key"},
            # "0" is an object member name here, not an array index.
            {"op": "test", "path": "/children/0/props/0", "value": "zero"},
            # Move backwards within the array: [a0,a1,a2] -> [a1,a2,a0].
            # The target index 2 is interpreted after the removal.
            {"op": "move", "from": "/children/0/children/0",
             "path": "/children/0/children/2"},
            # Copy subtree "b"; the duplicate id is only temporary.
            {"op": "copy", "from": "/children/1", "path": "/children/2"},
            {"op": "replace", "path": "/children/2/id", "value": "b-copy"},
            # "~0" escape inside the copied props; edit nested array element.
            {"op": "replace", "path": "/children/2/props/x~0y/nested/1",
             "value": 99},
            # "-" appends to an array as a copy target.
            {"op": "copy", "from": "/children/0/children/1",
             "path": "/children/2/children/-"},
            {"op": "replace", "path": "/children/2/children/0/id",
             "value": "a2-copy"},
        ]
        new_etag = self.store.patch(self.page_id, self.etag, json.dumps(patch))
        self.assertNotEqual(new_etag, self.etag)

        doc, etag = self.read_doc()
        self.assertEqual(etag, new_etag)
        self.assertEqual([c["id"] for c in doc["children"]],
                         ["a", "b", "b-copy"])
        # Array move used post-removal index semantics.
        self.assertEqual([c["id"] for c in doc["children"][0]["children"]],
                         ["a1", "a2", "a0"])
        # Source subtree "b" is untouched by edits to the copy.
        self.assertEqual(doc["children"][1]["props"]["x~y"]["nested"], [1, 2, 3])
        self.assertEqual(doc["children"][1]["props"]["x/y"], "slash")
        # The copy is independent and carries the modified nested props.
        copied = doc["children"][2]
        self.assertEqual(copied["props"]["x~y"]["nested"], [1, 99, 3])
        self.assertEqual([c["id"] for c in copied["children"]], ["a2-copy"])
        # The original a2 node keeps its own id.
        self.assertEqual(doc["children"][0]["children"][1]["id"], "a2")


class RootRebuildAndETagTests(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        self.page_id, self.etag = self.store.create(json.dumps(make_page()))

    def read_doc(self):
        document_json, etag = self.store.read(self.page_id)
        return json.loads(document_json), etag

    def test_root_rebuild_duplicate_id_failure_and_test_only_patch(self):
        # 1. A patch may remove the whole root and rebuild a valid page.
        rebuilt = {
            "id": "root2",
            "type": "page",
            "props": {},
            "children": [node("only-child")],
        }
        patch = [
            {"op": "remove", "path": ""},
            {"op": "add", "path": "", "value": rebuilt},
        ]
        etag2 = self.store.patch(self.page_id, self.etag, json.dumps(patch))
        self.assertNotEqual(etag2, self.etag)
        doc, _ = self.read_doc()
        self.assertEqual(doc, rebuilt)

        # 2. A patch whose final tree has duplicate ids fails atomically.
        bad_patch = [
            {"op": "add", "path": "/children/-", "value": node("unique-1")},
            {"op": "add", "path": "/children/-", "value": node("root2")},
        ]
        with self.assertRaises(InvalidDocumentError):
            self.store.patch(self.page_id, etag2, json.dumps(bad_patch))
        doc, etag_after = self.read_doc()
        self.assertEqual(etag_after, etag2)  # ETag unchanged on failure
        self.assertEqual(doc, rebuilt)       # first add was rolled back too

        # 3. A test-only patch succeeds and still rotates the ETag.
        test_only = [{"op": "test", "path": "/id", "value": "root2"}]
        etag3 = self.store.patch(self.page_id, etag2, json.dumps(test_only))
        self.assertNotEqual(etag3, etag2)
        doc, _ = self.read_doc()
        self.assertEqual(doc, rebuilt)

        # 4. The stale ETag can no longer be used for writes.
        with self.assertRaises(ETagMismatchError):
            self.store.patch(self.page_id, etag2, json.dumps(test_only))

        # 5. Even an empty patch produces a fresh, never-used ETag.
        etag4 = self.store.patch(self.page_id, etag3, "[]")
        self.assertNotIn(etag4, {self.etag, etag2, etag3})


class MoveEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        self.page_id, self.etag = self.store.create(json.dumps(make_page()))

    def test_move_into_own_descendant_fails_and_is_atomic(self):
        patch = [
            {"op": "move", "from": "/children/0", "path": "/children/0/children/0"},
        ]
        with self.assertRaises(PatchError):
            self.store.patch(self.page_id, self.etag, json.dumps(patch))
        _, etag_after = self.store.read(self.page_id)
        self.assertEqual(etag_after, self.etag)

    def test_move_to_itself_is_a_noop_but_requires_source(self):
        etag2 = self.store.patch(
            self.page_id, self.etag,
            json.dumps([{"op": "move", "from": "/children/0",
                         "path": "/children/0"}]),
        )
        self.assertNotEqual(etag2, self.etag)
        with self.assertRaises(PatchError):
            self.store.patch(
                self.page_id, etag2,
                json.dumps([{"op": "move", "from": "/children/9",
                             "path": "/children/9"}]),
            )


if __name__ == "__main__":
    unittest.main()
