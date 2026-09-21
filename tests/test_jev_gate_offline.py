"""离线验证 jev_gate 的降级行为：不发起任何真实请求。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, r"F:\Projects\Self_Learning_Agent")

from study import jev_gate


class GateDegradation(unittest.TestCase):
    HITS = [{"id": 1, "text": "相关段落：租赁合同解除的条件包括……"},
            {"id": 2, "text": "无关段落：公司历史介绍。"}]

    def test_unconfigured_passthrough(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            jev_gate._enabled_cache = None
            out = jev_gate.apply("问", self.HITS)
            self.assertEqual(len(out), 2)

    def test_toggle_off_passthrough(self):
        with patch.dict(os.environ, {"STUDY_JEV_GATE": "0", "TYPESAFE_API_KEY": "k"}):
            jev_gate._enabled_cache = None
            out = jev_gate.apply("问", self.HITS)
            self.assertEqual(len(out), 2)

    def test_network_error_passthrough(self):
        with patch.dict(os.environ, {"STUDY_JEV_GATE": "1", "TYPESAFE_API_KEY": "k"}):
            jev_gate._enabled_cache = None
            logs = []
            with patch("requests.post", side_effect=ConnectionError("offline")):
                out = jev_gate.apply("问", self.HITS,
                                     log_fn=lambda e, l, d: logs.append((e, l, d)))
            self.assertEqual(len(out), 2)
            self.assertTrue(any("调用失败" in d for _, l, d in logs if l == "warning"))

    def test_empty_hits(self):
        self.assertEqual(jev_gate.apply("问", []), [])

    def test_bad_shape_passthrough(self):
        with patch.dict(os.environ, {"STUDY_JEV_GATE": "1", "TYPESAFE_API_KEY": "k"}):
            jev_gate._enabled_cache = None
            class FakeResp:
                def raise_for_status(self): pass
                def json(self): return {"unexpected": True}
                def __enter__(self): return self
                def __exit__(self, *a): return False
            with patch("requests.post", return_value=FakeResp()):
                out = jev_gate.apply("问", self.HITS)
            self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
