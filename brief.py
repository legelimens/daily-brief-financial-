#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TriBrief 主程序：抓取 RSS → 去重 → LLM 打分点评 → 渲染 HTML → 邮件推送。

命令行参数：
  --dry-run     用内置示例数据生成 HTML，不调用 LLM、不发邮件（仅预览界面）
  --no-email    正常跑（抓取+打分+渲染）但只生成 HTML 不发信
  --hours N     覆盖配置里的时间窗口（小时）
  --config PATH 指定配置文件，默认 config.yaml
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import feedparser
import requests
import yaml
from dateutil import parser as date_parser
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SCRIPT_DIR / "templates"
TEMPLATE_NAME = "briefing.html"

TRACK_ORDER = ["chip", "embodied", "data"]          # 赛道展示顺序
VALID_TRACKS = set(TRACK_ORDER)
VALID_TIERS = {"一手", "二手"}

BATCH_SIZE = 10                                       # 每批送 LLM 的条目上限（太小=多轮API调用，太大=分析变浅）
HTTP_TIMEOUT = 20                                     # RSS 抓取超时（秒）
LLM_TIMEOUT = 120                                     # LLM 调用超时（秒）
LLM_MAX_RETRIES = 3                                   # LLM 调用最大重试次数
LLM_RETRY_BACKOFF = 2.0                               # 重试退避基数（秒），实际等待 backoff^attempt
USER_AGENT = "Mozilla/5.0 (compatible; TriBrief/1.0; +https://github.com/)"
BEIJING_TZ = timezone(timedelta(hours=8))            # 北京时间，用于日期/主题

logger = logging.getLogger("tribrief")


# ==================================================================
# 数据结构
# ==================================================================
@dataclass
class Item:
    """一条标准化后的原始新闻。"""
    title: str
    url: str
    summary_raw: str
    published: Optional[datetime]
    source_name: str
    region: str            # 'cn' | 'global'
    track_hint: str         # 来自源配置的赛道提示
    no_date: bool = False   # 无发布时间的条目标记
    author: str = ""         # 论文作者/机构（arXiv 等学术源特别重要）


@dataclass
class ScoredItem:
    """经 LLM 打分点评后的新闻。"""
    title: str
    url: str
    summary_raw: str
    published: Optional[datetime]
    source_name: str
    region: str
    track_hint: str
    track: str              # 'chip' | 'embodied' | 'data' | 'drop'
    score: float            # 0-10
    summary_cn: str
    position_cn: str        # 行业定位 / 横向对比
    angle_cn: str
    tier: str               # '一手' | '二手'
    conflict_note: str = ""
    no_date: bool = False
    author: str = ""


@dataclass
class Briefing:
    """渲染所需的完整简报对象。"""
    date: str
    highlights: str
    focus_track: str
    groups: dict[str, list[ScoredItem]]
    counts: dict[str, int] = field(default_factory=dict)


