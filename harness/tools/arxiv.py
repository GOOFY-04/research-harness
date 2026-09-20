"""
arxiv 工具 — 搜索和获取论文元数据

使用 arxiv 官方 API（无需 key），返回结构化论文列表。
"""

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional


ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def search_arxiv(
    query: str,
    max_results: int = 10,
    sort_by: str = "relevance",  # relevance | lastUpdatedDate | submittedDate
    category: Optional[str] = None,  # 如 cs.CV, cs.LG, cs.AI
) -> list[dict]:
    """
    搜索 arXiv 论文。

    Args:
        query: 搜索词（支持 AND/OR/NOT）
        max_results: 最多返回条数
        sort_by: 排序方式
        category: 限定 arXiv 分类

    Returns:
        论文列表，每项包含 title/authors/abstract/arxiv_id/url/published
    """
    search_query = query if ":" in query else f"all:({query})"
    if category:
        search_query = f"cat:{category} AND ({search_query})"

    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    })

    url = f"{ARXIV_API}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            xml_data = resp.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"arXiv API 请求失败: {e}") from e

    return _parse_arxiv_response(xml_data)


def fetch_paper(arxiv_id: str) -> dict:
    """获取单篇论文的详细信息。"""
    params = urllib.parse.urlencode({
        "id_list": arxiv_id,
        "max_results": 1,
    })
    url = f"{ARXIV_API}?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        xml_data = resp.read().decode("utf-8")
    results = _parse_arxiv_response(xml_data)
    return results[0] if results else {}


def _parse_arxiv_response(xml_data: str) -> list[dict]:
    root = ET.fromstring(xml_data)
    papers = []

    for entry in root.findall("atom:entry", NS):
        # arxiv_id: 从 <id> 提取
        id_text = entry.findtext("atom:id", "", NS)
        arxiv_id = id_text.split("/abs/")[-1].strip()

        title_el = entry.find("atom:title", NS)
        title = " ".join((title_el.text or "").split()) if title_el is not None else ""

        summary_el = entry.find("atom:summary", NS)
        abstract = " ".join((summary_el.text or "").split()) if summary_el is not None else ""

        authors = [
            a.findtext("atom:name", "", NS)
            for a in entry.findall("atom:author", NS)
        ]

        published = entry.findtext("atom:published", "", NS)[:10]  # YYYY-MM-DD

        # 分类
        categories = [
            c.get("term", "")
            for c in entry.findall("atom:category", NS)
        ]

        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors[:5],  # 最多5位作者
            "abstract": abstract[:500],  # 截断摘要
            "published": published,
            "categories": categories,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        })

    return papers
