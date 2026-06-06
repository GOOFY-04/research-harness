"""
DocumenterAgent — 生成项目文档

功能：
  1. 生成 README.md（项目介绍、安装、使用、实验流程）
  2. 生成 requirements.txt（如果 coding 阶段没有）
  3. 可选：生成 setup.py 或 pyproject.toml

输出：
  - readme: README.md 内容
  - requirements: requirements.txt 内容（如果需要）
"""

import json
import logging
import re

from harness.core.agent import BaseAgent

logger = logging.getLogger(__name__)


class DocumenterAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        return ""  # 在 run() 中自定义

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """生成项目文档。"""
        # 从上游阶段获取信息
        research_question = inputs.get("research_question", "")
        method_name = inputs.get("method_name", "")
        method_overview = inputs.get("method_overview", "")
        files = inputs.get("files") or []
        entry_point = inputs.get("entry_point", "")
        dependencies = inputs.get("dependencies", "")
        run_instructions = inputs.get("run_instructions", "")
        execution_summary = inputs.get("execution_summary", "")

        # ------------------------------------------------------------------
        # 生成 README.md
        # ------------------------------------------------------------------
        readme_prompt = f"""你是一位技术文档专家。请为以下研究项目生成一份完整的 README.md。

研究问题：{research_question}
方法名称：{method_name}
方法概述：{method_overview}

代码文件：
{json.dumps([f['path'] for f in files], ensure_ascii=False, indent=2)}

入口文件：{entry_point}
依赖：
{dependencies}

运行说明：
{run_instructions}

执行结果：
{execution_summary}

请生成一份专业的 README.md，包含以下章节：
1. 项目标题和简介（1-2段）
2. 方法概述（3-4段，说明核心思想）
3. 安装指南（pip install 命令）
4. 快速开始（如何运行训练/推理）
5. 代码结构（文件树和说明）
6. 实验流程（如何复现论文结果）
7. 引用（BibTeX 格式，如果发表）
8. 许可证（MIT）

要求：
- 使用 Markdown 格式
- 代码块用 ```bash 或 ```python 包裹
- 简洁专业，适合 GitHub 展示
- 不要输出 JSON，直接输出 Markdown 文本"""

        logger.info(f"[DocumenterAgent] 生成 README.md")
        readme_raw = self._call_llm(readme_prompt)

        # 提取 markdown（去除可能的围栏）
        md_match = re.search(r"```(?:markdown|md)?\s*\n?([\s\S]*?)\n?```", readme_raw)
        readme = md_match.group(1).strip() if md_match else readme_raw.strip()

        # ------------------------------------------------------------------
        # 组装输出
        # ------------------------------------------------------------------
        output = {
            "readme": readme,
            "requirements": dependencies,  # 直接使用 coding 阶段的依赖
        }

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"method": method_name, "readme_length": len(readme)},
                tags=["DocumenterAgent", stage_id],
            )

        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return {"readme": raw_text}
