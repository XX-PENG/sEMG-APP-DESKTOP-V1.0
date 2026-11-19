"""字节流来源。

四类来源共用同一接口，pipeline 不关心底下是 USB 串口、WIFI 网关 TCP 还是本地文件，
因此回放和硬件采集走的是完全相同的解析与时标路径。

read() 返回 (chunk, t_first, t_last)：t_first 是开始等第一个字节前的时刻，t_last 是
把这一批字节取走的时刻。协议没有时间戳，这两个时刻就是时标重建唯一的观测来源，所以
一次 read 要尽量小批量、快返回，而不是攒满 8 KB 再给。
"""

from __future__ import annotations

import gzip
import math
import socket
import time
import zlib
from pathlib import Path

import numpy as np
from scipy import signal

DEFAULT_SEMG_MACS = ["A4:CF:01", "A4:CF:02", "A4:CF:03", "A4:CF:04", "A4:CF:05", "A4:CF:06", "A4:CF:07", "A4:CF:08"]
DEFAULT_IMU_MAC = "12:34:90"


class SourceError(RuntimeError):
    pass


class DataSource:
    name = "source"
    capacity_bytes_per_second = 0

    def open(self) -> None:
        raise NotImplementedError

    def read(self, max_bytes: int) -> tuple[bytes, float, float]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.name


def list_serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


