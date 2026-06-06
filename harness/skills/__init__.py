"""
内置 Skills 模块
"""

from .code_review import CodeReviewSkill
from .dependency_check import DependencyCheckSkill
from .test_generation import TestGenerationSkill
from .paper_summary import PaperSummarySkill
from .latex_compile import LaTeXCompileSkill
from .experiment_tracker import ExperimentTrackerSkill
from .citation_format import CitationFormatSkill
from .plot_generation import PlotGenerationSkill

__all__ = [
    "CodeReviewSkill",
    "DependencyCheckSkill",
    "TestGenerationSkill",
    "PaperSummarySkill",
    "LaTeXCompileSkill",
    "ExperimentTrackerSkill",
    "CitationFormatSkill",
    "PlotGenerationSkill",
]
