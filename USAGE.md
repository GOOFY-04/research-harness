# research-harness 使用示例

本文档展示如何使用 research-harness 的新功能。

## 1. 基本使用

启动一个新的研究项目：

```bash
python main.py run --direction "基于 Transformer 的时间序列预测方法"
```

这将自动执行以下阶段：
1. **planning** - 选题与研究规划
2. **literature** - 文献调研（真实 arXiv API + 智能重试）
3. **method_design** - 方法设计
4. **coding** - 代码实现
5. **code_execution** - 代码执行与验证
6. **self_review** - 自我审稿
7. **revision** - 迭代修订（可回到 coding 重新执行）
8. **paper_writing** - 论文撰写
9. **documentation** - 文档生成

## 2. 迭代修订

### 2.1 工作机制

`revision` 阶段由 RevisionAgent 驱动，基于审稿意见决定是否需要修订：

- 总分 >= 8 且无 major 弱点 → 跳过修订
- 有代码错误或 reproducibility 低 → 代码修订
- 缺少关键实验或基线 → 实验修订
- clarity 评分低 → 写作修订

### 2.2 迭代流程

```
planning → literature → method_design → coding → code_execution
    → self_review → revision
        ├── needs_revision=true → 清除 coding/code_execution → 重新执行
        └── needs_revision=false → paper_writing → documentation
```

最多 5 轮迭代，防止无限循环。

## 3. 代码自动执行

`code_execution` 阶段会：
- 自动安装依赖（requirements.txt）
- 运行测试代码验证正确性
- 捕获执行日志和错误
- 提取性能指标
- 失败自动修复（LLM 驱动的修复循环）
- 可选：调用 code_review skill 进行代码审查

## 4. 社区 Skill 自动获取

### 4.1 功能说明

当某个阶段失败后，系统可以自动从开源社区搜索解决方案：

```yaml
# configs/skills.yaml
auto_skill_hunt:
  enabled: true         # 启用自动 skill 搜索
  max_search_attempts: 3
```

### 4.2 工作流程

```
阶段失败 (max_retries 耗尽)
  └→ SkillHunterAgent 分析失败原因
       └→ 搜索 GitHub / PyPI / HuggingFace
            └→ SkillIntegrator 下载 + 安全扫描 + 沙盒验证
                 └→ 注册到 SkillRegistry
                      └→ 重置失败阶段 → 重新执行
```

### 4.3 安全机制

- 静态 AST 扫描：检测 os/subprocess/socket/ctypes/eval/exec 等危险调用
- 许可证验证：MIT/Apache/BSD 可信，GPL 警告，无许可证拦截
- 沙盒执行：隔离子进程 + 受限 builtins + 60s 超时

### 4.4 管理社区 Skills

```python
from harness.tools.skill_integrator import SkillIntegrator

integrator = SkillIntegrator()
# 列出已安装的社区 skills
print(integrator.list_installed())
# 移除
integrator.remove("some_skill")
```

## 5. 自动生成 README

`documentation` 阶段会生成完整的 README.md，包含：
- 项目介绍
- 方法概述
- 安装指南
- 快速开始
- 代码结构
- 实验流程
- 引用和许可证

生成的 README.md 位于：`sessions/<session_id>/README.md`

## 6. 使用 Skills

### 6.1 内置 Skills

框架提供了 8 个内置 skills：

**代码质量**:
1. **code_review** - 代码质量审查，输出评分/问题/改进建议
2. **dependency_check** - 依赖包版本检查和安全漏洞扫描
3. **test_generation** - 自动生成单元测试

**文献与论文**:
4. **paper_summary** - 论文结构化摘要，提取贡献/方法/关键词
5. **citation_format** - BibTeX 验证与格式化，会议名标准化

**实验与分析**:
6. **experiment_tracker** - 实验指标追踪对比，自动生成对比表格
7. **plot_generation** - matplotlib 图表生成（折线/柱状/散点/多曲线对比）

