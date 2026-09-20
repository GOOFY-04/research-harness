# research-harness 更新记录

## 2026-09-18 — 流程可靠性与产物校验

- 修复执行目录缺失、失败阶段误标完成、嵌套输入映射和无效默认思考预算。
- 按依赖拓扑调度；代码执行成为审稿和论文的前置依赖。
- 修复 no-resume、resume 自定义工作流、重试耗尽恢复及下游失效传播。
- 加入结构校验、模型截断检测、按扩展名生成、代码语法与本地导入校验。
- 限制输出路径，使用独立执行目录与 session 虚拟环境，记录真实日志和指标。
- 修复 Markdown 围栏提取；参考文献从检索元数据生成独立 references.bib。
- 接通 Skills 配置，明确依赖检查的能力边界；模型客户端延迟初始化。
- 增加历史归档、原子持久化、进程锁、配置路径解析和非零失败退出码。
- 用断言测试替换返回布尔值的假通过测试，并增加离线八阶段集成测试。
- 增加 OpenAI 兼容协议适配，默认接入 Agnes AI Chat Completions。

以下为早期版本说明；当前行为以 README 和 USAGE 为准。

## 🎉 新增功能

### 1. 自主代码执行 (ExecutorAgent)

**位置**: `harness/agents/executor.py`

**功能**:
- 自动写入生成的代码文件到 `sessions/<session_id>/code/`
- 自动安装依赖 (`requirements.txt`)
- 运行测试代码并捕获输出
- 使用 LLM 分析执行结果（成功/失败、错误、指标）
- 可选：调用 `code_review` skill 进行代码质量审查

**输出**:
```json
{
  "code_dir": "sessions/.../code",
  "install_log": "pip install 日志",
  "test_log": "测试执行日志",
  "test_success": true/false,
  "analysis": {
    "success": true,
    "summary": "执行结果总结",
    "errors": ["错误1", "错误2"],
    "warnings": ["警告1"],
    "suggestions": ["建议1"],
    "metrics": {"loss": 0.5, "accuracy": 0.9}
  },
  "code_review": [...]  // 如果启用
}
```

**配置**:
```python
ExecutorAgent(timeout=600, enable_code_review=True)
```

---

### 2. 自动文档生成 (DocumenterAgent)

**位置**: `harness/agents/documenter.py`

**功能**:
- 为每个研究项目生成完整的 `README.md`
- 包含：项目介绍、方法概述、安装指南、快速开始、代码结构、实验流程、引用、许可证
- 使用 Markdown 格式，适合 GitHub 展示

**输出**:
```json
{
  "readme": "完整的 README.md 内容",
  "requirements": "requirements.txt 内容"
}
```

**生成位置**: `sessions/<session_id>/README.md`

---

### 3. Skills 系统

**核心组件**: `harness/core/skill.py`

**架构**:
```python
# Skill 基类
class Skill(ABC):
    name: str
    description: str

    def execute(self, inputs: dict) -> dict:
        pass

    def validate_inputs(self, inputs: dict) -> bool:
        pass

# Skill 注册表（单例模式）
class SkillRegistry:
    def register(self, skill: Skill) -> None
    def execute(self, name: str, inputs: dict) -> dict
    def list_skills(self) -> list[dict]
```

**内置 Skills**:

#### 3.1 CodeReviewSkill
- **位置**: `harness/skills/code_review.py`
- **功能**: 代码质量审查，发现潜在问题
- **输入**: `{"code": "...", "language": "python"}`
- **输出**: `{"score": 85, "issues": [...], "suggestions": [...], "summary": "..."}`

#### 3.2 DependencyCheckSkill
- **位置**: `harness/skills/dependency_check.py`
- **功能**: 检查依赖包的版本、冲突和安全漏洞
- **输入**: `{"dependencies": "requirements.txt 内容"}`
- **输出**: `{"outdated": [...], "conflicts": [...], "security_issues": [...]}`

#### 3.3 TestGenerationSkill
- **位置**: `harness/skills/test_generation.py`
- **功能**: 自动生成单元测试
- **输入**: `{"code": "...", "test_framework": "pytest"}`
- **输出**: `{"test_code": "...", "coverage_estimate": "..."}`

**使用方式**:
```python
from harness.core.skill import get_global_registry

# 获取全局注册表
registry = get_global_registry()

# 调用 skill
result = registry.execute("code_review", {
    "code": "def foo(): pass",
    "language": "python"
})

# 注册自定义 skill
registry.register(MyCustomSkill())
```

---

## 📋 更新的工作流

**文件**: `workflows/research.yaml`

新增了 2 个阶段：

```yaml
stages:
  # ... 原有阶段 ...

  - id: code_execution
    name: 代码执行与验证
    agent: executor
    depends_on: [coding]
    max_retries: 1
    input_from:
      files: coding.files
      entry_point: coding.entry_point
      dependencies: coding.dependencies
      test_snippet: coding.test_snippet

  - id: documentation
    name: 文档生成
    agent: documenter
    depends_on: [code_execution, paper_writing]
    max_retries: 1
    input_from:
      research_question: planning.research_question
      method_name: method_design.method_name
      method_overview: method_design.overview
      files: coding.files
      entry_point: coding.entry_point
      dependencies: coding.dependencies
      run_instructions: coding.run_instructions
      execution_summary: code_execution.analysis.summary
```

**完整流程** (8 个阶段):
1. planning → 2. literature → 3. method_design → 4. coding →
5. **code_execution** ⭐ → 6. self_review → 7. paper_writing → 8. **documentation** ⭐

---

## 📂 新增文件

