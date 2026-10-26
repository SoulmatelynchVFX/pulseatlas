"""Run with python -m pulseatlas [--demo] [--lhm-port 8085]."""
import argparse
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import os
import sys
import time

from .model import Selection, display, fresh, severity
from .sources import Collector, DemoCollector, Worker


class Instance:
    """Per-user/session application mutex; never touches another Python process."""
    def __init__(self, demo=False):
        self.handle = None
        self.kernel = None
        self.already_running = False
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            who = hashlib.sha256(getpass.getuser().encode("utf-8")).hexdigest()[:16]
            ctypes.set_last_error(0)
            self.handle = self.kernel.CreateMutexW(None, False, f"Local\\PulseAtlas-{who}-{'demo' if demo else 'live'}")
            error = ctypes.get_last_error()
            if not self.handle:
                raise OSError(error, "无法创建 PulseAtlas 实例锁")
            self.already_running = error == 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Monitor:
    def __init__(self, root, collector):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.collector = root, collector
        self.selection = Selection()
        self.worker = Worker(collector)
        self.closed, self.identities = False, []
        self.last_name = "所选显卡"
        self.root.title("PulseAtlas · 演示" if isinstance(collector, DemoCollector) else "PulseAtlas")
        self.root.configure(bg="#0d1117")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.resizable(False, False)
        self.root.geometry("+40+80")
        self.topmost = tk.BooleanVar(value=True)
        self.alpha = 0.94
        outer = tk.Frame(root, bg="#0d1117", padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="PULSEATLAS", bg="#0d1117", fg="#58a6ff", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.body = tk.Frame(outer, bg="#0d1117")
        self.body.pack(fill="x")
        tk.Label(self.body, text="选择显卡（一次显示一张）", bg="#0d1117", fg="#aeb8c5").pack(anchor="w", pady=(10, 3))
        self.combo = ttk.Combobox(self.body, state="readonly", width=48, takefocus=True)
        self.combo.pack(fill="x")
        self.combo.bind("<<ComboboxSelected>>", self.choose)
        self.labels = {}
        for key in ("cpu", "gpu", "memory"):
            label = tk.Label(self.body, text="等待采集", bg="#161b22", fg="#e6edf3", justify="left",
                             anchor="w", padx=10, pady=10, font=("Segoe UI", 10), wraplength=370)
            label.pack(fill="x", pady=(8, 0))
            self.labels[key] = label
        self.status = tk.Label(outer, text="等待采集", bg="#0d1117", fg="#aeb8c5", wraplength=370, justify="left")
        self.status.pack(anchor="w", pady=(10, 4))
        self.menu = tk.Menu(root, tearoff=False)
        self.menu.add_checkbutton(label="置顶", variable=self.topmost,
                                  command=lambda: root.attributes("-topmost", self.topmost.get()))
        self.menu.add_command(label="切换透明度", command=self.transparency)
        self.menu.add_command(label="折叠 / 展开", command=self.collapse)
        self.menu.add_command(label="退出", command=self.close)
        ttk.Button(outer, text="折叠 / 展开", command=self.collapse).pack(side="left")
        ttk.Button(outer, text="设置", command=self.show_menu).pack(side="left", padx=6)
        ttk.Button(outer, text="退出", command=self.close).pack(side="right")
        if isinstance(collector, DemoCollector):
            ttk.Button(self.body, text="模拟断开 / 恢复显卡", command=self.toggle_demo).pack(fill="x", pady=(8, 0))
        root.bind("<Button-3>", self.show_menu)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker.thread.start()
        self.tick()

    def show_menu(self, event=None):
        try:
            self.menu.tk_popup(event.x_root if event else self.root.winfo_rootx() + 20,
                               event.y_root if event else self.root.winfo_rooty() + 50)
        finally:
            self.menu.grab_release()

    def transparency(self):
        choices = [0.94, 0.75, 0.55, 1.0]
        self.alpha = choices[(choices.index(self.alpha) + 1) % len(choices)]
        self.root.attributes("-alpha", self.alpha)

    def collapse(self):
        if self.body.winfo_manager():
            self.body.pack_forget()
        else:
            self.body.pack(fill="x", before=self.status)

    def toggle_demo(self):
        self.collector.disconnected = not self.collector.disconnected

    def choose(self, event=None):
        index = self.combo.current()
        if 0 <= index < len(self.identities):
            self.selection.choose(self.identities[index])
            self.render()

    def render(self):
        snapshot = fresh(self.worker.snapshot(), time.monotonic())
        gpu = self.selection.resolve(snapshot.gpus)
        self.identities = [d.id for d in snapshot.gpus]
        labels = [f"{d.name}  [{d.id}]" for d in snapshot.gpus]
        if gpu:
            self.last_name = gpu.name
        elif self.selection.device_id is not None:
            self.identities.insert(0, self.selection.device_id)
            labels.insert(0, f"{self.last_name} [不可用 · {self.selection.device_id}]")
        self.combo.configure(values=labels)
        if self.selection.device_id in self.identities:
            self.combo.current(self.identities.index(self.selection.device_id))
        else:
            self.combo.set("暂无可用显卡")
        cpu, mem = snapshot.cpu, snapshot.memory
        self.labels["cpu"].configure(text=f"CPU  {display(cpu.get('util'), '%')}\n"
                                    f"温度 {display(cpu.get('temp'), ' °C')}  ·  功耗 {display(cpu.get('power'), ' W')}")
        self.labels["memory"].configure(text=f"RAM  {display(mem.get('util'), '%')}\n"
                                       f"{display(mem.get('used'), precision=1)} / {display(mem.get('total'), ' GiB', 1)}")
        if gpu:
            m = gpu.metrics
            details = [f"{gpu.name}  ·  {gpu.source}",
                       f"占用 {display(m.get('util'), '%')}  ·  温度 {display(m.get('temp'), ' °C')}",
                       f"显存 {display(m.get('mem_used'), precision=1)} / {display(m.get('mem_total'), ' GiB', 1)}",
                       f"功耗 {display(m.get('power'), ' W')}  ·  热点 {display(m.get('hotspot'), ' °C')}"]
            if m.get("memory_temp") is not None:
                details.append(f"显存温度 {display(m['memory_temp'], ' °C')}")
            if m.get("fan_rpm") is not None:
                details.append(f"风扇 {display(m['fan_rpm'], ' RPM')}")
            elif m.get("fan_percent") is not None:
                details.append(f"风扇 {display(m['fan_percent'], '%')}")
            self.labels["gpu"].configure(text="\n".join(details))
        else:
            m = {}
            self.labels["gpu"].configure(text=f"{self.last_name} · 不可用\n未显示旧读数，可从上方选择其他设备")
        colors = ["#e6edf3", "#e3b341", "#ff7b72"]
        for key, metrics in (("cpu", cpu), ("gpu", m), ("memory", mem)):
            self.labels[key].configure(fg=colors[severity(metrics.get("util"), metrics.get("temp"))])
        self.status.configure(text=snapshot.status)

    def tick(self):
        if not self.closed:
            self.render()
            self.root.after(500, self.tick)

    def close(self):
        self.closed = True
        self.worker.close()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="PulseAtlas · Windows 只读硬件监控悬浮窗")
    parser.add_argument("--demo", action="store_true", help="仅合成双显卡演示，不读取硬件或网络")
    parser.add_argument("--lhm-port", type=int, default=8085, help="本机 LHM HTTP 端口，默认 8085")
    args = parser.parse_args(argv)
    if not 1 <= args.lhm_port <= 65535:
        parser.error("端口须为 1–65535")
    if os.name != "nt" and not args.demo:
        parser.error("真实采集只支持 Windows；可使用 --demo")
    instance = Instance(args.demo)
    try:
        if instance.already_running:
            print("PulseAtlas 已在当前用户会话运行。")
            return 0
        import tkinter as tk
        root = tk.Tk()
        Monitor(root, DemoCollector() if args.demo else Collector(args.lhm_port))
        root.mainloop()
        return 0
    finally:
        instance.close()


if __name__ == "__main__":
    sys.exit(main())
