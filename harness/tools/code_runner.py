"""
代码执行工具 — 在隔离子进程中运行 Python 代码片段

用于验证 CoderAgent 生成的代码是否能跑通。
"""

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


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
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(textwrap.dedent(code))
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:2000],
            "returncode": result.returncode,
        }
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
    written = []
    for f in files:
        target = base / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
        written.append(str(target))
    return written