# ==================================================================
# 嵌入的 LLM 提示词（严格按 spec 原样实现）
# ==================================================================
SCORE_SYSTEM_PROMPT = """\
你是一名专注于硬科技一级市场的投研分析助理,服务对象是一位做投资的资深从业者（合伙人级别）。你会收到一批条目（可能包含行业新闻和学术论文预印本）,请逐条分析并以严格 JSON 数组返回结果,不要输出任何解释性文字或 Markdown。

【三个赛道定义】
- chip(芯片/算力):AI 芯片(英伟达、AMD、华为昇腾、寒武纪、地平线等)、HBM、先进制程、Chiplet、EDA、半导体设备、出口管制与供应链政策。
- embodied(具身智能/机器人):人形机器人本体、灵巧手、运动控制、VLA 模型、机器人基础模型、核心零部件（电机/传感器/减速器）。
- data(数据):机器人/具身学习所需的数据采集、遥操作、仿真、真机/合成数据,以及 AI 基础设施与一级市场融资动态。

【任务】对每一条,输出以下字段:
- "id": 对应输入编号。
- "track": 该条目最匹配的赛道,取 "chip" / "embodied" / "data";若与三个赛道都无关,或属于纯营销稿、旧闻综述、与投资判断无关的科普,则取 "drop"。
- "score": 0-10 的重要性评分(可带一位小数)。
  * 行业新闻评分参考(从高到低):重大融资/并购/估值变化 ≈ 9-10;重要技术突破/流片/标志性新品 ≈ 7-9;政策/出口管制 ≈ 6-8;关键人事/战略 ≈ 5-7;一般动态 ≈ 3-5;边缘信息 < 3。
  * 学术论文评分参考:里程碑式突破(如新架构/新范式) ≈ 8-10;显著改进 SOTA 或开辟新方向 ≈ 6-8;增量贡献 ≈ 3-5;与三赛道无关的基础研究 < 3。
- "summary_cn": 中文摘要,客观陈述核心事实,不超过 80 字。必须包含关键数字（金额、估值、比例等）如果有的话。**摘要必须有主语**——对行业新闻,写清谁（公司/机构）做了什么;对学术论文,写清哪个团队/机构提出了什么,从输入的"作者/机构"字段获取,例如"斯坦福团队提出...""MIT 与 NVIDIA 联合发布..."而不能只写"提出""发布"。
- "position_cn": 1-2 句行业定位/横向对比（约 50-100 字）,回答"这家公司/技术/产业在行业里处于什么位置"。要求:
  * 点明其所处层级（本体、核心零部件、模型/数据、芯片/算力、基础设施、应用场景等）和成熟度（龙头、追赶者、细分冠军、早期验证、工具链补位等）。
  * 尽量给出可比对象:全球领先版本、国内对应版本、同类上市公司、一级市场同赛道公司或上下游替代方案。
  * 只基于输入信息和通用行业常识做稳健判断;如果信息不足,写清"可比对象有限"或"定位仍需更多披露验证",不要编造市场份额、客户或估值。
- "angle_cn": 2-3 句投资视角点评（约 60-120 字）,回答"这件事对投资意味着什么"。要求:
  * 必须联系具体的公司、标的、估值或赛道,不能泛泛而谈。
  * 融资金额类:横向对比同赛道其他公司的估值,判断估值水位是否合理;指出哪些已上市公司或一级标的可能受影响。
  * 技术突破类:指出谁会因此受益（产业链上下游）、谁会被替代或边缘化;估算商业化时间窗口。
  * 政策/管制类:量化影响范围（哪些公司、多大收入占比受影响）;指出受益方和受损方。
  * 禁止使用"值得关注""需持续跟踪""具有重要影响""或将对行业产生深远影响"等空话。如果不确定具体影响,如实说"信息不足以判断具体影响",不要凑字数。
- "tier": 信息源等级。公司官方公告/新闻稿、路透/彭博等一手媒体、arXiv 等预印本（属于作者一手发布）取 "一手";自媒体、二手转述、聚合稿取 "二手"。
- "conflict_note": 仅当该条涉及的关键数字(融资额、估值等)在摘要中出现明显不确定或多种口径时,用一句话说明;否则留空字符串 ""。

【准确性铁律】
- 不要编造任何信息。摘要和点评只能基于输入内容,信息不足时如实简略,绝不脑补"据传""可能"。
- 不要拔高评分;拿不准赛道归属时,优先判 "drop"。
- arXiv 论文的 tier 默认为 "一手"（作者直接发布）,除非摘要明确说是转述/综述他人工作。
- 输出必须是合法 JSON 数组,每个元素含上述全部字段,顺序与输入一致。"""

HIGHLIGHTS_SYSTEM_PROMPT = """\
你是投研简报的主编。下面是今天已入选的新闻条目。请输出一个 JSON 对象,包含:
- "highlights": 3 行以内的中文速览,点出今天最关键的 1-3 件事,简洁有力,不超过 120 字。
- "focus_track": 今天最值得关注的重点赛道,取 "chip" / "embodied" / "data"。
只输出 JSON,不要其他文字。"""


# ==================================================================
# 配置加载
# ==================================================================
def _load_dotenv(path: str = ".env") -> None:
    """手动加载 .env 文件（不引入 python-dotenv 依赖）。
    只处理简单的 KEY=VALUE 行，忽略注释和空行，不覆盖已有的环境变量。
    优先从脚本所在目录查找，其次从当前工作目录。"""
    dotenv_path = SCRIPT_DIR / path
    if not dotenv_path.is_file():
        dotenv_path = Path(path)
        if not dotenv_path.is_file():
            return
    loaded = 0
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    logger.info(".env 文件已加载（从 %s），共 %d 个变量", dotenv_path, loaded)


def _env_substitute(value: Any, extra_vars: Optional[dict[str, str]] = None) -> Any:
    """递归地把字符串中的 ${VAR} 替换为环境变量值（或 extra_vars 中的值）。
    extra_vars 用于注入 config 自身的顶层变量（如 rsshub_base），优先级低于环境变量。"""
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name in os.environ:
                return os.environ[name]
            if extra_vars and name in extra_vars:
                return extra_vars[name]
            logger.warning("配置引用了未设置的变量 ${%s}，保持原样", name)
            return m.group(0)
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)
    if isinstance(value, dict):
        return {k: _env_substitute(v, extra_vars) for k, v in value.items()}
    if isinstance(value, list):
        return [_env_substitute(v, extra_vars) for v in value]
    return value


