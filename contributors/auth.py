"""GitHub token 获取。独立模块因为涉及子进程，需单独 mock。"""
import os
import shutil
import subprocess


class AuthError(RuntimeError):
    pass


def get_token() -> str:
    """环境变量优先，回退 gh auth token。"""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val

    if shutil.which("gh") is None:
        raise AuthError(
            "未找到 token。请设置环境变量 GITHUB_TOKEN，"
            "或安装 gh CLI 并执行 gh auth login"
        )
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AuthError(
            "gh auth token 执行失败。请先执行 gh auth login，"
            "或设置环境变量 GITHUB_TOKEN"
        ) from exc
    token = out.stdout.strip()
    if not token:
        raise AuthError("gh auth token 返回空值，请重新执行 gh auth login")
    return token
