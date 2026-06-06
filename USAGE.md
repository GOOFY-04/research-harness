# research-harness 使用示例

本文档展示如何使用 research-harness 的新功能。

## 1. 基本使用

启动一个新的研究项目：

```bash
python main.py run --direction "基于 Transformer 的时间序列预测方法"
```

这将自动执行以下阶段：
1. **planning** - 选题与研究规划
2. **literature** - 文献调研
3. **method_design** - 方法设计
4. **coding** - 代码实现
5. **code_execution** - 代码执行与验证（新增）
6. **self_review** - 自我审稿
7. **paper_writing** - 论文撰写
8. **documentation** - 文档生成（新增）

## 2. 代码自动执行

`code_execution` 阶段会：
- 自动安装依赖（requirements.txt）
- 运行测试代码验证正确性
- 捕获执行日志和错误
- 提取性能指标
- 可选：调用 code_review skill 进行代码审查

## 3. 自动生成 README

`documentation` 阶段会生成完整的 README.md，包含：
- 项目介绍
- 方法概述
- 安装指南
- 快速开始
- 代码结构
- 实验流程
- 引用和许可证

生成的 README.md 位于：`sessions/<session_id>/README.md`

## 4. 使用 Skills

### 4.1 内置 Skills

框架提供了 3 个内置 skills：

1. **code_review** - 代码审查
   - 检查代码质量、潜在 bug、性能问题
   - 输出：评分、问题列表、改进建议

2. **dependency_check** - 依赖检查
   - 检查依赖包的版本和冲突
   - 输出：过时包、冲突、安全漏洞

3. **test_generation** - 测试生成
   - 自动生成单元测试
   - 输出：测试代码

### 4.2 在 Agent 中调用 Skill

```python
from harness.core.skill import get_global_registry

# 在 Agent 的 run() 方法中
registry = get_global_registry()
result = registry.execute("code_review", {
    "code": "def foo(): pass",
    "language": "python"
})
```

### 4.3 创建自定义 Skill

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

## 5. 配置 Skills

编辑 `configs/skills.yaml` 来启用/禁用 skills：

```yaml
skills:
  code_review:
    enabled: true
    auto_trigger: false  # 是否在 coding 阶段后自动触发
```

## 6. 查看输出

完整的研究项目输出结构：

```
sessions/<session_id>/
├── README.md              # 项目文档（新增）
├── code/                  # 代码文件
│   ├── models/
│   ├── train.py
│   └── ...
├── output/
│   └── paper.tex         # 论文草稿
└── state.json            # 工作流状态
```

## 7. 高级用法

### 7.1 启用代码审查

修改 `main.py` 中的 `build_agent_registry()`：

```python
"executor": make(ExecutorAgent, "executor", timeout=600, enable_code_review=True),
```

### 7.2 列出所有可用 Skills

```python
from harness.core.skill import get_global_registry
registry = get_global_registry()
skills = registry.list_skills()
for skill in skills:
    print(f"{skill['name']}: {skill['description']}")
```

### 7.3 在工作流中调用 Skill

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

## 8. 故障排查

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

### 依赖安装失败

手动安装依赖：
```bash
cd sessions/<session_id>/code
pip install -r requirements.txt
```
