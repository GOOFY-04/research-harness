"""
ExecutorAgent — 自主运行实验代码

功能：
  1. 分析生成的代码结构
  2. 安装依赖（requirements.txt）
  3. 运行训练/测试脚本
  4. 捕获输出和错误
  5. 生成执行报告
  6. 可选：调用 skills 进行代码审查

输出：
  - execution_log: 执行日志
  - success: 是否成功
  - errors: 错误信息（如果有）
  - metrics: 提取的指标（如果有）
  - code_review: 代码审查结果（如果启用）
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from harness.core.agent import BaseAgent
from harness.core.skill import get_global_registry

logger = logging.getLogger(__name__)


class ExecutorAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def __init__(self, *args, timeout: int = 600, enable_code_review: bool = False, max_fix_attempts: int = 3, **kwargs):
        """
        Args:
            timeout: 代码执行超时时间（秒），默认 10 分钟
            enable_code_review: 是否启用代码审查 skill
            max_fix_attempts: 测试失败时最大自动修复次数
        """
        super().__init__(*args, **kwargs)
        self.timeout = timeout
        self.enable_code_review = enable_code_review
        self.max_fix_attempts = max_fix_attempts

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        return ""  # 不使用标准 prompt，在 run() 中自定义

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """执行代码并生成报告，支持自动修复和数据集注入。"""
        files = inputs.get("files", [])
        entry_point = inputs.get("entry_point", "")
        dependencies = inputs.get("dependencies", "")
        test_snippet = inputs.get("test_snippet", "")
        session_dir = state.get("session_dir", "")

        # 数据集路径注入（优先级：state > 环境变量）
        dataset_path = state.get("dataset_path", os.environ.get("RESEARCH_DATASET_PATH", ""))

        if not session_dir:
            return {"error": "缺少 session_dir", "success": False}

        code_dir = Path(session_dir) / "code"
        code_dir.mkdir(parents=True, exist_ok=True)

        # 用于记录自动修复历史
        fix_history = []

        # ------------------------------------------------------------------
        # 1. 写入代码文件
        # ------------------------------------------------------------------
        logger.info(f"[ExecutorAgent] 写入 {len(files)} 个代码文件到 {code_dir}")
        self._write_files(files, code_dir)

        # ------------------------------------------------------------------
        # 2. 写入 requirements.txt
        # ------------------------------------------------------------------
        if dependencies:
            req_file = code_dir / "requirements.txt"
            req_file.write_text(dependencies, encoding="utf-8")
            logger.info(f"[ExecutorAgent] 写入 requirements.txt")

        # ------------------------------------------------------------------
        # 3. 安装依赖
        # ------------------------------------------------------------------
        install_log, install_ok = self._install_dependencies(code_dir, dependencies)

        # ------------------------------------------------------------------
        # 4. 运行测试代码（带自动修复循环）
        # ------------------------------------------------------------------
        test_log = ""
        test_success = None
        if test_snippet and install_ok:
            test_log, test_success = self._run_test_with_autofix(
                test_snippet, files, code_dir, dataset_path, fix_history
            )
        elif test_snippet and not install_ok:
            test_log = "依赖安装失败，跳过测试执行。\n" + install_log
            logger.warning(f"[ExecutorAgent] 依赖安装失败，跳过测试")

        # ------------------------------------------------------------------
        # 5. 使用 LLM 分析执行结果
        # ------------------------------------------------------------------
        analysis = self._analyze_results(files, install_log, test_log, test_success, fix_history)

        # ------------------------------------------------------------------
        # 6. 组装输出
        # ------------------------------------------------------------------
        overall_success = (
            (test_success if test_success is not None else True)
            and not analysis.get("parse_error", False)
        )
        output = {
            "code_dir": str(code_dir),
            "install_log": install_log,
            "test_log": test_log,
            "test_success": test_success,
            "analysis": analysis,
            "success": overall_success,
            "fix_history": fix_history,  # 记录修复历史
            "dataset_path": dataset_path,  # 记录使用的数据集路径
        }

        # ------------------------------------------------------------------
        # 7. 可选：调用 code_review skill
        # ------------------------------------------------------------------
        if self.enable_code_review and files:
            output["code_review"] = self._run_code_review(files)

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={
                    "test_success": test_success,
                    "summary": analysis.get("summary", ""),
                    "fixes_applied": len(fix_history),
                },
                tags=["ExecutorAgent", stage_id],
            )

        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)

    # ========================================================================
    # 辅助方法
    # ========================================================================

    def _write_files(self, files: list, code_dir: Path):
        """写入代码文件到目录"""
        for file_info in files:
            file_path = code_dir / file_info["path"]
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(file_info["content"], encoding="utf-8")

    def _install_dependencies(self, code_dir: Path, dependencies: str) -> tuple[str, bool]:
        """安装依赖，返回 (日志, 是否成功)"""
        if not dependencies:
            return "", True

        logger.info(f"[ExecutorAgent] 安装依赖...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
                cwd=code_dir,
                capture_output=True,
                text=True,
                timeout=600,  # 10 分钟超时
            )
            install_log = result.stdout + result.stderr
            if result.returncode != 0:
                logger.warning(f"[ExecutorAgent] 依赖安装失败: {result.stderr[:200]}")
                return install_log, False
            return install_log, True
        except subprocess.TimeoutExpired:
            msg = "依赖安装超时（10分钟）"
            logger.warning(f"[ExecutorAgent] {msg}")
            return msg, False
        except Exception as e:
            msg = f"依赖安装异常: {e}"
            logger.warning(f"[ExecutorAgent] {msg}")
            return msg, False

    def _run_test_with_autofix(
        self,
        test_snippet: str,
        files: list,
        code_dir: Path,
        dataset_path: str,
        fix_history: list
    ) -> tuple[str, bool]:
        """运行测试，失败时自动修复，返回 (日志, 是否成功)"""
        test_file = code_dir / "test_quick.py"

        for attempt in range(self.max_fix_attempts + 1):
            # 写入测试文件（可能已被修复）
            test_file.write_text(test_snippet, encoding="utf-8")

            # 运行测试
            logger.info(f"[ExecutorAgent] 运行测试代码 (attempt {attempt + 1}/{self.max_fix_attempts + 1})...")
            test_log, test_success = self._run_test(test_file, code_dir, dataset_path)

            if test_success:
                logger.info(f"[ExecutorAgent] 测试成功")
                return test_log, True

            # 测试失败
            if attempt < self.max_fix_attempts:
                logger.warning(f"[ExecutorAgent] 测试失败，尝试自动修复...")
                fixed_snippet, fix_log = self._auto_fix_test(test_snippet, test_log, files)

                if fixed_snippet and fixed_snippet != test_snippet:
                    test_snippet = fixed_snippet
                    fix_history.append({
                        "attempt": attempt + 1,
                        "error": test_log[-500:],  # 最后 500 字符
                        "fix_applied": fix_log,
                    })
                    logger.info(f"[ExecutorAgent] 已应用修复，重新测试...")
                else:
                    logger.warning(f"[ExecutorAgent] 无法修复，停止重试")
                    break
            else:
                logger.warning(f"[ExecutorAgent] 已达最大重试次数")

        return test_log, False

    def _run_test(self, test_file: Path, code_dir: Path, dataset_path: str) -> tuple[str, bool]:
        """运行单次测试，返回 (日志, 是否成功)"""
        env = os.environ.copy()
        if dataset_path:
            env["DATASET_PATH"] = dataset_path

        try:
            result = subprocess.run(
                [sys.executable, test_file.name],
                cwd=code_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
            log = result.stdout + result.stderr
            success = result.returncode == 0
            return log, success
        except subprocess.TimeoutExpired:
            return f"测试超时（{self.timeout}秒）", False
        except Exception as e:
            return f"测试异常: {e}", False

    def _auto_fix_test(self, test_snippet: str, error_log: str, files: list) -> tuple[str, str]:
        """使用 LLM 自动修复测试代码，返回 (修复后的代码, 修复说明)"""
        file_signatures = self._extract_signatures(files)

        fix_prompt = f"""你是一位 Python 调试专家。以下测试代码运行失败，请修复它。

