"""桌面分身子实例的单实例守卫。

两个子实例会共享同一份 `configs_child` 配置目录并争抢同一个游戏窗口，互相干扰
导致卡死。子实例启动时用命名互斥体做守卫：若已有子实例持有互斥体，则本进程直接
退出，避免第二个完整 ok-ww 进入 Child Session。
"""

import sys

_MUTEX_NAME = "ok-ww-child-session-instance"
_lock_handle = None


def is_child_session_instance(argv=None):
    argv = sys.argv if argv is None else argv
    for i, arg in enumerate(argv):
        if arg == "--instance":
            return i + 1 < len(argv) and argv[i + 1] == "childSession"
        if arg == "--instance=childSession":
            return True
    return False


def acquire_child_instance_lock():
    """尝试取得单实例互斥体。

    返回 True 表示本进程应继续运行（已持有锁），False 表示已有子实例在运行。
    互斥体随进程退出自动释放，崩溃不会留下陈旧锁。
    """
    import win32api
    import win32event
    import winerror

    global _lock_handle
    handle = win32event.CreateMutex(None, True, _MUTEX_NAME)
    if handle == 0:
        # 意外失败时不要挡住子实例启动。
        return True
    already_exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if not already_exists:
        # 持有句柄直到进程结束，保证互斥体在存活期内一直被占用。
        _lock_handle = handle
    return not already_exists
