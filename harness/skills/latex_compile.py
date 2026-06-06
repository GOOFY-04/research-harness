"""
LaTeXCompileSkill — LaTeX 编译为 PDF

输入：
  - tex_content: LaTeX 源文件内容（必填）
  - tex_filename: 输出文件名（默认 "paper"）
  - compiler: "pdflatex" | "xelatex" | "lualatex"（默认 pdflatex）
  - bibtex: 是否运行 bibtex 处理参考文献（默认 True）
  - output_dir: 输出目录（默认由调用方指定，否则用临时目录）

输出：
  - success: True/False
  - pdf_path: 生成的 PDF 路径（成功时）
  - log: 编译日志
  - errors: 错误列表
  - warnings: 警告列表
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class LaTeXCompileSkill(Skill):
    name = "latex_compile"
    description = "编译 LaTeX 源文件为 PDF，检测错误和警告"

    def validate_inputs(self, inputs: dict) -> bool:
        return "tex_content" in inputs

    def execute(self, inputs: dict) -> dict:
        tex_content = inputs["tex_content"]
        tex_filename = inputs.get("tex_filename", "paper")
        compiler = inputs.get("compiler", "pdflatex")
        run_bibtex = inputs.get("bibtex", True)
        output_dir = inputs.get("output_dir")

        # 检查编译器是否可用
        compiler_path = shutil.which(compiler)
        if compiler_path is None:
            return {
                "success": False,
                "error": f"编译器 '{compiler}' 未安装或不在 PATH 中",
                "errors": [f"Compiler '{compiler}' not found"],
                "warnings": [],
            }

        # 使用临时目录编译（避免污染源目录）
        work_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="latex_"))
        work_dir.mkdir(parents=True, exist_ok=True)

        tex_path = work_dir / f"{tex_filename}.tex"
        tex_path.write_text(tex_content, encoding="utf-8")

        all_errors: list[str] = []
        all_warnings: list[str] = []
        full_log = ""
        pdf_path = work_dir / f"{tex_filename}.pdf"

        try:
            # 第 1 遍编译
            result = subprocess.run(
                [compiler, "-interaction=nonstopmode", "-output-directory", str(work_dir), str(tex_path)],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            log_text = result.stdout + result.stderr
            full_log += log_text
            errors, warnings = _parse_latex_log(log_text)
            all_errors.extend(errors)
            all_warnings.extend(warnings)

            # 如果需要 bibtex，运行它然后重新编译两次
            aux_file = work_dir / f"{tex_filename}.aux"
            if run_bibtex and aux_file.exists():
                bibtex_exe = shutil.which("bibtex")
                if bibtex_exe:
                    try:
                        subprocess.run(
                            [bibtex_exe, tex_filename],
                            cwd=work_dir,
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                    except (subprocess.TimeoutExpired, Exception) as e:
                        all_warnings.append(f"bibtex 运行异常: {e}")

                    # 第 2、3 遍编译（解析引用）
                    for _ in range(2):
                        result = subprocess.run(
                            [compiler, "-interaction=nonstopmode", "-output-directory", str(work_dir), str(tex_path)],
                            cwd=work_dir,
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        log_text = result.stdout + result.stderr
                        full_log += log_text
                        errors, warnings = _parse_latex_log(log_text)
                        all_errors.extend(errors)
                        all_warnings.extend(warnings)

            success = pdf_path.exists() and not _has_fatal_errors(all_errors)

            return {
                "success": success,
                "pdf_path": str(pdf_path) if pdf_path.exists() else None,
                "log": full_log,
                "errors": all_errors,
                "warnings": all_warnings,
                "work_dir": str(work_dir),
            }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "编译超时（120秒）",
                "errors": ["LaTeX compilation timed out"],
                "warnings": all_warnings,
                "pdf_path": None,
            }
        except Exception as e:
            logger.error(f"[LaTeXCompileSkill] 执行失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "errors": [str(e)],
                "warnings": all_warnings,
                "pdf_path": None,
            }


def _parse_latex_log(log: str) -> tuple[list[str], list[str]]:
    """解析 LaTeX 编译日志，提取错误和警告。"""
    errors: list[str] = []
    warnings: list[str] = []

    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue

        # LaTeX 错误行以 ! 开头
        if line.startswith("!"):
            errors.append(line[1:].strip())

        # LaTeX 警告
        if "Warning" in line or "warning" in line:
            # 去重
            if line not in warnings:
                warnings.append(line)

        # 未定义引用
        if "undefined" in line.lower() and ("reference" in line.lower() or "citation" in line.lower()):
            if line not in warnings:
                warnings.append(line)

    return errors, warnings


def _has_fatal_errors(errors: list[str]) -> bool:
    """检查是否存在致命错误（排除一些可忽略的 warning-level 信息）。"""
    # LaTeX 在 nonstopmode 下几乎所有 Error 都会生成 PDF，但质量不好
    # 我们宽容处理：只有明确的 fatal 错误才标记为失败
    fatal_keywords = [
        "Emergency stop",
        "Fatal error",
        "Output file removed",
        "File not found",
    ]
    return any(any(kw.lower() in e.lower() for kw in fatal_keywords) for e in errors)
