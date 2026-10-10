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
    _launch_finished = Signal()

    def __init__(self):
        super().__init__()
        self.host = None
        self.container = None
        self._busy = False
        self._launching = False
        self._desktop_visible = False
        self._smart_sizing = False

        # 状态栏（先创建，后面的信号连接要引用 self.status）
        self.status = BodyLabel("桌面分身未连接。点击「连接桌面分身」开始。")
        self.add_widget(self.status)

        self._status.connect(self.status.setText)
        self._download_progress.connect(self._on_download_progress)
        self._connect_finished.connect(self._on_connect_finished)
        self._launch_finished.connect(self._on_launch_finished)

        # 控制按钮
        row = QHBoxLayout()
        row.setSpacing(8)
        self.launch_button = PushButton(FluentIcon.PLAY, "启动子实例 ok-ww")
        self.connect_button = PrimaryPushButton(FluentIcon.CONNECT, "连接桌面分身")
        self.disconnect_button = PushButton(FluentIcon.CLOSE, "断开")
        self.kill_button = PushButton(FluentIcon.DELETE, "终止桌面分身")
        self.win_d_button = PushButton(FluentIcon.FULL_SCREEN, "Win+D")
        self.win_tab_button = PushButton(FluentIcon.TILES, "Win+Tab")
        self.toggle_button = PushButton(FluentIcon.HIDE, "隐藏/显示")
        self.scale_button = PushButton(FluentIcon.ZOOM, "缩放: 1:1")
        for widget in (self.launch_button, self.connect_button,
                       self.disconnect_button, self.kill_button, self.win_d_button,
                       self.win_tab_button, self.toggle_button, self.scale_button):
            row.addWidget(widget)
        self.addLayout(row)

        # RDP 视图嵌入区
        self.embed_layout = QVBoxLayout()
        self.embed_layout.setAlignment(Qt.AlignTop)
        self.addLayout(self.embed_layout, stretch=1)

        # 信号连接
        self.launch_button.clicked.connect(self.launch_okww)
        self.connect_button.clicked.connect(self.connect_desktop)
        self.disconnect_button.clicked.connect(self.disconnect_desktop)
        self.kill_button.clicked.connect(self.kill_desktop)
        self.win_d_button.clicked.connect(lambda: self._shortcut("win_d"))
        self.win_tab_button.clicked.connect(lambda: self._shortcut("win_tab"))
        self.toggle_button.clicked.connect(self.toggle_visibility)
        self.scale_button.clicked.connect(self.toggle_smart_sizing)

    @property
    def name(self):
        return "桌面分身"

    @property
    def icon(self):
        return FluentIcon.IOT

    # -- 动作 ---------------------------------------------------------------

    def launch_okww(self):
        if self._launching:
            self._status.emit("子实例正在启动中，请勿重复点击。")
            return
        session_id = wts_api.get_child_session_id()
        if session_id is None:
            self._status.emit("没有可用的 Child Session，请先启用。")
            return
        self._launching = True
        self.launch_button.setEnabled(False)
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
        finally:
            self._launch_finished.emit()

    def _on_launch_finished(self):
        self._launching = False
        self.launch_button.setEnabled(True)

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
            host.on_event("cursor_captured",
                          lambda d: self._status.emit(
                              "已捕获鼠标，按 Alt 释放。" if d.get("captured") else "已释放鼠标。"))
            hello = host.start()
            host.connect(smart_sizing=self._smart_sizing)
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
        self._desktop_visible = True

    def _clear_embed(self):
        if self.container is not None:
            self.embed_layout.removeWidget(self.container)
            self.container.setParent(None)
            self.container.deleteLater()
            self.container = None

    def disconnect_desktop(self):
        # stop(logoff=True) 会让 RdpHost 注销 Child Session（WTSLogoffSession 阻塞，
        # 最长约数秒）。放在后台线程，避免把 GUI 线程卡住造成窗口失去响应。
        self._clear_embed()
        host = self.host
        self.host = None
        if host is None:
            self._status.emit("已断开桌面分身。")
            return
        self._status.emit("正在断开桌面分身…")
        threading.Thread(target=self._disconnect_worker, args=(host,),
                         daemon=True, name="childsession-disconnect").start()

    def _disconnect_worker(self, host):
        try:
            host.stop(logoff=True)
        except Exception as ex:
            logger.warning(f"停止 RdpHost 失败：{ex}")
        self._status.emit("已断开桌面分身。")

    def kill_desktop(self):
        # 强制终止桌面分身及其中的游戏/子实例：先断开 RDP 视图，再在后台线程里
        # 逐个 TerminateProcess 并注销 Child Session（WTSLogoffSession 会阻塞）。
        self._clear_embed()
        if self.host:
            try:
                self.host.stop(logoff=False)
            except Exception as ex:
                logger.warning(f"停止 RdpHost 失败：{ex}")
            self.host = None
        self._status.emit("正在终止桌面分身及其中的程序…")
        threading.Thread(target=self._kill_worker, daemon=True,
                         name="childsession-kill").start()

    def _kill_worker(self):
        try:
            session_id = wts_api.kill_child_session()
        except Exception as ex:
            logger.exception("终止桌面分身失败")
            self._status.emit(f"终止桌面分身失败：{ex}")
            return
        self._status.emit(
            "已终止桌面分身及其中运行的程序。" if session_id is not None
            else "没有可终止的桌面分身。")

    def _shortcut(self, name):
        if not self.host:
            self._status.emit("尚未连接桌面分身。")
            return
        # 放到后台线程：send_shortcut 是同步管道往返，Win+Tab 会触发子会话渲染
        # Task View 的帧洪流，RdpHost 回包变慢时若在 GUI 线程上等待会把整个窗口卡住。
        threading.Thread(target=self._shortcut_worker, args=(name,), daemon=True,
                         name=f"childsession-shortcut-{name}").start()

    def _shortcut_worker(self, name):
        try:
            self.host.send_shortcut(name)
            self._status.emit(f"已发送 {name}")
        except Exception as ex:
            logger.warning(f"发送快捷键失败：{ex}")
            self._status.emit(f"发送快捷键失败：{ex}")

    def toggle_visibility(self):
        if self.container is None or not self.host:
            self._status.emit("尚未连接桌面分身。")
            return
        self._set_desktop_visible(not self._desktop_visible)
        self._status.emit("已隐藏桌面分身。" if not self._desktop_visible else "已显示桌面分身。")

    def toggle_smart_sizing(self):
        # 1:1（smart_sizing 关）= 鼠标 1:1 但画面会裁剪；适应缩放 = 画面完整但鼠标加速。
        # 在 RDP 端实时切换 SmartSizing，无需重连。
        if not self.host:
            self._status.emit("尚未连接桌面分身。")
            return
        self._smart_sizing = not self._smart_sizing
        try:
            self.host.set_smart_sizing(self._smart_sizing)
        except Exception as ex:
            logger.warning(f"切换缩放失败：{ex}")
            self._status.emit(f"切换缩放失败：{ex}")
            return
        self.scale_button.setText("缩放: 适应" if self._smart_sizing else "缩放: 1:1")
        self._status.emit(
            "已切换为适应缩放（画面完整，鼠标会加速）。" if self._smart_sizing
            else "已切换为 1:1（鼠标 1:1，画面会裁剪）。")

    def _set_desktop_visible(self, visible):
        # 直接对 createWindowContainer 包裹的外来窗口调用 setVisible(False/True) 会在
        # 再次显示时把原生窗口甩到屏幕左上角（Qt 重新嵌入外来窗口的已知问题）。
        # 因此只用 Win32 ShowWindow 切换原生窗口可见性，并折叠/恢复容器高度来回收空间。
        #
        # 顺序很关键：容器几何变化时 Qt 会同步调整外来窗口的尺寸与位置，所以外来窗口
        # 必须在容器变化期间保持可见，否则隐藏期间丢掉的位置更新会让它再次显示时错位
        # 到左上角。
        #   - 隐藏：先折叠容器（可见窗口随之缩到 0），再 SW_HIDE。
        #   - 显示：先 SW_SHOW（窗口在 0 尺寸下先恢复可见），再恢复容器高度（窗口随之放大到正确位置）。
        try:
            import win32con
            import win32gui

            hwnd = int(self.host.host_hwnd or 0)
        except Exception:
            hwnd = 0

        if visible:
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            self.container.setMinimumHeight(200)
            self.container.setMaximumHeight(16777215)
        else:
            self.container.setMinimumHeight(0)
            self.container.setMaximumHeight(0)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        self._desktop_visible = visible

    def _on_download_progress(self, percent, size_str):
        self._status.emit(f"正在下载 RdpHost… {percent:.0f}% ({size_str})")
