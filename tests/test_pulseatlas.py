import io
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pulseatlas.model import Device, Selection, Snapshot, display, fresh, number, severity
from pulseatlas.sources import (Collector, DemoCollector, NoRedirect, Worker, fetch_lhm,
                                parse_lhm, parse_nvidia, read_nvidia, sensor_value)


def hardware(identity, name, sensors):
    categories = {}
    for index, (kind, label, value) in enumerate(sensors):
        categories.setdefault(kind, []).append({
            "Text": label, "Type": kind, "RawValue": value,
            "Value": f"{value} MB" if kind == "SmallData" else f"{value} GB" if kind == "Data" else "not used",
            "SensorId": f"{identity}/{kind.lower()}/{index}", "Children": [],
        })
    return {"HardwareId": identity, "Text": name, "Children": [
        {"Text": kind, "Children": leaves} for kind, leaves in categories.items()]}


def tree(*devices):
    return {"Text": "", "Children": [{"Text": "Fixture computer", "Children": list(devices)}]}


def gpu(identity="/gpu-nvidia/0", name="Same GPU", util=20):
    return hardware(identity, name, [("Load", "GPU Core", util), ("Temperature", "GPU Core", 50),
                                     ("SmallData", "GPU Memory Used", 2048), ("Data", "GPU Memory Total", 8)])


