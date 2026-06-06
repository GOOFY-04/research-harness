"""
ExperimentTrackerSkill — 实验指标追踪与对比

输入：
  - experiments: [ {name, metrics: {key: value}, metadata: {...}}, ... ]  或
  - log_text: 原始实验日志文本（自动解析指标）
  - metric_keys: 要追踪的指标名列表（如 ["loss", "psnr", "ssim", "compression_rate"]）
  - output_format: "table" | "json" | "markdown"（默认 markdown）

输出：
  - comparison_table: 格式化的对比表
  - best: 各指标最优的实验
  - summary: 文字总结
  - raw_metrics: 解析后的原始指标数据
"""

import logging
import re
from typing import Any

from harness.core.skill import Skill

logger = logging.getLogger(__name__)

# 常见实验指标的解析模式
_METRIC_PATTERNS = {
    "loss": [
        r"(?:loss|Loss)\s*[:=]?\s*([\d.]+(?:e[+-]?\d+)?)",
        r"(?:train_loss|val_loss|test_loss)\s*[:=]?\s*([\d.]+(?:e[+-]?\d+)?)",
    ],
    "accuracy": [
        r"(?:accuracy|acc|Acc(?:uracy)?)\s*[:=]?\s*([\d.]+)",
        r"(?:top-?1)\s*[:=]?\s*([\d.]+)",
    ],
    "psnr": [
        r"(?:PSNR|psnr)\s*[:=]?\s*([\d.]+)\s*(?:dB)?",
    ],
    "ssim": [
        r"(?:SSIM|ssim)\s*[:=]?\s*([\d.]+)",
    ],
    "compression_rate": [
        r"(?:compression.rate|rate|ratio)\s*[:=]?\s*([\d.]+(?:e[+-]?\d+)?)",
        r"(?:压缩率|压缩比)\s*[:=]?\s*([\d.]+(?:e[+-]?\d+)?)",
    ],
    "num_gaussians": [
        r"(?:num.gaussians|gaussian.count|remaining)\s*[:=]?\s*(\d+)",
        r"(?:高斯数|保留数量)\s*[:=]?\s*(\d+)",
    ],
    "train_time": [
        r"(?:time|duration|elapsed)\s*[:=]?\s*([\d.]+)\s*(?:s|sec|秒|min|分钟|h|小时)",
    ],
    "memory": [
        r"(?:memory|VRAM|显存)\s*[:=]?\s*([\d.]+)\s*(?:MB|GB|MiB|GiB)",
    ],
}


class ExperimentTrackerSkill(Skill):
    name = "experiment_tracker"
    description = "追踪和对比多个实验的指标，生成对比表格和最优分析"

    def validate_inputs(self, inputs: dict) -> bool:
        return ("experiments" in inputs) or ("log_text" in inputs)

    def execute(self, inputs: dict) -> dict:
        experiments = inputs.get("experiments", [])
        log_text = inputs.get("log_text", "")
        metric_keys = inputs.get("metric_keys", [])
        output_format = inputs.get("output_format", "markdown")

        # 如果提供了原始日志，解析实验指标
        if log_text and not experiments:
            experiments = _parse_log_to_experiments(log_text, metric_keys)
        elif log_text and experiments:
            # 对已有 experiments 补充解析日志中的指标
            for exp in experiments:
                if "log" in exp:
                    parsed = _extract_metrics(exp["log"], metric_keys)
                    exp.setdefault("metrics", {}).update(parsed)

        if not experiments:
            return {"success": False, "error": "未提供实验数据", "comparison_table": "", "best": {}}

        # 自动检测所有指标名
        if not metric_keys:
            all_keys = set()
            for exp in experiments:
                all_keys.update(exp.get("metrics", {}).keys())
            metric_keys = sorted(all_keys)

        # 生成对比表
        comparison_table = _build_comparison_table(experiments, metric_keys, output_format)

        # 找到各指标最优
        best = _find_best(experiments, metric_keys)

        # 生成文字总结
        summary = _generate_summary(experiments, metric_keys, best)

        return {
            "success": True,
            "comparison_table": comparison_table,
            "best": best,
            "summary": summary,
            "raw_metrics": [{"name": e.get("name", "unknown"), "metrics": e.get("metrics", {})} for e in experiments],
        }


def _extract_metrics(text: str, metric_keys: list[str] | None = None) -> dict[str, float]:
    """从文本中提取数值指标。"""
    metrics: dict[str, float] = {}
    keys = metric_keys if metric_keys else list(_METRIC_PATTERNS.keys())

    for key in keys:
        patterns = _METRIC_PATTERNS.get(key, [(rf"(?:{key})\s*[:=]?\s*([\d.]+(?:e[+-]?\d+)?)",)])
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    metrics[key] = float(match.group(1))
                except ValueError:
                    pass
                break

    return metrics