**排版**:
8. **latex_compile** - LaTeX 编译为 PDF，错误检测

### 6.2 在 Agent 中调用 Skill

```python
from harness.core.skill import get_global_registry

# 在 Agent 的 run() 方法中
registry = get_global_registry()
result = registry.execute("code_review", {
    "code": "def foo(): pass",
    "language": "python"
})
```

### 6.3 创建自定义 Skill

```python
from harness.core.skill import Skill

class MyCustomSkill(Skill):
    name = "my_skill"
    description = "我的自定义技能"

    def validate_inputs(self, inputs: dict) -> bool:
        return "required_param" in inputs

    def execute(self, inputs: dict) -> dict:
        # 实现你的逻辑
        return {"success": True, "result": "..."}

# 注册到全局注册表
from harness.core.skill import get_global_registry
registry = get_global_registry()
registry.register(MyCustomSkill())
```

## 7. 配置 Skills

编辑 `configs/skills.yaml` 来启用/禁用 skills：

```yaml
auto_skill_hunt:
  enabled: true         # 失败时自动从社区搜索 solutions
  max_search_attempts: 3

skills:
  code_review:
    enabled: true
    auto_trigger: false
  experiment_tracker:
    enabled: true
    auto_trigger: true  # 在 code_execution 后自动触发
```

## 8. 查看输出

完整的研究项目输出结构：

```
sessions/<session_id>/
├── README.md              # 项目文档
├── session.log            # Session 专属日志
├── conversations/         # LLM 对话记录（每个 stage 的完整 prompt/response/token）
├── code/                  # 代码文件
│   ├── models/
│   ├── train.py
│   └── ...
├── output/
│   └── paper.tex         # 论文草稿
├── checkpoint.json       # 工作流状态
└── skills/               # 社区 skill 缓存
    └── community/        # 自动获取的社区 skills
```

## 9. 高级用法

### 9.1 启用代码审查

修改 `main.py` 中的 `build_agent_registry()`：

```python
"executor": make(ExecutorAgent, "executor", timeout=600, enable_code_review=True),
```

### 9.2 列出所有可用 Skills

```python
from harness.core.skill import get_global_registry
registry = get_global_registry()
skills = registry.list_skills()
for skill in skills:
    print(f"{skill['name']}: {skill['description']}")
```

### 9.3 在工作流中调用 Skill

在 Agent 的 `run()` 方法中：

```python
# 获取全局 skill 注册表
from harness.core.skill import get_global_registry
registry = get_global_registry()

# 调用 skill
result = registry.execute("test_generation", {
    "code": source_code,
    "test_framework": "pytest"
})

if result.get("success"):
    test_code = result["test_code"]
    # 使用生成的测试代码
```

### 9.4 启用社区 Skill 自动获取

设置 `configs/skills.yaml`:

```yaml
auto_skill_hunt:
  enabled: true
  max_search_attempts: 3
```

或通过代码：

```python
from harness.tools.skill_integrator import SkillIntegrator

integrator = SkillIntegrator()
# 查看已安装的社区 skills
for name, info in integrator.list_installed().items():
    print(f"{name}: {info['source']} - {info['license']}")
```

## 10. 故障排查

### 代码执行失败

查看执行日志：
```bash
cat sessions/<session_id>/code_execution/output.json
```

### Skill 调用失败

检查 skill 是否已注册：
```python
from harness.core.skill import get_global_registry
print(get_global_registry().list_skills())
```

### 社区 Skill 获取失败

查看已安装的社区 skills：
```python
from harness.tools.skill_integrator import SkillIntegrator
print(SkillIntegrator().list_installed())
```

检查 auto_skill_hunt 是否启用：
```yaml
# configs/skills.yaml
auto_skill_hunt:
  enabled: true
```

### 依赖安装失败

手动安装依赖：
```bash
cd sessions/<session_id>/code
pip install -r requirements.txt
```
