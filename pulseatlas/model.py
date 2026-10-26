"""Device identity and snapshot selection, independent of GUI and hardware."""
from dataclasses import dataclass, field
import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


@dataclass
class Device:
    id: str
    name: str
    source: str
    metrics: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    timestamp: float
    cpu: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    gpus: list = field(default_factory=list)
    status: str = "等待采集"


class Selection:
    """Pin an identity, not its list position, name, or presumed cross-source match."""
    def __init__(self):
        self.device_id = None

    def choose(self, device_id):
        self.device_id = device_id

    def resolve(self, devices):
        if self.device_id is None and devices:
            self.device_id = devices[0].id
        return next((d for d in devices if d.id == self.device_id), None)


def fresh(snapshot, now, ttl=6.0):
    if snapshot is None or not 0 <= now - snapshot.timestamp <= ttl:
        return Snapshot(now, status="数据过期或尚未采集 · 不显示旧读数")
    return snapshot


def display(value, suffix="", precision=0):
    value = number(value)
    return "—" if value is None else f"{value:.{precision}f}{suffix}"


def severity(util=None, temperature=None):
    """Display hints, not vendor-specific hardware safety limits."""
    scores = [0]
    for value, warn, crit in ((util, 60, 85), (temperature, 72, 85)):
        value = number(value)
        if value is not None:
            scores.append(2 if value >= crit else 1 if value >= warn else 0)
    return max(scores)
