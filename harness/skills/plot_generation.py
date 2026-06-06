"""
PlotGenerationSkill — 实验结果图表生成

输入：
  - data: 绘图数据，支持两种格式：
    1. {x: [values], y: [values], label: "series_name"}  — 单条曲线
    2. [ {x:..., y:..., label:...}, ... ]  — 多条曲线
    3. {categories: [...], values: [...]} — 柱状图
  - plot_type: "line" | "bar" | "scatter" | "multi_line"（默认根据数据推测）
  - title: 图表标题
  - xlabel: x 轴标签
  - ylabel: y 轴标签
  - output_path: 输出图片路径（必填）
  - figsize: 图片尺寸 (w, h)，默认 (10, 6)
  - style: "default" | "seaborn" | "ieee"（默认 seaborn）

输出：
  - success: True/False
  - output_path: 生成的图片路径
  - error: 错误信息（如果失败）
"""

import logging
import os
from pathlib import Path
from typing import Any

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class PlotGenerationSkill(Skill):
    name = "plot_generation"
    description = "生成实验结果的 matplotlib 图表（折线图、柱状图、散点图、多曲线对比）"

    def validate_inputs(self, inputs: dict) -> bool:
        return "data" in inputs and "output_path" in inputs

    def execute(self, inputs: dict) -> dict:
        try:
            import matplotlib
            matplotlib.use("Agg")  # 非交互后端
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as e:
            return {
                "success": False,
                "error": f"matplotlib 未安装: {e}。请运行 pip install matplotlib numpy",
            }

        data = inputs["data"]
        plot_type = inputs.get("plot_type", "auto")
        title = inputs.get("title", "")
        xlabel = inputs.get("xlabel", "")
        ylabel = inputs.get("ylabel", "")
        output_path = inputs["output_path"]
        figsize = inputs.get("figsize", (10, 6))
        style = inputs.get("style", "seaborn")

        # 设置样式
        if style == "seaborn":
            try:
                plt.style.use("seaborn-v0_8-darkgrid")
            except Exception:
                try:
                    plt.style.use("ggplot")
                except Exception:
                    pass
        elif style == "ieee":
            plt.rcParams.update({
                "font.family": "serif",
                "font.size": 10,
                "axes.titlesize": 12,
                "axes.labelsize": 10,
                "legend.fontsize": 9,
                "figure.dpi": 300,
            })

        # 创建输出目录
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots(figsize=figsize)

        try:
            # 自动推断 plot_type
            if plot_type == "auto":
                plot_type = _infer_plot_type(data)

            if plot_type in ("line", "multi_line"):
                series_list = _normalize_series(data)
                for s in series_list:
                    ax.plot(s["x"], s["y"], marker="o", markersize=3, linewidth=1.5, label=s.get("label", ""))
                if len(series_list) > 1:
                    ax.legend()

            elif plot_type == "bar":
                categories = data.get("categories", [])
                values = data.get("values", [])
                if not categories and isinstance(data, dict):
                    items = [(k, v) for k, v in data.items() if k not in ("categories", "values", "type")]
                    if items:
                        categories, values = zip(*items) if items else ([], [])

                x = np.arange(len(categories))
                bars = ax.bar(x, values, width=0.6, edgecolor="white", linewidth=0.5)
                ax.set_xticks(x)
                ax.set_xticklabels(categories, rotation=45, ha="right")

                # 柱顶显示数值
                for bar, val in zip(bars, values):
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                            f"{val:.3g}", ha="center", va="bottom", fontsize=8)

            elif plot_type == "scatter":
                series_list = _normalize_series(data)
                for s in series_list:
                    ax.scatter(s["x"], s["y"], alpha=0.6, s=20, label=s.get("label", ""))
                if len(series_list) > 1:
                    ax.legend()

            else:
                return {"success": False, "error": f"不支持的 plot_type: {plot_type}"}

            if title:
                ax.set_title(title)
            if xlabel:
                ax.set_xlabel(xlabel)
            if ylabel:
                ax.set_ylabel(ylabel)

            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(str(out_path), dpi=300, bbox_inches="tight")
            plt.close(fig)

            logger.info(f"[PlotGenerationSkill] 图表已保存: {out_path}")
            return {
                "success": True,
                "output_path": str(out_path),
                "plot_type": plot_type,
            }

        except Exception as e:
            plt.close(fig)
            logger.error(f"[PlotGenerationSkill] 绘图失败: {e}")
            return {"success": False, "error": str(e), "output_path": str(out_path)}


def _infer_plot_type(data: Any) -> str:
    """根据数据结构自动推断图表类型。"""
    if isinstance(data, list):
        if all(isinstance(d, dict) and "x" in d and "y" in d for d in data):
            return "multi_line"
    if isinstance(data, dict):
        if "categories" in data and "values" in data:
            return "bar"
        if "x" in data and "y" in data:
            return "line"
    return "line"


def _normalize_series(data: Any) -> list[dict]:
    """将数据规范化为 series 列表。"""
    if isinstance(data, list):
        return [{"x": d.get("x", []), "y": d.get("y", []), "label": d.get("label", "")} for d in data]
    if isinstance(data, dict):
        return [{"x": data.get("x", []), "y": data.get("y", []), "label": data.get("label", "")}]
    return []