测试代码：
```python
{test_snippet}
```

错误日志：
```
{error_log[-1500:]}
```

项目中的模块签名（供参考）：
{file_signatures[:2000]}

常见问题：
1. 函数/类的参数名或数量不匹配
2. 返回值的结构不符合预期
3. 缺少必要的 import

请输出 JSON：
{{
  "fixed_code": "修复后的完整测试代码（不要用围栏包裹）",
  "fix_explanation": "修复说明（1-2句话）"
}}"""

        try:
            response = self._call_llm(fix_prompt)
            result = self._parse_json(response)
            if result.get("parse_error"):
                return "", "LLM 输出解析失败"
            return result.get("fixed_code", ""), result.get("fix_explanation", "")
        except Exception as e:
            logger.warning(f"[ExecutorAgent] 自动修复失败: {e}")
            return "", str(e)

    def _extract_signatures(self, files: list) -> str:
        """从代码文件中提取类和函数签名"""
        import re
        signatures = []
        for file_info in files[:5]:  # 只看前5个文件
            content = file_info.get("content", "")
            path = file_info.get("path", "")
            # 提取 class 和 def
            classes = re.findall(r"^class\s+(\w+).*?:", content, re.MULTILINE)
            funcs = re.findall(r"^\s{0,8}def\s+(\w+)\((.*?)\).*?:", content, re.MULTILINE)
            if classes or funcs:
                signatures.append(f"\n# {path}")
                for cls in classes:
                    signatures.append(f"class {cls}")
                for name, args in funcs[:10]:  # 每个文件最多10个函数
                    signatures.append(f"  def {name}({args[:50]})")
        return "\n".join(signatures)

    def _analyze_results(
        self,
        files: list,
        install_log: str,
        test_log: str,
        test_success: bool,
        fix_history: list
    ) -> dict:
        """使用 LLM 分析执行结果"""
        fix_summary = ""
        if fix_history:
            fix_summary = f"\n自动修复历史（共 {len(fix_history)} 次）：\n"
            for fix in fix_history:
                fix_summary += f"- Attempt {fix['attempt']}: {fix['fix_applied'][:100]}\n"

        analysis_prompt = f"""你是一位代码执行分析专家。请分析以下代码执行结果。

