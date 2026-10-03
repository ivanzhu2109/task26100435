import json
import unittest

from page_store import (
    ETagMismatchError,
    PageStore,
    PatchError,
)


def node(node_id, props=None, children=None, node_type="widget"):
    return {
        "id": node_id,
        "type": node_type,
        "props": props if props is not None else {},
        "children": children if children is not None else [],
    }


class MoveCopySemanticsTest(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        doc = {
            "id": "root",
            "type": "page",
            "props": {"a/b": 1, "m~n": 2, "": 3, "0": "zero", "-": "dash"},
            "children": [
                node("a"),
                node("b"),
                node("c", props={"nested": {"x": 1}}, children=[node("c1")]),
                node("d"),
            ],
        }
        self.page_id, self.etag = self.store.create(json.dumps(doc))

    def test_move_copy_and_edit_copy(self):
        patch = [
            # Escaped keys, empty key, numeric and dash object keys on a dict.
            {"op": "test", "path": "/props/a~1b", "value": 1},
            {"op": "test", "path": "/props/m~0n", "value": 2},
            {"op": "test", "path": "/props/", "value": 3},
            {"op": "test", "path": "/props/0", "value": "zero"},
            {"op": "test", "path": "/props/-", "value": "dash"},
            # Move backwards inside the children array: [a,b,c,d] -> [a,d,b,c].
            {"op": "move", "from": "/children/3", "path": "/children/1"},
            # Copy subtree c (now at index 3) and insert the copy at index 1.
            {"op": "copy", "from": "/children/3", "path": "/children/1"},
            # Fix the duplicate id of the copy and edit its nested props.
            {"op": "replace", "path": "/children/1/id", "value": "c-copy"},
            {"op": "replace", "path": "/children/1/children/0/id", "value": "c1-copy"},
            {"op": "replace", "path": "/children/1/props/nested/x", "value": 10},
            {"op": "add", "path": "/children/1/props/nested/y", "value": 2},
        ]
        new_etag = self.store.patch(self.page_id, self.etag, json.dumps(patch))
        self.assertNotEqual(new_etag, self.etag)

        doc_json, read_etag = self.store.read(self.page_id)
        self.assertEqual(read_etag, new_etag)
        doc = json.loads(doc_json)

        self.assertEqual(
            [child["id"] for child in doc["children"]],
            ["a", "c-copy", "d", "b", "c"],
        )
        copied = doc["children"][1]
        self.assertEqual(copied["props"], {"nested": {"x": 10, "y": 2}})
        self.assertEqual(copied["children"][0]["id"], "c1-copy")
        # Source subtree is unchanged by the copy and later edits.
        source = doc["children"][4]
        self.assertEqual(source["id"], "c")
        self.assertEqual(source["props"], {"nested": {"x": 1}})
        self.assertEqual(source["children"][0]["id"], "c1")
        # Object keys are matched exactly as strings.
        self.assertEqual(doc["props"]["0"], "zero")
        self.assertEqual(doc["props"][""], 3)


class RootReplaceAndAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        doc = node("root", children=[node("x"), node("y")], node_type="page")
        self.page_id, self.etag = self.store.create(json.dumps(doc))

    def test_remove_root_then_rebuild(self):
        replacement = node("root-2", children=[node("z")], node_type="page")
        patch = [
            {"op": "remove", "path": ""},
            {"op": "add", "path": "", "value": replacement},
        ]
        new_etag = self.store.patch(self.page_id, self.etag, json.dumps(patch))
        self.assertNotEqual(new_etag, self.etag)
        doc_json, _ = self.store.read(self.page_id)
        self.assertEqual(json.loads(doc_json), replacement)

    def test_duplicate_id_fails_atomically(self):
        patch = [
            {"op": "copy", "from": "/children/0", "path": "/children/-"},
        ]
        with self.assertRaises(PatchError):
            self.store.patch(self.page_id, self.etag, json.dumps(patch))
        doc_json, etag_after = self.store.read(self.page_id)
        self.assertEqual(etag_after, self.etag)
        self.assertEqual(
            [c["id"] for c in json.loads(doc_json)["children"]], ["x", "y"]
        )

    def test_test_only_patch_still_rotates_etag(self):
        patch = [
            {"op": "test", "path": "/id", "value": "root"},
            {"op": "test", "path": "/children/0/id", "value": "x"},
        ]
        new_etag = self.store.patch(self.page_id, self.etag, json.dumps(patch))
        self.assertNotEqual(new_etag, self.etag)
        # Old ETag can no longer be used for writes.
        with self.assertRaises(ETagMismatchError):
            self.store.patch(self.page_id, self.etag, json.dumps([]))
        # New ETag works, and an empty patch also rotates the ETag.
        newer_etag = self.store.patch(self.page_id, new_etag, json.dumps([]))
        self.assertNotEqual(newer_etag, new_etag)
        doc_json, etag_after = self.store.read(self.page_id)
        self.assertEqual(etag_after, newer_etag)
        self.assertEqual(json.loads(doc_json)["id"], "root")


class EdgeSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.store = PageStore()
        doc = node("root", children=[node("x"), node("y")], node_type="page")
        self.page_id, self.etag = self.store.create(json.dumps(doc))

    def _assert_fails(self, patch):
        _, current_etag = self.store.read(self.page_id)
        with self.assertRaises(PatchError):
            self.store.patch(self.page_id, current_etag, json.dumps(patch))
        _, etag_after = self.store.read(self.page_id)
        self.assertEqual(etag_after, current_etag)

    def test_move_into_own_descendant_fails(self):
        self._assert_fails([{"op": "move", "from": "", "path": "/children"}])
        self._assert_fails(
            [{"op": "move", "from": "/children/0", "path": "/children/0/props/self"}]
        )

    def test_move_to_itself_is_noop_but_requires_source(self):
        etag = self.store.patch(
            self.page_id,
            self.etag,
            json.dumps([{"op": "move", "from": "/children/0", "path": "/children/0"}]),
        )
        self.assertNotEqual(etag, self.etag)
        self._assert_fails([{"op": "move", "from": "/children/9", "path": "/children/9"}])

    def test_test_compares_json_typed_values(self):
        self.store.patch(
            self.page_id,
            self.etag,
            json.dumps([{"op": "add", "path": "/props/n", "value": 1}]),
        )
        # 1 and 1.0 compare equal by numeric value.
        etag = self.store.read(self.page_id)[1]
        self.store.patch(
            self.page_id, etag, json.dumps([{"op": "test", "path": "/props/n", "value": 1.0}])
        )
        # bool is not a number; null is not a missing member.
        self._assert_fails([{"op": "test", "path": "/props/n", "value": True}])
        self._assert_fails([{"op": "test", "path": "/props/missing", "value": None}])

    def test_bad_paths_and_indices_fail(self):
        self._assert_fails([{"op": "remove", "path": "/children/02"}])
        self._assert_fails([{"op": "remove", "path": "/children/-"}])
        self._assert_fails([{"op": "replace", "path": "/children/2", "value": node("w")}])
        self._assert_fails([{"op": "add", "path": "/children/3", "value": node("w")}])
        self._assert_fails([{"op": "remove", "path": "children/0"}])

    def test_append_with_dash_and_insert_at_length(self):
        patch = [
            {"op": "add", "path": "/children/2", "value": node("w")},
            {"op": "add", "path": "/children/-", "value": node("v")},
        ]
        self.store.patch(self.page_id, self.etag, json.dumps(patch))
        doc = json.loads(self.store.read(self.page_id)[0])
        self.assertEqual([c["id"] for c in doc["children"]], ["x", "y", "w", "v"])


if __name__ == "__main__":
    unittest.main()
