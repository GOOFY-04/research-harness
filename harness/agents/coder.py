"""
CoderAgent — 代码实现（多轮生成，避免单次输出截断）

策略：
  第1轮：生成文件清单（路径 + 描述，无内容）
  第2轮起：逐文件生成完整代码内容
输出：
  - files: [{path, description, content}]
  - entry_point, dependencies, run_instructions, test_snippet
"""

import json
import logging
import re

from harness.core.agent import BaseAgent

logger = logging.getLogger(__name__)


class CoderAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        # 仅用于 WorkflowEngine 调用 run() 时传入 inputs，实际 prompt 在 run() 里构建
        return ""

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """多轮生成：先清单，再逐文件。"""
        method_name = inputs.get("method_name", "")
        components = inputs.get("components", [])
        algorithm = inputs.get("algorithm", "")
        overview = inputs.get("overview", "")

        context = f"""方法名称：{method_name}
方法概述：{overview}
核心模块：
{json.dumps(components, ensure_ascii=False, indent=2)}
算法伪代码：
{algorithm}"""

        # ------------------------------------------------------------------
        # 第1轮：获取文件清单（只要路径和描述，不要内容）
        # ------------------------------------------------------------------
        manifest_prompt = f"""你是一位 PyTorch 专家，请为以下方法规划代码文件结构。

{context}

请输出 JSON 文件清单（只需路径和描述，不需要代码内容）：
{{
  "files": [
    {{"path": "相对路径", "description": "功能描述（一句话）"}}
  ],
  "entry_point": "主入口文件路径",
  "dependencies": "requirements.txt 内容（每行一个包，带版本号）",
  "run_instructions": "运行说明（markdown，3-5行）",
  "test_snippet": "快速验证 forward pass 的测试代码（15-20行 Python）"
}}

要求：
- 使用 PyTorch，每个核心模块独立成文件
- 包含训练脚本和推理脚本
- 文件数量控制在 6-10 个"""

        logger.info(f"[CoderAgent] 第1轮：生成文件清单")
        manifest_raw = self._call_llm(manifest_prompt)
        manifest = self._parse_json(manifest_raw)

        if manifest.get("parse_error"):
            logger.warning("[CoderAgent] 文件清单解析失败，返回原始输出")
            return {"raw": manifest_raw, "parse_error": True}

        file_list = manifest.get("files", [])
        logger.info(f"[CoderAgent] 清单包含 {len(file_list)} 个文件")

        # ------------------------------------------------------------------
        # 第2轮起：逐文件生成代码内容
        # ------------------------------------------------------------------
        filled_files = []
        for i, file_info in enumerate(file_list):
            path = file_info.get("path", f"file_{i}.py")
            desc = file_info.get("description", "")
            logger.info(f"[CoderAgent] 生成文件 ({i+1}/{len(file_list)}): {path}")

            file_prompt = f"""请为以下文件生成完整的 Python 代码。

项目背景：
{context}

当前文件：
  路径：{path}
  功能：{desc}

已规划的其他文件：
{json.dumps([f['path'] for f in file_list if f['path'] != path], ensure_ascii=False)}

要求：
1. 代码完整可运行，包含所有 import
2. 包含类型注解和 docstring
3. 如果是模型文件，确保 forward() 方法完整
4. 不要输出 JSON，直接输出 Python 代码（用 ```python 围栏包裹）"""

            code_raw = self._call_llm(file_prompt)

            # 提取代码块
            code_match = re.search(r"```(?:python)?\s*\n?([\s\S]*?)\n?```", code_raw)
            content = code_match.group(1).strip() if code_match else code_raw.strip()

            filled_files.append({
                "path": path,
                "description": desc,
                "content": content,
            })

        output = {
            "files": filled_files,
            "entry_point": manifest.get("entry_point", ""),
            "dependencies": manifest.get("dependencies", ""),
            "run_instructions": manifest.get("run_instructions", ""),
            "test_snippet": manifest.get("test_snippet", ""),
        }

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"method": method_name, "files": [f["path"] for f in filled_files]},
                tags=["CoderAgent", stage_id],
            )

        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
