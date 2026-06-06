# research-harness

面向长流程科研的智能体框架，支持从选题到论文撰写的全自动化流程。

## ✨ 新功能

### 🔧 自主代码执行
- 自动安装依赖并运行测试代码
- 捕获执行日志和性能指标
- 智能分析执行结果

### 📝 自动文档生成
- 为每个项目生成完整的 README.md
- 包含安装指南、使用说明、实验流程
- 专业的 GitHub 展示格式

### 🎯 Skills 系统
- 可插拔的功能模块，轻松扩展框架能力
- 内置 3 个 skills：
  - **code_review**: 代码质量审查
  - **dependency_check**: 依赖检查
  - **test_generation**: 自动生成单元测试
- 支持自定义 skills

## 🚀 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env` 并填入你的 API 凭据：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
ANTHROPIC_AUTH_TOKEN=your_api_key_here
ANTHROPIC_BASE_URL=https://api.anthropic.com  # 可选
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
```

### 运行

启动一个新的研究项目：

```bash
python main.py run --direction "基于 Transformer 的时间序列预测方法"
```

## 📋 工作流阶段

完整的研究流程包含 8 个阶段：

1. **planning** - 选题与研究规划
2. **literature** - 文献调研
3. **method_design** - 方法设计
4. **coding** - 代码实现
5. **code_execution** - 代码执行与验证 ⭐ 新增
6. **self_review** - 自我审稿
7. **paper_writing** - 论文撰写
8. **documentation** - 文档生成 ⭐ 新增

## 📂 输出结构

```
sessions/<session_id>/
├── README.md              # 项目文档
├── session.log            # Session 专属日志 ⭐ 新增
├── conversations/         # LLM 对话记录 ⭐ 新增
│   ├── planning.json
│   ├── literature.json
│   ├── method_design.json
│   ├── coding.json
│   ├── code_execution.json
│   ├── self_review.json
│   ├── paper_writing.json
│   └── documentation.json
├── code/                  # 代码文件
│   ├── models/
│   ├── train.py
│   ├── infer.py
│   └── requirements.txt
├── output/
│   └── paper.tex         # 论文草稿
└── checkpoint.json       # 工作流状态
```

### 对话记录格式

每个 `conversations/<stage_id>.json` 是一个 JSON 数组，每轮对话包含：

```json
{
  "timestamp": "2026-05-24T20:47:04.085",
  "model": "claude-sonnet-4-6",
  "elapsed_seconds": 37.42,
  "messages": [{"role": "user", "content": "..."}],
  "response": "...",
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 456,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0
  }
}
```

## 🎯 使用 Skills

### 查看可用 Skills

```python
from harness.core.skill import get_global_registry

registry = get_global_registry()
for skill in registry.list_skills():
    print(f"{skill['name']}: {skill['description']}")
```

### 调用 Skill

```python
# 代码审查
result = registry.execute("code_review", {
    "code": "def foo(): pass",
    "language": "python"
})

# 生成测试
result = registry.execute("test_generation", {
    "code": source_code,
    "test_framework": "pytest"
})
```

### 创建自定义 Skill

```python
from harness.core.skill import Skill

class MySkill(Skill):
    name = "my_skill"
    description = "我的自定义技能"

    def execute(self, inputs: dict) -> dict:
        # 实现你的逻辑
        return {"success": True, "result": "..."}

# 注册
registry.register(MySkill())
```

详细使用说明请参考 [USAGE.md](USAGE.md)。

## 🛠️ 命令行工具

```bash
# 启动新研究
python main.py run --direction "你的研究方向"

# 从断点继续
python main.py resume --session <session_id>

# 查看进度
python main.py status --session <session_id>

# 重置失败阶段
python main.py reset-stage --session <session_id> <stage_name>

# 列出所有 session
python main.py list

# 修复解析错误
python main.py repair --session <session_id>
```

## 📖 示例

查看 `examples/` 目录获取更多示例：

```bash
# 演示 Skills 系统
python examples/demo_skills.py
```

## 🏗️ 架构

```
research-harness/
├── harness/
│   ├── core/              # 核心组件
│   │   ├── agent.py       # Agent 基类
│   │   ├── workflow.py    # 工作流引擎
│   │   ├── checkpoint.py  # 状态持久化
│   │   ├── memory.py      # 跨 session 记忆
│   │   └── skill.py       # Skill 系统 ⭐ 新增
│   ├── agents/            # 专职 Agents
│   │   ├── planner.py
│   │   ├── literature.py
│   │   ├── method.py
│   │   ├── coder.py
│   │   ├── executor.py    # ⭐ 新增
│   │   ├── reviewer.py
│   │   ├── writer.py
│   │   └── documenter.py  # ⭐ 新增
│   ├── skills/            # Skills 模块 ⭐ 新增
│   │   ├── code_review.py
│   │   ├── dependency_check.py
│   │   └── test_generation.py
│   └── tools/             # 工具函数
├── workflows/             # 工作流定义
│   └── research.yaml
├── configs/               # 配置文件
│   ├── default.yaml
│   └── skills.yaml        # ⭐ 新增
├── examples/              # 示例代码 ⭐ 新增
└── main.py                # CLI 入口
```

## 🔧 配置

### Agent 配置 (configs/default.yaml)

```yaml
agents:
  planner:
    use_extended_thinking: true
    thinking_budget: 10000
  coder:
    use_extended_thinking: false
```

### Skills 配置 (configs/skills.yaml)

```yaml
skills:
  code_review:
    enabled: true
    auto_trigger: false
```

## 📝 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📧 联系

如有问题，请提交 Issue。
