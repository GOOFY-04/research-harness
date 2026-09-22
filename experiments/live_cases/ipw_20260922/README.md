# Preserved IPW failure

The five Python files are unchanged generated source from the completed
`paired_ipw_20260922` workflow. `case.json` preserves normalized source hashes,
the original task, method and review outputs, the measurement contract, and
all 36 recorded metrics. The source comments and model claims contain errors.

All eight stages completed, and the initial automatic acceptance missed the
original-question violations. Subsequent independent executable diagnostics
identified the reversed propensity sign and a single-scenario primary metric
where the task required an equal three-scenario aggregate. Acceptance now rejects
the session using the supplemental original-requirements gate. The original
method and review records are retained, including their missed defects.

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m experiments.replay_ipw_case --output-dir .research/ipw-replay
```

No model calls or downloads are needed. This verifies source hashes, executes
the entry point, recomputes paired statistics, checks the original 36 metrics,
and reruns the requirement probes. Logs, raw pairs, checkpoint, probe record,
and `replay.json` remain in the chosen output directory. Exit code 0 means
the known failure was reproduced, not that the research is acceptable.

The probe is a case-specific, human-authored post-run diagnostic. Its aggregation
check uses the archived per-scenario means; it does not regenerate independent
statistical samples. This case does not establish general review accuracy or
algorithmic novelty.
