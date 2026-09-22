# 配对测量契约

`configs/full_experiment.yaml` 现在要求编码阶段在运行前保存 `experiment_contract`。
契约声明比较对象、指标定义与单位、优化方向、配对单位、评测范围和抽样假设。
它用于约束实际测量，不要求提出的方法优于基线。
文件清单与测量契约分步生成，契约通过校验后才生成源码。契约请求中断时保留已验证的文件清单，
下次只恢复契约步骤；无效清单和契约响应保存到会话 `.drafts/failures/`，不覆盖正常进度草稿。

每个比较使用如下结构；按实际课题修改字段，不能复制示例含义套用到其他指标。

```json
{
  "version": 1,
  "primary_comparison": "mse",
  "comparisons": [{
    "id": "mse",
    "metric_name": "mean squared estimation error",
    "definition": "每个独立种子的估计误差平方，再对种子取算术均值",
    "unit": "目标量单位的平方",
    "direction": "minimize",
    "sample_unit": "一个独立种子",
    "pairing": "同一种子中两种方法使用相同样本",
    "evaluation_scope": "固定合成分布、样本量和预先声明的种子集合",
    "sampling_assumptions": "不同种子视为独立重复；不将同一序列的时间点视为独立重复",
    "proposed_metric": "proposed_primary",
    "baseline_metric": "baseline_primary",
    "samples_path": "results/mse_pairs.json"
  }]
}
```

正式入口在运行时写出配对文件，格式为 JSON 数组：

```json
[
  {"pair_id": "seed-0", "proposed": 0.3, "baseline": 0.2},
  {"pair_id": "seed-1", "proposed": 0.5, "baseline": 0.3}
]
```

同时照常打印 `HARNESS_METRICS=...`。每个比较对应的指标必须等于各列的算术均值。
标准主指标 `proposed_primary`、`baseline_primary`、`improvement_delta` 和 `sample_count`
分别对应主比较的两列均值、提出方法减去基线的原始差值，以及配对行数。
多个场景聚合时，先按声明权重在种子组内聚合，再将独立种子组作为配对行；同时保留逐场景比较。

执行器会清除冒烟测试留下的同名结果文件，运行正式入口，再独立计算配对差值的均值和样本标准误。
原始配对文件复制到会话归档，保存字节数和 SHA-256；恢复和离线验收重新计算统计量，
并核对编码阶段的契约。自动代码修复保留契约，不能改指标或评测范围以适应结果。
CLI/TUI 的 Evidence 面板展示已记录的指标单位、方向、配对数、差值和描述性 SE；原始配对文件列入产物路径。
状态面板展示已保存记录，实时文件完整性以重新运行离线验收的结果为准。

当前资源边界为 1–16 个比较，每个文件至多 5 MiB、2–20000 个配对行。`pair_id` 必须唯一，
数值必须有限；运行时数据文件不能冒充编码阶段的静态源码文件。

这些检查证明的是声明、原始数据与汇总结果的一致性。它们无法自动证明样本独立、数据未伪造、
源码正确实现了测量定义，或实验设计足以回答原问题。配对 SE 是描述性统计，
不自动构成显著性检验或置信区间。抽样假设、泄漏和比较公平性仍需结合源码审查。

历史会话不会自动补出丢失的数据；需在明确契约后重新执行。旧配置可继续进行历史复跑，
但不能把未配置的配对校验描述为已经通过。验收报告逐项标明检查是否必需。

## 对原始研究要求的补充校验

IPW 实测暴露了数值契约的边界：程序、原始配对数据与汇总指标可以相互一致，却共同偏离用户问题。
因此允许在会话根目录登记 `requirements_audit.json`，作为额外的必需验收项
`evidence:original-requirements`。它不能覆盖其他失败项。

记录绑定原始问题、编码输出、正式执行指标及日志清单的上下文哈希；每条检查引用原题的连续原文，
给出已执行探针的指标名和有限数值期望。验收重新读取完整 stdout/stderr、校验哈希并提取指标，
而不是相信探针报告中的通过标志。固定容差为相对 `1e-9`、绝对 `1e-12`。
问题、代码或正式指标变化后，旧探针不能继续放行。探针文件变化使已有验收报告过期。

本次 IPW 专用命令会持有会话锁，并先在 checkpoint 元数据登记
`requirements_audit_required=true`，因此探针中断或文件丢失后验收仍会阻断。
它保留全部模型阶段输出，只追加登记和探针材料。其他未登记会话不自动获得此项验证。

```powershell
.\.venv\Scripts\python.exe -m experiments.audit_ipw_requirements sessions/paired_ipw_20260922
.\.venv\Scripts\python.exe main.py accept --session paired_ipw_20260922 --write-report
# 无 API 的已保存失败案例复跑；返回 0 表示预期缺陷成功复现
.\.venv\Scripts\python.exe -m experiments.replay_ipw_case --output-dir .research/ipw-replay
```

这套 IPW 探针是事后针对原有明确要求人工编写的诊断：实际调用缺失概率函数，并从已归档场景 MSE
重算应有的主指标。它不是通用自动科研验证器，不独立重建全部种子数据，也不能证明人工期望值本身正确。
哈希用于发现材料不一致，不是签名；能同时改写检查、checkpoint 和日志的人仍能伪造记录。

方法审计协议 v3 同时将原始问题传给初审和复核，绑定上下文哈希，防止约束改变后复用旧通过结论。
历史 v2 记录仍可作内部一致性记录读取，但不代表当时检查过原始约束。模型审计仍可能误判，不能替代执行探针。
复核允许选择 `candidate_quote_id`，由程序从当前候选取回确切原文；未知编号、编号与同时提供的引文不符
仍会阻断。这样避免为了复制代码缩进反复调用模型，同时保留逐字引用约束；编号本身不证明审计理由成立。

## 负结果审稿校准

```powershell
.\.venv\Scripts\python.exe -m experiments.review_calibration --output-dir .research/review-controls
.\.venv\Scripts\python.exe -m experiments.review_calibration --live --output-dir .research/live-review-controls
```

离线命令实际执行两个手工构造的对照：合法的负结果和读取未来标签的错误预测器。
`--live` 额外调用模型审稿，不伪造模型回答。它是诊断对照，不是自主研究能力或总体审稿准确率测评。

[2026-09-22 实测记录](../experiments/results/review_calibration_20260922.json) 保留了提示修改前后的回答：
第一次模型把负结果的低研究价值列为 validity 缺陷；修改后两项分类都正确，
但负结果回答仍使用缺少检验支持的“显著”措辞，并建议更换原假设。这些问题尚未解决。
验收已修正为只由 critical/major **validity** 问题阻断测量可信性；论文发表建议单独保留。

## 可复现的机制对照

```powershell
.\.venv\Scripts\python.exe -m experiments.harness_ablation --output experiments/results/harness_ablation_20260922.json
```

[对照结果](../experiments/results/harness_ablation_20260922.json) 中，故障注入程序保留真实配对观测，
但把报告的提出方法均值缩小 100 倍，并同步修改差值，使错误汇总仍满足减法关系。
仅检查数值关系和 stdout 归档时放行；配对原始数据检查拒绝了同一程序。
这证明新增检查在该故障下提供了额外检测能力，不是对全部错误的覆盖率，也不是对开源系统的性能排名。
