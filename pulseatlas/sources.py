"""Read-only sources. No elevation, driver installation, or service configuration."""
import csv
import ctypes
from ctypes import wintypes
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from .model import Device, Snapshot, number


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("LHM redirect refused")


def fetch_lhm(port=8085):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid loopback port")
    # Fixed IP, no environment proxies, redirects, credentials or remote endpoints.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://127.0.0.1:{port}/data.json", timeout=1.0) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("LHM response too large")
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("LHM response must be a tree")
    return result


def sensor_value(node, kind):
    if kind in {"Data", "SmallData"}:
        # LHM changed raw Data from GiB to bytes. The formatted unit is explicit
        # across versions; accept its display precision rather than guess raw units.
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*",
                             str(node.get("Value", "")))
        if not match:
            return None
        powers = {"B": -3, "KB": -2, "KiB": -2, "MB": -1, "MiB": -1,
                  "GB": 0, "GiB": 0, "TB": 1, "TiB": 1}
        return number(float(match[1]) * 1024 ** powers[match[2]])
    raw = node.get("RawValue")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = number(raw)
    else:
        # Reject ambiguous/localized text rather than turning '1,234' into 1.
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(%|°C|°F|C|W|RPM|MB|GB)\s*",
                             str(node.get("Value", "")))
        if not match:
            return None
        value, unit = float(match[1]), match[2]
        expected = {"Load": {"%"}, "Temperature": {"°C", "°F", "C"},
                    "Power": {"W"}, "Fan": {"RPM"}, "SmallData": {"MB"}, "Data": {"GB"}}
        if unit not in expected.get(kind, set()):
            return None
        if unit == "°F":
            value = (value - 32) * 5 / 9
    if value is None or (kind != "Temperature" and value < 0):
        return None
    if kind == "Load" and value > 100:
        return None
    return value


def parse_lhm(tree):
    """Group by hardware identifier; older trees derive it from SensorId."""
    groups = {}
    stack = [(tree, (), 0)]
    nodes = 0
    while stack:
        node, ancestors, depth = stack.pop()
        nodes += 1
        if nodes > 20000 or depth > 32 or not isinstance(node, dict):
            raise ValueError("unsupported LHM tree")
        text = str(node.get("Text", ""))
        sid = node.get("SensorId")
        if isinstance(sid, str) and sid.count("/") >= 4:
            hardware_id = sid.rsplit("/", 2)[0]
            name = ancestors[-2] if len(ancestors) >= 2 else hardware_id
            group = groups.setdefault(hardware_id, {"name": name, "sensors": []})
            kind = str(node.get("Type", ""))
            group["sensors"].append((kind, text.casefold(), sensor_value(node, kind)))
        children = node.get("Children", [])
        if not isinstance(children, list):
            raise ValueError("invalid LHM children")
        for child in reversed(children):
            stack.append((child, (*ancestors, text), depth + 1))

    def pick(group, candidates):
        for kind, label in candidates:
            values = [v for k, n, v in group["sensors"] if k == kind and n == label.casefold()]
            # Duplicate names are not silently overwritten, even inside a device.
            if len(values) > 1:
                return None
            if values and values[0] is not None:
                return values[0]
        return None

    gpus, cpus = [], []
    for key, group in sorted(groups.items()):
        family = key.strip("/").split("/")[0].lower()
        if family in {"gpu-nvidia", "gpu-amd", "gpu-intel", "nvidiagpu", "amdgpu", "intelgpu"}:
            metrics = {
                "util": pick(group, [("Load", "GPU Core"), ("Load", "GPU 3D")]),
                "temp": pick(group, [("Temperature", "GPU Core"), ("Temperature", "GPU Temperature")]),
                "hotspot": pick(group, [("Temperature", "GPU Hot Spot")]),
                "memory_temp": pick(group, [("Temperature", "GPU Memory Junction"), ("Temperature", "GPU Memory")]),
                "power": pick(group, [("Power", "GPU Package"), ("Power", "GPU Total")]),
                "fan_rpm": pick(group, [("Fan", "GPU Fan"), ("Fan", "GPU Fan 1")]),
            }
            for metric, label in (("mem_used", "GPU Memory Used"), ("mem_total", "GPU Memory Total")):
                metrics[metric] = pick(group, [("SmallData", label), ("Data", label)])
            gpus.append(Device("lhm:" + key, group["name"], "LHM", metrics))
        elif family in {"intelcpu", "amdcpu"}:
            cpus.append({
                "temp": pick(group, [("Temperature", "CPU Package"), ("Temperature", "CPU (Tctl/Tdie)"),
                                     ("Temperature", "Core (Tctl/Tdie)")]),
                "power": pick(group, [("Power", "CPU Package"), ("Power", "Package")]),
            })
    # Multi-socket aggregation requires a separate explicit design; don't guess.
    return (cpus[0] if len(cpus) == 1 else {}), gpus


