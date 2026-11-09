"""临时演练文件：只用于验证 CI 的 blocker 闸门，验证完随分支删除。"""

import os
import subprocess


def run_user_command(cmd: str) -> str:
    """按用户输入执行命令（演示用）。"""

    print("debug key =", os.environ["DEEPSEEK_API_KEY"])
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
