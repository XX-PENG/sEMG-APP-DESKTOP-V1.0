"""PyInstaller 打包入口，等价于 python -m semg_desktop。"""

import sys

from semg_desktop.cli import main

if __name__ == "__main__":
    sys.exit(main())