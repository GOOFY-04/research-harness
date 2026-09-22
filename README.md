# research-harness

## 一键部署

在仓库根目录执行一条命令，即可创建或复用 Python 虚拟环境、安装依赖、创建 `.env`、克隆或快进更新私有 OpenCode fork、安装 Bun 依赖、运行集成检查并启动研究 CLI：

```powershell
python deploy.py --start
```

需要预先安装 Python 3.10+、Git 和 Bun，并确保 Git 有权读取私有仓库 `GOOFY-04/opencode`。脚本不会覆盖已有 `.env`，也不会更新存在未提交修改的 `opencode-fork`。首次部署后，在 `.env` 中填写 `AGNES_API_KEY` 才能发起模型请求。

只部署和验证而不启动 CLI，执行 `python deploy.py`。网络受限且本地依赖已准备好时，可使用 `--no-update`；仅在明确需要快速重装时使用 `--skip-checks`。重复执行部署命令是安全的，OpenCode 更新采用 `--ff-only`，不会自动合并分叉历史。

部署检查会运行 Python 测试、研究工作台类型检查、Bash/PowerShell WASM 解析测试，以及实际 OpenCode 启动入口的 `--help`。Shell 解析使用依赖包内的 WASM 文件，fork 不再触发未使用的原生语法绑定编译，避免 Windows 深目录安装时的路径长度错误。

面向长流程科研的可恢复流水线：研究规划、文献检索、方法设计、代码生成与验证、审稿、论文草稿和项目文档。

默认运行的是**代码快速验证**，不是完整训练或科研结论验证。论文以实际执行日志为依据，未完成的实验应明确标为 TODO；最终研究结论仍需真实数据、基线比较和人工审查。

## 安装

需要 Python 3.10+。建议独立虚拟环境，以下为 Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Linux/macOS 使用 `.venv/bin/python`，并用 `cp .env.example .env` 复制配置。仅运行项目可安装 `requirements.txt`。

默认配置已经接入 Agnes AI 的 OpenAI 兼容接口。在 `.env` 设置：

```dotenv
AGNES_API_KEY=your_agnes_api_key
AGNES_API_BASE=https://apihub.agnes-ai.com/v1
AGNES_MODEL=agnes-3.0-flash
```

API 使用 Bearer Token 调用 `/v1/chat/completions`。已有系统环境变量优先于 `.env`。查看状态、重置、离线解析修复不需要 API 凭据。只有实际请求模型时才建立网络连接。

## 使用

下文 `python` 指已安装项目依赖的解释器；未激活环境时可替换为 `.\.venv\Scripts\python.exe`。

```bash
python main.py run --direction "基于 Transformer 的时间序列预测方法"
python main.py run --direction "研究方向" --session my_research
python main.py resume --session my_research
python main.py revise --session my_research
python main.py status --session my_research
python main.py accept --session my_research --write-report
python main.py list
python main.py reset-stage --session my_research coding
python main.py repair --session my_research
python main.py run --direction "新的研究方向" --session my_research --no-resume
```

- `resume` 使用 session 保存的工作流路径，也接受 `--workflow custom.yaml`。
- `reset-stage coding` 同时使所有下游结果失效，避免新代码与旧论文混用。
- `revise` 仅用于 `reject`/`weak_reject` 会话：归档当前产物，把重大问题、修改计划、缺失实验和上一轮执行证据注入方法设计，然后自动重跑方法、代码、实验、审稿、论文和文档。
- `--no-resume` 真正从空状态开始，旧 checkpoint 和产物归档到 `history/`。
- 相同 session 改研究方向时，必须使用新 session 或 `--no-resume`。
- 流程失败或被阻塞时退出码为 1；中断为 130；成功为 0。
- 同一 session 的 CLI 写操作使用进程锁，避免并发覆盖。
- `accept` 离线复核工作流、真实入口执行、指标策略、论文指标追溯、审稿意见和导出产物；`--write-report` 生成带 SHA-256 产物清单的 `acceptance.json`。验收失败时退出码为 1，并逐项说明原因。
- 每次流程结束都会刷新 `acceptance.json`；Evidence 页面显示验收结论和失败检查数。checkpoint 后续发生变化时，旧报告显示为 `stale`，不能继续作为当前证据。
- CLI 的 `repair` 只重新解析保存的原始 JSON，不调用模型；代码执行失败时的自动修复由工作流中的 `repair_from: coding` 单独控制。

## OpenCode 接入

