if __name__ == '__main__':
    # 桌面分身子实例只允许存在一个：重复启动会争抢同一份 configs_child 与游戏窗口。
    # 用命名互斥体做守卫，检测到已有子实例在运行就直接退出。
    from src.child_session import instance_guard
    if instance_guard.is_child_session_instance() and not instance_guard.acquire_child_instance_lock():
        raise SystemExit(0)

    from config import config
    from ok import OK

    config = config
    ok = OK(config)
    ok.start()
