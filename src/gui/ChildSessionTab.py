"""桌面分身（child session）侧边栏标签页 —— 在 ok-ww 主窗口内启动并控制第二个桌面。

通过 config.py 的 `custom_tabs` 注册（与 CharacterCodeTab 相同的机制），复用
src/child_session/* 的同一套 Phase-0 原语：启用 Child Session、把第二个 ok-ww
启动进该会话、按需下载并启动 RdpHost.exe、把 RDP 视图嵌入标签页并连接。

与 spike.py 的区别：本页运行在主 ok-ww 窗口内（侧边栏标签），spike.py 是独立
诊断窗口。
"""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import BodyLabel, FluentIcon, PrimaryPushButton, PushButton

from ok import Logger
from ok.gui.widget.CustomTab import CustomTab

from src.child_session import session_launcher, wts_api
from src.child_session.rdp_host import RdpHostProcess
from src.child_session.rdphost_download import ensure_rdphost

logger = Logger.get_logger(__name__)


class ChildSessionTab(CustomTab):
    # 跨线程回传（worker -> GUI 线程）。
    _status = Signal(str)
    _download_progress = Signal(float, str)
    _connect_finished = Signal(object)  # (host, hello) | (None, error_str)

    def __init__(self):
        super().__init__()
        self.host = None
        self.container = None
        self._busy = False

        self._status.connect(self.status.setText)
        self._download_progress.connect(self._on_download_progress)
        self._connect_finished.connect(self._on_connect_finished)

        # 状态栏
        self.status = BodyLabel("桌面分身未连接。点击「连接桌面分身」开始。")
        self.add_widget(self.status)

        # 控制按钮
        row = QHBoxLayout()
        row.setSpacing(8)
        self.enable_button = PushButton(FluentIcon.IOT, "启用 Child Session")
        self.launch_button = PushButton(FluentIcon.PLAY, "启动子实例 ok-ww")
        self.connect_button = PrimaryPushButton(FluentIcon.CONNECT, "连接桌面分身")
        self.disconnect_button = PushButton(FluentIcon.CLOSE, "断开")
        self.win_d_button = PushButton(FluentIcon.FULL_SCREEN, "Win+D")
        self.win_tab_button = PushButton(FluentIcon.TILES, "Win+Tab")
        self.toggle_button = PushButton(FluentIcon.HIDE, "隐藏/显示")
        for widget in (self.enable_button, self.launch_button, self.connect_button,
                       self.disconnect_button, self.win_d_button, self.win_tab_button,
                       self.toggle_button):
            row.addWidget(widget)
        self.addLayout(row)

        # RDP 视图嵌入区
        self.embed_layout = QVBoxLayout()
        self.embed_layout.setAlignment(Qt.AlignTop)
        self.addLayout(self.embed_layout, stretch=1)

        # 信号连接
        self.enable_button.clicked.connect(self.enable_session)
        self.launch_button.clicked.connect(self.launch_okww)
        self.connect_button.clicked.connect(self.connect_desktop)
        self.disconnect_button.clicked.connect(self.disconnect_desktop)
        self.win_d_button.clicked.connect(lambda: self._shortcut("win_d"))
        self.win_tab_button.clicked.connect(lambda: self._shortcut("win_tab"))
        self.toggle_button.clicked.connect(self.toggle_visibility)

    @property
    def name(self):
        return "桌面分身"

    @property
    def icon(self):
        return FluentIcon.IOT

    # -- 动作 ---------------------------------------------------------------

    def enable_session(self):
        try:
            if not wts_api.is_child_sessions_enabled():
                wts_api.enable_child_sessions()
            session_id = wts_api.get_child_session_id()
            self._status.emit(
                f"Child Session 已启用，id={session_id}，RDP 端口={wts_api.get_rdp_port()}，"
                f"rdpwrap={wts_api.is_rdp_wrapper_enabled()}")
        except Exception as ex:
            self._status.emit(f"启用 Child Session 失败：{ex}")

    def launch_okww(self):
        session_id = wts_api.get_child_session_id()
        if session_id is None:
            self._status.emit("没有可用的 Child Session，请先启用。")
            return
        self._status.emit("正在把子实例 ok-ww 启动进 Child Session…")
        threading.Thread(target=self._launch_okww_worker, args=(session_id,),
                         daemon=True, name="childsession-launch").start()

    def _launch_okww_worker(self, session_id):
        try:
            session_launcher.launch_okww_in_session(session_id)
            self._status.emit(
                f"已在会话 {session_id} 启动子实例 ok-ww（--instance childSession）。")
        except Exception as ex:
            logger.exception("启动子实例失败")
            self._status.emit(f"启动子实例失败：{ex}")

    def connect_desktop(self):
        if self._busy:
            return
        self._busy = True
        self.connect_button.setEnabled(False)
        self._status.emit("正在准备桌面分身…")
        threading.Thread(target=self._connect_worker, daemon=True,
                         name="childsession-connect").start()

    def _connect_worker(self):
        try:
            # 1) 启用 Child Session
            if not wts_api.is_child_sessions_enabled():
                wts_api.enable_child_sessions()
            # 2) 确保 RdpHost.exe 存在（缺失则下载，带进度）
            self._status.emit("正在检查 RdpHost.exe…")
            ensure_rdphost(callback=lambda p, s: self._download_progress.emit(p, s))
            # 3) 启动 RdpHost 并连接
            self._status.emit("正在启动 RdpHost 并连接…")
            host = RdpHostProcess()
            host.on_event("login_complete", lambda _d: self._status.emit("RDP 登录完成"))
            host.on_event("connection_failed",
                          lambda d: self._status.emit(f"RDP 连接失败：{d.get('message')}"))
            host.on_event("input_window_focus",
                          lambda d: self._status.emit(f"桌面分身输入焦点：{d.get('focused')}"))
            hello = host.start()
            host.connect()
            self._connect_finished.emit((host, hello))
        except Exception as ex:
            logger.exception("连接桌面分身失败")
            self._connect_finished.emit((None, str(ex)))

    def _on_connect_finished(self, result):
        host, hello_or_error = result
        self._busy = False
        self.connect_button.setEnabled(True)
        if host is None:
            self._status.emit(f"连接失败：{hello_or_error}")
            return
        self.host = host
        try:
            self._embed(int(hello_or_error.get("hostHwnd", 0)))
        except Exception as ex:
            self._status.emit(f"嵌入 RdpHost 窗口失败：{ex}")
            return
        self._status.emit(
            f"已连接，session={hello_or_error.get('sessionId')}，hwnd={hello_or_error.get('hostHwnd')}")

    def _embed(self, hwnd):
        if not hwnd:
            raise ValueError("RdpHost 未返回窗口句柄。")
        self._clear_embed()
        window = QWindow.fromWinId(hwnd)
        self.container = QWidget.createWindowContainer(window, self.view)
        self.container.setMinimumSize(320, 200)
        self.embed_layout.addWidget(self.container, stretch=1)

    def _clear_embed(self):
        if self.container is not None:
            self.embed_layout.removeWidget(self.container)
            self.container.setParent(None)
            self.container.deleteLater()
            self.container = None

    def disconnect_desktop(self):
        self._clear_embed()
        if self.host:
            try:
                self.host.stop(logoff=True)
            except Exception as ex:
                logger.warning(f"停止 RdpHost 失败：{ex}")
            self.host = None
        self._status.emit("已断开桌面分身。")

    def _shortcut(self, name):
        if not self.host:
            self._status.emit("尚未连接桌面分身。")
            return
        try:
            self.host.send_shortcut(name)
            self._status.emit(f"已发送 {name}")
        except Exception as ex:
            self._status.emit(f"发送快捷键失败：{ex}")

    def toggle_visibility(self):
        if self.container is None:
            self._status.emit("尚未连接桌面分身。")
            return
        visible = self.container.isVisible()
        self.container.setVisible(not visible)
        self._status.emit("已隐藏桌面分身。" if visible else "已显示桌面分身。")

    def _on_download_progress(self, percent, size_str):
        self._status.emit(f"正在下载 RdpHost… {percent:.0f}% ({size_str})")