def parse_nvidia(text):
    devices = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 9:
            continue
        uuid, name, *fields = (s.strip() for s in row)
        if not uuid.startswith("GPU-") or uuid in devices:
            raise ValueError("ambiguous NVIDIA identity")
        util, temp, used, total, power, fan, mem_temp = map(number, fields)
        def nonnegative(v):
            return v if v is not None and v >= 0 else None
        devices[uuid] = Device("smi:" + uuid, name or uuid, "nvidia-smi", {
            "util": util if util is not None and 0 <= util <= 100 else None,
            "temp": temp, "mem_used": nonnegative(used) / 1024 if nonnegative(used) is not None else None,
            "mem_total": nonnegative(total) / 1024 if nonnegative(total) is not None else None,
            "power": nonnegative(power), "fan_percent": fan if fan is not None and 0 <= fan <= 100 else None,
            "memory_temp": mem_temp, "hotspot": None,
        })
    return sorted(devices.values(), key=lambda d: d.id)


def read_nvidia():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    # Memory temperature isn't universally supported. Do not query it or call it hotspot.
    fields = "uuid,name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw,fan.speed"
    result = subprocess.run([executable, "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=2.0, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        return []
    # Supply an unavailable memory-temperature column, not a fabricated reading.
    return parse_nvidia("\n".join(line + ", N/A" for line in result.stdout.splitlines()))


class WindowsStats:
    def __init__(self):
        self.previous = None

    def read(self):
        if os.name != "nt":
            return {}, {}
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        idle, system, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        cpu, memory = {}, {}
        kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        kernel.GetSystemTimes.restype = wintypes.BOOL
        if kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            def ticks(v):
                return (v.dwHighDateTime << 32) | v.dwLowDateTime
            current = ticks(idle), ticks(system) + ticks(user)
            if self.previous and current[1] > self.previous[1]:
                util = 100 * (1 - (current[0] - self.previous[0]) / (current[1] - self.previous[1]))
                cpu["util"] = max(0.0, min(100.0, util))
            self.previous = current
        else:
            self.previous = None

        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (n, ctypes.c_ulonglong) for n in ("total", "available", "page_total", "page_available",
                                                "virtual_total", "virtual_available", "extended")]
        m = Memory()
        m.length = ctypes.sizeof(m)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Memory)]
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if kernel.GlobalMemoryStatusEx(ctypes.byref(m)):
            memory = {"util": float(m.load), "used": (m.total - m.available) / 2**30, "total": m.total / 2**30}
        return cpu, memory


class Collector:
    def __init__(self, port=8085, clock=time.monotonic, lhm=None, nvidia=read_nvidia, stats=None):
        self.clock, self.lhm, self.nvidia = clock, lhm or (lambda: fetch_lhm(port)), nvidia
        self.stats = stats or WindowsStats()

    def collect(self):
        started = self.clock()
        cpu, memory = self.stats.read()
        status = "LHM 不可用 · CPU/RAM 为 Windows API"
        gpus = []
        try:
            extra, gpus = parse_lhm(self.lhm())
            cpu.update(extra)
            status = "LHM · CPU/RAM 占用为 Windows API"
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
        if not gpus:
            try:
                gpus = self.nvidia()
            except (OSError, ValueError, subprocess.SubprocessError):
                gpus = []
            status += " · GPU: " + ("nvidia-smi" if gpus else "不可用")
        return Snapshot(started, cpu, memory, gpus, status)


class Worker:
    def __init__(self, collector):
        self.collector = collector
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.thread = threading.Thread(target=self._run, name="PulseAtlas sampler", daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                snapshot = self.collector.collect()
            except Exception:
                snapshot = Snapshot(time.monotonic(), status="采集失败 · 不显示旧读数")
            with self.lock:
                self.latest = snapshot
            self.stop.wait(2.0)

    def snapshot(self):
        with self.lock:
            return self.latest

    def close(self):
        self.stop.set()
        # Let a bounded, in-flight nvidia-smi call reap its own child before exit.
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=4.0)


class DemoCollector:
    """Synthetic metadata only: never probes hardware, network or processes."""
    def __init__(self):
        self.disconnected = False

    def collect(self):
        devices = [] if self.disconnected else [
            Device("demo:gpu-a", "示例显卡", "演示", {"util": 24, "temp": 48, "power": 65, "mem_used": 2, "mem_total": 8}),
            Device("demo:gpu-b", "示例显卡", "演示", {"util": 91, "temp": 86, "power": 180, "mem_used": 6, "mem_total": 12}),
        ]
        return Snapshot(time.monotonic(), {"util": 18, "temp": 46, "power": 35},
                        {"util": 37.5, "used": 12, "total": 32}, devices, "演示数据 · 不读取硬件")
