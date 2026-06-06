# research-harness 功能增强总结

## 🎉 新增功能

### 1. Per-Session 日志与对话记录 ⭐

**Per-Session 独立日志**:
- 每个 session 自动在 `sessions/<session_id>/session.log` 记录完整日志
- DEBUG 级别日志写入 session 目录，便于事后排查
- 全局日志（harness.log）保持不变，两者同时记录

**对话记录 (Conversation Log)**:
- 每个 stage 的 LLM 对话自动保存为 `sessions/<session_id>/conversations/<stage_id>.json`
- 记录内容包括：时间戳、模型、prompt、response、耗时、token 用量
- 支持多轮对话（多轮调用的 stage 会累加记录）
- 失败重试的对话也会被保留，便于调试 prompt

**session_dir 注入**:
- `WorkflowEngine` 运行时自动将 `session_dir` 注入 state，供所有 agent 使用
- ExecutorAgent 等需要写磁盘的 agent 可正确找到输出路径

**输出结构更新**:
```
sessions/<session_id>/
├── session.log              # ⭐ Session 专属日志
├── conversations/           # ⭐ 对话记录
│   ├── planning.json
│   ├── literature.json
│   ├── method_design.json
│   ├── coding.json
│   ├── code_execution.json
│   ├── self_review.json
│   ├── paper_writing.json
│   └── documentation.json
├── code/                    # 代码文件
├── output/                  # 论文等输出
├── README.md                # 项目文档
└── checkpoint.json          # 工作流状态
```

**实现细节**:
- `BaseAgent.__init__` 初始化 `_conversations` 列表
- `BaseAgent._call_llm` / `_call_llm_with_history` 每次调用后自动记录
- `BaseAgent.clear_conversations` / `save_conversations` 由 WorkflowEngine 调度
- `WorkflowEngine._run_stage` 在每个 stage 前后清空/保存对话
- `main.py:cmd_run` 为每个 session 添加专属 FileHandler

---

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
│   │   ├── documenter.py               # ⭐ 文档生成 Agent
│   │   ├── revision.py                 # ⭐ 迭代修订 Agent (2026-06-06)
│   │   └── skill_hunter.py             # ⭐ 社区 skill 发现 Agent (2026-06-06)
│   ├── skills/                         # ⭐ Skills 模块
│   │   ├── __init__.py
│   │   ├── code_review.py
│   │   ├── dependency_check.py
│   │   ├── test_generation.py
│   │   ├── paper_summary.py
│   │   ├── citation_format.py
│   │   ├── experiment_tracker.py
│   │   ├── plot_generation.py
│   │   └── latex_compile.py
│   └── tools/
│       ├── arxiv.py
│       ├── code_runner.py
│       └── skill_integrator.py          # ⭐ 社区 skill 集成 (2026-06-06)
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

## 🎉 新增功能（2026-06-06）

### 1. 迭代修订系统

**RevisionAgent** (`harness/agents/revision.py`):
- 基于审稿意见智能决策是否需要修订
- 支持四种修订类型：code / experiment / baseline / writing
- 自动制定修订计划并清除已完成阶段
- WorkflowEngine 支持迭代执行，最多 5 轮

**WorkflowEngine 迭代机制**:
- `_run_stage()` 返回 `(state, rerun_triggered)` 元组
- RevisionAgent 输出 `rerun_stages` 后自动清除相关阶段状态
- 立即中断当前迭代，开始新一轮执行
- 修订后重新通过代码执行和自我审稿

**工作流更新** (`workflows/research.yaml`):
- 新增 `revision` 阶段（在 self_review 之后）
- 完整流程：planning → literature → method_design → coding → code_execution → self_review → **revision** → paper_writing → documentation

### 2. 社区 Skill 自动获取系统

**SkillHunterAgent** (`harness/agents/skill_hunter.py`):
- 分析失败原因，识别缺失的能力
- 从 GitHub / PyPI / HuggingFace 搜索相关工具
- 评估候选 skill 的质量、安全性、兼容性
- 输出结构化推荐（含搜索关键词、集成策略、风险评估）

**SkillIntegrator** (`harness/tools/skill_integrator.py`):
- **下载模块**：
  - GitHub: git clone 或 zip 下载，支持单文件和完整仓库
  - PyPI: pip install 到隔离目录
  - HuggingFace: huggingface_hub 下载（自动跳过大权重文件），备选 HTTP 下载
  - 通用 URL 直链下载
- **安全模块**：
  - 静态 AST 扫描：检测 os/subprocess/socket/ctypes/eval/exec/compile 等危险调用
  - 许可证验证：MIT/Apache/BSD 可信，GPL/AGPL 警告，无许可证标记
  - 文件大小限制：单文件最大 10MB
- **沙盒执行**：
  - 隔离子进程执行，受限 builtins（阻止 socket/ctypes/code/multiprocessing）
  - 60s 超时保护
  - 通过 stdout 标记协议传递结果
- **生命周期管理**：
  - `install/remove/list` 社区 skills
  - manifest.json 持久化管理
  - CommunitySkill 动态 wrapper

