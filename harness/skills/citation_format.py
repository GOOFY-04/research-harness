"""
CitationFormatSkill — BibTeX 引用验证与格式化

输入：
  - bibtex: BibTeX 条目文本（必填）
  - style: 目标格式 "acm" | "ieee" | "neurips" | "iclr" | "cvpr"（默认 neurips）

输出：
  - valid: True/False
  - entries: 解析后的条目列表 [{key, type, fields}]
  - errors: 格式错误列表
  - warnings: 警告（如缺少推荐字段）
  - formatted: 格式化后的 BibTeX
"""

import logging
import re
from typing import Any

from harness.core.skill import Skill

logger = logging.getLogger(__name__)

# 每种 entry type 的必需字段和推荐字段
_REQUIRED_FIELDS = {
    "article": {"author", "title", "journal", "year"},
    "inproceedings": {"author", "title", "booktitle", "year"},
    "proceedings": {"title", "year"},
    "book": {"author|editor", "title", "publisher", "year"},
    "phdthesis": {"author", "title", "school", "year"},
    "mastersthesis": {"author", "title", "school", "year"},
    "techreport": {"author", "title", "institution", "year"},
    "misc": {"author|title"},
    "incollection": {"author", "title", "booktitle", "publisher", "year"},
    "unpublished": {"author", "title", "note"},
}

_RECOMMENDED_FIELDS = {
    "article": {"volume", "number", "pages", "doi", "month"},
    "inproceedings": {"pages", "doi", "address", "month", "organization"},
    "book": {"volume", "series", "address", "edition", "isbn"},
    "phdthesis": {"address", "month"},
    "techreport": {"number", "address", "month"},
}

# 常见会议/期刊缩写映射
_VENUE_ABBREV = {
    "neural information processing systems": "NeurIPS",
    "advances in neural information processing systems": "NeurIPS",
    "international conference on machine learning": "ICML",
    "international conference on learning representations": "ICLR",
    "computer vision and pattern recognition": "CVPR",
    "international conference on computer vision": "ICCV",
    "european conference on computer vision": "ECCV",
    "association for computational linguistics": "ACL",
    "empirical methods in natural language processing": "EMNLP",
    "north american chapter of the association for computational linguistics": "NAACL",
    "special interest group on computer graphics and interactive techniques": "SIGGRAPH",
    "acm transactions on graphics": "ACM TOG",
}


class CitationFormatSkill(Skill):
    name = "citation_format"
    description = "验证和格式化 BibTeX 引用条目，检查缺失字段，统一格式"

    def validate_inputs(self, inputs: dict) -> bool:
        return "bibtex" in inputs

    def execute(self, inputs: dict) -> dict:
        bibtex = inputs["bibtex"]
        style = inputs.get("style", "neurips")

        # 解析 BibTeX 条目
        entries, parse_errors = _parse_bibtex(bibtex)
        if not entries:
            return {
                "success": False,
                "valid": False,
                "entries": [],
                "errors": parse_errors,
                "warnings": [],
                "formatted": "",
            }

        # 验证每个条目
        all_errors: list[str] = list(parse_errors)
        all_warnings: list[str] = []

        for entry in entries:
            etype = entry["type"].lower()
            fields = set(entry["fields"].keys())

            # 检查必需字段
            required = _REQUIRED_FIELDS.get(etype, set())
            for req in required:
                if "|" in req:
                    alts = req.split("|")
                    if not any(a in fields for a in alts):
                        all_errors.append(f"[{entry['key']}] 缺少必需字段 {' 或 '.join(alts)}")
                elif req not in fields:
                    all_errors.append(f"[{entry['key']}] 缺少必需字段 '{req}'")

            # 检查推荐字段
            recommended = _RECOMMENDED_FIELDS.get(etype, set())
            for rec in recommended:
                if rec not in fields:
                    all_warnings.append(f"[{entry['key']}] 建议添加字段 '{rec}'")

            # 检查 year 格式
            if "year" in entry["fields"]:
                year = entry["fields"]["year"].strip()
                if not re.match(r"^\d{4}$", year):
                    all_warnings.append(f"[{entry['key']}] year '{year}' 格式异常")

            # 检查 author 格式
            if "author" in entry["fields"]:
                author = entry["fields"]["author"]
                if " and " not in author and "," not in author:
                    all_warnings.append(f"[{entry['key']}] author 可能只包含一个作者")

            # 标准化 venue 名称
            if "booktitle" in entry["fields"]:
                bt_lower = entry["fields"]["booktitle"].strip().lower()
                if bt_lower in _VENUE_ABBREV:
                    entry["fields"]["booktitle"] = _VENUE_ABBREV[bt_lower]
            if "journal" in entry["fields"]:
                j_lower = entry["fields"]["journal"].strip().lower()
                if j_lower in _VENUE_ABBREV:
                    entry["fields"]["journal"] = _VENUE_ABBREV[j_lower]

        # 格式化输出
        formatted = _format_bibtex(entries, style)

        return {
            "success": True,
            "valid": len(all_errors) == 0,
            "entries": entries,
            "errors": all_errors,
            "warnings": all_warnings,
            "formatted": formatted,
        }


