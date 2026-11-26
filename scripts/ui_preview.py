"""离屏渲染 + 主流程驱动：无显示器也能核对界面与交互闭环。

    python scripts/ui_preview.py --out docs/preview --seconds 6

流程：连接运动员模拟 →跑一段 →开始课次 →逐页截图 →结束课次 →报告页截图。
离屏平台字体库可能为空，theme.ensure_fonts() 会注册系统字体保证文字可读。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pump(app, win, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        win.refresh()
        time.sleep(0.01)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "preview"))
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--width", type=int, default=1560)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from semg_desktop.ui import theme as T
    from semg_desktop.ui.main_window import MainWindow

    pg.setConfigOptions(background=T.C["bg1"], foreground=T.C["muted"], antialias=True)
    app = QApplication.instance() or QApplication([])
    T.apply(app)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # 配置副本放临时目录：避免把 athletes.json 等持久化产物写进截图目录
    import tempfile

    cfg_dir = Path(tempfile.mkdtemp(prefix="semg_preview_"))
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_bytes((ROOT / "config" / "default.json").read_bytes())
    win = MainWindow(config_path=str(cfg_path))
    win.resize(args.width, args.height)
    win.show()
    pump(app, win, args.seconds)

    def shot(name: str) -> None:
        for _ in range(3):
            app.processEvents()
            win.refresh()
            time.sleep(0.04)
        win.grab().save(str(out / f"{name}.png"))
        print(f"{out / f'{name}.png'}")

    shot("01_live")
    win.toggle_session()
    pump(app, win, max(3.0, args.seconds))
    shot("02_live_session")
    for key, name in (("muscle", "03_muscle"), ("fatigue", "04_fatigue"),
                      ("events", "05_events"), ("roster", "06_roster"), ("devices", "07_devices")):
        win._select_nav(key)
        pump(app, win, 0.6)
        shot(name)
    win._select_nav("engineering")
    pump(app, win, 0.6)
    shot("07b_engineering")
    win.toggle_session()
    pump(app, win, 0.6)
    shot("08_report")

    st = win.acq.stats() if win.acq else None
    snap = win._snap
    print(f"风险={snap.score:.1f} 步数={snap.stride_count} 事件={len(win.engine.events)} "
          f"负荷={snap.load_au:.0f}AU")
    if st:
        print(f"链路 {st.bytes_per_second:,.0f} B/s 帧 {st.frames_per_second:.0f}/s "
              f"解析异常 {st.parse.parse_errors} 错误 {st.error}")
    win.disconnect_source()
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())