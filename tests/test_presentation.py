"""Exercise the actual presenter with widget doubles, without opening a desktop."""
import time
import unittest
from unittest.mock import Mock

from pulseatlas.__main__ import Monitor
from pulseatlas.model import Selection, Snapshot
from pulseatlas.sources import DemoCollector


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor.__new__(Monitor)
        self.monitor.selection = Selection()
        self.monitor.last_name = "所选显卡"
        self.monitor.combo = Mock()
        self.monitor.labels = {key: Mock() for key in ("cpu", "gpu", "memory")}
        self.monitor.status = Mock()
        self.snapshot = DemoCollector().collect()
        self.monitor.worker = Mock(snapshot=lambda: self.snapshot)

    def text(self, key):
        return next(call.kwargs["text"] for call in reversed(self.monitor.labels[key].configure.call_args_list)
                    if "text" in call.kwargs)

    def test_selection_renders_one_whole_device(self):
        self.monitor.render()
        self.assertIn("24%", self.text("gpu"))
        self.assertNotIn("91%", self.text("gpu"))
        self.monitor.combo.current.return_value = 1
        self.monitor.choose()
        self.assertIn("91%", self.text("gpu"))
        self.assertIn("180 W", self.text("gpu"))
        self.assertNotIn("65 W", self.text("gpu"))
        self.assertEqual(self.monitor.labels["gpu"].configure.call_args.kwargs["fg"], "#ff7b72")

    def test_disconnect_keeps_dropdown_selection_but_clears_metrics(self):
        self.monitor.render()
        self.snapshot = Snapshot(time.monotonic(), gpus=[])
        self.monitor.render()
        self.assertEqual(self.monitor.identities, ["demo:gpu-a"])
        self.assertIn("不可用", self.text("gpu"))
        self.assertNotIn("48", self.text("gpu"))
        self.snapshot = DemoCollector().collect()
        self.monitor.render()
        self.assertIn("24%", self.text("gpu"))

    def test_source_change_does_not_silently_select_new_device(self):
        self.monitor.render()
        self.snapshot.gpus[0].id = "smi:GPU-new"
        self.monitor.render()
        self.assertIn("不可用", self.text("gpu"))
        self.assertEqual(self.monitor.selection.device_id, "demo:gpu-a")

    def test_expired_snapshot_clears_cpu_and_gpu(self):
        self.monitor.render()
        self.snapshot.timestamp = time.monotonic() - 7
        self.monitor.render()
        self.assertIn("—", self.text("cpu"))
        self.assertIn("不可用", self.text("gpu"))
        self.assertIn("数据过期", self.monitor.status.configure.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
