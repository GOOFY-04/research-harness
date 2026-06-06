#!/usr/bin/env python3
"""
快速验证 research-harness 的三大新功能
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def demo_feature_1_code_execution():
    """功能 1: 自主代码执行"""
    print("=" * 70)
    print("功能 1: 自主代码执行 (ExecutorAgent)")
    print("=" * 70)
    print("""
ExecutorAgent 可以：
  ✓ 自动写入生成的代码文件
  ✓ 安装依赖 (pip install -r requirements.txt)
  ✓ 运行测试代码并捕获输出
  ✓ 使用 LLM 分析执行结果
  ✓ 可选：调用 code_review skill 进行代码审查

示例输出：
  {
    "test_success": true,
    "analysis": {
      "summary": "测试通过，模型 forward pass 正常",
      "metrics": {"loss": 0.5, "accuracy": 0.9}
    },
    "code_review": [...]  // 如果启用
  }

在工作流中的位置：
  coding → code_execution → self_review
    """)


def demo_feature_2_documentation():
    """功能 2: 自动文档生成"""
    print("\n" + "=" * 70)
    print("功能 2: 自动文档生成 (DocumenterAgent)")
    print("=" * 70)
    print("""
DocumenterAgent 可以：
  ✓ 为每个项目生成完整的 README.md
  ✓ 包含：项目介绍、方法概述、安装指南、快速开始、代码结构、实验流程
  ✓ 使用专业的 Markdown 格式，适合 GitHub 展示

生成的 README.md 包含：
  1. 项目标题和简介
  2. 方法概述
  3. 安装指南
  4. 快速开始
  5. 代码结构
  6. 实验流程
  7. 引用和许可证

输出位置：
  sessions/<session_id>/README.md

在工作流中的位置：
  code_execution + paper_writing → documentation
    """)


def demo_feature_3_skills():
    """功能 3: Skills 系统"""
    print("\n" + "=" * 70)
    print("功能 3: Skills 系统")
    print("=" * 70)
    print("""
Skills 是可插拔的功能模块，可以被 Agent 调用来完成特定任务。

内置 3 个 Skills：

1. code_review - 代码审查
   输入: {"code": "...", "language": "python"}
   输出: {"score": 85, "issues": [...], "suggestions": [...]}

2. dependency_check - 依赖检查
   输入: {"dependencies": "requirements.txt 内容"}
   输出: {"outdated": [...], "conflicts": [...], "security_issues": [...]}

3. test_generation - 测试生成
   输入: {"code": "...", "test_framework": "pytest"}
   输出: {"test_code": "...", "coverage_estimate": "..."}

使用方式：
    """)

    # 实际演示
    from harness.core.skill import get_global_registry
    from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill

    registry = get_global_registry()
    registry.register(CodeReviewSkill())
    registry.register(DependencyCheckSkill())
    registry.register(TestGenerationSkill())

    print("  from harness.core.skill import get_global_registry")
    print("  registry = get_global_registry()")
    print("  result = registry.execute('code_review', {'code': '...'})")
    print()
    print(f"✓ 当前已注册 {len(registry.list_skills())} 个 skills")


def demo_custom_skill():
    """演示如何创建自定义 Skill"""
    print("\n" + "=" * 70)
    print("创建自定义 Skill")
    print("=" * 70)
    print("""
from harness.core.skill import Skill

class MyCustomSkill(Skill):
    name = "my_skill"
    description = "我的自定义技能"

    def validate_inputs(self, inputs: dict) -> bool:
        return "required_param" in inputs

    def execute(self, inputs: dict) -> dict:
        # 实现你的逻辑
        return {"success": True, "result": "..."}

# 注册
from harness.core.skill import get_global_registry
registry = get_global_registry()
registry.register(MyCustomSkill())
    """)


def demo_workflow():
    """展示完整工作流"""
    print("\n" + "=" * 70)
    print("完整工作流 (8 个阶段)")
    print("=" * 70)
    print("""
1. planning          - 选题与研究规划
2. literature        - 文献调研
3. method_design     - 方法设计
4. coding            - 代码实现
5. code_execution    - 代码执行与验证 ⭐ 新增
6. self_review       - 自我审稿
7. paper_writing     - 论文撰写
8. documentation     - 文档生成 ⭐ 新增

运行命令：
  python main.py run --direction "基于 Transformer 的时间序列预测"

输出结构：
  sessions/<session_id>/
  ├── README.md              # ⭐ 项目文档
  ├── code/                  # 代码文件
  │   ├── models/
  │   ├── train.py
  │   └── requirements.txt
  ├── output/
  │   └── paper.tex         # 论文草稿
  └── state.json            # 工作流状态
    """)


def main():
    print("\n" + "🚀 " * 20)
    print("research-harness 新功能概览")
    print("🚀 " * 20 + "\n")

    demo_feature_1_code_execution()
    demo_feature_2_documentation()
    demo_feature_3_skills()
    demo_custom_skill()
    demo_workflow()

    print("\n" + "=" * 70)
    print("快速开始")
    print("=" * 70)
    print("""
# 1. 查看所有可用命令
python main.py --help

# 2. 运行完整流程
python main.py run --direction "你的研究方向"

# 3. 演示 Skills 系统
python examples/demo_skills.py

# 4. 运行测试
python tests/test_new_features.py

# 5. 查看文档
cat README.md
cat USAGE.md
cat CHANGELOG.md
    """)

    print("=" * 70)
    print("✅ research-harness 已就绪，开始你的科研之旅吧！")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
