"""The Replay Lab desktop window: PySide6 + Qt WebEngine showing the React / Lightweight Charts UI (replay/app/web).

    pip install -r requirements-replay.txt
    python -m replay app

Python owns data, indicators, replay and backtests (replay/app/host.py); the page only displays what it is sent
over QWebChannel. This window never connects to the live bot, its inbox or MT5 order functions.
"""
from __future__ import annotations
import json, logging, os, sys

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")


def main(argv=None, on_ready=None) -> int:
    """on_ready(window, host, app): optional hook after the window is shown (smoke tests, automation)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow
    except ImportError:
        print("The desktop window needs PySide6 (with Qt WebEngine): pip install -r requirements-replay.txt\n"
              "Headless replays work without it: python -m replay --help", file=sys.stderr)
        return 2
    if not os.path.exists(WEB):
        print(f"{WEB} is missing: build the UI with `cd replay_ui && npm ci && npm run build`", file=sys.stderr)
        return 2
    from replay.app.host import Host

    class Bridge(QObject):
        message = Signal(str)

        def __init__(self, host_ref):
            super().__init__()
            self.host_ref = host_ref

        @Slot(str)
        def command(self, text: str):
            try:
                cmd = json.loads(text)
            except ValueError:
                return
            if isinstance(cmd, dict):
                self.host_ref[0].handle(cmd)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Aureon Replay Lab")
    win = QMainWindow()
    win.setWindowTitle("Aureon Replay Lab — HISTORICAL REPLAY (simulation only)")
    view = QWebEngineView()
    channel = QWebChannel()
    ref = [None]
    bridge = Bridge(ref)
    host = Host(send=lambda m: bridge.message.emit(to_json(m)))
    ref[0] = host

    def pick_csv(_):
        path, _f = QFileDialog.getOpenFileName(win, "Candle CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            host.post({"type": "csv_picked", "path": path})

    def fullscreen(_):
        win.showNormal() if win.isFullScreen() else win.showFullScreen()

    def open_path(cmd):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(cmd.get("path", ""))))
    host.ui.update(pick_csv=pick_csv, fullscreen=fullscreen, open_path=open_path)
    channel.registerObject("bridge", bridge)
    view.page().setWebChannel(channel)
    view.load(QUrl.fromLocalFile(WEB))
    win.setCentralWidget(view)
    win.resize(1500, 900)
    win.show()
    timer = QTimer()
    timer.setInterval(16)                                   # one batch per frame at most (~60 Hz)
    timer.timeout.connect(host.pump)
    timer.start()
    if on_ready:
        on_ready(win, host, app)
    code = app.exec()
    host.shutdown()
    return code


def to_json(m) -> str:
    """One message for the page. NaN / inf are not JSON, so they travel as null."""
    return json.dumps(_clean(m), default=str, allow_nan=False)


def _clean(m):
    """NaN / inf are not JSON: send them as null (an EMA before it is defined, an empty volume)."""
    import math
    if isinstance(m, dict):
        return {k: _clean(v) for k, v in m.items()}
    if isinstance(m, list):
        return [_clean(v) for v in m]
    if isinstance(m, float) and not math.isfinite(m):
        return None
    return m


if __name__ == "__main__":
    sys.exit(main())
