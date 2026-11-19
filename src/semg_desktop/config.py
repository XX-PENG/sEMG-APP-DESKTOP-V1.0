from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SerialConfig:
    port: str | None = None
    baudrate: int = 2_000_000
    read_bytes: int = 8192
    timeout_seconds: float = 0.1


@dataclass
class TcpConfig:
    host: str = "192.168.4.1"
    port: int = 3333
    read_bytes: int = 8192
    timeout_seconds: float = 0.5


@dataclass
class ProtocolConfig:
    frame_period_seconds: float = 0.02
    semg_nominal_rate: float = 2000.0
    imu_nominal_rate: float = 500.0
    pll_gain: float = 0.02
    resync_threshold_seconds: float = 0.30
    max_frame_gap: int = 256


@dataclass
class SemgConfig:
    channel_count: int = 8
    baseline_offset: float = 4000.0
    auto_range: bool = True
    colors: list[str] = field(default_factory=lambda: ["#ffffff"] * 8)


@dataclass
class ImuConfig:
    show_magnetometer: bool = True
    attitude_cutoff_hz: float = 0.3  # 重力校正陀螺的互补滤波交越频率；越低越跟手、越易漂


@dataclass
class AnalyticsConfig:
    envelope_cutoff_hz: float = 4.0
    stats_window_seconds: float = 1.0
    output_rate_hz: float = 200.0
    psd_samples: int = 2048


@dataclass
class OtaConfig:
    """EMG / IMU 节点 BLE 升级参数（协议见 ota.py）。"""

    receiver_name_prefix: str = "nimble-ble-ota"
    scan_timeout_seconds: float = 8.0
    trigger_wait_seconds: float = 60.0
    reboot_wait_seconds: float = 1.5
    settle_seconds: float = 1.5
    ack_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 12.0
    firmware_dir: str = ""
    default_bins: dict[str, str] = field(default_factory=dict)  # {"semg": ..., "imu": ...}


@dataclass
class Config:
    buffer_seconds: float = 60.0
    display_span_seconds: float = 2.0
    target_fps: int = 30
    serial: SerialConfig = field(default_factory=SerialConfig)
    tcp: TcpConfig = field(default_factory=TcpConfig)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    semg: SemgConfig = field(default_factory=SemgConfig)
    imu: ImuConfig = field(default_factory=ImuConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    ota: OtaConfig = field(default_factory=OtaConfig)
    link_capacity: dict[str, int] = field(
        default_factory=lambda: {"serial": 200_000, "tcp": 11_520}
    )
    channel_map: dict[str, int] = field(default_factory=dict)
    # 肌肉贴点 → 采集通道：布局页俯视图拖拽交换后写回，判读数据跟着贴点走
    muscle_map: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _sub(cls, data: dict[str, Any]):
    known = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    raw: dict[str, Any] = {}
    if path is not None:
        path = Path(path)
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))

    link = raw.get("link", {})
    cfg = Config(
        buffer_seconds=raw.get("buffer_seconds", 60.0),
        display_span_seconds=raw.get("display_span_seconds", 2.0),
        target_fps=raw.get("target_fps", 30),
        serial=_sub(SerialConfig, raw.get("serial", {})),
        tcp=_sub(TcpConfig, raw.get("tcp", {})),
        protocol=_sub(ProtocolConfig, raw.get("protocol", {})),
        semg=_sub(SemgConfig, raw.get("semg", {})),
        imu=_sub(ImuConfig, raw.get("imu", {})),
        analytics=_sub(AnalyticsConfig, raw.get("analytics", {})),
        ota=_sub(OtaConfig, raw.get("ota", {})),
        link_capacity={
            "serial": int(link.get("serial_capacity_bytes_per_second", 200_000)),
            "tcp": int(link.get("tcp_capacity_bytes_per_second", 11_520)),
        },
        channel_map=dict(raw.get("channel_map", {})),
        muscle_map={k: int(v) for k, v in raw.get("muscle_map", {}).items()},
        raw=raw,
    )
    if len(cfg.semg.colors) < cfg.semg.channel_count:
        cfg.semg.colors = (cfg.semg.colors * cfg.semg.channel_count)[: cfg.semg.channel_count]
    return cfg


def save_channel_map(path: str | Path, channel_map: dict[str, int]) -> None:
    """把 MAC -> 显示通道号 的映射合并写回配置文件，跨次运行保持通道位置稳定。"""
    path = Path(path)
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["channel_map"] = dict(channel_map)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_muscle_map(path: str | Path, muscle_map: dict[str, int]) -> None:
    """把肌肉贴点 → 通道映射合并写回配置文件，跨次运行保持贴点交换结果。"""
    path = Path(path)
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["muscle_map"] = dict(muscle_map)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
