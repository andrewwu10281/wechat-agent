"""查学者学术信息工具：用 Semantic Scholar 官方 API（免注册、免代理、国内可直连）。

能查：某学者的总引用数、h 指数、论文数、单位，以及高被引论文列表。
（替代之前抓 Google Scholar 页面的方案——Google 对代理出口 IP 反爬太严，Semantic Scholar 更稳。）
"""
import re
import time

import requests

import config

BASE = "https://api.semanticscholar.org/graph/v1"

# 数据里可能混入全角空格等特殊空白，Windows GBK 控制台打不出来，统一换成普通空格
_WS = re.compile(r"[ -​  ]")


def _clean(s):
    return _WS.sub(" ", str(s)).strip() if s is not None else s
HEADERS = {"User-Agent": "wechat-agent/1.0 (course project)"}

AUTHOR_FIELDS = "name,affiliations,paperCount,citationCount,hIndex,url"
PAPER_FIELDS = "title,year,citationCount,venue"


def _get(path, params=None):
    url = BASE + path
    for attempt in range(3):
        r = requests.get(url, params=params, headers=HEADERS, timeout=25)
        # 限流 / 服务器瞬时错误：退避重试（免费额度偶尔会 429）
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            wait = float(r.headers.get("Retry-After", 0) or 0)
            time.sleep(min(wait if wait else 2 ** attempt, 8))
            continue
        break
    r.raise_for_status()
    return r.json()


def _affiliations(a) -> str:
    affs = a.get("affiliations") or []
    if not affs:
        return "未填写"
    return ", ".join(str(x) for x in affs)


def _fmt_profile(a, papers=None) -> str:
    lines = [
        f"学者：{a.get('name', '未知')}",
        f"单位：{_affiliations(a)}",
        f"指标：总引用 {a.get('citationCount') or 0}，h指数 {a.get('hIndex') or 0}，"
        f"论文 {a.get('paperCount') or 0} 篇",
    ]
    url = a.get("url")
    if url:
        lines.append(f"主页：{url}")
    if papers:
        lines.append("高被引论文（前10）：")
        for p in papers:
            lines.append(f"  {p.get('title', '')}（{p.get('year') or '未知年份'}，"
                         f"被引 {p.get('citationCount') or 0}）")
    return _clean("\n".join(lines))


def _profile(sid: str) -> str:
    a = _get(f"/author/{sid}", {"fields": AUTHOR_FIELDS})
    papers = _get(f"/author/{sid}/papers", {"fields": PAPER_FIELDS, "limit": 10})
    return _fmt_profile(a, (papers or {}).get("data") or [])


def search_author(name: str) -> str:
    data = _get("/author/search", {"query": name, "fields": AUTHOR_FIELDS, "limit": 5})
    authors = (data or {}).get("data") or []
    if not authors:
        return f"没搜到「{name}」，换更精确的名字试试。"

    top = authors[0]
    lines = [_profile(top["authorId"])]

    if len(authors) > 1:
        lines.append("")
        lines.append("其他同名/相近学者（把 ID 填进 .env 的 SCHOLAR_ID 可查对应详情）：")
        for i, a in enumerate(authors[1:], start=2):
            lines.append(f"  {i}. {a.get('name')}（{_affiliations(a)}）"
                         f"被引 {a.get('citationCount') or 0}，h指数 {a.get('hIndex') or 0}，"
                         f"ID: {a.get('authorId')}")
    return _clean("\n".join(lines))


def get_scholar_info(scholar_id: str = "", name: str = "") -> str:
    """查学者信息。有 scholar_id 就查主页详情；否则按姓名搜索。"""
    sid = (scholar_id or "").strip() or config.SCHOLAR_ID
    if sid:
        try:
            return _profile(sid)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return f"没找到 ID 为 {sid} 的学者，请确认 ID 正确。"
            return f"查询失败：{e}"
        except Exception as e:
            return f"查询失败（检查网络）：{e}"

    if not (name or "").strip():
        return ("错误：需要提供 scholar_id 或姓名。请先在 .env 的 SCHOLAR_ID 里填你的 "
                "Semantic Scholar 作者 ID（作者搜索结果里会显示 ID）。")
    try:
        return search_author(name)
    except Exception as e:
        return f"搜索失败（检查网络）：{e}"