**WorkflowEngine 自动触发** (`harness/core/workflow.py`):
- 新增 `auto_skill_hunt` 参数控制自动搜索开关
- `_try_auto_resolve()` 方法：失败后自动调用 SkillHunter → SkillIntegrator
- 集成成功后自动刷新 SkillRegistry 并重置失败阶段重试
- 异常安全：ImportError 等异常静默降级，不影响正常流程

**配置更新** (`configs/skills.yaml`):
```yaml
auto_skill_hunt:
  enabled: true         # 失败时自动从社区搜索
  max_search_attempts: 3
```

### 3. 真实数据与文献增强

**arXiv API 集成** (`harness/tools/arxiv.py`):
- 真实 arXiv API 调用（非模拟）
- 指数退避重试（1s → 2s → 4s），处理 429 限流
- 按项目/方法/任务三维度搜索

**数据集路径注入**:
- ExecutorAgent 通过环境变量 `DATASET_PATH` 传递数据集路径
- 消除硬编码路径和示例数据依赖

### 4. 代码质量保障

**自动修复循环**:
- LLM 驱动的测试失败检测和修复
- 最多 N 轮自动修复迭代
- 逐文件写入和测试运行

---

## 🐛 Bug 修复（2026-05-24）

### Critical
- **WriterAgent LaTeX 模板崩溃**: `_LATEX_TEMPLATE.format()` 在 LLM 生成的 LaTeX 内容包含花括号时会抛出 KeyError。已修复为在插值前转义花括号
- **CheckpointManager.load() JSON 损坏处理**: 当 checkpoint.json 损坏时不再崩溃，改为自动备份并返回空状态，支持手动恢复

### High
- **ExecutorAgent 误报失败**: 当不存在 test_snippet 时 `test_success` 初始化为 `False` 导致始终报告失败。改为 `None`（无测试），并据此调整成功判断逻辑
- **ExecutorAgent 依赖安装失败后继续运行**: pip install 失败时不再盲目运行测试。先检查 `install_ok`，失败时跳过测试并记录原因
- **fetch_paper 网络异常处理**: 增加了 URL 请求和 XML 解析的 try/except，网络故障时返回空 dict 而非崩溃
- **Skills 配置关联**: `configs/skills.yaml` 中的 `enabled` 标志现在真正生效。`setup_skills()` 读取 YAML 并仅注册 enabled=true 的 skill

### Medium
- **PlannerAgent 死代码移除**: `parse_output()` 中不可达的 return 语句已删除
- **Extended thinking 类属性修复**: `use_extended_thinking` 类属性不再被构造函数静默覆盖。参数未传入时回退到类属性值
- **_call_llm_with_history extended thinking**: 多轮对话现在正确支持 extended thinking，与 `_call_llm` 行为一致
- **Agent 导出完整性**: `ExecutorAgent` 和 `DocumenterAgent` 已加入 `harness/agents/__init__.py` 导出列表

### Low
- **内联 import 规整**: agent.py、coder.py、writer.py、documenter.py 中的 `import re` / `import json` 已移至模块顶层
- **CheckpointManager 类型检查**: `load()` 返回非 dict JSON 值（null/string/array）时回退到空状态

---

## ✅ 验证

所有修改通过编译和基本功能验证，PlannerAgent 类属性默认值和构造函数覆盖均正常。

---

## 🧩 新增 Skills（2026-05-24）

### 文献与论文
- **paper_summary**: 使用 LLM 对论文进行结构化摘要，自动提取贡献、方法类型、关键词、新颖度评估。支持 brief/standard/detailed 三种详细级别。可结合科研方向上下文评估关联度
- **citation_format**: 验证 BibTeX 条目的必需字段和推荐字段，检测格式异常（year/author），标准化会议/期刊名称缩写（NeurIPS/CVPR/ICML 等），按指定格式重新输出

### 实验与分析
- **experiment_tracker**: 从实验日志或结构化数据中提取指标，自动生成 Markdown/JSON/Plain 格式对比表，找出各指标最优实验，生成文字总结。内置常见指标（loss/psnr/ssim/compression_rate 等）的正则解析模式
- **plot_generation**: 生成 matplotlib 图表（折线图、柱状图、散点图、多曲线对比），支持 seaborn/IEEE 样式，自动推断图表类型

### 排版
- **latex_compile**: 编译 LaTeX 为 PDF（pdflatex/xelatex/lualatex），自动运行 bibtex 解析参考文献，检测编译错误和警告。无编译器时优雅降级

### Skills 配置更新
`configs/skills.yaml` 现包含全部 8 个 skill 的配置（3 个原有 + 5 个新增），新增按类别分组注释，`experiment_tracker` 默认 auto_trigger=true。

---

## 🎊 总结

research-harness 现在具备：

1. **完整的自动化流程**: 从选题到论文撰写，再到代码执行和文档生成
2. **可扩展的 Skills 系统**: 轻松添加新功能，无需修改核心代码
3. **专业的输出**: 每个项目都有完整的 README.md 和可运行的代码
4. **智能的代码验证**: 自动执行测试并分析结果
5. **完善的日志系统**: 全局日志 + Per-Session 日志，问题追踪更便捷
6. **对话可追溯**: 每个 stage 的 LLM 对话独立保存，包含 token 用量和耗时

框架已经可以用于实际的科研工作流！