# 顶层配置键中不属于"功能模块"的，可作为变量在配置内部引用
_CONFIG_VAR_KEYS = frozenset({"rsshub_base"})


def load_config(path: str) -> dict[str, Any]:
    """读取 YAML 配置并对其中的 ${VAR} 做环境变量替换。
    支持在 source URL 中使用 ${RSSHUB_BASE} 引用顶层 rsshub_base 值。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件 {path} 格式异常：顶层应为映射")
    # 收集顶层变量（非功能模块键），用于内部 ${VAR} 引用
    extra_vars = {k: str(raw[k]) for k in _CONFIG_VAR_KEYS if k in raw and isinstance(raw[k], str)}
    return _env_substitute(raw, extra_vars)


# ==================================================================
# 抓取
# ==================================================================
def _strip_html(text: str) -> str:
    """去掉 HTML 标签并反转义实体，折叠空白。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_published(entry: Any) -> Optional[datetime]:
    """从 feedparser 条目解析发布时间，统一为带时区的 UTC。"""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            # feedparser 已把 struct_time 规范到 UTC
            return datetime(*st[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        s = entry.get(key)
        if s:
            try:
                dt = date_parser.parse(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


def fetch_all(sources: dict[str, list[dict[str, str]]], hours: int) -> list[Item]:
    """遍历所有源抓取并标准化；只保留时间窗内的条目，单源失败跳过。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[Item] = []
    failed: list[str] = []
    total_sources = 0

    for track, src_list in (sources or {}).items():
        for src in src_list or []:
            total_sources += 1
            name = src.get("name", "未命名源")
            url = src.get("url", "")
            region = src.get("region", "global")
            try:
                resp = requests.get(url, timeout=HTTP_TIMEOUT,
                                    headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                if parsed.bozo and not parsed.entries:
                    raise ValueError(f"解析失败或为空：{getattr(parsed, 'bozo_exception', '')}")

                kept = 0
                for entry in parsed.entries:
                    title = (entry.get("title") or "").strip()
                    link = (entry.get("link") or "").strip()
                    if not title or not link:
                        continue
                    summary_raw = _strip_html(
                        entry.get("summary") or entry.get("description") or "")
                    published = _parse_published(entry)
                    no_date = published is None
                    # 提取作者信息（arXiv 等学术源特别重要）
                    author = ""
                    if entry.get("author"):
                        author = str(entry["author"]).strip()
                    elif entry.get("authors"):
                        authors = entry["authors"]
                        if isinstance(authors, list) and authors:
                            author = ", ".join(
                                str(a.get("name", a) if isinstance(a, dict) else a)
                                for a in authors[:3])
                    # 时间窗过滤：有时间且过旧的丢弃；无时间的保守保留并标记
                    if published is not None and published < cutoff:
                        continue
                    items.append(Item(
                        title=title, url=link, summary_raw=summary_raw,
                        published=published, source_name=name, region=region,
                        track_hint=track, no_date=no_date, author=author))
                    kept += 1
                logger.info("源 [%s] 抓取成功，窗口内 %d 条", name, kept)
            except Exception as exc:  # noqa: BLE001 单源失败不影响整体
                failed.append(name)
                logger.warning("源 [%s] 抓取失败，已跳过：%s", name, exc)

    logger.info("抓取完成：共 %d 个源，成功产出 %d 条；失败源 %d 个：%s",
                total_sources, len(items), len(failed),
                "、".join(failed) if failed else "无")
    return items


def test_sources(sources: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    """遍历所有 RSS 源并报告状态，不调用 LLM、不发信。返回结构化的测试报告。"""
    results: list[dict[str, Any]] = []
    ok = 0
    fail = 0
    empty = 0
    total_entries = 0

    print("\n" + "=" * 64)
    print("  TriBrief RSS 源连通性测试")
    print("=" * 64 + "\n")

    for track, src_list in (sources or {}).items():
        track_cn = {"chip": "芯片/算力", "embodied": "具身智能/机器人", "data": "数据"}.get(track, track)
        print(f"  [{track_cn}]")
        for src in src_list or []:
            name = src.get("name", "未命名源")
            url = src.get("url", "")
            status = "待测 "
            entries = 0
            err_msg = ""
            try:
                resp = requests.get(url, timeout=HTTP_TIMEOUT,
                                    headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                entries = len(parsed.entries)
                if entries == 0:
                    status = "空 "
                    empty += 1
                else:
                    status = "✓ "
                    ok += 1
                    total_entries += entries
            except Exception as exc:  # noqa: BLE001
                status = "✗ "
                err_msg = str(exc)[:120]
                fail += 1

            marker = {"✓ ": "\033[32m✓ \033[0m", "✗ ": "\033[31m✗ \033[0m",
                       "空 ": "\033[33m○ \033[0m", "待测 ": "…  "}.get(status, "…  ")
            print(f"    {marker} {name}")
            print(f"       {url}")
            if entries:
                print(f"       条目数: {entries}")
            if err_msg:
                print(f"       错误: {err_msg}")
            results.append({"track": track, "name": name, "url": url,
                            "ok": status == "✓ ", "entries": entries, "error": err_msg})
        print()

    summary = {
        "total_sources": ok + fail + empty,
        "ok": ok, "fail": fail, "empty": empty,
        "total_entries": total_entries,
        "details": results,
    }
    print(f"  汇总: {ok} 可用 / {empty} 空源 / {fail} 失败")
    print(f"  总条目数: {total_entries}")
    print()
    return summary


# ==================================================================
# 去重
# ==================================================================
def _normalize_url(url: str) -> str:
    """规范化 URL：去掉 query/fragment、去尾斜杠、统一小写。"""
    try:
        p = urlparse(url.strip())
        path = p.path.rstrip("/")
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", "")).lower()
    except ValueError:
        return url.strip().lower()


def _normalize_title(title: str) -> str:
    """标题归一化：去掉所有空白与标点，转小写（CJK 字符保留）。"""
    return re.sub(r"[\s\W_]+", "", title, flags=re.UNICODE).lower()


def _levenshtein_ratio(a: str, b: str) -> float:
    """计算两个字符串的 Levenshtein 相似度（0.0 ~ 1.0）。只用于短标题，不优化空间。"""
    if not a or not b:
        return 0.0
    len_a, len_b = len(a), len(b)
    # 长度差距过大直接返回 0，避免无意义的 O(n*m) 计算
    if max(len_a, len_b) == 0:
        return 1.0
    if min(len_a, len_b) / max(len_a, len_b) < 0.6:
        return 0.0
    # 滚动数组 DP
    prev = list(range(len_b + 1))
    curr = [0] * (len_b + 1)
    for i in range(1, len_a + 1):
        curr[0] = i
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    distance = prev[len_b]
    return 1.0 - distance / max(len_a, len_b)


def dedupe(items: list[Item]) -> list[Item]:
    """先按规范化 URL 去重，再按标题相似度合并同一事件，保留信息更全的一条。"""
    # 第一遍：URL 去重
    by_url: dict[str, Item] = {}
    url_ordered: list[Item] = []
    for it in items:
        key = _normalize_url(it.url)
        if key in by_url:
            existing = by_url[key]
            if len(it.summary_raw) > len(existing.summary_raw):
                url_ordered[url_ordered.index(existing)] = it
                by_url[key] = it
            continue
        by_url[key] = it
        url_ordered.append(it)

    # 第二遍：标题相似合并（精确匹配 → 包含关系 → 编辑距离）
    result: list[Item] = []
    kept_titles: list[tuple[str, Item]] = []
    for it in url_ordered:
        nt = _normalize_title(it.title)
        dup_at = -1
        for i, (kt, _kit) in enumerate(kept_titles):
            if not nt or not kt:
                continue
            # 完全相同
            if nt == kt:
                dup_at = i
                break
            # 包含关系（归一化后一方包含另一方，且最小长度 >= 6）
            if min(len(nt), len(kt)) >= 6 and (nt in kt or kt in nt):
                dup_at = i
                break
            # 编辑距离兜底：相似度 >= 0.85 且长度差距 <= 40%
            if _levenshtein_ratio(nt, kt) >= 0.85:
                dup_at = i
                break
        if dup_at >= 0:
            _kt, kept_item = kept_titles[dup_at]
            if len(it.summary_raw) > len(kept_item.summary_raw):
                result[result.index(kept_item)] = it
                kept_titles[dup_at] = (nt, it)
            continue
        result.append(it)
        kept_titles.append((nt, it))

    logger.info("去重完成：%d 条 → %d 条", len(items), len(result))
    return result


# ==================================================================
# LLM 调用
# ==================================================================
def _is_retryable(status_code: int) -> bool:
    """判断 HTTP 状态码是否值得重试（429 限流 / 5xx 服务端错误）。"""
    return status_code >= 500 or status_code == 429


def _chat_completion(config: dict[str, Any], messages: list[dict[str, str]],
                    max_tokens: int) -> Optional[str]:
    """调用 OpenAI 兼容 /chat/completions，返回 message.content 文本；失败自动重试后仍失败返回 None。"""
    ai = config["ai"]
    api_key = os.environ.get(ai["api_key_env"])
    if not api_key:
        logger.error("未设置环境变量 %s，无法调用 LLM", ai["api_key_env"])
        return None
    endpoint = ai["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": ai["model"],
        "messages": messages,
        "temperature": ai.get("temperature", 0.3),
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_exc: Optional[Exception] = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=LLM_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            # 非 200：判断是否值得重试
            if _is_retryable(resp.status_code):
                logger.warning("LLM 返回 %d，第 %d/%d 次尝试",
                               resp.status_code, attempt + 1, LLM_MAX_RETRIES)
            else:
                logger.error("LLM 返回 %d（不可重试），跳过：%s",
                             resp.status_code, resp.text[:300])
                return None
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning("LLM 网络异常（%s），第 %d/%d 次尝试",
                           exc.__class__.__name__, attempt + 1, LLM_MAX_RETRIES)
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 非网络异常不重试
            logger.error("LLM 调用失败（非网络错误）：%s", exc)
            return None

        # 指数退避：2s, 4s, 8s（上限 30s）
        if attempt < LLM_MAX_RETRIES - 1:
            wait = min(LLM_RETRY_BACKOFF ** (attempt + 1), 30.0)
            time.sleep(wait)

    if last_exc:
        logger.error("LLM 调用重试 %d 次后仍失败（最后异常：%s）", LLM_MAX_RETRIES, last_exc)
    return None


def _extract_json(text: str, expect: str) -> Any:
    """从模型输出中剥离代码块/多余文本后解析 JSON。expect 取 'array' 或 'object'。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    open_ch, close_ch = ("[", "]") if expect == "array" else ("{", "}")
    start, end = t.find(open_ch), t.rfind(close_ch)
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


def _build_score_user_message(batch: list[Item]) -> str:
    """把一批条目拼成 user message（编号 + 标题 + 来源 + 地区 + 日期 + 摘要），
    自动标注 arXiv 学术论文与普通新闻的区别。"""
    lines = []
    for idx, it in enumerate(batch, 1):
        pub = it.published.strftime("%Y-%m-%d") if it.published else "未知"
        region_cn = "国内" if it.region == "cn" else "国外"
        # 检测 arXiv 论文源
        is_arxiv = "arxiv" in it.source_name.lower() or "arxiv" in it.url.lower()
        kind_tag = "【学术论文/预印本】" if is_arxiv else ""
        # 拼合作者/机构信息（论文摘要通常不含作者，需要显式传入）
        author_line = f"\n     作者/机构：{it.author}" if it.author else ""
        lines.append(
            f"[{idx}] {kind_tag}标题：{it.title}\n"
            f"     来源：{it.source_name}（{region_cn}）｜发布：{pub}{author_line}\n"
            f"     原始摘要：{it.summary_raw[:400] or '（无摘要）'}")
    return "请分析以下条目（注意区分学术论文与行业新闻）：\n\n" + "\n\n".join(lines)


def score_and_enrich(items: list[Item], config: dict[str, Any]) -> list[ScoredItem]:
    """分批送 LLM 打分点评，解析结构化字段，丢弃 drop 与低分项。"""
    if not items:
        return []
    threshold = float(config["filtering"]["score_threshold"])
    scored: list[ScoredItem] = []

    for b in range(0, len(items), BATCH_SIZE):
        batch = items[b:b + BATCH_SIZE]
        messages = [
            {"role": "system", "content": SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_score_user_message(batch)},
        ]
        content = _chat_completion(config, messages, max_tokens=4800)
        if content is None:
            logger.warning("批次 %d 无返回，跳过该批 %d 条", b // BATCH_SIZE + 1, len(batch))
            continue
        try:
            arr = _extract_json(content, expect="array")
            if not isinstance(arr, list):
                raise ValueError("返回不是 JSON 数组")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("批次 %d JSON 解析失败，跳过：%s", b // BATCH_SIZE + 1, exc)
            continue

        by_id: dict[int, dict[str, Any]] = {}
        for obj in arr:
            if isinstance(obj, dict) and "id" in obj:
                try:
                    by_id[int(obj["id"])] = obj
                except (ValueError, TypeError):
                    continue

        for idx, it in enumerate(batch, 1):
            obj = by_id.get(idx)
            if not obj:
                continue
            track = str(obj.get("track", "drop")).strip().lower()
            if track not in VALID_TRACKS:
                continue  # drop 或非法赛道
            try:
                score = max(0.0, min(10.0, float(obj.get("score", 0))))
            except (ValueError, TypeError):
                score = 0.0
            if score < threshold:
                continue
            tier = str(obj.get("tier", "二手")).strip()
            if tier not in VALID_TIERS:
                tier = "二手"
            scored.append(ScoredItem(
                title=it.title, url=it.url, summary_raw=it.summary_raw,
                published=it.published, source_name=it.source_name,
                region=it.region, track_hint=it.track_hint, no_date=it.no_date,
                author=it.author,
                track=track, score=score,
                summary_cn=str(obj.get("summary_cn", "")).strip(),
                position_cn=str(obj.get("position_cn", "")).strip(),
                angle_cn=str(obj.get("angle_cn", "")).strip(),
                tier=tier,
                conflict_note=str(obj.get("conflict_note", "")).strip()))

    logger.info("打分完成：入选 %d 条（阈值 %.1f）", len(scored), threshold)
    return scored


# ==================================================================
# 组装简报
# ==================================================================
def _sort_key(s: ScoredItem) -> tuple[float, float]:
    """先按评分降序，再按发布时间降序（无时间排最后）。"""
    ts = s.published.timestamp() if s.published else 0.0
    return (-s.score, -ts)


def _fallback_highlights(groups: dict[str, list[ScoredItem]]) -> tuple[str, str]:
    """LLM 不可用时的兜底：拼接各赛道头条，重点赛道取最高分所在赛道。"""
    tops = [(t, lst[0]) for t, lst in groups.items() if lst]
    if not tops:
        return ("今日暂无入选动态。", TRACK_ORDER[0])
    tops.sort(key=lambda x: -x[1].score)
    focus = tops[0][0]
    line = "；".join(item.summary_cn or item.title for _t, item in tops[:3])
    return (f"今日要点（自动汇总）：{line}。", focus)


def build_briefing(scored: list[ScoredItem], config: dict[str, Any],
                  dry_run: bool = False) -> Briefing:
    """按赛道分组、排序、限量，并生成今日要点与重点赛道。"""
    max_per = int(config["filtering"]["max_per_track"])
    groups: dict[str, list[ScoredItem]] = {}
    counts: dict[str, int] = {}
    for track in TRACK_ORDER:
        lst = sorted([s for s in scored if s.track == track], key=_sort_key)[:max_per]
        groups[track] = lst
        counts[track] = len(lst)

    date_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    if dry_run:
        highlights = ("示例：今日具身赛道动作密集——Bear Robotics 收购 Kinisi 补强人形机器人数据；"
                      "Neura Robotics 完成 C 轮但估值口径存疑；芯片端国产具身芯片合资落地。")
        focus_track = "embodied"
    elif any(groups.values()):
        # 把入选条目（标题+赛道+评分）交给 LLM 生成速览
        lines = []
        for track in TRACK_ORDER:
            for s in groups[track]:
                lines.append(f"[{track}|{s.score:.1f}] {s.title}")
        messages = [
            {"role": "system", "content": HIGHLIGHTS_SYSTEM_PROMPT},
            {"role": "user", "content": "今日已入选条目：\n" + "\n".join(lines)},
        ]
        content = _chat_completion(config, messages, max_tokens=400)
        highlights, focus_track = "", ""
        if content:
            try:
                obj = _extract_json(content, expect="object")
                highlights = str(obj.get("highlights", "")).strip()
                focus_track = str(obj.get("focus_track", "")).strip().lower()
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("今日要点 JSON 解析失败，使用兜底：%s", exc)
        if not highlights or focus_track not in VALID_TRACKS:
            highlights, focus_track = _fallback_highlights(groups)
    else:
        highlights, focus_track = "今日暂无入选动态。", TRACK_ORDER[0]

    logger.info("简报组装完成：芯片 %d / 具身 %d / 数据 %d，重点赛道 %s",
                counts["chip"], counts["embodied"], counts["data"], focus_track)
    return Briefing(date=date_str, highlights=highlights, focus_track=focus_track,
                    groups=groups, counts=counts)


# ==================================================================
# 渲染
# ==================================================================
def render_html(briefing: Briefing, config: dict[str, Any]) -> str:
    """用 Jinja2 渲染模板，落盘到 output 目录并返回 HTML 字符串。"""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True, lstrip_blocks=True)
    template = env.get_template(TEMPLATE_NAME)
    generated_at = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    html = template.render(
        b=briefing,
        track_order=TRACK_ORDER,
        generated_at=generated_at,
        year=datetime.now(BEIJING_TZ).year)

    out_dir = Path(config.get("output", {}).get("dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"briefing_{briefing.date}.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("HTML 已生成：%s", out_path)
    return html


# ==================================================================
# 发信
# ==================================================================
def send_email(html: str, config: dict[str, Any], date_str: str | None = None) -> None:
    """用 smtplib 发送 HTML 邮件。失败记录日志并抛出，便于上层置非零退出码。"""
    ecfg = config["email"]
    if not ecfg.get("enabled", False):
        logger.info("email.enabled=false，跳过发信")
        return
    user = ecfg["smtp_user"]
    password = os.environ.get("SMTP_PASSWORD")
    if not password:
        raise RuntimeError("未设置环境变量 SMTP_PASSWORD，无法发信")
    recipients = ecfg.get("to", [])
    if not recipients:
        raise RuntimeError("config.email.to 为空，无收件人")

    date_str = date_str or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(f"投研简报 · {date_str}", "utf-8")
    msg["From"] = formataddr((str(Header(ecfg.get("from_name", "TriBrief"), "utf-8")), user))
    msg["To"] = ", ".join(recipients)

    host = ecfg["smtp_host"]
    port = int(ecfg["smtp_port"])
    try:
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            server.login(user, password)
            server.sendmail(user, recipients, msg.as_string())
        logger.info("邮件发送成功：收件人 %s", "、".join(recipients))
    except Exception as exc:  # noqa: BLE001 统一兜底并上抛
        logger.error("邮件发送失败：%s", exc)
        raise


# ==================================================================
# dry-run 内置示例数据（覆盖三赛道，含一条带 conflict_note）
# ==================================================================
def _sample_scored_items() -> list[ScoredItem]:
    now = datetime.now(timezone.utc)

    def ago(hours: int) -> datetime:
        return now - timedelta(hours=hours)

    return [
        ScoredItem(
            title="Bear Robotics 收购英国 Kinisi Robotics，补强 KR1 人形机器人与操作训练数据",
            url="https://www.therobotreport.com/", summary_raw="",
            published=ago(6), source_name="The Robot Report", region="global",
            track_hint="embodied", track="embodied", score=8.5,
            summary_cn="Bear Robotics 宣布收购英国 Kinisi Robotics，获得其 KR1 人形机器人及配套操作训练数据能力。",
            position_cn="Bear 原本偏商用服务机器人，Kinisi 补足人形本体与数据能力；对标 Figure、Agility，更像从场景运营向具身平台补位。",
            angle_cn="服务机器人公司向人形+数据栈延伸，数据资产成并购核心标的，提示具身赛道并购逻辑由本体转向数据闭环。",
            tier="一手", conflict_note=""),
        ScoredItem(
            title="Neura Robotics 完成 C 轮融资，亚马逊、英伟达参投",
            url="https://www.reuters.com/technology/", summary_raw="",
            published=ago(10), source_name="Reuters", region="global",
            track_hint="embodied", track="embodied", score=8.0,
            summary_cn="德国具身机器人公司 Neura Robotics 完成 C 轮融资，投资方包括亚马逊与英伟达。",
            position_cn="Neura 属于欧洲人形与协作机器人代表，资本阵容接近 Figure 的产业绑定路线；国内可对比智元、宇树等本体公司。",
            angle_cn="顶级产业资本入局欧洲人形机器人，强化英伟达具身生态卡位；估值口径需先核实再判断水位。",
            tier="一手",
            conflict_note="估值存在 70 亿美元与 40 亿欧元两种口径，差异显著，需以官方为准。"),
        ScoredItem(
            title="沐曦股份与优必选合资成立曦选创智，布局国产具身智能芯片",
            url="https://www.leiphone.com/", summary_raw="",
            published=ago(20), source_name="雷峰网", region="cn",
            track_hint="chip", track="chip", score=7.0,
            summary_cn="沐曦股份与优必选成立合资公司曦选创智，切入国产具身智能芯片。",
            position_cn="曦选创智处在具身算力芯片层，试图用国产 GPU 绑定机器人本体客户；全球参照是 NVIDIA Jetson/Isaac 生态。",
            angle_cn="国产 GPU 厂商绑定头部人形机器人客户，以场景换订单，利好国产算力在具身落地的确定性。",
            tier="二手", conflict_note=""),
        ScoredItem(
            title="美光 HBM4 或在英伟达 Vera Rubin 平台拿到更大供货份额",
            url="https://www.tomshardware.com/", summary_raw="",
            published=ago(28), source_name="Tom's Hardware", region="global",
            track_hint="chip", track="chip", score=6.5,
            summary_cn="有报道称美光 HBM4 可能在英伟达下一代 Vera Rubin 平台获得更高供货份额。",
            position_cn="美光处在 HBM 供应链追赶位置，领先者仍是 SK 海力士与三星；若份额提升，意味着 AI 存储不再是单一龙头格局。",
            angle_cn="HBM 供给格局向美光倾斜，影响 SK 海力士/三星份额预期，关注存储端在 AI 算力链的议价能力变化。",
            tier="二手", conflict_note=""),
        ScoredItem(
            title="国内具身智能 5 月融资环比降近六成，资本转向数据基础设施",
            url="https://36kr.com/", summary_raw="",
            published=ago(30), source_name="36氪", region="cn",
            track_hint="data", track="data", score=6.5,
            summary_cn="数据显示国内具身智能 5 月融资额环比下降近六成，资金更多流向数据采集与基础设施。",
            position_cn="数据采集与基础设施位于本体公司的上游工具层，国内对应遥操作、仿真和数据闭环服务商，成熟度低于芯片和本体融资主线。",
            angle_cn="一级市场由本体热转向数据层，提示具身投资进入冷静期，数据/遥操作/仿真类标的相对受青睐。",
            tier="二手", conflict_note=""),
        ScoredItem(
            title="开源遥操作数据集发布，降低具身学习真机数据采集门槛",
            url="https://www.marktechpost.com/", summary_raw="",
            published=ago(40), source_name="MarkTechPost", region="global",
            track_hint="data", track="data", score=6.2,
            summary_cn="新发布的开源遥操作数据集覆盖多种家庭操作任务，降低真机数据采集成本。",
            position_cn="开源遥操作数据集处在具身模型训练底层资产，商业公司可对比 Physical Intelligence、1X 等数据闭环路线。",
            angle_cn="数据采集成本下降利好长尾具身创业者，但也压缩纯数据采集/标注类公司的稀缺性溢价。",
            tier="二手", conflict_note=""),
    ]


# ==================================================================
# 主流程
# ==================================================================
def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                            datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # Windows 控制台中文兜底
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="TriBrief 三赛道投研简报")
    parser.add_argument("--dry-run", action="store_true",
                        help="用内置示例数据生成 HTML，不调 LLM、不发信")
    parser.add_argument("--no-email", action="store_true",
                        help="正常跑但只生成 HTML 不发信")
    parser.add_argument("--hours", type=int, default=None,
                        help="覆盖时间窗口（小时）")
    parser.add_argument("--test-sources", action="store_true",
                        help="仅测试所有 RSS 源连通性和条目数，不调用 LLM、不发信")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.yaml"),
                        help="配置文件路径，默认 config.yaml")
    args = parser.parse_args()

    _configure_logging()
    _load_dotenv()   # 自动加载 .env，不覆盖已有环境变量
    logger.info("TriBrief 启动（dry_run=%s, no_email=%s）", args.dry_run, args.no_email)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.error("读取配置失败：%s", exc)
        return 1

    hours = args.hours if args.hours is not None else int(config["filtering"]["time_window_hours"])

    # —— test-sources：仅测试源连通性 ——
    if args.test_sources:
        logger.info("test-sources 模式：仅测试 RSS 源连通性")
        test_sources(config.get("sources", {}))
        return 0

    # —— dry-run：示例数据直达渲染 ——
    if args.dry_run:
        logger.info("dry-run 模式：使用内置示例数据")
        briefing = build_briefing(_sample_scored_items(), config, dry_run=True)
        render_html(briefing, config)
        logger.info("dry-run 完成，未调用 LLM、未发信")
        return 0

    # —— 正常流程 ——
    items = fetch_all(config.get("sources", {}), hours)
    deduped = dedupe(items)
    scored = score_and_enrich(deduped, config)
    if deduped and not scored:
        logger.error("已抓取 %d 条新闻，但没有任何条目完成分析；停止生成和发信", len(deduped))
        return 1
    briefing = build_briefing(scored, config, dry_run=False)
    html = render_html(briefing, config)

    if args.no_email:
        logger.info("--no-email：仅生成 HTML，不发信")
        return 0

    try:
        send_email(html, config, briefing.date)
    except Exception:  # noqa: BLE001 已在 send_email 内记录
        logger.error("发信环节失败，HTML 已落盘，置非零退出码")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
