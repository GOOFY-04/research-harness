"""
代码执行工具 — 在隔离子进程中运行 Python 代码片段

用于验证 CoderAgent 生成的代码是否能跑通。
"""

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from harness.core.io import safe_path
from .validation import validate_files
from .process import run_command


def run_python_snippet(
    code: str,
    timeout: int = 30,
    extra_packages: list[str] | None = None,
) -> dict:
    """
    在子进程中执行 Python 代码片段。

    Returns:
        {
            "success": bool,
            "stdout": str,
            "stderr": str,
            "returncode": int,
        }
    """
    if extra_packages:
        raise ValueError("Use ExecutorAgent's isolated environment to install dependencies")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(textwrap.dedent(code))
        tmp_path = f.name

    try:
        return run_command([sys.executable, tmp_path], Path(tmp_path).parent, timeout, 2000)
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"执行超时（>{timeout}s）",
            "returncode": -1,
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def write_code_files(files: list[dict], base_dir: str | Path) -> list[str]:
    """
    将 CoderAgent 输出的文件列表写入磁盘。

    Args:
        files: [{"path": "相对路径", "content": "代码内容"}, ...]
        base_dir: 写入的根目录

    Returns:
        成功写入的文件路径列表
    """
    base = Path(base_dir)
    if not files:
        return []
    validate_files(files, base)
    written = []
    for f in files:
        target = safe_path(base, f["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
        written.append(str(target))
    return written