项目已经接入 [OpenCode fork](https://github.com/GOOFY-04/opencode/tree/research-harness)，实现持续同步到该分支。本地开发布局为：

```text
research-harness/
├── main.py
├── harness/
└── opencode-fork/
```

OpenCode 会自动发现父目录中的 harness；其他布局可设置 `RESEARCH_HARNESS_ROOT`。在 fork 中启动 OpenCode 后可使用：

```powershell
cd .\opencode-fork
bun run dev:harness
```

必须通过该脚本启动，使 Bun 读取 `packages/opencode/tsconfig.json` 中的 Solid JSX 配置；不要从仓库根直接运行 `bun run packages/opencode/src/index.ts .`，否则 TUI 会被错误地按 React JSX 编译。

- `/research`：打开配置与研究问题对话框并启动流程；
- `/research-board`：打开当前研究工作区；
- 工作区内按 `U` 从 checkpoint 恢复，按 `S` 选择并只读检查其他会话。

研究工作区采用 Chat / Plan / Execution / Evidence 四个持续可见的入口，一次聚焦一个视图。Plan 展示实验流水线，Execution 展示当前阶段和 worker 日志，Evidence 展示 checkpoint 中实际保存的阶段 JSON 输出与证据账本；左右方向键切换视图，按 O 可选择任意阶段输出。失败阶段、缺失指标、执行证据、科学有效性、审稿意见和产物位置均可直接检查，执行通过与科研结论成立始终分开表达。快捷键为 N 新建、S 会话、R 刷新、O 输出、U 恢复、X 重置、Esc 返回发起工作区的具体对话。

审稿输出把顶会录用建议与证据有效性分开：`recommendation` 评价发表成熟度，`evidence_verdict` 判断当前执行证据是支持、反驳、不充分还是无效。验收允许技术上可信的负结果（`contradicted`），但会拦截无效证据和重大 validity 缺陷。

当审稿结果为 `reject` 或 `weak_reject` 时，按 V 可启动审稿驱动修订。界面显示当前修订轮次；旧代码、实验和论文先归档，审稿中的重大问题及缺失实验会进入下一轮方法设计。

每个新生成的方法（含首次设计）都会接受一致性审计，并单独复核 critical/major 意见，要求引用候选原文并给出数值或执行顺序依据。已确认的问题才进入重新设计；格式错误、超时或尚无法确认的意见保留候选，从审计/复核子步骤恢复。被拒候选及完整审计保存到 session 的 `.audits/`，通过记录绑定候选 SHA-256，离线验收会检测候选后来被改写的情况。Execution 显示 `verifying audit findings` 时正在复核阻塞意见。它仍是模型评审，不是形式化证明，必须结合实际实验和人工核查。

OpenCode CLI 首页也使用 Research Harness 作为主信息架构：从“问题 → 流水线 → 证据 → 审查”开始，展示 checkpoint 可恢复、证据门控和结论边界三项研究契约，并读取真实 checkpoint 汇总当前需要关注的实验。首页输入框优先引导用户声明可证伪问题及所需证据，而不是直接要求生成结论。

进入实际对话后，顶部研究栏会持续显示 Chat / Plan / Execution / Evidence、总体进度、当前阶段和 worker 状态，原生聊天输入框保持可用。宽窗口额外展示完整阶段链，窄窗口保留关键状态；点击状态可直接进入 Execution，返回时回到原对话。正在运行的任务优先显示，没有运行任务时沿用工作台当前选择。

接入架构由 `harness/research_service.py` 和 `opencode-fork/packages/research/` 组成。研究服务调用既有 CLI 和 WorkflowEngine，OpenCode 负责交互；checkpoint 是阶段状态的唯一来源。`run/resume/repair/reset-stage` 立即返回后台任务的接收结果，**不表示研究已完成**。关闭聊天或看板后，已接收的研究任务继续运行。当前版本没有停止后台任务的 UI 命令。

模型阶段使用与输出结构匹配的 token 预算；连续两次供应商读取超时会提前失败，不再耗尽全部语义重试并长时间假运行。Execution 日志会记录每次模型请求的开始、耗时、响应长度，以及 Coder 的 manifest、逐文件生成和 smoke test 子步骤。自动 smoke test 根据实际源代码契约生成；如果测试错误猜测了返回类型，修复器会重建测试，而不是持续扭曲实现去迎合错误断言。

新建研究可选择快速验证、完整实验或 SfM 严格配置；该配置随后台任务保存，恢复时沿用。旧 CLI 会话没有配置记录，首次通过服务恢复时应显式指定原配置。harness 继续使用本目录的 `.env`，无需向 fork 复制 API 凭据。

无需模型调用即可独立检查：

```powershell
cd .\opencode-fork
bun run research status sfm_genview_camera_20260920 --config configs/sfm_full_experiment.yaml
bun run research watch my_research
bun run research logs my_research
bun run research resume my_research
```

看板分别显示流程状态、worker 状态和实验证据；流程完成不会被解释为科研结论成立。详细架构、边界和测试见 [Research workbench 架构说明](opencode-fork/packages/research/README.md)。相关实现已同步到 OpenCode 私有仓库的 `research-harness` 分支。

## 工作流

```text
planning → literature → method_design → coding → code_execution
                                                   ↓
                         documentation ← paper_writing ← self_review
```

引擎根据 `depends_on` 和 `input_from` 进行拓扑排序，支持任意深度的字典字段引用，例如 `code_execution.analysis.summary`。重复阶段、未知依赖、循环依赖、缺失输入字段会明确报错。

只有结构校验通过且未返回 `success: false`、`error`、`parse_error` 的阶段才能完成。重试次数按每次调用计算，累计尝试次数和错误保存在 checkpoint；用 `resume` 可重新尝试耗尽重试或被中断的阶段。改变阶段定义、输入或发现无效缓存时会使相应阶段及下游失效。

`code_execution` 默认把失败的子进程命令、返回码和截断日志反馈给 Coder。Python traceback 能定位到项目文件时，Coder 直接只重写最深故障帧对应的文件；无法确定定位时才生成最小修复计划。完整静态校验后更新 `coding` 检查点并重新执行。修复历史保存诊断、定位来源和文件前后哈希，最多修复次数由执行阶段的 `max_retries` 控制。

审稿驱动修订在进入编码前必须列出变量定义、单调方向和可执行的不变量测试，并经过独立的方法一致性审计。审计会检查更新符号、投影方向、目标函数与梯度主张；存在 critical/major 内部矛盾时方法阶段失败并重试，验收报告也要求保存通过的审计记录。已验证的方法候选会先写入上下文绑定草稿，审计请求超时后只重试审计，CLI/TUI 显示 `candidate ready · audit pending`。

Coder 的 manifest、每个已验证文件和 smoke test 会按研究上下文写入 `.drafts/` 原子草稿；Writer 同样保存标题、摘要和每个已验证章节。模型请求超时或响应截断后，同一上下文只继续缺失文件或章节。对应阶段成功、手动重置或审稿修订时，草稿随旧产物归档，避免错误复用。CLI/TUI 会显示编码文件数与论文章节数。

生成代码前会校验文件依赖图、重复路径和循环依赖，按依赖顺序生成文件，最后生成实验入口。后续文件同时读取已有源码与接口签名，并接收方法不变量和上一轮审稿问题，减少“调用签名正确但状态没有更新”的集成错误。smoke test 要覆盖方法与基线的多步调用顺序；这些检查仍不能代替实验有效性审查。

## 执行与产物校验

- 代码按扩展名生成；检查 Python 语法、YAML/JSON、重复路径以及直接的本地模块导入。
- 文件路径必须位于指定输出目录内，禁止绝对路径、目录穿越、Windows 设备名和替代数据流。
- 每次执行使用新的 `execution_*/` 源码目录，避免旧文件污染导入。
- 默认运行生成的快速测试；没有测试但有入口文件时运行入口。
- 依赖安装到 `session/.venv`。安装失败、子进程失败或超时会使阶段失败，并阻止论文生成。
- 正式入口可启用 `require_metrics: true`；此时退出码为 0 但缺少 `HARNESS_METRICS=<numeric JSON>` 仍会判定失败并进入修复流程。
- 指标 JSON 可嵌套，Harness 以点路径保留所有有限数值叶子。完整实验配置强制输出 `proposed_primary`、`baseline_primary`、`improvement_delta` 与 `sample_count`，校验差值等于前两者之差且样本数为正整数。
- `required_metric_keys` 校验指标是否齐全；`metric_constraints` 可为指标设置 `min`/`max` 数值边界。违反边界的真实执行会失败并把观测值反馈给自动修复器。
- 子进程日志有长度限制；超时或中断时清理进程树。模型服务凭据不传给生成程序。
- 虚拟环境用于依赖隔离，**不是操作系统安全沙箱**；生成程序仍使用当前用户权限。
- 仅从日志中的 `HARNESS_METRICS={"loss": 0.2}` 等 JSON 行提取数字指标，不由模型编造指标。
- 文献元数据来自 arXiv；检索异常使阶段失败。参考文献按检索元数据确定性生成到 `references.bib`，正文引用键必须存在。
- 模型输出截断、拒绝或为空时会明确失败。结构校验无法证明模型输出的学术正确性。

## 配置

`configs/default.yaml` 的 `project_root` 相对于该配置文件；其他项目路径相对于 `project_root`。默认配置不依赖命令运行目录。

模型优先级：Agent 的 `model` 配置 → `llm.default_model` → 对应环境变量 → Agent 内置模型。默认统一使用 `agnes-3.0-flash`。

`llm.protocol` 支持 `openai_compatible` 和 `anthropic`。若要切回 Anthropic，可把协议设为 `anthropic`，设置对应 Base URL、模型和 `api_key_env: ANTHROPIC_API_KEY`。

启用扩展思考时，未显式配置的 `max_tokens` 会至少预留 4096 个输出 token；显式设置必须满足 `1024 <= thinking_budget < max_tokens`。

正式执行入口可配置：

```yaml
agents:
  executor:
    timeout: 600
    install_dependencies: true
    run_entry_point: true
    entry_args: [--config, configs/default.yaml]
    require_metrics: true
```

需要自行准备入口所需数据、硬件和参数。成功运行入口不自动等于完成对照实验。默认 `run_entry_point: false`。

仓库提供 `configs/full_experiment.yaml` 作为端到端验证配置。它会在 smoke test 后执行实验入口，
并直接使用当前 Python 环境的依赖，适合已经安装 PyTorch 的环境：

```bash
python main.py --config configs/full_experiment.yaml run --direction "研究问题" --session full_test
```

## 输出

```text
sessions/<session_id>/
├── checkpoint.json
├── README.md
├── code/
│   ├── ...生成代码...
│   └── requirements.txt
├── output/
│   ├── paper.tex
│   └── references.bib
├── execution_*/       # 各次执行的独立源码
├── .venv/            # 有依赖安装时创建
└── history/          # 变更前 checkpoint 和失效产物
```

论文草稿会检查字面 `\n` 和 `begin/end` 环境是否平衡；完整排版仍需要本机 TeX 环境编译，程序不自动安装 TeX。常规构建：

```bash
cd sessions/<session_id>/output
pdflatex paper.tex
bibtex paper
pdflatex paper.tex
pdflatex paper.tex
```

旧 session 可读取；首次恢复时会校验已有输出，缺失新依赖或不满足新格式的阶段会重跑。旧生成代码和论文不会被本次源码升级自动修补。

## Skills

`configs/skills.yaml` 控制内置技能的 `enabled` 和 `auto_trigger`。启用自动触发后，在 coding 验证通过后执行，结果记录在 `coding.skill_results`。

- `code_review`：模型代码审查。
- `dependency_check`：检查当前解释器中包是否安装以及版本是否满足要求；不执行漏洞扫描或完整依赖求解。
- `test_generation`：生成并语法校验测试；自动触发本身不执行这些测试。

```python
from main import load_config, setup_skills

registry = setup_skills(load_config())
print(registry.list_skills())
result = registry.execute("dependency_check", {"dependencies": "numpy>=1.24"})
print(result)
```

自定义技能继承 `Skill` 并通过 `SkillRegistry.register()` 注册。更多接口示例见 [USAGE.md](USAGE.md)。

## 测试

```bash
python -m pytest -q
python -m pip check
```

恢复与证据门的离线对照实验可独立复现：

```powershell
.\.venv\Scripts\python.exe -m experiments.harness_ablation --output experiments/results/harness_ablation.json
```

开源项目的能力重叠、当前可辩护的差异点和声明边界见 [原创性对照](docs/ORIGINALITY.md)。

测试使用临时 session、模拟模型响应和真实的小型 Python 子进程，不调用付费 API、不下载训练数据。覆盖默认八阶段集成、CLI 恢复/失效传播、失败输出、路径限制、代码与文档校验、引用、配置和进程超时。

## 代码结构

- `harness/core/`：工作流、checkpoint、记忆、模型访问、路径与持久化。
- `harness/agents/`：八个专职 Agent。
- `harness/tools/`：arXiv、静态校验、子进程和文件写入。
- `harness/skills/`：可注册的辅助功能。
- `workflows/research.yaml`：默认流程与数据依赖。
- `tests/`：离线回归与集成测试。