class SerialSource(DataSource):
    name = "serial"

    def __init__(self, port: str, baudrate: int = 2_000_000, timeout: float = 0.05) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.capacity_bytes_per_second = max(baudrate // 10, 1)
        self._ser = None

    def open(self) -> None:
        import serial

        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except Exception as exc:  # OSError / SerialException
            raise SourceError(f"打开串口 {self.port} 失败: {exc}") from exc
        self._ser.reset_input_buffer()

    def read(self, max_bytes: int) -> tuple[bytes, float, float]:
        if self._ser is None:
            raise SourceError("串口未打开")
        t_first = time.monotonic()
        try:
            first = self._ser.read(1)
            if not first:
                return b"", t_first, time.monotonic()
            waiting = getattr(self._ser, "in_waiting", 0) or 0
            rest = self._ser.read(min(max_bytes - 1, waiting)) if waiting else b""
        except Exception as exc:
            raise SourceError(f"串口读取失败: {exc}") from exc
        return first + rest, t_first, time.monotonic()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def describe(self) -> str:
        return f"串口 {self.port} @ {self.baudrate}"


class TcpSource(DataSource):
    """WIFI 网关（sEMG-WIFI-ESP32-V1.0）TCP:3333 透传。

    注意其 UART1 是 115200 8N1，约 11.5 KB/s，而 8 路 sEMG + 1 路 IMU 的文本量约
    130-160 KB/s，这条链路撑不住全量数据；capacity_bytes_per_second 按 UART1 报，
    UI 会把它当硬上限显示。
    """

    name = "tcp"

    def __init__(self, host: str, port: int = 3333, connect_timeout: float = 3.0,
                 capacity_bytes_per_second: int = 11_520) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.capacity_bytes_per_second = capacity_bytes_per_second
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise SourceError(f"连接 {self.host}:{self.port} 失败: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        self._sock = sock

    def read(self, max_bytes: int) -> tuple[bytes, float, float]:
        if self._sock is None:
            raise SourceError("TCP 未连接")
        t_first = time.monotonic()
        self._sock.settimeout(0.25)
        try:
            chunk = self._sock.recv(max_bytes)
        except TimeoutError:
            return b"", t_first, time.monotonic()
        except OSError as exc:
            raise SourceError(f"TCP 读取失败: {exc}") from exc
        if not chunk:
            raise SourceError("TCP 连接被对端关闭")
        return chunk, t_first, time.monotonic()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    def describe(self) -> str:
        return f"TCP {self.host}:{self.port}"


class ReplaySource(DataSource):
    """按字节速率节拍的原始文本回放（.txt / .log / .log.gz）。"""

    name = "replay"

    def __init__(self, path: str | Path, speed: float = 1.0, chunk_bytes: int = 4096,
                 rate_bytes_per_second: float = 160_000.0, pace: bool = True) -> None:
        self.path = Path(path)
        self.speed = max(speed, 0.05)
        self.chunk_bytes = chunk_bytes
        self.rate_bytes_per_second = rate_bytes_per_second
        self.pace = pace
        self.capacity_bytes_per_second = int(rate_bytes_per_second * speed) if pace else 0
        self._fh = None
        self._deadline = 0.0
        self._consumed = 0
        self.total_bytes = 0
        self.paused = False

    def open(self) -> None:
        if not self.path.is_file():
            raise SourceError(f"回放文件不存在: {self.path}")
        opener = gzip.open if self.path.suffix == ".gz" else open
        self._fh = opener(self.path, "rb")
        try:
            self.total_bytes = self.path.stat().st_size
        except OSError:
            self.total_bytes = 0
        self._consumed = 0
        self._deadline = time.monotonic()

    def read(self, max_bytes: int) -> tuple[bytes, float, float]:
        if self._fh is None:
            raise SourceError("回放文件未打开")
        if self.paused:
            time.sleep(0.05)
            t = time.monotonic()
            return b"", t, t
        size = min(max_bytes, self.chunk_bytes)
        t_first = time.monotonic()
        chunk = self._fh.read(size)
        if not chunk:
            raise SourceError("回放到达文件末尾")
        self._consumed += len(chunk)
        if not self.pace:
            t = time.monotonic()
            return chunk, t, t
        target = self._deadline + len(chunk) / (self.rate_bytes_per_second * self.speed)
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        self._deadline = max(target, time.monotonic())
        return chunk, t_first, self._deadline

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def progress(self) -> float:
        if not self.total_bytes:
            return 0.0
        return min(1.0, self._consumed / self.total_bytes)

    def describe(self) -> str:
        return f"回放 {self.path.name} ×{self.speed:g}"


class SimulateSource(DataSource):
    """生成与网关逐字节同格式的文本流，无硬件时用于联调和演示。

    时标由 seq 重建，所以模拟流的帧节拍只要大致是 20 ms 就能跑通全链路；抖动故意做成
    随机值，用来验证 PLL 与缺口显示。
    """

    name = "simulate"

    def __init__(self, semg_channels: int = 8, frame_period: float = 0.02,
                 semg_rate: float = 2000.0, imu_rate: float = 500.0, jitter: float = 0.006,
                 loss_prob: float = 0.002) -> None:
        self.semg_channels = semg_channels
        self.frame_period = frame_period
        self.samples_per_frame = max(1, int(round(semg_rate * frame_period)))
        self.imu_samples_per_frame = max(1, int(round(imu_rate * frame_period)))
        self.jitter = jitter
        self.loss_prob = loss_prob
        self.capacity_bytes_per_second = 200_000
        self._seq = 0
        self._t_next = 0.0
        self._t_first = 0.0
        self._t_last = 0.0
        self._rng = np.random.default_rng(7)
        self._phase = np.zeros(semg_channels)
        self._phase_imu = 0.0
        self._active = semg_channels + 1
        self._pending = bytearray()

    def open(self) -> None:
        self._t_next = time.monotonic()
        lines = [
            f"I ({i * 120 + 40}) GW: Sensor connected: slot={i} conn={i} type=sEMG mac=AA:BB:CC:{DEFAULT_SEMG_MACS[i]}"
            for i in range(self.semg_channels)
        ]
        lines.append(
            f"I ({self.semg_channels * 120}) GW: Sensor connected: slot={self.semg_channels} "
            f"conn={self.semg_channels} type=IMU mac=AA:BB:CC:{DEFAULT_IMU_MAC}"
        )
        self._t_first = time.monotonic()
        self._t_last = self._t_first
        self._pending = bytearray("".join(ln + "\n" for ln in lines).encode())

    def read(self, max_bytes: int) -> tuple[bytes, float, float]:
        if not self._pending:
            self._pump()
        chunk = bytes(self._pending[:max_bytes])
        del self._pending[:max_bytes]
        return chunk, self._t_first, self._t_last

    def render(self, n_frames: int) -> bytes:
        """跳过实时节拍直接生成 n 帧文本，用于生成示例日志和测试固定输入。"""
        return b"".join(self._render_frame() for _ in range(n_frames))

    def _pump(self) -> None:
        now = time.monotonic()
        wait = self._t_next - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        elif now - self._t_next > 4 * self.frame_period:
            self._t_next = now  # 落后太多就重定节拍，不让追赶型突发把时标挤歪
        self._t_next += self.frame_period
        self._t_first = time.monotonic()
        self._pending += self._render_frame()
        # 设备侧节拍是准的，抖动来自传输与打印队列，所以只加在到达时刻上，让 PLL 去跟
        self._t_last = time.monotonic() + self._rng.uniform(0.0, self.jitter)

    def _render_frame(self) -> bytes:
        seq = self._seq & 0xFFFF
        self._seq += 1
        drop = 0
        if self._rng.random() < self.loss_prob:
            seq = (seq + int(self._rng.integers(2, 5))) & 0xFFFF
            drop = int(self._rng.integers(1, 60))
        out: list[str] = []
        for ch in range(self.semg_channels):
            values = " ".join(str(int(v)) for v in self._semg_samples(ch))
            out.append(
                f"{ch + 1}/{self._active} semg {DEFAULT_SEMG_MACS[ch]} seq={seq} "
                f"cnt={self.samples_per_frame} drop={drop} {values}"
            )
        n = self.imu_samples_per_frame
        out.append(
            f"{self.semg_channels + 1}/{self._active} imu {DEFAULT_IMU_MAC} seq={seq} cnt={n} drop={drop}"
        )
        out.extend("%.3f %.3f %.3f %.3f %.3f %.3f %d %d %d" % tuple(r) for r in self._imu_samples(n))
        return ("\n".join(out) + "\n").encode()

    def _semg_samples(self, ch: int) -> np.ndarray:
        n = self.samples_per_frame
        self._phase[ch] += n * self.frame_period
        p = self._phase[ch]
        burst = 1.0 if (int(p // 3.0) % 4) < 1 else 0.06
        sig = 1800.0 * burst * np.sin(2 * np.pi * (70.0 + 9.0 * ch) * p)
        noise = self._rng.normal(0.0, 120.0 + 400.0 * burst, n)
        hum = 260.0 * np.sin(2 * np.pi * 50.0 * p + ch)
        drift = 300.0 * np.sin(2 * np.pi * 0.4 * p)
        return np.clip(sig + noise + hum + drift, -32767, 32767)

    def _imu_samples(self, n: int) -> list[tuple]:
        self._phase_imu += n * self.frame_period
        p = self._phase_imu
        ax = 0.05 * np.sin(2 * np.pi * 0.7 * p) + self._rng.normal(0, 0.004, n)
        ay = 0.04 * np.sin(2 * np.pi * 0.5 * p + 1.0) + self._rng.normal(0, 0.004, n)
        az = 1.0 + self._rng.normal(0, 0.004, n)
        gx = 1.2 * np.sin(2 * np.pi * 0.9 * p) + self._rng.normal(0, 0.3, n)
        gy = self._rng.normal(0, 0.3, n)
        gz = 0.4 * np.sin(2 * np.pi * 1.3 * p) + self._rng.normal(0, 0.3, n)
        mx = np.full(n, 32.0) + 6 * np.sin(2 * np.pi * 0.2 * p)
        my = np.full(n, -18.0) + 5 * np.sin(2 * np.pi * 0.2 * p)
        mz = np.full(n, 44.0)
        return list(zip(ax, ay, az, gx, gy, gz, mx.astype(int), my.astype(int), mz.astype(int)))

    def describe(self) -> str:
        return f"模拟 {self.semg_channels} 路 sEMG + 1 路 IMU @{1 / self.frame_period:g} Hz"


class AthleteSimSource(SimulateSource):
    """曲棍球运动员跑动模拟：EMG 爆发与步态相位锁定，谱随疲劳下移。

    与通用模拟源的区别在于"信号有生理含义"，因此上层算出来的不对称、H:Q、MDF 斜率、
    末端摆动负荷都是可复现的真值，而不是随机噪声碰巧越限：

    * 腘绳肌（BF/ST）在摆动末期与支撑初期放电，股四头肌（VL/RF）在触地前后制动；
    * 大腿 IMU 矢状面角速度呈摆动正峰→触地前回摆，触地时刻加速度模长出现冲击峰；
    * 带通上截止随课次时间下移，MDF 因此真实下降；同时 RMS 上升，构成典型疲劳图谱；
    * 右侧腘绳肌幅度按 asym 衰减，并随疲劳放大，用来复现伤后侧的失衡。

    输出仍是网关那套文本行，解析、时标、录制路径与真实硬件完全一致。
    """

    name = "athlete"

    def __init__(self, drill: str = "sprint", asym: float = 0.14, fatigue_seconds: float = 150.0,
                 ham_gain: float = 1.0, quad_gain: float = 1.45, seed: int = 11,
                 fatigue_rate: float = 1.0, **kwargs) -> None:
        super().__init__(**kwargs)
        from .guard.model import DRILL_BY_ID, MUSCLES

        self._muscles = MUSCLES
        self._drills = DRILL_BY_ID
        self.drill_id = drill if drill in DRILL_BY_ID else "sprint"
        self.asym = float(asym)
        self.fatigue_seconds = max(20.0, float(fatigue_seconds))
        self.fatigue_rate = max(0.0, float(fatigue_rate))
        self.ham_gain = float(ham_gain)
        self.quad_gain = float(quad_gain)
        self._rng = np.random.default_rng(seed)
        self._t_session = 0.0
        self._fs_semg = self.samples_per_frame / self.frame_period
        self._fs_imu = self.imu_samples_per_frame / self.frame_period
        self._bp_sos: dict[int, np.ndarray] = {}
        self._bp_zi: dict[int, np.ndarray] = {}
        self._bp_fc: dict[int, float] = {}
        self._stride_index = 0

    @property
    def drill(self):
        return self._drills[self.drill_id]

    @classmethod
    def from_profile(cls, profile, drill: str = "sprint", **kwargs) -> AthleteSimSource:
        """按档案推演这个人的跑动特征：伤后复出周数决定激活 deficit 与疲劳速度。

        MVC 基线代表"最大自主收缩能力"，这里的 asym 代表"跑动中实际募集不足"，两者
        是不同量：能力可以接近，但伤后侧在高速下就是募集不上去，这正是风险来源。
        """
        week = profile.return_week
        if week is not None and week <= 12:
            asym = 0.10 + (12 - week) * 0.008
        elif profile.injuries:
            asym = 0.075
        else:
            asym = 0.04
        fatigue_seconds = max(90.0, 380.0 - profile.chronic_load * 0.35)
        # 用 crc32 而不是 hash()：str hash 每进程随机化，会让"同一个运动员"每次跑出生理
        # 不同的模拟，测试与复现都不可比。
        seed = zlib.crc32(profile.id.encode("utf-8")) % 10000
        return cls(drill=drill, asym=asym, fatigue_seconds=fatigue_seconds,
                   seed=seed, **kwargs)

    def set_drill(self, drill_id: str) -> None:
        if drill_id in self._drills:
            self.drill_id = drill_id

    @property
    def fatigue_progress(self) -> float:
        """疲劳推演进度。指数 1.3 让频谱下移先慢后快，符合实际疲劳累积曲线。"""
        u = self._t_session * self.fatigue_rate / self.fatigue_seconds
        return float(np.clip(u, 0.0, 1.0) ** 1.3)

    def open(self) -> None:
        self._t_session = 0.0
        self._bp_sos.clear()
        self._bp_zi.clear()
        self._bp_fc.clear()
        super().open()

    def _render_frame(self) -> bytes:
        self._t_session += self.frame_period
        self._stride_index = int(self._t_session * self.drill.stride_hz)
        return super()._render_frame()

    # ── 激活包络 ────────────────────────────────────────────────────────────

    @staticmethod
    def _bump(phase: np.ndarray, mu: float, sigma: float) -> np.ndarray:
        d = np.abs(phase - mu)
        d = np.minimum(d, 1.0 - d)
        return np.exp(-0.5 * np.square(d / sigma))

    def _activation(self, muscle, tt: np.ndarray) -> np.ndarray:
        drill = self.drill
        fp = self.fatigue_progress
        phase = np.mod(tt * drill.stride_hz, 1.0)
        if drill.id == "nordic":
            u = phase
            if muscle.group == "ham":
                a = 0.14 + 1.15 * np.power(u, 1.4)
            else:
                a = 0.07 + 0.22 * u
        elif muscle.group == "ham":
            swing_w = 0.98 if muscle.abbr == "BF" else 0.72
            stance_w = 0.52 if muscle.abbr == "BF" else 0.74
            # 疲劳让末端摆动放电更晚更窄：触地前预激活提前量随之缩短
            mu = 0.93 + 0.030 * fp
            sigma = 0.072 - 0.016 * fp
            a = 0.11 + swing_w * self._bump(phase, mu, sigma) + stance_w * self._bump(phase, 0.07, 0.058)
        else:
            a = 0.09 + 0.88 * self._bump(phase, 0.02, 0.070) + 0.46 * self._bump(phase, 0.88, 0.062)

        gain = self.ham_gain if muscle.group == "ham" else self.quad_gain
        gain *= 0.34 + 0.66 * drill.intensity
        gain *= 1.0 + (0.24 if muscle.group == "ham" else 0.10) * fp   # 疲劳：同等输出下 RMS 上升
        if muscle.side == "R" and muscle.group == "ham":
            gain *= 1.0 - self.asym * (1.0 + 0.55 * fp)
        if drill.id == "cut" and (self._stride_index % 3 == 0):
            gain *= 1.0 + (0.30 if muscle.side == "L" else -0.12)
        if drill.id == "nordic" and muscle.group == "ham":
            gain *= 1.25
        return a * gain

    def _semg_samples(self, ch: int) -> np.ndarray:
        n = self.samples_per_frame
        muscle = self._muscles[ch % len(self._muscles)]
        fs = self._fs_semg
        tt = self._t_session - self.frame_period + np.arange(n) / fs
        act = self._activation(muscle, tt)

        fp = self.fatigue_progress
        fc = 205.0 - 52.0 * fp                      # 带通上截止下移 → MDF 真实下降
        if ch not in self._bp_sos or abs(fc - self._bp_fc[ch]) > 0.03 * self._bp_fc[ch]:
            nyq = fs / 2.0
            self._bp_sos[ch] = signal.butter(3, [20.0 / nyq, min(fc, nyq * 0.9) / nyq],
                                             btype="band", output="sos")
            self._bp_zi[ch] = signal.sosfilt_zi(self._bp_sos[ch]) * 0.0
            self._bp_fc[ch] = fc
        noise = self._rng.normal(0.0, 1.0, n)
        shaped, self._bp_zi[ch] = signal.sosfilt(self._bp_sos[ch], noise, zi=self._bp_zi[ch])
        # 带通只留下白噪声的一小部分功率，且占比随 fc 下移而变小；不归一的话疲劳期
        # RMS 会随带宽变窄而下降，与"同等输出下 RMS 上升"的真实疲劳图谱正好相反。
        shaped *= math.sqrt((fs / 2.0) / max(fc - 20.0, 20.0))
        mvc = 2600.0 if muscle.group == "ham" else 3100.0
        sig = shaped * act * (mvc / 0.8)
        hum = 90.0 * np.sin(2 * np.pi * 50.0 * tt + ch)
        drift = 110.0 * np.sin(2 * np.pi * 0.33 * tt + 0.7 * ch)
        baseline = self._rng.normal(0.0, 55.0, n)
        return np.clip(sig + hum + drift + baseline, -32767, 32767)

    def _imu_samples(self, n: int) -> list[tuple]:
        drill = self.drill
        fs = self._fs_imu
        tt = self._t_session - self.frame_period + np.arange(n) / fs
        phase = np.mod(tt * drill.stride_hz, 1.0)
        intensity = drill.intensity
        stance = phase < 0.35
        u = np.clip((phase - 0.35) / 0.65, 0.0, 1.0)

        swing = np.sin(2 * np.pi * u)
        gy = np.where(stance, 42.0 * np.sin(2 * np.pi * phase * 2.0), 790.0 * intensity * swing)
        gy = gy + self._rng.normal(0.0, 14.0, n)
        gx = 55.0 * intensity * np.sin(2 * np.pi * phase + 0.4) + self._rng.normal(0.0, 9.0, n)
        gz = (150.0 if drill.id == "cut" else 48.0) * intensity * np.sin(2 * np.pi * phase * 1.5) \
            + self._rng.normal(0.0, 10.0, n)

        d = np.minimum(phase, 1.0 - phase)
        impact = 5.4 * intensity * np.exp(-0.5 * np.square(d / 0.026))
        ax = 0.92 * intensity * np.sin(2 * np.pi * phase + 0.6) + self._rng.normal(0.0, 0.035, n)
        ay = 0.40 * intensity * np.sin(2 * np.pi * phase * 1.2) + self._rng.normal(0.0, 0.035, n)
        az = 1.0 + 0.34 * np.sin(2 * np.pi * phase) + impact + self._rng.normal(0.0, 0.04, n)
        mx = np.full(n, 32.0) + 5 * np.sin(2 * np.pi * 0.2 * tt)
        my = np.full(n, -18.0) + 4 * np.sin(2 * np.pi * 0.2 * tt)
        mz = np.full(n, 44.0)
        return list(zip(ax, ay, az, gx, gy, gz, mx.astype(int), my.astype(int), mz.astype(int)))

    def describe(self) -> str:
        return f"运动员模拟 · {self.drill.name} · 8 路 sEMG + 大腿 IMU"
