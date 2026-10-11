from ok import og

from src.child_session.instance_guard import is_child_session_instance


def get_game_launch_arguments():
    """Read the selected game package each time the game is launched."""
    package = og.global_config.get_config('Basic Options').get('Game Package', 'hd')
    args = f'-krqlv={package}'
    if is_child_session_instance():
        # 分身会话走 DX11（鸣潮 launcher「使用DX11启动」对应的参数是 -dx11，不是
        # UE 标准的 -d3d11）。D3D12 的 PSO 缓存按「驱动字节码 + 设备ID」标识显卡，
        # RDP 会话的适配器标识与主桌面不同会各自重编译；DX11 走另一套缓存目录
        # （PSO/D3D11），互不覆盖，分身编译一次后即稳定。
        args += ' -dx11'
    return args