# ------------------------------------------------------------------
# BibTeX 解析器
# ------------------------------------------------------------------

_BIBTEX_ENTRY_RE = re.compile(
    r"""@(\w+)\s*\{\s*([^,]+)\s*,\s*   # @type{key,
        ([\s\S]*?)                       # fields
    \}\s*$""",                          # }
    re.MULTILINE | re.VERBOSE,
)


def _parse_bibtex(text: str) -> tuple[list[dict], list[str]]:
    """解析 BibTeX 文本为条目列表。"""
    entries = []
    errors = []

    # 移除注释和空行
    text = re.sub(r"%.*$", "", text, flags=re.MULTILINE)

    # 方法：查找每个 @type{ 并匹配到对应的 }
    pos = 0
    while pos < len(text):
        at_pos = text.find("@", pos)
        if at_pos == -1:
            break

        # 找到类型和 key
        brace_start = text.find("{", at_pos)
        if brace_start == -1:
            errors.append(f"位置 {at_pos}: 缺少 '{{'")
            pos = at_pos + 1
            continue

        # 匹配花括号（注意嵌套）
        depth = 0
        end_pos = brace_start
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break

        if depth != 0:
            errors.append(f"位置 {at_pos}: 花括号不匹配")
            pos = at_pos + 1
            continue

        # 提取 entry 内容
        entry_text = text[at_pos:end_pos + 1]
        match = re.match(r"@(\w+)\s*\{\s*([^,]+)\s*,\s*([\s\S]*)", entry_text)
        if not match:
            errors.append(f"位置 {at_pos}: 无法解析条目")
            pos = end_pos + 1
            continue

        etype = match.group(1).strip()
        key = match.group(2).strip()
        fields_text = match.group(3).strip()

        # 解析字段
        fields = _parse_fields(fields_text)
        entries.append({"type": etype, "key": key, "fields": fields})

        pos = end_pos + 1

    return entries, errors


def _parse_fields(text: str) -> dict[str, str]:
    """解析 BibTeX 条目内的字段。"""
    fields = {}
    # 匹配 key = {value} 或 key = "value" 或 key = value
    pattern = re.compile(r"""(\w+)\s*=\s*(?:\{([^{}]*)\}|"([^"]*)"|(\S+))\s*,?""")
    for match in pattern.finditer(text):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        value = value.strip().rstrip(",")
        fields[key] = value
    return fields


def _format_bibtex(entries: list[dict], style: str) -> str:
    """按指定格式输出 BibTeX。"""
    lines = []
    for entry in entries:
        lines.append(f"@{entry['type']}{{{entry['key']},")
        for k, v in entry["fields"].items():
            lines.append(f"  {k} = {{{v}}},")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)