代码结构：
{json.dumps([f['path'] for f in files], ensure_ascii=False)}

依赖安装日志：
{install_log[:1000] if install_log else '（无）'}

测试执行日志：
{test_log[-2000:] if test_log else '（无）'}

测试是否成功：{test_success}
{fix_summary}

请输出 JSON 格式的分析报告：
{{
  "success": true/false,
  "summary": "执行结果总结（2-3句话）",
  "errors": ["错误1", "错误2"],
  "warnings": ["警告1"],
  "suggestions": ["建议1", "建议2"],
  "metrics": {{"key": "value"}}
}}"""

        analysis_raw = self._call_llm(analysis_prompt)
        return self._parse_json(analysis_raw)

    def _run_code_review(self, files: list) -> list:
        """调用 code_review skill 审查代码"""
        logger.info(f"[ExecutorAgent] 调用 code_review skill")
        reviews = []
        try:
            registry = get_global_registry()
            for file_info in files[:3]:  # 审查前3个文件
                result = registry.execute("code_review", {
                    "code": file_info["content"],
                    "language": "python",
                })
                if result.get("success"):
                    reviews.append({
                        "file": file_info["path"],
                        "score": result.get("score", 0),
                        "issues": result.get("issues", []),
                        "summary": result.get("summary", ""),
                    })
        except Exception as e:
            logger.warning(f"[ExecutorAgent] code_review skill 调用失败: {e}")
        return reviews
