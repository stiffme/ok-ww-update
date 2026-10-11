"""Manages the .NET RdpHost.exe helper process and its control pipe.

See `native/RdpHost/README.md` for the helper and the wire protocol. The helper
owns the RDP ActiveX control; this class launches it, exposes request/response
calls, and dispatches its events (login, disconnect, focus, raw mouse) to callbacks.
"""

import json
import os
import subprocess
import threading
import uuid

from ok import Logger
from ok.util.file import get_path_relative_to_exe

from src.child_session import pipe as pipe_mod

logger = Logger.get_logger(__name__)

_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080


class RdpHostError(RuntimeError):
    pass


class RdpHostProcess:
    def __init__(self, host_width=_DEFAULT_WIDTH, host_height=_DEFAULT_HEIGHT):
        self._width = host_width
        self._height = host_height
        self._pipe_name = f"okww.rdphost.{os.getpid()}.{uuid.uuid4().hex}"
        self._proc = None
        self._client = None
        self._reader = None
        self._running = False
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._event_handlers = {}
        self._binary_handlers = []
        self._hello = {}

    @staticmethod
    def exe_path():
        return get_path_relative_to_exe(os.path.join("assets", "native", "RdpHost", "RdpHost.exe"))

    @property
    def host_hwnd(self):
        return self._hello.get("hostHwnd")

    @property
    def hello(self):
        return dict(self._hello)

    def start(self, timeout=30.0):
        exe = self.exe_path()
        if not exe or not os.path.isfile(exe):
            raise RdpHostError(f"找不到 RdpHost.exe：{exe}（首次使用会自动下载，或见 native/RdpHost/README.md）")

        self._proc = subprocess.Popen([
            exe, "--pipe", self._pipe_name,
            "--host-width", str(self._width),
            "--host-height", str(self._height),
            "--parent-pid", str(os.getpid()),
        ])
        client = pipe_mod.PipeClient(self._pipe_name)
        client.connect(timeout=timeout)
        self._client = client
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, name="rdphost-reader", daemon=True)
        self._reader.start()
        self._hello = self.request("hello", timeout=timeout)
        logger.info(f"RdpHost 已启动 pid={self._proc.pid} session={self._hello.get('sessionId')} "
                    f"hwnd={self._hello.get('hostHwnd')}")
        return self._hello

    def request(self, op, args=None, timeout=10.0):
        if not self._client:
            raise RdpHostError("RdpHost 尚未启动。")
        request_id = uuid.uuid4().hex
        event = threading.Event()
        slot = {"event": event, "response": None}
        with self._lock:
            self._pending[request_id] = slot
        try:
            message = json.dumps({"v": 1, "id": request_id, "op": op, "args": args or {}},
                                 ensure_ascii=False)
            # 串行化写入，避免后台线程（快捷键等）与 GUI 线程并发写帧导致管道流交错。
            with self._write_lock:
                self._client.write_frame(pipe_mod.PAYLOAD_JSON, message.encode("utf-8"))
            if not event.wait(timeout):
                raise RdpHostError(f"RdpHost 请求超时：{op}")
            response = slot["response"]
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

        if not response.get("ok"):
            raise RdpHostError(response.get("error") or f"RdpHost 请求失败：{op}")
        return response.get("data") or {}

    def on_event(self, name, callback):
        self._event_handlers.setdefault(name, []).append(callback)

    def on_binary(self, callback):
        self._binary_handlers.append(callback)

    def _read_loop(self):
        while self._running:
            try:
                frame = self._client.read_frame()
            except Exception as ex:
                if self._running:
                    logger.warning(f"RdpHost 管道读取结束：{ex}")
                break
            if frame is None:
                break
            payload_type, payload = frame
            if payload_type == pipe_mod.PAYLOAD_JSON:
                self._handle_json(payload)
            else:
                for callback in list(self._binary_handlers):
                    try:
                        callback(payload_type, payload)
                    except Exception as ex:
                        logger.error(f"RdpHost 二进制帧处理失败：{ex}")

    def _handle_json(self, payload):
        try:
            message = json.loads(payload.decode("utf-8"))
        except Exception as ex:
            logger.warning(f"解析 RdpHost 消息失败：{ex}")
            return

        op = message.get("op")
        if op == "response":
            with self._lock:
                slot = self._pending.get(message.get("id"))
            if slot is not None:
                slot["response"] = message
                slot["event"].set()
        elif op == "event":
            name = message.get("event")
            data = message.get("data") or {}
            for callback in list(self._event_handlers.get(name, ())):
                try:
                    callback(data)
                except Exception as ex:
                    logger.error(f"RdpHost 事件处理失败 {name}：{ex}")

    def stop(self, logoff=False):
        if self._client and self._client.connected:
            try:
                self.request("shutdown", {"logoff": logoff}, timeout=5.0)
            except Exception:
                pass
        self._running = False
        if self._client:
            self._client.close()
            self._client = None
        if self._proc:
            try:
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # -- convenience wrappers -------------------------------------------------

    def connect(self, width=None, height=None, smart_sizing=False, audio_muted=False,
                send_system_shortcuts=True):
        return self.request("connect", {
            "width": width or self._width,
            "height": height or self._height,
            "smartSizing": smart_sizing,
            "audioMuted": audio_muted,
            "sendSystemShortcuts": send_system_shortcuts,
        })

    def disconnect(self):
        return self.request("disconnect")

    def resize(self, width, height):
        return self.request("resize", {"width": width, "height": height})

    def set_smart_sizing(self, enabled):
        return self.request("set_smart_sizing", {"enabled": bool(enabled)})

    def set_audio_muted(self, muted):
        return self.request("set_audio_muted", {"muted": bool(muted)})

    def set_send_system_shortcuts(self, enabled):
        return self.request("set_send_system_shortcuts", {"enabled": bool(enabled)})

    def send_shortcut(self, name):
        return self.request("send_shortcut", {"name": name})

    def focus_input(self):
        return bool(self.request("focus_input").get("focused"))

    def query_state(self):
        return self.request("query_state")
