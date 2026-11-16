from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .clock import DeviceClock  # noqa: F401  (保持导入可用，自检里按名义速率比对)
from .config import Config, load_config
from .pipeline import Acquisition
from .recorder import export_csv
from .sources import ReplaySource, SerialSource, SimulateSource, TcpSource, SourceError, list_serial_ports


def _add_source_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--serial", metavar="PORT", help="USB 串口设备名，Windows 形如 COM7，macOS 形如 /dev/cu.usbmodem*")
    g.add_argument("--tcp", metavar="HOST[:PORT]", help="WIFI 网关 TCP 透传地址，默认端口 3333")
    g.add_argument("--replay", metavar="FILE", help="回放原始文本日志（.txt/.log/.log.gz）")
    g.add_argument("--simulate", action="store_true", help="内置信号模拟，无需硬件")


def build_source(args, cfg: Config):
    if args.serial:
        return SerialSource(args.serial, baudrate=args.baud or cfg.serial.baudrate,
                            timeout=cfg.serial.timeout_seconds)
    if args.tcp:
        host, _, port = args.tcp.partition(":")
        return TcpSource(host, int(port or cfg.tcp.port), capacity_bytes_per_second=cfg.link_capacity["tcp"])
    if args.replay:
        return ReplaySource(args.replay, speed=args.speed, pace=not args.no_pace)
    return SimulateSource()


def cmd_selftest(args) -> int:
    cfg = load_config(args.config)
    seconds = args.duration
    src = SimulateSource()
    acq = Acquisition(src, cfg)
    acq.source.open()
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        acq.pump(8192)
    rows = acq.device_rows()
    semg = [r for r in rows if r["kind"] == "semg"]
    imu = [r for r in rows if r["kind"] == "imu"]
    st = acq.parser.stats
    print(f"设备: {len(semg)} 路 sEMG + {len(imu)} 路 IMU（{seconds:g} s）")
    print(f"{'通道':<6}{'MAC':<10}{'帧':>7}{'样本':>9}{'实测速率':>12}{'缺口帧':>8}{'drop':>7}")
    ok = True
    for r in semg + imu:
        target = cfg.protocol.semg_nominal_rate if r["kind"] == "semg" else cfg.protocol.imu_nominal_rate
        drift = abs(r["rate"] - target) / target if target else 1.0
        flag = "" if drift < 0.05 else "  ←偏差>5%"
        if drift >= 0.05:
            ok = False
        print(f"{(r['index'] + 1) if r['index'] is not None else '-':<6}{r['mac']:<10}{r['frames']:>7}"
              f"{r['samples']:>9}{r['rate']:>10.1f}/s{r['seq_gaps']:>8}{r['drop_total']:>7}{flag}")
    print(
        "解析: "
        f"行={st.lines} semg帧={st.semg_frames} imu帧={st.imu_frames} imu数据行={st.imu_sample_lines} "
        f"字节={st.bytes_fed}"
    )
    print(
        "异常: "
        f"imu块截断={st.imu_block_truncated} imu越界={st.imu_out_of_range} semg短帧={st.semg_short} "
        f"坏token={st.bad_token_lines} 垃圾行={st.garbage_lines} 超长行={st.oversize_lines}"
    )
    if st.parse_errors:
        print("FAIL: 存在解析异常计数")
        return 1
    if len(semg) != cfg.semg.channel_count or len(imu) != 1:
        print("FAIL: 设备数量不符")
        return 1
    if not ok:
        return 1
    print("PASS: 协议解析与时标重建一致")
    return 0


def cmd_parse(args) -> int:
    cfg = load_config(args.config)
    src = ReplaySource(args.file, pace=False)
    acq = Acquisition(src, cfg)
    src.open()
    try:
        while True:
            acq.pump(65536)
    except SourceError as exc:
        if "末尾" not in str(exc):
            raise
    rows = acq.device_rows()
    st = acq.parser.stats
    print(f"文件: {args.file}  字节={st.bytes_fed}  行={st.lines}")
    print(f"{'MAC':<10}{'类型':<6}{'槽':>4}{'帧':>8}{'样本':>10}{'drop':>8}{'缺口帧':>8}{'重同步':>8}{'实测速率':>11}")
    for r in rows:
        print(f"{r['mac']:<10}{r['kind']:<6}{str(r['slot']):>4}{r['frames']:>8}{r['samples']:>10}"
              f"{r['drop_total']:>8}{r['seq_gaps']:>8}{r['resyncs']:>8}{r['rate']:>9.1f}/s")
    print(
        "健康度: "
        f"imu块截断={st.imu_block_truncated} imu越界={st.imu_out_of_range} semg短帧={st.semg_short} "
        f"semg样本数>cnt={st.semg_overflow_cnt} int16削顶={st.semg_saturated} "
        f"坏token={st.bad_token_lines} 垃圾行={st.garbage_lines} 超长行={st.oversize_lines}"
    )
    return 0


def cmd_export(args) -> int:
    out = export_csv(Path(args.session), args.channel)
    print(f"已写出 {out}")
    return 0


def cmd_ports(args) -> int:
    ports = list_serial_ports()
    if not ports:
        print("未发现串口设备（pyserial 未安装或确实没有设备）")
        return 0
    for dev, desc in ports:
        print(f"{dev}\t{desc}")
    return 0


def cmd_gui(args) -> int:
    from .ui.app import run_gui

    cfg = load_config(args.config)
    source = build_source(args, cfg) if args.source_given else None
    return run_gui(config_path=args.config, source=source)


SUBCOMMANDS = ("selftest", "parse", "export", "ports", "gui")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["gui", *argv]

    parser = argparse.ArgumentParser(
        prog="semg-desktop",
        description="sEMG(8ch) + IMU 实时波形上位机（对接蓝牙网关文本协议）",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_gui = sub.add_parser("gui", help="启动图形界面（默认子命令）")
    _add_source_flags(p_gui)
    _add_common(p_gui)
    p_gui.set_defaults(func=cmd_gui, source_given=True)

    p_st = sub.add_parser("selftest", help="用模拟源跑一遍解析+时标链路并自检")
    p_st.add_argument("--duration", type=float, default=3.0)
    _add_common(p_st)
    p_st.set_defaults(func=cmd_selftest)

    p_parse = sub.add_parser("parse", help="离线分析一份原始串口日志")
    p_parse.add_argument("file")
    _add_common(p_parse)
    p_parse.set_defaults(func=cmd_parse)

    p_exp = sub.add_parser("export", help="把一次录制会话导出成长表 CSV")
    p_exp.add_argument("session")
    p_exp.add_argument("--channel", nargs="*", default=None)
    _add_common(p_exp)
    p_exp.set_defaults(func=cmd_export)

    p_ports = sub.add_parser("ports", help="列出可用串口")
    _add_common(p_ports)
    p_ports.set_defaults(func=cmd_ports)

    args = parser.parse_args(argv)
    if not any(getattr(args, k, None) for k in ("serial", "tcp", "replay", "simulate")):
        args.source_given = False
    args.config = args.config or _default_config_path()
    return args.func(args)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="配置文件路径（JSON），默认 config/default.json")
    p.add_argument("--baud", type=int, default=None, help="串口波特率，默认取配置里的 2000000")
    p.add_argument("--speed", type=float, default=1.0, help="回放倍速")
    p.add_argument("--no-pace", action="store_true", help="回放不按字节速率节流，全速灌入")


def _default_config_path() -> str | None:
    for cand in (Path("config/default.json"), Path(__file__).resolve().parents[2] / "config" / "default.json"):
        if cand.is_file():
            return str(cand)
    return None