```
research-harness/
├── harness/
│   ├── core/
│   │   └── skill.py                    # ⭐ Skill 系统核心
│   ├── agents/
│   │   ├── executor.py                 # ⭐ 代码执行 Agent
│   │   └── documenter.py               # ⭐ 文档生成 Agent
│   └── skills/                         # ⭐ Skills 模块
│       ├── __init__.py
│       ├── code_review.py
│       ├── dependency_check.py
│       └── test_generation.py
├── configs/
│   └── skills.yaml                     # ⭐ Skills 配置
├── examples/                           # ⭐ 示例代码
│   └── demo_skills.py
├── README.md                           # ⭐ 更新
└── USAGE.md                            # ⭐ 使用指南
```

---

## 🔧 配置更新

### main.py

**更新的导入**:
```python
from harness.agents.executor import ExecutorAgent
from harness.agents.documenter import DocumenterAgent
from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill
from harness.core.skill import get_global_registry
```

**更新的 agent 注册**:
```python
def build_agent_registry(config: dict, memory: MemoryStore) -> dict:
    return {
        # ... 原有 agents ...
        "executor":   make(ExecutorAgent,   "executor", timeout=600),
        "documenter": make(DocumenterAgent, "documenter"),
    }

def setup_skills() -> None:
    """注册所有内置 skills 到全局注册表。"""
    registry = get_global_registry()
    registry.register(CodeReviewSkill())
    registry.register(DependencyCheckSkill())
    registry.register(TestGenerationSkill())
```

**更新的 cmd_run**:
```python
def cmd_run(args, config: dict) -> None:
    # ... 原有代码 ...

    # 设置 skills
    setup_skills()
    skill_registry = get_global_registry()

    # 创建 WorkflowEngine（传入 skill_registry）
    engine = WorkflowEngine(workflow_path, checkpoint, agents, skill_registry)

    # ... 原有代码 ...

    # 输出 README.md
    doc_output = checkpoint.get_stage_output(final_state, "documentation")
    if doc_output and not doc_output.get("parse_error"):
        readme_path = Path(paths["sessions_dir"]) / checkpoint.session_id / "README.md"
        readme_path.write_text(doc_output.get("readme", ""), encoding="utf-8")
        print(f"项目文档已保存至: {readme_path}")
```

### workflow.py

**更新的 __init__**:
```python
def __init__(
    self,
    workflow_path: str | Path,
    checkpoint: CheckpointManager,
    agent_registry: dict[str, Any],
    skill_registry: Optional[Any] = None,  # ⭐ 新增
):
    self.skill_registry = skill_registry
    # ...
```

**新增方法**:
```python
def call_skill(self, skill_name: str, inputs: dict) -> dict:
    """调用一个 skill。"""
    return self.skill_registry.execute(skill_name, inputs)

def list_skills(self) -> list[dict[str, str]]:
    """列出所有可用的 skills。"""
    return self.skill_registry.list_skills()
```

---

## 🎯 使用示例

### 运行完整流程

```bash
python main.py run --direction "基于 Mamba 的高效视频理解方法"
```

**输出**:
```
sessions/session_20260519_173003/
├── README.md              # ⭐ 项目文档
├── code/                  # 代码文件
│   ├── models/
│   ├── train.py
│   └── requirements.txt
├── output/
│   └── paper.tex         # 论文草稿
└── state.json            # 工作流状态
```

### 演示 Skills

```bash
python examples/demo_skills.py
```

**输出示例**:
```
代码质量评分: 45/100

发现 6 个问题:
  [high] 行 6: ZeroDivisionError：当 numbers 为空列表时会抛出除零异常
  [medium] 行 10: 性能问题：使用 range(len()) 反模式

改进建议:
  1. 使用 sum() 内置函数替代手动循环累加
  2. 添加类型注解提升可维护性
```

---

## 🚀 扩展性

### 创建自定义 Skill

```python
from harness.core.skill import Skill

class VisualizationSkill(Skill):
    name = "visualization"
    description = "生成实验结果可视化图表"

    def validate_inputs(self, inputs: dict) -> bool:
        return "data" in inputs

    def execute(self, inputs: dict) -> dict:
        data = inputs["data"]
        # 生成图表逻辑
        return {
            "success": True,
            "plot_path": "plot.png",
            "summary": "生成了 3 个图表"
        }

# 注册
from harness.core.skill import get_global_registry
get_global_registry().register(VisualizationSkill())
```

### 在 Agent 中使用 Skill

```python
class MyAgent(BaseAgent):
    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        # 获取全局 skill 注册表
        from harness.core.skill import get_global_registry
        registry = get_global_registry()

        # 调用 skill
        result = registry.execute("code_review", {
            "code": inputs["code"],
            "language": "python"
        })

        if result.get("success"):
            # 使用 skill 的输出
            score = result["score"]
            # ...

        return output
```

---

## ✅ 测试结果

所有新功能已通过测试：

1. ✅ ExecutorAgent 和 DocumenterAgent 导入成功
2. ✅ Skills 注册和调用正常
3. ✅ main.py 更新后运行正常
4. ✅ demo_skills.py 演示成功

---

## 📚 文档

- **README.md**: 项目概览和快速开始
- **USAGE.md**: 详细使用指南
- **examples/demo_skills.py**: Skills 系统演示

---

## 🎊 总结

research-harness 现在具备：

1. **完整的自动化流程**: 从选题到论文撰写，再到代码执行和文档生成
2. **可扩展的 Skills 系统**: 轻松添加新功能，无需修改核心代码
3. **专业的输出**: 每个项目都有完整的 README.md 和可运行的代码
4. **智能的代码验证**: 自动执行测试并分析结果

框架已经可以用于实际的科研工作流！
