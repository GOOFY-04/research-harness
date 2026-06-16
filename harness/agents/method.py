"""
MethodAgent — 方法设计

输入：research_question, research_gaps, key_baselines（来自 LiteratureAgent）
输出：
  - method_name: 方法名称
  - overview: 方法概述
  - components: 核心模块列表
  - algorithm: 伪代码
  - method_section_draft: 论文 Method 节草稿
"""

import json
import re

from harness.core.agent import BaseAgent


class MethodAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    use_extended_thinking = False
    thinking_budget = 10000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        rq = inputs.get("research_question", "")
        gaps = inputs.get("research_gaps", [])
        baselines = inputs.get("key_baselines", [])
        related_work = inputs.get("related_work_draft", "")

        return f"""请为以下研究问题设计一个创新性方法，并定义完整的代码接口契约。

研究问题：{rq}

已识别的研究空白：
{json.dumps(gaps, ensure_ascii=False, indent=2)}

主要基线方法：{', '.join(baselines) if baselines else '未指定'}

相关工作摘要：
{related_work[:1000] if related_work else '（无）'}

请输出如下 JSON 结构（注意：interface_manifest 字段极其重要，定义了跨文件接口契约）：

{{
  "method_name": "方法名称（英文缩写 + 中文全称）",
  "overview": "方法概述（3-5句话，说明核心思路）",
  "key_insight": "核心洞察（1-2句话）",
  "components": [
    {{
      "name": "模块名称",
      "role": "该模块的作用",
      "novelty": "相比现有方法的创新点",
      "implementation_hint": "实现要点"
    }}
  ],
  "algorithm": "伪代码（用缩进表示层级，不超过30行）",
  "interface_manifest": {{
    "modules": [
      {{
        "file": "model.py",
        "description": "核心模型定义",
        "exports": [
          "class ModelName(nn.Module): __init__(self, param1: int, param2: float=0.1); forward(self, x: Tensor) -> Tensor"
        ]
      }}
    ],
    "cross_file_contracts": {{
      "train.py": [
        "from model import ModelName",
        "from dataset import DatasetClass",
        "from loss import LossClass"
      ]
    }},
    "data_format": {{
      "dataset_item": "{{'image': Tensor[B,C,H,W], 'label': Tensor[B], ...}}",
      "batch": "{{'images': Tensor[B,K,C,H,W], 'cameras': List[Camera], ...}}"
    }}
  }},
  "complexity": {{
    "time": "时间复杂度分析",
    "space": "空间复杂度分析"
  }},
  "method_section_draft": "论文 Method 节草稿（600-800字）",
  "ablation_targets": ["消融实验目标1", "消融实验目标2"]
}}

interface_manifest 编写规范：
- modules.exports 中每个类/函数的签名必须精确（参数名、类型、默认值）
- cross_file_contracts 列出所有跨文件导入关系
- 确保同一类名在 import 方和 export 方完全一致
- data_format 描述数据集的 __getitem__ 返回值结构
- 这些契约将在代码生成时作为硬约束，并在运行时进行验证"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
