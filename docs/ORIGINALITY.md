# Originality and overlap audit

This document records a bounded comparison made on 2026-09-21. It does not
claim that no unpublished or uninspected system has the same design. The links
below are primary project documentation; claims are limited to behavior those
projects publicly describe.

## What is shared with existing projects

| Project | Documented overlap | Consequence for our claims |
| --- | --- | --- |
| [AI Scientist](https://github.com/SakanaAI/AI-Scientist) | Idea generation, experiments, paper generation, and automated review form an end-to-end research loop. | An end-to-end autonomous research pipeline is not unique to Research Harness. |
| [Agent Laboratory](https://github.com/Point-Zheng/agentlaboratory) | Specialized agents cover literature review, planning, experiments, and reports. | Role-specialized research agents and the stage sequence are not unique. |
| [Thinkless](https://github.com/wici-ai/thinkless) | Durable goal/plan artifacts, failure recovery, continued execution, and a terminal workspace are core features. | Persistence, crash recovery, and a research-styled TUI are not individually unique. |
| [OpenHands](https://github.com/OpenHands/OpenHands) | Persistent agent state, event history, resumable conversations, execution runtimes, and control surfaces are general agent infrastructure. | Session persistence and observable execution are established agent capabilities. |

## Narrow differentiated claim

Research Harness currently differentiates itself through the integration of
four mechanisms around a research checkpoint:

1. Dependency-aware stage invalidation and targeted code repair preserve valid
   upstream evidence while replacing affected downstream claims.
2. Executed metrics are machine-readable evidence. The paper's
   `verified_metrics` and `evidence_scope` must exactly match the execution
   checkpoint instead of being reconstructed from prose.
3. The acceptance gate distinguishes workflow completion from scientific
   acceptance. It rejects failed metric policies, weak reviews, major unresolved
   weaknesses, and exported artifacts that differ from checkpoint content.
4. `acceptance.json` binds that decision to the checkpoint SHA-256 and hashes
   every delivered artifact. The CLI/TUI marks the report stale after any later
   checkpoint mutation.

The defensible claim is this integrated evidence contract and its observable
failure behavior. We do not claim invention of autonomous research, multi-agent
workflows, checkpoints, self-review, terminal interfaces, or recovery by
themselves.

## Reproducible counterfactual

Run the offline benchmark:

```powershell
.\.venv\Scripts\python.exe -m experiments.harness_ablation --output experiments/results/harness_ablation.json
```

The recovery comparison starts both variants after the same injected failure.
Checkpoint resume runs only the failed stage; stateless restart reruns the valid
upstream stage. The evidence comparison holds the completed checkpoint constant
while either tampering with an exported file or inserting a weak review. A
status-only gate accepts both packages, while the strict gate rejects them and
names the violated checks.

This experiment validates the claimed mechanics. It does not establish better
research quality, lower real-model cost, or uniqueness across the entire open
source ecosystem. Those claims require multi-project benchmarks and more than
one real research run.
