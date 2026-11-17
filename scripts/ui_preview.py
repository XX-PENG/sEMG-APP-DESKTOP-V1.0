"""离屏渲染各视图并导出 PNG。

没有显示器（CI、SSH）也能核对界面画得对不对：用模拟源灌一段真实格式的流，再逐个 tab
grab 成图片。用法：

    python scripts/ui_preview.py --out docs/screenshots --seconds 2
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--span", type=float, default=1.0, help="波形窗口秒数")
    args = ap.parse_args()

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from semg_desktop.config import load_config
    from semg_desktop.sources import SimulateSource
    from semg_desktop.ui.main_window import MainWindow

    pg.setConfigOptions(background="k", foreground="w", antialias=False)
    app = QApplication.instance() or QApplication([])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # 用配置副本，避免断开时把通道映射写回仓库里的 default.json
    cfg_path = out / "_preview_config.json"
    cfg_path.write_bytes((ROOT / "config" / "default.json").read_bytes())
    cfg = load_config(cfg_path)
    win = MainWindow(config_path=str(cfg_path), source=SimulateSource())
    win.resize(1500, 880)
    win.show()

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    win.emg.cb_span.setValue(args.span)
    names = {0: "emg_stacked", 1: "imu", 2: "analysis", 3: "status", 4: "ota"}
    for index, name in names.items():
        win.tabs.setCurrentIndex(index)
        for _ in range(3):
            app.processEvents()
            win.refresh()
            time.sleep(0.05)
        pix = win.grab()
        path = out / f"{name}.png"
        pix.save(str(path))
        print(f"{path}  {pix.width()}x{pix.height()}")

    win.emg.cb_mode.setCurrentText("2x4 网格")
    win.tabs.setCurrentIndex(0)
    for _ in range(3):
        app.processEvents()
        win.refresh()
        time.sleep(0.05)
    win.grab().save(str(out / "emg_grid.png"))
    print(f"{out / 'emg_grid.png'}")

    st = win.acq.stats()
    rows = win.acq.device_rows()
    print(f"帧: semg={st.parse.semg_frames} imu={st.parse.imu_frames} 异常={st.parse.parse_errors} "
          f"设备={len(rows)} 字节={st.bytes_per_second:,.0f}/s")
    win.disconnect_source()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
