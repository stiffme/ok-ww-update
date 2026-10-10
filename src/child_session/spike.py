"""Phase 0 feasibility spike for the desktop-clone feature (Windows only).

Run from the repo root, as administrator:

    python -m src.child_session.spike                 # enable + embed + Win+D
    python -m src.child_session.spike --launch notepad.exe
    python -m src.child_session.spike --launch-okww    # also start a 2nd ok-ww

This exercises the four gated primitives:
  1. enable the child session and read its id / RDP port
  2. launch a process into the child session (invisible on the main desktop)
  3. start RdpHost.exe and connect it to the child session
  4. embed the helper window inside a Qt container and send Win+D

It is a diagnostic, not the real UI (see src/child_session/window.py in later phases).
"""

import argparse
import os
import sys
import threading

if sys.platform != "win32":
    raise SystemExit("桌面分身仅在 Windows 上可用。")

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from src.child_session import session_launcher, wts_api
from src.child_session.rdp_host import RdpHostProcess
from src.child_session.rdphost_download import ensure_rdphost


class SpikeWindow(QWidget):
    _download_progress = Signal(float, str)
    _rdphost_ready = Signal(object)

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.host = None
        self.container = None
        self.setWindowTitle("ok-ww 桌面分身 — Phase 0 spike")
        self.resize(1000, 700)
        self._download_progress.connect(self._on_download_progress)
        self._rdphost_ready.connect(self._on_rdphost_ready)

        root = QVBoxLayout(self)
        self.status = QLabel("初始化中…")
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        for text, slot in (
            ("启动测试程序", self.launch_test_exe),
            ("启动子实例 ok-ww", self.launch_okww),
            ("运行桌面 / Win+D", lambda: self._shortcut("win_d")),
            ("Win+Tab", lambda: self._shortcut("win_tab")),
            ("隐藏/显示桌面", self.toggle_desktop),
            ("终止桌面分身", self.kill_desktop),
            ("RDP 硬件GPU", self.toggle_rdp_gpu),
            ("断开并退出", self.shutdown),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        root.addLayout(buttons)

        self.embed_holder = QVBoxLayout()
        root.addLayout(self.embed_holder, stretch=1)

    # -- actions --------------------------------------------------------------

    def enable_session(self):
        try:
            if not wts_api.is_child_sessions_enabled():
                wts_api.enable_child_sessions()
            self.status.setText(
                f"Child Session 已启用，id={wts_api.get_child_session_id()}，"
                f"RDP 端口={wts_api.get_rdp_port()}，rdpwrap={wts_api.is_rdp_wrapper_enabled()}")
        except Exception as ex:
            self.status.setText(f"启用 Child Session 失败：{ex}")

    def launch_test_exe(self):
        try:
            session_id = wts_api.get_child_session_id()
            if session_id is None:
                self.status.setText("没有可用的 Child Session，请先启用。")
                return
            target = self.args.launch or "notepad.exe"
            session_launcher.launch_in_session(session_id, target)
            self.status.setText(f"已在会话 {session_id} 启动 {target}（主桌面上不可见即正确）")
        except Exception as ex:
            self.status.setText(f"启动失败：{ex}")

    def launch_okww(self):
        try:
            session_id = wts_api.get_child_session_id()
            if session_id is None:
                self.status.setText("没有可用的 Child Session，请先启用。")
                return
            session_launcher.launch_okww_in_session(session_id)
            self.status.setText(f"已在会话 {session_id} 启动子实例 ok-ww（--instance childSession）")
        except Exception as ex:
            self.status.setText(f"启动子实例失败：{ex}")

    def start_host(self):
        exe = RdpHostProcess.exe_path()
        if not exe or not os.path.isfile(exe):
            self.status.setText("正在下载 RdpHost…")
            threading.Thread(target=self._ensure_rdphost, daemon=True,
                             name="rdphost-download").start()
            return
        self._start_host()

    def _ensure_rdphost(self):
        try:
            ensure_rdphost(callback=lambda p, s: self._download_progress.emit(p, s))
            self._rdphost_ready.emit((True, None))
        except Exception as ex:
            self._rdphost_ready.emit((False, str(ex)))

    def _on_download_progress(self, percent, size_str):
        self.status.setText(f"正在下载 RdpHost… {percent:.0f}% ({size_str})")

    def _on_rdphost_ready(self, result):
        ok, error = result
        if ok:
            self.status.setText("RdpHost 下载完成，正在启动…")
            self._start_host()
        else:
            self.status.setText(f"RdpHost 下载失败：{error}")

    def _start_host(self):
        try:
            self.host = RdpHostProcess()
            self.host.on_event("login_complete", lambda _d: self.status.setText("RDP 登录完成"))
            self.host.on_event("connection_failed",
                               lambda d: self.status.setText(f"RDP 连接失败：{d.get('message')}"))
            self.host.on_event("input_window_focus",
                               lambda d: self.status.setText(f"桌面分身输入焦点：{d.get('focused')}"))
            hello = self.host.start()
            if not hello.get("hostHwnd"):
                self.status.setText("RdpHost 未返回窗口句柄。")
                return
            window = QWindow.fromWinId(int(hello["hostHwnd"]))
            self.container = QWidget.createWindowContainer(window, self)
            self.container.setMinimumSize(320, 200)
            self.embed_holder.addWidget(self.container)
            self.host.connect()
            self.status.setText(f"已嵌入 RdpHost 窗口 hwnd={hello.get('hostHwnd')}，正在连接…")
        except Exception as ex:
            self.status.setText(f"启动 RdpHost 失败：{ex}")

    def _shortcut(self, name):
        try:
            self.host.send_shortcut(name)
            self.status.setText(f"已发送 {name}")
        except Exception as ex:
            self.status.setText(f"发送快捷键失败：{ex}")

    def toggle_desktop(self):
        if self.container is None:
            self.status.setText("尚未嵌入 RdpHost 窗口，无法隐藏。")
            return
        visible = self.container.isVisible()
        self.container.setVisible(not visible)
        self.status.setText("已隐藏桌面分身。" if visible else "已显示桌面分身。")

    def toggle_rdp_gpu(self):
        try:
            enabled = wts_api.is_rdp_hw_gpu_enabled()
            wts_api.set_rdp_hw_gpu(not enabled)
            enabled = wts_api.is_rdp_hw_gpu_enabled()
            self.status.setText(
                "已启用 RDP 硬件 GPU，重新连接桌面分身后生效。" if enabled
                else "已禁用 RDP 硬件 GPU（恢复软件渲染）。")
        except Exception as ex:
            self.status.setText(f"切换 RDP 硬件 GPU 失败：{ex}")

    def kill_desktop(self):
        try:
            if self.host:
                try:
                    self.host.stop(logoff=False)
                except Exception:
                    pass
                self.host = None
            if self.container is not None:
                self.embed_holder.removeWidget(self.container)
                self.container.setParent(None)
                self.container.deleteLater()
                self.container = None
            session_id = wts_api.kill_child_session()
            self.status.setText(
                "已终止桌面分身及其中运行的程序。" if session_id is not None
                else "没有可终止的桌面分身。")
        except Exception as ex:
            self.status.setText(f"终止桌面分身失败：{ex}")

    def shutdown(self):
        if self.host:
            self.host.stop(logoff=True)
        self.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch", help="exe to launch into the child session (default notepad.exe)")
    parser.add_argument("--launch-okww", action="store_true", help="start a 2nd ok-ww immediately")
    parser.add_argument("--no-connect", action="store_true", help="do not auto-start the RdpHost")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = SpikeWindow(args)
    window.show()
    if not args.no_connect:
        window.enable_session()
        window.start_host()
    if args.launch_okww:
        window.launch_okww()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
