"""录制与导出。

热路径只做两件便宜事：原始字节 tee 进 gzip、解析后的帧按设备追加到内存块列表。CSV
留给离线 export 命令生成，避免每秒上万次的字符串格式化挤占采集线程。

时标存的是"相对会话起点秒数"，绝对起点 UTC 时刻写进 manifest.json。
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .protocol import FrameEvent, ImuFrame, SemgFrame

IMU_FIELDS = ["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "mx_raw", "my_raw", "mz_raw"]


@dataclass
class Recording:
    root: Path
    source_description: str = ""
    config: dict = field(default_factory=dict)
    channel_map: dict = field(default_factory=dict)
    max_samples_per_device: int = 8_000_000
    raw_enabled: bool = True
    parsed_enabled: bool = True

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "parsed").mkdir(exist_ok=True)
        self.started_wall = dt.datetime.now(dt.timezone.utc)
        self._t_ref: float | None = None
        self._raw = gzip.open(self.root / "raw.log.gz", "wb", compresslevel=1) if self.raw_enabled else None
        self._bytes_written = 0
        self._blocks: dict[str, list[np.ndarray]] = {}
        self._times: dict[str, list[np.ndarray]] = {}
        self._kinds: dict[str, str] = {}
        self._truncated: set[str] = set()
        self._samples = 0
        self.closed = False

    @property
    def raw_path(self) -> Path:
        return self.root / "raw.log.gz"

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def samples_written(self) -> int:
        return self._samples

    def write_raw(self, chunk: bytes) -> None:
        if self._raw is None or not chunk:
            return
        self._raw.write(chunk)
        self._bytes_written += len(chunk)

    def record(self, ev: FrameEvent, times: np.ndarray) -> None:
        if not self.parsed_enabled:
            return
        if self._t_ref is None:
            self._t_ref = float(ev.t_arrival)
        ref = self._t_ref
        key = ev.mac.lower()
        if isinstance(ev, ImuFrame):
            rel = times[: ev.samples.shape[0]] - ref
            block = ev.samples
        elif isinstance(ev, SemgFrame):
            rel = times[: ev.values.size] - ref
            block = ev.values
        else:  # pragma: no cover - 解析器只会产出上面两类
            return
        if key not in self._kinds:
            self._kinds[key] = ev.kind
            self._blocks[key] = []
            self._times[key] = []
        total = sum(b.shape[0] for b in self._blocks[key])
        if total >= self.max_samples_per_device:
            self._truncated.add(key)
            return
        self._blocks[key].append(block)
        self._times[key].append(rel.astype(np.float64))
        self._samples += int(block.shape[0])

    def close(self) -> list[Path]:
        if self.closed:
            return []
        self.closed = True
        written: list[Path] = []
        if self._raw is not None:
            self._raw.close()
            written.append(self.raw_path)
        npz_path: Path | None = None
        if self.parsed_enabled and self._blocks:
            payload: dict[str, np.ndarray] = {}
            for key, blocks in self._blocks.items():
                kind = self._kinds[key]
                values = np.concatenate(blocks)
                times = np.concatenate(self._times[key])
                if kind == "imu":
                    for i, name in enumerate(IMU_FIELDS):
                        payload[f"{name}@{key}"] = values[:, i]
                    payload[f"t@{key}"] = times
                else:
                    payload[f"semg@{key}"] = values
                    payload[f"t@{key}"] = times
            npz_path = self.root / "parsed" / "data.npz"
            np.savez_compressed(npz_path, **payload)
            written.append(npz_path)
        manifest = {
            "started_utc": self.started_wall.isoformat(),
            "t_ref_seconds_offset": 0.0,
            "source": self.source_description,
            "channel_map": self.channel_map,
            "raw_bytes": self._bytes_written,
            "parsed_samples": self._samples,
            "devices": {k: self._kinds.get(k, "semg") for k in self._blocks},
            "truncated_devices": sorted(self._truncated),
            "config": self.config,
        }
        mpath = self.root / "manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(mpath)
        return written


def export_csv(session_root: Path | str, columns: list[str] | None = None) -> Path:
    """把一次录制会话的 npz 展平成按时间对齐的 CSV。

    各设备采样率不同，长表（device, t, field, value）会大到没法看，这里按时间做前向
    对齐的宽表：sEMG 通道按自身采样时刻成行，IMU 采样点单独成行，缺失列为空。
    """
    session_root = Path(session_root)
    npz_path = session_root / "parsed" / "data.npz"
    if not npz_path.is_file():
        raise FileNotFoundError(npz_path)
    data = np.load(npz_path)
    t_keys = sorted(k for k in data.files if k.startswith("t@"))
    rows: list[tuple[float, str, str, float]] = []
    for tk in t_keys:
        key = tk.split("@", 1)[1]
        times = data[tk]
        if f"semg@{key}" in data.files:
            for t, v in zip(times, data[f"semg@{key}"]):
                rows.append((float(t), key, "semg_raw", float(v)))
        for name in IMU_FIELDS:
            col = f"{name}@{key}"
            if col in data.files:
                for t, v in zip(times, data[col]):
                    rows.append((float(t), key, name, float(v)))
    if columns:
        rows = [r for r in rows if r[1] in columns or r[2] in columns]
    rows.sort(key=lambda r: r[0])

    out = session_root / "parsed" / "data_long.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_seconds", "device_mac", "signal", "value"])
        w.writerows(rows)
    return out


def new_session_dir(base: Path | str = "records") -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(base) / f"rec-{stamp}"
    n = 1
    while root.exists():
        n += 1
        root = Path(base) / f"rec-{stamp}-{n}"
    return root
