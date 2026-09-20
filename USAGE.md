# 使用与扩展

安装、环境配置和运行方式见 [README](README.md)。

## 自定义工作流

```yaml
name: custom_research
stages:
  - id: planning
    agent: planner
    max_retries: 2
    inputs:
      research_direction: "时间序列建模"
  - id: literature
    agent: literature
    input_from:
      research_question: planning.research_question
      keywords: planning.keywords
```

```bash
python main.py run --direction "时间序列建模" --workflow custom.yaml --session custom
python main.py resume --session custom
```

即使 YAML 顺序不同，引擎也会按依赖排序；`input_from` 的来源自动成为依赖。CLI 的研究方向覆盖 planning 中的静态输入。修改已完成阶段的定义后恢复，会使该阶段和下游失效。

`condition` 为真表示**跳过**该阶段，且依赖它的阶段也跳过。条件只允许对 state 使用下标、比较及布尔运算，不允许调用方法或任意 Python 代码：

```yaml
condition: "state['metadata']['skip_optional'] == True"
```

必须预先提供所引用的 metadata；条件错误会使阶段失败。自定义工作流所有阶段都被跳过时，执行过程仍可正常结束，请通过各阶段状态判断产物是否存在。

`timeout` 单位为秒：内置模型请求共享该阶段的剩余时间，Executor 同时限制环境准备、安装和执行的总时间。任意自定义 Python Agent 内部阻塞不能由引擎强制抢占，自定义 Agent 应自行实现可取消的 I/O。没有阶段 timeout 时仍有模型请求超时和 Executor 超时。

## 自定义 Agent

```python
from harness.core.agent import BaseAgent
from harness.core.workflow import WorkflowEngine
from harness.core.checkpoint import CheckpointManager

class SummaryAgent(BaseAgent):
    required_fields = {"summary": str}

    def build_prompt(self, stage_id, inputs, state):
        return '输出 JSON 对象，包含非空 summary 字段。输入：' + str(inputs)

    def parse_output(self, raw_text, stage_id, inputs):
        return self._parse_json(raw_text)

agent = SummaryAgent(model="claude-sonnet-4-6", max_tokens=4096)
checkpoint = CheckpointManager("sessions", "summary")
# YAML 中 agent: summary 对应这个注册名称
engine = WorkflowEngine("summary.yaml", checkpoint, {"summary": agent})
state = engine.run()
```

输出必须为 dict。用 `required_fields` 声明必需字段类型，空字符串默认不允许；可通过 `allow_empty_fields` 声明允许为空的文本字段。任何 `success=False`、非空 `error` 或 `parse_error=True` 都会失败。解析失败的原始响应会保存在失败阶段的 output 中供排查和 repair 使用。

执行类阶段可以声明 `repair_from`，指向其已完成的上游生成阶段。失败输出为结构化 dict 时，引擎调用上游 Agent 的 `repair(stage_id, previous_output, failure_output, state)`，校验修复结果并更新上游检查点，然后再执行当前阶段。默认研究工作流使用 `repair_from: coding`。

`BaseAgent` 延迟创建 SDK client，因此可以离线实例化，也可以注入 `client=` 测试替身。API 层关闭 SDK 隐式重试，由工作流统一管理重试次数。

## Skills

```python
from harness.core.skill import Skill, SkillRegistry

class CountLines(Skill):
    name = "count_lines"
    description = "统计代码行数"

    def validate_inputs(self, inputs):
        return isinstance(inputs.get("code"), str)

    def execute(self, inputs):
        return {"success": True, "lines": len(inputs["code"].splitlines())}

registry = SkillRegistry()
registry.register(CountLines())
print(registry.execute("count_lines", {"code": "print(1)"}))
```

独立 registry 避免不同工作流互相污染。全局 registry 保留用于旧代码，但不会自动注册内置技能。CLI 使用 `setup_skills(load_config())` 加载配置并创建自己的 registry；启用 Executor 的代码审查时共享该 registry。

自动触发只支持已注册的技能。内置 dependency_check 的 `success` 表示检查过程完成，`satisfied` 表示请求的包和版本满足要求。`security_issues=None` 表示未检查漏洞，不能解释为没有漏洞。

## 执行指标

程序可以在 stdout 写入：

```python
import json
print("HARNESS_METRICS=" + json.dumps({"loss": 0.2, "accuracy": 0.8}))
```

这些数字会进入 `code_execution.analysis.metrics`，连同命令、退出码和日志传入审稿与论文阶段。快速测试中的合成指标只能用于快速测试，不能充当真实数据集指标。应在正式实验中自行定义数据来源、划分、随机种子和基线。

依赖文本接受常规 PEP 508 包名、版本与环境条件，不接受 pip 参数、递归 requirements 文件或直接下载 URL。`install_dependencies=False` 时使用当前解释器，由使用者保证依赖已安装。

## 状态与历史

checkpoint 通过临时文件和原子替换写入。CLI 修改 session 前保存历史 checkpoint，旧代码/论文/README 失效时移动到 history，避免静默销毁之前的研究产物。不要直接修改 `completed_stages`；使用 reset-stage 维护依赖一致性。

`resume` 不会清除累计 attempts，但会为未完成阶段提供一轮新的重试机会。模型输出、代码或数据错误需修复相应输入后重试。工作流失败时，成功完成的前置阶段仍会保存和导出。

记忆按 topic 持久化，并用原子写入和 topic 文件锁避免并发写丢失。默认规划只回顾最近三条规划记录。记忆是历史上下文，不是可信实验数据库。