class ParserTests(unittest.TestCase):
    def test_same_name_gpus_keep_identity_and_metrics(self):
        cpu, devices = parse_lhm(tree(gpu(), gpu("/gpu-nvidia/1", util=90)))
        self.assertEqual(cpu, {})
        self.assertEqual([d.name for d in devices], ["Same GPU", "Same GPU"])
        self.assertNotEqual(devices[0].id, devices[1].id)
        self.assertEqual([d.metrics["util"] for d in devices], [20, 90])
        self.assertEqual(devices[0].metrics["mem_used"], 2)
        self.assertEqual(devices[0].metrics["mem_total"], 8)

    def test_old_tree_without_hardware_id(self):
        device = gpu("/nvidiagpu/0")
        del device["HardwareId"]
        self.assertEqual(parse_lhm(tree(device))[1][0].id, "lhm:/nvidiagpu/0")

    def test_amd_and_intel_not_dependent_on_nvidia(self):
        _, devices = parse_lhm(tree(gpu("/gpu-amd/0"), gpu("/gpu-intel/0")))
        self.assertEqual(len(devices), 2)

    def test_board_temperature_and_virtual_memory_not_cpu_or_ram(self):
        root = tree(hardware("/lpc/nct/0", "Board", [("Temperature", "CPU Package", 99)]),
                    hardware("/ram/virtual", "Virtual Memory", [("Load", "Memory", 98)]),
                    hardware("/intelcpu/0", "CPU", [("Temperature", "CPU Package", 42)]))
        self.assertEqual(parse_lhm(root)[0]["temp"], 42)
        self.assertNotIn("memory", parse_lhm(root)[0])

    def test_multi_socket_does_not_guess_cpu_temperature(self):
        root = tree(*[hardware(f"/intelcpu/{i}", "CPU", [("Temperature", "CPU Package", 40 + i)]) for i in range(2)])
        self.assertEqual(parse_lhm(root)[0], {})

    def test_duplicate_label_is_unavailable_not_last_wins(self):
        root = tree(hardware("/gpu-amd/0", "GPU", [("Load", "GPU Core", 10), ("Load", "GPU Core", 80)]))
        self.assertIsNone(parse_lhm(root)[1][0].metrics["util"])

    def test_hotspot_and_memory_temperature_are_separate(self):
        root = tree(hardware("/gpu-amd/0", "GPU", [("Temperature", "GPU Hot Spot", 80),
                                                   ("Temperature", "GPU Memory Junction", 70)]))
        m = parse_lhm(root)[1][0].metrics
        self.assertEqual((m["hotspot"], m["memory_temp"]), (80, 70))

    def test_fan_controls_not_rotation_or_percentage(self):
        root = tree(hardware("/gpu-amd/0", "GPU", [("Control", "GPU Fan", 0), ("Fan", "GPU Fan", 1200)]))
        self.assertEqual(parse_lhm(root)[1][0].metrics["fan_rpm"], 1200)

    def test_locale_unknown_units_and_nonfinite_are_not_guessed(self):
        for value in ("1,234 MB", "N/A", "NaN %", "2 TB", "30 RPM", "--"):
            with self.subTest(value=value):
                self.assertIsNone(sensor_value({"Value": value}, "Load"))
        for raw in (float("nan"), float("inf"), True, -1, 101):
            self.assertIsNone(sensor_value({"RawValue": raw}, "Load"))

    def test_old_formatted_values_and_fahrenheit(self):
        self.assertEqual(sensor_value({"Value": "0 %"}, "Load"), 0)
        self.assertAlmostEqual(sensor_value({"Value": "122 °F"}, "Temperature"), 50)
        self.assertEqual(sensor_value({"RawValue": "formatted", "Value": "2.5 W"}, "Power"), 2.5)

    def test_data_raw_unit_changes_do_not_change_capacity(self):
        for raw in (8, 8 * 1024**3):
            self.assertEqual(sensor_value({"RawValue": raw, "Value": "8.0 GB"}, "Data"), 8)
        self.assertEqual(sensor_value({"RawValue": 2048, "Value": "2048 MB"}, "SmallData"), 2)
        self.assertEqual(sensor_value({"Value": "1073741824 B"}, "Data"), 1)
        self.assertIsNone(sensor_value({"RawValue": 8}, "Data"))
        self.assertIsNone(sensor_value({"RawValue": 8192, "Value": "8,192 MB"}, "Data"))

    def test_invalid_and_deep_tree_rejected(self):
        with self.assertRaises(ValueError):
            parse_lhm({"Children": "wrong"})
        root = {}
        for _ in range(34):
            root = {"Children": [root]}
        with self.assertRaises(ValueError):
            parse_lhm(root)

    def test_nvidia_uuid_quoted_name_and_unavailable_values(self):
        devices = parse_nvidia('GPU-2,"Model, GPU",75,50,2048,8192,65,30,N/A\nGPU-1,Other,N/A,N/A,N/A,N/A,N/A,N/A,N/A')
        self.assertEqual([d.id for d in devices], ["smi:GPU-1", "smi:GPU-2"])
        self.assertEqual(devices[1].name, "Model, GPU")
        self.assertEqual(devices[1].metrics["mem_used"], 2)
        self.assertIsNone(devices[1].metrics["hotspot"])
        self.assertIsNone(devices[0].metrics["util"])

    def test_duplicate_nvidia_uuid_rejected(self):
        line = "GPU-1,Model,1,2,3,4,5,6,N/A"
        with self.assertRaises(ValueError):
            parse_nvidia(line + "\n" + line)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.devices = parse_lhm(tree(gpu(), gpu("/gpu-nvidia/1", util=90)))[1]
        self.selection = Selection()

    def test_choose_one_then_reorder(self):
        self.assertEqual(self.selection.resolve(self.devices), self.devices[0])
        self.selection.choose(self.devices[1].id)
        self.assertEqual(self.selection.resolve(list(reversed(self.devices))).metrics["util"], 90)

    def test_disconnect_and_restore_preserve_selection(self):
        self.selection.choose(self.devices[1].id)
        self.assertIsNone(self.selection.resolve(self.devices[:1]))
        self.assertEqual(self.selection.device_id, self.devices[1].id)
        self.assertEqual(self.selection.resolve(self.devices), self.devices[1])

    def test_never_match_between_sources_by_name(self):
        self.selection.resolve(self.devices)
        self.assertIsNone(self.selection.resolve([Device("smi:GPU-1", "Same GPU", "nvidia-smi", {"util": 99})]))

    def test_expiry_removes_all_readings(self):
        snapshot = Snapshot(10, {"util": 20}, {"util": 30}, self.devices)
        self.assertIs(fresh(snapshot, 16), snapshot)
        self.assertEqual(fresh(snapshot, 16.1).gpus, [])
        self.assertEqual(fresh(snapshot, 16.1).cpu, {})
        self.assertEqual(fresh(snapshot, 9).gpus, [])
        self.assertEqual(fresh(None, 1).memory, {})

    def test_display_missing_is_not_zero(self):
        self.assertEqual(display(None), "—")
        self.assertEqual(display(0, "%"), "0%")
        self.assertIsNone(number(float("inf")))

    def test_hot_temperature_alert_even_when_idle(self):
        self.assertEqual(severity(5, 90), 2)
        self.assertEqual(severity(80, 40), 1)


