"""The faster paths read exactly what the slow ones did: concurrent bridge calls, concurrent rule reads, expat, the snapshot cache."""

from __future__ import annotations

import os
import shutil
import threading
import time
import unittest
import zipfile
from unittest import mock

from tests.helpers import fixture, py_script, temp_dir

from gof import snapshot_cache
from gof.agent_feishu import FeishuServe
from gof.feishu import Bridge, read_rules
from gof.snapshot import Snapshot
from gof.util import Blocked
from gof.xmlparse import _NotSure, _parse_expat, parse_xml, parse_xml_exact


def tree(node):
    return (node.name, node.attrs, node.text, [tree(c) for c in node.children])


class BridgeConcurrencyTests(unittest.TestCase):
    def test_calls_from_several_threads_each_get_their_own_reply(self) -> None:
        # A module that answers in reverse order of arrival, once four requests are in.
        fake = py_script(temp_dir("serve-"), "feishu.py",
                         "import json, sys\n"
                         "held = []\n"
                         "for line in sys.stdin:\n"
                         "    held.append(json.loads(line))\n"
                         "    if len(held) == 4:\n"
                         "        for r in reversed(held):\n"
                         "            print(json.dumps({'id': r['id'], 'reply': {'ok': True, 'data': r['input']}}), flush=True)\n"
                         "        held = []\n")
        serve = FeishuServe({**os.environ, "FEISHU_CLI": fake})
        answers: dict[int, object] = {}

        def ask(n: int) -> None:
            answers[n] = serve.call("api-get", {"n": n})["data"]

        threads = [threading.Thread(target=ask, args=(n,)) for n in range(4)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
            self.assertEqual(answers, {n: {"n": n} for n in range(4)})
        finally:
            serve.close()

    def test_waiting_calls_fail_when_the_process_ends(self) -> None:
        fake = py_script(temp_dir("serve-"), "feishu.py", "import sys\nsys.stdin.readline()\nsys.exit(1)\n")
        serve = FeishuServe({**os.environ, "FEISHU_CLI": fake})
        self.assertRaises(ConnectionError, serve.call, "api-get", {})
        serve.close()


class RuleReadTests(unittest.TestCase):
    sheet = [{"sheet_id": "s1", "grid_properties": {"row_count": 12_000, "column_count": 150}}]

    @mock.patch("gof.feishu.RULE_READS_AT_ONCE", 3)
    def test_concurrent_reads_keep_the_order_of_the_ranges(self) -> None:
        def reply(name, payload):
            rect = payload["params"]["range"].split("!")[1]
            time.sleep(0.05 if rect.startswith("A1:") else 0)  # the first range answers last
            return {"ok": True, "data": {"sheetId": "s1", "dataValidations": [
                {"dataValidationType": "list", "conditionValues": [rect], "ranges": [f"s1!{rect.split(':')[0]}:{rect.split(':')[0]}"]}]}}

        options = [r["options"][0] for r in read_rules(Bridge(reply), "tok", self.sheet)]
        self.assertEqual(options, ["A1:CV5000", "CW1:ET5000", "A5001:CV10000", "CW5001:ET10000", "A10001:CV12000", "CW10001:ET12000"])

    def test_too_many_requests_falls_back_to_one_at_a_time(self) -> None:
        lock = threading.Lock()
        state = {"now": 0, "most": 0, "limited": 0}

        def reply(name, payload):
            rect = payload["params"]["range"].split("!")[1]
            with lock:
                state["now"] += 1
                busy = state["now"]
            try:
                time.sleep(0.02)
                if busy > 1:  # Feishu takes only one at a time
                    with lock:
                        state["limited"] += 1
                    return {"ok": False, "error": {"kind": "api", "code": 90217, "message": "too many request"}}
                return {"ok": True, "data": {"sheetId": "s1", "dataValidations": [
                    {"dataValidationType": "list", "conditionValues": [rect], "ranges": [f"s1!{rect.split(':')[0]}:{rect.split(':')[0]}"]}]}}
            finally:
                with lock:
                    state["now"] -= 1

        with mock.patch("gof.feishu.RATE_PAUSE_S", 0.01), mock.patch("gof.feishu.RULE_READS_AT_ONCE", 3):
            options = [r["options"][0] for r in read_rules(Bridge(reply), "tok", self.sheet)]
        self.assertEqual(options, ["A1:CV5000", "CW1:ET5000", "A5001:CV10000", "CW5001:ET10000", "A10001:CV12000", "CW10001:ET12000"])
        self.assertGreater(state["limited"], 0)

    def test_a_rate_limited_write_is_not_a_read_to_retry(self) -> None:
        from gof.feishu import RateLimited

        bridge = Bridge(lambda n, p: {"ok": False, "error": {"kind": "api", "code": 90217, "message": "too many request"}})
        self.assertRaises(RateLimited, bridge.call, "api-get", {})
        try:
            bridge.call("sheets-cells-set", {}, write=True)
        except Exception as error:  # noqa: BLE001
            self.assertNotIsInstance(error, RateLimited)

    @mock.patch("gof.feishu.RULE_READS_AT_ONCE", 3)
    def test_the_first_bad_range_in_order_is_the_refusal(self) -> None:
        def reply(name, payload):
            rect = payload["params"]["range"].split("!")[1]
            if rect.startswith("A1:"):
                time.sleep(0.05)
                return {"ok": True, "data": {"sheetId": "other", "dataValidations": []}}
            return {"ok": False, "error": {"kind": "api", "message": "later failure"}}

        self.assertRaisesRegex(Blocked, "another sheetId", read_rules, Bridge(reply), "tok", self.sheet)


class ExpatTests(unittest.TestCase):
    def test_same_tree_as_the_exact_reader_on_every_fixture_part(self) -> None:
        checked = 0
        for name in sorted(os.listdir(fixture(""))):
            if not name.endswith(".xlsx"):
                continue
            with zipfile.ZipFile(fixture(name)) as z:
                for part in z.namelist():
                    if part.endswith((".xml", ".rels")):
                        source = z.read(part).decode("utf-8")
                        self.assertEqual(tree(parse_xml(source)), tree(parse_xml_exact(source)), f"{name} {part}")
                        checked += 1
        self.assertGreater(checked, 50)

    def test_what_expat_would_read_differently_goes_to_the_exact_reader(self) -> None:
        for source in ['<a v="x\ty"/>', "<a>x\r\ny</a>", '<!DOCTYPE a [<!ENTITY e "boom">]><a>&e;</a>', "<a>&nbsp;</a>",
                       '<a b="1" b="2"/>', "<a><b></a>", "<a x:y='1' xmlns:x='u'/>" * 1 + "<b/>"]:
            with self.subTest(source=source):
                self.assertRaises(_NotSure, _parse_expat, source)
        # Read by the exact reader as before: the tab is kept, the unknown entity left as it is, the DOCTYPE skipped.
        self.assertEqual(parse_xml('<a v="x\ty"/>').attrs, {"v": "x\ty"})
        self.assertEqual(parse_xml("<a>&nbsp;</a>").text, "&nbsp;")
        self.assertEqual(parse_xml('<!DOCTYPE a [<!ENTITY e "boom">]><a>&e;</a>').text, "&e;")
        self.assertRaisesRegex(Blocked, "XML tags do not match", parse_xml, "<a><b></a>")

    def test_prefixes_and_namespaces_as_the_exact_reader(self) -> None:
        source = '<x:root xmlns:x="u" xmlns="v" x:a="1" b="&amp;&#65;"><x:c>t<![CDATA[<z>]]></x:c><!-- c --><?p q?></x:root>'
        self.assertEqual(tree(parse_xml(source)), tree(parse_xml_exact(source)))
        self.assertEqual(tree(parse_xml(source)), ("root", {"a": "1", "b": "&A"}, "", [("c", {}, "t<z>", [])]))


class SnapshotCacheTests(unittest.TestCase):
    def test_a_second_read_comes_from_the_cache_and_is_the_same(self) -> None:
        user = temp_dir("cache-user-")
        with mock.patch.dict(os.environ, {"AGENT_USER_DIR": user}):
            os.environ.pop("AGENT_USER_OPEN_ID", None)
            tmp = os.path.join(user, "requirement-auto", "tmp")
            os.makedirs(tmp)
            file = os.path.join(tmp, "snap.xlsx")
            shutil.copy(fixture("base.xlsx"), file)
            first = Snapshot(file)
            cached = os.listdir(os.path.join(tmp, snapshot_cache.DIR_NAME))
            self.assertEqual(len(cached), 1)
            copy = os.path.join(tmp, "copy.xlsx")
            shutil.copy(file, copy)
            with mock.patch("gof.snapshot.read_workbook", side_effect=AssertionError("parsed again")):
                second = Snapshot(copy)
            self.assertEqual(second.path, os.path.abspath(copy))
            self.assertEqual((second.fingerprint, second.cells, second.rules, second.sheet_names),
                             (first.fingerprint, first.cells, first.rules, first.sheet_names))
            self.assertIs(second._by_name[second.sheet_names[0]], second.book.sheets[0])

    def test_files_outside_the_data_directory_and_bad_entries_are_parsed(self) -> None:
        user = temp_dir("cache-user-")
        with mock.patch.dict(os.environ, {"AGENT_USER_DIR": user}):
            os.environ.pop("AGENT_USER_OPEN_ID", None)
            Snapshot(fixture("base.xlsx"))
            self.assertFalse(os.path.exists(os.path.join(user, "requirement-auto", "tmp", snapshot_cache.DIR_NAME)))
            tmp = os.path.join(user, "requirement-auto", "tmp")
            os.makedirs(tmp)
            file = os.path.join(tmp, "snap.xlsx")
            shutil.copy(fixture("base.xlsx"), file)
            directory = os.path.join(tmp, snapshot_cache.DIR_NAME)
            os.makedirs(directory)
            import pickle
            import zlib

            # An entry naming anything but the workbook classes is never loaded.
            with open(os.path.join(directory, snapshot_cache.key_of(file) + ".pickle.z"), "wb") as handle:
                handle.write(zlib.compress(pickle.dumps({"x": os.system})))
            self.assertIsNone(snapshot_cache.load(directory, snapshot_cache.key_of(file)))
            self.assertEqual(Snapshot(file).fingerprint, Snapshot(fixture("base.xlsx")).fingerprint)


if __name__ == "__main__":
    unittest.main()
