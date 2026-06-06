"""
arxiv 工具 — 搜索和获取论文元数据

使用 arxiv 官方 API（无需 key），返回结构化论文列表。
"""

import time
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
    retries: int = 3,
    backoff: float = 5.0,
) -> list[dict]:
    """
    搜索 arXiv 论文，支持 429 限流自动退避重试。

    Args:
        query: 搜索词（支持 AND/OR/NOT）
        max_results: 最多返回条数
        sort_by: 排序方式
        category: 限定 arXiv 分类
        retries: 最大重试次数
        backoff: 初始退避秒数（每次翻倍）

    Returns:
        论文列表，每项包含 title/authors/abstract/arxiv_id/url/published
    """
    import logging
    logger = logging.getLogger(__name__)

    search_query = query
    if category:
        search_query = f"cat:{category} AND ({query})"

    params = urllib.parse.urlencode({
        "search_query": f"all:{search_query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": "descending",
    })

    url = f"{ARXIV_API}?{params}"
    wait = backoff
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-harness/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                xml_data = resp.read().decode("utf-8")
            return _parse_arxiv_response(xml_data)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning(f"[arxiv] 429 限流，{wait:.0f}s 后重试 (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                wait *= 2
            else:
                raise RuntimeError(f"arXiv API 请求失败: {e}") from e
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"[arxiv] 请求失败: {e}，{wait:.0f}s 后重试")
                time.sleep(wait)
                wait *= 2
            else:
                raise RuntimeError(f"arXiv API 请求失败: {e}") from e
    raise RuntimeError(f"arXiv API 在 {retries} 次重试后仍失败")


def fetch_paper(arxiv_id: str) -> dict:
    """获取单篇论文的详细信息，支持退避重试。"""
    import logging
    logger = logging.getLogger(__name__)
    params = urllib.parse.urlencode({
        "id_list": arxiv_id,
        "max_results": 1,
    })
    url = f"{ARXIV_API}?{params}"
    wait = 5.0
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-harness/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                xml_data = resp.read().decode("utf-8")
            results = _parse_arxiv_response(xml_data)
            return results[0] if results else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning(f"[arxiv] fetch_paper 429 限流，{wait:.0f}s 后重试")
                time.sleep(wait)
                wait *= 2
            else:
                logger.warning(f"[arxiv] fetch_paper 请求失败: {e}")
                return {}
        except Exception as e:
            logger.warning(f"[arxiv] fetch_paper 失败: {e}")
            return {}
    return {}


def _parse_arxiv_response(xml_data: str) -> list[dict]:
    import logging
    logger = logging.getLogger(__name__)
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        logger.warning(f"[arxiv] XML 解析错误: {e}")
        return []
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