class SourceTests(unittest.TestCase):
    def stats(self):
        return Mock(read=Mock(return_value=({"util": 25}, {"util": 50})))

    def test_lhm_primary_and_windows_physical_memory(self):
        nvidia = Mock(side_effect=AssertionError("must not query fallback"))
        c = Collector(clock=lambda: 12, lhm=lambda: tree(gpu()), nvidia=nvidia, stats=self.stats())
        snap = c.collect()
        self.assertEqual(snap.memory["util"], 50)
        self.assertEqual(snap.gpus[0].source, "LHM")
        self.assertEqual(snap.timestamp, 12)
        nvidia.assert_not_called()

    def test_failure_clears_old_temperatures(self):
        c = Collector(lhm=Mock(side_effect=[tree(gpu()), OSError("offline")]),
                      nvidia=lambda: [], stats=self.stats())
        self.assertEqual(len(c.collect().gpus), 1)
        second = c.collect()
        self.assertEqual(second.gpus, [])
        self.assertNotIn("temp", second.cpu)
        self.assertIn("不可用", second.status)

    def test_nvidia_fallback_without_cross_device_merge(self):
        fallback = Device("smi:GPU-1", "GPU", "nvidia-smi", {"util": 10})
        c = Collector(lhm=Mock(side_effect=OSError()), nvidia=lambda: [fallback], stats=self.stats())
        self.assertEqual(c.collect().gpus, [fallback])

    def test_fallback_timeout_is_unavailable(self):
        c = Collector(lhm=lambda: {}, stats=self.stats(),
                      nvidia=Mock(side_effect=subprocess.TimeoutExpired("test", 2)))
        self.assertEqual(c.collect().gpus, [])

    def test_http_loopback_only_no_proxy_no_redirect(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"Children": []}'
        opener = Mock(open=Mock(return_value=response))
        with patch("pulseatlas.sources.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(fetch_lhm(8085), {"Children": []})
        opener.open.assert_called_once_with("http://127.0.0.1:8085/data.json", timeout=1.0)
        self.assertEqual(build.call_args.args[0].proxies, {})
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://external.invalid/")

    def test_http_bad_port_and_response_size(self):
        for port in (0, 65536, "8085", True):
            with self.assertRaises(ValueError):
                fetch_lhm(port)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * (2 * 1024 * 1024 + 1)
        with patch("pulseatlas.sources.urllib.request.build_opener", return_value=Mock(open=Mock(return_value=response))):
            with self.assertRaises(ValueError):
                fetch_lhm()

    def test_nvidia_command_hidden_bounded_and_not_hotspot(self):
        result = subprocess.CompletedProcess([], 0, "GPU-1,Example,10,40,2048,8192,55,0\n", "")
        with patch("pulseatlas.sources.shutil.which", return_value="nvidia-smi"), \
                patch("pulseatlas.sources.subprocess.run", return_value=result) as run:
            devices = read_nvidia()
        self.assertEqual(devices[0].metrics["fan_percent"], 0)
        self.assertNotIn("temperature.memory", str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_demo_has_no_real_io_and_explicit_disconnect(self):
        with patch("pulseatlas.sources.fetch_lhm", side_effect=AssertionError()), \
                patch("pulseatlas.sources.subprocess.run", side_effect=AssertionError()):
            demo = DemoCollector()
            snap = demo.collect()
            self.assertEqual(len(snap.gpus), 2)
            self.assertEqual(snap.gpus[0].name, snap.gpus[1].name)
            demo.disconnected = True
            self.assertEqual(demo.collect().gpus, [])

    def test_worker_reads_off_caller_thread_and_stops(self):
        sampled = threading.Event()
        threads = []
        def collect():
            threads.append(threading.get_ident())
            sampled.set()
            return Snapshot(time.monotonic())
        worker = Worker(Mock(collect=collect))
        worker.thread.start()
        try:
            self.assertTrue(sampled.wait(1))
            self.assertNotEqual(threads[0], threading.get_ident())
        finally:
            worker.close()
            worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