def _parse_log_to_experiments(log_text: str, metric_keys: list[str]) -> list[dict]:
    """将日志文本解析为实验列表。尝试按 epoch 或分隔符拆分。"""
    experiments = []

    # 按 epoch 拆分
    epoch_blocks = re.split(r"(?:Epoch|epoch)\s+\d+", log_text)
    if len(epoch_blocks) > 1:
        for i, block in enumerate(epoch_blocks[1:], 1):  # 跳过第一个空块
            metrics = _extract_metrics(block, metric_keys)
            if metrics:
                experiments.append({"name": f"Epoch {i}", "metrics": metrics})

    # 如果按 epoch 拆不出来，尝试按实验名拆分
    if not experiments:
        sections = re.split(r"={3,}|\*{3,}|#{1,3}\s+", log_text)
        for i, section in enumerate(sections):
            section = section.strip()
            if not section:
                continue
            # 取第一行作为名称
            lines = section.split("\n")
            name = lines[0].strip()[:80] if lines else f"Experiment {i + 1}"
            metrics = _extract_metrics(section, metric_keys)
            if metrics:
                experiments.append({"name": name, "metrics": metrics})

    # 如果还是空的，整体当做一个实验
    if not experiments:
        metrics = _extract_metrics(log_text, metric_keys)
        if metrics:
            experiments.append({"name": "Experiment", "metrics": metrics})

    return experiments


def _build_comparison_table(experiments: list[dict], metric_keys: list[str], fmt: str) -> str:
    """生成对比表。"""
    if not experiments or not metric_keys:
        return ""

    if fmt == "markdown":
        header = "| Experiment | " + " | ".join(k for k in metric_keys) + " |"
        sep = "|---" * (len(metric_keys) + 1) + "|"
        rows = []
        for exp in experiments:
            name = exp.get("name", "?")
            vals = []
            for k in metric_keys:
                v = exp.get("metrics", {}).get(k)
                vals.append(f"{v:.4f}" if isinstance(v, float) else f"{v}" if v is not None else "-")
            rows.append(f"| {name} | " + " | ".join(vals) + " |")
        return "\n".join([header, sep] + rows)

    elif fmt == "json":
        import json
        return json.dumps(experiments, ensure_ascii=False, indent=2)

    else:
        # plain table
        lines = []
        col_widths = [max(len(e.get("name", "?")) for e in experiments)] + [12] * len(metric_keys)
        header_parts = ["Experiment".ljust(col_widths[0])]
        for i, k in enumerate(metric_keys):
            header_parts.append(k.ljust(col_widths[i + 1]))
        lines.append("  ".join(header_parts))
        lines.append("-" * sum(col_widths + [2] * len(col_widths)))
        for exp in experiments:
            parts = [exp.get("name", "?").ljust(col_widths[0])]
            for i, k in enumerate(metric_keys):
                v = exp.get("metrics", {}).get(k, "-")
                parts.append(f"{v:.4f}".ljust(col_widths[i + 1]) if isinstance(v, float) else str(v).ljust(col_widths[i + 1]))
            lines.append("  ".join(parts))
        return "\n".join(lines)


def _find_best(experiments: list[dict], metric_keys: list[str]) -> dict[str, dict]:
    """找出每个指标的最优实验（lower_is_better）和最优值。"""
    # 通常越低越好的指标
    lower_is_better = {"loss", "memory", "train_time", "compression_rate", "num_gaussians"}
    # 越高越好的指标
    higher_is_better = {"accuracy", "psnr", "ssim"}

    best: dict[str, dict] = {}
    for key in metric_keys:
        candidates = []
        for exp in experiments:
            val = exp.get("metrics", {}).get(key)
            if val is not None:
                candidates.append((val, exp.get("name", "?")))

        if not candidates:
            continue

        if key in higher_is_better:
            best_val, best_name = max(candidates, key=lambda x: x[0])
        else:
            best_val, best_name = min(candidates, key=lambda x: x[0])

        best[key] = {"experiment": best_name, "value": best_val, "direction": "higher" if key in higher_is_better else "lower"}

    return best


def _generate_summary(experiments: list[dict], metric_keys: list[str], best: dict) -> str:
    """生成文字总结。"""
    lines = [f"共追踪 {len(experiments)} 个实验，{len(metric_keys)} 个指标。"]
    for key, info in best.items():
        direction = "最高" if info["direction"] == "higher" else "最低"
        lines.append(f"- {key}: {direction} = {info['value']:.4f} ({info['experiment']})")
    return "\n".join(lines)
