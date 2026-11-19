#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""连接客户端托管的 Chrome CDP 会话，打开页面并转发百应响应给桌面端。"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import mimetypes
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    import jieba.posseg as jieba_posseg
except ImportError:
    jieba_posseg = None

try:
    from selection_settings import DEFAULT_BANNED_WORDS, SUPERSTITION_BANNED_WORDS
except ImportError:  # 支持以 engine.browser_cdp 形式从项目根目录导入
    from .selection_settings import DEFAULT_BANNED_WORDS, SUPERSTITION_BANNED_WORDS

try:
    from image_to_video import build_product_cover, resolve_ffmpeg
except ImportError:  # 支持以 engine.browser_cdp 形式从项目根目录导入
    from .image_to_video import build_product_cover, resolve_ffmpeg

CAPTURE_MARKERS = (
    "pack_detail",
    "selection_square",
    "search/query/recommend",
    "material_list",
    "channel_activity",
    "selection/common",
    "category",
    "cate_info",
    "cate_list",
    "cate_tree",
    "cat_list",
)
ALLOWED_OPEN_HOSTS = (
    "douyin.com",
    "jinritemai.com",
    "douyinec.com",
    "oceanengine.com",
    "xiaohongshu.com",
    "xhslink.com",
    "toutiao.com",
    "ixigua.com",
)
DOUYIN_CREATOR_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
DOUYIN_CREATOR_POST_PATH = "/creator-micro/content/post/video"
BAIYING_SELECTION_CART_URL = "https://buyin.jinritemai.com/dashboard/merch-picking-cart"
BAIYING_SELECTION_LIBRARY_URL = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
BAIYING_LOGIN_CHECK_URL = "https://buyin.jinritemai.com/dashboard/merch-picking-library"
SEARCHBOX_RECOVERY_RETRIES = 2
SEARCHBOX_READY_TIMEOUT_MS = 10_000
DOUYIN_CREATOR_LOGIN_CHECK_URL = DOUYIN_CREATOR_UPLOAD_URL
BAIYING_WORKBENCH_SHELL_MARKERS = ("选品广场", "选品中心", "选品AI助手", "同行跟进", "热点趋势", "联盟榜单")
BAIYING_CART_ROW_SELECTORS = (
    "tbody tr",
    '[role="row"]',
    '[class*="aurora-table-row"]',
    '[class*="__row___"]',
    '[class*="productRow"]',
)
_CREATOR_SHORT_TITLE_STOPWORDS = frozenset({
    "正宗", "新鲜", "包邮", "现货", "旗舰店", "官方", "厂家", "直销", "爆款", "热卖",
    "优惠", "特价", "促销", "秒杀", "推荐", "精选", "优选", "商品", "产品",
    "每单", "售价", "直播", "全国", "发货", "下单", "拍下", "限时", "活动",
})
_CREATOR_SHORT_TITLE_ALIAS_GROUPS = (
    frozenset({"猕猴桃", "奇异果"}),
)
BAIYING_ROW_METADATA_SCRIPT = r"""(element, expected) => {
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const id = String(expected?.productId || '');
  const title = normalize(expected?.productTitle || '');
  const nodes = [element, ...Array.from(element.querySelectorAll('*')).slice(0, 120)];
  const identityHints = [];
  const domIdentityHints = [];
  const reactIdentityHints = [];
  const reactStack = [];
  const seen = new Set();
  let propsHasId = false;
  let visited = 0;
  const isProductIdKey = (key) => /^(product|goods|item)[_-]?id$/i.test(String(key || '').replace(/^data-/i, ''));
  const addHint = (target, hint) => {
    if (!target.includes(hint) && target.length < 8) target.push(hint);
    if (!identityHints.includes(hint) && identityHints.length < 8) identityHints.push(hint);
  };
  for (const node of nodes) {
    for (const attribute of Array.from(node.attributes || [])) {
      const key = attribute.name.toLowerCase();
      const value = String(attribute.value || '').trim();
      if (value && isProductIdKey(key)) {
        addHint(domIdentityHints, `${attribute.name}=${value.slice(0, 48)}`);
        if (id && value === id) propsHasId = true;
      }
    }
    const propsKey = Object.keys(node).find((key) => key.startsWith('__reactProps$'));
    if (propsKey && node[propsKey] && typeof node[propsKey] === 'object') reactStack.push(node[propsKey]);
  }
  while (reactStack.length && visited < 350) {
    const value = reactStack.pop();
    if (!value || typeof value !== 'object' || seen.has(value)) continue;
    seen.add(value);
    visited += 1;
    if (Array.isArray(value)) {
      for (const child of value) if (child && typeof child === 'object') reactStack.push(child);
      continue;
    }
    for (const [key, child] of Object.entries(value)) {
      if (isProductIdKey(key) && (typeof child === 'string' || typeof child === 'number')) {
        const hint = `${key}=${String(child).slice(0, 48)}`;
        addHint(reactIdentityHints, hint);
        if (id && String(child) === id) propsHasId = true;
      }
      if (child && typeof child === 'object') reactStack.push(child);
    }
  }
  const rowText = normalize(element.innerText || element.textContent || '');
  const unavailablePattern = /暂无库存|无库存|库存不足|已售罄|售罄|缺货|已下架|暂不可带货|暂不可带|不可带货|不可加车|暂不可添加/;
  const statusHints = [];
  const addStatusHint = (value) => {
    const text = normalize(value);
    if (text && unavailablePattern.test(text) && !statusHints.includes(text)) {
      statusHints.push(text.slice(0, 100));
    }
  };
  for (const node of nodes) {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    addStatusHint(node.innerText || node.textContent || '');
    for (const attribute of ['aria-label', 'aria-description', 'title', 'alt', 'data-tooltip', 'data-status', 'data-state']) {
      addStatusHint(node.getAttribute?.(attribute));
    }
    for (const pseudo of ['::before', '::after']) {
      const content = getComputedStyle(node, pseudo).content;
      if (content && content !== 'none' && content !== 'normal') addStatusHint(content.replace(/^['\"]|['\"]$/g, ''));
    }
  }
  const compactTitle = title.replace(/\s+/g, '');
  const titlePrefixLength = Math.min(24, Math.max(14, Math.ceil(compactTitle.length * 0.55)));
  const matchesTitleText = (value) => {
    const candidate = normalize(value);
    if (!candidate || !title) return false;
    if (candidate === title || candidate.includes(title)) return true;
    const compactCandidate = candidate.replace(/\s+/g, '');
    if (compactTitle.length < titlePrefixLength) return false;
    if (compactCandidate.startsWith(compactTitle.slice(0, titlePrefixLength))) return true;
    const truncation = candidate.search(/[.…⋯]|\.{3}/);
    if (truncation < 0) return false;
    const visiblePrefix = candidate.slice(0, truncation).replace(/\s+/g, '');
    return visiblePrefix.length >= titlePrefixLength && compactTitle.startsWith(visiblePrefix);
  };
  return {
    matchesProductId: Boolean(id && propsHasId),
    matchesProductTitle: Boolean(title && nodes.some((node) =>
      matchesTitleText(node.getAttribute?.('title')) ||
      matchesTitleText(node.getAttribute?.('aria-label')) ||
      matchesTitleText(node.innerText || node.textContent || ''))),
    rowKey: element.getAttribute('data-row-key') || element.getAttribute('data-id') || '',
    rowText: rowText.slice(0, 180),
    statusHints,
    identityHints,
    domIdentityHints,
    reactIdentityHints,
    descendantCount: Math.max(0, nodes.length - 1),
    reactObjectsVisited: visited,
  };
}"""
BAIYING_PRIORITY_SOURCES = {
    "hotSpot": ("选品热点", "https://buyin.jinritemai.com/dashboard/merch-picking-utils/hot-spot"),
    "allianceRank": ("带货商品榜", "https://buyin.jinritemai.com/dashboard/merch-picking-hall/rank"),
}
BAIYING_SELECTION_SQUARE_SOURCE = (
    "选品广场", "https://buyin.jinritemai.com/dashboard/merch-picking-library"
)
BAIYING_PRIORITY_ROOT_SELECTOR = (
    '[class*="__card___"], [class*="__row___"], [class*="productRow"], '
    '[class*="goodsList"], tbody tr, [role="row"], [class~="kol-product-card"]'
)
BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR = (
    '[class*="__card___"], [class*="__row___"], tbody tr, [role="row"], '
    '[class~="kol-product-card"]'
)
BAIYING_PRIORITY_LIST_CONTAINER_SELECTOR = (
    '[role="list"], [role="grid"], [role="rowgroup"], ul, tbody, '
    '[class*="list"], [class*="List"], [class*="goods"], [class*="Goods"], '
    '[class*="product"], [class*="Product"], [class*="rank"], [class*="Rank"], '
    '[class*="table"], [class*="Table"]'
)
BAIYING_PRIORITY_SOURCE_LIST_SELECTORS = {
    "hotSpot": '[class*="hotSpotList"] [class*="infinite-scroller"]',
    "allianceRank": BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR,
}
BAIYING_PRIORITY_ACTIVE_LOADING_SELECTOR = (
    '.auxo-spin-spinning, .aurora-spin-spinning, [aria-busy="true"], '
    '[data-loading="true"], [class*="is-loading"], [class*="loading-active"]'
)
BAIYING_PRIORITY_CONTENT_ROOT_SELECTOR = (
    'main, [role="main"], .content-container, [class*="content-layout-has-nav"]'
)
BAIYING_PRIORITY_EMPTY_PATTERN = r"暂无商品|暂无相关商品|暂无数据|没有相关商品|未找到商品|当前没有商品|暂时没有商品"
BAIYING_PRIORITY_READY_TIMEOUT_MS = 60_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SELECTION_PAGE_READY_TIMEOUT_MS = 60_000
CATEGORY_PAGE_POLLING_MS = 250
CATEGORY_PATH_SEPARATOR = "__RC_CASCADER_SPLIT__"
SEARCH_KEYWORD_INPUT_SELECTOR = ".searchInput__input input[role='combobox']"
SEARCH_KEYWORD_CLEAR_SELECTOR = ".searchInput__input [aria-label='close-circle']"
ACTIVE_CATEGORY_DROPDOWN_SELECTOR = (
    '.aurora-cascader-dropdown:visible:not([class*="aurora-slide-up-leave"]):not(.aurora-select-dropdown-hidden)'
)
MATERIAL_LIST_CACHE_TTL_SECONDS = 15 * 60
MATERIAL_LIST_CACHE_MAX_ENTRIES = 256
BAIYING_RATE_LIMIT_COOLDOWN_SECONDS = 60
DETAIL_ANALYSIS_TIMEOUT_SECONDS = 45
DETAIL_CATEGORY_READ_TIMEOUT_SECONDS = 12
DETAIL_ANALYSIS_INTERVAL_MS = 1_000
DETAIL_PAYLOAD_CACHE_TTL_SECONDS = 10 * 60
_OUTPUT_LOCK = threading.Lock()
_RATE_LIMIT_LOCK = threading.Lock()
_MATERIAL_LIST_CACHE: Dict[tuple[int, str], tuple[float, Any]] = {}
_DETAIL_PAYLOAD_CACHE: Dict[tuple[int, str], tuple[float, Any]] = {}
_PAGE_LIFECYCLE_STATS: Dict[int, Dict[str, Any]] = {}


def _page_lifecycle_stats(context: BrowserContext) -> Dict[str, Any]:
    return _PAGE_LIFECYCLE_STATS.setdefault(id(context), {
        "browserStarts": 0,
        "newPages": 0,
        "reusedPages": 0,
        "navigations": 0,
        "stageDurationsMs": {},
    })


def _tracked_new_page(context: BrowserContext, purpose: str) -> Page:
    stats = _page_lifecycle_stats(context)
    stats["newPages"] = int(stats.get("newPages", 0)) + 1
    by_stage = stats.setdefault("newPagesByPurpose", {})
    by_stage[purpose] = int(by_stage.get(purpose, 0)) + 1
    return context.new_page()


def _track_reused_page(context: BrowserContext, purpose: str) -> None:
    stats = _page_lifecycle_stats(context)
    stats["reusedPages"] = int(stats.get("reusedPages", 0)) + 1
    by_stage = stats.setdefault("reusedPagesByPurpose", {})
    by_stage[purpose] = int(by_stage.get(purpose, 0)) + 1


def _record_page_navigation(context: BrowserContext, purpose: str, elapsed_ms: float) -> None:
    stats = _page_lifecycle_stats(context)
    stats["navigations"] = int(stats.get("navigations", 0)) + 1
    durations = stats.setdefault("stageDurationsMs", {})
    durations[purpose] = round(float(durations.get(purpose, 0)) + max(0.0, elapsed_ms), 1)


def page_lifecycle_stats(context: BrowserContext) -> Dict[str, Any]:
    return json.loads(json.dumps(_page_lifecycle_stats(context), ensure_ascii=False))
_PAGE_RATE_LIMIT_UNTIL: Dict[int, float] = {}
_RATE_LIMIT_STATE_PATH: Optional[Path] = None
_PROFILE_RATE_LIMIT_UNTIL = 0.0


class RecoverablePrioritySourceReadError(RuntimeError):
    """来源页未能读到列表结构；按本轮候选策略安全跳过该来源。"""


class CandidateDetailClickBlockedError(RuntimeError):
    """商品身份已确认，但当前商品详情入口不可安全点击；可复核原行后跳过单品。"""


class CandidateSourceRefreshSkip(RuntimeError):
    """候选在来源类目刷新后消失或不再符合筛选；可安全跳过单品。"""


class CandidateCartSkip(RuntimeError):
    """百应行证据已确认当前商品不可加车；记录单品跳过并继续其他候选。"""

    def __init__(self, product_id: str, reason: str) -> None:
        self.product_id = str(product_id or "").strip()
        self.reason = " ".join(str(reason or "").split())
        super().__init__(f"商品 ID {self.product_id or '未知'}：{self.reason}")


class WrongBaiyingProductRow(CandidateCartSkip):
    """百应当前可见结果行的商品 ID 明确与目标 ID 不同。"""


class PublishVerificationIsolated(RuntimeError):
    """发布核验对错行有限重查耗尽；只隔离当前任务，不上传视频。"""

    classification = "PUBLISH_VERIFY_OTHER_ROW_ISOLATED"

    def __init__(self, product_id: str, retry_count: int, reason: str) -> None:
        self.product_id = str(product_id or "").strip()
        self.retry_count = max(0, int(retry_count))
        self.reason = " ".join(str(reason or "商品核验未通过").split())
        self.warning = (
            f"商品 ID {self.product_id or '未知'} 已追加重查 {self.retry_count} 次，仍未能确认目标商品行；"
            "此前已观察到其他商品 ID 行，"
            f"本发布任务已隔离；未上传视频、未关联商品、未发布。原因：{self.reason}"
        )
        super().__init__(self.warning)


BAIYING_PUBLISH_VERIFY_RECHECKS = 2


_CANDIDATE_UNAVAILABLE_MARKERS = (
    "暂无库存", "无库存", "库存不足", "已售罄", "售罄", "缺货", "已下架",
    "暂不可带货", "暂不可带", "不可带货", "不可加车", "暂不可添加",
)


def _candidate_unavailable_reason(value: Any) -> str:
    text = " ".join(str(value or "").split())
    compact = text.replace(" ", "").lower()
    return next((marker for marker in _CANDIDATE_UNAVAILABLE_MARKERS if marker.lower() in compact), "")


def _baiying_row_unavailable_reason(row: Dict[str, Any]) -> str:
    values = [row.get("rowText"), row.get("text"), row.get("accessibleText"), row.get("statusText")]
    hints = row.get("statusHints")
    if isinstance(hints, str):
        values.append(hints)
    elif isinstance(hints, (list, tuple)):
        values.extend(hints)
    return next((reason for value in values if (reason := _candidate_unavailable_reason(value))), "")


def _row_identity_product_ids(identity_hints: Any) -> set[str]:
    if not isinstance(identity_hints, (list, tuple)):
        return set()
    product_ids: set[str] = set()
    for hint in identity_hints:
        match = re.match(
            r"\s*(?:data-)?(?:product|goods|item)[_-]?id\s*=\s*([^\s,;]+)",
            str(hint or ""),
            re.IGNORECASE,
        )
        if match:
            product_ids.add(match.group(1).strip().strip("'\""))
    return product_ids


def _baiying_row_skip_reason(product_id: str, rows: Sequence[Dict[str, Any]]) -> str:
    """只在可见行 DOM 身份明确冲突或明确无库存时返回业务跳过原因。"""
    visible_rows = [row for row in rows if isinstance(row, dict)]
    if not visible_rows:
        return ""
    id_matches = [row for row in visible_rows if row.get("matchesProductId") is True]
    title_matches = [row for row in visible_rows if row.get("matchesProductTitle") is True]
    unavailable = [_baiying_row_unavailable_reason(row) for row in visible_rows]
    if id_matches:
        if all(unavailable[index] for index, row in enumerate(visible_rows) if row.get("matchesProductId") is True):
            markers = sorted({unavailable[index] for index, row in enumerate(visible_rows) if row.get("matchesProductId") is True})
            return "目标商品行明确显示不可加车状态：" + "、".join(markers)
        return ""
    row_ids = [
        _row_identity_product_ids(
            row.get("domIdentityHints") or row.get("identityHints")
        )
        for row in visible_rows
    ]
    expected = str(product_id or "").strip()
    if title_matches:
        title_row_ids = [
            _row_identity_product_ids(
                row.get("domIdentityHints") or row.get("identityHints")
            )
            for row in title_matches
        ]
        if title_row_ids and all(ids and expected not in ids for ids in title_row_ids):
            other_ids = sorted({value for ids in title_row_ids for value in ids})
            return "标题匹配行的 DOM 商品 ID 与目标 ID 不符（可见商品 ID：" + "、".join(other_ids[:5]) + "）"
    elif row_ids and all(ids and expected not in ids for ids in row_ids):
        other_ids = sorted({value for ids in row_ids for value in ids})
        return "按商品 ID 查询后只看到其他商品行（可见商品 ID：" + "、".join(other_ids[:5]) + "）"
    if not id_matches and not title_matches and len(visible_rows) == 1 and unavailable[0]:
        return f"核验行不可识别·唯一可见行明确显示无库存/不可加车：{unavailable[0]}"
    if title_matches:
        title_indexes = [index for index, row in enumerate(visible_rows) if row.get("matchesProductTitle") is True]
        if title_indexes and all(unavailable[index] for index in title_indexes):
            markers = sorted({unavailable[index] for index in title_indexes})
            return "标题匹配的目标商品行明确显示不可加车状态：" + "、".join(markers)
    return ""


def _baiying_row_skip_exception(product_id: str, reason: str) -> CandidateCartSkip:
    """把有明确错 ID 证据的行冲突与无库存等业务跳过区分开。"""
    if "只看到其他商品行" in reason or "与目标 ID 不符" in reason or "商品 ID 为 " in reason:
        return WrongBaiyingProductRow(product_id, reason)
    return CandidateCartSkip(product_id, reason)


def _candidate_card_skip_reason(product_id: str, cards: Sequence[Any], title_matched: bool = False) -> str:
    row_samples: list[Dict[str, Any]] = []
    for card in cards:
        try:
            row_text = " ".join(str(card.inner_text() or "").split())[:300]
        except Exception:
            row_text = ""
        try:
            card_ids = _candidate_card_product_ids(card)
            identity_hints = [f"productId={value}" for value in card_ids]
        except Exception:
            card_ids = []
            identity_hints = []
        row_samples.append({
            "rowText": row_text,
            "identityHints": identity_hints,
            "matchesProductId": product_id in card_ids,
            "matchesProductTitle": title_matched,
        })
    return _baiying_row_skip_reason(product_id, row_samples)


def _candidate_skip_result(candidate: Dict[str, Any], reason: Any, cart_attempted: bool = False) -> Dict[str, Any]:
    product_id = str(candidate.get("productId") or "").strip()
    reason_text = " ".join(str(reason or "该商品当前不可加入选品车").split())
    if product_id and product_id not in reason_text:
        reason_text = f"商品 ID {product_id}：{reason_text}"
    unavailable = _candidate_unavailable_reason(reason_text)
    skip_kind = f"{unavailable}，已跳过" if unavailable else "可见行属于其他商品或无法唯一确认，已跳过"
    return {
        "productId": product_id,
        "title": str(candidate.get("title") or ""),
        "cartAdded": False,
        "cartAttempted": bool(cart_attempted),
        "skipped": True,
        "analysisRejected": True,
        "thresholdReasons": [reason_text],
        "analysisReasons": [reason_text],
        "cartStatus": skip_kind,
        "status": reason_text + "；已跳过此候选，继续下一候选。",
    }


def _get_cached_material_list(page_key: int, signature: str) -> Optional[Any]:
    """短时复用已成功的筛选结果；加车阶段仍会按商品 ID 强制回百应复核。"""
    key = (page_key, signature)
    cached = _MATERIAL_LIST_CACHE.get(key)
    if cached is None:
        return None
    created_at, payload = cached
    if time.monotonic() - created_at > MATERIAL_LIST_CACHE_TTL_SECONDS:
        _MATERIAL_LIST_CACHE.pop(key, None)
        return None
    return payload


def _store_cached_material_list(page_key: int, signature: str, payload: Any) -> None:
    now = time.monotonic()
    expired = [
        key for key, (created_at, _payload) in _MATERIAL_LIST_CACHE.items()
        if now - created_at > MATERIAL_LIST_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _MATERIAL_LIST_CACHE.pop(key, None)
    _MATERIAL_LIST_CACHE[(page_key, signature)] = (now, payload)
    while len(_MATERIAL_LIST_CACHE) > MATERIAL_LIST_CACHE_MAX_ENTRIES:
        oldest = min(_MATERIAL_LIST_CACHE, key=lambda key: _MATERIAL_LIST_CACHE[key][0])
        _MATERIAL_LIST_CACHE.pop(oldest, None)


def _clear_page_material_cache(page_key: int) -> None:
    for key in [key for key in _MATERIAL_LIST_CACHE if key[0] == page_key]:
        _MATERIAL_LIST_CACHE.pop(key, None)


def _material_list_response_diagnostics(response: Any, result: Any) -> str:
    """只保留定位响应所需的白名单字段，不读取 Cookie、请求参数或完整 URL。"""
    try:
        status: Any = getattr(response, "status", None)
    except Exception:
        status = None
    if callable(status):
        try:
            status = status()
        except Exception:
            status = None
    status_text = str(status) if isinstance(status, int) and 100 <= status <= 599 else "未知"

    response_message = result.get("msg") if isinstance(result, dict) else None
    if response_message is None:
        response_message = "未提供"
    response_message = re.sub(r"[\r\n\t\x00-\x1f]+", " ", str(response_message)).strip()
    response_message = re.sub(
        r"(?i)(https?://[^\s?#]+)\?[^\s#]*",
        r"\1?[参数已隐藏]",
        response_message,
    )
    response_message = re.sub(
        r"(?i)(cookie|set-cookie)\s*[:=]\s*[^\s,;]+",
        r"\1=[已隐藏]",
        response_message,
    )[:160]
    response_message = re.sub(
        r"(?i)\b(search_text|search_id|keyword|token|access_token|authorization|signature|sign)\s*[:=]\s*[^\s,;&]+",
        r"\1=[已隐藏]",
        response_message,
    )

    headers: Any = None
    try:
        all_headers = getattr(response, "all_headers", None)
    except Exception:
        all_headers = None
    if callable(all_headers):
        try:
            headers = all_headers()
        except Exception:
            headers = None
    if not isinstance(headers, dict):
        try:
            headers = getattr(response, "headers", None)
        except Exception:
            headers = None
    if not isinstance(headers, dict):
        headers = {}
    headers = {str(name).lower(): value for name, value in headers.items()}
    trace_names = (
        "x-tt-logid", "x-tt-log-id", "x-request-id", "x-requestid",
        "request-id", "x-b3-traceid", "x-tt-trace-id", "trace-id",
    )
    trace_id = next((headers.get(name) for name in trace_names if headers.get(name)), None)
    if trace_id is not None:
        trace_id = str(trace_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", trace_id):
            trace_id = "存在但格式不符合安全记录规则"
    else:
        trace_id = "未提供"

    code = result.get("code", "未知") if isinstance(result, dict) else "未知"
    code_text = str(code)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,40}", code_text):
        code_text = "非标准值已过滤"
    return (
        f"HTTP {status_text}；响应业务码 {code_text}；"
        f"百应响应说明：{response_message}；请求追踪号：{trace_id}"
    )


def _mark_page_rate_limited(page_key: int) -> None:
    global _PROFILE_RATE_LIMIT_UNTIL
    with _RATE_LIMIT_LOCK:
        cooldown_until = time.time() + BAIYING_RATE_LIMIT_COOLDOWN_SECONDS
        _PAGE_RATE_LIMIT_UNTIL[page_key] = time.monotonic() + BAIYING_RATE_LIMIT_COOLDOWN_SECONDS
        _PROFILE_RATE_LIMIT_UNTIL = cooldown_until
        if len(_PAGE_RATE_LIMIT_UNTIL) > MATERIAL_LIST_CACHE_MAX_ENTRIES:
            oldest = min(_PAGE_RATE_LIMIT_UNTIL, key=_PAGE_RATE_LIMIT_UNTIL.get)
            _PAGE_RATE_LIMIT_UNTIL.pop(oldest, None)
        state_path = _RATE_LIMIT_STATE_PATH
        if state_path is not None:
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
                temporary_path.write_text(json.dumps({
                    "cooldownUntil": cooldown_until,
                    "updatedAt": datetime.now().isoformat(timespec="seconds"),
                }), encoding="utf-8")
                os.replace(temporary_path, state_path)
            except OSError:
                # 状态文件不可写时仍保留进程内冷却，不能因辅助持久化失败中断页面流程。
                pass


def _page_rate_limit_remaining(page_key: int) -> int:
    global _PROFILE_RATE_LIMIT_UNTIL
    with _RATE_LIMIT_LOCK:
        monotonic_until = _PAGE_RATE_LIMIT_UNTIL.get(page_key, 0.0)
        monotonic_remaining = monotonic_until - time.monotonic()
        profile_remaining = _PROFILE_RATE_LIMIT_UNTIL - time.time()
        remaining = max(monotonic_remaining, profile_remaining)
        if monotonic_remaining <= 0:
            _PAGE_RATE_LIMIT_UNTIL.pop(page_key, None)
        if profile_remaining <= 0 and _PROFILE_RATE_LIMIT_UNTIL:
            _PROFILE_RATE_LIMIT_UNTIL = 0.0
            if _RATE_LIMIT_STATE_PATH is not None:
                try:
                    _RATE_LIMIT_STATE_PATH.unlink()
                except OSError:
                    pass
        return math.ceil(max(0.0, remaining))


def _configure_rate_limit_profile(profile_path: Path) -> None:
    """按账号 profile 载入限流冷却，桥接或客户端重启后仍能避免立即重试。"""
    global _RATE_LIMIT_STATE_PATH, _PROFILE_RATE_LIMIT_UNTIL
    resolved = profile_path.expanduser().resolve()
    profile_hash = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:20]
    _RATE_LIMIT_STATE_PATH = resolved.parent / ".touguang-state" / f"rate-limit-{profile_hash}.json"
    _PAGE_RATE_LIMIT_UNTIL.clear()
    try:
        state = json.loads(_RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
        _PROFILE_RATE_LIMIT_UNTIL = float(state.get("cooldownUntil", 0.0)) if isinstance(state, dict) else 0.0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _PROFILE_RATE_LIMIT_UNTIL = 0.0
    if _PROFILE_RATE_LIMIT_UNTIL <= time.time():
        _PROFILE_RATE_LIMIT_UNTIL = 0.0
        try:
            _RATE_LIMIT_STATE_PATH.unlink()
        except OSError:
            pass


def emit(payload: Dict[str, Any]) -> None:
    with _OUTPUT_LOCK:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _report_operation_progress(
    callback: Optional[Callable[[Dict[str, Any]], None]],
    stage: str,
    *,
    index: Optional[int] = None,
    total: Optional[int] = None,
    percent: Optional[int] = None,
    unit: str = "",
    message: str = "",
    product_id: str = "",
    product_title: str = "",
) -> None:
    if callback is None:
        return
    payload: Dict[str, Any] = {"stage": stage, "indeterminate": percent is None}
    if index is not None:
        payload["index"] = index
    if total is not None:
        payload["total"] = total
    if percent is not None:
        payload["percent"] = max(0, min(100, int(percent)))
    if unit:
        payload["unit"] = unit
    if message:
        payload["message"] = message
    if product_id:
        payload["productId"] = product_id
    if product_title:
        payload["productTitle"] = product_title
    callback(payload)


def read_port(profile_path: Path) -> int:
    active_port = profile_path / "DevToolsActivePort"
    try:
        port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError) as error:
        raise RuntimeError(f"Chrome 没有提供 CDP 端口文件：{active_port}") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("Chrome CDP 端口无效")
    return port


def is_allowed_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_OPEN_HOSTS)


def should_forward_response(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host.endswith("jinritemai.com") or host.endswith("douyinec.com")):
        return False
    lowered = url.lower()
    return any(marker in lowered for marker in CAPTURE_MARKERS)


def attach_page(page: Page, attached: set[int]) -> None:
    page_id = id(page)
    if page_id in attached:
        return
    attached.add(page_id)

    def on_response(response: Any) -> None:
        if getattr(page, "_touguang_skip_baiying_capture", False):
            return
        url = str(getattr(response, "url", ""))
        if not should_forward_response(url):
            return
        try:
            body = response.body()
            if len(body) > MAX_RESPONSE_BYTES:
                return
            emit({"type": "baiying-response", "url": url, "body": body.decode("utf-8", errors="replace")})
        except Exception:
            # 响应可能已被页面释放，单条响应失败不影响浏览器会话。
            return

    page.on("response", on_response)


def page_snapshot(context: BrowserContext) -> list[Dict[str, str]]:
    pages: list[Dict[str, str]] = []
    for page in context.pages:
        try:
            pages.append({"url": page.url, "title": page.title()})
        except Exception:
            pages.append({"url": page.url, "title": ""})
    return pages


def _creator_page(context: BrowserContext) -> Page:
    """在账号专属 Chrome 中打开或复用创作者中心上传页。"""
    page = next((
        candidate for candidate in reversed(context.pages)
        if not candidate.is_closed()
        and _is_creator_publish_page(candidate.url)
    ), None)
    if page is None:
        page = _tracked_new_page(context, "creator")
        page.goto(DOUYIN_CREATOR_UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
    else:
        _track_reused_page(context, "creator")
    page.bring_to_front()
    return page


def _is_creator_publish_page(url: str) -> bool:
    """只接受投稿截图对应的抖音创作者中心视频投稿页。"""
    parsed = urlparse(str(url or ""))
    return (
        (parsed.hostname or "").lower() == "creator.douyin.com"
        and parsed.path.rstrip("/") == DOUYIN_CREATOR_POST_PATH
    )


def _is_visible(locator: Any) -> bool:
    try:
        return locator.is_visible(timeout=500)
    except TypeError:
        # ElementHandle.is_visible 不接收 timeout；弹窗内的已解析节点使用这一接口。
        try:
            return locator.is_visible()
        except Exception:
            return False
    except Exception:
        return False


def _creator_work_title_field(page: Page) -> Optional[Any]:
    """定位投稿基础信息中的作品标题，避免把简介或商品短标题当成标题。"""
    candidates = page.locator("input:not([type='file']):not([type='hidden']), textarea, [contenteditable='true'], [role='textbox']")
    matches: list[Any] = []
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not _is_visible(candidate):
            continue
        try:
            metadata = candidate.evaluate("""el => {
                const attrs = [el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                    el.getAttribute('data-placeholder'), el.getAttribute('data-testid')]
                    .filter(Boolean).join(' ');
                const labels = Array.from(el.labels || []).map(label => label.innerText || '').join(' ');
                return `${attrs} ${labels}`.toLowerCase();
            }""")
        except Exception:
            metadata = ""
        if "作品标题" in str(metadata):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _creator_description_field(page: Page) -> Optional[Any]:
    candidates = page.locator("textarea, [contenteditable='true'], [role='textbox']")
    visible_fallback: list[Any] = []
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not _is_visible(candidate):
            continue
        try:
            attributes = candidate.evaluate("""el => [
                el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                el.getAttribute('data-placeholder'), el.getAttribute('data-testid'),
                el.getAttribute('class'), el.getAttribute('contenteditable')
            ].filter(Boolean).join(' ').toLowerCase()""")
        except Exception:
            attributes = ""
        if any(word in str(attributes) for word in ("描述", "简介", "文案")):
            return candidate
        visible_fallback.append(candidate)
    return visible_fallback[0] if len(visible_fallback) == 1 else None


def _creator_text_value(locator: Any) -> str:
    return str(locator.evaluate("el => el.isContentEditable ? el.innerText : (el.value || el.textContent || '')"))


def _creator_text_limit(locator: Any, fallback: int) -> int:
    try:
        raw = locator.get_attribute("maxlength", timeout=1_000)
        if raw is not None:
            limit = int(raw)
            if limit > 0:
                return min(fallback, limit)
    except Exception:
        pass
    return fallback


def _creator_text_length_units(value: str) -> int:
    """HTML maxlength 按 UTF-16 code unit 计数；中文 BMP 字符各占一个单位。"""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _creator_saved_banned_words(command: Dict[str, Any]) -> list[str]:
    supplied = command.get("bannedWords")
    if isinstance(supplied, list):
        return list(dict.fromkeys(str(word).strip() for word in supplied if str(word).strip()))
    database_path = str(command.get("selectionSettingsDbPath") or "").strip()
    if database_path:
        connection: Optional[sqlite3.Connection] = None
        try:
            path = Path(database_path).expanduser().resolve()
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=3)
            row = connection.execute(
                "SELECT settings_json FROM selection_settings WHERE scope_key='global'"
            ).fetchone()
            if row:
                settings = json.loads(row[0])
                words = settings.get("bannedWords") if isinstance(settings, dict) else None
                if isinstance(words, list):
                    return list(dict.fromkeys(str(word).strip() for word in words if str(word).strip()))
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            pass
        finally:
            if connection is not None:
                connection.close()
    return list(DEFAULT_BANNED_WORDS)


def _creator_manual_review_active(command: Dict[str, Any]) -> bool:
    """最终点击前复查本机开关，防止上传期间切换到人工复核后继续自动发布。"""
    database_path = str(command.get("selectionSettingsDbPath") or "").strip()
    if not database_path:
        return False
    path = Path(database_path).expanduser().resolve()
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=3)
        row = connection.execute(
            "SELECT manual_review_enabled FROM publish_settings WHERE setting_id = 1"
        ).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(f"人工复核开关无法读取，已停在最终发布前：{error}") from error
    finally:
        if connection is not None:
            connection.close()
    if row is None:
        raise RuntimeError("人工复核开关记录不存在，已停在最终发布前")
    return bool(row[0])


def _creator_strip_price_mentions(value: Any) -> str:
    text = str(value or "")
    label_price = re.compile(
        r"(?:每单(?:赚|佣金)?|佣金|售价|价格|原价|现价|到手价|券后价|优惠价|特价|活动价|直播价|"
        r"会员价|专享价|秒杀价|团购价|低至|仅需|只要)\s*[:：]?\s*"
        r"(?:人民币|RMB)?\s*[￥¥$]?\s*\d+(?:[,.]\d+)?\s*(?:元|块(?:钱)?|人民币)?",
        flags=re.IGNORECASE,
    )
    text = label_price.sub(" ", text)
    text = re.sub(r"[￥¥$]\s*\d+(?:[,.]\d+)?\s*(?:元|块(?:钱)?)?", " ", text)
    text = re.sub(
        r"(?<![A-Za-z0-9])\d+(?:[,.]\d+)?\s*(?:元|块(?:钱)?)(?![A-Za-z0-9])",
        " ", text,
    )
    text = re.sub(r"(?<![A-Za-z0-9])\d+[.]\d{1,2}(?![A-Za-z0-9])", " ", text)
    text = re.sub(r"\bRMB\s*\d+(?:[,.]\d+)?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?:每单赚|每单佣金|佣金|售价|价格|原价|现价|到手价|券后价|优惠价|特价|活动价|直播价|"
        r"会员价|专享价|秒杀价|团购价|低至|仅需|只要)\s*[:：]?",
        " ", text, flags=re.IGNORECASE,
    )
    return text


def _creator_clean_publication_text(value: Any, banned_words: Sequence[str] = ()) -> str:
    """去掉价格、当前全局屏蔽词和分词器识别的人名，供发布标题及短标题使用。"""
    text = _creator_strip_price_mentions(value)
    if jieba_posseg is not None:
        person_names = {
            str(getattr(token, "word", "") or "").strip()
            for token in jieba_posseg.cut(text, HMM=True)
            if str(getattr(token, "flag", "") or "").lower().startswith("nr")
        }
        for name in sorted((name for name in person_names if name), key=len, reverse=True):
            text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    for word in sorted((str(word).strip() for word in banned_words if str(word).strip()), key=len, reverse=True):
        text = re.sub(re.escape(word), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s|｜,，;；:：/\\·•、—–-]+", " ", text)
    text = re.sub(r"^[\s。.!！?？,，;；:：]+|[\s。.!！?？,，;；:：]+$", "", text)
    return " ".join(text.split())


def _creator_truncate_text_units(value: str, limit: int) -> str:
    result: list[str] = []
    used = 0
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > max(0, limit):
            break
        result.append(character)
        used += units
    return "".join(result).rstrip()


def _creator_publication_title(product_title: str, limit: int, banned_words: Sequence[str]) -> str:
    clean = _creator_clean_publication_text(product_title, banned_words)
    return _creator_truncate_text_units(clean, limit)


def _creator_strip_leading_title_tag(value: str) -> str:
    """移除短括号店铺前缀，让商品本身的词优先；长括号商品标题整体保留。"""
    text = " ".join(str(value or "").split())
    for _ in range(3):
        match = re.match(
            r"^\s*(?:【[^【】]{1,12}】|\[[^\[\]]{1,12}\]|（[^（）]{1,12}）|\([^()]{1,12}\))\s*",
            text,
        )
        if not match:
            break
        text = text[match.end():]
    return text


def _creator_product_short_title(
    product_title: str,
    description: str,
    limit: int = 10,
    banned_words: Sequence[str] = (),
) -> str:
    """从商品标题和作品简介抽取关键词，按来源顺序组合，不再截取标题前缀。"""
    if jieba_posseg is None:
        raise RuntimeError("中文关键词组件未安装，无法安全生成商品短标题")

    title_text = _creator_clean_publication_text(_creator_strip_leading_title_tag(product_title), banned_words)
    description_text = _creator_clean_publication_text(_creator_strip_leading_title_tag(description), banned_words)
    source_values = ((title_text, 2.0, 0), (description_text, 1.0, 1))
    terms: Dict[str, Dict[str, Any]] = {}
    pos_weights = {
        "n": 1.35, "nr": 1.1, "nt": 1.1, "nz": 1.2, "vn": 1.0,
        "v": 0.7, "a": 0.75, "eng": 1.0, "x": 0.45,
    }

    for source_text, source_weight, source_index in source_values:
        if not source_text:
            continue
        position = 0
        for token in jieba_posseg.cut(source_text, HMM=True):
            word = str(getattr(token, "word", "") or "").strip()
            flag = str(getattr(token, "flag", "") or "").lower()
            if (
                not word or word.casefold() in _CREATOR_SHORT_TITLE_STOPWORDS or
                flag == "ns" or flag.startswith("nr") or flag == "m"
            ):
                position += len(word)
                continue
            pos_weight = pos_weights.get(flag)
            if pos_weight is None and flag.startswith("n"):
                pos_weight = 1.1
            elif pos_weight is None and flag.startswith("v"):
                pos_weight = 0.7
            elif pos_weight is None and flag.startswith("a"):
                pos_weight = 0.75
            if pos_weight is None:
                position += len(word)
                continue
            if (len(word) < 2 or _creator_text_length_units(word) > limit
                    or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", word)):
                position += len(word)
                continue
            key = word.casefold()
            record = terms.setdefault(key, {
                "word": word,
                "score": 0.0,
                "title_position": None,
                "description_position": None,
            })
            length_bonus = 1.0 + min(len(word), 6) * 0.05
            generic_term_penalty = 0.55 if key in {"水果", "零食", "日用品"} else 1.0
            record["score"] += source_weight * pos_weight * length_bonus * generic_term_penalty
            position_key = "title_position" if source_index == 0 else "description_position"
            if record[position_key] is None:
                record[position_key] = position
            position += len(word)

    ranked = sorted(
        terms.values(),
        key=lambda item: (
            -float(item["score"]),
            -len(str(item["word"])),
            item["title_position"] if item["title_position"] is not None else 10**9,
            item["description_position"] if item["description_position"] is not None else 10**9,
        ),
    )
    selected: list[Dict[str, Any]] = []
    used_units = 0
    for candidate in ranked:
        word = str(candidate["word"])
        if any(word in str(item["word"]) or str(item["word"]) in word for item in selected):
            continue
        if any(
            word in alias_group and str(item["word"]) in alias_group
            for item in selected
            for alias_group in _CREATOR_SHORT_TITLE_ALIAS_GROUPS
        ):
            continue
        units = _creator_text_length_units(word)
        if used_units + units > limit:
            continue
        selected.append(candidate)
        used_units += units
        if used_units == limit:
            break

    selected.sort(key=lambda item: (
        0 if item["title_position"] is not None else 1,
        item["title_position"] if item["title_position"] is not None else item["description_position"],
    ))
    result = "".join(str(item["word"]) for item in selected)
    if not result:
        raise RuntimeError("清理价格、全局屏蔽词和人名后，未能提取可用商品短标题；未点击发布")
    return result


def _creator_product_card(page: Page, product_name: str) -> tuple[Optional[Any], str]:
    """按商品名定位已添加商品卡片及其唯一编辑按钮。"""
    candidates = page.locator("button, [role='button']")
    matches: list[tuple[int, Any, str]] = []
    for index in range(candidates.count()):
        button = candidates.nth(index)
        if not _is_visible(button):
            continue
        try:
            label = " ".join(str(button.inner_text(timeout=500) or "").split())
        except Exception:
            continue
        if label != "编辑":
            continue
        try:
            card_text = str(button.evaluate("""(el, expectedName) => {
                for (let node = el.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
                    const text = String(node.innerText || '').replace(/\\s+/g, ' ').trim();
                    const actions = Array.from(node.querySelectorAll('button, [role="button"]'))
                        .map(item => String(item.innerText || '').replace(/\\s+/g, ' ').trim());
                    if (text.includes(expectedName) && actions.includes('编辑') &&
                        (actions.includes('移除') || text.includes('短标题'))) return text;
                }
                return '';
            }""", product_name))
        except Exception:
            card_text = ""
        if card_text:
            matches.append((len(card_text), button, card_text))
    if not matches:
        return None, ""
    shortest = min(item[0] for item in matches)
    best = [item for item in matches if item[0] == shortest]
    return (best[0][1], best[0][2]) if len(best) == 1 else (None, "")


def _creator_product_card_hrefs(edit_button: Any, product_name: str) -> list[str]:
    """只读取当前唯一商品卡片自身的链接，不借用输入框或其它卡片的商品 ID。"""
    if edit_button is None:
        return []
    try:
        values = edit_button.evaluate("""(el, expectedName) => {
            for (let node = el.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
                const text = String(node.innerText || '').replace(/\\s+/g, ' ').trim();
                const actions = Array.from(node.querySelectorAll('button, [role="button"]'))
                    .map(item => String(item.innerText || '').replace(/\\s+/g, ' ').trim());
                if (text.includes(expectedName) && actions.includes('编辑') &&
                    (actions.includes('移除') || text.includes('短标题'))) {
                    return Array.from(node.querySelectorAll('a[href]')).map(link => String(link.href || ''));
                }
            }
            return [];
        }""", product_name)
        return [str(value) for value in values] if isinstance(values, list) else []
    except Exception:
        return []


def _creator_product_short_title_field(page: Page) -> Optional[Any]:
    candidates = page.locator("input:not([type='file']):not([type='hidden']), textarea")
    matches: list[tuple[int, int, Any]] = []
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not _is_visible(candidate):
            continue
        try:
            metadata = candidate.evaluate("""el => {
                const attrs = [el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                    el.getAttribute('data-testid')].filter(Boolean).join(' ');
                const labels = Array.from(el.labels || []).map(label => label.innerText || '').join(' ');
                const contexts = [];
                for (let node = el.parentElement, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
                    contexts.push(String(node.innerText || '').replace(/\\s+/g, ' ').trim());
                }
                return { attrs, labels, contexts };
            }""")
        except Exception:
            continue
        attrs = str(metadata.get("attrs") or "")
        labels = str(metadata.get("labels") or "")
        contexts = [str(value) for value in metadata.get("contexts", [])]
        direct_match = "商品短标题" in attrs or "商品短标题" in labels
        context_depth = next((depth for depth, value in enumerate(contexts) if "商品短标题" in value), None)
        if direct_match:
            matches.append((0, len(attrs) + len(labels), candidate))
        elif context_depth is not None:
            matches.append((context_depth + 1, len(contexts[context_depth]), candidate))
    if not matches:
        return None
    best_rank = min((item[0], item[1]) for item in matches)
    best = [item for item in matches if (item[0], item[1]) == best_rank]
    return best[0][2] if len(best) == 1 else None


def _creator_card_short_title(card_text: str) -> str:
    compact = " ".join(str(card_text or "").split())
    match = re.search(r"短标题\s*[:：]\s*(.+?)(?=\s*(?:每单赚|售价|$))", compact)
    return match.group(1).strip() if match else ""


def _creator_product_edit_modal(page: Page) -> Optional[Any]:
    """只识别当前可见的“编辑商品”弹窗，避免误填投稿页的其他输入框。"""
    for selector in (".semi-modal", "[role='dialog']"):
        dialogs = page.locator(selector)
        matches = []
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if not _is_visible(dialog):
                continue
            if _visible_exact_text(dialog, "编辑商品") and _visible_exact_text(dialog, "商品短标题*"):
                matches.append(dialog)
        if len(matches) > 1:
            raise RuntimeError("当前有多个“编辑商品”弹窗，无法确定本次商品；未点击发布")
        if matches:
            return matches[0]
    return None


def _fill_creator_product_short_title(
    page: Page,
    product_name: str,
    description: str = "",
    expected_short_title: Optional[str] = None,
    banned_words: Sequence[str] = (),
) -> str:
    # 新版页面点击“添加链接”就先弹必填短标题；旧版则先显示卡片，再点“编辑”。
    # 两种入口都先确认当前弹窗的原标题，完成后才允许核验商品关联。
    dialog = _creator_product_edit_modal(page)
    if dialog is None:
        # 给添品后的自动弹窗一点时间，避免在旧卡片仍可见时抢先点“编辑”。
        modal_deadline = time.monotonic() + 2
        while time.monotonic() < modal_deadline:
            dialog = _creator_product_edit_modal(page)
            if dialog is not None:
                break
            page.wait_for_timeout(200)
    if dialog is None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            dialog = _creator_product_edit_modal(page)
            if dialog is not None:
                break
            edit_button, _ = _creator_product_card(page, product_name)
            if edit_button is not None:
                edit_button.click(timeout=10_000)
                break
            page.wait_for_timeout(200)
        if dialog is None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                dialog = _creator_product_edit_modal(page)
                if dialog is not None:
                    break
                page.wait_for_timeout(200)
    if dialog is None:
        raise RuntimeError("添加链接后没有出现当前商品的编辑弹窗或可编辑商品卡片；未点击发布")

    expected_name = " ".join(product_name.strip().split())
    dialog_text = " ".join((dialog.inner_text(timeout=2_000) or "").split())
    if not expected_name or expected_name not in dialog_text:
        raise RuntimeError("编辑商品弹窗的原标题与当前队列商品不一致；未填写短标题或点击发布")
    field = _creator_product_short_title_field(dialog)
    if field is None:
        raise RuntimeError("商品编辑框中没有唯一识别到“商品短标题”；未点击发布")
    short_title_limit = _creator_text_limit(field, 10)
    short_title = expected_short_title or _creator_product_short_title(
        product_name, description, short_title_limit, banned_words
    )
    if _creator_text_length_units(short_title) > short_title_limit:
        short_title = _creator_product_short_title(product_name, description, short_title_limit, banned_words)
    if not short_title:
        raise RuntimeError("商品名称为空，无法填写商品短标题；未点击发布")
    _fill_creator_text_with_readback(
        page,
        field,
        short_title,
        "商品短标题",
        normalizer=lambda value: str(value or "").strip(),
    )

    done_buttons = dialog.get_by_role("button", name="完成编辑", exact=True)
    if done_buttons.count() != 1 or not _is_visible(done_buttons.first):
        raise RuntimeError("商品短标题已填写，但没有唯一识别到“完成编辑”按钮；未点击发布")
    ready_deadline = time.monotonic() + 5
    while time.monotonic() < ready_deadline:
        done_button = done_buttons.first
        disabled_class = "disabled" in str(done_button.get_attribute("class") or "").lower()
        if (done_button.is_enabled() and done_button.get_attribute("aria-disabled") != "true"
                and not disabled_class):
            break
        page.wait_for_timeout(100)
    else:
        raise RuntimeError("商品短标题已填写，但“完成编辑”仍不可用；未点击发布")
    done_buttons.first.click(timeout=10_000)
    try:
        dialog.wait_for(state="hidden", timeout=10_000)
    except Exception as error:
        raise RuntimeError(f"商品短标题编辑结果未确认；未点击发布。原因：{error}") from error

    deadline = time.monotonic() + 10
    actual = ""
    card_text = ""
    while time.monotonic() < deadline:
        _, card_text = _creator_product_card(page, product_name)
        actual = _creator_card_short_title(card_text)
        if actual == short_title:
            break
        page.wait_for_timeout(200)
    if actual != short_title:
        if not card_text:
            raise RuntimeError("完成编辑后没有唯一识别到当前商品卡片；未点击发布")
        raise RuntimeError(
            f"商品卡片短标题与商品名称不一致（预期：{short_title}；回读：{actual or '空'}）；未点击发布"
        )
    return short_title


def normalize_creator_description_readback(value: Any) -> str:
    """统一换行，并移除创作者中心编辑器附加的末尾空行/零宽光标占位符。"""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\u200b\n")


def _fill_creator_text_with_readback(
    page: Page,
    field: Any,
    expected: str,
    label: str,
    *,
    normalizer: Optional[Callable[[Any], str]] = None,
    timeout_seconds: float = 3.0,
) -> str:
    """填写创作者中心文本并等待框架回写；失效时用键盘输入重试一次，始终按回读值放行。"""
    normalize = normalizer or normalize_creator_description_readback
    expected_value = normalize(expected)
    fill_error = ""
    try:
        field.fill(expected, timeout=10_000)
    except Exception as error:
        fill_error = type(error).__name__

    def wait_for_expected_value() -> tuple[bool, str]:
        actual = ""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                actual = _creator_text_value(field)
                if normalize(actual) == expected_value:
                    return True, actual
            except Exception:
                actual = ""
            page.wait_for_timeout(100)
        return False, actual

    matched, actual = wait_for_expected_value()
    if matched:
        return actual

    keyboard_error = ""
    try:
        field.click(timeout=5_000)
        field.press("Control+A", timeout=3_000)
        field.press_sequentially(expected, delay=5, timeout=15_000)
    except Exception as error:
        keyboard_error = type(error).__name__
    matched, actual = wait_for_expected_value()
    if matched:
        return actual

    details = f"预期字符数 {len(expected)}，实际回读字符数 {len(actual)}"
    if fill_error:
        details += f"；fill={fill_error}"
    if keyboard_error:
        details += f"；键盘重试={keyboard_error}"
    raise RuntimeError(f"{label}未能准确填入；{details}")


def _creator_product_link_field(page: Page) -> Optional[Any]:
    candidates = page.locator("input:not([type='file']):not([type='hidden']):not([type='checkbox']):not([type='radio'])")
    scored: list[tuple[int, Any]] = []
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not _is_visible(candidate):
            continue
        try:
            metadata = candidate.evaluate("""el => {
                const parts = [];
                for (let node = el, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
                    parts.push(node.innerText || '');
                }
                return {
                    attrs: [el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('data-testid')].filter(Boolean).join(' ').toLowerCase(),
                    value: el.value || '',
                    context: parts.join(' ').toLowerCase(),
                    sameRowAddLink: Array.from(el.parentElement?.children || []).filter(node =>
                        node !== el && String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim() === '添加链接'
                    ).length
                };
            }""")
        except Exception:
            continue
        attrs = str(metadata.get("attrs") or "")
        current = str(metadata.get("value") or "").lower()
        context = str(metadata.get("context") or "")
        # 先限定到实际链接行；仅靠五层祖先的文字可能把作品标题误当链接框。
        if metadata.get("sameRowAddLink") != 1:
            continue
        score = 6
        if any(word in attrs for word in ("商品链接", "链接", "url")):
            score += 5
        if "haohuo.jinritemai.com" in current:
            score += 5
        if "购物车" in context:
            score += 2
        if "添加链接" in context:
            score += 2
        scored.append((score, candidate))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    best = [candidate for score, candidate in scored if score == best_score]
    return best[0] if len(best) == 1 else None


def _product_url_matches_product(product_url: str, product_id: str) -> bool:
    parsed = urlparse(str(product_url or "").strip())
    if parsed.scheme != "https" or parsed.hostname != "haohuo.jinritemai.com":
        return False
    return _url_matches_exact_product_id(parsed, str(product_id))


def _url_path_has_exact_product_id(path: str, product_id: str) -> bool:
    """仅接受 URL 路径中完整匹配的商品 ID 段，避免 ID 子串误匹配。"""
    expected = str(product_id or "").strip()
    if not expected:
        return False
    return any(unquote(segment) == expected for segment in str(path or "").split("/") if segment)


def _url_matches_exact_product_id(parsed: Any, product_id: str) -> bool:
    expected = str(product_id or "").strip()
    query = parse_qs(str(getattr(parsed, "query", "") or ""))
    query_ids = [
        str(value) for key in ("id", "product_id", "productId")
        for value in query.get(key, [])
    ]
    if query_ids:
        return bool(expected) and all(value == expected for value in query_ids)
    return _url_path_has_exact_product_id(str(getattr(parsed, "path", "") or ""), expected)


def _extract_copied_product_url(clipboard_text: str, product_id: str) -> str:
    """只接受百应复制的好货链接，且链接中必须能核对当前商品 ID。"""
    match = re.search(r"https://[^\s<>\"']+", str(clipboard_text or ""))
    if not match:
        raise RuntimeError("百应提示已复制商品链接，但系统剪贴板中没有可用链接；未继续投稿")
    product_url = match.group(0).rstrip(".,;，。；）】\u300b")
    if not _product_url_matches_product(product_url, product_id):
        raise RuntimeError("百应复制的商品链接域名或商品 ID 与当前选品不匹配；未继续投稿")
    return product_url


def _read_windows_clipboard_text() -> str:
    """读取百应刚复制到 Windows 剪贴板的文本，不记录或改写剪贴板内容。"""
    if os.name != "nt":
        raise RuntimeError("读取百应复制链接需要 Windows 剪贴板")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    clipboard_open = False
    for _ in range(20):
        if user32.OpenClipboard(None):
            clipboard_open = True
            break
        time.sleep(0.1)
    if not clipboard_open:
        raise RuntimeError("Windows 剪贴板暂时被占用，未能读取百应商品链接")
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _visible_exact_text(page: Page, text: str) -> list[Any]:
    matches = page.get_by_text(text, exact=True)
    return [matches.nth(index) for index in range(matches.count()) if _is_visible(matches.nth(index))]


def _click_unique_visible_text(page: Page, text: str, description: str, timeout_ms: int = 5_000) -> Any:
    matches = _visible_exact_text(page, text)
    if len(matches) != 1:
        raise RuntimeError(f"没有唯一识别到“{description}”；已停止，未继续投稿")
    matches[0].click(timeout=timeout_ms)
    return matches[0]


def _baiying_cart_page(context: BrowserContext) -> Page:
    page = next((
        candidate for candidate in reversed(context.pages)
        if not candidate.is_closed()
        and urlparse(candidate.url).hostname == "buyin.jinritemai.com"
        and urlparse(candidate.url).path.rstrip("/") == "/dashboard/merch-picking-cart"
    ), None)
    if page is None:
        page = _tracked_new_page(context, "baiying_cart")
        page.goto(BAIYING_SELECTION_CART_URL, wait_until="domcontentloaded", timeout=60_000)
    else:
        _track_reused_page(context, "baiying_cart")
    page.bring_to_front()
    try:
        body = page.locator("body").inner_text(timeout=5_000)
    except Exception:
        body = ""
    if "登录" in page.url.lower() or "扫码登录" in body:
        raise RuntimeError("当前账号的百应选品车未登录；未复制商品链接或上传视频")
    return page


def _baiying_product_search_field(page: Page, product_id: str) -> Any:
    selector = (
        "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='file']),"
        " textarea, [contenteditable='true'], [role='searchbox']"
    )
    scored: list[tuple[int, Any]] = []
    visible_fields: list[str] = []
    for frame in page.frames:
        candidates = frame.locator(selector)
        try:
            candidate_count = candidates.count()
        except Exception:
            continue
        for index in range(candidate_count):
            candidate = candidates.nth(index)
            if not _is_visible(candidate):
                continue
            try:
                metadata = candidate.evaluate("""el => {
                    const context = [];
                    for (let node = el, depth = 0; node && depth < 5; node = node.parentElement, depth++) {
                        context.push(node.innerText || '');
                    }
                    return {
                        value: el.value || el.innerText || el.textContent || '',
                        attrs: [el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('name'), el.getAttribute('title')].filter(Boolean).join(' ').toLowerCase(),
                        context: context.join(' ').toLowerCase(),
                        tag: el.tagName.toLowerCase()
                    };
                }""")
            except Exception:
                continue
            value = str(metadata.get("value") or "").strip()
            attrs = str(metadata.get("attrs") or "")
            context = str(metadata.get("context") or "")
            visible_fields.append(f"{metadata.get('tag')}:{attrs[:48]}")
            score = 0
            if value == product_id:
                score += 10
            if "商品名称" in attrs and "id" in attrs:
                score += 15
            elif any(token in attrs for token in ("商品", "product", "搜索", "查询")):
                score += 8
            if "商品名称" in context or re.search(r"商品.{0,8}id", context):
                score += 4
            if score:
                scored.append((score, candidate))
    if not scored:
        page_path = urlparse(page.url).path
        frames = [urlparse(frame.url).path for frame in page.frames if frame.url and frame.url != "about:blank"]
        fields = "、".join(visible_fields[:6]) or "无可见输入框"
        raise RuntimeError(
            f"选品车中没有识别到商品名称/ID 搜索框；页面路径={page_path}；"
            f"可见输入框={fields}；子页面路径={frames[:3]}；未继续投稿"
        )
    best_score = max(score for score, _ in scored)
    best = [field for score, field in scored if score == best_score]
    if len(best) != 1:
        raise RuntimeError("选品车商品搜索框不唯一；未继续投稿")
    return best[0]


def _select_baiying_product_row(candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """优先按商品 ID，退回到唯一商品标题，选择百应选品车中的目标行。"""
    id_matches = [row for row in candidates if row.get("matchesProductId")]
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        raise RuntimeError(f"商品 ID 命中 {len(id_matches)} 个可见商品行")

    title_matches = [row for row in candidates if row.get("matchesProductTitle")]
    if len(title_matches) == 1:
        return title_matches[0]
    if len(title_matches) > 1:
        raise RuntimeError(f"商品标题命中 {len(title_matches)} 个可见商品行")
    raise RuntimeError("当前可见商品行中没有匹配目标商品 ID 或标题")


def _open_baiying_product_content_menu(page: Page, row: Any, row_diagnostics: str) -> None:
    """悬停目标商品行内唯一的“去带货”，等待其二级菜单，再点短视频/图文带货。"""
    actions = _visible_exact_text(row, "去带货")
    if len(actions) != 1:
        raise RuntimeError(
            f"目标商品行已确认（{row_diagnostics}），但行内“去带货”操作匹配 {len(actions)} 个；"
            "已停止，未悬停菜单或上传视频"
        )
    try:
        # “短视频/图文带货”是“去带货”的悬停子菜单；直接全页查找会在菜单尚未展开时误报不存在。
        actions[0].hover(timeout=5_000)
    except Exception as error:
        raise RuntimeError(
            f"已确认目标商品行（{row_diagnostics}），但无法悬停“去带货”展开菜单："
            f"{_safe_diagnostic_text(error, 320)}；未上传视频"
        ) from error

    deadline = time.monotonic() + 5
    last_count = 0
    while time.monotonic() < deadline:
        menu_items = _visible_exact_text(page, "短视频/图文带货")
        last_count = len(menu_items)
        if last_count == 1:
            try:
                menu_items[0].click(timeout=5_000)
                return
            except Exception as error:
                raise RuntimeError(
                    f"悬停“去带货”后已唯一找到“短视频/图文带货”，但点击失败："
                    f"{_safe_diagnostic_text(error, 320)}；未上传视频"
                ) from error
        page.wait_for_timeout(100)

    raise RuntimeError(
        f"已悬停目标商品行内“去带货”（{row_diagnostics}）并等待 5 秒，"
        f"但可见的“短视频/图文带货”菜单匹配 {last_count} 个；未继续投稿或上传视频"
    )


def _baiying_row_snapshot(page: Page, max_rows: int = 2) -> str:
    """只读记录查询前/后的可见行数量、文本和商品 ID 属性线索。"""
    for selector in BAIYING_CART_ROW_SELECTORS:
        rows = page.locator(selector)
        try:
            total = rows.count()
        except Exception as error:
            return f"{selector}:读取失败 {type(error).__name__}"
        samples: list[str] = []
        visible_count = 0
        for index in range(total):
            row = rows.nth(index)
            if not _is_visible(row):
                continue
            visible_count += 1
            if len(samples) >= max_rows:
                continue
            try:
                metadata = row.evaluate(BAIYING_ROW_METADATA_SCRIPT, {"productId": "", "productTitle": ""})
            except Exception as error:
                samples.append(f"#{index + 1}读取失败:{type(error).__name__}")
                continue
            text = str(metadata.get("rowText") or "无文本")[:72]
            row_key = str(metadata.get("rowKey") or "无")[:24]
            hints = metadata.get("identityHints") or []
            hint_text = f";ID线索={hints[:2]}" if hints else ""
            status_hints = metadata.get("statusHints") or []
            status_text = f";状态线索={status_hints[:2]}" if status_hints else ""
            samples.append(f"#{index + 1} key={row_key} {text}{hint_text}{status_text}")
        if visible_count:
            return f"{selector}:行{visible_count};样本={samples}"
    return "未找到可见商品行（所有已知行选择器均为 0）"


def _locate_baiying_product_row(page: Page, product_id: str, product_title: str) -> tuple[Any, str]:
    """百应已按商品 ID 查询；优先识别 ID/标题行，页面身份线索缺失时信任查询结果行。"""
    diagnostics: list[str] = []
    sampled_visible_rows = False
    fallback_row: Optional[Dict[str, Any]] = None
    for selector in BAIYING_CART_ROW_SELECTORS:
        rows = page.locator(selector)
        visible_rows: list[Dict[str, Any]] = []
        visible_count = 0
        row_read_errors: list[str] = []
        for index in range(rows.count()):
            row = rows.nth(index)
            if not _is_visible(row):
                continue
            visible_count += 1
            try:
                metadata = row.evaluate(BAIYING_ROW_METADATA_SCRIPT, {"productId": product_id, "productTitle": product_title})
            except Exception as error:
                if len(row_read_errors) < 3:
                    row_read_errors.append(f"第{index + 1}行:{type(error).__name__}")
                continue
            candidate_row = {"index": index, "locator": row, **metadata}
            if metadata.get("matchesProductId") or metadata.get("matchesProductTitle"):
                visible_rows.append(candidate_row)
            elif fallback_row is None and len(_visible_exact_text(row, "去带货")) == 1:
                # 百应搜索框已提交完整商品 ID。部分页面版本没有把行内商品 ID
                # 暴露给 DOM/React；这种情况下以查询返回的首个可操作结果行继续。
                fallback_row = candidate_row

        if not visible_rows:
            samples = []
            sample_limit = 2 if visible_count and not sampled_visible_rows else 0
            for index in range(rows.count()):
                if len(samples) >= sample_limit:
                    break
                row = rows.nth(index)
                if not _is_visible(row):
                    continue
                try:
                    sample = row.evaluate(
                        BAIYING_ROW_METADATA_SCRIPT,
                        {"productId": product_id, "productTitle": product_title},
                    )
                    samples.append({
                        "rowKey": str(sample.get("rowKey") or "无")[:32],
                        "text": str(sample.get("rowText") or "无文本")[:72],
                        "identityHints": (sample.get("identityHints") or [])[:2],
                        "domIdentityHints": (sample.get("domIdentityHints") or [])[:2],
                        "statusHints": (sample.get("statusHints") or [])[:4],
                        "matchesProductId": bool(sample.get("matchesProductId")),
                        "matchesProductTitle": bool(sample.get("matchesProductTitle")),
                    })
                except Exception as error:
                    samples.append({"row": index + 1, "readError": type(error).__name__})
            diagnostics.append(f"{selector}:行{visible_count},ID0,标题0")
            if samples:
                diagnostics[-1] += f",样本={samples}"
                sampled_visible_rows = True
                skip_reason = _baiying_row_skip_reason(product_id, samples)
                if skip_reason:
                    skip_exception = _baiying_row_skip_exception(product_id, skip_reason)
                    if not isinstance(skip_exception, WrongBaiyingProductRow):
                        raise skip_exception
            if row_read_errors:
                diagnostics[-1] += f",读取错误={row_read_errors}"
            continue

        try:
            selected = _select_baiying_product_row(visible_rows)
        except RuntimeError as error:
            if not any(row.get("matchesProductId") for row in visible_rows):
                actionable_rows = [row for row in visible_rows
                                   if len(_visible_exact_text(row["locator"], "去带货")) == 1]
                if actionable_rows:
                    selected = actionable_rows[0]
                else:
                    selected = None
            else:
                selected = None
            if selected is not None:
                row_key = str(selected.get("rowKey") or "未提供")[:48]
                return selected["locator"], (
                    f"百应已按商品 ID 查询；沿用当前可操作结果行，不再要求行身份唯一匹配；rowKey={row_key}"
                )
            row_samples = [
                {
                    "rowKey": str(row.get("rowKey") or "无")[:32],
                    "text": str(row.get("rowText") or "无文本")[:72],
                    "identityHints": (row.get("identityHints") or [])[:2],
                }
                for row in visible_rows[:5]
            ]
            skip_reason = _baiying_row_skip_reason(product_id, visible_rows)
            if skip_reason:
                raise _baiying_row_skip_exception(product_id, skip_reason) from error
            raise RuntimeError(
                f"百应商品行无法唯一匹配：{error}；候选行数 {len(visible_rows)}，"
                f"商品 ID 命中 {sum(bool(row.get('matchesProductId')) for row in visible_rows)}，"
                f"标题命中 {sum(bool(row.get('matchesProductTitle')) for row in visible_rows)}，"
                f"候选行样本={row_samples}"
            ) from error

        selected_ids = _row_identity_product_ids(
            selected.get("domIdentityHints") if "domIdentityHints" in selected else selected.get("identityHints")
        )
        if selected.get("matchesProductTitle") and not selected.get("matchesProductId") and selected_ids and product_id not in selected_ids:
            row_key = str(selected.get("rowKey") or "未提供")[:48]
            return selected["locator"], (
                f"百应已按商品 ID 查询；沿用当前标题结果行，不再重复核对行内 DOM ID；rowKey={row_key}"
            )
        unavailable_reason = _baiying_row_unavailable_reason(selected)
        if unavailable_reason:
            raise CandidateCartSkip(product_id, f"目标商品行明确显示{unavailable_reason}")
        identity = "商品 ID" if selected.get("matchesProductId") else "商品标题（唯一回退）"
        row_key = str(selected.get("rowKey") or "未提供")[:48]
        return selected["locator"], f"按{identity}命中 1 行；候选行数 {len(visible_rows)}；rowKey={row_key}"

    if fallback_row is not None:
        row_key = str(fallback_row.get("rowKey") or "未提供")[:48]
        return fallback_row["locator"], (
            f"按商品 ID 查询后采用百应当前结果行；不再要求页面重复暴露商品 ID；rowKey={row_key}"
        )
    raise RuntimeError("百应按商品 ID 查询后没有可操作的结果行；诊断：" + "；".join(diagnostics[:3]))


def _copy_baiying_product_link(
    context: BrowserContext,
    product_id: str,
    product_title: str,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[Page, str]:
    """按商品 ID 从选品车触发带货入口，并读取百应刚复制的商品链接。"""
    cart_page = _baiying_cart_page(context)
    old_copy_markers = cart_page.get_by_text("已成功复制商品链接", exact=False)
    if any(_is_visible(old_copy_markers.nth(index)) for index in range(old_copy_markers.count())):
        try:
            _dismiss_baiying_copy_dialog(cart_page)
            dismiss_deadline = time.monotonic() + 3
            while time.monotonic() < dismiss_deadline:
                old_copy_markers = cart_page.get_by_text("已成功复制商品链接", exact=False)
                if not any(_is_visible(old_copy_markers.nth(index)) for index in range(old_copy_markers.count())):
                    break
                cart_page.wait_for_timeout(100)
            else:
                raise RuntimeError("旧复制提示未关闭")
        except Exception:
            # 旧复制提示无法安全关闭时，使用同账号的新选品车页执行本次查询。
            cart_page = context.new_page()
            cart_page.goto(BAIYING_SELECTION_CART_URL, wait_until="commit", timeout=45_000)
            cart_page.bring_to_front()
    search_field = None
    search_error: Optional[Exception] = None
    search_deadline = time.monotonic() + 10
    while time.monotonic() < search_deadline:
        try:
            search_field = _baiying_product_search_field(cart_page, product_id)
            break
        except RuntimeError as error:
            search_error = error
            cart_page.wait_for_timeout(250)
    if search_field is None:
        raise RuntimeError(str(search_error or "百应选品车搜索框等待超时；未继续投稿")) from search_error
    search_field.fill(product_id, timeout=5_000)
    if (search_field.input_value(timeout=2_000) or "").strip() != product_id:
        raise RuntimeError("选品车商品 ID 搜索条件没有准确填入；未继续投稿")

    before_query_snapshot = _baiying_row_snapshot(cart_page, max_rows=0)
    selected_row = None
    row_diagnostics = ""
    last_row_error: Optional[Exception] = None
    wrong_row_error: Optional[WrongBaiyingProductRow] = None
    attempts = BAIYING_PUBLISH_VERIFY_RECHECKS + 1
    for query_attempt in range(1, attempts + 1):
        query_buttons = _visible_exact_text(cart_page, "查询")
        if len(query_buttons) != 1:
            if wrong_row_error is not None:
                raise RuntimeError(
                    f"已观察到百应错行，但重查时无法唯一定位查询按钮；最后错行证据：{wrong_row_error.reason}"
                ) from wrong_row_error
            raise RuntimeError("选品车中没有唯一识别到商品查询按钮；未继续投稿")
        try:
            query_buttons[0].click(timeout=5_000)
        except Exception as error:
            if wrong_row_error is not None:
                raise RuntimeError(
                    f"已观察到百应错行，但重查按钮执行失败（{type(error).__name__}）；"
                    f"最后错行证据：{wrong_row_error.reason}"
                ) from error
            raise RuntimeError(f"选品车商品查询按钮点击失败；未继续投稿：{error}") from error

        if query_attempt > 1:
            _report_operation_progress(
                progress,
                "百应商品核验重查中",
                index=query_attempt - 1,
                total=BAIYING_PUBLISH_VERIFY_RECHECKS,
                message=(
                    f"商品 ID {product_id} 上次只看到其他商品行；正在第 {query_attempt - 1}/"
                    f"{BAIYING_PUBLISH_VERIFY_RECHECKS} 次重查"
                ),
                product_id=product_id,
                product_title=product_title,
            )
            cart_page.wait_for_timeout(450)

        selected_row = None
        last_row_error = None
        row_deadline = time.monotonic() + (15 if query_attempt == 1 else 6)
        while time.monotonic() < row_deadline:
            try:
                selected_row, row_diagnostics = _locate_baiying_product_row(cart_page, product_id, product_title)
                break
            except WrongBaiyingProductRow as error:
                wrong_row_error = error
                last_row_error = error
                break
            except CandidateCartSkip:
                # 无库存等明确业务状态不属于暂时错行，不要重试或掩盖。
                raise
            except RuntimeError as error:
                visible_error = _visible_cart_error_message(cart_page)
                unavailable_reason = _candidate_unavailable_reason(visible_error)
                if unavailable_reason and _is_product_specific_cart_rejection(visible_error):
                    raise CandidateCartSkip(
                        product_id,
                        f"百应按商品 ID 查询后明确提示{unavailable_reason}：{_safe_diagnostic_text(visible_error)}",
                    ) from error
                if visible_error:
                    raise RuntimeError(
                        f"百应商品核验期间出现平台提示，保留原错误语义：{_safe_diagnostic_text(visible_error)}"
                    ) from error
                if _selection_error_requires_pause(error):
                    raise RuntimeError(
                        f"百应错行重查期间浏览器或账号状态异常，未按商品错行隔离：{error}"
                    ) from error
                last_row_error = error
                cart_page.wait_for_timeout(250)
        if selected_row is not None:
            if query_attempt > 1:
                _report_operation_progress(
                    progress,
                    "百应商品核验重查成功",
                    index=query_attempt - 1,
                    total=BAIYING_PUBLISH_VERIFY_RECHECKS,
                    message=f"第 {query_attempt} 次查询已唯一匹配商品 ID {product_id}，继续复制链接",
                    product_id=product_id,
                    product_title=product_title,
                )
            break
        if wrong_row_error is not None and query_attempt < attempts:
            _report_operation_progress(
                progress,
                "百应商品核验重查中",
                index=query_attempt,
                total=BAIYING_PUBLISH_VERIFY_RECHECKS,
                message=(
                    f"商品 ID {product_id} 查询结果仅包含其他商品；已确认错行证据，"
                    f"准备追加重查 {query_attempt}/{BAIYING_PUBLISH_VERIFY_RECHECKS}：{wrong_row_error.reason}"
                ),
                product_id=product_id,
                product_title=product_title,
            )
            continue
    if selected_row is None:
        after_query_snapshot = _baiying_row_snapshot(cart_page, max_rows=0)
        if wrong_row_error is not None:
            isolated = PublishVerificationIsolated(
                product_id,
                BAIYING_PUBLISH_VERIFY_RECHECKS,
                f"最后可见行与目标商品不匹配；重查前={before_query_snapshot}；重查后={after_query_snapshot}；"
                f"最后错行证据：{wrong_row_error.reason}",
            )
            _report_operation_progress(
                progress,
                "商品核验已隔离",
                index=BAIYING_PUBLISH_VERIFY_RECHECKS,
                total=BAIYING_PUBLISH_VERIFY_RECHECKS,
                message=f"{isolated.classification}；{isolated.warning}",
                product_id=product_id,
                product_title=product_title,
            )
            raise isolated from wrong_row_error
        if isinstance(last_row_error, CandidateCartSkip):
            raise last_row_error
        message = (
            f"按商品 ID 查询后未能唯一识别目标商品行；商品 ID={product_id}；"
            f"商品标题={product_title[:64]}；查询前={before_query_snapshot}；查询后={after_query_snapshot}；"
            f"{last_row_error or '等待商品行超时'}；未点击“去带货”"
        )
        # 发布队列的 platform_error 上限为 1000 字，为阶段前缀留空间，避免诊断尾部被落库截断。
        raise RuntimeError(message[:880] + ("…（诊断已截短）" if len(message) > 880 else "")) from last_row_error

    _open_baiying_product_content_menu(cart_page, selected_row, row_diagnostics)

    confirm_deadline = time.monotonic() + 10
    while time.monotonic() < confirm_deadline:
        copy_confirmation = cart_page.get_by_text("已成功复制商品链接", exact=False)
        if any(_is_visible(copy_confirmation.nth(index)) for index in range(copy_confirmation.count())):
            break
        cart_page.wait_for_timeout(200)
    else:
        raise RuntimeError("百应没有确认复制商品链接；未继续投稿或上传视频")
    copied_url = ""
    clipboard_error: Optional[Exception] = None
    for _ in range(20):
        try:
            copied_url = _extract_copied_product_url(_read_windows_clipboard_text(), product_id)
            break
        except RuntimeError as error:
            clipboard_error = error
            time.sleep(0.15)
    if not copied_url:
        if clipboard_error:
            raise clipboard_error
        raise RuntimeError("没有读取到百应复制的商品链接；未继续投稿")
    return cart_page, copied_url


def _visible_baiying_copy_dialog(cart_page: Page) -> Optional[Any]:
    """按复制成功提示定位当前弹窗，避免扫描全页同名按钮。"""
    try:
        markers = cart_page.get_by_text("已成功复制商品链接", exact=False)
        visible_markers = [markers.nth(index) for index in range(markers.count()) if _is_visible(markers.nth(index))]
        if len(visible_markers) != 1:
            return None
        handle = visible_markers[0].evaluate_handle("""element => {
          let fallback = null;
          for (let node = element; node && node !== document.body; node = node.parentElement) {
            const text = String(node.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text.includes('已成功复制商品链接')) continue;
            const cls = typeof node.className === 'string' ? node.className : '';
            if (!fallback && (node.matches('[role="dialog"], [aria-modal="true"]') || /modal|dialog/i.test(cls))) fallback = node;
            if (text.length <= 1500 && (text.includes('知道啦') || text.includes('我知道了') || text.includes('知道了'))) return node;
          }
          return fallback;
        }""")
        dialog = handle.as_element()
        return dialog if dialog is not None and _is_visible(dialog) else None
    except Exception:
        return None


def _baiying_creator_action_score(value: str) -> int:
    label = "".join(str(value or "").split())
    if not label or len(label) > 18:
        return 0
    if label in {"添品并创作", "添加并创作", "添加商品创作", "添加商品并创作"}:
        return 2
    return int("创作" in label and label.startswith(("添", "加", "去", "前往", "立即", "开始", "进入")))


def _is_baiying_creator_action_label(value: str) -> bool:
    return _baiying_creator_action_score(value) > 0


def _baiying_dialog_creator_actions(dialog: Any) -> list[Any]:
    """在复制弹窗内按操作语义查找 CTA；非标准按钮只回退到最内层可见短文案。"""
    scored_actions: list[tuple[int, Any]] = []
    for element in dialog.query_selector_all('button, [role="button"], a, input[type="button"], input[type="submit"]'):
        try:
            if not _is_visible(element) or not element.is_enabled():
                continue
            labels = element.evaluate("""node => [node.innerText, node.value, node.getAttribute('aria-label'), node.getAttribute('title')]""")
            score = max((_baiying_creator_action_score(str(label)) for label in labels if label), default=0)
            if score:
                scored_actions.append((score, element))
        except Exception:
            continue
    if scored_actions:
        top_score = max(score for score, _ in scored_actions)
        return [element for score, element in scored_actions if score == top_score]
    for element in dialog.query_selector_all("*"):
        try:
            if not _is_visible(element):
                continue
            metadata = element.evaluate("""node => {
              const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const label = normalize(node.innerText || node.textContent || '');
              const childHasSameText = Array.from(node.children || []).some(child =>
                normalize(child.innerText || child.textContent || '') === label);
              return {label, childHasSameText};
            }""")
            score = _baiying_creator_action_score(str(metadata.get("label") or ""))
            if not score or metadata.get("childHasSameText"):
                continue
            target_handle = element.evaluate_handle("""node => {
              for (let target = node, depth = 0; target && depth < 4; target = target.parentElement, depth++) {
                if (target.matches(':disabled, [aria-disabled="true"]')) return null;
                const propsKey = Object.keys(target).find(key => key.startsWith('__reactProps$'));
                const reactClick = propsKey && typeof target[propsKey]?.onClick === 'function';
                const cls = typeof target.className === 'string' ? target.className : '';
                const clickable = target.matches('button, [role="button"], a, input[type="button"], input[type="submit"]') ||
                  typeof target.onclick === 'function' || reactClick ||
                  getComputedStyle(target).cursor === 'pointer' || /(?:^|[-_\\s])(?:btn|button)(?:$|[-_\\s])/i.test(cls);
                if (clickable) return target;
              }
              return null;
            }""")
            target = target_handle.as_element()
            if target is not None and _is_visible(target) and target.is_enabled():
                scored_actions.append((score, target))
        except Exception:
            continue
    if not scored_actions:
        return []
    top_score = max(score for score, _ in scored_actions)
    return [element for score, element in scored_actions if score == top_score]


def _open_creator_directly(context: BrowserContext, page: Optional[Page] = None) -> Page:
    """复制链接已按商品 ID 核验后，使用同账号浏览器打开官方投稿地址。"""
    page = page or context.new_page()
    try:
        page.goto(DOUYIN_CREATOR_UPLOAD_URL, wait_until="commit", timeout=45_000)
    except Exception as error:
        raise RuntimeError(f"百应创作入口未能完成跳转，直接打开创作者中心也失败：{_safe_diagnostic_text(error, 300)}") from error
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _is_creator_publish_page(page.url) or any(marker in page.url.lower() for marker in ("/login", "/passport/")):
            page.bring_to_front()
            return page
        page.wait_for_timeout(250)
    raise RuntimeError(f"已直接打开创作者中心，但页面未进入投稿或登录地址：{urlparse(page.url).path[:100]}；未上传视频")


def _open_creator_from_baiying(
    context: BrowserContext,
    cart_page: Page,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Page:
    """优先使用复制弹窗的创作入口；入口变化时通过已核验链接进入官方投稿页。"""
    before_urls = {id(page): page.url for page in context.pages if not page.is_closed()}
    clicked = False
    click_error = ""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        dialog = _visible_baiying_copy_dialog(cart_page)
        if dialog is not None:
            try:
                actions = _baiying_dialog_creator_actions(dialog)
            except Exception:
                actions = []
            if len(actions) == 1:
                try:
                    actions[0].click(timeout=5_000)
                    clicked = True
                except Exception as error:
                    # 点击异常时页面可能已经跳转；先核对页面，不重复点击。
                    clicked = True
                    click_error = _safe_diagnostic_text(error, 180)
                break
        try:
            cart_page.wait_for_timeout(250)
        except Exception:
            break

    if clicked:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for page in context.pages:
                if page.is_closed():
                    continue
                target = _is_creator_publish_page(page.url) or any(
                    marker in page.url.lower() for marker in ("/login", "/passport/")
                )
                if target and (id(page) not in before_urls or before_urls[id(page)] != page.url):
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    except PlaywrightTimeoutError:
                        pass
                    page.bring_to_front()
                    return page
            try:
                cart_page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
        new_pages = [page for page in context.pages if not page.is_closed() and id(page) not in before_urls]
        changed_pages = [
            page for page in context.pages
            if not page.is_closed() and id(page) in before_urls and page.url != before_urls[id(page)]
        ]
        markers = cart_page.get_by_text("已成功复制商品链接", exact=False) if not cart_page.is_closed() else None
        dialog_still_visible = bool(markers is not None and any(
            _is_visible(markers.nth(index)) for index in range(markers.count())
        ))
        creator_intermediate = [
            page for page in [*new_pages, *changed_pages]
            if (page in new_pages or page is cart_page)
            and urlparse(page.url).hostname == "creator.douyin.com"
            and not _is_creator_publish_page(page.url)
            and not any(marker in page.url.lower() for marker in ("/login", "/passport/"))
        ]
        if len(creator_intermediate) == 1 and len(new_pages) + len(changed_pages) == 1:
            _report_operation_progress(
                progress, "打开创作者中心投稿页", message="已到创作者中心，正在进入视频投稿页"
            )
            return _open_creator_directly(context, creator_intermediate[0])
        if new_pages or changed_pages or not dialog_still_visible:
            observed = [urlparse(page.url).path[:80] for page in [*new_pages, *changed_pages]]
            raise RuntimeError(
                "点击百应创作入口后页面状态已变化，但未确认本次投稿页；"
                f"新增页 {len(new_pages)}，跳转页 {len(changed_pages)}，路径 {observed[:3]}"
                + (f"；点击异常：{click_error}" if click_error else "")
                + "；未上传视频，请核查当前浏览器页面。"
            )
        _report_operation_progress(progress, "打开创作者中心投稿页", message="百应创作入口未触发跳转，改用官方投稿地址")
    else:
        _report_operation_progress(progress, "打开创作者中心投稿页", message="百应弹窗创作入口未稳定出现，改用官方投稿地址")
    return _open_creator_directly(context)


def _dismiss_baiying_copy_dialog(cart_page: Page) -> None:
    dialog = _visible_baiying_copy_dialog(cart_page)
    if dialog is None:
        return
    dismiss_actions = []
    for element in dialog.query_selector_all('button, [role="button"], a'):
        if not _is_visible(element):
            continue
        label = element.evaluate("node => String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim()")
        if label in {"知道啦", "我知道了", "知道了"}:
            dismiss_actions.append(element)
    if len(dismiss_actions) != 1:
        raise RuntimeError(f"百应复制提示中确认按钮匹配 {len(dismiss_actions)} 个，无法安全关闭弹窗")
    dismiss_actions[0].click(timeout=5_000)


def _select_creator_cart_tag(page: Page) -> Any:
    """投稿页若尚未选购物车，先选中购物车标签，再定位商品链接框。"""
    field = _creator_product_link_field(page)
    if field is not None:
        return field

    positions = _visible_exact_text(page, "位置")
    if len(positions) != 1:
        raise RuntimeError("没有唯一识别到投稿页“添加标签”的当前类型；未关联商品或点击发布")
    positions[0].click(timeout=5_000)

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        options = _visible_exact_text(page, "购物车")
        if len(options) == 1:
            options[0].click(timeout=5_000)
            break
        if len(options) > 1:
            role_options = page.get_by_role("option", name="购物车", exact=True)
            if role_options.count() == 1 and _is_visible(role_options.first):
                role_options.first.click(timeout=5_000)
                break
            raise RuntimeError("投稿页“购物车”标签选项不唯一；未关联商品或点击发布")
        page.wait_for_timeout(200)
    else:
        visible_options: list[str] = []
        option_error = ""
        try:
            options = page.locator('[role="option"]')
            for index in range(min(options.count(), 20)):
                option = options.nth(index)
                if not _is_visible(option):
                    continue
                try:
                    label = " ".join((option.inner_text(timeout=1_000) or "").split())
                except Exception:
                    continue
                if label and label not in visible_options:
                    visible_options.append(label[:40])
                if len(visible_options) >= 8:
                    break
        except Exception as error:
            option_error = type(error).__name__
        option_hint = (
            f"；当前下拉可见选项：{'、'.join(visible_options)}" if visible_options
            else f"；下拉选项读取失败：{option_error}" if option_error
            else "；未识别到展开的下拉选项"
        )
        raise RuntimeError(
            "当前投稿页没有可见的“购物车”标签选项"
            f"（路径：{urlparse(str(getattr(page, 'url', ''))).path or '/'}{option_hint}）；未关联商品或点击发布"
        )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        field = _creator_product_link_field(page)
        if field is not None:
            return field
        page.wait_for_timeout(200)
    raise RuntimeError("选择购物车标签后没有出现商品链接输入框；未关联商品或点击发布")


def _creator_add_link_control(page: Page, field: Any) -> Any:
    """只在已核验的商品链接输入框所在行查找“添加链接”动作。"""
    row = field.locator("xpath=..")
    actions = _visible_exact_text(row, "添加链接")
    if len(actions) != 1:
        raise RuntimeError(f"商品链接输入框同一行的“添加链接”操作匹配 {len(actions)} 个")
    action = actions[0]
    if action.get_attribute("aria-disabled") == "true" or action.get_attribute("disabled") is not None:
        raise RuntimeError("商品链接输入框同一行的“添加链接”操作不可用")
    return action


def _creator_cart_link_context(field: Any) -> Dict[str, Any]:
    try:
        value = field.evaluate("""el => {
            for (let node = el.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
                const text = String(node.innerText || '').replace(/\\s+/g, ' ').trim();
                if (text.includes('购物车')) {
                    const exactTexts = Array.from(node.querySelectorAll('*'))
                      .map(child => String(child.innerText || child.textContent || '').replace(/\\s+/g, ' ').trim())
                      .filter(Boolean);
                    const hrefs = Array.from(node.querySelectorAll('a[href]')).map(link => String(link.href || '')).filter(Boolean);
                    return {text, exactTexts: Array.from(new Set(exactTexts)).slice(0, 100), hrefs: Array.from(new Set(hrefs)).slice(0, 20)};
                }
            }
            return {text: '', exactTexts: [], hrefs: []};
        }""")
        return value if isinstance(value, dict) else {"text": "", "exactTexts": [], "hrefs": []}
    except Exception:
        return {"text": "", "exactTexts": [], "hrefs": []}


def _creator_cart_context_matches_product(context: Dict[str, Any], product_id: str, product_name: str) -> bool:
    hrefs = context.get("hrefs", []) if isinstance(context, dict) else []
    if any(_product_url_matches_product(str(href), product_id) for href in hrefs if str(href)):
        return True
    exact_texts = context.get("exactTexts", []) if isinstance(context, dict) else []
    expected_title = " ".join(str(product_name or "").split())
    return bool(expected_title and any(" ".join(str(value).split()) == expected_title for value in exact_texts))


def _creator_resume_linked_short_title(
    card_hrefs: list[str], card_text: str, product_id: str, product_name: str,
    description: str = "", banned_words: Sequence[str] = (),
) -> Optional[str]:
    """续跑时只有商品 ID 与短标题都确认一致才复用现有关联，避免重复添加或错挂。"""
    id_matches = any(
        _product_url_matches_product(str(href), product_id) for href in card_hrefs if str(href)
    )
    if not id_matches and not card_text:
        return None
    if not id_matches:
        raise RuntimeError("现有投稿已有商品卡片，但未能确认它与本次商品 ID 一致；未重复添加链接或点击发布")
    if not card_text:
        raise RuntimeError("现有投稿已有商品链接，但未能确认对应商品卡片；未重复添加链接或点击发布")
    actual_short_title = _creator_card_short_title(card_text)
    expected_short_title = _creator_product_short_title(product_name, description, 10, banned_words)
    if actual_short_title != expected_short_title:
        raise RuntimeError("现有投稿已关联商品，但短标题未能核对；未重复添加链接或点击发布")
    return actual_short_title


def _creator_radio_state(locator: Any) -> Optional[bool]:
    """读取原生 radio、ARIA radio 或其可见标签的选中状态。"""
    try:
        return bool(locator.is_checked())
    except Exception:
        pass
    try:
        aria_checked = locator.get_attribute("aria-checked")
        if aria_checked in {"true", "false"}:
            return aria_checked == "true"
    except Exception:
        pass
    try:
        state = locator.evaluate(r"""el => {
            for (let node = el, depth = 0; node && depth < 7; node = node.parentElement, depth++) {
                const aria = node.getAttribute && node.getAttribute('aria-checked');
                if (aria === 'true' || aria === 'false') return aria === 'true';
                const marked = node.getAttribute && node.getAttribute('data-checked');
                if (marked === 'true' || marked === 'false') return marked === 'true';
                if (node.matches && node.matches('input[type="radio"], input[type="checkbox"]')) return !!node.checked;
                if (node.matches && node.matches('label')) {
                    const input = node.querySelector('input[type="radio"], input[type="checkbox"]');
                    if (input) return !!input.checked;
                }
                const className = typeof node.className === 'string' ? node.className.toLowerCase() : '';
                if (/\b(?:radio-)?checked\b/.test(className)) return true;
                if (/\b(?:radio-)?unchecked\b/.test(className)) return false;
            }
            return null;
        }""")
        return state if isinstance(state, bool) else None
    except Exception:
        return None


def _creator_scoped_publish_option(page: Page, name: str) -> Optional[Any]:
    """按发布设置字段分组找可见选项，兼容平台没有稳定 radio accessible name 的 DOM。"""
    groups = {
        "允许": ("保存权限", "不允许"),
        "立即发布": ("发布时间", "定时发布"),
    }
    group_label, alternative = groups.get(name, ("", ""))
    if not group_label:
        return None
    for label in _visible_exact_text(page, group_label):
        container = label
        for _ in range(8):
            try:
                container = container.locator("xpath=..")
                text = " ".join((container.inner_text(timeout=500) or "").split())
            except Exception:
                break
            if group_label not in text or alternative not in text:
                continue
            options = _visible_exact_text(container, name)
            alternatives = _visible_exact_text(container, alternative)
            if len(options) == 1 and len(alternatives) == 1:
                return options[0]
    return None


def _ensure_creator_radio(page: Page, name: str) -> None:
    radios = page.get_by_role("radio", name=name, exact=True)
    visible_radios = [radios.nth(index) for index in range(radios.count()) if _is_visible(radios.nth(index))]
    target = visible_radios[0] if len(visible_radios) == 1 else _creator_scoped_publish_option(page, name)
    if target is None:
        raise RuntimeError(f"发布设置中没有找到唯一可见的“{name}”选项；未点击发布")

    target.scroll_into_view_if_needed(timeout=3_000)
    if _creator_radio_state(target) is True:
        return
    try:
        target.check(timeout=3_000)
    except Exception:
        # check() 可能已经触发平台更新，先回读再决定是否点击，避免双击把选项取消。
        if _creator_radio_state(target) is not True:
            target.click(timeout=3_000)
    checked = None
    for _ in range(15):
        checked = _creator_radio_state(target)
        if checked is not False:
            break
        page.wait_for_timeout(100)
    if checked is False:
        raise RuntimeError(f"点击后“{name}”仍未选中；未点击发布")
    # 某些平台版本只暴露视觉标签，不暴露 checked/aria-checked；唯一字段分组中的选项
    # 已被显式点击且未报错，按用户确认的默认设置继续提交，避免无障碍树缺失造成假阻断。


class PublishSubmissionError(RuntimeError):
    """最终发布按钮操作发生异常，并标记是否已进入可能提交阶段。"""

    def __init__(self, message: str, submission_attempted: bool) -> None:
        super().__init__(message)
        self.submission_attempted = submission_attempted


class CreatorCoverRequirementBlocked(PublishSubmissionError):
    """平台明确因缺少封面阻止提交；隔离当前任务并以弱提示留痕。"""

    classification = "CREATOR_COVER_REQUIRED_ISOLATED"

    def __init__(self, message: str, submission_attempted: bool) -> None:
        self.warning = (
            "平台明确要求封面；本任务未记录为发布成功，正在隔离处理。请人工检查创作者中心后处理。"
            f" 平台提示：{_safe_diagnostic_text(message, 500)}"
        )
        super().__init__(self.warning, submission_attempted)


def _creator_cover_requirement_blocked(text: str) -> bool:
    compact = " ".join(str(text or "").split())
    return any(marker in compact for marker in (
        "横/竖双封面缺失",
        "请上传封面",
        "请先上传封面",
        "请设置封面",
        "请先设置封面",
        "请添加封面",
        "请先添加封面",
        "请选择封面",
        "封面不能为空",
        "封面必填",
        "缺少封面",
        "封面未设置",
        "未设置封面",
        "需要设置封面",
    ))


def _publish_creator_post(
    page: Page,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[str, str]:
    _report_operation_progress(progress, "提交抖音发布", message="已完成视频和商品配置，正在提交发布")
    publish_buttons = page.get_by_role("button", name="发布", exact=True)
    publish_button_count = publish_buttons.count()
    if publish_button_count == 0:
        try:
            current_text = " ".join((page.locator("body").inner_text(timeout=1_000) or "").split())
        except Exception:
            current_text = ""
        if _creator_cover_requirement_blocked(current_text):
            raise CreatorCoverRequirementBlocked(current_text, False)
        raise PublishSubmissionError("没有唯一可用的最终“发布”按钮；已停止，未重复提交", False)
    if publish_button_count != 1:
        raise PublishSubmissionError("没有唯一可用的最终“发布”按钮；已停止，未重复提交", False)
    if not publish_buttons.first.is_enabled():
        try:
            current_text = " ".join((page.locator("body").inner_text(timeout=1_000) or "").split())
        except Exception:
            current_text = ""
        if _creator_cover_requirement_blocked(current_text):
            raise CreatorCoverRequirementBlocked(current_text, False)
        raise PublishSubmissionError("最终“发布”按钮不可用；已停止，未点击发布", False)
    try:
        publish_buttons.first.scroll_into_view_if_needed(timeout=3_000)
    except Exception as error:
        raise PublishSubmissionError(f"最终发布按钮无法定位；未点击发布。原因：{error}", False) from error
    try:
        initial_text = " ".join((page.locator("body").inner_text(timeout=1_000) or "").split())
    except Exception:
        initial_text = ""
    try:
        # 点击调用本身发生超时也视为可能已提交，禁止自动再次点击。
        publish_buttons.first.click(timeout=10_000)
    except Exception as error:
        raise PublishSubmissionError(f"点击最终发布按钮时结果不确定：{error}", True) from error

    try:
        deadline = time.monotonic() + 60
        confirmation_clicked = False
        latest_text = ""
        success_markers = ("发布成功", "作品已发布", "作品发布成功", "发布完成")
        submitted_markers = ("提交成功", "作品已提交", "已提交审核", "提交审核中", "作品审核中")
        failure_markers = ("发布失败", "提交失败", "网络错误，请重试", "作品发布失败")
        last_progress_at = 0.0
        while time.monotonic() < deadline:
            try:
                for index in range(page.get_by_role("dialog").count()):
                    dialog = page.get_by_role("dialog").nth(index)
                    if not _is_visible(dialog):
                        continue
                    dialog_text = " ".join((dialog.inner_text(timeout=500) or "").split())
                    if "确认发布" in dialog_text and not confirmation_clicked:
                        confirm = dialog.get_by_role("button", name="确认发布", exact=True)
                        if confirm.count() == 1 and confirm.first.is_enabled():
                            confirm.first.click(timeout=5_000)
                            confirmation_clicked = True
                            break
                latest_text = " ".join((page.locator("body").inner_text(timeout=1000) or "").split())
            except Exception:
                latest_text = ""
            if any(marker in latest_text and marker not in initial_text for marker in submitted_markers):
                _report_operation_progress(progress, "抖音已接收作品", percent=100, message="平台已返回审核/提交回执")
                return "submitted_review", latest_text[:800]
            if any(marker in latest_text and marker not in initial_text for marker in success_markers):
                _report_operation_progress(progress, "抖音发布成功", percent=100, message="已识别平台成功回执")
                return "published", latest_text[:800]
            if (
                _creator_cover_requirement_blocked(latest_text)
                and not _creator_cover_requirement_blocked(initial_text)
            ):
                raise CreatorCoverRequirementBlocked(latest_text, True)
            if any(marker in latest_text and marker not in initial_text for marker in failure_markers):
                raise RuntimeError("创作者中心返回发布失败；请检查页面，不要自动重试")
            if time.monotonic() - last_progress_at >= 2:
                _report_operation_progress(progress, "等待抖音发布回执", message="已提交，正在等待平台确认")
                last_progress_at = time.monotonic()
            page.wait_for_timeout(1000)
        raise RuntimeError(
            "点击发布后 60 秒内没有识别到平台成功回执；发布状态不确定，请检查创作者中心，禁止自动重发。"
            + (f" 页面提示：{latest_text[:240]}" if latest_text else "")
        )
    except Exception as error:
        if isinstance(error, PublishSubmissionError):
            raise
        raise PublishSubmissionError(str(error), True) from error


def _creator_upload_complete(page: Page, video_path: Path) -> tuple[bool, str]:
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return False, ""
    compact = " ".join(body.split())
    if any(marker in compact for marker in ("上传失败", "上传出错", "视频上传失败")):
        raise RuntimeError("创作者中心提示视频上传失败；已保留页面供检查，未点击发布")
    if _creator_upload_in_progress(compact):
        return False, compact[:800]
    has_video_preview = False
    try:
        has_video_preview = page.locator("video").count() > 0
    except Exception:
        pass
    has_progress = False
    try:
        has_progress = any(
            _is_visible(page.locator(selector).nth(index))
            for selector in ("[role='progressbar']", "progress")
            for index in range(page.locator(selector).count())
        )
    except Exception:
        pass
    has_completion_text = any(marker in compact for marker in ("上传完成", "上传成功", "发布设置", "发布作品"))
    has_file_name = video_path.name in compact
    has_description_field = _creator_description_field(page) is not None
    if has_video_preview and not has_progress and (has_completion_text or has_file_name or has_description_field):
        return True, compact[:800]
    return False, compact[:800]


def _discard_creator_unpublished_video_draft(page: Page, *, preserve_existing: bool = False) -> bool:
    """新任务不复用创作者中心旧草稿；只在精确提示和按钮均唯一时放弃旧草稿。"""
    prompt_marker = "你还有上次未发布的视频"
    try:
        body_text = page.locator("body").inner_text(timeout=1_500)
    except Exception as error:
        raise RuntimeError(f"无法读取创作者中心页面状态；未处理草稿或上传视频。原因：{error}") from error
    if prompt_marker not in body_text:
        return False
    if preserve_existing:
        raise RuntimeError("创作者中心存在未发布草稿；手动重发未放弃草稿或重新上传，请先核对该草稿")

    abandon_buttons = page.get_by_role("button", name="放弃编辑", exact=True)
    continue_buttons = page.get_by_role("button", name="继续编辑", exact=True)
    if abandon_buttons.count() != 1 or continue_buttons.count() != 1:
        raise RuntimeError("检测到创作者中心未发布草稿，但“放弃编辑/继续编辑”按钮不唯一；未上传视频")
    abandon = abandon_buttons.first
    continue_edit = continue_buttons.first
    if not _is_visible(abandon) or not _is_visible(continue_edit):
        raise RuntimeError("检测到创作者中心未发布草稿，但处理按钮当前不可见；未上传视频")
    if not abandon.is_enabled():
        raise RuntimeError("创作者中心“放弃编辑”按钮不可用；未上传视频")

    # 用户已明确选择丢弃旧草稿；绝不点击“继续编辑”，只从当前队列任务重新开始。
    abandon.click(timeout=5_000)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=1_000)
        except Exception:
            body_text = ""
        if prompt_marker not in body_text:
            if not _is_creator_publish_page(page.url):
                raise RuntimeError("放弃旧草稿后创作者中心投稿页已变化；当前任务未上传")
            return True
        page.wait_for_timeout(200)
    raise RuntimeError("点击“放弃编辑”后草稿提示仍存在；未上传当前队列视频")


def _creator_upload_in_progress(text: str) -> bool:
    """排除投稿页提示气泡中的静态“点击发布后……上传中”说明。"""
    compact = " ".join(str(text or "").split())
    compact = re.sub(r"点击发布后[^。]*(?:上传中|正在上传|处理中|视频校验中)[^。]*。", "", compact)
    return any(marker in compact for marker in ("上传中", "正在上传", "处理中", "视频校验中"))


def _creator_upload_progress_percent(page: Page) -> Optional[int]:
    """只读取页面可见进度控件的数值；没有可验证数值时返回 None。"""
    try:
        for selector in ("[role='progressbar']", "progress"):
            locator = page.locator(selector)
            for index in range(locator.count()):
                element = locator.nth(index)
                if not _is_visible(element):
                    continue
                current = element.get_attribute("aria-valuenow") or element.get_attribute("value")
                maximum = element.get_attribute("aria-valuemax") or element.get_attribute("max") or "100"
                value_number = float(current) if current is not None else None
                max_number = float(maximum)
                if value_number is not None and max_number > 0:
                    return max(0, min(100, round(value_number * 100 / max_number)))
    except Exception:
        return None
    return None


def _creator_ai_cover_section(page: Page) -> tuple[Optional[Any], str]:
    """定位 AI 推荐封面区，兼容平台用图片或 CSS 背景绘制候选卡片。"""
    labels = page.get_by_text("AI智能推荐封面", exact=False)
    candidates: list[tuple[int, Any, str]] = []
    diagnostics: list[str] = []
    for index in range(labels.count()):
        label = labels.nth(index)
        if not _is_visible(label):
            continue
        section = label.locator("xpath=..")
        for depth in range(10):
            if not _is_visible(section):
                break
            try:
                metrics = section.evaluate("""root => {
                    const visible = el => {
                        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const images = Array.from(root.querySelectorAll('img')).filter(visible);
                    const backgrounds = Array.from(root.querySelectorAll('*')).filter(el => {
                        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                        return rect.width >= 35 && rect.height >= 35 && visible(el) && style.backgroundImage && style.backgroundImage !== 'none';
                    });
                    const text = String(root.innerText || '').replace(/\\s+/g, ' ').trim();
                    return {text, images: images.length, backgrounds: backgrounds.length};
                }""")
                text = str(metrics.get("text") or "")
                if "AI智能推荐封面" in text:
                    diagnostics.append(f"文本候选 {index + 1} 第 {depth + 1} 层：img={metrics.get('images', 0)}, 背景图={metrics.get('backgrounds', 0)}")
                    visual_count = max(int(metrics.get("images") or 0), int(metrics.get("backgrounds") or 0))
                    if visual_count >= 2:
                        candidates.append((depth, section, f"img={metrics.get('images', 0)}, 背景图={metrics.get('backgrounds', 0)}"))
                        break
            except Exception as error:
                diagnostics.append(f"文本候选 {index + 1} 第 {depth + 1} 层读取失败：{error}")
            section = section.locator("xpath=..")

    if not candidates:
        visible_count = sum(_is_visible(labels.nth(index)) for index in range(labels.count()))
        summary = "; ".join(diagnostics[-4:]) or "未找到可见标题或候选图节点"
        return None, f"可见标题数={visible_count}；{summary}"
    nearest_depth = min(item[0] for item in candidates)
    nearest = [item for item in candidates if item[0] == nearest_depth]
    if len(nearest) != 1:
        return None, f"候选封面区有歧义：匹配到 {len(nearest)} 个同层区域；" + "; ".join(diagnostics[-4:])
    return nearest[0][1], nearest[0][2]


def _creator_ai_cover_selection(section: Any) -> Dict[str, Any]:
    """从图片或 CSS 背景缩略图及卡片样式读取封面选中证据。"""
    try:
        result = section.evaluate("""root => {
            const visible = el => {
                const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const images = Array.from(root.querySelectorAll('img')).filter(visible);
            const backgrounds = Array.from(root.querySelectorAll('*')).filter(el => {
                const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                return rect.width >= 35 && rect.height >= 35 && visible(el) && style.backgroundImage && style.backgroundImage !== 'none';
            });
            const options = images.length >= 2 ? images : backgrounds;
            options.forEach((el, index) => el.setAttribute('data-codex-ai-cover-option', String(index)));
            const evidenceFor = option => {
                let node = option;
                for (let depth = 0; node && node !== root && depth < 6; depth++, node = node.parentElement) {
                    if (node !== option) {
                        const nestedImages = Array.from(node.querySelectorAll('img')).filter(visible).length;
                        const nestedBackgrounds = Array.from(node.querySelectorAll('*')).filter(el => {
                            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                            return rect.width >= 35 && rect.height >= 35 && visible(el) && style.backgroundImage && style.backgroundImage !== 'none';
                        }).length;
                        if (Math.max(nestedImages, nestedBackgrounds) > 1) break;
                    }
                    const cls = String(node.className && node.className.baseVal || node.className || '');
                    const attrs = ['aria-checked', 'aria-selected', 'data-selected', 'data-state']
                        .map(name => String(node.getAttribute(name) || '').toLowerCase());
                    if (attrs.some(value => ['true', 'checked', 'selected'].includes(value))) return 'aria/data-selected';
                    if (/(selected|checked|active|current)/i.test(cls)) return 'selected-class';
                    const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''}`;
                    if (/已选中|当前封面|已选择/.test(label)) return 'selected-label';
                    const check = node.querySelector('svg[class*="check" i], [class*="checkmark" i], [class*="checked" i], [aria-label*="选中"]');
                    if (check) return 'check-indicator';
                    const style = getComputedStyle(node);
                    if (/rgb\\(\\s*254\\s*,\\s*44\\s*,\\s*85\\s*\\)|rgb\\(\\s*255\\s*,\\s*0\\s*,\\s*0\\s*\\)/i.test(style.borderColor)) return 'selected-border';
                }
                return '';
            };
            const sourceFor = el => el.tagName === 'IMG'
                ? String(el.currentSrc || el.src || '')
                : String(getComputedStyle(el).backgroundImage || '');
            return {
                count: options.length,
                kind: images.length >= 2 ? 'img' : 'background',
                options: options.map((el, index) => ({index, source: sourceFor(el)})),
                selected: options.map((el, index) => ({index, evidence: evidenceFor(el)})).filter(item => item.evidence)
            };
        }""")
        return result if isinstance(result, dict) else {"count": 0, "selected": []}
    except Exception as error:
        return {"count": 0, "selected": [], "error": str(error)}


def _creator_wait_ai_cover_selection(
    page: Page,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    timeout_ms: int = 60_000,
) -> tuple[Optional[Dict[str, Any]], str]:
    """等待唯一可见 AI 封面区；保留唯一已选项，否则点首张并回读选中证据。"""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    last_summary = "尚未出现带至少两张候选图的 AI 推荐封面区"
    last_report_at = 0.0
    while time.monotonic() <= deadline:
        section, section_summary = _creator_ai_cover_section(page)
        last_summary = section_summary
        if section is not None:
            state = _creator_ai_cover_selection(section)
            try:
                count = int(state.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if count >= 2:
                selected = state.get("selected") if isinstance(state.get("selected"), list) else []
                if len(selected) > 1:
                    return None, f"AI 推荐封面已有多个选中项，无法唯一确认；{section_summary}"
                if len(selected) == 1:
                    index = selected[0].get("index") if isinstance(selected[0], dict) else None
                    options = state.get("options") if isinstance(state.get("options"), list) else []
                    option = next((item for item in options if isinstance(item, dict) and item.get("index") == index), {})
                    return {
                        "index": index,
                        "source": str(option.get("source") or ""),
                        "selectedEvidence": str(selected[0].get("evidence") or ""),
                        "selectedBeforeClick": True,
                        "section": section,
                    }, section_summary

                first = section.locator('[data-codex-ai-cover-option="0"]')
                if first.count() != 1:
                    return None, f"AI 推荐封面首张候选定位不唯一（{first.count()} 个）；{section_summary}"
                try:
                    first.click(force=True, timeout=5_000, no_wait_after=True)
                except Exception as error:
                    return None, f"点击首张 AI 推荐封面失败：{error}；{section_summary}"

                confirm_deadline = time.monotonic() + 5
                while time.monotonic() <= confirm_deadline:
                    confirmed_section, confirmed_summary = _creator_ai_cover_section(page)
                    if confirmed_section is not None:
                        confirmed = _creator_ai_cover_selection(confirmed_section)
                        picked = confirmed.get("selected") if isinstance(confirmed.get("selected"), list) else []
                        if len(picked) == 1:
                            options = confirmed.get("options") if isinstance(confirmed.get("options"), list) else []
                            picked_index = picked[0].get("index") if isinstance(picked[0], dict) else None
                            option = next((item for item in options if isinstance(item, dict) and item.get("index") == picked_index), {})
                            return {
                                "index": picked_index,
                                "source": str(option.get("source") or ""),
                                "selectedEvidence": str(picked[0].get("evidence") or ""),
                                "selectedBeforeClick": False,
                                "section": confirmed_section,
                            }, confirmed_summary
                        if len(picked) > 1:
                            return None, f"点击首张候选后选中态有歧义；{confirmed_summary}"
                    page.wait_for_timeout(200)
                return None, f"点击首张 AI 推荐封面后未读回唯一选中态；{section_summary}"

        now = time.monotonic()
        if progress and now - last_report_at >= 5:
            _report_operation_progress(
                progress,
                "等待平台 AI 推荐封面",
                message="等待唯一可见的 AI 推荐封面区加载候选图（最多 60 秒）",
            )
            last_report_at = now
        page.wait_for_timeout(250)
    return None, f"等待 AI 推荐封面区超时；{last_summary}"


def _creator_cover_frame_slots(page: Page) -> Dict[str, Dict[str, Any]]:
    """读取投稿页的 3:4 / 4:3 封面槽位及当前图片加载状态。"""
    slots: Dict[str, Dict[str, Any]] = {}
    cards = page.locator('[class*="coverControl-"]')
    for index in range(cards.count()):
        card = cards.nth(index)
        if not _is_visible(card):
            continue
        try:
            state = card.evaluate("""el => {
                const visible = node => {
                    const style = getComputedStyle(node), rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const text = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
                const images = Array.from(el.querySelectorAll('img')).filter(visible).map(img => ({
                    src: img.currentSrc || img.src || '', width: img.naturalWidth || 0, height: img.naturalHeight || 0
                }));
                return {text, images};
            }""")
        except Exception:
            continue
        text = " ".join(str(state.get("text") or "").split())
        orientation = (
            "portrait" if "竖封面3:4" in text
            else "landscape" if "横封面4:3" in text
            else ""
        )
        if not orientation:
            continue
        slots.setdefault(orientation, {
            "locator": card,
            "text": text,
            "images": state.get("images", []),
            "ready": any(
                isinstance(image, dict)
                and int(image.get("width") or 0) > 0
                and int(image.get("height") or 0) > 0
                and str(image.get("src") or "").strip()
                for image in state.get("images", [])
            ),
        })
    return slots


def _creator_cover_slots_complete(slots: Dict[str, Dict[str, Any]]) -> bool:
    """正式槽位必须同时有 3:4 竖图和 4:3 横图。"""
    return all(
        isinstance(slots.get(orientation), dict) and slots[orientation].get("ready") is True
        for orientation in ("portrait", "landscape")
    )


def _creator_cover_assistant_status(page: Page) -> Dict[str, Any]:
    try:
        body = " ".join((page.locator("body").inner_text(timeout=2_000) or "").split())
        readable = True
    except Exception:
        body = ""
        readable = False
    warnings = [marker for marker in ("封面边框面积过大", "封面不佳") if marker in body]
    return {
        "readable": readable,
        "doubleCoverMissingVisible": "横/竖双封面缺失" in body,
        "qualityWarnings": warnings,
    }


def _creator_cover_ratio_matches(value: Any, target_aspect: float) -> bool:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return False
    return ratio > 0 and abs(ratio - target_aspect) <= max(0.09, target_aspect * 0.08)


def _creator_wait_for_cover_frame_confirmation(
    page: Page,
    orientation: str,
    *,
    timeout_ms: int = 6_000,
) -> Dict[str, Any]:
    """等到目标模式与目标比例预览都被 DOM 证据确认，或返回最后探针。"""
    config = _CREATOR_COVER_FRAME_CONFIGS[orientation]
    marker = "hasPortraitMode" if orientation == "portrait" else "hasLandscapeMode"
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    state: Dict[str, Any] = {"editorVisible": False, "previewCandidates": []}
    while True:
        state = _creator_cover_editor_metrics(page, float(config["aspect"]))
        if (
            state.get("editorVisible")
            and (state.get("markers") or {}).get(marker)
            and isinstance(state.get("previewAspect"), dict)
        ):
            return state
        if time.monotonic() >= deadline:
            return state
        page.wait_for_timeout(150)


def _creator_cover_editor_state_from_probe(probe: Any, aspect: float) -> Dict[str, Any]:
    """把编辑器探针规范化；保留不匹配的可见预览，避免空数组掩盖 DOM 结构。"""
    if not isinstance(probe, dict):
        return {"editorVisible": False, "error": "封面编辑器 DOM 探针没有返回对象"}

    raw_modes = probe.get("modeControls") if isinstance(probe.get("modeControls"), dict) else {}
    modes: Dict[str, Dict[str, Any]] = {}
    for orientation in ("portrait", "landscape"):
        raw = raw_modes.get(orientation) if isinstance(raw_modes.get(orientation), dict) else {}
        modes[orientation] = {
            "present": raw.get("present") is True,
            "selected": raw.get("selected") if raw.get("selected") in (True, False) else None,
            "selectionKnown": raw.get("selectionKnown") is True,
            "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), list) else [],
        }

    candidates: list[Dict[str, Any]] = []
    for raw in probe.get("previewCandidates", []) if isinstance(probe.get("previewCandidates"), list) else []:
        if not isinstance(raw, dict):
            continue
        item: Dict[str, Any] = {}
        for key in ("tag", "id", "className", "selectorHint", "declaredAspect"):
            value = raw.get(key)
            if isinstance(value, (str, int, float)):
                item[key] = str(value)[:180] if key != "declaredAspect" else value
        for key in ("width", "height", "ratio"):
            try:
                value = float(raw.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                item[key] = round(value, 3)
        if "ratio" not in item and item.get("width") and item.get("height"):
            item["ratio"] = round(item["width"] / item["height"], 3)
        item["usableForAspect"] = raw.get("usableForAspect") is True
        candidates.append(item)

    def candidate_distance(item: Dict[str, Any]) -> float:
        values = [item.get("ratio"), item.get("declaredAspect")]
        ratios = []
        for value in values:
            try:
                ratio = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(ratio) and ratio > 0:
                ratios.append(ratio)
        return min((min(abs(ratio - 0.75), abs(ratio - 4 / 3)) for ratio in ratios), default=9.0)

    candidates.sort(key=candidate_distance)
    candidates = candidates[:12]

    def has_preview_aspect(target: float) -> bool:
        for candidate in candidates:
            if not candidate.get("usableForAspect"):
                continue
            if _creator_cover_ratio_matches(candidate.get("ratio"), target):
                return True
            if _creator_cover_ratio_matches(candidate.get("declaredAspect"), target):
                return True
        return False

    portrait_selected = modes["portrait"]["selected"]
    landscape_selected = modes["landscape"]["selected"]
    if portrait_selected is True and landscape_selected is not True:
        has_portrait = True
    elif modes["portrait"]["selectionKnown"] and portrait_selected is False:
        has_portrait = False
    elif landscape_selected is True:
        has_portrait = False
    else:
        has_portrait = has_preview_aspect(0.75)
    if landscape_selected is True and portrait_selected is not True:
        has_landscape = True
    elif modes["landscape"]["selectionKnown"] and landscape_selected is False:
        has_landscape = False
    elif portrait_selected is True:
        has_landscape = False
    else:
        has_landscape = has_preview_aspect(4 / 3)

    target_orientation = (
        "portrait" if _creator_cover_ratio_matches(aspect, 0.75)
        else "landscape" if _creator_cover_ratio_matches(aspect, 4 / 3)
        else ""
    )
    target_mode = modes.get(target_orientation, {})
    target_mode_inactive = target_mode.get("selectionKnown") and target_mode.get("selected") is False
    matching = [] if target_mode_inactive else [
        candidate for candidate in candidates
        if candidate.get("usableForAspect")
        and (
            _creator_cover_ratio_matches(candidate.get("ratio"), aspect)
            or _creator_cover_ratio_matches(candidate.get("declaredAspect"), aspect)
        )
    ]
    matching.sort(key=lambda item: min(
        abs(float(item.get("ratio", aspect)) - aspect)
        if _creator_cover_ratio_matches(item.get("ratio"), aspect) else float("inf"),
        abs(float(item.get("declaredAspect", aspect)) - aspect)
        if _creator_cover_ratio_matches(item.get("declaredAspect"), aspect) else float("inf"),
    ))
    preview_aspect = matching[0] if matching else None

    dom_evidence = probe.get("domEvidence") if isinstance(probe.get("domEvidence"), dict) else {}
    root = dom_evidence.get("root") if isinstance(dom_evidence.get("root"), dict) else {}
    root_text = str(root.get("text") or "")
    return {
        "editorVisible": probe.get("editorVisible") is True,
        "markers": {
            "hasPortraitMode": has_portrait,
            "hasLandscapeMode": has_landscape,
            "hasDetect": probe.get("hasDetect") is True,
            "hasDone": probe.get("hasDone") is True,
            "portraitControlPresent": modes["portrait"]["present"],
            "landscapeControlPresent": modes["landscape"]["present"],
            "activeMode": (
                "portrait" if portrait_selected is True and landscape_selected is not True
                else "landscape" if landscape_selected is True and portrait_selected is not True
                else None
            ),
        },
        "modeControls": modes,
        "previewAspect": preview_aspect,
        "previewCandidates": candidates,
        "domEvidence": dom_evidence,
        "rootText": root_text[:600],
    }


def _creator_cover_editor_metrics(page: Page, aspect: float) -> Dict[str, Any]:
    """读取编辑器活动比例控件及预览 DOM；候选列表包含不匹配比例的可见对象。"""
    try:
        probe = page.evaluate("""targetAspect => {
            const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
            const visible = el => {
                const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const modeSpecs = {
                portrait: {label: '设置竖封面', preview: /竖封面预览.{0,8}3[:：]4/},
                landscape: {label: '设置横封面', preview: /横封面预览.{0,8}4[:：]3/}
            };
            const textOf = el => normalize(el && (el.innerText || el.textContent));
            const hasModeText = el => Object.values(modeSpecs).some(spec => textOf(el).includes(spec.label) || spec.preview.test(textOf(el)));
            const dialogSelector = '[role="dialog"],[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"]';
            const dialogCandidates = Array.from(document.querySelectorAll(dialogSelector))
                .filter(el => visible(el) && textOf(el).includes('完成') && hasModeText(el));
            const doneLeaves = Array.from(document.querySelectorAll('body *'))
                .filter(el => visible(el) && el.children.length === 0 && textOf(el) === '完成');
            const ancestorCandidates = new Set();
            for (const leaf of doneLeaves) {
                for (let node = leaf, depth = 0; node && node !== document.body && depth < 14; node = node.parentElement, depth++) {
                    if (visible(node) && hasModeText(node) && textOf(node).includes('完成')) ancestorCandidates.add(node);
                }
            }
            const roots = [...new Set([...dialogCandidates, ...ancestorCandidates])];
            const containsBothModes = el => Object.values(modeSpecs).every(spec => textOf(el).includes(spec.label) || spec.preview.test(textOf(el)));
            const bothModeRoots = roots.filter(containsBothModes);
            const rootArea = el => { const r = el.getBoundingClientRect(); return r.width * r.height; };
            // 优先选同时包含两种模式和“完成”的编辑器根；旧逻辑取最小后代，可能只看到横向标签。
            const preferredRoots = bothModeRoots.length ? bothModeRoots : roots;
            preferredRoots.sort((a, b) => rootArea(a) - rootArea(b));
            const root = preferredRoots[0] || null;
            const scope = root ? [root, ...Array.from(root.querySelectorAll('*'))] : [];
            const rootText = root ? textOf(root) : '';
            const hasDone = Boolean(root) && rootText.includes('完成');
            const classText = el => typeof el.className === 'string' ? el.className : String(el.className && el.className.baseVal || '');
            const selectedSignal = el => {
                const attrs = ['aria-selected','aria-pressed','aria-checked','aria-current','data-selected','data-state','data-active']
                    .map(name => String(el.getAttribute(name) || '').toLowerCase());
                const activeValues = ['true','page','checked','selected','active','on','open'];
                const knownValues = [...activeValues, 'false','inactive','unselected','off','closed','unchecked'];
                const attrSelected = attrs.some(value => activeValues.includes(value));
                const cls = classText(el);
                const classHasState = /active|inactive|selected|current|checked/i.test(cls);
                const classSelected = /(^|[\\s_-])(active|selected|current|checked)(?:$|[\\s_-])|(?:Active|Selected|Current|Checked)/.test(cls);
                const attributeHasState = attrs.some(value => knownValues.includes(value));
                return {selected: attrSelected || classSelected, known: attributeHasState || classHasState, attrs, className: cls};
            };
            const readMode = orientation => {
                const spec = modeSpecs[orientation];
                const leaves = scope.filter(el => visible(el) && el.children.length === 0 &&
                    (textOf(el) === spec.label || spec.preview.test(textOf(el))));
                const targets = new Set();
                for (const leaf of leaves) {
                    let target = leaf;
                    for (let depth = 0; target && target !== root && depth < 6; target = target.parentElement, depth++) {
                        if (target.matches('button,[role="tab"],[role="button"],a,[tabindex],label') || /tab|switch|mode|option/i.test(classText(target))) break;
                    }
                    targets.add(target);
                }
                const evidence = Array.from(targets).filter(Boolean).slice(0, 4).map(el => {
                    const signal = selectedSignal(el);
                    return {
                        tag: el.tagName,
                        role: el.getAttribute('role') || '',
                        className: signal.className.slice(0, 180),
                        ariaSelected: el.getAttribute('aria-selected') || '',
                        ariaPressed: el.getAttribute('aria-pressed') || '',
                        dataState: el.getAttribute('data-state') || '',
                        dataActive: el.getAttribute('data-active') || '',
                        selected: signal.selected,
                        selectionKnown: signal.known,
                        text: textOf(el).slice(0, 100)
                    };
                });
                const selected = evidence.filter(item => item.selected).length;
                const known = evidence.filter(item => item.selectionKnown).length;
                return {
                    present: leaves.length > 0,
                    selected: selected === 1 ? true : selected > 1 ? null : known > 0 ? false : null,
                    selectionKnown: known > 0,
                    evidence
                };
            };
            const parseRatio = value => {
                const text = String(value || '').trim();
                const pair = text.match(/(\\d+(?:\\.\\d+)?)\\s*[:/]\\s*(\\d+(?:\\.\\d+)?)/);
                if (pair) return Number(pair[1]) / Number(pair[2]);
                const number = Number(text);
                return Number.isFinite(number) && number > 0 ? number : null;
            };
            const candidates = [];
            const seen = new Set();
            const selectorHint = 'canvas,video,img,[role="img"],[style*="aspect-ratio"],[data-ratio],[data-aspect-ratio],[class*="preview"],[class*="cover"],[class*="crop"],[class*="frame"]';
            for (const el of scope) {
                if (!visible(el) || seen.has(el)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 50 || rect.height < 60 || rect.width * rect.height < 3_000) continue;
                const cls = classText(el), id = String(el.id || '');
                const style = getComputedStyle(el);
                const mediaTag = ['CANVAS','VIDEO','IMG'].includes(el.tagName);
                const hasDeclaredAspect = style.aspectRatio !== 'auto' || el.hasAttribute('data-ratio') || el.hasAttribute('data-aspect-ratio');
                const semanticSurface = /preview|cover|crop|frame|video|media/i.test(`${cls} ${id}`) ||
                    style.backgroundImage !== 'none' || style.aspectRatio !== 'auto' ||
                    el.getAttribute('role') === 'img' || el.hasAttribute('data-ratio') || el.hasAttribute('data-aspect-ratio');
                const unclassifiedLargeSurface = !mediaTag && !semanticSurface && el !== root &&
                    rect.width >= 100 && rect.height >= 120 && rect.width * rect.height >= 15_000 && textOf(el).length < 80;
                if (!mediaTag && !semanticSurface && !unclassifiedLargeSurface) continue;
                seen.add(el);
                const declaredAspect = parseRatio(el.getAttribute('data-aspect-ratio') || el.getAttribute('data-ratio') || style.aspectRatio);
                const ratio = rect.width / rect.height;
                const namedPreview = /preview|crop|frame|video|media/i.test(`${cls} ${id}`);
                candidates.push({
                    tag: el.tagName,
                    id: id.slice(0, 100),
                    className: cls.slice(0, 180),
                    selectorHint,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    ratio: Number(ratio.toFixed(3)),
                    declaredAspect: declaredAspect && Number.isFinite(declaredAspect) ? Number(declaredAspect.toFixed(3)) : null,
                    usableForAspect: !unclassifiedLargeSurface && (
                        el.tagName === 'CANVAS' || el.tagName === 'VIDEO' || hasDeclaredAspect ||
                        namedPreview || (el.tagName === 'IMG' && semanticSurface && rect.width * rect.height >= 60_000)
                    )
                });
            }
            candidates.sort((a, b) => Math.abs(a.ratio - targetAspect) - Math.abs(b.ratio - targetAspect));
            const pageUrl = (() => { try { const u = new URL(location.href); u.search = ''; u.hash = ''; return u.href; } catch { return ''; } })();
            const modes = {portrait: readMode('portrait'), landscape: readMode('landscape')};
            return {
                editorVisible: Boolean(root) && hasDone,
                hasDone,
                hasDetect: Boolean(root) && /封面检测|重新检测/.test(rootText),
                modeControls: modes,
                previewCandidates: candidates.slice(0, 12),
                domEvidence: {
                    pageUrl,
                    pageTitle: String(document.title || '').slice(0, 160),
                    readyState: document.readyState,
                    root: root ? {
                        tag: root.tagName,
                        role: root.getAttribute('role') || '',
                        className: classText(root).slice(0, 180),
                        text: rootText.slice(0, 600),
                        width: Math.round(rootArea(root) ? root.getBoundingClientRect().width : 0),
                        height: Math.round(root.getBoundingClientRect().height)
                    } : null,
                    modeControls: modes,
                    previewCandidates: candidates.slice(0, 12)
                }
            };
        }""", aspect)
        return _creator_cover_editor_state_from_probe(probe, aspect)
    except Exception as error:
        return {"editorVisible": False, "error": str(error), "previewCandidates": []}


def _creator_write_cover_editor_diagnostic(
    page: Page,
    orientation: str,
    state: Dict[str, Any],
    reason: str,
) -> str:
    """封面编辑器未确认时保存脱敏结构探针和截图，供按真实 DOM 修正。"""
    diagnostic_id = f"cover-editor-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    diagnostic_dir = root / "diagnostics" / "creator-cover"
    json_path = diagnostic_dir / f"{diagnostic_id}.json"
    screenshot_path = diagnostic_dir / f"{diagnostic_id}.png"
    try:
        parsed = urlparse(str(getattr(page, "url", "") or ""))
        safe_url = f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"
    except Exception:
        safe_url = ""
    payload = {
        "diagnosticId": diagnostic_id,
        "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "orientation": orientation,
        "reason": str(reason)[:1_000],
        "pageUrl": safe_url,
        "pageTitle": str(state.get("domEvidence", {}).get("pageTitle") or "")[:160],
        "readyState": str(state.get("domEvidence", {}).get("readyState") or ""),
        "editorState": state,
        "privacy": "DOM 探针不读取表单值或凭证；视口截图可能包含当前可见投稿内容，诊断文件仅存本机。",
    }
    try:
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
            payload["screenshotPath"] = str(screenshot_path)
        except Exception as screenshot_error:
            payload["screenshotError"] = type(screenshot_error).__name__
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(json_path)
    except Exception as write_error:
        return f"未能保存封面 DOM 诊断：{type(write_error).__name__}"


def _creator_click_cover_editor_text(page: Page, text: str, position: str, *, force: bool = False) -> Any:
    """点击编辑器目标比例标签，或底部唯一的“完成”保存当前边框。"""
    matches = _visible_exact_text(page, text)
    if not matches:
        raise RuntimeError(f"封面编辑器中没有可见的“{text}”")
    located: list[tuple[float, float, Any]] = []
    for match in matches:
        try:
            rect = match.evaluate("el => { const r = el.getBoundingClientRect(); return {y:r.y, area:r.width*r.height}; }")
            located.append((float(rect.get("y", 0)), float(rect.get("area", 0)), match))
        except Exception:
            continue
    if not located:
        raise RuntimeError(f"无法读取封面编辑器“{text}”控件位置")
    if position == "top":
        edge = min(item[0] for item in located)
        edge_items = [item for item in located if item[0] <= edge + 2]
    else:
        edge = max(item[0] for item in located)
        edge_items = [item for item in located if item[0] >= edge - 2]
    edge_items.sort(key=lambda item: item[1])
    if len(edge_items) > 1 and abs(edge_items[0][1] - edge_items[1][1]) < 1:
        raise RuntimeError(f"封面编辑器“{text}”控件位置有歧义")
    target = edge_items[0][2]
    target.click(force=force, timeout=5_000, no_wait_after=True)
    return target


def _creator_install_cover_file_input_probe(page: Page) -> None:
    """记录“上传封面”点击实际触发的图片 input，供无 filechooser 事件时精确回退。"""
    page.evaluate(r"""() => {
      const key = '__codexCreatorCoverFileInputProbe';
      const previous = window[key];
      if (previous?.listener) document.removeEventListener('click', previous.listener, true);
      const state = { selected: null, listener: null };
      state.listener = event => {
        const target = event.target instanceof Element ? event.target : null;
        const input = target?.closest?.('input[type="file"]');
        if (!input) return;
        const accept = String(input.getAttribute('accept') || '').toLowerCase();
        if (!accept.includes('image') || accept.includes('video') || accept.includes('.mp4')) return;
        let editorContext = '';
        for (let node = input.parentElement, depth = 0;
             node && node !== document.body && depth < 14; node = node.parentElement, depth++) {
          const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
          if (text.includes('上传封面') && text.includes('完成') &&
              (text.includes('设置竖封面') || text.includes('设置横封面'))) {
            editorContext = text.slice(0, 180);
            break;
          }
        }
        if (!editorContext) return;
        state.selected = {
          inputIndex: Array.from(document.querySelectorAll('input[type="file"]')).indexOf(input),
          accept,
          className: String(input.className || '').slice(0, 140),
          editorContext,
          eventTrusted: Boolean(event.isTrusted)
        };
      };
      window[key] = state;
      document.addEventListener('click', state.listener, true);
    }""")


def _creator_stop_cover_file_input_probe(page: Page) -> Dict[str, Any]:
    try:
        result = page.evaluate(r"""() => {
          const key = '__codexCreatorCoverFileInputProbe';
          const state = window[key];
          if (state?.listener) document.removeEventListener('click', state.listener, true);
          const selected = state?.selected || null;
          delete window[key];
          return selected;
        }""")
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _creator_cover_image_file_input(
    page: Page,
    upload_trigger: Optional[Any] = None,
    click_probe: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[Any], Dict[str, Any]]:
    """按刚才点击的封面上传控件定位图片 input，避免误选视频或其它图片入口。"""
    inputs = page.locator("input[type='file']")
    candidates: list[tuple[int, Any, Dict[str, Any]]] = []
    evidence: list[Dict[str, Any]] = []
    trigger_details: Dict[str, Any] = {}
    trigger_association: Dict[str, Any] = {}
    trigger_handle = None
    if upload_trigger is not None:
        try:
            trigger_probe = upload_trigger.evaluate(r"""el => {
              const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
              const className = node => typeof node.className === 'string' ? node.className : '';
              const globalInputs = Array.from(document.querySelectorAll('input[type="file"]'));
              let association = {
                matchedCount: 0,
                reason: 'no_upload_button_semi_container',
                candidates: []
              };
              for (let node = el; node && node !== document.body; node = node.parentElement) {
                if (!/\bsemi-upload\b/.test(className(node))) continue;
                const text = normalize(node.innerText || node.textContent);
                if (!text.includes('上传封面') || !text.includes('点击上传文件或拖拽文件到这里')) continue;
                const localInputs = Array.from(node.querySelectorAll('input[type="file"]'))
                  .filter(input => {
                    const accept = String(input.getAttribute('accept') || '').toLowerCase();
                    return !input.disabled && accept.includes('image') &&
                      !accept.includes('video') && !accept.includes('.mp4');
                  });
                const primaryInputs = localInputs.filter(input =>
                  !String(input.className || '').includes('semi-upload-hidden-input-replace')
                );
                association = {
                  matchedCount: primaryInputs.length,
                  reason: primaryInputs.length === 1
                    ? 'unique_primary_image_input_in_upload_cover_container'
                    : 'ambiguous_primary_image_inputs_in_upload_cover_container',
                  wrapperClassName: className(node).slice(0, 140),
                  wrapperText: text.slice(0, 240),
                  localImageInputCount: localInputs.length,
                  candidates: primaryInputs.slice(0, 4).map(input => ({
                    inputIndex: globalInputs.indexOf(input),
                    accept: String(input.getAttribute('accept') || '').toLowerCase(),
                    className: String(input.className || '').slice(0, 140)
                  }))
                };
                if (primaryInputs.length > 0) break;
              }
              const r = el.getBoundingClientRect();
              return {
                trigger: {
                  tag: el.tagName.toLowerCase(),
                  className: className(el).slice(0, 140),
                  text: normalize(el.innerText || el.textContent).slice(0, 160),
                  rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                  outerHTML: String(el.outerHTML || '').slice(0, 1200)
                },
                association
              };
            }""")
            if isinstance(trigger_probe, dict):
                trigger_details = trigger_probe.get("trigger") if isinstance(trigger_probe.get("trigger"), dict) else {}
                trigger_association = (
                    trigger_probe.get("association")
                    if isinstance(trigger_probe.get("association"), dict) else {}
                )
        except Exception as error:
            trigger_details = {"inspectionError": type(error).__name__}
        try:
            trigger_handle = upload_trigger.element_handle(timeout=1_000)
        except Exception:
            trigger_handle = None
    clicked_input_index = click_probe.get("inputIndex") if isinstance(click_probe, dict) else None
    for index in range(inputs.count()):
        candidate = inputs.nth(index)
        try:
            item = candidate.evaluate("""(input, trigger) => {
                const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const accept = String(input.getAttribute('accept') || '').toLowerCase();
                let editorContext = '';
                for (let node = input.parentElement, depth = 0; node && node !== document.body && depth < 14; node = node.parentElement, depth++) {
                    const text = normalize(node.innerText || node.textContent);
                    if (text.includes('上传封面') && text.includes('完成') &&
                        (text.includes('设置竖封面') || text.includes('设置横封面'))) {
                        editorContext = text.slice(0, 180);
                        break;
                    }
                }
                let domDistance = null;
                if (trigger && trigger.ownerDocument === input.ownerDocument) {
                    const triggerAncestors = new Map();
                    let depth = 0;
                    for (let node = trigger; node; node = node.parentElement, depth++) {
                        triggerAncestors.set(node, depth);
                    }
                    depth = 0;
                    for (let node = input; node; node = node.parentElement, depth++) {
                        if (triggerAncestors.has(node)) {
                            domDistance = depth + triggerAncestors.get(node);
                            break;
                        }
                    }
                }
                const rect = input.getBoundingClientRect();
                return {
                    accept,
                    disabled: Boolean(input.disabled),
                    inCoverEditor: Boolean(editorContext),
                    editorContext,
                    name: String(input.getAttribute('name') || '').slice(0, 100),
                    ariaLabel: String(input.getAttribute('aria-label') || '').slice(0, 100),
                    className: String(input.className || '').slice(0, 140),
                    fileCount: input.files ? input.files.length : 0,
                    domDistance,
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    parentTag: input.parentElement?.tagName || '',
                    parentClassName: String(input.parentElement?.className || '').slice(0, 140)
                };
            }""", trigger_handle)
        except Exception as error:
            item = {"inspectionError": type(error).__name__}
        item["index"] = index
        item["clickedByUploadTrigger"] = isinstance(clicked_input_index, int) and clicked_input_index == index
        evidence.append(item)
        accept = str(item.get("accept") or "")
        image_input = "image" in accept and "video" not in accept and ".mp4" not in accept
        contextual_untyped_input = not accept and bool(item.get("inCoverEditor"))
        if not item.get("disabled") and (image_input or contextual_untyped_input):
            candidates.append((index, candidate, item))

    summary = {
        "totalFileInputs": inputs.count(),
        "uploadTrigger": trigger_details,
        "uploadTriggerAssociation": trigger_association,
        "clickProbe": click_probe if isinstance(click_probe, dict) else {},
        "imageCandidates": [item[2] for item in candidates],
        "inputs": evidence,
    }
    if trigger_handle is not None:
        try:
            if not summary["uploadTrigger"]:
                summary["uploadTrigger"] = upload_trigger.evaluate("""el => ({
                    tag: el.tagName.toLowerCase(),
                    className: String(el.className || '').slice(0, 140),
                    text: String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
                    outerHTML: String(el.outerHTML || '').slice(0, 1200)
                })""")
        except Exception:
            pass
    if isinstance(clicked_input_index, int):
        clicked = [
            item for item in candidates
            if item[0] == clicked_input_index
            and str(item[2].get("accept") or "") == str(click_probe.get("accept") or "")
            and str(item[2].get("className") or "") == str(click_probe.get("className") or "")
        ]
        if len(clicked) == 1:
            summary["matchedCount"] = 1
            summary["selectionReason"] = "upload_trigger_click_event"
            return clicked[0][1], summary

    if trigger_association.get("matchedCount") == 1:
        associated = trigger_association.get("candidates")
        if isinstance(associated, list) and len(associated) == 1:
            association = associated[0]
            associated_index = association.get("inputIndex") if isinstance(association, dict) else None
            if isinstance(associated_index, int):
                matched = [item for item in candidates if item[0] == associated_index]
                if len(matched) == 1:
                    summary["matchedCount"] = 1
                    summary["selectionReason"] = "upload_trigger_semi_container_primary_input"
                    return matched[0][1], summary

    nearest = candidates
    distances = [item[2].get("domDistance") for item in candidates if isinstance(item[2].get("domDistance"), (int, float))]
    if distances:
        nearest_distance = min(distances)
        nearest = [item for item in candidates if item[2].get("domDistance") == nearest_distance]
        primary = [item for item in nearest if "semi-upload-hidden-input-replace" not in str(item[2].get("className") or "")]
        if primary:
            nearest = primary
        summary["nearestDomDistance"] = nearest_distance
    if len(nearest) != 1:
        summary["matchedCount"] = len(nearest)
        return None, summary
    summary["matchedCount"] = 1
    summary["selectionReason"] = "unique_nearest_cover_input"
    return nearest[0][1], summary


_CREATOR_COVER_FRAME_CONFIGS = {
    "portrait": {"label": "竖封面3:4", "tab": "设置竖封面", "aspect": 0.75, "ratio": "3:4"},
    "landscape": {"label": "横封面4:3", "tab": "设置横封面", "aspect": 4 / 3, "ratio": "4:3"},
}


def _creator_open_cover_frame_editor(page: Page, orientation: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """打开指定正式槽编辑器并切到正确比例，回读实际预览比例。"""
    config = _CREATOR_COVER_FRAME_CONFIGS.get(orientation)
    if config is None:
        raise RuntimeError(f"未知封面比例：{orientation}")

    slot = _creator_cover_frame_slots(page).get(orientation)
    if slot is None:
        raise RuntimeError(f"投稿页没有唯一识别到“{config['label']}”封面槽位")
    card = slot["locator"]
    previews = card.locator('[class*="cover-Jg3T4p"]')
    if previews.count() > 1:
        raise RuntimeError(f"“{config['label']}”封面预览匹配 {previews.count()} 个，未打开编辑器")
    if previews.count() == 1:
        try:
            previews.first.hover(timeout=3_000)
        except Exception:
            pass
    edit_labels = _visible_exact_text(card, "编辑封面")
    if len(edit_labels) == 1:
        edit_labels[0].click(timeout=5_000)
    elif previews.count() == 1:
        previews.first.click(timeout=5_000)
    else:
        card.click(timeout=5_000)

    deadline = time.monotonic() + 10
    editor_state: Dict[str, Any] = {"editorVisible": False}
    while time.monotonic() < deadline:
        editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
        if editor_state.get("editorVisible"):
            break
        page.wait_for_timeout(200)
    if not editor_state.get("editorVisible"):
        raise RuntimeError(
            f"点击“{config['label']}”后封面编辑器未就绪；诊断={json.dumps(editor_state, ensure_ascii=False)}"
        )

    click_errors: list[str] = []
    try:
        _creator_click_cover_editor_text(page, str(config["tab"]), "top")
    except Exception as error:
        click_errors.append(f"普通点击：{type(error).__name__}: {error}")
    editor_state = _creator_wait_for_cover_frame_confirmation(page, orientation, timeout_ms=6_000)
    marker = "hasPortraitMode" if orientation == "portrait" else "hasLandscapeMode"
    if not (editor_state.get("markers") or {}).get(marker):
        # 普通点击没有留下目标模式证据时，对同一唯一可见标签做一次强制 DOM 点击并回读。
        try:
            _creator_click_cover_editor_text(page, str(config["tab"]), "top", force=True)
        except Exception as error:
            click_errors.append(f"DOM 强制点击：{type(error).__name__}: {error}")
        editor_state = _creator_wait_for_cover_frame_confirmation(page, orientation, timeout_ms=6_000)
    preview_aspect = editor_state.get("previewAspect") if isinstance(editor_state.get("previewAspect"), dict) else None
    if not editor_state.get("editorVisible") or not (editor_state.get("markers") or {}).get(marker) or preview_aspect is None:
        reason = f"未确认活动模式 {orientation} 或 {config['ratio']} 预览比例"
        if click_errors:
            reason += "；" + "；".join(click_errors)
        diagnostic_path = _creator_write_cover_editor_diagnostic(page, orientation, editor_state, reason)
        raise RuntimeError(
            f"点击“{config['tab']}”后未确认 {config['ratio']} 封面模式和预览比例；"
            f"诊断文件={diagnostic_path}；探针={json.dumps(editor_state, ensure_ascii=False)}"
        )
    return config, preview_aspect


def _creator_select_ai_option_in_cover_editor(page: Page, orientation: str, source: str) -> bool:
    """仅当编辑器内能唯一匹配 AI 候选图时才选择；不把普通视频帧误当 AI 图。"""
    if not source:
        return False
    state = page.evaluate("""({orientation, source}) => {
      const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const visible = el => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
      };
      const aspectLabel = orientation === 'portrait' ? '设置竖封面' : '设置横封面';
      const roots = Array.from(document.querySelectorAll('[role="dialog"],[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"]'))
        .filter(el => visible(el) && normalize(el.innerText).includes(aspectLabel) && normalize(el.innerText).includes('完成'));
      const expected = value => {
        const raw = String(value || '').match(/url\\(["']?(.*?)["']?\\)/)?.[1] || String(value || '');
        try { const url = new URL(raw, location.href); return `${url.origin}${url.pathname}`.toLowerCase(); }
        catch { return raw.split(/[?#]/, 1)[0].toLowerCase(); }
      };
      const expectedSource = expected(source);
      if (!expectedSource) return {count: 0, selected: false};
      const matches = [];
      for (const root of roots) {
        for (const el of [root, ...Array.from(root.querySelectorAll('*'))]) {
          if (!visible(el)) continue;
          const style = getComputedStyle(el);
          const actual = el.tagName === 'IMG' ? (el.currentSrc || el.src || '') : style.backgroundImage;
          if (!actual || actual === 'none' || expected(actual) !== expectedSource) continue;
          let target = el;
          for (let depth = 0; target !== root && depth < 5; depth++, target = target.parentElement) {
            if (target.matches('button,[role="button"],[role="option"],[tabindex],label')) break;
          }
          const cls = String(target.className && target.className.baseVal || target.className || '');
          const attrs = ['aria-checked','aria-selected','data-selected','data-state'].map(name => String(target.getAttribute(name) || '').toLowerCase());
          const checked = attrs.some(value => ['true','checked','selected'].includes(value)) || /(selected|checked|active|current)/i.test(cls) || !!target.querySelector('[class*="check" i],[aria-label*="选中"]');
          target.setAttribute('data-codex-ai-cover-editor-option', 'true');
          matches.push({checked});
        }
      }
      return {count: matches.length, selected: matches.length === 1 && matches[0].checked};
    }""", {"orientation": orientation, "source": source})
    if not isinstance(state, dict) or state.get("count") != 1:
        return False
    option = page.locator('[data-codex-ai-cover-editor-option="true"]')
    if option.count() != 1:
        return False
    if not state.get("selected"):
        option.click(force=True, timeout=5_000, no_wait_after=True)
        state = page.evaluate("""() => {
          const el = document.querySelector('[data-codex-ai-cover-editor-option="true"]');
          if (!el) return {selected:false};
          const cls = String(el.className && el.className.baseVal || el.className || '');
          const attrs = ['aria-checked','aria-selected','data-selected','data-state'].map(name => String(el.getAttribute(name) || '').toLowerCase());
          return {selected: attrs.some(value => ['true','checked','selected'].includes(value)) || /(selected|checked|active|current)/i.test(cls) || !!el.querySelector('[class*="check" i],[aria-label*="选中"]')};
        }""")
        if not isinstance(state, dict) or not state.get("selected"):
            return False
    return True


def _creator_save_selected_ai_cover_frame(
    page: Page,
    orientation: str,
    source: str,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """在缺失比例的正式槽编辑器内唯一选择同一 AI 图，保存并回读槽位图片。"""
    config = _CREATOR_COVER_FRAME_CONFIGS.get(orientation)
    if config is None:
        raise RuntimeError(f"未知封面比例：{orientation}")
    try:
        config, preview_aspect = _creator_open_cover_frame_editor(page, orientation)
        if not _creator_select_ai_option_in_cover_editor(page, orientation, source):
            raise RuntimeError(f"编辑器内未能唯一确认 AI 推荐图用于 {config['ratio']} 槽")
        _report_operation_progress(
            progress,
            "保存 AI 推荐封面",
            message=f"已在 {config['ratio']} 正式槽编辑器选择同一 AI 推荐图，正在保存",
        )
        _creator_click_cover_editor_text(page, "完成", "bottom")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
            if not editor_state.get("editorVisible"):
                break
            page.wait_for_timeout(150)
        else:
            raise RuntimeError(f"保存 {config['ratio']} AI 推荐封面后编辑器仍未关闭")
        slot = _creator_cover_frame_slots(page).get(orientation)
        if slot is None or not slot.get("ready"):
            raise RuntimeError(f"{config['ratio']} AI 推荐封面保存后没有从正式槽读回图片")
        return {
            "orientation": orientation,
            "aspectRatio": config["ratio"],
            "saved": True,
            "source": "平台 AI 推荐封面",
            "previewAspect": preview_aspect,
        }
    except Exception:
        # AI 编辑器路径不完整时关闭未保存弹窗，再由商品详情图后备路径补齐双槽。
        try:
            editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
            if editor_state.get("editorVisible"):
                try:
                    _creator_click_cover_editor_text(page, "取消", "bottom")
                except Exception:
                    page.keyboard.press("Escape")
        except Exception:
            pass
        raise


def _creator_set_cover_frame(
    page: Page,
    orientation: str,
    cover_image: Path,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """将本地商品详情图上传到对应比例封面槽，再核对比例并保存。"""
    config = {
        "portrait": {"label": "竖封面3:4", "tab": "设置竖封面", "aspect": 0.75, "ratio": "3:4"},
        "landscape": {"label": "横封面4:3", "tab": "设置横封面", "aspect": 4 / 3, "ratio": "4:3"},
    }.get(orientation)
    if not config:
        raise RuntimeError(f"未知封面比例：{orientation}")
    if not cover_image.is_file():
        raise FileNotFoundError(f"本地商品封面文件不存在：{cover_image}")

    slots = _creator_cover_frame_slots(page)
    slot = slots.get(orientation)
    if slot is None:
        raise RuntimeError(f"投稿页没有唯一识别到“{config['label']}”封面槽位")

    card = slot["locator"]
    previews = card.locator('[class*="cover-Jg3T4p"]')
    if previews.count() > 1:
        raise RuntimeError(f"“{config['label']}”封面预览匹配 {previews.count()} 个，未打开编辑器")
    if previews.count() == 1:
        try:
            previews.first.hover(timeout=3_000)
        except Exception:
            pass
    edit_labels = _visible_exact_text(card, "编辑封面")
    if len(edit_labels) == 1:
        edit_labels[0].click(timeout=5_000)
    elif previews.count() == 1:
        previews.first.click(timeout=5_000)
    else:
        card.click(timeout=5_000)

    deadline = time.monotonic() + 10
    editor_state: Dict[str, Any] = {"editorVisible": False}
    while time.monotonic() < deadline:
        editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
        if editor_state.get("editorVisible"):
            break
        page.wait_for_timeout(200)
    if not editor_state.get("editorVisible"):
        raise RuntimeError(
            f"点击“{config['label']}”后封面编辑器未就绪；诊断={json.dumps(editor_state, ensure_ascii=False)}"
        )

    _creator_click_cover_editor_text(page, str(config["tab"]), "top")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
        if editor_state.get("editorVisible") and isinstance(editor_state.get("previewAspect"), dict):
            break
        page.wait_for_timeout(150)
    preview_aspect = editor_state.get("previewAspect") if isinstance(editor_state.get("previewAspect"), dict) else None
    if preview_aspect is None:
        raise RuntimeError(
            f"已切换到“{config['tab']}”，但未确认 {config['ratio']} 封面预览比例；诊断={json.dumps(editor_state, ensure_ascii=False)}"
        )

    _report_operation_progress(
        progress,
        "上传商品详情图封面",
        message=f"正在把本地商品详情图设置为 {config['ratio']} 封面（不使用平台 AI）",
    )
    selected_name = ""
    upload_trigger: Optional[Any] = None
    click_probe: Dict[str, Any] = {}
    probe_installed = False
    try:
        try:
            _creator_install_cover_file_input_probe(page)
            probe_installed = True
        except Exception as probe_error:
            editor_state["fileInputProbeInstallError"] = f"{type(probe_error).__name__}: {probe_error}"[:300]
        with page.expect_file_chooser(timeout=5_000) as chooser_info:
            upload_trigger = _creator_click_cover_editor_text(page, "上传封面", "bottom")
        chooser = chooser_info.value
        chooser.set_files(str(cover_image), timeout=20_000)
        try:
            selected_name = str(chooser.element.evaluate("el => el.files?.[0]?.name || ''") or "")
        except Exception:
            selected_name = ""
    except PlaywrightTimeoutError as error:
        if probe_installed:
            click_probe = _creator_stop_cover_file_input_probe(page)
            probe_installed = False
        # 有些创作者中心版本不向 CDP 发出 filechooser 事件，但编辑器里的图片
        # input 仍可用。优先使用点击事件实际触发的 input，其次按与上传按钮的 DOM 距离唯一定位。
        file_input, file_input_evidence = _creator_cover_image_file_input(page, upload_trigger, click_probe)
        editor_state["fileInputEvidence"] = file_input_evidence
        if file_input is None:
            diagnostic_path = _creator_write_cover_editor_diagnostic(
                page,
                orientation,
                editor_state,
                "点击上传封面后未捕获 filechooser，且图片上传 input 无法唯一识别",
            )
            raise RuntimeError(
                "点击“上传封面”后未触发文件选择器，且无法唯一确认封面图片上传控件；"
                f"已停止以避免误选视频控件。诊断文件：{diagnostic_path}"
            ) from error
        try:
            file_input.set_input_files(str(cover_image), timeout=20_000)
            selected_name = str(file_input.evaluate("el => el.files?.[0]?.name || ''") or "")
        except Exception as fallback_error:
            editor_state["fileInputFallbackError"] = f"{type(fallback_error).__name__}: {fallback_error}"[:500]
            diagnostic_path = _creator_write_cover_editor_diagnostic(
                page,
                orientation,
                editor_state,
                "唯一图片上传 input 已识别，但直接写入失败",
            )
            raise RuntimeError(
                "创作者中心未能接收本地商品封面；直接写入唯一图片控件失败。"
                f"诊断文件：{diagnostic_path}"
            ) from fallback_error
    except Exception as error:
        if probe_installed:
            _creator_stop_cover_file_input_probe(page)
        raise RuntimeError(f"创作者中心未能接收本地商品封面：{error}") from error
    else:
        if probe_installed:
            _creator_stop_cover_file_input_probe(page)
    if selected_name and str(selected_name) != cover_image.name:
        raise RuntimeError(
            f"创作者中心封面上传未确认（期望 {cover_image.name}，页面回读 {selected_name or '空'}）；未保存封面"
        )
    editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
    if not editor_state.get("editorVisible") or not isinstance(editor_state.get("previewAspect"), dict):
        raise RuntimeError(f"上传详情图后封面编辑器预览异常；诊断={json.dumps(editor_state, ensure_ascii=False)}")

    _report_operation_progress(
        progress,
        "保存商品详情图封面",
        message=f"本地商品详情图已载入，正在保存 {config['ratio']} 封面",
    )
    _creator_click_cover_editor_text(page, "完成", "bottom")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        editor_state = _creator_cover_editor_metrics(page, float(config["aspect"]))
        if not editor_state.get("editorVisible"):
            break
        page.wait_for_timeout(150)
    else:
        raise RuntimeError(f"“{config['ratio']}”封面点击完成后编辑器仍未关闭")

    refreshed_slots = _creator_cover_frame_slots(page)
    refreshed = refreshed_slots.get(orientation)
    if refreshed is None or not refreshed.get("ready"):
        raise RuntimeError(f"“{config['ratio']}”封面点完成后没有读回已保存的封面图片")
    return {
        "orientation": orientation,
        "aspectRatio": config["ratio"],
        "saved": True,
        "source": "商品详情图",
        "sourceFile": str(cover_image),
        "previewAspect": preview_aspect,
    }


class _CreatorProductDetailImagesUnavailable(RuntimeError):
    """仅表示本地没有详情图文件；图像处理错误不能用此异常表示。"""

    def __init__(
        self,
        source_dir: Path,
        reason_code: str,
        *,
        file_count: int = 0,
        ignored_files: Optional[Sequence[str]] = None,
    ) -> None:
        self.source_dir = source_dir
        self.reason_code = reason_code
        self.file_count = file_count
        self.ignored_files = list(ignored_files or [])
        super().__init__(f"{reason_code}: {source_dir}")

    def diagnostic(self) -> Dict[str, Any]:
        return {
            "status": "unavailable",
            "reasonCode": self.reason_code,
            "sourceDir": str(self.source_dir),
            "imageCount": 0,
            "fileCount": self.file_count,
            "ignoredFiles": self.ignored_files,
        }


def _creator_product_cover_assets(video_path: Path) -> tuple[Dict[str, Path], Dict[str, Any]]:
    """从该发布视频同批次下载的商品详情图生成本地竖/横封面。"""
    source_dir = video_path.parent / "商品详情图"
    if not source_dir.exists():
        raise _CreatorProductDetailImagesUnavailable(source_dir, "DETAIL_IMAGE_DIRECTORY_MISSING")
    if not source_dir.is_dir():
        raise RuntimeError(f"商品详情图路径存在但不是目录；不能将其当作无图并切换 AI：{source_dir}")
    source_entries = sorted(source_dir.iterdir())
    image_paths = sorted(
        path for path in source_entries
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    )
    if not image_paths:
        ignored_files = [path.name for path in source_entries if path.is_file()]
        raise _CreatorProductDetailImagesUnavailable(
            source_dir,
            "NO_SUPPORTED_DETAIL_IMAGES",
            file_count=len(ignored_files),
            ignored_files=ignored_files[:20],
        )

    # pack_detail 的图库顺序以主图优先；保持首张图片作为封面主体，避免把
    # 规格说明、白底图等后续详情图误当成商品封面。
    source_image = image_paths[0]
    ffmpeg = resolve_ffmpeg()
    output_dir = video_path.parent / "投稿封面"
    outputs = {
        "portrait": (output_dir / "商品详情图封面_竖版3比4.jpg", 1080, 1440),
        "landscape": (output_dir / "商品详情图封面_横版4比3.jpg", 1440, 1080),
    }
    assets: Dict[str, Path] = {}
    for orientation, (output, width, height) in outputs.items():
        if (
            not output.is_file()
            or output.stat().st_size < 1024
            or output.stat().st_mtime < source_image.stat().st_mtime
        ):
            try:
                build_product_cover(ffmpeg, source_image, output, width, height)
            except Exception as error:
                raise RuntimeError(
                    "检测到商品详情图，但生成封面失败；该图片不会被当作‘无图’并切换 AI。"
                    f" source={source_image}; reason={type(error).__name__}: {_safe_diagnostic_text(error, 500)}"
                ) from error
        if not output.is_file() or output.stat().st_size < 1024:
            raise RuntimeError(
                "检测到商品详情图，但生成的封面文件缺失或无效；不会切换 AI。"
                f" source={source_image}; output={output}"
            )
        assets[orientation] = output
    return assets, {
        "status": "available",
        "reasonCode": "DETAIL_IMAGES_READY",
        "sourceDir": str(source_dir),
        "imageCount": len(image_paths),
        "selectedSource": str(source_image),
        "coverFiles": {name: str(path) for name, path in assets.items()},
    }


def _creator_set_both_cover_frames(
    page: Page,
    cover_assets: Dict[str, Path],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """上传本地商品详情图并保存 3:4 竖封面和 4:3 横封面。"""
    deadline = time.monotonic() + 12
    slots = _creator_cover_frame_slots(page)
    while time.monotonic() < deadline and not all(
        slots.get(name) is not None for name in ("portrait", "landscape")
    ):
        page.wait_for_timeout(200)
        slots = _creator_cover_frame_slots(page)
    if not all(slots.get(name) is not None for name in ("portrait", "landscape")):
        # 有些投稿页会先显示空白槽位；只要槽位 DOM 已出现，仍可打开编辑器上传自定义图。
        missing = [name for name in ("portrait", "landscape") if not slots.get(name)]
        raise RuntimeError(f"创作者中心双比例封面槽位尚未就绪：{', '.join(missing)}")
    frames = []
    for orientation in ("portrait", "landscape"):
        frames.append(_creator_set_cover_frame(page, orientation, cover_assets[orientation], progress))
    saved_slots = _creator_cover_frame_slots(page)
    if not _creator_cover_slots_complete(saved_slots):
        raise RuntimeError("商品详情图封面写入后，正式槽回读仍未同时确认 3:4 和 4:3 图片")
    assistant_status = _creator_cover_assistant_status(page)
    _report_operation_progress(
        progress,
        "双比例商品详情图封面已保存",
        message="本地商品详情图已设置为竖封面 3:4 和横封面 4:3",
    )
    return {
        "mode": "product_detail_images",
        "frames": frames,
        "qualityWarnings": assistant_status["qualityWarnings"],
        "assistantStatusReadable": assistant_status["readable"],
        "assistantCoverWarningPersisted": assistant_status["doubleCoverMissingVisible"],
        "slotReadback": {name: bool(saved_slots[name].get("ready")) for name in ("portrait", "landscape")},
    }


def _creator_try_ai_cover_frames(
    page: Page,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[Optional[Dict[str, Any]], str]:
    """无可用商品详情图时，将平台 AI 推荐图写入并回读正式双比例封面槽。"""
    slots_before = _creator_cover_frame_slots(page)
    selection, reason = _creator_wait_ai_cover_selection(page, progress, timeout_ms=60_000)
    if selection is None:
        return None, reason

    slots = _creator_cover_frame_slots(page)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and not all(slots.get(name) is not None for name in ("portrait", "landscape")):
        page.wait_for_timeout(200)
        slots = _creator_cover_frame_slots(page)
    if not all(slots.get(name) is not None for name in ("portrait", "landscape")):
        return None, "AI 推荐已选中，但投稿页没有同时出现竖/横正式封面槽位"

    source = str(selection.get("source") or "")
    if not source:
        return None, "AI 推荐候选没有可回读的图像来源，不能确认写入的是该推荐图"

    frames: list[Dict[str, Any]] = []
    changed_to_ready = {
        orientation for orientation in ("portrait", "landscape")
        if not (slots_before.get(orientation) or {}).get("ready") and (slots.get(orientation) or {}).get("ready")
    }
    needs_editor = []
    for orientation in ("portrait", "landscape"):
        slot = slots.get(orientation) or {}
        if selection.get("selectedBeforeClick") and slot.get("ready"):
            config = _CREATOR_COVER_FRAME_CONFIGS[orientation]
            frames.append({
                "orientation": orientation,
                "aspectRatio": config["ratio"],
                "saved": True,
                "source": "平台 AI 推荐封面（原已选中，正式槽回读有图）",
            })
        elif orientation in changed_to_ready:
            config = _CREATOR_COVER_FRAME_CONFIGS[orientation]
            frames.append({
                "orientation": orientation,
                "aspectRatio": config["ratio"],
                "saved": True,
                "source": "平台 AI 推荐封面（选择后正式槽出现图片）",
            })
        else:
            needs_editor.append(orientation)

    for orientation in needs_editor:
        try:
            frames.append(_creator_save_selected_ai_cover_frame(page, orientation, source, progress))
        except Exception as error:
            return None, f"AI 推荐图未能写入 {_CREATOR_COVER_FRAME_CONFIGS[orientation]['ratio']} 正式槽：{error}"

    saved_slots = _creator_cover_frame_slots(page)
    if not _creator_cover_slots_complete(saved_slots):
        return None, "AI 推荐路径结束后，正式槽未同时回读到 3:4 和 4:3 图片"
    assistant_status = _creator_cover_assistant_status(page)
    _report_operation_progress(
        progress,
        "AI 推荐双比例封面已保存",
        message="平台 AI 推荐图已写入竖封面 3:4 和横封面 4:3 正式槽并回读确认",
    )
    return {
        "mode": "ai_recommend",
        "frames": sorted(frames, key=lambda frame: 0 if frame.get("orientation") == "portrait" else 1),
        "qualityWarnings": assistant_status["qualityWarnings"],
        "assistantStatusReadable": assistant_status["readable"],
        "assistantCoverWarningPersisted": assistant_status["doubleCoverMissingVisible"],
        "slotReadback": {name: bool(saved_slots[name].get("ready")) for name in ("portrait", "landscape")},
        "selectionEvidence": selection.get("selectedEvidence"),
    }, ""


def _ensure_creator_product_covers(
    page: Page,
    video_path: Path,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """详情图优先写正式双槽；只有明确没有可用详情图时才尝试平台 AI。"""
    try:
        cover_assets, detail_image_check = _creator_product_cover_assets(video_path)
    except _CreatorProductDetailImagesUnavailable as unavailable:
        detail_image_check = unavailable.diagnostic()
        _report_operation_progress(
            progress,
            "无商品详情图，使用 AI 封面后备",
            message=(
                f"详情图检查={json.dumps(detail_image_check, ensure_ascii=False)}；"
                "仅因本地没有可用详情图，开始尝试 AI 推荐双比例封面"
            ),
        )
        ai_result, ai_reason = _creator_try_ai_cover_frames(page, progress)
        if ai_result is None:
            raise RuntimeError(
                "本地没有可用商品详情图，AI 后备也未能确认双比例正式槽；"
                f"详情图检查={json.dumps(detail_image_check, ensure_ascii=False)}；AI原因={ai_reason}"
            )
        ai_result["productDetailImageCheck"] = detail_image_check
        ai_result["fallbackReason"] = "NO_USABLE_PRODUCT_DETAIL_IMAGES"
        return ai_result
    except Exception as error:
        message = (
            "商品详情图存在但无法生成可用封面；为避免误用 AI，本次停止封面准备。"
            f" reason={type(error).__name__}: {_safe_diagnostic_text(error, 800)}"
        )
        _report_operation_progress(progress, "商品详情图处理失败", message=message)
        raise RuntimeError(message) from error

    _report_operation_progress(
        progress,
        "优先写入商品详情图封面",
        message=(
            f"详情图检查={json.dumps(detail_image_check, ensure_ascii=False)}；"
            "商品详情图可用，直接写入竖/横正式槽，本次不调用 AI"
        ),
    )
    result = _creator_set_both_cover_frames(page, cover_assets, progress)
    result["productDetailImageCheck"] = detail_image_check
    return result


def _creator_skip_product_covers(
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """按用户闸门不操作封面区，并明确沿用平台默认封面。"""
    _report_operation_progress(
        progress,
        "跳过封面（用户闸门/沿用平台默认）",
        message="不点击上传封面、不写入 3:4/4:3 正式槽；详情图与 AI 封面路径均跳过",
    )
    return {
        "mode": "skipped",
        "status": "skipped",
        "reasonCode": "USER_GATE_PLATFORM_DEFAULT",
        "platformDefaultRetained": True,
        "writtenSlots": [],
    }


def prepare_douyin_publish(
    context: BrowserContext,
    command: Dict[str, Any],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """从百应选品车复制商品链接并准备投稿；仅自动发布任务会点击最终发布。"""
    account_id = command.get("accountId")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
        raise RuntimeError("创作者中心账号 ID 无效")
    product_id = str(command.get("productId") or "").strip()
    product_name = str(command.get("productName") or "").strip()
    product_url = str(command.get("productUrl") or "").strip()
    description = str(command.get("description") or "").strip()
    if not product_id or not product_name or not product_url or not description:
        raise RuntimeError("缺少已选商品、商品链接或作品描述，未执行上传")
    if len(description) > 500:
        raise RuntimeError("视频简介不能超过 500 个字符，未执行上传")
    banned_words = _creator_saved_banned_words(command)
    expected_work_title = _creator_publication_title(product_name, 30, banned_words)
    if not expected_work_title:
        raise RuntimeError("商品标题清理价格、全局屏蔽词和人名后没有可用内容；请先调整商品标题，未上传视频")
    expected_product_short_title = _creator_product_short_title(product_name, description, 10, banned_words)
    if not _product_url_matches_product(product_url, product_id):
        raise RuntimeError("商品链接域名或商品 ID 与当前选品记录不匹配，未执行上传")
    publish_mode = str(command.get("publishMode") or "").strip().lower()
    if publish_mode not in {"", "prepare", "submit"}:
        raise RuntimeError("创作者中心发布模式无效")
    # PostFlow 的准备阶段永远不能点击最终发布；未传模式时保留旧调用兼容性。
    auto_publish = publish_mode == "submit" or (not publish_mode and command.get("autoPublish") is True)
    if publish_mode == "prepare":
        auto_publish = False
    video_path = Path(str(command.get("videoPath") or "")).expanduser().resolve(strict=True)
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise RuntimeError("视频路径不是有效的 MP4 文件，未执行上传")
    resume_after_upload = command.get("resumeAfterUpload") is True

    resume_page: Optional[Page] = None
    if resume_after_upload:
        _report_operation_progress(progress, "核验已有投稿页面", message="检查是否存在可安全恢复的投稿")
        resume_page = next((
            candidate for candidate in reversed(context.pages)
            if not candidate.is_closed() and _is_creator_publish_page(candidate.url)
        ), None)
        if resume_page is None:
            raise RuntimeError("恢复投稿前没有找到现有创作者中心视频页；为避免重复上传，未执行")
        existing_upload_complete, existing_snapshot = _creator_upload_complete(resume_page, video_path)
        if not existing_upload_complete:
            raise RuntimeError(
                "当前创作者中心页面未能确认已有视频预览；为避免重复上传，未继续。"
                + (f" 页面提示：{existing_snapshot[:180]}" if existing_snapshot else "")
            )

    _report_operation_progress(progress, "核验百应商品并复制链接", message=f"商品：{product_name}", product_id=product_id, product_title=product_name)
    # 先从百应选品车按商品 ID 复制平台分享链接。链接未核验前不会选择或上传视频。
    cart_page, copied_product_url = _copy_baiying_product_link(context, product_id, product_name, progress)
    if resume_after_upload:
        try:
            _dismiss_baiying_copy_dialog(cart_page)
        except Exception:
            # 已有投稿页在另一标签；复制提示只属于百应页，不影响本次投稿核对。
            _report_operation_progress(progress, "核验已有投稿页面", message="百应复制提示未关闭，继续核对原投稿页")
        page = resume_page
        assert page is not None
        page.bring_to_front()
    else:
        _report_operation_progress(progress, "打开创作者中心投稿页", message="百应商品链接已确认，正在进入投稿流程")
        page = _open_creator_from_baiying(context, cart_page, progress)

    login_url = page.url.lower()
    if any(marker in login_url for marker in ("/login", "/passport/")):
        raise RuntimeError("当前账号的创作者中心尚未登录；请在此账号专属浏览器登录后重试")
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    if page.locator("input[type='password']").count() or "扫码登录" in body:
        raise RuntimeError("当前账号的创作者中心尚未登录；请在此账号专属浏览器登录后重试")

    if not resume_after_upload:
        if _discard_creator_unpublished_video_draft(
            page, preserve_existing=command.get("preserveExistingDraft") is True,
        ):
            _report_operation_progress(
                progress,
                "跳过旧草稿",
                message="已放弃创作者中心上次未发布草稿；本次只使用当前队列视频",
            )
        _report_operation_progress(progress, "准备上传视频", message="正在定位创作者中心的视频上传控件")
        file_input = None
        upload_input_deadline = time.monotonic() + 20
        while time.monotonic() < upload_input_deadline:
            inputs = page.locator("input[type='file']")
            video_inputs: list[Any] = []
            unknown_inputs: list[Any] = []
            for index in range(inputs.count()):
                candidate = inputs.nth(index)
                try:
                    accept = (candidate.get_attribute("accept") or "").lower()
                except Exception:
                    accept = ""
                if "image" in accept and "video" not in accept and ".mp4" not in accept:
                    continue
                if "video" in accept or ".mp4" in accept or "media" in accept:
                    video_inputs.append(candidate)
                elif not accept:
                    unknown_inputs.append(candidate)
            file_input = video_inputs[0] if video_inputs else (unknown_inputs[0] if len(unknown_inputs) == 1 else None)
            if file_input is not None:
                break
            page.wait_for_timeout(250)
        if file_input is None:
            raise RuntimeError("已打开创作者中心，但没有识别到唯一的视频上传控件；未选择文件")

        # 这是本次唯一的文件上传动作。恢复现有投稿时不会重新选择或上传文件。
        file_input.set_input_files(str(video_path), timeout=30_000)
        _report_operation_progress(progress, "视频已提交，等待平台上传", message="创作者中心正在处理视频；未检测到真实百分比时显示动态进度")
    page.bring_to_front()

    deadline = time.monotonic() + 8 * 60
    description_field = None
    work_title_field = None
    upload_snapshot = ""
    last_wait_progress_at = 0.0
    while time.monotonic() < deadline:
        description_field = _creator_description_field(page)
        work_title_field = _creator_work_title_field(page)
        if description_field is not None and work_title_field is not None:
            break
        if time.monotonic() - last_wait_progress_at >= 2:
            _report_operation_progress(progress, "等待投稿表单就绪", message="创作者中心尚未显示作品标题和简介输入框")
            last_wait_progress_at = time.monotonic()
        page.wait_for_timeout(500)
    if description_field is None:
        raise RuntimeError("视频已提交给创作者中心，简介输入框尚未出现；状态不确定，请检查当前页面后再继续")
    if work_title_field is None:
        raise RuntimeError("视频已提交给创作者中心，但作品标题输入框尚未出现；状态不确定，请检查当前页面后再继续")

    try:
        work_title = _creator_publication_title(
            product_name, _creator_text_limit(work_title_field, 30), banned_words
        )
        if not work_title:
            raise RuntimeError("商品名称为空，无法填写作品标题")
        _fill_creator_text_with_readback(
            page,
            work_title_field,
            work_title,
            "作品标题",
            normalizer=lambda value: normalize_creator_description_readback(value).strip(),
        )
        _fill_creator_text_with_readback(page, description_field, description, "简介")
    except Exception as error:
        raise RuntimeError(f"已提交视频，但作品标题或简介预填失败；请检查当前页面。原因：{error}") from error
    _report_operation_progress(progress, "填写作品内容", message="作品标题已按商品名填入，简介已填入并核对")

    # 先连续确认视频预览和平台上传完成，再关联商品并提交。
    stable_ready_polls = 0
    upload_snapshot = ""
    last_upload_progress_at = 0.0
    while time.monotonic() < deadline:
        complete, upload_snapshot = _creator_upload_complete(page, video_path)
        if complete:
            stable_ready_polls += 1
            if stable_ready_polls >= 4:
                break
        else:
            stable_ready_polls = 0
        if time.monotonic() - last_upload_progress_at >= 2:
            upload_percent = _creator_upload_progress_percent(page)
            _report_operation_progress(
                progress,
                "等待创作者中心完成视频上传",
                percent=upload_percent,
                message=(
                    "创作者中心正在处理视频；页面未提供可读取的百分比"
                    if upload_percent is None else f"创作者中心显示上传进度 {upload_percent}%"
                ),
                product_id=product_id,
                product_title=product_name,
            )
            last_upload_progress_at = time.monotonic()
        page.wait_for_timeout(1000)
    if stable_ready_polls < 4:
        excerpt = upload_snapshot[:240]
        raise RuntimeError(
            "创作者中心未在 8 分钟内显示稳定的上传完成状态；请检查页面，未关联商品或点击发布。"
            + (f" 页面提示：{excerpt}" if excerpt else "")
        )

    # 当前用户闸门要求完全跳过封面；视频已稳定上传后继续现行挂车和发布核验。
    cover_selection = _creator_skip_product_covers(progress)

    # 恢复现有投稿时先回读已关联商品，避免再次点“添加链接”生成重复购物车标签。
    existing_card, existing_card_text = _creator_product_card(page, product_name) if resume_after_upload else (None, "")
    existing_card_hrefs = _creator_product_card_hrefs(existing_card, product_name) if resume_after_upload else []
    product_short_title = _creator_resume_linked_short_title(
        existing_card_hrefs, existing_card_text, product_id, product_name, description, banned_words,
    ) if resume_after_upload else None
    if product_short_title is not None:
        _report_operation_progress(progress, "核对已有商品关联", message="已确认同一商品及短标题，跳过重复添加链接")
    else:
        # 按百应提示流程选择“购物车”，填写此前已按商品 ID 核验的链接。
        _report_operation_progress(progress, "关联百应商品", message="视频上传完成，正在绑定已核验的商品链接")
        product_link_field = _select_creator_cart_tag(page)
        try:
            add_link_control = _creator_add_link_control(page, product_link_field)
            product_link_field.fill(copied_product_url, timeout=5_000)
            if (product_link_field.input_value(timeout=2_000) or "").strip() != copied_product_url:
                raise RuntimeError("商品链接没有准确填入")
            add_link_control.click(timeout=10_000)
        except Exception as error:
            raise RuntimeError(f"视频已上传，但商品链接未能关联；已停止，未点击发布。原因：{error}") from error

        _report_operation_progress(progress, "填写商品短标题", message="平台要求先完成商品编辑，再确认购物车关联")
        try:
            product_short_title = _fill_creator_product_short_title(
                page, product_name, description, expected_product_short_title, banned_words,
            )
        except Exception as error:
            raise RuntimeError(f"视频已上传，但商品短标题编辑未完成；已停止，未点击发布。原因：{error}") from error

        link_deadline = time.monotonic() + 15
        link_snapshot = ""
        saw_success_hint = False
        while time.monotonic() < link_deadline:
            try:
                link_snapshot = " ".join((page.locator("body").inner_text(timeout=1000) or "").split())
            except Exception:
                link_snapshot = ""
            saw_success_hint = saw_success_hint or any(
                marker in link_snapshot for marker in ("商品添加成功", "链接添加成功", "已添加商品", "已关联商品")
            )
            link_context = _creator_cart_link_context(product_link_field)
            if _creator_cart_context_matches_product(link_context, product_id, product_name):
                break
            page.wait_for_timeout(500)
        else:
            hint = "；页面虽提示添加成功，但未核验到当前商品 ID 或完整名称" if saw_success_hint else ""
            raise RuntimeError(f"视频已上传，但页面没有确认当前商品链接已关联{hint}；已停止，未点击发布")

    # 按用户要求沿用平台当前可见范围，不探测或改动“谁可以看”；其余选项仍照常核验。
    # 封面保持平台默认，本流程不操作封面区。
    _report_operation_progress(progress, "核对发布选项", message="确认允许保存和立即发布设置；可见范围沿用页面当前选择")
    for option in ("允许", "立即发布"):
        try:
            _ensure_creator_radio(page, option)
        except Exception as error:
            raise RuntimeError(f"视频和商品链接已填入，但发布选项“{option}”无法确认；已停止，未点击发布。原因：{error}") from error

    if auto_publish and _creator_manual_review_active(command):
        auto_publish = False
    if auto_publish:
        publish_page_url = page.url
        publish_page_title = page.title()
        platform_status, publish_snapshot = _publish_creator_post(page, progress)
        _report_operation_progress(progress, "发布流程完成", percent=100, message="抖音已返回最终平台回执")
        return {
            "ok": True,
            "published": platform_status == "published",
            "platformStatus": platform_status,
            "accountId": int(account_id),
            "productId": product_id,
            "productName": product_name,
            "workTitle": work_title,
            "productShortTitle": product_short_title,
            "coverSelection": cover_selection,
            "productUrl": copied_product_url,
            "description": description,
            "videoPath": str(video_path),
            "url": publish_page_url,
            "pageTitle": publish_page_title,
            "manualPublishRequired": False,
            "prepareOnly": False,
            "submissionAttempted": True,
            "publishMode": "submit",
            "platformSnapshot": publish_snapshot,
            "message": "已自动提交最终发布；状态以创作者中心回执为准。",
        }

    _report_operation_progress(progress, "上传准备完成，等待人工发布", percent=100, message="视频和商品已准备完成；最终发布仍由用户确认")
    return {
        "ok": True,
        "published": False,
        "platformStatus": "uploaded_waiting_publish",
        "accountId": int(account_id),
        "productId": product_id,
        "productName": product_name,
        "workTitle": work_title,
        "productShortTitle": product_short_title,
        "coverSelection": cover_selection,
        "productUrl": copied_product_url,
        "description": description,
        "videoPath": str(video_path),
        "url": page.url,
        "pageTitle": page.title(),
        "manualPublishRequired": True,
        "prepareOnly": True,
        "submissionAttempted": False,
        "publishMode": "prepare",
        "message": "视频与商品链接已准备完成；流程停在最终发布前，请在创作者中心人工检查并发布。",
    }

def open_douyin_creator(context: BrowserContext, command: Dict[str, Any]) -> Dict[str, Any]:
    account_id = command.get("accountId")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
        raise RuntimeError("创作者中心账号 ID 无效")
    page = _creator_page(context)
    return {"ok": True, "accountId": int(account_id), "url": page.url, "pageTitle": page.title()}


def submit_douyin_publish(
    context: BrowserContext,
    command: Dict[str, Any],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """只提交已有上传检查点对应的投稿，不导航、不上传、不重新关联商品。"""
    account_id = command.get("accountId")
    queue_id = command.get("queueId")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
        raise RuntimeError("创作者中心账号 ID 无效")
    if isinstance(queue_id, bool) or not isinstance(queue_id, int) or queue_id < 1:
        raise RuntimeError("发布队列任务 ID 无效")
    product_id = str(command.get("productId") or "").strip()
    product_name = str(command.get("productName") or "").strip()
    product_url = str(command.get("productUrl") or "").strip()
    description = str(command.get("description") or "").strip()
    if not product_id or not product_name or not description:
        raise RuntimeError("发布任务缺少商品或作品描述，未点击发布")
    if not _product_url_matches_product(product_url, product_id):
        raise RuntimeError("队列商品链接与商品 ID 不匹配，未点击发布")
    banned_words = _creator_saved_banned_words(command)
    expected_work_title = _creator_publication_title(product_name, 30, banned_words)
    if not expected_work_title:
        raise RuntimeError("商品标题清理价格、全局屏蔽词和人名后没有可用内容；未点击发布")
    video_path = Path(str(command.get("videoPath") or "")).expanduser().resolve(strict=True)
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise RuntimeError("发布任务对应的本地 MP4 不可用，未点击发布")

    page = next((
        candidate for candidate in reversed(context.pages)
        if not candidate.is_closed() and _is_creator_publish_page(candidate.url)
    ), None)
    if page is None:
        raise RuntimeError("没有找到本账号现有的创作者中心投稿页；未新建页面或点击发布")
    page.bring_to_front()
    _report_operation_progress(progress, "核对待发布作品", message="不重新上传视频；正在核对当前投稿与队列商品")

    description_field = _creator_description_field(page)
    if description_field is None:
        raise RuntimeError("当前投稿页没有唯一可识别的作品简介；未点击发布")
    actual_description = _creator_text_value(description_field)
    if normalize_creator_description_readback(actual_description) != description:
        raise RuntimeError("当前投稿简介与队列任务不一致；未点击发布")

    work_title_field = _creator_work_title_field(page)
    if work_title_field is None:
        raise RuntimeError("当前投稿页没有唯一可识别的作品标题；未点击发布")
    expected_work_title = _creator_publication_title(
        product_name, _creator_text_limit(work_title_field, 30), banned_words
    )
    actual_work_title = normalize_creator_description_readback(_creator_text_value(work_title_field)).strip()
    if not expected_work_title or actual_work_title != expected_work_title:
        raise RuntimeError("当前作品标题与商品名称不一致；未点击发布")

    complete, upload_snapshot = _creator_upload_complete(page, video_path)
    if not complete:
        raise RuntimeError(
            "当前投稿页未能确认视频上传完成；未点击发布。"
            + (f" 页面提示：{upload_snapshot[:180]}" if upload_snapshot else "")
        )
    body_text = " ".join((page.locator("body").inner_text(timeout=1500) or "").split())
    if video_path.name not in body_text:
        raise RuntimeError("当前投稿页未显示队列对应的视频文件名；为防止发错作品，未点击发布")

    product_field = _creator_product_link_field(page)
    product_context = _creator_cart_link_context(product_field) if product_field is not None else ""
    if not isinstance(product_context, dict) or not _creator_cart_context_matches_product(
        product_context, product_id, product_name
    ):
        raise RuntimeError("当前投稿页无法核对已关联的队列商品；未点击发布")
    _, product_card_text = _creator_product_card(page, product_name)
    expected_short_title = _creator_product_short_title(product_name, description, 10, banned_words)
    if not expected_short_title or _creator_card_short_title(product_card_text) != expected_short_title:
        raise RuntimeError("当前关联商品短标题与商品名称不一致；未点击发布")

    # 最终提交前不重开或核验封面槽；沿用准备阶段的用户闸门和平台默认状态。
    cover_selection = _creator_skip_product_covers(progress)

    # 恢复已上传投稿时同样沿用页面当前可见范围，不因“公开”控件无法识别而阻断。
    for option in ("允许", "立即发布"):
        _ensure_creator_radio(page, option)
    if command.get("respectManualReview") is True and _creator_manual_review_active(command):
        raise RuntimeError("人工复核已开启，已停在最终发布前；请在发布队列人工检查后点击发布")
    publish_page_url = page.url
    publish_page_title = page.title()
    return_status, snapshot = _publish_creator_post(page, progress)
    _report_operation_progress(progress, "最终发布提交完成", percent=100, message="抖音已返回平台提交回执")
    return {
        "ok": True,
        "queueId": queue_id,
        "accountId": account_id,
        "productId": product_id,
        "coverSelection": cover_selection,
        "platformStatus": return_status,
        "submissionAttempted": True,
        "prepareOnly": False,
        "publishMode": "submit",
        "platformSnapshot": snapshot,
        "url": publish_page_url,
        "pageTitle": publish_page_title,
    }


def _capture_publish_failure(context: Any, queue_id: Any) -> str:
    """仅在投稿失败时保存当前账号投稿页截图，绝不点击或重试发布。"""
    root = os.environ.get("TOUGUANG_DATA_DIR", "").strip()
    if not root:
        return ""
    page = next((item for item in reversed(context.pages)
                 if not item.is_closed() and "douyin.com" in urlparse(item.url).netloc
                 and "/post/video" in urlparse(item.url).path), None)
    if page is None:
        return ""
    try:
        folder = Path(root) / "diagnostics" / "publish"
        folder.mkdir(parents=True, exist_ok=True)
        safe_id = str(queue_id) if str(queue_id).isdigit() else "unknown"
        path = folder / f"queue-{safe_id}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
        page.screenshot(path=str(path), full_page=False, timeout=5_000)
        return str(path)
    except Exception:
        return ""


def run_watch(profile_path: Path) -> int:
    _configure_rate_limit_profile(profile_path)
    commands: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue()

    def read_commands() -> None:
        for line in sys.stdin:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    commands.put(value)
            except json.JSONDecodeError:
                continue
        commands.put(None)

    reader = threading.Thread(target=read_commands, name="cdp-command-reader", daemon=True)
    reader.start()

    try:
        port = read_port(profile_path)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            if not browser.contexts:
                raise RuntimeError("Chrome CDP 会话没有默认浏览器上下文")
            context = browser.contexts[0]
            attached: set[int] = set()
            for page in context.pages:
                attach_page(page, attached)
            context.on("page", lambda page: attach_page(page, attached))
            emit({"type": "ready", "port": port, "pages": len(context.pages)})

            running = True
            while running:
                if not browser.is_connected():
                    emit({"type": "fatal", "error": "托管 Chrome 已关闭，CDP 连接失效"})
                    return 1
                command: Optional[Dict[str, Any]]
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    command = {}

                if command is None:
                    break
                if command:
                    action = command.get("action")
                    command_id = str(command.get("id", ""))
                    if action == "open":
                        url = str(command.get("url", ""))
                        if not is_allowed_url(url):
                            emit({"type": "result", "id": command_id, "ok": False, "error": "目标网址不在允许的平台范围内"})
                        else:
                            try:
                                target_path = urlparse(url).path.rstrip("/")
                                page = next((
                                    candidate for candidate in reversed(context.pages)
                                    if not candidate.is_closed()
                                    and urlparse(candidate.url).path.rstrip("/") == target_path
                                    and getattr(candidate, "_touguang_managed_selection_page", False) is not True
                                    and not candidate.evaluate(
                                        "() => window.__touguangManagedSelectionPage === true || window.name === '__touguangManagedSelectionPage' || window.name === '__touguangBackgroundSelectionPage'"
                                    )
                                ), None)
                                if page is None:
                                    page = context.new_page()
                                    attach_page(page, attached)
                                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                                else:
                                    page.bring_to_front()
                                    if page.url != url:
                                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                                emit({"type": "result", "id": command_id, "ok": True, "url": page.url, "title": page.title()})
                            except Exception as error:
                                emit({"type": "result", "id": command_id, "ok": False, "error": str(error)})
                    elif action == "inspect":
                        emit({"type": "result", "id": command_id, "ok": True, "pages": page_snapshot(context)})
                    elif action == "inspect-selection":
                        page = find_selection_page(context, attached, browser=browser)
                        if page is None:
                            emit({
                                "type": "result",
                                "id": command_id,
                                "ok": False,
                                "error": (
                                    f"百应选品页等待 {SELECTION_PAGE_READY_TIMEOUT_MS // 1000} 秒后，"
                                    "仍未检测到商品类目及带货/商品信息筛选项。请确认百应已登录，"
                                    "并能正常打开选品广场。"
                                ),
                            })
                        else:
                            try:
                                emit({
                                    "type": "result",
                                    "id": command_id,
                                    **collect_filter_catalog(
                                        page,
                                        include_category_tree=command.get("includeCategoryTree") is True,
                                    ),
                                })
                            finally:
                                try:
                                    page.close()
                                except Exception:
                                    pass
                    elif action == "check-account-logins":
                        try:
                            account_id = command.get("accountId")
                            if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id < 1:
                                raise ValueError("登录预检账号 ID 无效")
                            result = check_account_login_states(context, browser, account_id)
                            emit({"type": "result", "id": command_id, **result})
                        except Exception as error:
                            emit({"type": "result", "id": command_id, "ok": False, "error": str(error)})
                    elif action == "run-selection":
                        page = None
                        close_selection_page = False
                        try:
                            report_progress = lambda payload: emit({"type": "progress", "id": command_id, **payload})
                            _report_operation_progress(report_progress, "正在连接百应选品页", message="等待托管页面就绪")
                            page = find_selection_page(context, attached, browser=browser, progress=report_progress)
                            if page is None:
                                raise RuntimeError(
                                    f"准备百应选品页面失败：托管页面等待 {SELECTION_PAGE_READY_TIMEOUT_MS // 1000} 秒后，"
                                    "仍未检测到商品类目及带货/商品信息筛选项。请确认托管 Chrome 已登录百应，"
                                    "并能正常打开选品广场后重试。"
                                )
                            result = run_selection_command(page, context, command, report_progress)
                            completed_rows = result.get("completed")
                            close_selection_page = result.get("ok") is True and not any(
                                isinstance(item, dict) and item.get("manualAction")
                                for item in (completed_rows if isinstance(completed_rows, list) else [])
                            )
                            emit({"type": "result", "id": command_id, **result})
                        except Exception as error:
                            emit({"type": "result", "id": command_id, "ok": False, "error": str(error)})
                        finally:
                            if page is not None and close_selection_page:
                                try:
                                    page.close()
                                except Exception:
                                    pass
                    elif action == "prepare-douyin-publish":
                        try:
                            report_progress = lambda payload: emit({"type": "progress", "id": command_id, **payload})
                            _report_operation_progress(report_progress, "正在准备抖音发布", message="检查账号与任务")
                            result = prepare_douyin_publish(context, command, report_progress)
                            emit({"type": "result", "id": command_id, **result})
                        except Exception as error:
                            screenshot_path = _capture_publish_failure(context, command.get("queueId"))
                            emit({
                                "type": "result",
                                "id": command_id,
                                "ok": False,
                                "error": str(error),
                                "screenshotPath": screenshot_path,
                                **({
                                    "isolated": True,
                                    "classification": error.classification,
                                    "productId": error.product_id,
                                    "retryCount": error.retry_count,
                                    "warning": error.warning,
                            } if isinstance(error, PublishVerificationIsolated) else ({
                                "isolated": True,
                                "classification": error.classification,
                                "queueId": command.get("queueId"),
                                "productId": str(command.get("productId") or ""),
                                "warning": error.warning,
                            } if isinstance(error, CreatorCoverRequirementBlocked) else {})),
                                **({"submissionAttempted": error.submission_attempted} if isinstance(error, PublishSubmissionError) else {}),
                            })
                    elif action == "submit-douyin-publish":
                        try:
                            report_progress = lambda payload: emit({"type": "progress", "id": command_id, **payload})
                            _report_operation_progress(report_progress, "核对抖音待发布作品", message="检查账号和已上传投稿")
                            result = submit_douyin_publish(context, command, report_progress)
                            emit({"type": "result", "id": command_id, **result})
                        except Exception as error:
                            screenshot_path = _capture_publish_failure(context, command.get("queueId"))
                            emit({
                                "type": "result",
                                "id": command_id,
                                "ok": False,
                                "error": str(error),
                                "screenshotPath": screenshot_path,
                                **({
                                    "isolated": True,
                                    "classification": error.classification,
                                    "queueId": command.get("queueId"),
                                    "productId": str(command.get("productId") or ""),
                                    "warning": error.warning,
                                } if isinstance(error, CreatorCoverRequirementBlocked) else {}),
                                **({"submissionAttempted": error.submission_attempted} if isinstance(error, PublishSubmissionError) else {}),
                            })
                    elif action == "open-douyin-creator":
                        try:
                            result = open_douyin_creator(context, command)
                            emit({"type": "result", "id": command_id, **result})
                        except Exception as error:
                            emit({"type": "result", "id": command_id, "ok": False, "error": str(error)})
                    elif action == "close-browser":
                        try:
                            # 仅由主进程在确认 Chrome 是本轮自动启动后调用；关闭浏览器窗口会保留磁盘 profile 和登录态。
                            browser_cdp = browser.new_browser_cdp_session()
                            browser_cdp.send("Browser.close")
                            emit({"type": "result", "id": command_id, "ok": True, "browserClosed": True})
                            running = False
                        except Exception as error:
                            if not browser.is_connected():
                                emit({"type": "result", "id": command_id, "ok": True, "browserClosed": True})
                                running = False
                            else:
                                emit({"type": "result", "id": command_id, "ok": False, "error": str(error)})
                    elif action == "stop":
                        emit({"type": "result", "id": command_id, "ok": True})
                        running = False
                    else:
                        emit({"type": "result", "id": command_id, "ok": False, "error": "未知 CDP 命令"})

                pages = context.pages
                if pages:
                    try:
                        pages[-1].wait_for_timeout(80)
                    except Exception:
                        time.sleep(0.05)
                else:
                    time.sleep(0.08)

            # sync_playwright 退出时会释放本进程的连接。这里不调用 browser.close()，
            # 避免客户端退出或热更新时关闭用户仍在使用的托管 Chrome。
        return 0
    except Exception as error:
        emit({"type": "fatal", "error": str(error)})
        return 1


def run_inspect(profile_path: Path) -> int:
    try:
        port = read_port(profile_path)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            contexts = browser.contexts
            result = {"ok": True, "port": port, "pages": page_snapshot(contexts[0]) if contexts else []}
        emit(result)
        return 0
    except Exception as error:
        emit({"ok": False, "error": str(error)})
        return 1


def wait_for_selection_page_ready(
    page: Page,
    timeout_ms: int = SELECTION_PAGE_READY_TIMEOUT_MS,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    progress_stage: str = "等待百应选品页就绪",
    progress_context: str = "等待商品类目及带货/商品信息筛选项",
) -> bool:
    """等待百应选品页完成筛选区渲染；不点击控件、不重载页面。"""
    timeout_seconds = max(1, math.ceil(timeout_ms / 1000))
    started_at = time.monotonic()
    last_progress_at = started_at - 1.0
    predicate = """() => {
      const roots = document.querySelectorAll('.merch-filter-cate-cascader-item');
      const rows = Array.from(document.querySelectorAll('.merch-filter-virtual-table .table-row .combine-select-content'));
      const hasFilter = rows.some((node) => {
        if (node.closest('.merch-filter-cate-cascader-item')) return false;
        const section = (node.closest('.table-row')?.querySelector('.table-cell.label')?.innerText || '').trim();
        return (section === '带货情况' || section === '商品信息') && (node.innerText || '').trim();
      });
      return roots.length > 0 && hasFilter;
    }"""
    if progress is None:
        try:
            page.wait_for_function(
                predicate,
                timeout=timeout_ms,
                polling=CATEGORY_PAGE_POLLING_MS,
            )
            return True
        except PlaywrightTimeoutError:
            return False
    while True:
        if page.evaluate(predicate):
            if progress is not None:
                elapsed_seconds = max(0, math.ceil(time.monotonic() - started_at))
                _report_operation_progress(
                    progress,
                    progress_stage,
                    percent=100,
                    message=f"百应筛选控件已就绪；本次等待 {elapsed_seconds} 秒",
                )
            return True
        elapsed = time.monotonic() - started_at
        elapsed_ms = int(elapsed * 1000)
        if elapsed_ms >= timeout_ms:
            if progress is not None:
                _report_operation_progress(
                    progress,
                    progress_stage,
                    index=timeout_seconds,
                    total=timeout_seconds,
                    percent=100,
                    unit="秒",
                    message=f"等待超时（{timeout_seconds}/{timeout_seconds} 秒），筛选控件仍未就绪",
                )
            return False
        if progress is not None and time.monotonic() - last_progress_at >= 1.0:
            elapsed_seconds = min(timeout_seconds, int(elapsed))
            percent = min(99, int(elapsed_ms * 100 / max(1, timeout_ms)))
            _report_operation_progress(
                progress,
                progress_stage,
                index=elapsed_seconds,
                total=timeout_seconds,
                percent=percent,
                unit="秒",
                message=f"{progress_context}（已等待 {elapsed_seconds}/{timeout_seconds} 秒）",
            )
            last_progress_at = time.monotonic()
        remaining_ms = max(1, timeout_ms - elapsed_ms)
        page.wait_for_timeout(min(CATEGORY_PAGE_POLLING_MS, remaining_ms))


def _create_background_page(context: BrowserContext, browser: Browser) -> Page:
    """通过 CDP 创建不抢当前标签焦点的专用页，供自动化独占使用。"""
    cdp = browser.new_browser_cdp_session()
    target_id: Optional[str] = None
    try:
        with context.expect_page(timeout=10_000) as page_info:
            created = cdp.send("Target.createTarget", {
                "url": "about:blank",
                "background": True,
                "focus": False,
            })
            target_id = str(created.get("targetId", "")) or None
        return page_info.value
    except Exception:
        if target_id:
            try:
                cdp.send("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        raise
    finally:
        try:
            cdp.detach()
        except Exception:
            pass


def _create_selection_work_page(context: BrowserContext) -> Page:
    """选品临时页在托管 Chrome 中后台打开，不打断用户当前标签。"""
    browser = context.browser
    if isinstance(browser, Browser):
        return _create_background_page(context, browser)
    return context.new_page()


def _login_preflight_page(context: BrowserContext, browser: Browser, account_id: int) -> Page:
    """每个账号只保留一个客户端专用登录检查页，依次检查百应和创作者中心。"""
    marker = f"__touguangLoginPreflight__account_{account_id}"
    legacy_markers = {
        f"__touguangLoginPreflight__baiying_{account_id}",
        f"__touguangLoginPreflight__creator_{account_id}",
    }
    matches: list[tuple[int, Page]] = []
    for candidate in reversed(context.pages):
        try:
            if candidate.is_closed():
                continue
            candidate_marker = getattr(candidate, "_touguang_login_preflight_marker", None)
            if not isinstance(candidate_marker, str):
                candidate_marker = candidate.evaluate("() => window.name")
            if candidate_marker == marker:
                matches.append((0, candidate))
            elif candidate_marker in legacy_markers:
                # 兼容旧版留下的两个检查页，优先复用百应页，其余稍后关闭。
                priority = 1 if candidate_marker.endswith(f"baiying_{account_id}") else 2
                matches.append((priority, candidate))
        except Exception:
            continue

    matches.sort(key=lambda item: item[0])
    page = matches[0][1] if matches else _create_background_page(context, browser)
    setattr(page, "_touguang_login_preflight_marker", marker)
    setattr(page, "_touguang_skip_baiying_capture", True)
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            if page.is_closed():
                raise RuntimeError("登录检查标签页已关闭")
            page.evaluate("marker => { window.name = marker; window.__touguangSkipBaiyingCapture = true; }", marker)
            break
        except Exception as error:
            last_error = error
            if not _is_login_navigation_race(error) or attempt == 2:
                raise
            _wait_for_login_page_stable(page)
    else:
        if last_error:
            raise last_error

    # 只关闭旧版由客户端打过标记的登录检查页，不碰用户自己的标签页。
    for _, duplicate in matches[1:]:
        try:
            if not duplicate.is_closed():
                duplicate.close()
        except Exception:
            pass
    return page


def _is_login_navigation_race(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "execution context was destroyed",
        "most likely because of a navigation",
        "frame was detached",
        "navigation interrupted",
        "target closed",
    ))


def _wait_for_login_page_stable(page: Page, timeout_ms: int = 3_000) -> bool:
    """登录页可能在 SSO/站内跳转；等当前文档稳定后再读取可见 DOM。"""
    try:
        if page.is_closed():
            return False
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(200)
        return not page.is_closed()
    except Exception:
        return False


def _read_login_page_snapshot(page: Page) -> tuple[str, str, Optional[Exception]]:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            if page.is_closed():
                raise RuntimeError("登录检查标签页已关闭")
            url = page.url.lower()
            body = page.locator("body").inner_text(timeout=2_000)[:6_000]
            return url, body, None
        except Exception as error:
            last_error = error
            if not _is_login_navigation_race(error) or attempt == 2:
                break
            _wait_for_login_page_stable(page)
    return "", "", last_error


def _login_page_state(page: Page, platform: str) -> Dict[str, str]:
    """只根据可见页面状态判断登录情况，不读取 Cookie、存储或账号凭据。"""
    url, body, read_error = _read_login_page_snapshot(page)
    if read_error is not None and _is_login_navigation_race(read_error):
        return {
            "state": "unknown",
            "message": "平台页面仍在跳转，登录状态暂时无法读取；请等页面加载稳定后重新检查。",
        }
    parsed = urlparse(url)

    normalized = re.sub(r"\s+", "", body)
    challenge_markers = ("安全验证", "请完成验证", "拖动完成上方拼图", "滑块验证", "完成下列验证", "异常验证")
    if any(marker in normalized for marker in challenge_markers):
        return {"state": "verification_required", "message": "页面要求完成安全验证，需先在账号浏览器中人工处理。"}

    login_url = any(marker in url for marker in ("/login", "/passport/", "passport.douyin.com", "sso.jinritemai.com"))
    login_text = any(marker in normalized for marker in (
        "扫码登录", "请先登录", "登录后继续", "登录后使用", "登录后查看", "手机号登录", "密码登录", "抖音账号登录"
    ))
    password_visible = False
    try:
        password_inputs = page.locator("input[type='password']")
        password_visible = any(password_inputs.nth(index).is_visible() for index in range(min(password_inputs.count(), 4)))
    except Exception:
        pass
    if login_url or login_text or password_visible:
        label = "百应" if platform == "baiying" else "抖音创作者中心"
        return {"state": "login_required", "message": f"{label}当前显示登录页面，请在该账号专属浏览器中扫码或登录。"}

    expected_host = "buyin.jinritemai.com" if platform == "baiying" else "creator.douyin.com"
    if parsed.hostname != expected_host:
        actual_host = parsed.hostname or "空白页"
        return {
            "state": "unknown",
            "message": f"页面当前位于 {actual_host}，没有停留在 {expected_host}，无法确认登录状态。",
        }
    if platform == "baiying":
        matched_markers = [marker for marker in BAIYING_WORKBENCH_SHELL_MARKERS if marker in normalized]
        if len(matched_markers) >= 2:
            return {
                "state": "logged_in",
                "message": "百应工作台已加载（导航标志：" + "、".join(matched_markers[:3]) + "）。",
            }
    return {"state": "unknown", "message": "页面已打开，但未识别到足以确认登录的工作台界面。"}


def _goto_login_check_page(page: Page, url: str, wait_until: str, timeout_ms: int = 12_000) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as error:
            last_error = error
            if not _is_login_navigation_race(error) or attempt == 1:
                raise
            if not _wait_for_login_page_stable(page):
                raise
    if last_error:
        raise last_error
    return None


def wait_for_baiying_login_interface(page: Page, timeout_ms: int = 15_000) -> bool:
    """登录预检等待百应筛选工作台或新版工作台导航，不等候单一旧版筛选 DOM。"""
    try:
        page.wait_for_function(
            """({ markers }) => {
              const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const shellMatches = markers.filter((marker) => body.includes(marker));
              const roots = document.querySelectorAll('.merch-filter-cate-cascader-item');
              const fields = Array.from(document.querySelectorAll('.merch-filter-virtual-table .table-row .combine-select-content'));
              const hasFilter = fields.some((node) => {
                if (node.closest('.merch-filter-cate-cascader-item')) return false;
                const section = (node.closest('.table-row')?.querySelector('.table-cell.label')?.innerText || '').trim();
                return (section === '带货情况' || section === '商品信息') && (node.innerText || '').trim();
              });
              return shellMatches.length >= 2 || (roots.length > 0 && hasFilter);
            }""",
            arg={"markers": list(BAIYING_WORKBENCH_SHELL_MARKERS)},
            timeout=timeout_ms,
            polling=250,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def _check_baiying_login(page: Page) -> Dict[str, str]:
    try:
        response = _goto_login_check_page(page, BAIYING_LOGIN_CHECK_URL, wait_until="commit")
    except Exception as error:
        state = _login_page_state(page, "baiying")
        if state["state"] != "unknown":
            return state
        return {"state": "unknown", "message": f"百应页面加载失败，无法确认登录状态（{type(error).__name__}）。"}
    status = getattr(response, "status", None) if response is not None else None
    if callable(status):
        status = status()
    state = _login_page_state(page, "baiying")
    if state["state"] != "unknown":
        return state
    if isinstance(status, int) and status in {401, 403}:
        return {"state": "login_required", "message": f"百应页面返回 HTTP {status}，当前登录或访问权限不可用。"}
    try:
        # 只有确实停在百应域名时才等 SPA 工作台；错域名时立即返回，避免空等 15 秒。
        if urlparse(page.url.lower()).hostname != "buyin.jinritemai.com":
            return state
        if wait_for_baiying_login_interface(page, timeout_ms=5_000):
            state = _login_page_state(page, "baiying")
            if state["state"] != "unknown":
                return state
            return {"state": "logged_in", "message": "百应选品工作台筛选区已加载。"}
    except Exception:
        pass
    state = _login_page_state(page, "baiying")
    if state["state"] != "unknown":
        return state
    return state


def _check_creator_login(page: Page) -> Dict[str, str]:
    try:
        # 创作者中心是重型 SPA；用 commit 尽快拿到 HTTP 状态，再用页面标志确认工作台。
        response = _goto_login_check_page(page, DOUYIN_CREATOR_LOGIN_CHECK_URL, wait_until="commit")
    except Exception as error:
        state = _login_page_state(page, "creator")
        if state["state"] != "unknown":
            return state
        return {"state": "unknown", "message": f"创作者中心页面加载失败，无法确认登录状态（{type(error).__name__}）。"}
    status = getattr(response, "status", None) if response is not None else None
    if callable(status):
        status = status()
    state = _login_page_state(page, "creator")
    if state["state"] != "unknown":
        return state
    if isinstance(status, int) and status in {401, 403}:
        return {"state": "unknown", "message": f"创作者中心页面返回 HTTP {status}，无法确认登录或账号权限。"}

    if urlparse(page.url.lower()).hostname != "creator.douyin.com":
        return state

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = _login_page_state(page, "creator")
        if state["state"] != "unknown":
            return state
        try:
            body = re.sub(r"\s+", "", page.locator("body").inner_text(timeout=1_000))[:6_000]
            has_upload_input = page.locator("input[type='file']").count() > 0
            recognized_shell = any(marker in body for marker in ("作品管理", "内容管理", "视频管理", "创作者服务中心"))
            upload_page = "上传视频" in body and has_upload_input
            if recognized_shell or upload_page:
                return {"state": "logged_in", "message": "抖音创作者中心工作界面已加载。"}
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
    return _login_page_state(page, "creator")


def check_account_login_states(
    context: BrowserContext,
    browser: Browser,
    account_id: int,
) -> Dict[str, Any]:
    """复用单个账号专属后台页，依次核验百应和创作者中心。"""
    login_page: Optional[Page] = None
    try:
        login_page = _login_preflight_page(context, browser, account_id)
        baiying = _check_baiying_login(login_page)
    except Exception as error:
        message = (
            "百应页面正在跳转，暂时无法读取登录状态；请等页面加载稳定后重新检查。"
            if _is_login_navigation_race(error)
            else f"百应登录状态检查失败，无法确认（{type(error).__name__}）。"
        )
        baiying = {"state": "unknown", "message": message}
    try:
        if login_page is None:
            login_page = _login_preflight_page(context, browser, account_id)
        creator = _check_creator_login(login_page)
    except Exception as error:
        message = (
            "创作者中心页面正在跳转，暂时无法读取登录状态；请等页面加载稳定后重新检查。"
            if _is_login_navigation_race(error)
            else f"创作者中心登录状态检查失败，无法确认（{type(error).__name__}）。"
        )
        creator = {"state": "unknown", "message": message}

    if login_page is not None:
        if baiying["state"] == "logged_in" and creator["state"] == "logged_in":
            # 登录确认后移除客户端的临时检查页；已有手动标签和 Cookie 均保留。
            try:
                login_page.close()
            except Exception:
                pass
        else:
            # 暂停时只把一页留给用户处理，并显示优先需要登录的站点。
            if baiying["state"] != "logged_in":
                try:
                    login_page.goto(BAIYING_LOGIN_CHECK_URL, wait_until="commit", timeout=12_000)
                except Exception:
                    pass
            try:
                login_page.bring_to_front()
            except Exception:
                pass
    return {
        "ok": True,
        "accountId": account_id,
        "baiying": baiying,
        "creatorCenter": creator,
    }


def find_selection_page(
    context: BrowserContext,
    attached: Optional[set[int]] = None,
    allow_navigation: bool = True,
    browser: Optional[Browser] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Optional[Page]:
    # 自动化只使用新版专用后台页，绝不接管用户正在手动操作的百应标签。
    managed_pages: list[Page] = []
    for page in reversed(context.pages):
        try:
            if not page.is_closed() and (
                getattr(page, "_touguang_managed_selection_page", False) is True
                or page.evaluate("() => window.__touguangManagedSelectionPage === true || window.name === '__touguangManagedSelectionPage' || window.name === '__touguangBackgroundSelectionPage'")
            ):
                managed_pages.append(page)
        except Exception:
            continue
    candidates = [
        page for page in managed_pages
        if urlparse(page.url).path.rstrip("/").endswith("/merch-picking-library")
    ]
    background_candidates: list[Page] = []
    for candidate in candidates:
        try:
            if (
                getattr(candidate, "_touguang_background_selection_page", False) is True
                or candidate.evaluate("() => window.__touguangBackgroundSelectionPage === true || window.name === '__touguangBackgroundSelectionPage'")
            ):
                background_candidates.append(candidate)
                continue
        except Exception:
            pass
        # 旧版本标记的前台页只解除客户端占用，不关闭或操控，
        # 后续选品改在 CDP 新建的后台专用页执行。
        try:
            candidate.evaluate("""() => {
              if (window.__touguangManagedSelectionPage === true) window.__touguangManagedSelectionPage = false;
              if (window.name === '__touguangManagedSelectionPage') window.name = '';
            }""")
        except Exception:
            continue
    candidates = background_candidates
    for index, page in enumerate(candidates, start=1):
        try:
            # 百应页面异步加载较慢时继续等当前标签，不要重新导航打断页面初始化。
            if wait_for_selection_page_ready(
                page,
                timeout_ms=15_000,
                progress=progress,
                progress_stage="检查已打开的百应选品页",
                progress_context=f"检查已打开的托管页面 {index}/{len(candidates)}",
            ):
                for duplicate in candidates:
                    if duplicate is page:
                        continue
                    try:
                        duplicate.close()
                    except Exception:
                        pass
                return page
        except Exception:
            continue

    # 程序自己的选品标签若一直停在“正在加载”，自动刷新一次自愈；
    # 同一次查找不会循环刷新，避免把平台瞬时延迟变成人工重试。
    if candidates:
        target = candidates[0]
        try:
            _report_operation_progress(
                progress,
                "刷新百应选品页",
                message="已打开的托管页未就绪，正在刷新一次后重新等待筛选控件",
            )
            target.reload(wait_until="domcontentloaded", timeout=45_000)
            target.evaluate("() => { window.__touguangManagedSelectionPage = true; window.__touguangBackgroundSelectionPage = true; window.name = '__touguangBackgroundSelectionPage' }")
            if wait_for_selection_page_ready(
                target,
                timeout_ms=30_000,
                progress=progress,
                progress_stage="等待刷新后的百应页面就绪",
                progress_context="等待刷新后的类目及商品筛选控件",
            ):
                target.evaluate("() => { window.__touguangManagedSelectionPage = true; window.__touguangBackgroundSelectionPage = true; window.name = '__touguangBackgroundSelectionPage' }")
                for duplicate in candidates[1:]:
                    try:
                        duplicate.close()
                    except Exception:
                        pass
                return target
        except Exception:
            pass
        for stale_page in candidates:
            try:
                stale_page.close()
            except Exception:
                pass
        managed_pages = []
    if not allow_navigation:
        return None

    # 找回已有托管后台页；没有时创建后台标签，保留人工页面及其当前焦点。
    active_browser = browser or context.browser
    if active_browser is None:
        return None
    try:
        target = _create_background_page(context, active_browser)
    except Exception:
        return None
    setattr(target, "_touguang_managed_selection_page", True)
    setattr(target, "_touguang_background_selection_page", True)

    ready = False
    try:
        if attached is not None:
            attach_page(target, attached)
        _report_operation_progress(
            progress,
            "打开百应选品广场",
            message="正在打开客户端托管的百应选品页；不会接管手动页面",
        )
        target.goto("https://buyin.jinritemai.com/dashboard/merch-picking-library", wait_until="domcontentloaded", timeout=45_000)
        target.evaluate("() => { window.__touguangManagedSelectionPage = true; window.__touguangBackgroundSelectionPage = true; window.name = '__touguangBackgroundSelectionPage' }")
        if not wait_for_selection_page_ready(
            target,
            progress=progress,
            progress_stage="等待百应筛选控件渲染",
            progress_context="等待商品类目及带货/商品信息筛选项",
        ):
            return None
        # 页面可能经历重定向或脚本重载，确认就绪后重新打自动化标记。
        target.evaluate("() => { window.__touguangManagedSelectionPage = true; window.__touguangBackgroundSelectionPage = true; window.name = '__touguangBackgroundSelectionPage' }")
        ready = True
        return target
    except Exception:
        return None
    finally:
        if not ready:
            try:
                target.close()
            except Exception:
                pass


def _filter_control(page: Page, label: str) -> Any:
    lookup = """(targetLabel) => Array.from(document.querySelectorAll('.merch-filter-virtual-table .table-row .combine-select-content'))
      .findIndex(node => !node.closest('.merch-filter-cate-cascader-item') &&
        (node.innerText || '').replace(/\\s+/g, ' ').trim() === targetLabel)"""
    index = page.evaluate(lookup, arg=label)
    if not isinstance(index, int) or index < 0:
        # 类目或上一项筛选提交后，百应会短暂卸载整行筛选控件。
        # 只等待同一精确标签重新出现，不通过相邻索引猜控件。
        try:
            page.wait_for_function(
                f"(targetLabel) => ({lookup})(targetLabel) >= 0",
                arg=label,
                timeout=3_000,
                polling=150,
            )
        except PlaywrightTimeoutError:
            pass
        index = page.evaluate(lookup, arg=label)
    if not isinstance(index, int) or index < 0:
        visible_labels = page.evaluate("""() => Array.from(document.querySelectorAll(
          '.merch-filter-virtual-table .table-row .combine-select-content'))
          .filter(node => !node.closest('.merch-filter-cate-cascader-item'))
          .filter(node => node.getBoundingClientRect().width > 0)
          .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
          .filter(Boolean).slice(0, 20)""")
        summary = "、".join(visible_labels) if isinstance(visible_labels, list) else "无法读取"
        raise RuntimeError(f"百应页面没有找到筛选项「{label}」（等待 3 秒；当前可见筛选项：{summary or '无'}）")
    # 查询索引与 locator 必须使用同一筛选器容器；全局列表还包含 17 个一级类目，
    # 在该列表中直接按筛选器索引取元素会错点类目控件。
    return page.locator(".merch-filter-virtual-table .table-row .combine-select-content").nth(index)


def _wait_filter_option_selected(option: Any, timeout_ms: int = 1_000) -> bool:
    """读取选项自身的 option-selected 状态，并短暂等待前端状态提交。"""
    try:
        return bool(option.evaluate("""async (element, timeoutMs) => {
          const selected = () => element.classList.contains('option-selected');
          if (selected()) return true;
          return await new Promise(resolve => {
            let settled = false;
            let timer = null;
            const observer = new MutationObserver(() => {
              if (selected()) finish(true);
            });
            const finish = value => {
              if (settled) return;
              settled = true;
              observer.disconnect();
              if (timer !== null) clearTimeout(timer);
              resolve(value);
            };
            observer.observe(element, { attributes: true, attributeFilter: ['class'] });
            timer = setTimeout(() => finish(selected()), timeoutMs);
            if (selected()) finish(true);
          });
        }""", timeout_ms, timeout=timeout_ms + 500))
    except Exception:
        return False


def _visible_filter_dom_click(element: Any) -> bool:
    """仅对仍在视口布局中的筛选控件或选项派发 DOM click。"""
    result = element.evaluate("""element => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const visible = rect.width > 0 && rect.height > 0 &&
        style.display !== 'none' && style.visibility !== 'hidden';
      if (!visible) return false;
      element.click();
      return true;
    }""", timeout=1_500)
    return result is True


def _visible_filter_dropdown_has_options(page: Page) -> bool:
    return bool(page.evaluate("""() => Array.from(document.querySelectorAll(
      '.aurora-select-dropdown:not(.aurora-cascader-dropdown)')).some(node => {
        const style = getComputedStyle(node);
        return node.offsetWidth > 0 && node.offsetHeight > 0 &&
          style.visibility === 'visible' && style.display !== 'none' &&
          !node.classList.contains('aurora-select-dropdown-hidden') &&
          !String(node.className).includes('aurora-slide-up-leave') &&
          node.querySelectorAll('.aurora-select-item-option').length > 0;
      })"""))


def _click_filter_control(page: Page, control: Any, label: str, expected_open: bool) -> None:
    """短超时点击筛选控件；超时后先核对菜单状态，再决定是否派发 DOM click。"""
    try:
        control.click(force=True, timeout=1_500, no_wait_after=True)
        return
    except Exception as click_error:
        expected_state_seen = False
        try:
            expected_state_seen = _visible_filter_dropdown_has_options(page) is expected_open
        except Exception:
            pass
        # click 超时不代表事件一定没发出；若目标开/关状态已出现，不重复切换菜单。
        if expected_state_seen:
            return
        fallback_error: Optional[BaseException] = None
        try:
            if _visible_filter_dom_click(control):
                return
        except Exception as error:
            fallback_error = error
        detail = f"；DOM 回退：{type(fallback_error).__name__}: {fallback_error}" if fallback_error else "；DOM 回退时控件不可见"
        raise RuntimeError(
            f"百应筛选控件「{label}」Locator.click 超时，菜单状态未达到预期{detail}；原始错误：{click_error}"
        ) from click_error


def _click_filter_option(
    option: Any,
    *,
    verify_selected: bool = True,
    confirmation_check: Optional[Callable[[], bool]] = None,
) -> None:
    """短超时点选筛选项；可用菜单外的已应用筛选标签确认单选结果。"""
    try:
        option_class = str(option.get_attribute("class", timeout=1_000) or "")
    except Exception:
        option_class = ""
    try:
        label = " ".join(str(option.inner_text(timeout=1_000) or "").split())[:100]
    except Exception:
        label = ""

    if verify_selected and "option-selected" in option_class:
        return

    click_error: Optional[BaseException] = None
    try:
        option.click(force=True, timeout=1_500, no_wait_after=True)
    except Exception as error:
        click_error = error

    def is_confirmed() -> bool:
        if verify_selected and _wait_filter_option_selected(option):
            return True
        if confirmation_check is not None:
            try:
                return bool(confirmation_check())
            except Exception:
                return False
        return not verify_selected

    if click_error is None:
        if is_confirmed():
            return
        target = f"「{label}」" if label else "（option-selected）"
        raise RuntimeError(
            f"百应筛选选项{target}的 Locator.click 已返回，但等待 option-selected 状态 timeout；未确认筛选生效"
        )

    if is_confirmed():
        return
    if not verify_selected:
        # 确认按钮若已让菜单消失，调用方还会等待菜单关闭；不对已消失的确认项再次点击。
        try:
            if not option.is_visible(timeout=500):
                return
        except Exception:
            pass

    fallback_error: Optional[BaseException] = None
    fallback_clicked = False
    try:
        fallback_clicked = _visible_filter_dom_click(option)
    except Exception as error:
        fallback_error = error
    if fallback_clicked and is_confirmed():
        return

    target = f"「{label}」" if label else f"（{option_class or '筛选项'}）"
    fallback_detail = (
        f"；DOM 回退错误：{type(fallback_error).__name__}: {fallback_error}"
        if fallback_error else "；DOM 回退未确认选中状态"
    )
    raise RuntimeError(
        f"百应筛选选项{target} Locator.click 超时且未确认 option-selected；未假报成功；"
        f"原始错误：{click_error}{fallback_detail}"
    ) from click_error


class CommissionFilterUnconfirmed(RuntimeError):
    """平台佣金筛选结果未核实；仅跳过选品广场，不放宽商品佣金门槛。"""


class BaiyingLoginRequired(RuntimeError):
    """百应列表接口明确返回未登录；不能把账号状态误报成筛选条件失败。"""


def _commission_error_requires_pause(error: BaseException) -> bool:
    """只把账号、浏览器、限流及未结束请求升级为全局停点。"""
    message = str(error).lower()
    return any(marker in message for marker in (
        "11001", "限流", "频控", "冷却", "滑块", "验证码", "安全验证", "风控", "登录",
        "captcha", "verification required", "risk control", "login required", "unauthorized",
        "rate limit", "target page", "target closed", "browser has been closed",
        "context has been closed", "page has been closed", "browser disconnected",
        "connection closed", "disconnected", "websocket", "cdp", "远程调试",
        "浏览器已关闭", "页面已关闭", "连接中断", "net::err_", "request failed",
        "material_list_request_pending", "material_list 返回", "环境危险", "环境异常",
    ))


def _wait_filter_applied_chip(page: Page, label: str, value: str, timeout_ms: int = 1_000) -> bool:
    """在可见的“已筛选”摘要区回读单选条件，避免依赖关闭菜单后已卸载的 option 节点。"""
    try:
        page.wait_for_function("""({label, value}) => {
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const clean = (text) => String(text || '').replace(/\\s+/g, '')
            .replace(/[：:]/g, ':').replace(/[‐‑‒–—−]/g, '-');
          const cleanLabel = (text) => clean(text).replace(/(数量|人数)/g, '');
          const leaves = Array.from(document.querySelectorAll('body *')).filter((node) =>
            node.children.length === 0 && visible(node));
          const heading = leaves.find((node) => clean(node.innerText || node.textContent) === '已筛选');
          const end = leaves.find((node) => clean(node.innerText || node.textContent) === '为你推荐以下商品');
          const startY = heading ? heading.getBoundingClientRect().top : -Infinity;
          const endY = end ? end.getBoundingClientRect().top : Infinity;
          const wantedLabel = cleanLabel(label);
          const wantedValue = clean(value);
          for (const leaf of leaves) {
            const rect = leaf.getBoundingClientRect();
            if (rect.top < startY || rect.top >= endY) continue;
            for (let node = leaf, depth = 0; node && depth < 4; node = node.parentElement, depth += 1) {
              const text = clean(node.innerText || node.textContent);
              if (text.length > 180) continue;
              if (cleanLabel(text).includes(wantedLabel) && text.includes(wantedValue)) return true;
            }
          }
          return false;
        }""", arg={"label": label, "value": value}, timeout=timeout_ms, polling=100)
        return True
    except PlaywrightTimeoutError:
        return False


def _wait_filter_summary_text(page: Page, label: str, value: str, timeout_ms: int = 1_000) -> bool:
    """仅回读“已筛选”结果区；弹窗中的输入与选项不能充当已应用证据。"""
    try:
        page.wait_for_function("""({label, value}) => {
          const clean = (text) => String(text || '').replace(/\\s+/g, '')
            .replace(/[：:]/g, ':').replace(/[‐‑‒–—−]/g, '-');
          const wanted = `${clean(label)}:${clean(value)}`;
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const leaves = Array.from(document.querySelectorAll('body *')).filter((node) =>
            node.children.length === 0 && visible(node));
          const heading = leaves.find((node) => clean(node.innerText || node.textContent) === '已筛选');
          if (!heading) return false;
          const startY = heading.getBoundingClientRect().top - 8;
          const end = leaves.find((node) => clean(node.innerText || node.textContent) === '为你推荐以下商品');
          const endY = end ? end.getBoundingClientRect().top : startY + 140;
          return leaves.some((leaf) => {
            if (leaf.closest('.aurora-select-dropdown')) return false;
            const y = leaf.getBoundingClientRect().top;
            if (y < startY || y >= endY) return false;
            for (let node = leaf, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
              const text = clean(node.innerText || node.textContent);
              if (text.length <= 160 && text.includes(wanted)) return true;
            }
            return false;
          });
        }""", arg={"label": label, "value": value}, timeout=timeout_ms, polling=100)
        return True
    except PlaywrightTimeoutError:
        return False


def _is_retryable_filter_apply_error(error: BaseException) -> bool:
    message = str(error).lower()
    if any(marker in message for marker in ("11001", "验证码", "安全验证", "登录失效", "连接断开", "没有选项")):
        return False
    if "百应页面没有找到筛选项" in message:
        return True
    return any(marker in message for marker in (
        "option-selected", "未确认筛选生效", "locator.click", "下拉菜单仍打开", "筛选应用确认",
        "下拉菜单未稳定加载出选项", "无法定位稳定选项",
    )) and ("timeout" in message or "超时" in message or "未确认" in message or "仍打开" in message or "未稳定" in message or "无法定位稳定" in message)


def _apply_configured_filter_field_once(
    page: Page,
    label: str,
    selected: Sequence[str],
    cached_options: Any,
    warnings: list[str],
) -> None:
    # 上一次点击可能已提交，只是菜单关闭后 option 节点被卸载；先读回筛选摘要，避免重复切换。
    if len(selected) == 1 and _wait_filter_applied_chip(page, label, selected[0], timeout_ms=250):
        return
    control, menu = _open_filter_dropdown(page, label)
    try:
        option_locator = menu.locator(".aurora-select-item-option")
        option_texts = [" ".join(text.split()) for text in option_locator.all_inner_texts()]
        unavailable = [item for item in selected if item not in option_texts]
        if unavailable:
            visible_options = "、".join(option_texts[:12]) or "（页面没有返回选项）"
            raise RuntimeError(
                f"百应当前筛选项「{label}」没有选项「{'、'.join(unavailable)}」；"
                f"页面当前可选：{visible_options}"
            )
        if not isinstance(cached_options, list) or any(item not in cached_options for item in selected):
            # 百应部分筛选项随类目变化；以当前实际页面选项为准。
            warning = f"百应筛选项「{label}」与本机目录不同，已按当前页面选项执行。"
            if warning not in warnings:
                warnings.append(warning)
        multiple = "aurora-select-dropdown-multiple" in (menu.get_attribute("class") or "")
        if multiple:
            desired = set(selected)
            for index, text in enumerate(option_texts):
                if text not in desired:
                    continue
                option = option_locator.nth(index)
                classes = option.get_attribute("class") or ""
                if "option-selected" not in classes:
                    _click_filter_option(option)
            confirm = menu.get_by_text("确 定", exact=True)
            if confirm.count():
                _click_filter_option(confirm, verify_selected=False)
                try:
                    _wait_filter_dropdown_closed(page, timeout_ms=1_500)
                except PlaywrightTimeoutError as error:
                    raise RuntimeError("百应筛选确认按钮点击后，下拉菜单仍打开；未确认筛选应用") from error
        else:
            match_index = next((index for index, text in enumerate(option_texts) if text == selected[0]), None)
            if match_index is None:
                raise RuntimeError(f"百应筛选项「{label}」无法定位选项「{selected[0]}」")
            match = option_locator.nth(match_index)
            if "option-selected" not in (match.get_attribute("class") or ""):
                _click_filter_option(
                    match,
                    confirmation_check=lambda: _wait_filter_applied_chip(
                        page, label, selected[0], timeout_ms=1_000
                    ),
                )
    finally:
        _close_filter_dropdown(page, control)


def _apply_configured_filter_field(
    page: Page,
    label: str,
    selected: Sequence[str],
    cached_options: Any,
    warnings: list[str],
    max_attempts: int = 3,
) -> None:
    last_error: Optional[BaseException] = None
    retry_attempted = False
    for attempt in range(max_attempts):
        try:
            _apply_configured_filter_field_once(page, label, selected, cached_options, warnings)
            return
        except Exception as error:
            last_error = error
            if not _is_retryable_filter_apply_error(error):
                break
            if attempt + 1 >= max_attempts:
                break
            retry_attempted = True
            _wait_pending_filter_request_before_retry(page)
            # 每次重试都会重新打开菜单并回读选中态，避免在多选菜单上重复切换已经选中的项。
            page.wait_for_timeout(250 * (attempt + 1))
    if not retry_attempted:
        if last_error:
            raise last_error
        raise RuntimeError(f"百应筛选项「{label}」没有执行。")
    raise RuntimeError(
        f"FILTER_APPLY_RETRIES_EXHAUSTED：百应筛选项「{label}」自动重试 {max_attempts} 次后仍未确认应用；"
        f"最后原因：{last_error}"
    ) from last_error


def _wait_pending_filter_request_before_retry(page: Page) -> None:
    """存在本轮列表请求时，先等它结束再重试菜单操作，避免请求重叠。"""
    state = getattr(page, "_touguang_filter_retry_collector", None)
    if not isinstance(state, tuple) or len(state) != 2:
        return
    collector, mark = state
    if collector.rate_limit_error_since(mark):
        raise RuntimeError("material_list 返回 11001，停止筛选重试并保留限流冷却。")
    if not collector.pending_since(mark):
        return
    status = _wait_for_material_list_settled_bounded(
        page, collector, mark, window_seconds=8, max_windows=3, quiet_seconds=1.2
    )
    if collector.rate_limit_error_since(mark):
        raise RuntimeError("material_list 返回 11001，停止筛选重试并保留限流冷却。")
    if status == "pending":
        raise RuntimeError(
            "MATERIAL_LIST_REQUEST_PENDING：上一次筛选请求等待 3 个窗口（最多 24 秒）仍未结束；"
            "为避免重复请求，已停止本次筛选重试。"
        )


def _wait_filter_dropdown_closed(page: Page, timeout_ms: int = 1_500) -> None:
    page.wait_for_function("""() => !Array.from(document.querySelectorAll(
      '.aurora-select-dropdown:not(.aurora-cascader-dropdown)')).some(node =>
        node.offsetWidth > 0 && node.offsetHeight > 0 && getComputedStyle(node).visibility === 'visible' &&
        !node.classList.contains('aurora-select-dropdown-hidden') &&
        !String(node.className).includes('aurora-slide-up-leave'))""",
      timeout=timeout_ms, polling=100)


def _wait_filter_confirm_stable(page: Page, confirm: Any, timeout_ms: int = 2_500) -> None:
    """等待 Aurora 确认按钮完成入场/布局动画，再执行真实点击。"""
    deadline = time.monotonic() + timeout_ms / 1000
    previous_box: Optional[tuple[float, float, float, float]] = None
    stable_since: Optional[float] = None
    last_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            if not confirm.is_visible(timeout=300) or not confirm.is_enabled(timeout=300):
                stable_since = None
                previous_box = None
            else:
                box = confirm.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    current_box = tuple(round(float(box.get(key, 0)), 1) for key in ("x", "y", "width", "height"))
                    if current_box == previous_box:
                        if stable_since is None:
                            stable_since = time.monotonic()
                        if time.monotonic() - stable_since >= 0.18:
                            return
                    else:
                        stable_since = None
                    previous_box = current_box
        except Exception as error:
            if _commission_error_requires_pause(error):
                raise
            last_error = error
            stable_since = None
            previous_box = None
        page.wait_for_timeout(60)
    raise RuntimeError("选品广场佣金区间确认按钮连续 2.5 秒未稳定；未执行重复点击") from last_error


def _click_filter_confirm_stably(
    page: Page,
    confirm: Any,
    confirmation_check: Optional[Callable[[], bool]] = None,
) -> None:
    """确认按钮最多点击一次；不确定时先等弹窗关闭再读应用状态。"""
    try:
        _wait_filter_confirm_stable(page, confirm, timeout_ms=2_500)
    except RuntimeError as error:
        if _commission_error_requires_pause(error):
            raise
        raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：确认按钮未稳定，未点击") from error
    try:
        confirm.scroll_into_view_if_needed(timeout=1_000)
    except Exception:
        pass
    click_error: Optional[BaseException] = None
    try:
        confirm.click(timeout=2_500, no_wait_after=True)
    except (PlaywrightTimeoutError, TimeoutError) as error:
        if _commission_error_requires_pause(error):
            raise
        click_error = error
    try:
        _wait_filter_dropdown_closed(page, timeout_ms=2_000)
    except PlaywrightTimeoutError as error:
        raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：确认后弹窗未关闭，提交结果不明") from (click_error or error)
    if click_error is not None:
        if confirmation_check is not None and confirmation_check():
            return
        raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：点击收尾超时，已停止重复提交") from click_error


def _stabilize_background_filter_dropdown(page: Page) -> None:
    """后台标签页可能暂停 Aurora 入场动画，将菜单的起始缩放状态固定为可定位状态。"""
    page.evaluate("""() => {
      const styleId = 'touguang-background-filter-dropdown-fix';
      if (document.getElementById(styleId)) return;
      const style = document.createElement('style');
      style.id = styleId;
      style.textContent = '.aurora-select-dropdown.aurora-slide-up-enter-start {' +
        ' transform: none !important; opacity: 1 !important; }';
      (document.head || document.documentElement).appendChild(style);
    }""")


def _open_filter_dropdown(page: Page, label: str) -> tuple[Any, Any]:
    _stabilize_background_filter_dropdown(page)
    control = _filter_control(page, label)
    last_error = "百应筛选项下拉菜单没有稳定显示"
    for attempt in range(2):
        # 等 Aurora 收到离场状态，避免把正在关闭的旧菜单当作新菜单。
        menu_already_open = False
        try:
            page.keyboard.press("Escape")
            _wait_filter_dropdown_closed(page, timeout_ms=1_000)
        except PlaywrightTimeoutError:
            # 后台页偶尔收不到 Escape；点一次目标控件后检查它是否已切到目标菜单。
            try:
                _click_filter_control(page, control, label, expected_open=False)
                try:
                    _wait_filter_dropdown_closed(page, timeout_ms=1_000)
                except PlaywrightTimeoutError:
                    menu_already_open = True
            except Exception:
                pass
        except Exception:
            pass

        if not menu_already_open:
            _click_filter_control(page, control, label, expected_open=True)
        try:
            page.wait_for_function("""() => Array.from(document.querySelectorAll(
              '.aurora-select-dropdown:not(.aurora-cascader-dropdown)')).some(node =>
                node.offsetWidth > 0 && node.offsetHeight > 0 && getComputedStyle(node).visibility === 'visible' &&
                !node.classList.contains('aurora-select-dropdown-hidden') &&
                !String(node.className).includes('aurora-slide-up-leave') &&
                node.querySelectorAll('.aurora-select-item-option').length > 0)""",
              # 切换类目后百应会重载动态筛选选项；首次打开失败时自动重新打开一次。
              timeout=5_000, polling=100)
        except PlaywrightTimeoutError as error:
            last_error = f"百应筛选项「{label}」的下拉菜单未稳定加载出选项"
            _close_filter_dropdown(page, control)
            if attempt == 0:
                page.wait_for_timeout(250)
                continue
            raise RuntimeError(last_error) from error

        deadline = time.monotonic() + 1.5
        while True:
            dropdowns = page.locator(".aurora-select-dropdown:not(.aurora-cascader-dropdown)")
            for index in range(dropdowns.count() - 1, -1, -1):
                dropdown = dropdowns.nth(index)
                classes = str(dropdown.get_attribute("class") or "")
                if "aurora-select-dropdown-hidden" in classes or "aurora-slide-up-leave" in classes:
                    continue
                if dropdown.is_visible() and dropdown.locator(".aurora-select-item-option").count() > 0:
                    return control, dropdown
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(100)
        last_error = f"百应筛选项「{label}」的下拉菜单虽然显示，但无法定位稳定选项"
        _close_filter_dropdown(page, control)
        if attempt == 0:
            page.wait_for_timeout(250)
    raise RuntimeError(last_error)


def _close_filter_dropdown(page: Page, control: Any) -> None:
    try:
        page.keyboard.press("Escape")
        _wait_filter_dropdown_closed(page, timeout_ms=1_000)
    except PlaywrightTimeoutError:
        try:
            _click_filter_control(page, control, "关闭筛选菜单", expected_open=False)
            _wait_filter_dropdown_closed(page, timeout_ms=1_000)
        except Exception:
            pass
    except Exception:
        pass


def _hover_category_expand(expand: Any) -> None:
    """通过真实鼠标悬停展开百应级联分支，避免后台页的点击事件切换或关闭弹层。"""
    # 百应级联菜单在箭头 hover 时加载子列；直接 DOM click 不触发 React，
    # force click 在重置筛选后的菜单状态下偶发关闭弹层。
    expand.hover(force=True, timeout=3_000)


def _click_category_item(item: Any) -> None:
    """通过真实鼠标事件切换类目项，避免 DOM click 未触发百应 React 状态。"""
    item.locator(".aurora-cascader-menu-item-content").click(force=True, timeout=3_000, no_wait_after=True)


def _collect_category_tree(page: Page, roots: Any) -> list[Dict[str, Any]]:
    """只展开百应类目箭头读取三级目录，不勾选或改变当前类目筛选。"""
    result: list[Dict[str, Any]] = []
    for raw_root in roots if isinstance(roots, list) else []:
        if not isinstance(raw_root, dict):
            continue
        root_id = str(raw_root.get("id", ""))
        root_name = str(raw_root.get("name", "")).strip()
        if not root_id or not root_name or not re.fullmatch(r"[\w-]+", root_id):
            continue
        root = page.locator(f".merch-filter-cate-cascader-item.business_id_{root_id}")
        if not root.count():
            result.append({"id": root_id, "name": root_name, "children": []})
            continue
        control = root.first.locator(".combine-select-content")
        try:
            dropdown = _open_category_dropdown(page, root_id, control)
            first_column = dropdown.locator(".aurora-cascader-menu").first
            rows = first_column.locator(".aurora-cascader-menu-item[data-path-key]")
            children: list[Dict[str, Any]] = []
            for row_index in range(rows.count()):
                row = first_column.locator(".aurora-cascader-menu-item[data-path-key]").nth(row_index)
                child_id = str(row.get_attribute("data-path-key") or "").strip()
                child_name = " ".join((row.get_attribute("title") or row.inner_text()).split())
                if not child_id or not child_name or child_name == "不限":
                    continue
                child: Dict[str, Any] = {"id": child_id, "name": child_name, "children": []}
                expand = row.locator(".aurora-cascader-menu-item-expand-icon")
                if expand.count():
                    _hover_category_expand(expand)
                    prefix = child_id + CATEGORY_PATH_SEPARATOR
                    try:
                        page.wait_for_function("""(args) => {
                          const dropdowns = Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).filter(node => node.offsetWidth && node.offsetHeight);
                          const columns = dropdowns.at(-1)?.querySelectorAll('.aurora-cascader-menu');
                          if (!columns || columns.length < 2) return false;
                          return Array.from(columns[1].querySelectorAll('.aurora-cascader-menu-item[data-path-key]'))
                            .some(node => (node.getAttribute('data-path-key') || '').startsWith(args.prefix));
                        }""", arg={"prefix": prefix}, timeout=8_000, polling=CATEGORY_PAGE_POLLING_MS)
                    except PlaywrightTimeoutError:
                        # 个别二级类目可能没有三级项，或平台接口本次没有及时返回。
                        # 返回已读取的父级目录，前端会与本机完整缓存合并，不能让单个分支拖垮整次同步。
                        children.append(child)
                        continue
                    columns = dropdown.locator(".aurora-cascader-menu")
                    third_column = columns.nth(1)
                    third_rows = third_column.locator(".aurora-cascader-menu-item[data-path-key]")
                    for third_index in range(third_rows.count()):
                        third = third_column.locator(".aurora-cascader-menu-item[data-path-key]").nth(third_index)
                        third_id = str(third.get_attribute("data-path-key") or "").strip()
                        third_name = " ".join((third.get_attribute("title") or third.inner_text()).split())
                        if third_id and third_id.startswith(prefix) and third_name and third_name != "不限":
                            child["children"].append({"id": third_id, "name": third_name, "children": []})
                children.append(child)
            result.append({"id": root_id, "name": root_name, "children": children})
        except PlaywrightTimeoutError:
            # 单个一级目录的百应弹层未及时返回时保留该一级节点；
            # 调用方会与本机完整目录缓存递归合并，不让平台瞬时延迟使整次同步失败。
            result.append({"id": root_id, "name": root_name, "children": []})
        finally:
            _close_category_dropdown(page, root_id, control)
    return result


def _open_category_dropdown(
    page: Page, root_id: str, control: Any, expected_item_id: Optional[str] = None
) -> Any:
    # data-active 表示该一级类目已有选值，不代表 portal 菜单当前是否可见。
    # 如果目标菜单已在当前 portal 展示则直接复用，避免重复点击把弹层关闭。
    if expected_item_id:
        existing = _category_dropdown_for_item(page, expected_item_id)
        if existing is not None:
            return existing

    # 先收起残留弹层；确认已关闭后再打开，避免 toggle 状态不确定导致菜单反复开合。
    _close_category_dropdown(page, root_id, control)
    if page.locator(ACTIVE_CATEGORY_DROPDOWN_SELECTOR).count():
        existing = _category_dropdown_for_item(page, expected_item_id) if expected_item_id else None
        if existing is not None:
            return existing
        raise RuntimeError("百应上一次类目弹层未能关闭，已停止操作以避免选错分支")

    # 百应按钮文字上方可能停留 tooltip 覆盖层；点文字中心会落到覆盖层而没有触发菜单。
    # 优先点右侧展开箭头，并保留旧 DOM 的文字控件回退。
    _click_category_control(page, control)
    if expected_item_id:
        _wait_category_item_present(page, expected_item_id, timeout_ms=12_000)
        # 百应级联菜单的 portal 会在数据出现后继续做一次重挂载；
        # 等待条件可能先命中旧 portal，短暂轮询到稳定包含目标项的菜单再继续。
        deadline = time.monotonic() + 2.0
        while True:
            dropdown = _category_dropdown_for_item(page, expected_item_id)
            if dropdown is not None:
                return dropdown
            if time.monotonic() >= deadline:
                raise RuntimeError(f"百应打开类目菜单后未找到目标类目 ID {expected_item_id}")
            page.wait_for_timeout(100)

    # 目录采集时还没有已知的目标子类目，等待当前菜单至少加载出真实选项。
    page.wait_for_function("""(id) => {
      const dropdowns = Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).filter(node =>
        node.offsetWidth && node.offsetHeight && getComputedStyle(node).visibility === 'visible' &&
        !String(node.className).includes('aurora-slide-up-leave') &&
        !node.classList.contains('aurora-select-dropdown-hidden'));
      const first = dropdowns.at(-1)?.querySelector('.aurora-cascader-menu');
      const hasLoadedCategory = first && Array.from(first.querySelectorAll('.aurora-cascader-menu-item[data-path-key]'))
        .some(item => (item.getAttribute('data-path-key') || '').trim().length > 0);
      return !!hasLoadedCategory;
    }""", arg=root_id, timeout=12_000, polling=CATEGORY_PAGE_POLLING_MS)
    dropdowns = page.locator(ACTIVE_CATEGORY_DROPDOWN_SELECTOR)
    dropdowns.last.wait_for(state="visible", timeout=3_000)
    return page.locator(ACTIVE_CATEGORY_DROPDOWN_SELECTOR).last


def _wait_category_dropdowns_hidden(page: Page, timeout_ms: int = 1_200) -> None:
    page.wait_for_function("""() => !Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).some(node => {
      const style = getComputedStyle(node);
      return node.offsetWidth > 0 && node.offsetHeight > 0 && style.display !== 'none' &&
        style.visibility === 'visible' && !node.classList.contains('aurora-select-dropdown-hidden');
    })""", timeout=timeout_ms, polling=CATEGORY_PAGE_POLLING_MS)


def _click_category_control(page: Page, control: Any) -> None:
    """点击百应类目控件右侧箭头，避开覆盖在文字上的 tooltip。"""
    try:
        neutral = page.evaluate("""() => {
          const width = window.innerWidth || document.documentElement.clientWidth || 2;
          const height = window.innerHeight || document.documentElement.clientHeight || 2;
          const points = [[width - 3, height - 3], [width - 3, 3], [3, height - 3], [3, 3]];
          for (const [x, y] of points) {
            const target = document.elementFromPoint(x, y);
            if (!target || !target.closest('button,a,input,select,textarea,[role="button"],[aria-haspopup],[data-variant]'))
              return {x, y};
          }
          return {x: 3, y: 3};
        }""")
        if isinstance(neutral, dict):
            page.mouse.move(float(neutral.get("x", 3)), float(neutral.get("y", 3)))
            page.wait_for_timeout(100)
    except Exception:
        # 某些测试替身或旧版页面不提供坐标 API；箭头点击仍可继续尝试。
        pass
    try:
        parent = control.locator("xpath=..")
        # 打开时是 down，展开时会变成 up；关闭回退同样需要点当前箭头。
        arrow = parent.locator('[aria-label="down"], [aria-label="up"]')
        count = arrow.count()
        if isinstance(count, int) and count > 0 and arrow.is_visible():
            arrow.click(force=True, timeout=3_000, no_wait_after=True)
            return
    except Exception:
        # 老版百应 DOM 或测试替身没有箭头定位时，沿用文字控件。
        pass
    control.click(force=True, timeout=3_000, no_wait_after=True)


def _close_category_dropdown(page: Page, root_id: str, control: Any) -> None:
    visible_dropdowns = page.locator(".aurora-cascader-dropdown:visible")
    if not visible_dropdowns.count():
        return
    # 后台页可能没有键盘焦点，优先发 Escape；无效时对所属一级控件发送真实鼠标点击。
    try:
        page.keyboard.press("Escape")
        _wait_category_dropdowns_hidden(page, timeout_ms=1_200)
        return
    except PlaywrightTimeoutError:
        pass
    for _attempt in range(2):
        visible_dropdowns = page.locator(".aurora-cascader-dropdown:visible")
        if not visible_dropdowns.count():
            return
        leaving = page.evaluate("""() => Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).some(node => {
          const style = getComputedStyle(node);
          return node.offsetWidth > 0 && node.offsetHeight > 0 && style.visibility === 'visible' &&
            String(node.className).includes('aurora-slide-up-leave');
        })""")
        if leaving:
            try:
                _wait_category_dropdowns_hidden(page, timeout_ms=1_200)
                return
            except PlaywrightTimeoutError:
                continue
        try:
            _click_category_control(page, control)
            _wait_category_dropdowns_hidden(page, timeout_ms=1_200)
            return
        except PlaywrightTimeoutError:
            # 动画中的 Aurora portal 偶尔需要第二次真实输入事件；每次都先重新检查状态。
            continue
        except Exception:
            return


def _category_item_by_id(column: Any, category_id: str) -> Optional[Any]:
    """按精确 ID 或旧缓存的短 ID 查找百应级联项。"""
    exact = column.locator(_category_item_selector(category_id))
    if exact.count():
        return exact.first
    target_id = category_id.rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
    if not target_id:
        return None
    fallback = column.locator(_category_item_selector(target_id, suffix=True))
    return fallback.first if fallback.count() == 1 else None


def _category_item_selector(category_id: str, suffix: bool = False) -> str:
    """构造按平台类目路径 ID 定位的选择器，避免遍历会被重挂载的菜单节点。"""
    value = str(category_id).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")
    operator = "$=" if suffix else "="
    return f'.aurora-cascader-menu-item[data-path-key{operator}"{value}"]'


def _category_path_id(category_id: str, parent_id: Optional[str] = None) -> str:
    """把缓存中的三级短 ID 转成百应当前菜单使用的父子路径 ID。"""
    if CATEGORY_PATH_SEPARATOR in category_id or not parent_id:
        return category_id
    return f"{parent_id}{CATEGORY_PATH_SEPARATOR}{category_id}"


def _open_category_branch(page: Page, root_id: str, second_id: str, third_id: str, dropdown: Any) -> tuple[Any, Any]:
    """确保指定二级分支已展开，并返回当前可见的三级选项。"""
    third_path_id = _category_path_id(third_id, second_id)
    columns = dropdown.locator(".aurora-cascader-menu")
    if columns.count() > 1:
        leaf = _category_item_by_id(columns.nth(1), third_path_id)
        if leaf is not None and leaf.is_visible():
            return dropdown, leaf

    first_column = dropdown.locator(".aurora-cascader-menu").first
    item = first_column.locator(f'.aurora-cascader-menu-item[data-path-key="{second_id}"]')
    if not item.count():
        # 账号 profile 首次打开类目时会先挂载“不限”占位项，再异步加载真实类目。
        # 等待目标 ID 出现，避免把占位项误判为菜单加载完成。
        try:
            _wait_category_item_present(page, second_id, timeout_ms=12_000)
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"百应菜单等待 12 秒后仍未加载二级类目 ID {second_id}，请确认登录页面和类目目录后重试") from error
        dropdown = _category_dropdown_for_item(page, second_id)
        if dropdown is None:
            raise RuntimeError(f"百应展开菜单后未找到二级类目 ID {second_id}")
        first_column = dropdown.locator(".aurora-cascader-menu").first
        item = first_column.locator(f'.aurora-cascader-menu-item[data-path-key="{second_id}"]')
    if not item.count():
        raise RuntimeError(f"百应当前菜单等待后仍找不到二级类目 ID {second_id}，请刷新百应筛选项后重试")
    expand = item.locator(".aurora-cascader-menu-item-expand-icon")
    if not expand.count():
        raise RuntimeError(f"百应二级类目 {second_id} 当前没有三级展开箭头")
    expand.wait_for(state="visible", timeout=3_000)
    _hover_category_expand(expand)

    try:
        _wait_category_item_present(page, third_path_id, timeout_ms=8_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(f"百应菜单等待 8 秒后仍未加载三级类目 ID {third_path_id}") from error
    deadline = time.monotonic() + 2.0
    while True:
        dropdown = _category_dropdown_for_item(page, third_path_id)
        if dropdown is not None:
            columns = dropdown.locator(".aurora-cascader-menu")
            for index in range(columns.count()):
                leaf = _category_item_by_id(columns.nth(index), third_path_id)
                if leaf is not None and leaf.is_visible():
                    return dropdown, leaf
        if time.monotonic() >= deadline:
            raise RuntimeError(f"百应三级菜单已展开，但未找到三级类目 ID {third_id}")
        page.wait_for_timeout(100)


def _open_category_branch_with_retry(
    page: Page,
    root_id: str,
    second_id: str,
    third_id: str,
    control: Any,
    dropdown: Any,
    allow_second_level_retry: bool = False,
) -> tuple[Any, Any]:
    """菜单路径短暂未定位到时有限补试；二级缺失只允许在未点击前重开。"""
    try:
        return _open_category_branch(page, root_id, second_id, third_id, dropdown)
    except RuntimeError as error:
        message = str(error)
        retryable_menu_errors = ["未加载三级类目 ID"]
        if allow_second_level_retry:
            retryable_menu_errors.extend((
                "未找到二级类目 ID",
                "当前菜单等待后仍找不到二级类目",
            ))
        if not any(marker in message for marker in retryable_menu_errors):
            raise
        # 补试只重开并定位菜单，不重复选择；二级类目缺失的补试由调用方
        # 确认当前流程尚未点击类目后显式开启。
        page.wait_for_timeout(500)
        refreshed = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
        return _open_category_branch(page, root_id, second_id, third_id, refreshed)


def _category_item_is_checked(item: Any) -> Optional[bool]:
    """短超时读取类目状态；菜单重挂载期间的未知状态不能按未选中处理。"""
    try:
        value = item.get_attribute("aria-checked", timeout=1_500)
    except TypeError:
        # 兼容只提供最小 get_attribute 接口的测试替身。
        value = item.get_attribute("aria-checked")
    except PlaywrightTimeoutError:
        return None
    return value == "true"


def _click_category_and_confirm(
    page: Page,
    root_id: str,
    second_id: str,
    third_id: str,
    control: Any,
    dropdown: Any,
    item: Any,
) -> tuple[Any, Any]:
    """只在类目仍未勾选时补点一次，避免漏点或把已勾选项误取消。"""
    checked = _category_item_is_checked(item)
    if checked is True:
        return dropdown, item
    if checked is None:
        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
        dropdown, item = _open_category_branch_with_retry(
            page, root_id, second_id, third_id, control, dropdown,
            allow_second_level_retry=True,
        )
        checked = _category_item_is_checked(item)
        if checked is True:
            return dropdown, item
        if checked is None:
            raise RuntimeError(f"百应三级类目 ID {third_id} 状态暂时无法确认，未重复点击")
    _click_category_item(item)
    category_path_id = _category_path_id(third_id, second_id)
    try:
        _wait_category_item_checked(page, category_path_id, timeout_ms=2_500)
    except PlaywrightTimeoutError:
        pass
    dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
    dropdown, item = _open_category_branch_with_retry(
        page, root_id, second_id, third_id, control, dropdown
    )
    checked = _category_item_is_checked(item)
    if checked is True:
        return dropdown, item
    if checked is None:
        raise RuntimeError(f"百应三级类目 ID {third_id} 状态暂时无法确认，未重复点击")

    # 首击后等待完整状态窗口并重新读取仍为 false，才补点一次。
    page.wait_for_timeout(300)
    _click_category_item(item)
    try:
        _wait_category_item_checked(page, category_path_id, timeout_ms=2_500)
    except PlaywrightTimeoutError:
        pass
    dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
    dropdown, item = _open_category_branch_with_retry(
        page, root_id, second_id, third_id, control, dropdown
    )
    checked = _category_item_is_checked(item)
    if checked is True:
        return dropdown, item
    if checked is None:
        raise RuntimeError(f"百应三级类目 ID {third_id} 状态暂时无法确认，未重复点击")
    name = " ".join((item.get_attribute("title", timeout=1_000) or item.inner_text()).split())
    raise RuntimeError(f"百应没有应用三级类目「{name or third_id}」")


def collect_filter_catalog(page: Page, include_category_tree: bool = True) -> Dict[str, Any]:
    """从当前百应页面已渲染的筛选器和下拉菜单读取字段与选项，不构造平台选项。"""
    raw = page.evaluate("""() => ({
      fields: Array.from(document.querySelectorAll('.merch-filter-virtual-table .table-row .combine-select-content'))
        .filter((node) => !node.closest('.merch-filter-cate-cascader-item'))
        .map((node) => {
          const r = node.getBoundingClientRect();
          const row = node.closest('.table-row');
          const section = (row?.querySelector('.table-cell.label')?.innerText || '').trim();
          return { label: (node.innerText || '').trim(), section, x: r.x, y: r.y, h: r.height };
        })
        .filter((node) => node.section === '带货情况' || node.section === '商品信息'),
      roots: Array.from(document.querySelectorAll('.merch-filter-cate-cascader-item'))
        .map((node) => ({ id: (String(node.className).match(/business_id_([\\w-]+)/) || [])[1] || '',
          name: (node.querySelector('.combine-select-content')?.innerText || '').trim() })),
      features: (() => {
        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const visibleLeaves = Array.from(document.querySelectorAll('body *')).filter((node) => {
          if (node.children.length || !clean(node.innerText)) return false;
          const rect = node.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(node).visibility === 'visible';
        });
        const heading = visibleLeaves.find((node) => clean(node.innerText) === '特色货品');
        if (!heading) return [];
        const section = heading.closest('.merch-filter-virtual-table') || document.body;
        const headingTop = heading.getBoundingClientRect().top;
        const endMarkers = new Set(['为你推荐以下商品', '为你找到以下商品', '商品列表']);
        const boundary = visibleLeaves
          .filter((node) => section.contains(node) && endMarkers.has(clean(node.innerText)) && node.getBoundingClientRect().top > headingTop)
          .reduce((top, node) => Math.min(top, node.getBoundingClientRect().top), Number.POSITIVE_INFINITY);
        const excluded = new Set(['特色货品', '已筛选', '重置筛选', '为你推荐以下商品', '为你找到以下商品', '商品列表', '全部', '展开', '收起']);
        return visibleLeaves.filter((node) => {
          if (!section.contains(node)) return false;
          const label = clean(node.innerText);
          const top = node.getBoundingClientRect().top;
          return label && label.length < 40 && !excluded.has(label) && top >= headingTop && top < boundary;
        }).map((node) => clean(node.innerText));
      })(),
      bodyLines: (document.body?.innerText || '').split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean)
    })""")
    category_roots = _collect_category_tree(page, raw.get("roots", [])) if include_category_tree else []
    fields = [field for field in raw.get("fields", []) if field.get("label")]
    result: Dict[str, Any] = {}
    multiple_fields: set[str] = set()
    for field in fields:
        control = None
        try:
            control, menu = _open_filter_dropdown(page, field["label"])
            options = [" ".join(text.split()) for text in menu.locator(".aurora-select-item-option").all_inner_texts()]
            result[field["label"]] = list(dict.fromkeys(option for option in options if option))
            if "aurora-select-dropdown-multiple" in (menu.get_attribute("class") or ""):
                multiple_fields.add(field["label"])
        except RuntimeError:
            # 百应可能按账号权限临时隐藏某个下拉层；保留字段并返回空选项，
            # 由客户端与上次成功同步的本机目录合并，不能让一个字段中断整次同步。
            result[field["label"]] = []
        finally:
            if control is not None:
                _close_filter_dropdown(page, control)

    grouped: Dict[str, list[Dict[str, Any]]] = {"带货情况": [], "商品信息": [], "特色货品": []}
    for field in fields:
        label = field["label"]
        heading = field.get("section", "")
        if heading in grouped:
            grouped[heading].append({"label": label, "options": result.get(label, []), "multiple": label in multiple_fields})

    features = [
        " ".join(str(item).split())
        for item in raw.get("features", [])
        if isinstance(item, str) and item.strip()
    ]
    if not features:
        lines = raw.get("bodyLines", [])
        try:
            feature_start = lines.index("特色货品") + 1
            for line in lines[feature_start:]:
                if line in {"已筛选", "重置筛选", "为你推荐以下商品", "为你找到以下商品", "商品列表", "全部", "展开", "收起"}:
                    break
                if line and len(line) < 40:
                    features.append(line)
        except ValueError:
            pass
    return {
        "ok": True,
        "pagePath": urlparse(page.url).path,
        "filters": result,
        "filterGroups": grouped,
        "features": list(dict.fromkeys(features)),
        "categoryRoots": category_roots,
    }


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        for key in ("origin", "value", "count", "amount", "text"):
            if key in value:
                parsed = _number(value[key])
                if parsed is not None:
                    return parsed
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _source_count(value: Any) -> Optional[float]:
    """仅用于热点主题数量，保留页面上的万/千/亿单位。"""
    if not isinstance(value, str):
        return _number(value)
    match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([千亿万]?)\+?\s*", value)
    if not match:
        return None
    return float(match.group(1).replace(",", "")) * {"": 1, "千": 1_000, "万": 10_000, "亿": 100_000_000}[match.group(2)]


def _find_metric(value: Any, keys: Sequence[str], depth: int = 0) -> Optional[float]:
    if depth > 8 or not isinstance(value, (dict, list)):
        return None
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                found = _number(value[key])
                if found is not None:
                    return found
        for child in value.values():
            found = _find_metric(child, keys, depth + 1)
            if found is not None:
                return found
    else:
        for child in value:
            found = _find_metric(child, keys, depth + 1)
            if found is not None:
                return found
    return None


def _trend_analysis(product_info: Dict[str, Any]) -> Dict[str, Any]:
    series = product_info.get("sale_axis")
    if not isinstance(series, list) or len(series) < 3:
        return {"known": False, "status": "unknown", "slope": None, "points": 0}
    values: list[tuple[float, float]] = []
    for point in series:
        if not isinstance(point, dict):
            continue
        x = _number(point.get("x"))
        y = _number(point.get("y"))
        if x is not None and y is not None:
            values.append((x, y))
    values.sort(key=lambda item: item[0])
    if len(values) < 3:
        return {"known": False, "status": "unknown", "slope": None, "points": len(values)}
    mean_x = sum(item[0] for item in values) / len(values)
    mean_y = sum(item[1] for item in values) / len(values)
    denominator = sum((x - mean_x) ** 2 for x, _ in values)
    if denominator <= 0:
        return {"known": False, "status": "unknown", "slope": None, "points": len(values)}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in values) / denominator
    status = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    return {"known": True, "status": status, "slope": slope, "points": len(values)}


def _trend_is_rising(product_info: Dict[str, Any]) -> bool:
    return _trend_analysis(product_info).get("status") == "rising"


def _promotion_items(payload: Any) -> list[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("summary_promotions")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _enabled_priority_source_order(settings: Dict[str, Any]) -> list[str]:
    config = settings.get("sources") if isinstance(settings.get("sources"), dict) else {}
    enabled = {
        "hotSpot": config.get("hotSpotEnabled", True) is not False,
        "allianceRank": config.get("allianceRankEnabled", True) is not False,
    }
    raw = config.get("priority") if isinstance(config.get("priority"), list) else []
    order = [str(value) for value in raw if str(value) in BAIYING_PRIORITY_SOURCES]
    for source_key in BAIYING_PRIORITY_SOURCES:
        if source_key not in order:
            order.append(source_key)
    return [source_key for source_key in order if enabled.get(source_key, False)]


def _category_id_tail(value: Any) -> str:
    return str(value or "").strip().rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]


def _expected_category_ids(settings: Dict[str, Any]) -> set[str]:
    selection = settings.get("categorySelection")
    if not isinstance(selection, dict):
        return set()
    for level in ("level3", "level2", "level1"):
        values = selection.get(level)
        if isinstance(values, list):
            ids = {_category_id_tail(value) for value in values if _category_id_tail(value)}
            if ids:
                return ids
    return set()


def _response_request_body(response: Any) -> Optional[Any]:
    """兼容 CDP 未能预解析 JSON 的请求，回退读取 Playwright 原始请求体。"""
    request = getattr(response, "request", None)
    if request is None:
        return None
    try:
        body = getattr(request, "post_data_json", None)
        if callable(body):
            body = body()
    except Exception:
        body = None
    if isinstance(body, (dict, list)):
        return body
    try:
        raw_body = getattr(request, "post_data", None)
        if callable(raw_body):
            raw_body = raw_body()
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8", errors="strict")
        if isinstance(raw_body, str) and raw_body.strip():
            body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return body if isinstance(body, (dict, list)) else None


def _response_matches_category(response: Any, settings: Dict[str, Any]) -> bool:
    """只接受请求体明确包含当前选中类目 ID 的商品列表响应。"""
    expected_ids = _expected_category_ids(settings)
    if not expected_ids:
        return True
    body = _response_request_body(response)
    if not isinstance(body, (dict, list)):
        return False

    category_values: list[str] = []

    def collect_values(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect_values(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_values(nested)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                category_values.append(_category_id_tail(text))

    def find_category(node: Any) -> None:
        if isinstance(node, dict):
            selector_name = next((
                str(node.get(name, "")).casefold()
                for name in ("key", "name", "field", "filter_key")
                if str(node.get(name, "")).casefold() in {"mendelcid", "mendel_cid"}
            ), None)
            if selector_name:
                collect_values(node.get("value"))
            for key, value in node.items():
                if str(key).casefold() in {"mendelcid", "mendel_cid"}:
                    collect_values(value)
                else:
                    find_category(value)
        elif isinstance(node, list):
            for value in node:
                find_category(value)

    find_category(body)
    selected = settings.get("categorySelection") if isinstance(settings.get("categorySelection"), dict) else {}
    selected_path = [str(selected[level][0]) for level in ("level1", "level2", "level3")
                     if isinstance(selected.get(level), list) and len(selected[level]) == 1]
    single_path = all(len(selected.get(level, [])) <= 1 for level in ("level1", "level2", "level3"))
    if single_path and selected_path:
        allowed = {_category_id_tail(value) for value in selected_path}
        observed = set(category_values)
        return _category_id_tail(selected_path[-1]) in observed and observed.issubset(allowed)
    return bool(expected_ids.intersection(category_values))


def _response_matches_search_keyword(response: Any, settings: Dict[str, Any]) -> bool:
    """只接受请求词与当前批次设置完全相同的商品列表响应。"""
    body = _response_request_body(response)
    if not isinstance(body, dict) or "search_text" not in body:
        return False
    expected = str(settings.get("searchKeyword") or "").strip()
    actual = str(body.get("search_text") or "").strip()
    return actual == expected


def _title_banned_word(title: Any, settings: Optional[Dict[str, Any]]) -> str:
    """返回标题命中的屏蔽词；封建迷信词作为系统规则始终生效。"""
    configured_words = settings.get("bannedWords", []) if isinstance(settings, dict) else []
    if not isinstance(configured_words, list):
        configured_words = []
    words = dict.fromkeys(
        str(word).strip()
        for word in [*configured_words, *SUPERSTITION_BANNED_WORDS]
        if str(word).strip()
    )
    normalized_title = str(title or "").casefold()
    return next((word for word in words if word.casefold() in normalized_title), "")


def _screen_candidate_titles(
    candidates: Sequence[Dict[str, Any]], settings: Optional[Dict[str, Any]]
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """过滤实时结果或旧检查点中的屏蔽标题，并保留可审计的命中记录。"""
    eligible: list[Dict[str, Any]] = []
    rejected: list[Dict[str, Any]] = []
    for candidate in candidates:
        matched_word = _title_banned_word(candidate.get("title"), settings)
        if not matched_word:
            eligible.append(candidate)
            continue
        reason = f"商品标题命中屏蔽词「{matched_word}」"
        existing_reasons = candidate.get("thresholdReasons")
        reasons = [str(value) for value in existing_reasons if str(value)] if isinstance(existing_reasons, list) else []
        rejected.append({
            **candidate,
            "titleBannedWord": matched_word,
            "titleFilterExcluded": True,
            "titleFilterWasSelected": candidate.get("selected") is True,
            "analysisStatus": "商品标题命中屏蔽词",
            "thresholdReasons": list(dict.fromkeys([*reasons, reason])),
            "eligible": False,
            "cartAdded": candidate.get("cartAdded") is True,
            "skipped": True,
            "status": reason + "；已排除，未执行加车。",
            "selected": False,
        })
    return eligible, rejected


def _candidate_rows(
    payload: Any,
    settings: Dict[str, Any],
    limit: int,
    preserve_product_ids: Optional[Sequence[str]] = None,
) -> tuple[list[Dict[str, Any]], list[str], list[Dict[str, Any]]]:
    raw_rows = _promotion_items(payload)
    filters = settings.get("filters", {}) if isinstance(settings.get("filters"), dict) else {}
    thresholds = settings.get("thresholds", {}) if isinstance(settings.get("thresholds"), dict) else {}
    pool_priority = [str(pool) for pool in settings.get("poolPriority", []) if str(pool)]
    fallback = settings.get("poolFallback", True) is not False
    source_membership = settings.get("_sourceMembership") if isinstance(settings.get("_sourceMembership"), dict) else {}
    source_order = _enabled_priority_source_order(settings)
    messages: list[str] = []
    rejected: list[Dict[str, Any]] = []
    ranked: list[tuple[tuple[int, int, int, float], Dict[str, Any]]] = []

    for card_index, row in enumerate(raw_rows):
        base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
        info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
        promotion = base.get("promotion_info") if isinstance(base.get("promotion_info"), dict) else {}
        shop = base.get("shop_info") if isinstance(base.get("shop_info"), dict) else {}
        name = str(info.get("name") or row.get("title") or "").strip()
        product_id = str(row.get("product_id") or "").strip()
        commodity_id = str(row.get("promotion_id") or row.get("commodity_id") or "").strip()
        if not product_id or not name:
            continue
        matched_word = _title_banned_word(name, settings)
        if matched_word:
            rejected.append({
                "cardIndex": card_index,
                "productId": product_id,
                "commodityId": commodity_id,
                "title": name,
                "titleBannedWord": matched_word,
                "titleFilterExcluded": True,
                "titleFilterWasSelected": False,
                "thresholdReasons": [f"商品标题命中屏蔽词「{matched_word}」"],
                "eligible": False,
                "analysisStatus": "商品标题命中屏蔽词",
                "selected": False,
            })
            continue

        month_sales = _find_metric(
            [info, row], ("month_sale", "month_sales", "sales_count", "sale_count", "total_sales_count")
        )
        # “sales_volume”语义可能是销量而非金额，不映射为销售额，避免单位混淆造成错误筛选。
        sale_amount = _find_metric(row, ("sale_amount", "sales_amount", "month_gmv", "gmv", "total_sales_amount"))
        conversion = _find_metric(row, ("conversion_rate", "convert_rate", "conversion_ratio"))
        views = _find_metric(row, ("view_count", "views", "browse_count", "browse_num"))
        cos_info = promotion.get("cos_info") if isinstance(promotion.get("cos_info"), dict) else {}
        cos_ratio = _find_metric(
            [cos_info, promotion, row], ("cos_ratio_text", "commission_rate", "commission_ratio", "cos_ratio")
        )
        price_value = _number(info.get("price"))
        if price_value is None:
            price_info = base.get("marketing_info") if isinstance(base.get("marketing_info"), dict) else {}
            sale_price = price_info.get("price_desc", {})
            price_value = _find_metric(sale_price, ("origin", "price"))
        estimated_sales_amount = price_value * month_sales if price_value is not None and month_sales is not None else None
        shop_score = _find_metric(shop, ("experience_score", "experienceScore", "shop_score", "shopScore", "score"))
        metrics = {
            "salesAmount": sale_amount,
            "salesCount": month_sales,
            "conversionRate": conversion,
            "views": views,
            "commissionRate": cos_ratio,
            "experienceScore": shop_score,
        }
        metric_labels = {
            "salesAmount": "销售额",
            "salesCount": "销量",
            "conversionRate": "转化率",
            "views": "浏览量",
            "commissionRate": "佣金率",
            "experienceScore": "体验分",
        }
        known_rejection_reasons: list[str] = []
        pending_thresholds: list[str] = []
        commission_lower, commission_upper = _commission_rate_bounds(settings)
        for metric_name, threshold in thresholds.items():
            if metric_name in {"commissionRate", "commissionRateMax"}:
                continue
            if threshold is None or threshold == "":
                continue
            actual = metrics.get(metric_name)
            if actual is None:
                pending_thresholds.append(metric_name)
                try:
                    boundary = float(threshold)
                except (TypeError, ValueError):
                    continue
                known_rejection_reasons.append(
                    f"缺少{metric_labels.get(metric_name, metric_name)}实测数据，无法核验"
                    f"阈值 {boundary:g}"
                )
                continue
            try:
                boundary = float(threshold)
                if actual < boundary:
                    known_rejection_reasons.append(
                        f"{metric_labels.get(metric_name, metric_name)} {actual:g} 低于设定阈值 {boundary:g}"
                    )
            except (TypeError, ValueError):
                continue
        if commission_lower is not None or commission_upper is not None:
            if cos_ratio is None:
                pending_thresholds.append("commissionRate")
                known_rejection_reasons.append("缺少佣金率实测数据，无法核验设定区间")
            elif not math.isfinite(cos_ratio) or not 0 <= cos_ratio <= 100:
                known_rejection_reasons.append("佣金率数据超出 0–100%，无法核验设定区间")
            elif commission_lower is not None and cos_ratio < commission_lower:
                known_rejection_reasons.append(f"佣金率 {cos_ratio:g} 低于设定下限 {commission_lower:g}")
            elif commission_upper is not None and cos_ratio > commission_upper:
                known_rejection_reasons.append(f"佣金率 {cos_ratio:g} 超过设定上限 {commission_upper:g}")

        tags = json.dumps({
            "tag_info": base.get("tag_info", {}),
            "activity": row.get("custom_model", {}),
            "promotion": promotion,
        }, ensure_ascii=False).casefold()
        sources = [
            source_key for source_key in source_order
            if product_id in source_membership.get(source_key, set())
        ]
        pool_rank = next((index for index, pool in enumerate(pool_priority) if pool.casefold() in tags), None)
        if pool_rank is None:
            if not fallback and not sources:
                continue
            pool_rank = len(pool_priority)
        trend = _trend_analysis(info)
        rising = trend.get("status") == "rising"
        if settings.get("trendPreference") == "上升优先" and not rising:
            trend_rank = 1
        else:
            trend_rank = 0
        score = sale_amount if sale_amount is not None else (month_sales or 0)
        category = info.get("category") if isinstance(info.get("category"), dict) else {}
        image_info = info.get("main_img") if isinstance(info.get("main_img"), dict) else {}
        img_urls = image_info.get("url_list") if isinstance(image_info.get("url_list"), list) else []
        candidate = {
                "cardIndex": card_index,
                "productId": product_id,
                "commodityId": commodity_id,
                "title": name,
                "price": _find_metric(base.get("marketing_info", {}), ("origin", "price")),
                "monthSales": month_sales,
                "saleAmount": sale_amount,
                "estimatedSalesAmount": estimated_sales_amount,
                "conversionRate": conversion,
                "views": views,
                "commissionRate": cos_ratio,
                "shopScore": shop_score,
                "trendRising": rising,
                "trendKnown": trend.get("known") is True,
                "trendStatus": str(trend.get("status") or "unknown"),
                "trendSlope": trend.get("slope"),
                "trendPoints": int(trend.get("points") or 0),
                "dataMetrics": metrics,
                "metricSources": {
                    key: ("商品详情" if key in row.get("_detailMetricKeys", []) else "来源商品行")
                    for key, value in metrics.items() if value is not None
                },
                "detailMetricCapturedAt": str(row.get("_detailMetricCapturedAt") or ""),
                "detailMetricError": str(row.get("_detailMetricError") or ""),
                "pendingThresholds": pending_thresholds,
                "thresholdReasons": known_rejection_reasons,
                "eligible": not known_rejection_reasons,
                "analysisStatus": "候选列表数据已读取",
                "analysisReasons": [],
                "poolRank": pool_rank,
                "sources": sources,
                "selectionMode": str(settings.get("selectionMode") or "general"),
                "categoryMode": str(settings.get("categoryMode") or "auto"),
                "categoryIds": _promotion_row_category_ids(row),
                "coverUrl": str(img_urls[0]) if img_urls else "",
                "inCart": bool((base.get("extend_info") or {}).get("is_in_cart")) if isinstance(base.get("extend_info"), dict) else False,
                "selected": False,
            }
        if known_rejection_reasons:
            candidate["eligible"] = False
            rejected.append(candidate)
            continue
        ranked.append((_candidate_rank(candidate, settings), candidate))

    ranked.sort(key=lambda pair: pair[0])
    # 百应可能为同一商品返回多个推广计划；软件按 product_id 锁商品，
    # 所以候选也必须按同一身份去重，优先保留排序最高且通过筛选的推广记录。
    unique_ranked: list[tuple[tuple[int, int, int, float], Dict[str, Any]]] = []
    ranked_product_ids: set[str] = set()
    for rank, item in ranked:
        product_id = str(item.get("productId") or "").strip()
        if not product_id or product_id in ranked_product_ids:
            continue
        ranked_product_ids.add(product_id)
        unique_ranked.append((rank, item))
    unique_rejected: list[Dict[str, Any]] = []
    rejected_product_ids: set[str] = set()
    for item in rejected:
        product_id = str(item.get("productId") or "").strip()
        if not product_id or product_id in ranked_product_ids or product_id in rejected_product_ids:
            continue
        rejected_product_ids.add(product_id)
        unique_rejected.append(item)
    rejected = unique_rejected
    # candidateLimit 限制本批新选候选数。恢复时额外保留已持久化的商品 ID，
    # 避免平台排序变化后，原商品因排在前 N 名之外而无法按 ID 续跑。
    candidates = [item for _, item in unique_ranked[:limit]]
    preserved_ids = {str(value) for value in (preserve_product_ids or []) if str(value)}
    if preserved_ids:
        included_ids = {str(item.get("productId", "")) for item in candidates}
        for _, item in unique_ranked:
            product_id = str(item.get("productId", ""))
            if product_id in preserved_ids and product_id not in included_ids:
                candidates.append(item)
                included_ids.add(product_id)
    for candidate in candidates:
        candidate["selected"] = True
    return candidates, list(dict.fromkeys(messages)), rejected


def _source_candidate_filter_settings(settings: Dict[str, Any], source_key: str) -> Dict[str, Any]:
    """返回单一来源的本地候选筛选设置。

    各入口的平台筛选语义不同，但最终入池仍统一复核已配置的本地阈值、
    屏蔽词和商品 ID。热点没有佣金页面控件，因此以商品实测值核验。
    """
    scoped = copy.deepcopy(settings)
    if source_key == "hotSpot":
        scoped["_sourceFilterPolicy"] = "hotspot_category_and_shared_threshold_recheck"
    elif source_key == "selectionSquare":
        scoped["_sourceFilterPolicy"] = "selection_square_platform_filters_then_local_recheck"
    else:
        scoped["_sourceFilterPolicy"] = "alliance_rank_configured_filters"
    return scoped


def _candidate_rows_by_source_policy(
    rows_by_source: Dict[str, list[Dict[str, Any]]],
    source_membership: Dict[str, set[str]],
    settings: Dict[str, Any],
    limit: int,
    preserve_product_ids: Optional[Sequence[str]] = None,
) -> tuple[list[Dict[str, Any]], list[str], list[Dict[str, Any]]]:
    """按来源分别筛选，再按商品 ID 合并，避免来源规则互相污染。"""
    source_order = [*_enabled_priority_source_order(settings), "selectionSquare"]
    accepted_by_id: Dict[str, Dict[str, Any]] = {}
    rejected_by_id: Dict[str, Dict[str, Any]] = {}
    messages: list[str] = []

    for source_key in source_order:
        rows = rows_by_source.get(source_key, [])
        if not rows:
            continue
        scoped_settings = _source_candidate_filter_settings(settings, source_key)
        candidates, source_messages, rejected = _candidate_rows(
            {"data": {"summary_promotions": rows}},
            {**scoped_settings, "_sourceMembership": source_membership},
            max(1, min(2000, limit)),
            preserve_product_ids,
        )
        messages.extend(source_messages)
        source_ids = source_membership.get(source_key, set())

        def annotate(item: Dict[str, Any]) -> Dict[str, Any]:
            value = copy.deepcopy(item)
            product_id = str(value.get("productId") or "")
            value["sourceKey"] = source_key
            value["sourcePolicy"] = scoped_settings.get("_sourceFilterPolicy", "")
            value["sources"] = [key for key in source_order if product_id in source_membership.get(key, set())]
            if source_key not in value["sources"] and product_id in source_ids:
                value["sources"].append(source_key)
            return value

        for item in candidates:
            product_id = str(item.get("productId") or "")
            if not product_id:
                continue
            value = annotate(item)
            if product_id in accepted_by_id:
                merged = _merge_candidate_source_rows(accepted_by_id[product_id], value)
                merged["sources"] = list(dict.fromkeys([
                    *(accepted_by_id[product_id].get("sources") or []),
                    *(value.get("sources") or []),
                ]))
                merged["sourcePolicies"] = list(dict.fromkeys([
                    *(accepted_by_id[product_id].get("sourcePolicies") or []),
                    str(value.get("sourcePolicy") or ""),
                ]))
                accepted_by_id[product_id] = merged
            else:
                value["sourcePolicies"] = [str(value.get("sourcePolicy") or "")]
                accepted_by_id[product_id] = value
            # 同一商品只要有一个来源通过，就不能被另一个来源的阈值拒绝。
            rejected_by_id.pop(product_id, None)

        for item in rejected:
            product_id = str(item.get("productId") or "")
            if not product_id or product_id in accepted_by_id:
                continue
            value = annotate(item)
            if product_id in rejected_by_id:
                merged = _merge_candidate_source_rows(rejected_by_id[product_id], value)
                merged["sources"] = list(dict.fromkeys([
                    *(rejected_by_id[product_id].get("sources") or []),
                    *(value.get("sources") or []),
                ]))
                merged["sourcePolicies"] = list(dict.fromkeys([
                    *(rejected_by_id[product_id].get("sourcePolicies") or []),
                    str(value.get("sourcePolicy") or ""),
                ]))
                rejected_by_id[product_id] = merged
            else:
                value["sourcePolicies"] = [str(value.get("sourcePolicy") or "")]
                rejected_by_id[product_id] = value

    return (
        list(accepted_by_id.values()),
        list(dict.fromkeys(messages)),
        list(rejected_by_id.values()),
    )


def _enabled_threshold_names(settings: Dict[str, Any]) -> list[str]:
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    return list(dict.fromkeys(
        "commissionRate" if str(name) == "commissionRateMax" else str(name) for name, value in thresholds.items()
        if value is not None and value != "" and str(name)
    ))


def _promotion_row_metric_values(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """读取来源候选行已有的统一指标；详情补核验前只用于判断缺口。"""
    base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
    info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    promotion = base.get("promotion_info") if isinstance(base.get("promotion_info"), dict) else {}
    shop = base.get("shop_info") if isinstance(base.get("shop_info"), dict) else {}
    cos_info = promotion.get("cos_info") if isinstance(promotion.get("cos_info"), dict) else {}
    return {
        "salesAmount": _find_metric(row, ("sale_amount", "sales_amount", "month_gmv", "gmv", "total_sales_amount")),
        "salesCount": _find_metric([info, row], ("month_sale", "month_sales", "sales_count", "sale_count", "total_sales_count")),
        "conversionRate": _find_metric(row, ("conversion_rate", "convert_rate", "conversion_ratio")),
        "views": _find_metric(row, ("view_count", "views", "browse_count", "browse_num")),
        "commissionRate": _find_metric([cos_info, promotion, row], ("cos_ratio_text", "commission_rate", "commission_ratio", "cos_ratio")),
        "experienceScore": _find_metric(shop, ("experience_score", "experienceScore", "shop_score", "shopScore", "score")),
    }


def _promotion_row_category_ids(row: Dict[str, Any]) -> list[str]:
    base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
    info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    category = info.get("category") if isinstance(info.get("category"), dict) else {}
    return [_category_id_scalar(category.get(key)) for key in ("first", "second", "third")
            if _category_id_scalar(category.get(key))]


def _merge_detail_metrics_into_promotion_row(
    row: Dict[str, Any],
    payload: Optional[Dict[str, Any]],
    product_id: str,
) -> Dict[str, Any]:
    """将已核对商品详情的指标回填到来源行，供三来源统一解析器复用。"""
    if not isinstance(payload, dict):
        return row
    detail = _detail_metrics(payload, product_id)
    if detail.get("error") or detail.get("productId") != product_id:
        row["_detailMetricError"] = str(detail.get("error") or "详情商品 ID 未能核对")
        return row
    metrics = detail.get("metrics") if isinstance(detail.get("metrics"), dict) else {}
    base = row.setdefault("base_model", {})
    if not isinstance(base, dict):
        base = {}
        row["base_model"] = base
    info = base.setdefault("product_info", {})
    if not isinstance(info, dict):
        info = {}
        base["product_info"] = info
    promotion = base.setdefault("promotion_info", {})
    if not isinstance(promotion, dict):
        promotion = {}
        base["promotion_info"] = promotion
    cos_info = promotion.setdefault("cos_info", {})
    if not isinstance(cos_info, dict):
        cos_info = {}
        promotion["cos_info"] = cos_info
    shop = base.setdefault("shop_info", {})
    if not isinstance(shop, dict):
        shop = {}
        base["shop_info"] = shop
    field_map: Dict[str, tuple[Dict[str, Any], str]] = {
        "salesAmount": (row, "sale_amount"),
        "salesCount": (info, "month_sale"),
        "conversionRate": (row, "conversion_rate"),
        "views": (row, "view_count"),
        "commissionRate": (cos_info, "cos_ratio_text"),
        "experienceScore": (shop, "experience_score"),
    }
    detail_keys: list[str] = []
    for metric_name, value in metrics.items():
        if value is None or metric_name not in field_map:
            continue
        target, key = field_map[metric_name]
        target[key] = value
        detail_keys.append(metric_name)
    row["_detailMetricKeys"] = detail_keys
    row["_detailMetricCapturedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return row


def _commission_rate_bounds(settings: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    platform_bounds = settings.get("_platformCommissionRateBounds")
    raw_bounds = platform_bounds if isinstance(platform_bounds, (list, tuple)) and len(platform_bounds) == 2 else (
        thresholds.get("commissionRate"), thresholds.get("commissionRateMax")
    )
    bounds: list[Optional[float]] = []
    for raw in raw_bounds:
        if raw is None or raw == "":
            bounds.append(None)
            continue
        try:
            bound = float(raw)
        except (TypeError, ValueError) as error:
            raise RuntimeError("佣金率须为 0–100 之间的百分数") from error
        if not math.isfinite(bound) or not 0 <= bound <= 100:
            raise RuntimeError("佣金率须为 0–100 之间的百分数")
        bounds.append(bound)
    lower, upper = bounds
    if lower is not None and upper is not None and lower > upper:
        raise RuntimeError("最低佣金率不能超过最高佣金率")
    return lower, upper


def _apply_square_commission_range(page: Page, settings: Dict[str, Any]) -> None:
    """选品广场自定义佣金区间；以结果区标签确认应用，绝不重复提交未知点击。"""
    lower, upper = _commission_rate_bounds(settings)
    if lower is None and upper is None:
        return
    expected_min, expected_max = f"{lower if lower is not None else 0:g}", f"{upper if upper is not None else 100:g}"
    expected_value = f"{expected_min}%-{expected_max}%"
    def applied(timeout_ms: int = 350) -> bool:
        return _wait_filter_summary_text(page, "佣金区间", expected_value, timeout_ms=timeout_ms)

    # 截图显示旧标签在弹窗打开时仍可见。只有弹窗已关闭时，才允许把旧标签当作已应用状态。
    try:
        _wait_filter_dropdown_closed(page, timeout_ms=350)
        if applied():
            return
    except PlaywrightTimeoutError:
        pass

    apply_error: Optional[BaseException] = None
    apply_stage = "打开佣金筛选"
    confirm_attempted = False
    collector_state = getattr(page, "_touguang_filter_retry_collector", None)
    collector = collector_state[0] if isinstance(collector_state, tuple) and len(collector_state) == 2 else None
    confirm_request_mark: Optional[int] = None
    confirm = None
    minimum = None
    maximum = None
    try:
        _control, menu = _open_filter_dropdown(page, "佣金区间")
        apply_stage = "定位佣金输入框"
        minimum = menu.locator('input[placeholder*="最低"]')
        maximum = menu.locator('input[placeholder*="最高"]')
        if minimum.count() != 1 or maximum.count() != 1:
            raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：自定义佣金输入框未唯一定位")
        apply_stage = "填写佣金区间"
        minimum.fill(expected_min)
        maximum.fill(expected_max)
        apply_stage = "回读佣金输入值"
        actual_min, actual_max = minimum.input_value(), maximum.input_value()
        if isinstance(actual_min, str) and isinstance(actual_max, str) and (
            actual_min.strip() != expected_min or actual_max.strip() != expected_max
        ):
            raise CommissionFilterUnconfirmed(
                f"COMMISSION_FILTER_UNCONFIRMED：输入值与目标 {expected_value} 不一致；未点击确认"
            )
        apply_stage = "定位佣金确认按钮"
        confirm = menu.get_by_role("button", name=re.compile(r"^确\s*定$"))
        if confirm.count() != 1:
            confirm = menu.get_by_text("确定", exact=True)
            if not confirm.count():
                confirm = menu.get_by_text("确 定", exact=True)
        if confirm.count() != 1:
            raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：佣金确认控件未唯一定位")
        apply_stage = "点击佣金确认按钮"
        # 末尾输入框先失焦，让 Aurora 完成受控输入值的提交。
        maximum.press("Tab")
        if minimum.input_value().strip() != expected_min or maximum.input_value().strip() != expected_max:
            raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：失焦后佣金输入值改变；未点击确认")
        if collector is not None:
            confirm_request_mark = collector.mark()
        confirm_attempted = True
        _click_filter_confirm_stably(
            page, confirm, confirmation_check=lambda: applied(timeout_ms=2_500)
        )
    except Exception as error:
        apply_error = error
    if apply_error is not None and _commission_error_requires_pause(apply_error):
        raise apply_error
    if getattr(page, "is_closed", lambda: False)() is True:
        raise RuntimeError("百应页面已关闭，停止当前账号采集")
    failure_detail = (
        f"阶段：{apply_stage}；底层错误：{type(apply_error).__name__}："
        f"{_safe_diagnostic_text(apply_error, 180)}"
        if apply_error is not None else ""
    )
    if apply_error is not None and not confirm_attempted:
        # 尚未点击时保留弹窗及输入值，供失败截图定位；不以主动关闭掩盖原始阶段。
        raise CommissionFilterUnconfirmed(
            f"COMMISSION_FILTER_UNCONFIRMED：{failure_detail}；未执行确认"
        ) from apply_error
    try:
        _wait_filter_dropdown_closed(page, timeout_ms=3_000)
    except PlaywrightTimeoutError as error:
        # 只有已回读到弹窗仍开、输入值未变、没有目标标签及新列表请求，才认为
        # 首次点击没有提交。此时对已定位的确认控件派发一次 DOM 点击；
        # 避免鼠标坐标落在弹窗浮层外，任何请求或不明状态都停下。
        if (confirm_attempted and collector is not None and confirm_request_mark is not None and
                confirm is not None and minimum is not None and maximum is not None):
            _wait_pending_filter_request_before_retry(page)
            if collector.rate_limit_error_since(confirm_request_mark):
                raise RuntimeError("material_list 返回 11001，停止佣金确认并保留限流冷却")
            if (collector.request_count_since(confirm_request_mark) == 0 and
                    not collector.pending_since(confirm_request_mark) and
                    minimum.input_value().strip() == expected_min and
                    maximum.input_value().strip() == expected_max and
                    not applied(timeout_ms=350) and confirm.is_visible(timeout=500) and
                    confirm.is_enabled(timeout=500)):
                apply_stage = "无提交迹象后恢复确认按钮"
                confirm.evaluate("node => (node.closest('button, [role=button]') || node).click()")
                try:
                    _wait_filter_dropdown_closed(page, timeout_ms=3_000)
                except PlaywrightTimeoutError:
                    pass
                else:
                    if applied(timeout_ms=3_000):
                        return
        raise CommissionFilterUnconfirmed(
            f"COMMISSION_FILTER_UNCONFIRMED：佣金弹窗关闭状态无法核实；阶段：{apply_stage}；{failure_detail}"
        ) from (apply_error or error)
    if applied(timeout_ms=3_000):
        return
    if apply_error is not None:
        raise CommissionFilterUnconfirmed(
            f"COMMISSION_FILTER_UNCONFIRMED：确认后结果区未出现目标佣金范围；{failure_detail}"
        ) from apply_error
    raise CommissionFilterUnconfirmed("COMMISSION_FILTER_UNCONFIRMED：确认按钮返回后结果区仍无目标佣金范围")


def _effective_selection_filters(settings: Dict[str, Any]) -> Dict[str, Any]:
    """数字佣金区间由专门的页面筛选处理，旧手选档位不得覆盖当前设置。"""
    filters = copy.deepcopy(settings.get("filters") if isinstance(settings.get("filters"), dict) else {})
    for group in ("delivery", "product"):
        values = filters.get(group)
        if isinstance(values, dict):
            values.pop("佣金区间", None)
    return filters


def _selection_filter_signature(settings: Dict[str, Any]) -> str:
    filters = _effective_selection_filters(settings)
    desired_settings = {
        "categorySelection": settings.get("categorySelection", {}),
        "filters": filters,
        "commissionRateBounds": _commission_rate_bounds(settings),
        "searchKeyword": str(settings.get("searchKeyword") or "").strip(),
    }
    return json.dumps(desired_settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SearchboxUnavailableError(RuntimeError):
    """选品搜索控件有限恢复后仍不可用；由 UI 按当前账号软失败处理。"""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification


def _searchbox_page_url(page: Page) -> str:
    try:
        parsed = urlparse(page.url)
        return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"
    except Exception:
        return ""


def _write_searchbox_diagnostic(
    page: Page,
    selector: str,
    selector_hit_count: Optional[int],
    visible_hit_count: Optional[int],
    attempt: int,
    reason: str,
    classification: str = "SEARCHBOX_ZERO_EXHAUSTED",
) -> Dict[str, str]:
    """保存搜索控件状态和视口截图；URL 查询参数与整页文本不会写入诊断。"""
    diagnostic_id = f"searchbox-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    diagnostic_dir = root / "diagnostics" / "selection"
    json_path = diagnostic_dir / f"{diagnostic_id}.json"
    screenshot_path = diagnostic_dir / f"{diagnostic_id}.png"
    try:
        title = str(page.title() or "")[:160]
    except Exception as error:
        title = f"读取失败：{type(error).__name__}"
    try:
        ready_state = str(page.evaluate("() => document.readyState") or "unknown")
    except Exception as error:
        ready_state = f"读取失败：{type(error).__name__}"
    payload: Dict[str, Any] = {
        "diagnosticId": diagnostic_id,
        "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "classification": classification,
        "attempt": attempt,
        "reason": _safe_diagnostic_text(reason, 600),
        "url": _searchbox_page_url(page),
        "title": title,
        "readyState": ready_state,
        "selector": selector,
        "selectorHitCount": selector_hit_count,
        "visibleHitCount": visible_hit_count,
        "privacy": "未保存 Cookie、请求头、请求体、URL 查询参数或整页文本。",
    }
    try:
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
            payload["screenshotPath"] = str(screenshot_path)
        except Exception as error:
            payload["screenshotError"] = type(error).__name__
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        payload["writeError"] = type(error).__name__
    return {
        "diagnosticId": diagnostic_id,
        "url": str(payload.get("url") or ""),
        "title": str(payload.get("title") or ""),
        "readyState": str(payload.get("readyState") or "unknown"),
        "selectorHitCount": str(selector_hit_count if selector_hit_count is not None else "读取失败"),
        "visibleHitCount": str(visible_hit_count if visible_hit_count is not None else "读取失败"),
        "screenshotPath": str(payload.get("screenshotPath") or "未能保存"),
        "jsonPath": str(json_path) if json_path.exists() else "未能保存",
    }


def _raise_if_search_recovery_hit_rate_limit(
    page: Page, collector: Optional[Any], collector_mark: Optional[int]
) -> None:
    if collector is not None and collector_mark is not None:
        rate_limit_error = collector.rate_limit_error_since(collector_mark)
        if rate_limit_error:
            _mark_page_rate_limited(id(page))
            detail = rate_limit_error[2]
            raise RuntimeError(
                f"BAIYING_RATE_LIMIT：百应 material_list 返回业务错误 11001；搜索框恢复已中止。"
                f"百应响应诊断：{str(detail)[:320]}"
            )
    remaining = _page_rate_limit_remaining(id(page))
    if remaining > 0:
        raise RuntimeError(
            f"BAIYING_RATE_LIMIT：百应仍在自动冷却期，还需约 {remaining} 秒；搜索框恢复已中止。"
        )


def _visible_unique_searchbox(page: Page) -> tuple[Optional[Any], int, int]:
    inputs = page.locator(SEARCH_KEYWORD_INPUT_SELECTOR)
    count = inputs.count()
    visible_count = 0
    visible_input = None
    candidates = [inputs.first] if count == 1 else [inputs.nth(index) for index in range(count)]
    for candidate in candidates:
        try:
            is_visible = getattr(candidate, "is_visible", None)
            if not callable(is_visible) or is_visible(timeout=500):
                visible_count += 1
                visible_input = candidate
        except Exception:
            continue
    if count == 1 and visible_count == 1:
        return visible_input, count, visible_count
    return None, count, visible_count


def _search_keyword_input(
    page: Page,
    *,
    recovery_retries: int = SEARCHBOX_RECOVERY_RETRIES,
    ready_timeout_ms: int = SEARCHBOX_READY_TIMEOUT_MS,
    collector: Optional[Any] = None,
    collector_mark: Optional[int] = None,
) -> Any:
    diagnostics: list[Dict[str, str]] = []
    last_reason = "搜索控件未唯一可见"
    try:
        retries = max(0, int(recovery_retries))
    except (TypeError, ValueError):
        retries = SEARCHBOX_RECOVERY_RETRIES
    for retry in range(retries + 1):
        _raise_if_search_recovery_hit_rate_limit(page, collector, collector_mark)
        try:
            field, count, visible_count = _visible_unique_searchbox(page)
        except Exception as error:
            field, count, visible_count = None, None, None
            last_reason = f"读取搜索框选择器失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}"
        else:
            last_reason = f"搜索框选择器命中 {count} 个，其中可见 {visible_count} 个"
        if field is not None:
            return field
        diagnostics.append(_write_searchbox_diagnostic(
            page,
            SEARCH_KEYWORD_INPUT_SELECTOR,
            count,
            visible_count,
            retry,
            last_reason,
        ))
        if retry >= retries:
            break
        _raise_if_search_recovery_hit_rate_limit(page, collector, collector_mark)
        try:
            page.goto(BAIYING_SELECTION_LIBRARY_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.evaluate("""() => {
                  window.__touguangManagedSelectionPage = true;
                  window.__touguangBackgroundSelectionPage = true;
                  window.name = '__touguangBackgroundSelectionPage';
                }""")
            except Exception:
                pass
            deadline = time.monotonic() + max(0, int(ready_timeout_ms)) / 1000
            while True:
                _raise_if_search_recovery_hit_rate_limit(page, collector, collector_mark)
                field, count, visible_count = _visible_unique_searchbox(page)
                if field is not None:
                    return field
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(min(250, max(1, int((deadline - time.monotonic()) * 1000))))
        except Exception as error:
            # Rate limit is raised through this block too and must never become a searchbox soft-fail.
            if "BAIYING_RATE_LIMIT" in str(error) or "11001" in str(error):
                raise
            last_reason = f"回到选品页或等待搜索框失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}"
            _raise_if_search_recovery_hit_rate_limit(page, collector, collector_mark)
    final = diagnostics[-1] if diagnostics else {}
    details = "; ".join(
        f"URL={item.get('url', '')}, title={item.get('title', '')}, readyState={item.get('readyState', '')}, "
        f"selectorHitCount={item.get('selectorHitCount', '')}, visibleHitCount={item.get('visibleHitCount', '')}, "
        f"screenshotPath={item.get('screenshotPath', '')}, jsonPath={item.get('jsonPath', '')}"
        for item in diagnostics
    )
    raise SearchboxUnavailableError(
        "SEARCHBOX_ZERO_EXHAUSTED",
        f"SEARCHBOX_ZERO_EXHAUSTED：百应选品搜索框经 {retries} 次回页恢复仍未唯一可见；"
        f"最后状态：{last_reason}；诊断：{details or '未能保存诊断文件'}",
    )


def _configured_search_keyword(settings: Dict[str, Any]) -> str:
    return str(settings.get("searchKeyword") or "").strip()


def _read_search_keyword(page: Page) -> str:
    return str(_search_keyword_input(page).input_value(timeout=3_000) or "").strip()


def _apply_search_keyword(
    page: Page,
    settings: Dict[str, Any],
    collector: Optional[Any] = None,
    collector_mark: Optional[int] = None,
) -> bool:
    """使用百应控件的清除动作同步搜索词，避免只改输入框而保留内部 search_id。"""
    desired = _configured_search_keyword(settings)
    field = _search_keyword_input(page, collector=collector, collector_mark=collector_mark)
    current = str(field.input_value(timeout=3_000) or "")
    changed = current != desired
    if changed:
        if desired:
            raise RuntimeError(
                "当前百应搜索词与设置不一致；自动流程不能安全切换非空关键词，已停止查询。"
            )
        clear_controls = page.locator(SEARCH_KEYWORD_CLEAR_SELECTOR)
        clear_count = clear_controls.count()
        if clear_count != 1:
            _raise_if_search_recovery_hit_rate_limit(page, collector, collector_mark)
            diagnostic = _write_searchbox_diagnostic(
                page, SEARCH_KEYWORD_CLEAR_SELECTOR, clear_count, None, 0,
                "搜索框有旧关键词，但内置清除按钮未唯一命中",
                classification="SEARCHBOX_CONTROL_EXHAUSTED",
            )
            raise SearchboxUnavailableError(
                "SEARCHBOX_CONTROL_EXHAUSTED",
                f"SEARCHBOX_CONTROL_EXHAUSTED：百应内置清除按钮应唯一可定位，当前找到 {clear_count} 个；"
                f"URL={diagnostic['url']}, title={diagnostic['title']}, readyState={diagnostic['readyState']}, "
                f"selectorHitCount={diagnostic['selectorHitCount']}, screenshotPath={diagnostic['screenshotPath']}, "
                f"jsonPath={diagnostic['jsonPath']}。",
            )
        clear_control = clear_controls.first
        clear_errors: list[str] = []
        last_clear_error: Optional[BaseException] = None

        # 先保留平台内置清除动作（它会同步页面维护的 search_id），但缩短
        # Playwright 动作等待并绕过动画期间的命中检测。清空是否成功只认 input_value。
        try:
            clear_control.click(force=True, timeout=1_500, no_wait_after=True)
        except Exception as error:
            last_clear_error = error
            clear_errors.append(f"初次点击：{type(error).__name__}: {str(error)[:400]}")
        if str(field.input_value(timeout=3_000) or "") != desired:
            # close-circle 已唯一定位；若真实鼠标动作卡在派发阶段，调用该控件的
            # click handler 作为回退。JS 仅在节点仍可见时触发，随后仍必须回读输入框。
            try:
                clear_control.evaluate("""element => {
                  const rect = element.getBoundingClientRect();
                  const style = getComputedStyle(element);
                  const visible = rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
                  if (!visible) return false;
                  element.click();
                  return true;
                }""", timeout=2_000)
            except Exception as error:
                last_clear_error = error
                clear_errors.append(f"控件回退：{type(error).__name__}: {str(error)[:400]}")
        if str(field.input_value(timeout=3_000) or "") != desired:
            # 最后使用键盘输入事件清空，确保受控输入框收到 input/change 事件。
            try:
                field.press("Control+A", timeout=2_000)
                field.press("Backspace", timeout=2_000)
            except Exception as error:
                last_clear_error = error
                clear_errors.append(f"键盘回退：{type(error).__name__}: {str(error)[:400]}")
        if str(field.input_value(timeout=3_000) or "") != desired:
            reason = "；操作明细：" + "；".join(clear_errors) if clear_errors else ""
            raise RuntimeError(
                "百应搜索框旧关键词未能清空；点击、可见控件回退和键盘清空后回读仍不为空" + reason
            ) from last_clear_error
    actual = str(field.input_value(timeout=3_000) or "")
    if actual != desired:
        raise RuntimeError("百应搜索框未能应用当前设置的关键词，已停止查询。")
    # 搜索框由页面框架维护；等待一次渲染周期，避免旧状态回写后仍点击搜索。
    page.wait_for_timeout(150)
    stable_value = str(field.input_value(timeout=3_000) or "")
    if stable_value != desired:
        raise RuntimeError("百应搜索框在页面更新后与当前设置不一致，已停止查询。")
    return changed


def _page_applied_filter_signature(page: Page) -> str:
    try:
        return str(page.evaluate("() => window.__touguangAppliedFilterSettings || ''") or "")
    except Exception:
        return ""


def _can_reuse_applied_non_category_filters(applied_signature: str, settings: Dict[str, Any]) -> bool:
    """只有上一查询与当前查询的非类目筛选完全相同时，才允许只切换类目。"""
    if not applied_signature:
        return False
    try:
        applied = json.loads(applied_signature)
    except (TypeError, ValueError):
        return False
    if not isinstance(applied, dict):
        return False
    applied_filters = applied.get("filters") if isinstance(applied.get("filters"), dict) else {}
    current_filters = _effective_selection_filters(settings)
    applied_keyword = str(applied.get("searchKeyword") or "").strip()
    current_keyword = _configured_search_keyword(settings)
    return (
        applied_filters == current_filters
        and applied.get("commissionRateBounds") == list(_commission_rate_bounds(settings))
        and applied_keyword == current_keyword
    )


def _candidate_result(
    payload: Any,
    settings: Dict[str, Any],
    preserve_product_ids: Optional[Sequence[str]] = None,
    include_source_ids: bool = False,
) -> Dict[str, Any]:
    limit = settings.get("_candidatePoolLimit", settings.get("candidateLimit", 5))
    max_limit = 2_000 if settings.get("_candidatePoolCollectOnly") is True or settings.get("_unifiedSourceCollection") is True else 30 if "_candidatePoolLimit" in settings else 10
    try:
        limit = min(max_limit, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    candidates, warnings, rejected = _candidate_rows(payload, settings, limit, preserve_product_ids)
    result = {
        "ok": True,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "rejectedCandidates": rejected,
        "warnings": warnings,
    }
    if include_source_ids:
        rows = _promotion_items(payload)
        result["_sourceProductIds"] = [
            str(row.get("product_id") or "").strip() for row in rows
            if str(row.get("product_id") or "").strip()
        ]
        result["_sourcePromotionIds"] = [
            str(row.get("promotion_id") or row.get("commodity_id") or "").strip() for row in rows
            if str(row.get("promotion_id") or row.get("commodity_id") or "").strip()
        ]
    return result


def _refreshed_candidate_by_id(
    refreshed: Dict[str, Any], product_id: str
) -> tuple[str, Optional[Dict[str, Any]], str]:
    """先按原始商品 ID 查刷新结果，再区分初筛淘汰、ID 字段差异和真正缺失。"""
    eligible = [
        row for row in refreshed.get("candidates", [])
        if isinstance(row, dict) and str(row.get("productId") or "").strip() == product_id
    ]
    rejected = [
        row for row in refreshed.get("rejectedCandidates", [])
        if isinstance(row, dict) and str(row.get("productId") or "").strip() == product_id
    ]
    if len(eligible) + len(rejected) > 1:
        return "ambiguous", None, f"刷新后的百应类目结果中商品 ID {product_id} 命中多条候选记录"
    if eligible:
        return "eligible", eligible[0], ""
    if rejected:
        reasons = rejected[0].get("thresholdReasons", [])
        reason_text = "；".join(str(reason) for reason in reasons if str(reason))
        return "filtered", rejected[0], reason_text or "刷新后的商品数据未通过当前筛选条件"

    source_product_ids = [str(value).strip() for value in refreshed.get("_sourceProductIds", []) if str(value).strip()]
    if product_id in source_product_ids:
        return "raw_only", None, "原始 material_list 响应含该商品 ID，但候选解析未生成可用商品记录（可能是标题缺失、屏蔽词或字段结构变化）"
    source_promotion_ids = [str(value).strip() for value in refreshed.get("_sourcePromotionIds", []) if str(value).strip()]
    if product_id in source_promotion_ids:
        return "promotion_id_only", None, "该数字只出现在推广/商品关联 ID 字段，不在原始 product_id 字段；为避免把不同 ID 混用，未按商品 ID 继续"
    return "absent", None, "刷新后的原始 material_list 响应不含该商品 ID；商品可能已离开当前类目结果或列表发生变化"


def _rendered_material_payload(page: Page) -> Optional[Dict[str, Any]]:
    """从百应当前实际渲染的商品卡片 React props 读取商品行，避免消费未展示的中间响应。"""
    try:
        rows = page.evaluate("""() => Array.from(document.querySelectorAll('[class*="__card___"]'))
          .map((element) => {
            const propsKey = Object.keys(element).find((key) => key.startsWith('__reactProps$'));
            const item = propsKey ? element[propsKey]?.children?.props?.item : null;
            return item && item.product_id ? item : null;
          }).filter(Boolean)""")
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    return {"data": {"summary_promotions": rows}}


def _save_priority_source_dom_evidence(
    source_key: str,
    page: Page,
    snapshot: Dict[str, Any],
    readiness_reached: bool,
) -> Dict[str, Any]:
    """候选未识别时保存页面结构证据；类目缺失改用字段级诊断。"""
    diagnostic_id = f"priority-source-{source_key}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    evidence_dir = root / "diagnostics" / "selection" / "priority-sources"
    json_path = evidence_dir / f"{diagnostic_id}.json"
    html_path = evidence_dir / f"{diagnostic_id}.html"
    body_html = str(snapshot.get("bodyHtml") or "")
    scroll_html = str(snapshot.get("scrollContainerOuterHtml") or "")
    html_limit = 1_500_000
    html_truncated = len(body_html) > html_limit
    body_html = body_html[:html_limit]
    scroll_html_truncated = len(scroll_html) > html_limit
    scroll_html = scroll_html[:html_limit]
    scroll_html_path = evidence_dir / f"{diagnostic_id}-scroll-container.html"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if body_html:
            html_path.write_text(body_html, encoding="utf-8")
        if scroll_html:
            scroll_html_path.write_text(scroll_html, encoding="utf-8")
        metadata = {
            "diagnosticId": diagnostic_id,
            "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "sourceKey": source_key,
            "pageUrl": str(snapshot.get("pageUrl") or "")[:500],
            "pageTitle": str(snapshot.get("pageTitle") or "")[:160],
            "pagePath": str(snapshot.get("pagePath") or "")[:240],
            "documentReadyState": str(snapshot.get("readyState") or "unknown"),
            "readinessTimeoutMs": BAIYING_PRIORITY_READY_TIMEOUT_MS,
            "readinessWaitMs": int(snapshot.get("readinessWaitMs", 0) or 0),
            "readinessReached": readiness_reached,
            "candidateSelector": str(snapshot.get("candidateSelector") or BAIYING_PRIORITY_ROOT_SELECTOR),
            "listContainerSelector": str(snapshot.get("listContainerSelector") or BAIYING_PRIORITY_LIST_CONTAINER_SELECTOR),
            "activeLoadingSelector": BAIYING_PRIORITY_ACTIVE_LOADING_SELECTOR,
            "scrollContainerMatchCount": int(snapshot.get("listContainerMatchCount", 0) or 0),
            "scrollContainerVisibleCount": int(snapshot.get("listContainerVisibleCount", 0) or 0),
            "scrollContainerOuterHTMLPath": str(scroll_html_path) if scroll_html else "未找到滚动区容器",
            "scrollContainerOuterHTMLLength": len(scroll_html),
            "scrollContainerOuterHTMLTruncated": scroll_html_truncated,
            "candidateSelectorMatchCount": int(snapshot.get("rootMatchCount", 0) or 0),
            "visibleCandidateNodeCount": int(snapshot.get("visibleRowCount", 0) or 0),
            "candidateNodeChildCounts": snapshot.get("rootChildCounts", []),
            "listContainerMatchCount": int(snapshot.get("listContainerMatchCount", 0) or 0),
            "listContainerVisibleCount": int(snapshot.get("listContainerVisibleCount", 0) or 0),
            "navigationContainerMatchCount": int(snapshot.get("navigationContainerMatchCount", 0) or 0),
            "listContainersWithChildren": int(snapshot.get("listContainersWithChildren", 0) or 0),
            "listContainerChildCounts": snapshot.get("listContainerChildCounts", []),
            "listContainerSamples": snapshot.get("listContainerSamples", []),
            "contentRootSelector": str(snapshot.get("contentRootSelector") or BAIYING_PRIORITY_CONTENT_ROOT_SELECTOR),
            "contentRootMatchCount": int(snapshot.get("contentRootMatchCount", 0) or 0),
            "contentRootVisibleCount": int(snapshot.get("contentRootVisibleCount", 0) or 0),
            "contentRootSamples": snapshot.get("contentRootSamples", []),
            "loadingIndicatorVisibleCount": int(snapshot.get("loadingIndicatorVisibleCount", 0) or 0),
            "loadingIndicatorSamples": snapshot.get("loadingIndicatorSamples", []),
            "loadingTextDetected": bool(snapshot.get("loadingTextDetected")),
            "recognizedProductIdCount": int(snapshot.get("recognizedProductIdCount", 0) or 0),
            "completeCategoryPathProductCount": int(snapshot.get("completeCategoryPathProductCount", 0) or 0),
            "missingCategoryPathProductCount": int(snapshot.get("missingCategoryPathProductCount", 0) or 0),
            "categoryFieldSamples": snapshot.get("categoryFieldSamples", []),
            "bodyTextLength": int(snapshot.get("bodyTextLength", 0) or 0),
            "bodyHtmlPath": str(html_path) if body_html else "未取得 body.outerHTML",
            "bodyHtmlTruncated": html_truncated,
            "privacy": "DOM 已移除脚本、内嵌样式、框架、表单输入值及疑似凭据属性；URL 查询参数中的 token、secret、auth、session、password、signature、sign 等键已脱敏；未保存 Cookie 或请求头。",
        }
        json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "diagnosticId": diagnostic_id,
            "jsonPath": str(json_path),
            "htmlPath": str(html_path) if body_html else "",
            "scrollContainerHtmlPath": str(scroll_html_path) if scroll_html else "",
        }
    except Exception as error:
        return {
            "diagnosticId": diagnostic_id,
            "jsonPath": "未能保存：" + type(error).__name__,
            "htmlPath": str(html_path) if body_html else "",
            "scrollContainerHtmlPath": str(scroll_html_path) if scroll_html else "",
        }


def _source_category_tabs(
    page: Page, source_key: str, category_tree: Sequence[Any],
    rank_root_catalog: Optional[Dict[str, set[str]]] = None,
) -> list[Dict[str, Any]]:
    """读取来源分类控件；热点名称仅用于唯一定位，须再核对请求 ID。"""
    selector = (
        'div[class*="subCategoryGroup"] > div[class*="box"]'
        if source_key == "allianceRank"
        else 'div[class*="cateFilterMain"] > div[class*="box"]'
    )
    raw = page.evaluate("""({ selector }) => {
      const nodes = Array.from(document.querySelectorAll(selector));
      const validId = (value) => {
        const text = String(value ?? '').trim();
        return /^\\d+(?:__RC_CASCADER_SPLIT__\\d+)*$/.test(text) &&
          text.split('__RC_CASCADER_SPLIT__').every(part => Number(part) > 0) ? text : '';
      };
      const reactId = (element, name) => {
        const ids = new Set();
        const seen = new WeakSet();
        let count = 0;
        const walk = (node, depth) => {
          if (!node || typeof node !== 'object' || seen.has(node) || depth > 5 || ++count > 250) return;
          seen.add(node);
          if (Array.isArray(node)) { node.slice(0, 20).forEach(child => walk(child, depth + 1)); return; }
          const label = String(node.name ?? node.label ?? node.title ?? node.text ?? '').trim();
          for (const key of ['category_id', 'categoryId', 'cate_id', 'cateId', 'pathKey']) {
            const id = validId(node[key]);
            if (id && (!label || label === name)) ids.add(id);
          }
          if (label === name) {
            const id = validId(node.id ?? node.value);
            if (id) ids.add(id);
          }
          for (const [key, child] of Object.entries(node)) {
            if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
            walk(child, depth + 1);
          }
        };
        for (let current = element, depth = 0; current && depth < 3; current = current.parentElement, depth += 1) {
          for (const key of Object.keys(current)) if (key.startsWith('__reactProps$')) walk(current[key], 0);
        }
        return ids.size === 1 ? [...ids][0] : '';
      };
      return nodes.slice(0, 60).map((node, index) => {
        const name = String(node.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 50);
        const ownIds = [node.getAttribute('data-path-key'), node.getAttribute('data-category-id'),
          node.getAttribute('data-cate-id'), node.getAttribute('data-id')].map(validId).filter(Boolean);
        const id = new Set(ownIds).size === 1 ? ownIds[0] : ownIds.length ? '' : reactId(node, name);
        const idSource = id ? (ownIds.length ? 'attribute' : 'react') : '';
        const className = String(node.className || '');
        return { index, name, categoryId: id || '', idSource, selected:
          node.getAttribute('aria-selected') === 'true' || node.getAttribute('aria-checked') === 'true' ||
          node.getAttribute('data-active') === 'true' ||
          /(?:active|selected|checked|current)/i.test(className),
          ambiguousId: ownIds.length > 1 && new Set(ownIds).size !== 1 };
      }).filter(item => item.name);
    }""", {"selector": selector})
    tabs: list[Dict[str, Any]] = []
    hot_root_names: dict[str, set[str]] = {}
    if source_key == "hotSpot":
        for root in category_tree:
            if not isinstance(root, dict):
                continue
            name = str(root.get("name") or "").strip()
            category_id = _category_id_scalar(root.get("id"))
            if name and category_id:
                hot_root_names.setdefault(name, set()).add(category_id)
    visible_name_counts: dict[str, int] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            visible_name_counts[name] = visible_name_counts.get(name, 0) + 1
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        category_id = _category_id_scalar(item.get("categoryId"))
        id_source = str(item.get("idSource") or "")
        if source_key == "allianceRank" and not category_id and isinstance(rank_root_catalog, dict):
            verified_ids = rank_root_catalog.get(str(item.get("name") or ""), set())
            if len(verified_ids) == 1:
                category_id = next(iter(verified_ids))
                id_source = "catalog"
        if source_key == "hotSpot" and not category_id:
            name = str(item.get("name") or "").strip()
            matching_ids = hot_root_names.get(name, set())
            if visible_name_counts.get(name) == 1 and len(matching_ids) == 1:
                # 名称只用于定位按钮；入池前仍须从点击后的真实请求核实 ID。
                category_id = next(iter(matching_ids))
                id_source = "tree_name_pending_request"
        path, level = _resolve_single_category_id(category_id, category_tree)
        tabs.append({
            "index": int(item.get("index", 0)), "name": str(item.get("name") or "")[:50],
            "categoryId": category_id, "selected": item.get("selected") is True,
            "categoryIds": path, "level": level,
            "idSource": id_source,
            "status": ("request_pending" if id_source == "tree_name_pending_request" else "mapped") if path else "unresolved",
        })
    return tabs


def _rank_option_selected(option: Any) -> bool:
    return bool(option.evaluate("""node => {
      for (let item = node, depth = 0; item && depth < 4; item = item.parentElement, depth++) {
        if (item.getAttribute('aria-selected') === 'true' || item.getAttribute('aria-checked') === 'true') return true;
        if (/(?:active|selected|checked|current)/i.test(String(item.className || ''))) return true;
      }
      return false;
    }"""))


_RANK_PRICE_BOUNDS: Dict[str, tuple[Optional[float], Optional[float]]] = {
    "不限": (None, None), "1-10": (1, 10), "10-50": (10, 50),
    "50-100": (50, 100), "100-500": (100, 500),
    "500-5000": (500, 5000), "5000+": (5000, None),
}


def _rank_category_ids(path: Any) -> list[str]:
    if not isinstance(path, list) or len(path) > 3:
        raise RecoverablePrioritySourceReadError("带货商品榜分类路径格式无效")
    result = [_category_id_scalar(value).rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1] for value in path]
    if any(not value for value in result):
        raise RecoverablePrioritySourceReadError("带货商品榜分类路径缺少稳定 ID")
    return result


def _select_rank_category(page: Page, path: list[str], requests: list[Dict[str, Any]]) -> Dict[str, Any]:
    ids = _rank_category_ids(path)
    control = page.locator('[class*="categoryCascader"] .aurora-select').first
    if not control.is_visible():
        raise RecoverablePrioritySourceReadError("带货商品榜分类控件不可见")
    page.wait_for_timeout(350)
    previous_request_count = len(requests)
    control.click(timeout=8_000, no_wait_after=True)
    expected_names: list[str] = []
    keys = [CATEGORY_PATH_SEPARATOR.join(ids[:index]) for index in range(1, len(ids) + 1)] or ["-1"]
    for key in keys:
        options = page.locator('.aurora-cascader-menu-item[data-path-key="' + key + '"]')
        visible = [options.nth(index) for index in range(min(options.count(), 5)) if options.nth(index).is_visible()]
        if len(visible) != 1:
            raise RecoverablePrioritySourceReadError(f"带货商品榜分类 ID {key} 未唯一定位")
        expected_names.append(visible[0].inner_text().strip())
        if ids or visible[0].get_attribute("aria-checked") != "true":
            # 后台页的 CSS 过渡可能长期保持“不稳定”；DOM click 仍交给百应 React 处理，之后回读请求。
            visible[0].evaluate("node => node.click()")
        page.wait_for_timeout(200)
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)
    display = page.locator('[class*="categoryCascader"] .aurora-select-content-value').first.inner_text().strip()
    if not display or (ids and not all(name in display for name in expected_names)) or (not ids and display != "总榜"):
        raise RecoverablePrioritySourceReadError("带货商品榜分类点击后未回读到预期路径")
    new_requests = requests[previous_request_count:]
    if new_requests:
        last = new_requests[-1]
        expected_id = ids[-1] if ids else "-1"
        if str(last.get("cate_id")) != expected_id or str(last.get("cate_level")) != str(len(ids) - 1):
            raise RecoverablePrioritySourceReadError("带货商品榜分类请求 ID 与所选路径不一致")
    return {"categoryIds": ids, "categoryLabel": display, "categoryId": ids[-1] if ids else "-1"}


def _select_rank_dropdown(
    page: Page, label: str, value: str, request_key: str,
    request_value: str, requests: list[Dict[str, Any]],
) -> str:
    trigger = page.locator('[data-variant="combineSelect"]').filter(has_text=label)
    if trigger.count() != 1 or not trigger.first.is_visible():
        raise RecoverablePrioritySourceReadError(f"带货商品榜{label}控件未唯一定位")
    marker = {"售价区间": "1-10", "佣金率": "≥10%", "体验分": "≥75"}.get(label)
    if not marker:
        raise RecoverablePrioritySourceReadError("带货商品榜未知筛选控件")
    option_script = """({ value, marker, click }) => {
      const menus = [...document.querySelectorAll('.aurora-select-dropdown')]
        .filter(node => !node.classList.contains('aurora-select-dropdown-hidden') &&
          !node.classList.contains('aurora-slide-up-leave') &&
          [...node.querySelectorAll('.aurora-select-item-option')]
            .some(option => option.getAttribute('title') === marker));
      const matches = menus.flatMap(menu => [...menu.querySelectorAll('.aurora-select-item-option')])
        .filter(node => node.getAttribute('title') === value);
      if (matches.length !== 1) return { count: matches.length, selected: false };
      const selected = matches[0].classList.contains('aurora-select-item-option-selected');
      if (click) matches[0].click();
      return { count: 1, selected };
    }"""
    previous_request_count = len(requests)
    trigger.evaluate("node => node.click()")
    state = page.evaluate(option_script, {"value": value, "marker": marker, "click": False})
    if not isinstance(state, dict) or state.get("count") != 1:
        # 后台页上一弹窗的退出动画可能悬停；再打开当前控件并按唯一档位回读。
        trigger.evaluate("node => node.click()")
        state = page.evaluate(option_script, {"value": value, "marker": marker, "click": False})
    if not isinstance(state, dict) or state.get("count") != 1:
        raise RecoverablePrioritySourceReadError(f"带货商品榜{label}档位 {value} 未唯一定位")
    if not state.get("selected") or value == "不限":
        page.evaluate(option_script, {"value": value, "marker": marker, "click": True})
        page.wait_for_timeout(250)
    if value == "不限":
        if trigger.get_attribute("data-active") != "false":
            raise RecoverablePrioritySourceReadError(f"带货商品榜{label}未回读到不限状态")
    else:
        trigger.evaluate("node => node.click()")
        state = page.evaluate(option_script, {"value": value, "marker": marker, "click": False})
        if not isinstance(state, dict) or state.get("count") != 1 or not state.get("selected"):
            raise RecoverablePrioritySourceReadError(f"带货商品榜{label}点击后未回读到选中状态")
    page.keyboard.press("Escape")
    new_requests = requests[previous_request_count:]
    if new_requests and str(new_requests[-1].get(request_key) or "") != request_value:
        raise RecoverablePrioritySourceReadError(f"带货商品榜{label}请求与选中档位不一致")
    return value


def _select_rank_price_range(page: Page, value: str, requests: list[Dict[str, Any]]) -> str:
    if value not in _RANK_PRICE_BOUNDS:
        raise RecoverablePrioritySourceReadError("带货商品榜售价区间无效")
    return _select_rank_dropdown(page, "售价区间", value, "sale_range", "" if value == "不限" else value, requests)


def _rank_commission_platform_floor(settings: Dict[str, Any]) -> str:
    lower, _ = _commission_rate_bounds(settings)
    available = (10, 20, 30, 50)
    floor = next((level for level in reversed(available) if lower is not None and lower >= level), None)
    return f"≥{floor}%" if floor is not None else "不限"


def _rank_experience_platform_floor(settings: Dict[str, Any]) -> str:
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    score = _number(thresholds.get("experienceScore"))
    # 4.x 店铺体验分与榜单的 75–90 档位单位不同，不能擅自换算。
    floor = next((level for level in (90, 85, 80, 75) if score is not None and score >= level), None)
    return f"≥{floor}" if floor is not None else "不限"


def _rank_row_price_yuan(row: Dict[str, Any]) -> Optional[float]:
    base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
    marketing = base.get("marketing_info") if isinstance(base.get("marketing_info"), dict) else {}
    description = marketing.get("price_desc") if isinstance(marketing.get("price_desc"), dict) else {}
    price = description.get("price") if isinstance(description.get("price"), dict) else {}
    cents = _number(price.get("origin"))
    return cents / 100 if cents is not None and math.isfinite(cents) and cents >= 0 else None


def _rank_row_matches_price(row: Dict[str, Any], value: str) -> bool:
    lower, upper = _RANK_PRICE_BOUNDS[value]
    if lower is None and upper is None:
        return True
    price = _rank_row_price_yuan(row)
    return price is not None and (lower is None or price >= lower) and (upper is None or price <= upper)


def _select_rank_source_options(page: Page, settings: Dict[str, Any]) -> Dict[str, str]:
    config = settings.get("sources") if isinstance(settings.get("sources"), dict) else {}
    try:
        page.get_by_text("爆款榜", exact=True).first.wait_for(state="visible", timeout=8_000)
    except PlaywrightTimeoutError as error:
        raise RecoverablePrioritySourceReadError("带货商品榜选项未加载，不能确认榜单类型") from error
    def visible_options(names: Sequence[str]) -> list[Any]:
        options: list[Any] = []
        for name in names:
            choices = page.get_by_text(name, exact=True)
            options.extend(choices.nth(i) for i in range(min(choices.count(), 12)) if choices.nth(i).is_visible())
        return options

    selected: Dict[str, str] = {}
    groups = (
        ("rankBoard", "爆款榜", ("爆款榜", "趋势榜", "带货榜", "常销榜")),
        ("rankPeriod", "昨日", ("实时", "昨日", "近7日", "近30日")),
        ("rankCarrier", "不限载体", ("不限载体", "不限体裁", "短视频", "直播", "橱窗", "图文")),
    )
    for key, expected, labels in groups:
        label = str(config.get(key) or expected)
        # 百应当前页面把旧设置名“不限载体”显示为“不限体裁”；保留旧设置值，避免改写批次快照。
        if key == "rankCarrier" and label == "不限体裁":
            label = "不限载体"
        aliases = ("不限载体", "不限体裁") if key == "rankCarrier" and label == "不限载体" else (label,)
        visible = visible_options(aliases)
        if len(visible) != 1:
            raise RecoverablePrioritySourceReadError(f"带货商品榜「{label}」控件未唯一定位，不能确认榜单筛选")
        option = visible[0]
        if not _rank_option_selected(option):
            try:
                option.click(timeout=8_000)
            except PlaywrightTimeoutError as error:
                raise RecoverablePrioritySourceReadError(f"带货商品榜「{label}」控件未能选中") from error
            page.wait_for_timeout(700)
            visible = visible_options(aliases)
            if len(visible) != 1 or not _rank_option_selected(visible[0]):
                raise RecoverablePrioritySourceReadError(f"带货商品榜「{label}」点击后未回读到选中状态")
        selected_labels = []
        for item_label in labels:
            matches = page.get_by_text(item_label, exact=True)
            matches_visible = [matches.nth(i) for i in range(min(matches.count(), 12)) if matches.nth(i).is_visible()]
            if len(matches_visible) == 1 and _rank_option_selected(matches_visible[0]):
                selected_labels.append("不限载体" if key == "rankCarrier" and item_label == "不限体裁" else item_label)
        if selected_labels != [label]:
            raise RecoverablePrioritySourceReadError(f"带货商品榜「{label}」选中状态不唯一，未采集当前页")
        selected[key] = label
    return selected


def _candidate_source_scroll_checkpoint_key(settings: Optional[Dict[str, Any]], source_key: str) -> str:
    config = settings if isinstance(settings, dict) else {}
    raw_path = config.get("_quotaCategoryPath") or config.get("_sourceCategoryPath")
    if not isinstance(raw_path, list):
        selection = config.get("categorySelection")
        raw_path = [
            value for level in ("level1", "level2", "level3")
            for value in (selection.get(level) if isinstance(selection, dict) and isinstance(selection.get(level), list) else [])[:1]
            if value not in (None, "")
        ]
    path = [str(value).strip() for value in raw_path if str(value).strip()] if isinstance(raw_path, list) else []
    return f"{source_key}:{'/'.join(path)}" if path else source_key


def _scroll_position_top(value: Any) -> Optional[int]:
    raw = value.get("top") if isinstance(value, dict) else value
    try:
        return max(0, min(100_000_000, int(float(raw))))
    except (TypeError, ValueError, OverflowError):
        return None


def _hotspot_request_category_ids(request: Any) -> set[str]:
    """只提取百应热点列表请求中的显式类目 ID，不保留 URL 或请求正文。"""
    try:
        parsed = urlparse(str(request.url))
        if parsed.hostname != "buyin.jinritemai.com" or str(request.resource_type) not in {"xhr", "fetch"}:
            return set()
        keys = {
            "cate_id", "cateId", "category_id", "categoryId",
            "first_cate_id", "firstCateId", "first_category_id", "root_cate_id", "top_cate_id",
        }
        found: set[str] = set()

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 3:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in keys:
                        candidates = child if isinstance(child, list) else [child]
                        for candidate in candidates:
                            category_id = _category_id_scalar(candidate)
                            if category_id and category_id.isdigit() and int(category_id) > 0:
                                found.add(category_id)
                    elif isinstance(child, (dict, list)):
                        collect(child, depth + 1)
            elif isinstance(value, list):
                for child in value[:30]:
                    collect(child, depth + 1)

        collect(parse_qs(parsed.query))
        try:
            payload = request.post_data_json
        except Exception:
            payload = None
        if isinstance(payload, (dict, list)):
            collect(payload)
        else:
            data = request.post_data
            if isinstance(data, str):
                collect(parse_qs(data))
        return found
    except Exception:
        return set()


def _read_priority_source_product_ids(
    page: Page, source_key: str, category_tree: Sequence[Any] = (),
    category_tab: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> tuple[set[str], list[Dict[str, Any]], Dict[str, Any]]:
    """只读取热点/榜单当前可见行的商品数据；不搜索、加车或翻页。"""
    source = BAIYING_PRIORITY_SOURCES.get(source_key)
    if source is None:
        raise RuntimeError("未知的百应优先来源")
    label, url = source
    candidate_selector = (
        BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR
        if source_key == "allianceRank"
        else BAIYING_PRIORITY_ROOT_SELECTOR
    )
    list_container_selector = BAIYING_PRIORITY_SOURCE_LIST_SELECTORS.get(
        source_key, BAIYING_PRIORITY_LIST_CONTAINER_SELECTOR
    )
    source_page: Optional[Page] = None
    rate_limit_errors: list[str] = []
    rank_requests: list[Dict[str, Any]] = []
    rank_root_catalog: Dict[str, set[str]] = {}
    hot_category_requests: list[set[str]] = []
    source_product_request_ids: set[int] = set()

    def observe_hot_category_request(request: Any) -> None:
        if source_key != "hotSpot":
            return
        ids = _hotspot_request_category_ids(request)
        if ids:
            hot_category_requests.append(ids)
            source_product_request_ids.add(id(request))

    def observe_rank_request(request: Any) -> None:
        if source_key != "allianceRank" or "/pc/leaderboard_26/board_pmt" not in str(request.url):
            return
        source_product_request_ids.add(id(request))
        try:
            payload = request.post_data_json
            if isinstance(payload, dict):
                rank_requests.append({key: payload.get(key) for key in ("cate_id", "cate_level", "sale_range", "cos_ratio", "exp_score")})
        except Exception:
            return

    def observe_rank_catalog(response: Any) -> None:
        if source_key != "allianceRank" or "/pc/leaderboard_26/get_board_default_info" not in str(response.url):
            return
        try:
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            industry = data.get("industry_list") if isinstance(data, dict) else []
            for top in industry if isinstance(industry, list) else []:
                children = top.get("child_cate_list") if isinstance(top, dict) else []
                for child in children if isinstance(children, list) else []:
                    if not isinstance(child, dict) or str(child.get("father_cate_id")) != "0":
                        continue
                    category_id = _category_id_scalar(child.get("cate_id"))
                    name = str(child.get("cate_name") or "").strip()
                    if category_id and name:
                        rank_root_catalog.setdefault(name, set()).add(category_id)
        except Exception:
            return

    def observe_rate_limit(response: Any) -> None:
        request = getattr(response, "request", None)
        try:
            parsed_url = urlparse(str(getattr(response, "url", "")))
            if (parsed_url.hostname != "buyin.jinritemai.com" or request is None or
                    id(request) not in source_product_request_ids):
                return
            resource_type = str(getattr(request, "resource_type", ""))
            if callable(getattr(request, "resource_type", None)):
                resource_type = str(request.resource_type())
            if resource_type not in {"xhr", "fetch"}:
                return
            headers = getattr(response, "headers", {})
            if callable(headers):
                headers = headers()
            content_type = str(headers.get("content-type", "")).lower() if isinstance(headers, dict) else ""
            if "json" not in content_type:
                return
            payload = response.json()
            if isinstance(payload, dict) and str(payload.get("code")) == "11001":
                diagnostics = _material_list_response_diagnostics(response, payload)
                rate_limit_errors.append(
                    f"百应{label}商品来源请求返回业务码 11001；"
                    f"接口 {parsed_url.path[:180]}；百应响应诊断：{diagnostics[:320]}"
                )
        except Exception:
            return

    try:
        source_page = _create_selection_work_page(page.context)
        source_page.on("response", observe_rate_limit)
        if source_key == "hotSpot":
            source_page.on("request", observe_hot_category_request)
        if source_key == "allianceRank":
            source_page.on("response", observe_rank_catalog)
            source_page.on("request", observe_rank_request)
        # SPA 页面可能迟迟不触发 DOMContentLoaded；先等首个响应提交，再按真实列表/空状态判断就绪。
        response = source_page.goto(url, wait_until="commit", timeout=45_000)
        if rate_limit_errors:
            raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求")
        parsed = urlparse(source_page.url)
        if parsed.hostname != "buyin.jinritemai.com":
            raise RuntimeError("百应来源页面未留在百应工作台，可能需要重新登录；已停止当前账号")
        if parsed.path.rstrip("/") != urlparse(url).path.rstrip("/"):
            raise RuntimeError("百应来源页跳转到其他页面，登录状态或页面身份不确定；已停止当前账号")
        status = getattr(response, "status", None) if response is not None else None
        if callable(status):
            status = status()
        if isinstance(status, int) and status in {401, 403}:
            raise RuntimeError(f"百应{label}页面返回 HTTP {status}，请检查账号登录或页面权限")

        prompt_kind, prompt_text = _visible_baiying_prompt(source_page)
        if prompt_kind == "manual_verification":
            raise RuntimeError("百应要求验证码或安全验证，已停止当前账号")
        if prompt_kind:
            raise RuntimeError(f"百应来源页面存在待处理提示，商品身份无法唯一确认：{prompt_text[:160]}")

        if source_key == "hotSpot":
            root_names = [
                str(root.get("name") or "").strip() for root in category_tree
                if isinstance(root, dict) and str(root.get("name") or "").strip()
            ]
            if not root_names:
                raise RecoverablePrioritySourceReadError("选品热点缺少当前百应一级类目目录，无法定位分类按钮")
            try:
                source_page.wait_for_function(
                    """({ selector, names }) => {
                      const expected = new Set(names);
                      return Array.from(document.querySelectorAll(selector)).some(node => {
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 &&
                          expected.has(String(node.textContent || '').trim());
                      });
                    }""",
                    arg={
                        "selector": 'div[class*="cateFilterMain"] > div[class*="box"]',
                        "names": root_names,
                    },
                    timeout=15_000,
                )
            except PlaywrightTimeoutError as error:
                if rate_limit_errors:
                    raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求") from error
                raise RecoverablePrioritySourceReadError(
                    "选品热点一级分类按钮在页面加载后 15 秒内未出现；未用全部页代替分类结果"
                ) from error

        rank_selection: Dict[str, Any] = {}
        if source_key == "allianceRank":
            rank_selection = _select_rank_source_options(source_page, settings or {})
            config = settings.get("sources") if isinstance(settings, dict) and isinstance(settings.get("sources"), dict) else {}
            quota_path = settings.get("_quotaCategoryPath") if isinstance(settings, dict) else None
            source_path = settings.get("_sourceCategoryPath") if isinstance(settings, dict) else None
            requested_path = (
                quota_path if isinstance(quota_path, list) and quota_path else
                source_path if isinstance(source_path, list) and source_path else
                config.get("rankCategoryPath", [])
            )
            requested_path = _rank_category_ids(requested_path)
            if requested_path:
                resolved, _ = _resolve_single_category_id(requested_path[-1], category_tree)
                if len(resolved) != len(requested_path) or any(
                    not _category_id_matches(actual, expected) for actual, expected in zip(resolved, requested_path)
                ):
                    raise RecoverablePrioritySourceReadError("带货商品榜分类 ID 无法在当前百应类目树中唯一核实")
            rank_selection.update(_select_rank_category(source_page, requested_path, rank_requests))
            price_range = str(config.get("rankPriceRange") or "不限")
            rank_selection["rankPriceRange"] = _select_rank_price_range(source_page, price_range, rank_requests)
            commission_floor = _rank_commission_platform_floor(settings or {})
            rank_selection["rankCommissionFloor"] = _select_rank_dropdown(
                source_page, "佣金率", commission_floor, "cos_ratio",
                "" if commission_floor == "不限" else commission_floor[1:-1], rank_requests,
            )
            experience_floor = _rank_experience_platform_floor(settings or {})
            rank_selection["rankExperienceFloor"] = _select_rank_dropdown(
                source_page, "体验分", experience_floor, "exp_score",
                "" if experience_floor == "不限" else experience_floor[1:], rank_requests,
            )

        tab_read_error = ""
        try:
            tabs = _source_category_tabs(source_page, source_key, category_tree, rank_root_catalog)
        except Exception as error:
            if category_tab is not None:
                raise RecoverablePrioritySourceReadError(f"{label}分类控件无法安全回读") from error
            tabs = []
            tab_read_error = type(error).__name__
        if category_tab is not None:
            expected_id = str(category_tab.get("categoryId") or "")
            expected_index = int(category_tab.get("index", -1))
            requested_path = category_tab.get("categoryIds") if isinstance(category_tab.get("categoryIds"), list) else []
            if source_key == "hotSpot" and len(requested_path) != 1:
                raise RecoverablePrioritySourceReadError("选品热点只有一级分类入口，不能按二级或三级配额采集")
            selected_tab = next((item for item in tabs if item["index"] == expected_index), None)
            if not expected_id or selected_tab is None or selected_tab["categoryId"] != expected_id:
                raise RecoverablePrioritySourceReadError(f"{label}来源分类控件已变化，无法按稳定 ID 选中分类")
            hot_request_start = 0 if selected_tab.get("selected") else len(hot_category_requests)
            tab_selector = ('div[class*="subCategoryGroup"] > div[class*="box"]'
                            if source_key == "allianceRank" else 'div[class*="cateFilterMain"] > div[class*="box"]')
            if source_key == "allianceRank":
                source_page.locator(tab_selector).nth(expected_index).evaluate("node => node.click()")
            elif not selected_tab.get("selected"):
                source_page.locator(tab_selector).nth(expected_index).click(timeout=8_000)
            source_page.wait_for_timeout(1_000)
            if rate_limit_errors:
                raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求")
            tabs = _source_category_tabs(source_page, source_key, category_tree, rank_root_catalog)
            if source_key == "allianceRank":
                display = source_page.locator('[class*="categoryCascader"] .aurora-select-content-value').first.inner_text().strip()
                if (str(category_tab.get("name") or "") not in display or not rank_requests or
                        str(rank_requests[-1].get("cate_id")) != expected_id):
                    raise RecoverablePrioritySourceReadError("带货商品榜分类标签与实际榜单请求 ID 不一致")
                # 百应点击一级分类后将它移入级联标题，下方标签随即变成二级，原 index 不再存在。
                selected_tab = {**category_tab, "selected": True, "index": -1}
            else:
                if len(requested_path) > 1:
                    deep_matches = [
                        item for item in tabs
                        if item.get("status") == "mapped"
                        and isinstance(item.get("categoryIds"), list)
                        and len(item["categoryIds"]) >= len(requested_path)
                        and all(_category_id_matches(str(item["categoryIds"][level]), str(requested_path[level]))
                                for level in range(len(requested_path)))
                    ]
                    if len(deep_matches) != 1:
                        raise RecoverablePrioritySourceReadError(
                            f"{label}未能在当前页面唯一打开三级类目路径；未读取全部页"
                        )
                    deep_tab = deep_matches[0]
                    if int(deep_tab.get("index", -1)) < 0:
                        raise RecoverablePrioritySourceReadError(f"{label}深层类目控件缺少稳定索引；未读取全部页")
                    source_page.locator(tab_selector).nth(int(deep_tab["index"])).click(timeout=8_000)
                    source_page.wait_for_timeout(1_000)
                    tabs = _source_category_tabs(source_page, source_key, category_tree, rank_root_catalog)
                    selected_tab = next(
                        (item for item in tabs if item.get("index") == deep_tab.get("index")
                         and item.get("categoryId") == deep_tab.get("categoryId") and item.get("selected") is True),
                        None,
                    )
                else:
                    selected_tab = next((item for item in tabs if item["index"] == expected_index), None)
                path_matches = (
                    len(requested_path) <= 1
                    or (isinstance(selected_tab, dict) and isinstance(selected_tab.get("categoryIds"), list)
                        and len(selected_tab["categoryIds"]) >= len(requested_path)
                        and all(_category_id_matches(str(selected_tab["categoryIds"][level]), str(requested_path[level]))
                                for level in range(len(requested_path))))
                )
                if selected_tab is None or (len(requested_path) <= 1 and selected_tab.get("categoryId") != expected_id) or not path_matches or not selected_tab.get("selected"):
                    raise RecoverablePrioritySourceReadError(f"{label}来源分类选中状态无法回读；未将商品归入该分类")
                if source_key == "hotSpot" and selected_tab.get("idSource") == "tree_name_pending_request":
                    for _ in range(4):
                        if rate_limit_errors or any(expected_id in ids for ids in hot_category_requests[hot_request_start:]):
                            break
                        source_page.wait_for_timeout(500)
                    if rate_limit_errors:
                        raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求")
                    if not any(expected_id in ids for ids in hot_category_requests[hot_request_start:]):
                        raise RecoverablePrioritySourceReadError(
                            "选品热点分类名称虽与类目树唯一匹配，但未从该分类的实际请求回读到一级类目 ID；结果不入池"
                        )
                    selected_tab = {**selected_tab, "status": "mapped", "idSource": "request_confirmed"}
        else:
            if source_key == "allianceRank" and rank_selection.get("categoryIds"):
                selected_tab = {
                    "name": str(rank_selection.get("categoryLabel") or "")[:50],
                    "categoryId": rank_selection["categoryIds"][-1],
                    "categoryIds": rank_selection["categoryIds"],
                    "level": len(rank_selection["categoryIds"]),
                    "status": "mapped", "selected": True, "index": -1,
                }
            else:
                selected_tabs = [item for item in tabs if item["selected"]]
                selected_tab = selected_tabs[0] if len(selected_tabs) == 1 else None

        readiness_reached = False
        readiness_wait_started = time.monotonic()
        readiness_wait_ms = 0
        try:
            source_page.wait_for_function(
                """({ selector, listContainerSelector, loadingSelector, emptyPattern, sourceKey }) => {
                  const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const isNavigationElement = (element) => Boolean(element.closest(
                    'nav, [role="navigation"], [role="menu"], .auxo-menu, [class*="headerNav"], [class*="menu-bar"]'
                  ));
                  const loadingVisible = Array.from(document.querySelectorAll(loadingSelector)).some(visible);
                  const loadingText = /正在加载|加载中|loading/i.test(document.body?.innerText || '');
                  const stillLoading = loadingVisible || loadingText;
                  const isExcludedAllianceRow = (element) => {
                    if (element.closest('nav, [role="navigation"], [role="menu"], .auxo-menu, [class*="headerNav"], [class*="menu-bar"]')) return true;
                    if (element.matches('thead, th, [role="columnheader"]') ||
                        (element.matches('tr, [role="row"]') && element.querySelector('th, [role="columnheader"]'))) return true;
                    for (let node = element, level = 0; node && level < 5; node = node.parentElement, level += 1) {
                      const className = String(node.className || '').toLowerCase();
                      if (/productrow|measure|placeholder|skeleton|tablehead|thead|tableheader|headerrow|columnheader/.test(className) ||
                          (level === 0 && className.includes('header'))) return true;
                      if (level === 0 && className.includes('goodslistwrap')) return true;
                      if (node.matches('thead, th, [role="columnheader"]')) return true;
                    }
                    return false;
                  };
                  const readAllianceProductIds = (root) => {
                    const scanIds = (value, ids, seen, depth, budget) => {
                      if (!value || typeof value !== 'object' || depth > 8 || budget.count > 2500 || seen.has(value)) return;
                      seen.add(value);
                      budget.count += 1;
                      if (Array.isArray(value)) {
                        for (const item of value) scanIds(item, ids, seen, depth + 1, budget);
                        return;
                      }
                      const productId = value.product_id ?? value.productId;
                      const hasProductShape = value.product_info || value.productInfo || value.base_model || value.baseModel ||
                        value.promotion_info || value.promotionInfo || value.product_name || value.title || value.name;
                      if (productId != null && hasProductShape && /^\\d{5,}$/.test(String(productId).trim())) ids.add(String(productId).trim());
                      for (const [key, child] of Object.entries(value)) {
                        if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
                        scanIds(child, ids, seen, depth + 1, budget);
                      }
                    };
                    for (let element = root, level = 0; element && level < 6; element = element.parentElement, level += 1) {
                      const localIds = new Set();
                      for (const key of Object.keys(element)) {
                        if (key.startsWith('__reactProps$')) scanIds(element[key], localIds, new WeakSet(), 0, { count: 0 });
                      }
                      if (localIds.size) return localIds.size === 1 ? localIds : new Set();
                    }
                    return new Set();
                  };
                  const visibleRoots = Array.from(document.querySelectorAll(selector)).filter(visible).slice(0, 120);
                  const roots = sourceKey === 'allianceRank'
                    ? visibleRoots.filter((root) => !isExcludedAllianceRow(root) && readAllianceProductIds(root).size === 1)
                    : visibleRoots.slice(0, 80);
                  const ids = new Set();
                  const scan = (value, seen, depth, budget) => {
                    if (!value || typeof value !== 'object' || depth > 7 || budget.count > 3500 || seen.has(value)) return;
                    seen.add(value);
                    budget.count += 1;
                    if (Array.isArray(value)) {
                      for (const item of value) scan(item, seen, depth + 1, budget);
                      return;
                    }
                    const productId = value.product_id ?? value.productId;
                    const hasProductShape = value.product_info || value.productInfo || value.base_model || value.baseModel ||
                      value.promotion_info || value.promotionInfo || value.product_name || value.title || value.name;
                    if (productId != null && hasProductShape) ids.add(String(productId));
                    for (const [key, child] of Object.entries(value)) {
                      if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
                      scan(child, seen, depth + 1, budget);
                    }
                  };
                  if (sourceKey === 'allianceRank') {
                    for (const root of roots) for (const id of readAllianceProductIds(root)) ids.add(id);
                  } else for (const root of roots) {
                    let element = root;
                    for (let level = 0; element && level < 6; level += 1, element = element.parentElement) {
                      for (const key of Object.keys(element)) {
                        if (key.startsWith('__reactProps$')) scan(element[key], new WeakSet(), 0, { count: 0 });
                      }
                    }
                  }
                  const emptyPatternMatcher = new RegExp(emptyPattern);
                  const bodyHasEmptyText = emptyPatternMatcher.test(document.body?.innerText || '');
                  const hasExplicitEmptyState = bodyHasEmptyText && Array.from(document.querySelectorAll('body *')).some((element) => {
                    if (!visible(element)) return false;
                    const text = (element.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!text || text.length > 80 || !emptyPatternMatcher.test(text)) return false;
                    return Array.from(element.children).every((child) =>
                      !emptyPatternMatcher.test((child.innerText || '').replace(/\\s+/g, ' ').trim())
                    );
                  });
                  const listContainers = Array.from(document.querySelectorAll(listContainerSelector))
                    .filter((element) => visible(element) && !isNavigationElement(element));
                  const hasVisibleProductRoots = roots.some((root) => listContainers.some((container) =>
                    container.contains(root) || root.contains(container)
                  ));
                  if (sourceKey === 'hotSpot') {
                    // 热点趋势只认真实滚动区里的商品节点/ID；筛选控件和空状态文案不作为就绪信号。
                    return !stillLoading && (ids.size > 0 || hasVisibleProductRoots);
                  }
                  return !stillLoading && (hasExplicitEmptyState || ids.size > 0);
                }""",
                arg={
                    "selector": candidate_selector,
                    "listContainerSelector": list_container_selector,
                    "loadingSelector": BAIYING_PRIORITY_ACTIVE_LOADING_SELECTOR,
                    "emptyPattern": BAIYING_PRIORITY_EMPTY_PATTERN,
                    "sourceKey": source_key,
                },
                timeout=BAIYING_PRIORITY_READY_TIMEOUT_MS,
                polling=500,
            )
            readiness_reached = True
        except PlaywrightTimeoutError:
            # 占位节点不算就绪；下方会再检查一次真实商品 ID 和明确空状态。
            prompt_kind, prompt_text = _visible_baiying_prompt(source_page)
            if prompt_kind == "manual_verification":
                raise RuntimeError("百应要求验证码或安全验证，已停止当前账号")
            if prompt_kind:
                raise RuntimeError(f"百应来源页面存在待处理提示，商品身份无法唯一确认：{prompt_text[:160]}")
        finally:
            readiness_wait_ms = int((time.monotonic() - readiness_wait_started) * 1000)

        snapshot_script = """({ selector, listContainerSelectorArg, contentRootSelectorArg, loadingSelectorArg, emptyPattern, sourceKey }) => {
          const visible = (element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const allRoots = Array.from(document.querySelectorAll(selector));
          const visibleRoots = allRoots.filter(visible);
          const isExcludedAllianceRow = (element) => {
            if (element.closest('nav, [role="navigation"], [role="menu"], .auxo-menu, [class*="headerNav"], [class*="menu-bar"]')) return true;
            if (element.matches('thead, th, [role="columnheader"]') ||
                (element.matches('tr, [role="row"]') && element.querySelector('th, [role="columnheader"]'))) return true;
            for (let node = element, level = 0; node && level < 5; node = node.parentElement, level += 1) {
              const className = String(node.className || '').toLowerCase();
              if (/productrow|measure|placeholder|skeleton|tablehead|thead|tableheader|headerrow|columnheader/.test(className) ||
                  (level === 0 && className.includes('header'))) return true;
              if (level === 0 && className.includes('goodslistwrap')) return true;
              if (node.matches('thead, th, [role="columnheader"]')) return true;
            }
            return false;
          };
          const readAllianceProductIds = (root) => {
            const scanIds = (value, ids, seen, depth, budget) => {
              if (!value || typeof value !== 'object' || depth > 8 || budget.count > 2500 || seen.has(value)) return;
              seen.add(value);
              budget.count += 1;
              if (Array.isArray(value)) {
                for (const item of value) scanIds(item, ids, seen, depth + 1, budget);
                return;
              }
              const productId = value.product_id ?? value.productId;
              const hasProductShape = value.product_info || value.productInfo || value.base_model || value.baseModel ||
                value.promotion_info || value.promotionInfo || value.product_name || value.title || value.name;
              if (productId != null && hasProductShape && /^\\d{5,}$/.test(String(productId).trim())) ids.add(String(productId).trim());
              for (const [key, child] of Object.entries(value)) {
                if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
                scanIds(child, ids, seen, depth + 1, budget);
              }
            };
            for (let element = root, level = 0; element && level < 6; element = element.parentElement, level += 1) {
              const localIds = new Set();
              for (const key of Object.keys(element)) {
                if (key.startsWith('__reactProps$')) scanIds(element[key], localIds, new WeakSet(), 0, { count: 0 });
              }
              if (localIds.size) return localIds.size === 1 ? localIds : new Set();
            }
            return new Set();
          };
          const allianceProductIdsByRoot = new Map();
          const roots = sourceKey === 'allianceRank'
            ? visibleRoots.filter((root) => {
                if (isExcludedAllianceRow(root)) return false;
                const rowIds = readAllianceProductIds(root);
                if (rowIds.size !== 1) return false;
                allianceProductIdsByRoot.set(root, rowIds);
                return true;
              })
            : visibleRoots;
          const verifiedAllianceIds = new Set();
          for (const rowIds of allianceProductIdsByRoot.values()) for (const id of rowIds) verifiedAllianceIds.add(id);
          const ids = new Set();
          const rows = new Map();
          const categoryFieldSamples = [];
          const visited = new WeakSet();
          let visitedCount = 0;
          let currentTopicMetrics = null;
          const scan = (value, depth) => {
            if (!value || typeof value !== 'object' || depth > 9 || visited.has(value) || visitedCount > 12000) return;
            visited.add(value);
            visitedCount += 1;
            if (Array.isArray(value)) {
              for (const item of value) scan(item, depth + 1);
              return;
            }
            const productId = value.product_id ?? value.productId;
            const hasProductShape = value.product_info || value.productInfo || value.base_model || value.baseModel ||
              value.promotion_info || value.promotionInfo || value.product_name || value.title || value.name;
            if (productId != null && hasProductShape && (sourceKey !== 'allianceRank' || verifiedAllianceIds.has(String(productId).trim()))) {
              const id = String(productId).trim();
              if (id) {
                ids.add(id);
                const base = value.base_model || value.baseModel || {};
                const productInfo = base.product_info || base.productInfo || value.product_info || value.productInfo || {};
                const promotionInfo = base.promotion_info || base.promotionInfo || value.promotion_info || value.promotionInfo || {};
                if (!rows.has(id) && productInfo && typeof productInfo === 'object') {
                  const safeJson = (item) => {
                    try {
                      const seen = new WeakSet();
                      return JSON.parse(JSON.stringify(item, (_key, child) => {
                        if (!child || typeof child !== 'object') return child;
                        if (seen.has(child)) return undefined;
                        seen.add(child);
                        return child;
                      }));
                    } catch (_) { return {}; }
                  };
                  const collectCategoryFields = (root) => {
                    const found = {};
                    const seen = new WeakSet();
                    let visited = 0;
                    const scalarId = (value) => {
                      if (typeof value !== 'string' && typeof value !== 'number') return '';
                      const text = String(value).trim();
                      return /^\d+(?:__RC_CASCADER_SPLIT__\d+)*$/.test(text) ? text : '';
                    };
                    const walk = (node, path, depth, categoryScope) => {
                      if (!node || typeof node !== 'object' || depth > 6 || visited >= 500 || seen.has(node)) return;
                      seen.add(node);
                      visited += 1;
                      if (Array.isArray(node)) {
                        node.slice(0, 12).forEach((child, index) => walk(child, `${path}[${index}]`, depth + 1, categoryScope));
                        return;
                      }
                      for (const [key, child] of Object.entries(node)) {
                        if (Object.keys(found).length >= 24) break;
                        const nextPath = path ? `${path}.${key}` : key;
                        const inCategory = categoryScope || /(category|cate)/i.test(key);
                        const numericId = inCategory ? scalarId(child) : '';
                        if (numericId) found[nextPath] = numericId;
                        if (child && typeof child === 'object') walk(child, nextPath, depth + 1, inCategory);
                      }
                    };
                    walk(root, '', 0, false);
                    return found;
                  };
                  const category = productInfo.category && typeof productInfo.category === 'object'
                    ? productInfo.category
                    : {};
                  const hasCompleteCategoryPath = ['first', 'second', 'third'].every((key) =>
                    category[key] != null && String(category[key]).trim() !== ''
                  );
                  if (!hasCompleteCategoryPath && categoryFieldSamples.length < 5) {
                    categoryFieldSamples.push({
                      productId: id,
                      productInfoKeys: Object.keys(productInfo).slice(0, 60),
                      categoryLikeFields: collectCategoryFields(value)
                    });
                  }
                  rows.set(id, {
                    product_id: id,
                    promotion_id: String(value.promotion_id || value.promotionId || value.commodity_id || value.commodityId || ''),
                    title: String(value.title || value.product_name || productInfo.name || ''),
                    base_model: {
                      product_info: safeJson(productInfo),
                      promotion_info: safeJson(promotionInfo),
                      shop_info: safeJson(base.shop_info || base.shopInfo || {}),
                      marketing_info: safeJson(base.marketing_info || base.marketingInfo || {}),
                      extend_info: safeJson(base.extend_info || base.extendInfo || {})
                    },
                    custom_model: safeJson(value.custom_model || value.customModel || {}),
                    _topicMetrics: currentTopicMetrics
                  });
                }
              }
            }
            for (const [key, child] of Object.entries(value)) {
              if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
              scan(child, depth + 1);
            }
          };
          for (const root of roots) {
            currentTopicMetrics = null;
            if (sourceKey === 'hotSpot') {
              for (let ancestor = root, depth = 0; ancestor && depth < 7; ancestor = ancestor.parentElement, depth++) {
                const text = String(ancestor.innerText || '').replace(/\\s+/g, ' ');
                if (text.length > 2200) break;
                const exposure = text.match(/曝光热度\\s*([\\d.]+\\s*[万千亿]?)/);
                const related = text.match(/关联商品数\\s*([\\d.]+\\s*[万千亿]?\\+?)/);
                if (exposure && related) {
                  currentTopicMetrics = { exposure: exposure[1], relatedProducts: related[1] };
                  break;
                }
              }
            }
            let element = root;
            for (let level = 0; element && level < 6; level += 1, element = element.parentElement) {
              for (const key of Object.keys(element)) {
                if (key.startsWith('__reactProps$')) scan(element[key], 0);
              }
            }
          }
          const emptyPatternMatcher = new RegExp(emptyPattern);
          const bodyContainsEmptyText = emptyPatternMatcher.test(document.body?.innerText || '');
          const emptyState = bodyContainsEmptyText && Array.from(document.querySelectorAll('body *')).some((element) => {
            if (!visible(element)) return false;
            const text = (element.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 80 || !emptyPatternMatcher.test(text)) return false;
            return Array.from(element.children).every((child) =>
              !emptyPatternMatcher.test((child.innerText || '').replace(/\\s+/g, ' ').trim())
            );
          });
          const classHints = Array.from(document.querySelectorAll(
            '[class*="product"], [class*="Product"], [class*="goods"], [class*="Goods"]'
          )).filter(visible).map((element) => String(element.className || '').trim())
            .filter(Boolean).filter((value, index, values) => values.indexOf(value) === index)
            .slice(0, 6).map((value) => value.slice(0, 100));
          const listContainerSelector = listContainerSelectorArg;
          const isNavigationElement = (element) => Boolean(element.closest(
            'nav, [role="navigation"], [role="menu"], .auxo-menu, [class*="headerNav"], [class*="menu-bar"]'
          ));
          const allListContainers = Array.from(document.querySelectorAll(listContainerSelector));
          const navigationContainerMatchCount = allListContainers.filter(isNavigationElement).length;
          const visibleListContainers = allListContainers.filter((element) => visible(element) && !isNavigationElement(element));
          const visibleProductNodes = sourceKey === 'allianceRank'
            ? roots
            : roots.filter((element) =>
                visibleListContainers.some((container) => container.contains(element) || element.contains(container))
              );
          const listContainerChildCounts = visibleListContainers.slice(0, 40).map((element) => Number(element.childElementCount || 0));
          const listContainerSamples = visibleListContainers.slice(0, 24).map((element) => ({
              tag: String(element.tagName || '').toLowerCase(),
              role: String(element.getAttribute('role') || '').slice(0, 40),
              className: String(element.className || '').trim().slice(0, 160),
              childElementCount: Number(element.childElementCount || 0),
              textLength: String(element.innerText || '').trim().length
            }));
          const contentRootSelector = contentRootSelectorArg;
          const allContentRoots = Array.from(document.querySelectorAll(contentRootSelector));
          const visibleContentRoots = allContentRoots.filter(visible);
          const contentRootSamples = visibleContentRoots.slice(0, 12).map((element) => ({
            tag: String(element.tagName || '').toLowerCase(),
            className: String(element.className || '').trim().slice(0, 180),
            childElementCount: Number(element.childElementCount || 0),
            textLength: String(element.innerText || '').trim().length,
            text: String(element.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
          }));
          const loadingSelector = loadingSelectorArg;
          const loadingIndicatorSamples = Array.from(document.querySelectorAll(loadingSelector)).filter(visible).slice(0, 12).map((element) => ({
            tag: String(element.tagName || '').toLowerCase(),
            className: String(element.className || '').trim().slice(0, 160),
            text: String(element.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)
          }));
          const loadingTextDetected = /正在加载|加载中|loading/i.test(document.body?.innerText || '');
          const clone = document.body ? document.body.cloneNode(true) : null;
          if (clone) {
            clone.querySelectorAll('script, style, iframe, object, embed, noscript').forEach((node) => node.remove());
            clone.querySelectorAll('input, textarea, select').forEach((node) => {
              node.removeAttribute('value');
              node.removeAttribute('data-value');
              if (node.tagName === 'TEXTAREA') node.textContent = '';
              if (node.tagName === 'SELECT') node.selectedIndex = -1;
            });
            clone.querySelectorAll('*').forEach((node) => {
              for (const attribute of Array.from(node.attributes || [])) {
                const name = String(attribute.name || '');
                if (/^(on)/i.test(name) || /(cookie|token|secret|auth|session|password)/i.test(name)) {
                  node.removeAttribute(name);
                } else if (name === 'href' || name === 'src' || name === 'action' || name === 'formaction') {
                  try {
                    const target = new URL(String(attribute.value || ''), location.origin);
                    target.search = '';
                    target.hash = '';
                    node.setAttribute(name, target.origin === location.origin
                      ? `${target.pathname}`
                      : `${target.origin}${target.pathname}`);
                  } catch (_) {
                    node.removeAttribute(name);
                  }
                }
              }
            });
          }
          const bodyHtml = clone ? String(clone.outerHTML || '').slice(0, 1500001) : '';
          const clonedScrollContainer = clone
            ? Array.from(clone.querySelectorAll(listContainerSelector)).find((element) => !isNavigationElement(element))
            : null;
          const scrollContainerOuterHtml = clonedScrollContainer
            ? String(clonedScrollContainer.outerHTML || '').slice(0, 1500001)
            : '';
          const safePageUrl = new URL(location.href);
          for (const key of Array.from(safePageUrl.searchParams.keys())) {
            if (/(cookie|token|secret|auth|session|password|signature|sign)/i.test(key)) {
              safePageUrl.searchParams.set(key, '[redacted]');
            }
          }
          safePageUrl.hash = '';
          return {
            ids: Array.from(ids),
            rows: Array.from(rows.values()).slice(0, 200),
            categoryFieldSamples,
            candidateSelector: selector,
            rootMatchCount: allRoots.length,
            visibleRowCount: roots.length,
            visibleProductNodeCount: visibleProductNodes.length,
            emptyState,
            pageTitle: (document.title || '').slice(0, 80),
            pagePath: location.pathname,
            pageUrl: safePageUrl.toString(),
            readyState: document.readyState,
            bodyTextLength: String(document.body?.innerText || '').length,
            rootChildCounts: roots.slice(0, 12).map((element) => Number(element.childElementCount || 0)),
            listContainerMatchCount: allListContainers.length,
            listContainerVisibleCount: visibleListContainers.length,
            navigationContainerMatchCount,
            listContainersWithChildren: listContainerChildCounts.filter((count) => count > 0).length,
            listContainerChildCounts,
            listContainerSamples,
            scrollPosition: visibleListContainers.slice(0, 1).map((element) => ({
              top: Number(element.scrollTop || 0), height: Number(element.scrollHeight || 0),
              viewport: Number(element.clientHeight || 0)
            }))[0] || null,
            scrollContainerOuterHtml,
            contentRootSelector,
            contentRootMatchCount: allContentRoots.length,
            contentRootVisibleCount: visibleContentRoots.length,
            contentRootSamples,
            loadingIndicatorVisibleCount: loadingIndicatorSamples.length,
            loadingIndicatorSamples,
            loadingTextDetected,
            bodyHtml,
            rootClassSamples: roots.slice(0, 5).map((element) => String(element.className || '').trim().slice(0, 100)),
            classHints
          };
        }"""
        snapshot_args = {
            "selector": candidate_selector,
            "listContainerSelectorArg": list_container_selector,
            "contentRootSelectorArg": BAIYING_PRIORITY_CONTENT_ROOT_SELECTOR,
            "loadingSelectorArg": BAIYING_PRIORITY_ACTIVE_LOADING_SELECTOR,
            "emptyPattern": BAIYING_PRIORITY_EMPTY_PATTERN,
            "sourceKey": source_key,
        }
        scroll_positions = settings.get("_candidatePoolScrollPositions") if isinstance(settings, dict) else None
        scroll_checkpoint_key = _candidate_source_scroll_checkpoint_key(settings, source_key)
        requested_scroll_top = (
            scroll_positions.get(scroll_checkpoint_key, scroll_positions.get(source_key, 0)) if isinstance(scroll_positions, dict)
            else settings.get("_candidatePoolScrollPosition", 0) if isinstance(settings, dict) else 0
        )
        try:
            requested_scroll_top = max(0, min(100_000_000, int(float(requested_scroll_top or 0))))
        except (TypeError, ValueError, OverflowError):
            requested_scroll_top = 0
        if requested_scroll_top:
            source_page.evaluate("""({ selector, top }) => {
              const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
              const candidates = new Set([document.scrollingElement]);
              for (const node of nodes) {
                for (let current = node, depth = 0; current && depth < 8; current = current.parentElement, depth++)
                  candidates.add(current);
              }
              const target = [...candidates].filter((element) => element && element.clientHeight > 50 &&
                element.scrollHeight > element.clientHeight + 20).sort((a, b) => a.clientHeight - b.clientHeight)[0];
              if (!target) return 0;
              const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
              let resumeTop = Math.min(top, maxTop);
              if (resumeTop >= maxTop - 2) resumeTop = Math.max(0, maxTop - 2);
              target.scrollTop = resumeTop;
              try { target.dispatchEvent(new Event('scroll', { bubbles: true })); } catch (_) {}
              return target.scrollTop;
            }""", {"selector": candidate_selector, "top": requested_scroll_top})
            source_page.wait_for_timeout(550)
        snapshot = source_page.evaluate(snapshot_script, snapshot_args)
        if rate_limit_errors:
            raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求")
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"百应{label}页面未返回可识别的列表状态")
        snapshot["listContainerSelector"] = list_container_selector
        snapshot["candidateSelector"] = candidate_selector
        snapshot["readinessWaitMs"] = readiness_wait_ms
        ids = {str(value).strip() for value in snapshot.get("ids", []) if str(value).strip()}
        raw_rows = snapshot.get("rows") if isinstance(snapshot.get("rows"), list) else []
        product_rows = [
            dict(row) for row in raw_rows
            if isinstance(row, dict) and str(row.get("product_id") or "").strip()
        ]
        quota_path_for_scroll = (settings.get("_quotaCategoryPath") or rank_selection.get("categoryIds")) if isinstance(settings, dict) else None
        if (ids and isinstance(quota_path_for_scroll, list) and quota_path_for_scroll and
                isinstance(selected_tab, dict) and selected_tab.get("status") == "mapped" and
                isinstance(selected_tab.get("categoryIds"), list) and selected_tab["categoryIds"] and
                _category_id_matches(str(selected_tab["categoryIds"][0]), str(quota_path_for_scroll[0]))):
            rows_by_id = {str(row["product_id"]): row for row in product_rows}
            scroll_reads: list[Dict[str, Any]] = []
            no_new = 0
            try:
                scroll_limit = max(1, min(120, int(settings.get("_candidatePoolScrollSteps", 6))))
            except (TypeError, ValueError):
                scroll_limit = 6
            scroll_limit_reached = False
            for _ in range(scroll_limit):
                movement = source_page.evaluate("""({ selector }) => {
                  const nodes = Array.from(document.querySelectorAll(selector));
                  const candidates = new Set([document.scrollingElement]);
                  for (const node of nodes) {
                    for (let current = node, depth = 0; current && depth < 8; current = current.parentElement, depth++)
                      candidates.add(current);
                  }
                  const scrollables = [...candidates].filter(node => node &&
                    node.scrollHeight > node.clientHeight + 20 && node.clientHeight > 50);
                  const target = scrollables.sort((a, b) => a.clientHeight - b.clientHeight)[0];
                  if (!target) return { moved: false, position: 0, atEnd: true };
                  const before = target.scrollTop;
                  target.scrollTop = Math.min(target.scrollHeight, before + Math.max(300, target.clientHeight * 0.8));
                  return { moved: target.scrollTop > before, position: target.scrollTop,
                    atEnd: target.scrollTop >= target.scrollHeight - target.clientHeight - 2 };
                }""", {"selector": list_container_selector})
                if not isinstance(movement, dict) or not movement.get("moved"):
                    break
                source_page.wait_for_timeout(550)
                current_tabs = _source_category_tabs(source_page, source_key, category_tree, rank_root_catalog)
                if selected_tab and selected_tab.get("index") != -1 and not any(tab["categoryId"] == selected_tab["categoryId"] and tab["selected"] for tab in current_tabs):
                    raise RecoverablePrioritySourceReadError(f"{label}滚动后分类选中状态变化，停止计入品类配额")
                next_snapshot = source_page.evaluate(snapshot_script, snapshot_args)
                if rate_limit_errors:
                    raise RuntimeError(rate_limit_errors[-1] + "；已停止当前账号后续平台请求")
                next_rows = next_snapshot.get("rows") if isinstance(next_snapshot, dict) else []
                before_count = len(rows_by_id)
                for row in next_rows if isinstance(next_rows, list) else []:
                    if isinstance(row, dict) and str(row.get("product_id") or "").isdigit():
                        rows_by_id.setdefault(str(row["product_id"]), dict(row))
                new_count = len(rows_by_id) - before_count
                scroll_reads.append({"position": movement["position"], "newProductIds": new_count})
                no_new = 0 if new_count else no_new + 1
                if no_new >= 2:
                    break
            else:
                scroll_limit_reached = True
            product_rows = list(rows_by_id.values())
            ids.update(rows_by_id)
            snapshot["quotaScrollReads"] = scroll_reads
            snapshot["quotaScrollLimitReached"] = scroll_limit_reached
            snapshot["scrollPosition"] = scroll_reads[-1]["position"] if scroll_reads else snapshot.get("scrollPosition")
        if source_key == "hotSpot":
            config = settings.get("sources") if isinstance(settings, dict) and isinstance(settings.get("sources"), dict) else {}
            min_exposure = config.get("hotSpotMinExposure")
            min_related = config.get("hotSpotMinRelatedProducts")
            if min_exposure is not None or min_related is not None:
                filtered_rows: list[Dict[str, Any]] = []
                for row in product_rows:
                    metrics = row.get("_topicMetrics") if isinstance(row.get("_topicMetrics"), dict) else {}
                    exposure = _source_count(metrics.get("exposure"))
                    related = _source_count(metrics.get("relatedProducts"))
                    if min_exposure is not None and (exposure is None or exposure < float(min_exposure) * 10_000):
                        continue
                    if min_related is not None and (related is None or related < float(min_related)):
                        continue
                    filtered_rows.append(row)
                snapshot["sourceFilterRejected"] = len(product_rows) - len(filtered_rows)
                product_rows = filtered_rows
                ids = {str(row["product_id"]) for row in product_rows}
        if source_key == "allianceRank" and rank_selection.get("rankPriceRange") != "不限":
            before_count = len(product_rows)
            product_rows = [row for row in product_rows if _rank_row_matches_price(row, rank_selection["rankPriceRange"])]
            snapshot["sourceFilterRejected"] = before_count - len(product_rows)
            ids = {str(row["product_id"]) for row in product_rows}
        if selected_tab and selected_tab.get("status") == "mapped":
            selection_confirmed = category_tab is not None or (
                source_key == "allianceRank" and bool(rank_selection.get("categoryIds"))
            )
            for row in product_rows:
                row["_sourceCategory"] = {
                    "name": selected_tab["name"], "id": selected_tab["categoryId"],
                    "categoryIds": selected_tab["categoryIds"], "level": selected_tab["level"],
                    "source": source_key, "selected": True,
                    "selectionConfirmed": selection_confirmed,
                }
        snapshot["sourceCategoryTabs"] = tabs
        snapshot["sourceCategoryReadError"] = tab_read_error
        snapshot["selectedSourceCategory"] = selected_tab
        snapshot["rankSelection"] = rank_selection
        snapshot["readAt"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        complete_category_path_count = 0
        for row in product_rows:
            base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
            product_info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
            category = product_info.get("category") if isinstance(product_info.get("category"), dict) else {}
            if all(category.get(key) is not None and str(category.get(key)).strip() for key in ("first", "second", "third")):
                complete_category_path_count += 1
        snapshot["recognizedProductIdCount"] = len(ids)
        snapshot["completeCategoryPathProductCount"] = complete_category_path_count
        snapshot["missingCategoryPathProductCount"] = max(0, len(product_rows) - complete_category_path_count)
        if ids:
            recognized_detail = {
                "来源结果": "① 滚动区出现商品",
                "候选节点": int(snapshot.get("visibleProductNodeCount", 0) or 0),
                "滚动区商品节点": int(snapshot.get("visibleProductNodeCount", 0) or 0),
                "商品ID": len(ids),
                "商品ID前3个": list(snapshot.get("ids", []))[:3],
                "解析后候选商品": len(product_rows),
                "含完整三级类目路径商品": complete_category_path_count,
                "缺少完整三级类目路径商品": max(0, len(product_rows) - complete_category_path_count),
                "类目字段样例": snapshot.get("categoryFieldSamples", [])[:5],
                "document.readyState": str(snapshot.get("readyState") or "unknown"),
                "列表容器匹配节点": int(snapshot.get("listContainerMatchCount", 0) or 0),
                "可见列表容器": int(snapshot.get("listContainerVisibleCount", 0) or 0),
                "列表容器子节点数": snapshot.get("listContainerChildCounts", [])[:24],
                "实际等待毫秒": readiness_wait_ms,
            }
            if product_rows and complete_category_path_count == 0:
                recognized_detail["类目缺失诊断"] = "仅记录数值类目字段样例；详情来源诊断写入类目补读报告"
            return ids, product_rows, {
                "candidateNodeCount": int(snapshot.get("visibleProductNodeCount", 0) or 0),
                "productNodeCount": int(snapshot.get("visibleProductNodeCount", 0) or 0),
                "firstProductIds": list(snapshot.get("ids", []))[:3],
                "sourceOutcome": "① 滚动区出现商品",
                "sourceCategoryTabs": tabs,
                "sourceCategoryReadError": tab_read_error,
                "selectedSourceCategory": selected_tab,
                "quotaScrollReads": snapshot.get("quotaScrollReads", []),
                "quotaScrollLimitReached": snapshot.get("quotaScrollLimitReached") is True,
                "scrollPosition": snapshot.get("scrollPosition"),
                "readAt": snapshot.get("readAt"),
                "pagePath": snapshot.get("pagePath"),
                "detail": json.dumps(recognized_detail, ensure_ascii=False),
            }
        evidence = _save_priority_source_dom_evidence(source_key, source_page, snapshot, readiness_reached)
        evidence_detail = {
            "来源结果": "",
            "URL": str(snapshot.get("pageUrl") or "")[:300],
            "标题": str(snapshot.get("pageTitle") or "")[:120],
            "document.readyState": str(snapshot.get("readyState") or "unknown"),
            "候选选择器匹配节点": int(snapshot.get("rootMatchCount", 0) or 0),
            "列表容器选择器": list_container_selector,
            "可见候选节点": int(snapshot.get("visibleRowCount", 0) or 0),
            "候选节点子节点数": snapshot.get("rootChildCounts", [])[:12],
            "列表容器匹配节点": int(snapshot.get("listContainerMatchCount", 0) or 0),
            "可见列表容器": int(snapshot.get("listContainerVisibleCount", 0) or 0),
            "导航菜单误匹配数（已排除）": int(snapshot.get("navigationContainerMatchCount", 0) or 0),
            "有子节点的列表容器": int(snapshot.get("listContainersWithChildren", 0) or 0),
            "列表容器子节点数": snapshot.get("listContainerChildCounts", [])[:24],
            "列表容器样例": snapshot.get("listContainerSamples", [])[:12],
            "内容区选择器": str(snapshot.get("contentRootSelector") or BAIYING_PRIORITY_CONTENT_ROOT_SELECTOR),
            "可见内容区": int(snapshot.get("contentRootVisibleCount", 0) or 0),
            "内容区样例": snapshot.get("contentRootSamples", [])[:8],
            "可见加载指示器": int(snapshot.get("loadingIndicatorVisibleCount", 0) or 0),
            "加载文本仍可见": bool(snapshot.get("loadingTextDetected")),
            "加载指示器样例": snapshot.get("loadingIndicatorSamples", [])[:8],
            "body文本长度": int(snapshot.get("bodyTextLength", 0) or 0),
            "商品ID": 0,
            "等待真实列表或明确空状态": readiness_reached,
            "等待上限毫秒": BAIYING_PRIORITY_READY_TIMEOUT_MS,
            "实际等待毫秒": readiness_wait_ms,
            "明确空状态": bool(snapshot.get("emptyState")),
            "DOM证据JSON": evidence.get("jsonPath", "未能保存"),
            "DOM证据HTML": evidence.get("htmlPath", "未能保存"),
            "滚动区DOM文件": evidence.get("scrollContainerHtmlPath", "未找到滚动区容器"),
        }
        evidence["detail"] = json.dumps(evidence_detail, ensure_ascii=False)
        containers_visible = int(snapshot.get("listContainerVisibleCount", 0) or 0)
        containers_with_children = int(snapshot.get("listContainersWithChildren", 0) or 0)
        product_node_count = int(snapshot.get("visibleProductNodeCount", 0) or 0)
        loading_visible = int(snapshot.get("loadingIndicatorVisibleCount", 0) or 0) > 0
        loading_text_detected = bool(snapshot.get("loadingTextDetected"))
        still_loading = loading_visible or loading_text_detected
        timed_out_with_empty_container = (
            not readiness_reached
            and readiness_wait_ms >= BAIYING_PRIORITY_READY_TIMEOUT_MS - 1_000
            and containers_visible > 0
            and product_node_count == 0
            and (containers_with_children == 0 or snapshot.get("emptyState") is True)
        )
        explicit_empty_without_products = (
            bool(snapshot.get("emptyState")) and containers_visible > 0 and product_node_count == 0
        )
        if explicit_empty_without_products or timed_out_with_empty_container:
            evidence_detail["来源结果"] = (
                "② 页面未渲染商品：等满 60 秒后实际滚动区仍为空；页面同时显示明确空状态"
                + ("，加载指示器仍可见" if still_loading else "")
                if timed_out_with_empty_container and snapshot.get("emptyState") is True
                else "② 页面未渲染商品：等满 60 秒后实际滚动区仍为空"
                + ("，加载指示器仍可见" if still_loading else "")
                if timed_out_with_empty_container
                else "② 页面未渲染商品：百应页面显示明确空状态，滚动区没有商品节点"
            )
            evidence_detail["页面列表判定"] = evidence_detail["来源结果"]
            evidence["pageListDiagnosis"] = evidence_detail["页面列表判定"]
            evidence["sourceOutcome"] = evidence_detail["来源结果"]
            evidence["scrollContainerHtmlPath"] = evidence.get("scrollContainerHtmlPath", "")
            evidence["candidateNodeCount"] = product_node_count
            evidence["detail"] = json.dumps(evidence_detail, ensure_ascii=False)
            # 只有实际滚动区存在且确认没有商品节点，才归为“页面未渲染商品”。
            return set(), [], evidence
        root_samples = [str(value)[:100] for value in snapshot.get("rootClassSamples", []) if str(value).strip()]
        class_hints = [str(value)[:100] for value in snapshot.get("classHints", []) if str(value).strip()]
        page_list_diagnosis = (
            f"③ 滚动区有 {product_node_count} 个商品节点但没有解析出商品 ID；需依据已保存 DOM 检查解析器/结构"
            if product_node_count > 0
            else f"③ 滚动区容器有 {containers_with_children} 个非空节点但没有识别到商品节点/ID；需依据已保存 DOM 检查页面结构/解析器"
            if containers_with_children > 0
            else f"③ 等满 {BAIYING_PRIORITY_READY_TIMEOUT_MS // 1000} 秒后加载指示器仍可见；已保存 DOM，无法判定为空"
            if still_loading
            else "③ 未找到实际滚动区容器；可能是页面结构变化，已保存当前 DOM"
        )
        evidence_detail["来源结果"] = page_list_diagnosis
        evidence_detail["页面列表判定"] = page_list_diagnosis
        evidence["pageListDiagnosis"] = page_list_diagnosis
        evidence["sourceOutcome"] = page_list_diagnosis
        evidence["scrollContainerHtmlPath"] = evidence.get("scrollContainerHtmlPath", "")
        evidence["detail"] = json.dumps(evidence_detail, ensure_ascii=False)
        diagnostic = {
            "路径": str(snapshot.get("pagePath", ""))[:120],
            "标题": str(snapshot.get("pageTitle", ""))[:80],
            "document.readyState": str(snapshot.get("readyState", "unknown")),
            "等待上限毫秒": BAIYING_PRIORITY_READY_TIMEOUT_MS,
            "候选选择器匹配节点": int(snapshot.get("rootMatchCount", 0) or 0),
            "可见候选节点": int(snapshot.get("visibleRowCount", 0) or 0),
            "滚动区商品节点": product_node_count,
            "候选节点子节点数": snapshot.get("rootChildCounts", [])[:8],
            "列表容器匹配节点": int(snapshot.get("listContainerMatchCount", 0) or 0),
            "可见列表容器": containers_visible,
            "导航菜单误匹配数（已排除）": int(snapshot.get("navigationContainerMatchCount", 0) or 0),
            "有子节点的列表容器": containers_with_children,
            "列表容器子节点数": snapshot.get("listContainerChildCounts", [])[:24],
            "列表容器样例": snapshot.get("listContainerSamples", [])[:8],
            "可见内容区": int(snapshot.get("contentRootVisibleCount", 0) or 0),
            "内容区样例": snapshot.get("contentRootSamples", [])[:8],
            "可见加载指示器": int(snapshot.get("loadingIndicatorVisibleCount", 0) or 0),
            "加载文本仍可见": loading_text_detected,
            "加载指示器样例": snapshot.get("loadingIndicatorSamples", [])[:8],
            "商品ID": len(ids),
            "已等待真实列表或明确空状态": readiness_reached,
            "实际等待毫秒": readiness_wait_ms,
            "列表节点样例": root_samples[:3],
            "商品节点样例": class_hints[:3],
            "明确空状态": bool(snapshot.get("emptyState")),
            "页面列表判定": page_list_diagnosis,
            "DOM证据JSON": evidence.get("jsonPath", "未能保存"),
            "DOM证据HTML": evidence.get("htmlPath", "未能保存"),
        }
        raise RecoverablePrioritySourceReadError(
            f"百应{label}来源页面等待后仍未取得商品 ID；{diagnostic['页面列表判定']}；DOM证据 JSON：{evidence.get('jsonPath', '未能保存')}；HTML：{evidence.get('htmlPath', '未能保存')}；诊断："
            f"{json.dumps(diagnostic, ensure_ascii=False)}"
        )
    finally:
        if source_page is not None:
            try:
                source_page.close()
            except Exception:
                pass


def _collect_priority_source_ids(
    page: Page,
    settings: Dict[str, Any],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[dict[str, set[str]], list[str], list[Dict[str, Any]], dict[str, list[Dict[str, Any]]]]:
    """逐个读取已启用的优先来源；空来源静默略过，安全异常仍遵循 A-53 停批。"""
    config = settings.get("sources") if isinstance(settings.get("sources"), dict) else {}
    enabled = {
        "hotSpot": config.get("hotSpotEnabled", True) is not False,
        "allianceRank": config.get("allianceRankEnabled", True) is not False,
    }
    raw_priority = config.get("priority") if isinstance(config.get("priority"), list) else []
    priority = [str(value) for value in raw_priority if str(value) in BAIYING_PRIORITY_SOURCES]
    for source_key in BAIYING_PRIORITY_SOURCES:
        if source_key not in priority:
            priority.append(source_key)
    selected_sources = [key for key in priority if enabled.get(key, False)]
    collector_source = str(settings.get("_collectorSourceKey") or "")
    if collector_source:
        selected_sources = [key for key in selected_sources if key == collector_source]
    membership: dict[str, set[str]] = {}
    warnings: list[str] = []
    source_results: list[Dict[str, Any]] = []
    source_candidates: dict[str, list[Dict[str, Any]]] = {}
    category_tree = settings.get("categoryTree", []) if isinstance(settings.get("categoryTree"), list) else []
    raw_tab_offsets = settings.get("_sourceCategoryTabOffsets")
    tab_offsets = raw_tab_offsets if isinstance(raw_tab_offsets, dict) else {}
    try:
        target_count = max(1, min(10, int(settings.get("candidateLimit", 5))))
    except (TypeError, ValueError):
        target_count = 5
    tab_read_budget = min(8, max(3, target_count * 2))
    if settings.get("_candidatePoolCollectOnly") is True and settings.get("_quotaCategoryPath") == []:
        try:
            source_target = max(1, min(2_000, int(settings.get("_candidatePoolLimit") or target_count)))
        except (TypeError, ValueError):
            source_target = target_count
        # 来源级配额按允许的一级/二级路径补池；单次仍有界，避免长时间占用浏览器桥接。
        tab_read_budget = min(8, max(tab_read_budget, (source_target + 24) // 25))
    for index, source_key in enumerate(selected_sources, start=1):
        label = BAIYING_PRIORITY_SOURCES[source_key][0]
        _report_operation_progress(
            progress,
            "读取优先选品来源",
            index=index,
            total=len(selected_sources),
            percent=int((index - 1) * 100 / max(1, len(selected_sources))),
            message=f"正在读取{label}当前页；不会触发搜索、加车或发布",
        )
        try:
            # 榜单按一级或允许的二级分支逐项查询；明确的正向配额保留指定深度。
            rank_category_path = config.get("rankCategoryPath") if isinstance(config.get("rankCategoryPath"), list) else []
            quota_path_for_source = settings.get("_quotaCategoryPath")
            if source_key == "allianceRank":
                requested_rank_path = quota_path_for_source or rank_category_path
                rank_paths = (
                    _allowed_paths_for_requested_category(settings, requested_rank_path)
                    if isinstance(requested_rank_path, list) and requested_rank_path else
                    _selection_square_allowed_category_paths(settings)
                )
                excluded_selection = settings.get("excludedCategorySelection") if isinstance(settings.get("excludedCategorySelection"), dict) else {}
                has_exclusions = any(
                    isinstance(excluded_selection.get(level), list) and excluded_selection.get(level)
                    for level in ("level1", "level2", "level3")
                )
                if not rank_paths:
                    raise RecoverablePrioritySourceReadError(
                        f"{label}没有可核实的允许类目路径；未查询总榜或放宽排除类目"
                    )
                try:
                    rank_offset = max(0, min(len(rank_paths), int(tab_offsets.get(source_key, 0))))
                except (TypeError, ValueError):
                    rank_offset = 0
                if rank_offset >= len(rank_paths):
                    rank_offset = 0
                rank_batch = rank_paths[rank_offset:rank_offset + tab_read_budget]
                if rank_batch:
                    rank_ids: set[str] = set()
                    rank_rows_by_product: Dict[str, Dict[str, Any]] = {}
                    rank_reads: list[Dict[str, Any]] = []
                    rank_read_warnings: list[str] = []
                    for category_path in rank_batch:
                        rank_settings = copy.deepcopy(settings)
                        rank_settings["_sourceCategoryPath"] = list(category_path)
                        try:
                            path_ids, path_rows, path_evidence = _read_priority_source_product_ids(
                                page, source_key, category_tree, settings=rank_settings
                            )
                            rank_ids.update(path_ids)
                            rank_reads.append({
                                "categoryIds": list(category_path),
                                "scrollKey": _candidate_source_scroll_checkpoint_key(rank_settings, source_key),
                                "scrollPosition": path_evidence.get("scrollPosition"),
                                "productCount": len(path_ids),
                                "readAt": path_evidence.get("readAt"),
                                "pagePath": path_evidence.get("pagePath"),
                            })
                            for row in path_rows:
                                product_id = str(row.get("product_id") or "").strip()
                                if not product_id:
                                    continue
                                source_category = row.get("_sourceCategory")
                                row["_sourceCategories"] = [source_category] if isinstance(source_category, dict) else []
                                if product_id in rank_rows_by_product:
                                    rank_rows_by_product[product_id] = _merge_candidate_source_rows(
                                        rank_rows_by_product[product_id], row
                                    )
                                else:
                                    rank_rows_by_product[product_id] = row
                        except Exception as error:
                            if _selection_error_requires_pause(error):
                                raise
                            rank_read_warnings.append(
                                f"{label}类目 {'/'.join(category_path)} 读取失败；已跳过该类目："
                                f"{_safe_diagnostic_text(error, 180)}"
                            )
                    product_ids = rank_ids
                    product_rows = list(rank_rows_by_product.values())
                    source_candidates[source_key] = product_rows
                    membership[source_key] = product_ids
                    warnings.extend(rank_read_warnings)
                    source_results.append({
                        "sourceKey": source_key, "label": label,
                        "status": "read" if product_ids else "empty",
                        "productCount": len(product_ids), "parsedCandidateCount": len(product_rows),
                        "categoryTabReads": rank_reads,
                        "categoryTabNextOffset": rank_offset + len(rank_batch),
                        "categoryTabTotal": len(rank_paths),
                        "categoryTabReadLimitReached": rank_offset + len(rank_batch) < len(rank_paths),
                        "sourceCategoryPaths": [list(path) for path in rank_batch],
                        "scrollPositions": {
                            str(item["scrollKey"]): top for item in rank_reads
                            if item.get("scrollKey") and (top := _scroll_position_top(item.get("scrollPosition"))) is not None
                        },
                        "productIdFingerprint": hashlib.sha256("|".join(sorted(product_ids)).encode("utf-8")).hexdigest(),
                        "readAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    })
                    continue
            product_ids, product_rows, evidence = _read_priority_source_product_ids(page, source_key, category_tree, settings=settings)
            excluded_selection = settings.get("excludedCategorySelection") if isinstance(settings.get("excludedCategorySelection"), dict) else {}
            if source_key == "hotSpot":
                hot_paths = _hotspot_allowed_root_paths(settings)
                if isinstance(quota_path_for_source, list) and quota_path_for_source:
                    if len(quota_path_for_source) != 1:
                        raise RecoverablePrioritySourceReadError("选品热点只支持一级类目配额；二级、三级配额须由其他来源采集")
                    hot_paths = [path for path in hot_paths if _category_id_matches(path[0], str(quota_path_for_source[0]))]
                tabs_for_paths = evidence.get("sourceCategoryTabs") if isinstance(evidence.get("sourceCategoryTabs"), list) else []
                root_tabs = {
                    str(tab.get("categoryIds", [""])[0]): tab
                    for tab in tabs_for_paths
                    if isinstance(tab, dict) and tab.get("status") in {"mapped", "request_pending"}
                    and isinstance(tab.get("categoryIds"), list) and len(tab["categoryIds"]) == 1
                }
                if not hot_paths:
                    raise RecoverablePrioritySourceReadError(
                        f"{label}没有可采集的允许一级类目；已跳过被排除的一级及含下级排除项的整个一级"
                    )
                visible_hot_paths = [path for path in hot_paths if path[0] in root_tabs]
                if len(visible_hot_paths) < len(hot_paths):
                    warnings.append(f"{label}有 {len(hot_paths) - len(visible_hot_paths)} 个允许的一级类目未在当前页面唯一定位，未使用全部页结果")
                if not visible_hot_paths:
                    raise RecoverablePrioritySourceReadError(
                        f"{label}当前一级分类控件没有可唯一定位的类目；未读取全部页"
                    )
                try:
                    hot_offset = max(0, min(len(visible_hot_paths), int(tab_offsets.get(source_key, 0))))
                except (TypeError, ValueError):
                    hot_offset = 0
                if hot_offset >= len(visible_hot_paths):
                    hot_offset = 0
                hot_batch = visible_hot_paths[hot_offset:hot_offset + tab_read_budget]
                hot_ids: set[str] = set()
                hot_rows_by_product: Dict[str, Dict[str, Any]] = {}
                hot_reads: list[Dict[str, Any]] = []
                for category_path in hot_batch:
                    category_tab = {
                        **root_tabs[category_path[0]],
                        "categoryIds": list(category_path), "level": 1,
                    }
                    path_settings = copy.deepcopy(settings)
                    path_settings["_sourceCategoryPath"] = list(category_path)
                    try:
                        path_ids, path_rows, path_evidence = _read_priority_source_product_ids(
                            page, source_key, category_tree, category_tab=category_tab, settings=path_settings
                        )
                        hot_ids.update(path_ids)
                        hot_reads.append({
                            "categoryIds": list(category_path), "productCount": len(path_ids),
                            "scrollKey": _candidate_source_scroll_checkpoint_key(path_settings, source_key),
                            "scrollPosition": path_evidence.get("scrollPosition"),
                            "readAt": path_evidence.get("readAt"), "pagePath": path_evidence.get("pagePath"),
                        })
                        for row in path_rows:
                            product_id = str(row.get("product_id") or "").strip()
                            if not product_id:
                                continue
                            source_category = row.get("_sourceCategory")
                            row["_sourceCategories"] = [source_category] if isinstance(source_category, dict) else []
                            hot_rows_by_product[product_id] = (
                                _merge_candidate_source_rows(hot_rows_by_product[product_id], row)
                                if product_id in hot_rows_by_product else row
                            )
                    except Exception as error:
                        if _selection_error_requires_pause(error):
                            raise
                        warnings.append(
                            f"{label}类目 {'/'.join(category_path)} 读取失败；已跳过该类目："
                            f"{_safe_diagnostic_text(error, 180)}"
                        )
                product_ids = hot_ids
                product_rows = list(hot_rows_by_product.values())
                source_candidates[source_key] = product_rows
                membership[source_key] = product_ids
                source_results.append({
                    "sourceKey": source_key, "label": label,
                    "status": "read" if product_ids else "empty",
                    "productCount": len(product_ids), "parsedCandidateCount": len(product_rows),
                    "categoryTabReads": hot_reads,
                    "categoryTabNextOffset": hot_offset + len(hot_batch),
                    "categoryTabTotal": len(visible_hot_paths),
                    "categoryTabReadLimitReached": hot_offset + len(hot_batch) < len(visible_hot_paths),
                    "sourceCategoryPaths": [list(path) for path in hot_batch],
                    "sourceCategoryTabs": [
                        {key: tab.get(key) for key in ("name", "categoryId", "selected", "categoryIds", "level", "status", "idSource")}
                        for tab in tabs_for_paths if isinstance(tab, dict)
                    ],
                    "scrollPositions": {
                        str(item["scrollKey"]): top for item in hot_reads
                        if item.get("scrollKey") and (top := _scroll_position_top(item.get("scrollPosition"))) is not None
                    },
                    "productIdFingerprint": hashlib.sha256("|".join(sorted(product_ids)).encode("utf-8")).hexdigest(),
                    "readAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                })
                continue
            quota_path = settings.get("_quotaCategoryPath")
            if source_key == "allianceRank" and not quota_path:
                quota_path = config.get("rankCategoryPath")
            if isinstance(quota_path, list) and quota_path:
                selected_category = evidence.get("selectedSourceCategory")
                if source_key == "allianceRank":
                    requested_ids = _rank_category_ids(quota_path)
                    selected_ids = selected_category.get("categoryIds") if isinstance(selected_category, dict) else []
                    if selected_ids != requested_ids:
                        raise RecoverablePrioritySourceReadError("带货商品榜分类 ID 未回读到设定路径；不计入该分类")
                    quota_tab = selected_category
                else:
                    tabs = evidence.get("sourceCategoryTabs") if isinstance(evidence.get("sourceCategoryTabs"), list) else []
                    matching_tabs = [tab for tab in tabs if isinstance(tab, dict) and tab.get("status") == "mapped"
                                     and isinstance(tab.get("categoryIds"), list) and tab["categoryIds"]
                                     and len(tab["categoryIds"]) <= len(quota_path)
                                     and all(_category_id_matches(str(tab["categoryIds"][level]), str(quota_path[level]))
                                             for level in range(len(tab["categoryIds"]))) ]
                    if matching_tabs:
                        deepest = max(len(tab["categoryIds"]) for tab in matching_tabs)
                        matching_tabs = [tab for tab in matching_tabs if len(tab["categoryIds"]) == deepest]
                    if len(matching_tabs) != 1:
                        raise RecoverablePrioritySourceReadError(
                            f"{label}无法唯一定位配额品类的稳定来源分类 ID；不计入该品类")
                    quota_tab = {
                        **matching_tabs[0],
                        # 先用已映射的上级控件进入分类，再要求页面继续选到
                        # 本条配额的实际二级/三级路径；只停在一级不能归入该配额。
                        "categoryIds": list(quota_path),
                        "level": len(quota_path),
                    }
                    if not isinstance(selected_category, dict) or selected_category.get("categoryId") != quota_tab.get("categoryId"):
                        product_ids, product_rows, evidence = _read_priority_source_product_ids(
                            page, source_key, category_tree, category_tab=quota_tab, settings=settings)
                for product_row in product_rows:
                    source_category = product_row.get("_sourceCategory")
                    product_row["_sourceCategories"] = [source_category] if isinstance(source_category, dict) else []
                evidence["categoryTabReads"] = [{"categoryId": quota_tab.get("categoryId"), "productCount": len(product_ids)}]
                evidence["categoryTabNextOffset"] = 1
                evidence["categoryTabTotal"] = 1
                # 配额模式只读取选中的分类，不混入“全部”页或其他分类商品。
                membership[source_key] = product_ids
                source_candidates[source_key] = product_rows
                source_results.append({
                    "sourceKey": source_key, "label": label, "status": "read" if product_ids else "empty",
                    "productCount": len(product_ids), "parsedCandidateCount": len(product_rows),
                    "categoryTabReads": evidence["categoryTabReads"], "categoryTabNextOffset": 1,
                    "categoryTabTotal": 1,
                    "sourceReadLimitReached": evidence.get("quotaScrollLimitReached") is True,
                    "rankSelection": evidence.get("rankSelection", {}),
                    "quotaScrollReads": evidence.get("quotaScrollReads", []),
                    "productIdFingerprint": hashlib.sha256("|".join(sorted(product_ids)).encode("utf-8")).hexdigest(),
                    "readAt": evidence.get("readAt", ""), "pagePath": evidence.get("pagePath", ""),
                    "scrollPosition": evidence.get("scrollPosition"),
                    "scrollPositions": {
                        _candidate_source_scroll_checkpoint_key(settings, source_key): top
                    } if (top := _scroll_position_top(evidence.get("scrollPosition"))) is not None else {},
                })
                continue
            tabs = evidence.get("sourceCategoryTabs") if isinstance(evidence.get("sourceCategoryTabs"), list) else []
            excluded_selection = settings.get("excludedCategorySelection") if isinstance(settings.get("excludedCategorySelection"), dict) else {}
            def excluded_tab(tab: Dict[str, Any]) -> bool:
                path = tab.get("categoryIds")
                if not isinstance(path, list) or not path:
                    return False
                for level, key in enumerate(("level1", "level2", "level3"), start=1):
                    excluded_ids = [
                        _category_id_scalar(value)
                        for value in (excluded_selection.get(key) or [])
                        if _category_id_scalar(value)
                    ]
                    if not excluded_ids:
                        continue
                    if len(path) < level:
                        return True
                    if any(_category_id_matches(str(path[level - 1]), value) for value in excluded_ids):
                        return True
                return False

            mapped_tabs = [tab for tab in tabs if isinstance(tab, dict) and tab.get("status") == "mapped"
                           and tab.get("selected") is not True and not excluded_tab(tab)]
            category_tabs_available = any(
                isinstance(tab, dict) and tab.get("status") == "mapped" and tab.get("selected") is not True
                for tab in tabs
            )
            if category_tabs_available and not mapped_tabs:
                warnings.append(
                    f"{label}当前分类控件无法提供满足排除类目深度的可选路径；"
                    "未读取全部页商品，等待更深层类目控件或后续补池。"
                )
            # 只要来源页面能回读稳定的分类 ID，就丢弃“全部”页的原始行，
            # 后续候选只来自逐个点击的允许分类；否则热点/榜单会先把排除类目
            # 混入池中，再依赖详情阶段补救，和选品广场的顺序不一致。
            # “全部”页不能证明商品属于哪一个已允许的分类。即使商品行带有
            # 类目字段，也须先由页面筛选确认来源路径，才可计入候选池。
            product_ids = set()
            by_product: Dict[str, Dict[str, Any]] = {}
            if not category_tabs_available:
                warnings.append(f"{label}未回读到稳定的分类 ID；全部页商品不计入候选池")
            try:
                tab_offset = max(0, min(len(mapped_tabs), int(tab_offsets.get(source_key, 0))))
            except (TypeError, ValueError):
                tab_offset = 0
            if tab_offset >= len(mapped_tabs):
                tab_offset = 0
            tab_batch = mapped_tabs[tab_offset:tab_offset + tab_read_budget]
            tab_reads: list[Dict[str, Any]] = []
            for tab in tab_batch:
                try:
                    tab_settings = copy.deepcopy(settings)
                    tab_settings["_sourceCategoryPath"] = list(tab.get("categoryIds", []))
                    tab_ids, tab_rows, tab_evidence = _read_priority_source_product_ids(
                        page, source_key, category_tree, category_tab=tab, settings=tab_settings
                    )
                    product_ids.update(tab_ids)
                    tab_reads.append({"categoryId": tab.get("categoryId"), "productCount": len(tab_ids),
                                      "scrollKey": _candidate_source_scroll_checkpoint_key(
                                          {**settings, "_sourceCategoryPath": tab.get("categoryIds", [])}, source_key),
                                      "readAt": tab_evidence.get("readAt"), "pagePath": tab_evidence.get("pagePath"),
                                      "scrollPosition": tab_evidence.get("scrollPosition")})
                    for row in tab_rows:
                        product_id = str(row.get("product_id") or "").strip()
                        if not product_id:
                            continue
                        source_category = row.get("_sourceCategory")
                        if product_id not in by_product:
                            by_product[product_id] = row
                            row["_sourceCategories"] = [source_category] if isinstance(source_category, dict) else []
                        else:
                            existing = by_product[product_id]
                            categories = existing.get("_sourceCategories") if isinstance(existing.get("_sourceCategories"), list) else []
                            if isinstance(source_category, dict) and source_category not in categories:
                                categories.append(source_category)
                            merged = _merge_candidate_source_rows(existing, row)
                            merged["_sourceCategories"] = categories
                            by_product[product_id] = merged
                except Exception as error:
                    if _selection_error_requires_pause(error):
                        raise
                    warnings.append(f"{label}分类 {tab.get('name') or tab.get('categoryId')} 读取失败；其商品不使用来源级类目：{_safe_diagnostic_text(error, 180)}")
            product_rows = list(by_product.values())
            evidence["categoryTabReads"] = tab_reads
            evidence["unmappedCategoryTabs"] = sum(1 for tab in tabs if isinstance(tab, dict) and tab.get("status") != "mapped")
            evidence["categoryTabNextOffset"] = tab_offset + len(tab_batch)
            evidence["categoryTabTotal"] = len(mapped_tabs)
            evidence["categoryTabReadLimitReached"] = evidence["categoryTabNextOffset"] < len(mapped_tabs)
            evidence["productIdFingerprint"] = hashlib.sha256("|".join(sorted(product_ids)).encode("utf-8")).hexdigest()
            if evidence["categoryTabReadLimitReached"]:
                warnings.append(f"{label}本轮读取 {len(tab_batch)} 个分类，剩余 {len(mapped_tabs) - evidence['categoryTabNextOffset']} 个按检查点续读")
            membership[source_key] = product_ids
            source_candidates[source_key] = product_rows
            source_results.append({
                "sourceKey": source_key,
                "label": label,
                "status": "read" if product_ids else "empty",
                "productCount": len(product_ids),
                "parsedCandidateCount": len(product_rows),
                "candidateNodeCount": int(evidence.get("candidateNodeCount", 0) or 0),
                "productNodeCount": int(evidence.get("productNodeCount", 0) or 0),
                "firstProductIds": evidence.get("firstProductIds", [])[:3],
                "sourceOutcome": str(evidence.get("sourceOutcome") or ""),
                "pageListDiagnosis": str(evidence.get("pageListDiagnosis") or ""),
                "detail": str(evidence.get("detail") or ""),
                "diagnosticId": str(evidence.get("diagnosticId") or ""),
                "categoryTabReads": evidence.get("categoryTabReads", []),
                "categoryTabNextOffset": evidence.get("categoryTabNextOffset", 0),
                "categoryTabTotal": evidence.get("categoryTabTotal", 0),
                "sourceReadLimitReached": (
                    evidence.get("categoryTabReadLimitReached") is True or
                    evidence.get("quotaScrollLimitReached") is True
                ),
                "scrollPositions": {
                    str(item["scrollKey"]): top for item in tab_reads
                    if item.get("scrollKey") and (top := _scroll_position_top(item.get("scrollPosition"))) is not None
                },
                "sourceCategoryTabs": [
                    {key: tab.get(key) for key in ("name", "categoryId", "selected", "categoryIds", "level", "status")}
                    for tab in tabs if isinstance(tab, dict)
                ],
                "unmappedCategoryTabs": evidence.get("unmappedCategoryTabs", 0),
                "rankSelection": evidence.get("rankSelection", {}),
                "sourceFilterRejected": evidence.get("sourceFilterRejected", 0),
                "productIdFingerprint": evidence.get("productIdFingerprint", ""),
                "readAt": evidence.get("readAt", ""),
                "pagePath": evidence.get("pagePath", ""),
                "scrollPosition": evidence.get("scrollPosition"),
            })
        except RecoverablePrioritySourceReadError as error:
            detail = _safe_diagnostic_text(error, 2_400)
            warnings.append(f"跳过来源读取：{label}页面读取失败，已继续处理其余来源；原因：{detail}")
            membership[source_key] = set()
            source_candidates[source_key] = []
            source_results.append({
                "sourceKey": source_key,
                "label": label,
                "status": "skipped",
                "productCount": 0,
                "detail": detail,
            })
        except Exception as error:
            if _selection_error_requires_pause(error):
                raise RuntimeError(f"读取{label}来源时遇到账号/风控/连接异常，已暂停当前账号：{error}") from error
            detail = _safe_diagnostic_text(error, 2_400)
            warnings.append(f"跳过来源读取：{label}页面读取失败，已继续处理其余来源；原因：{detail}")
            membership[source_key] = set()
            source_candidates[source_key] = []
            source_results.append({
                "sourceKey": source_key,
                "label": label,
                "status": "skipped",
                "productCount": 0,
                "detail": detail,
            })
        current_result = source_results[-1]
        _report_operation_progress(
            progress,
            "优先来源读取完成",
            index=index,
            total=len(selected_sources),
            percent=int(index * 100 / max(1, len(selected_sources))),
            message=(
                f"{current_result.get('sourceOutcome') or '① 滚动区出现商品'}；滚动区商品节点 {current_result.get('productNodeCount', 0)}，商品 ID {current_result['productCount']}；前3个 ID：{', '.join(current_result.get('firstProductIds', [])) or '无'}；解析 {current_result.get('parsedCandidateCount', 0)} 个候选"
                if current_result["status"] == "read"
                else f"{label}{current_result.get('pageListDiagnosis') or '页面列表为空'}"
                if current_result["status"] == "empty"
                else f"{label}页面 DOM 已保存但未解析出商品 ID；{current_result.get('detail', '')}"
            ),
        )
    return membership, warnings, source_results, source_candidates


def _attach_priority_source_results(
    result: Dict[str, Any],
    source_results: Sequence[Dict[str, Any]],
    match_status: str = "complete",
) -> Dict[str, Any]:
    """补充每个来源在本次选品结果中的 ID 匹配数，便于过程日志区分读取与匹配。"""
    rows = list(result.get("candidates", [])) + list(result.get("rejectedCandidates", []))
    for source_result in source_results:
        source_key = str(source_result.get("sourceKey") or "")
        matched_ids = {
            str(row.get("productId") or "")
            for row in rows
            if isinstance(row, dict)
            and source_key in (row.get("sources") if isinstance(row.get("sources"), list) else [])
            and str(row.get("productId") or "")
        }
        source_result["matchedCandidateCount"] = len(matched_ids) if match_status != "unavailable" else None
    result["prioritySourceResults"] = [dict(item) for item in source_results]
    result["prioritySourceMatchStatus"] = match_status
    return result


def _manual_category_selection_ids(settings: Dict[str, Any]) -> set[str]:
    selection = settings.get("categorySelection")
    if not isinstance(selection, dict):
        return set()
    return {
        str(value).strip()
        for level in ("level1", "level2", "level3")
        for value in (selection.get(level, []) if isinstance(selection.get(level, []), list) else [])
        if str(value).strip()
    }


def _candidate_matches_manual_categories(candidate: Dict[str, Any], selected_ids: set[str]) -> bool:
    candidate_ids = candidate.get("categoryIds")
    if not isinstance(candidate_ids, list):
        candidate_ids = []
    source_ids = candidate.get("sourceCategoryIds")
    if isinstance(source_ids, list) and candidate.get("categoryStatus") in {"source_level", "known"}:
        candidate_ids = [*candidate_ids, *source_ids]
    selected_aliases = {value.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1] for value in selected_ids}
    candidate_aliases = {
        str(value).strip().rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]
        for value in candidate_ids if str(value).strip()
    }
    return bool(selected_ids.intersection(str(value).strip() for value in candidate_ids if str(value).strip()) or
                selected_aliases.intersection(candidate_aliases))


def _verified_source_category_paths(categories: Any) -> list[list[str]]:
    """仅保留已选中且与本轮来源筛选结果关联的稳定分类路径。"""
    paths: list[list[str]] = []
    for item in (categories if isinstance(categories, list) else []):
        if not isinstance(item, dict) or item.get("selected") is not True or item.get("selectionConfirmed") is not True:
            continue
        raw_path = item.get("categoryIds")
        if not isinstance(raw_path, list) or not 1 <= len(raw_path) <= 3:
            continue
        path = [_category_id_scalar(value) for value in raw_path]
        if all(path) and path not in paths:
            paths.append(path)
    return paths


def _source_category_path_satisfies_settings(path: list[str], settings: Dict[str, Any]) -> bool:
    """页面筛选路径须覆盖本根类目的有效排除分支和正向配额。"""
    if not path:
        return False
    quota_path = settings.get("_quotaCategoryPath")
    if isinstance(quota_path, list) and quota_path:
        if len(path) < len(quota_path) or any(
            not _category_id_matches(path[index], str(expected))
            for index, expected in enumerate(quota_path)
        ):
            return False
    blocked_roots, blocked_seconds = _effective_excluded_category_branches(settings)
    if any(_category_id_matches(path[0], root_id) for root_id in blocked_roots):
        return False
    blocked_under_root = [second_id for root_id, second_id in blocked_seconds
                          if _category_id_matches(path[0], root_id)]
    if blocked_under_root and (len(path) < 2 or any(
        _category_id_matches(path[1], second_id) for second_id in blocked_under_root
    )):
        return False
    if settings.get("selectionMode") == "category" and settings.get("categoryMode") == "manual":
        selected = _manual_category_selection_ids(settings)
        if selected and not any(
            _category_id_matches(value, selected_id)
            for value in path for selected_id in selected
        ):
            return False
    return True


def _candidate_source_category_satisfies_general_exclusions(
    candidate: Dict[str, Any], settings: Dict[str, Any]
) -> bool:
    """消费账号沿用采集时确认的筛选路径，并应用自身类目条件。"""
    if candidate.get("categoryStatus") == "ambiguous":
        return False
    source_path = candidate.get("sourceCategoryIds")
    return isinstance(source_path, list) and _source_category_path_satisfies_settings(
        [_category_id_scalar(value) for value in source_path], settings
    )


def _excluded_category_selection_ids(settings: Dict[str, Any]) -> set[str]:
    selection = settings.get("excludedCategorySelection")
    if not isinstance(selection, dict):
        return set()
    return {
        str(value).strip()
        for level in ("level1", "level2", "level3")
        for value in (selection.get(level, []) if isinstance(selection.get(level, []), list) else [])
        if str(value).strip()
    }


def _category_name_path(category_tree: Any, leaf_category_id: str) -> str:
    wanted = str(leaf_category_id or "").strip()
    if not wanted or not isinstance(category_tree, list):
        return ""
    wanted_tail = wanted.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]
    matches: list[str] = []

    def walk(nodes: Any, path: list[tuple[str, str]]) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            category_id = str(node.get("id") or "").strip()
            name = str(node.get("name") or "").strip()
            next_path = path + ([(category_id, name)] if category_id else [])
            node_tail = category_id.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]
            if category_id == wanted or (node_tail == wanted_tail and not node.get("children")):
                labels = [label for _, label in next_path if label]
                if labels:
                    matches.append(" / ".join(labels))
            walk(node.get("children", []), next_path)

    walk(category_tree, [])
    return matches[0] if len(set(matches)) == 1 else ""


def _candidate_as_promotion_row(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """把已解析的选品广场行转换为三源统一解析器可消费的结构。"""
    metrics = candidate.get("dataMetrics") if isinstance(candidate.get("dataMetrics"), dict) else {}
    category_ids = [str(value) for value in candidate.get("categoryIds", []) if str(value)]
    category = {
        key: category_ids[index]
        for index, key in enumerate(("first", "second", "third"))
        if index < len(category_ids)
    }
    return {
        "product_id": str(candidate.get("productId") or ""),
        "promotion_id": str(candidate.get("commodityId") or ""),
        "title": str(candidate.get("title") or ""),
        "base_model": {
            "product_info": {
                "name": str(candidate.get("title") or ""),
                "month_sale": metrics.get("salesCount"),
                "sale_amount": metrics.get("salesAmount"),
                "conversion_rate": metrics.get("conversionRate"),
                "view_count": metrics.get("views"),
                "price": candidate.get("price"),
                "category": category,
                "sale_axis": candidate.get("saleAxis", []),
                "main_img": {"url_list": [str(candidate.get("coverUrl"))] if candidate.get("coverUrl") else []},
            },
            "promotion_info": {"cos_info": {"cos_ratio_text": metrics.get("commissionRate")}},
            "shop_info": {"score": metrics.get("experienceScore")},
            "marketing_info": {},
            "extend_info": {"is_in_cart": bool(candidate.get("inCart"))},
        },
    }


def _scroll_selection_square_rows(
    page: Page, settings: Dict[str, Any], known_ids: set[str], max_steps: int = 6,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]], bool]:
    """只滚动现有选品广场页面，收集实际产生的新 material_list 响应。"""
    collector = _MaterialListCollector(settings)
    rows: list[Dict[str, Any]] = []
    reads: list[Dict[str, Any]] = []
    unchanged = 0
    scroll_limit_reached = False
    _listen_to_material_list(page, collector)
    try:
        for step in range(1, max_steps + 1):
            mark = collector.mark()
            position = page.evaluate("""({ startTop }) => {
              const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' && style.visibility !== 'hidden';
              };
              const canScroll = (element) => element && visible(element) &&
                element.scrollHeight > element.clientHeight + 80;
              const cards = Array.from(document.querySelectorAll('[class*="__card___"]')).filter(visible);
              let target = null;
              let kind = '';
              for (const card of cards) {
                for (let node = card.parentElement, depth = 0; node && depth < 10; node = node.parentElement, depth += 1) {
                  if (canScroll(node)) { target = node; kind = 'card_ancestor'; break; }
                }
                if (target) break;
              }
              if (!target) {
                target = Array.from(document.querySelectorAll(
                  '[class*="infinite-scroller"], [class*="virtual-list"], [class*="scroll-container"], main'
                )).find(canScroll);
                if (target) kind = 'list_container';
              }
              if (!target && canScroll(document.scrollingElement)) {
                target = document.scrollingElement; kind = 'document';
              }
              if (!target) return { moved: false, found: false, top: 0, height: 0 };
              const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
              const requestedTop = Number(startTop);
              if (Number.isFinite(requestedTop) && requestedTop > 0) {
                let resumeTop = Math.min(requestedTop, maxTop);
                if (resumeTop >= maxTop - 2) resumeTop = Math.max(0, maxTop - 2);
                target.scrollTop = resumeTop;
              }
              const before = target.scrollTop;
              target.scrollTop = Math.min(maxTop,
                before + Math.max(250, Math.round(target.clientHeight * .8)));
              try { target.dispatchEvent(new Event('scroll', { bubbles: true })); } catch (_) {}
              return { moved: target.scrollTop > before, found: true, kind,
                top: target.scrollTop, height: target.scrollHeight,
                atEnd: target.scrollTop >= target.scrollHeight - target.clientHeight - 2 };
            }""", {"startTop": settings.get("_candidatePoolScrollPosition", 0) if step == 1 else 0})
            if not isinstance(position, dict) or not position.get("moved"):
                break
            page.wait_for_timeout(900)
            if collector.request_count_since(mark):
                state = _wait_for_material_list_settled_bounded(
                    page, collector, mark, window_seconds=4, max_windows=2, quiet_seconds=0.8
                )
                if collector.rate_limit_error_since(mark):
                    _mark_page_rate_limited(id(page))
                    raise RuntimeError("百应选品广场滚动时返回 11001，已停止当前账号后续请求")
                if state in {"pending", "unstable"}:
                    reads.append({"step": step, "scrollTop": position.get("top"), "status": state})
                    scroll_limit_reached = True
                    break
            if collector.rate_limit_error_since(mark):
                _mark_page_rate_limited(id(page))
                raise RuntimeError("百应选品广场滚动时返回 11001，已停止当前账号后续请求")
            new_ids: set[str] = set()
            response_new_ids = 0
            for sequence, payload in collector.successes:
                if sequence <= mark:
                    continue
                for row in _promotion_items(payload):
                    product_id = str(row.get("product_id") or "").strip()
                    if product_id and product_id not in known_ids:
                        known_ids.add(product_id)
                        new_ids.add(product_id)
                        response_new_ids += 1
                        row["_sourceReadPage"] = f"scroll:{step}"
                        rows.append(row)
            rendered = _rendered_material_payload(page)
            react_new_ids = 0
            for row in _promotion_items(rendered):
                product_id = str(row.get("product_id") or "").strip()
                if product_id and product_id not in known_ids:
                    known_ids.add(product_id)
                    new_ids.add(product_id)
                    react_new_ids += 1
                    row["_sourceReadPage"] = f"scroll:{step}"
                    row["_categorySourceHint"] = "react_card"
                    rows.append(row)
            reads.append({
                "step": step, "scrollTop": position.get("top"), "scrollHeight": position.get("height"),
                "containerKind": position.get("kind"), "atEnd": position.get("atEnd"),
                "readAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "requestCount": collector.request_count_since(mark), "newProductIds": len(new_ids),
                "responseNewProductIds": response_new_ids, "reactNewProductIds": react_new_ids,
            })
            unchanged = 0 if new_ids else unchanged + 1
            if unchanged >= 2:
                break
        else:
            scroll_limit_reached = True
    finally:
        _remove_material_list_listeners(page, collector)
    return rows, reads, scroll_limit_reached


def _effective_excluded_category_branches(
    settings: Dict[str, Any]
) -> tuple[set[str], set[tuple[str, str]]]:
    """三级排除上提至其二级父分支；不能靠名称或不唯一的末级 ID 猜测。"""
    tree = settings.get("categoryTree") if isinstance(settings.get("categoryTree"), list) else []
    excluded = settings.get("excludedCategorySelection") if isinstance(settings.get("excludedCategorySelection"), dict) else {}
    roots = {_category_id_scalar(value) for value in (excluded.get("level1") or []) if _category_id_scalar(value)}
    seconds: set[tuple[str, str]] = set()
    for level, values in ((2, excluded.get("level2") or []), (3, excluded.get("level3") or [])):
        for value in values:
            category_id = _category_id_scalar(value)
            if not category_id:
                continue
            matches: set[tuple[str, str]] = set()
            for root in tree:
                if not isinstance(root, dict):
                    continue
                for second in (root.get("children") if isinstance(root.get("children"), list) else []):
                    if not isinstance(second, dict):
                        continue
                    if level == 2:
                        found = _category_id_matches(_category_id_scalar(second.get("id")), category_id)
                    else:
                        found = any(
                            isinstance(third, dict) and
                            _category_id_matches(_category_id_scalar(third.get("id")), category_id)
                            for third in (second.get("children") if isinstance(second.get("children"), list) else [])
                        )
                    if found:
                        matches.add((_category_id_scalar(root.get("id")), _category_id_scalar(second.get("id"))))
            if len(matches) != 1:
                raise RuntimeError(f"排除的{level}级类目 ID {category_id} 无法在当前百应目录中唯一定位父分支")
            seconds.update(matches)
    return roots, seconds


def _selection_square_allowed_category_paths(settings: Dict[str, Any]) -> list[tuple[str, ...]]:
    """无下级排除时查一级；有下级排除时只查允许的二级兄弟分支。"""
    tree = settings.get("categoryTree") if isinstance(settings.get("categoryTree"), list) else []
    blocked_roots, blocked_seconds = _effective_excluded_category_branches(settings)
    paths: list[tuple[str, ...]] = []
    for root in tree:
        if not isinstance(root, dict):
            continue
        root_id = _category_id_scalar(root.get("id"))
        if not root_id or any(_category_id_matches(root_id, blocked) for blocked in blocked_roots):
            continue
        blocked_here = {second_id for first_id, second_id in blocked_seconds
                        if _category_id_matches(root_id, first_id)}
        if not blocked_here:
            paths.append((root_id,))
            continue
        for second in (root.get("children") if isinstance(root.get("children"), list) else []):
            if not isinstance(second, dict):
                continue
            second_id = _category_id_scalar(second.get("id"))
            if second_id and not any(_category_id_matches(second_id, blocked) for blocked in blocked_here):
                paths.append((root_id, second_id))
    return paths


def _hotspot_allowed_root_paths(settings: Dict[str, Any]) -> list[tuple[str, ...]]:
    """热点只有一级入口；含任何有效下级排除项的一级整支跳过。"""
    tree = settings.get("categoryTree") if isinstance(settings.get("categoryTree"), list) else []
    blocked_roots, blocked_seconds = _effective_excluded_category_branches(settings)
    paths: list[tuple[str, ...]] = []
    for root in tree:
        if not isinstance(root, dict):
            continue
        root_id = _category_id_scalar(root.get("id"))
        if not root_id or any(_category_id_matches(root_id, blocked) for blocked in blocked_roots):
            continue
        if any(_category_id_matches(root_id, first) for first, _ in blocked_seconds):
            continue
        paths.append((root_id,))
    return paths


def _allowed_paths_for_requested_category(
    settings: Dict[str, Any], requested: Sequence[Any]
) -> list[tuple[str, ...]]:
    """正向配额保持原深度；只有根配额含排除分支时才展开允许的二级。"""
    path = tuple(_category_id_scalar(value) for value in requested)
    if not 1 <= len(path) <= 3 or not all(path):
        return []
    blocked_roots, blocked_seconds = _effective_excluded_category_branches(settings)
    if any(_category_id_matches(path[0], blocked) for blocked in blocked_roots):
        return []
    if len(path) >= 2:
        if any(_category_id_matches(path[0], first) and _category_id_matches(path[1], second)
               for first, second in blocked_seconds):
            return []
        return [path]
    return [allowed for allowed in _selection_square_allowed_category_paths(settings)
            if _category_id_matches(allowed[0], path[0])]


def _collect_selection_square_rows(
    page: Page,
    settings: Dict[str, Any],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """读取选品广场筛选结果及实际滚动产生的新列表响应。"""
    label, url = BAIYING_SELECTION_SQUARE_SOURCE
    parsed_current = urlparse(page.url)
    parsed_target = urlparse(url)
    if parsed_current.hostname != parsed_target.hostname or parsed_current.path.rstrip("/") != parsed_target.path.rstrip("/"):
        page.goto(url, wait_until="commit", timeout=45_000)
    parsed_after = urlparse(page.url)
    if parsed_after.hostname != parsed_target.hostname or parsed_after.path.rstrip("/") != parsed_target.path.rstrip("/"):
        raise RuntimeError("百应选品广场页面身份不匹配；已停止三源候选采集。")
    prompt_kind, prompt_text = _visible_baiying_prompt(page)
    if prompt_kind:
        raise RuntimeError(f"百应选品广场存在待处理状态（{prompt_text or prompt_kind}）；已停止候选采集。")

    selection_mode = "category" if settings.get("selectionMode") == "category" else "general"
    category_mode = "manual" if settings.get("categoryMode") == "manual" else "auto"
    query_base = copy.deepcopy(settings)
    warnings: list[str] = []
    if isinstance(settings.get("_quotaCategoryPath"), list) and settings["_quotaCategoryPath"]:
        quota_path = [_category_id_scalar(value) for value in settings["_quotaCategoryPath"]]
        if not 1 <= len(quota_path) <= 3 or not all(quota_path):
            raise RuntimeError("选品广场配额类目路径缺少稳定 ID；未读取全部页商品")
        category_paths = _allowed_paths_for_requested_category(query_base, quota_path)
        if not category_paths:
            raise RuntimeError("选品广场配额类目没有可查询的允许下级类目；未使用全部页商品")
        queries = []
        for path in category_paths:
            query = copy.deepcopy(query_base)
            query["categorySelection"] = {
                level: [path[index]] if index < len(path) else []
                for index, level in enumerate(("level1", "level2", "level3"))
            }
            queries.append(query)
    elif selection_mode != "category" or category_mode != "manual":
        query_base["categorySelection"] = {"level1": [], "level2": [], "level3": []}
        # 通用模式也按百应页面的类目逐项读取。先由首个查询应用佣金、体验分、
        # 带货情况等非类目条件，后续查询只替换当前类目，避免“全部”页把被排除
        # 类目混入候选后再依赖商品详情补救。
        if not isinstance(query_base.get("categoryTree"), list) or not query_base.get("categoryTree"):
            # 旧批次设置可能没有随快照保存类目树；只从当前页面读取类目目录，
            # 不勾选、不搜索、不消费商品，避免无树时退回“全部”页绕过排除类目。
            try:
                live_catalog = collect_filter_catalog(page, include_category_tree=True)
                live_tree = live_catalog.get("categoryRoots") if isinstance(live_catalog, dict) else None
                if isinstance(live_tree, list) and live_tree:
                    query_base["categoryTree"] = live_tree
                    if not isinstance(query_base.get("filterCatalog"), dict):
                        query_base["filterCatalog"] = {
                            "filters": live_catalog.get("filters", {}),
                            "features": live_catalog.get("features", []),
                        }
            except Exception as error:
                warnings.append(f"百应类目目录读取失败，未执行通用模式全量查询：{_safe_diagnostic_text(error, 240)}")
        paths = _selection_square_allowed_category_paths(query_base)
        if not paths:
            raise RuntimeError("百应类目目录未能读取或没有可查询的允许类目；未使用全部页商品入池")
        queries = []
        for path in paths:
            query = copy.deepcopy(query_base)
            query["categorySelection"] = {
                level: [path[index]] if index < len(path) else []
                for index, level in enumerate(("level1", "level2", "level3"))
            }
            queries.append(query)
    else:
        queries = _category_query_settings(query_base)
    raw_category_offsets = settings.get("_sourceCategoryTabOffsets")
    saved_query_offset = 0
    if isinstance(raw_category_offsets, dict) and not (
        isinstance(settings.get("_quotaCategoryPath"), list) and settings.get("_quotaCategoryPath")
    ):
        try:
            saved_query_offset = max(0, min(len(queries), int(raw_category_offsets.get("selectionSquare", 0))))
        except (TypeError, ValueError):
            saved_query_offset = 0
        if saved_query_offset >= len(queries):
            saved_query_offset = 0
    query_budget = 3 if settings.get("_candidatePoolCollectOnly") is True and not settings.get("_quotaCategoryPath") else len(queries)
    query_batch = queries[saved_query_offset:saved_query_offset + query_budget]
    rows_by_id: Dict[str, Dict[str, Any]] = {}
    category_selection_by_id: Dict[str, Dict[str, Any]] = {}
    observed_rows = 0
    page_reads: list[Dict[str, Any]] = []
    try:
        raw_target = max(5, min(2_000, int(settings.get("_candidatePoolRawLimit") or
                                          (settings.get("_candidatePoolLimit") or settings.get("candidateLimit") or 5) * 8)))
    except (TypeError, ValueError):
        raw_target = 40
    query_limit_reached = False
    next_query_offset = saved_query_offset
    scroll_limit_reached = False
    for index, query in enumerate(query_batch, start=saved_query_offset + 1):
        if len(rows_by_id) >= raw_target:
            query_limit_reached = True
            break
        next_query_offset = index
        query_settings = copy.deepcopy(query)
        # 来源原始行先保留，平台自定义佣金区间单独应用；商品数据随后统一核验。
        query_settings["_platformCommissionRateBounds"] = list(_commission_rate_bounds(settings))
        query_settings["thresholds"] = {}
        query_settings["_unifiedSourceCollection"] = True
        pool_limit = max(1, min(2_000, int(settings.get("_candidatePoolLimit") or settings.get("candidateLimit") or 5)))
        query_settings["_candidatePoolLimit"] = pool_limit
        query_settings["candidateLimit"] = pool_limit
        _report_operation_progress(
            progress, "读取选品广场候选", index=index - 1, total=len(queries),
            percent=int((index - 1) * 100 / max(1, len(queries))),
            message=f"采集选品广场当前可见商品（类目查询 {index}/{len(queries)}）",
        )
        try:
            result = _apply_one_category_and_collect_candidates(
                page,
                query_settings,
                force_refresh=index == 1,
                preserve_other_filters=(
                    index > 1 and _can_reuse_applied_non_category_filters(
                        _page_applied_filter_signature(page), query_settings
                    )
                ),
                replace_category_selection=len(queries) > 1 or bool(settings.get("_quotaCategoryPath")),
            )
        except CommissionFilterUnconfirmed:
            # 佣金条件未回读确认时不能继续使用任何类目结果。
            raise
        except Exception as error:
            if _selection_error_requires_pause(error):
                raise
            warnings.append(
                f"跳过选品广场类目查询 {index}/{len(queries)}；"
                f"未执行加车：{_safe_diagnostic_text(error, 240)}"
            )
            continue
        category_warnings = [
            str(warning) for warning in result.get("warnings", [])
            if isinstance(warning, str) and (
                "已跳过类目勾选" in warning or "没有可应用的下级类目" in warning
            )
        ]
        if category_warnings:
            warnings.extend(
                f"选品广场类目查询 {index}/{len(queries)} 未确认，结果不计入候选池：{warning}"
                for warning in category_warnings
            )
            continue
        payload = _get_cached_material_list(id(page), _selection_filter_signature(query_settings))
        if payload is None:
            warnings.append(
                f"选品广场类目查询 {index}/{len(queries)} 未收到匹配类目请求的列表响应，结果不计入候选池"
            )
            continue
        raw_rows = _promotion_items(payload)
        if not raw_rows:
            raw_rows = [
                {**_candidate_as_promotion_row(candidate), "_categorySourceHint": "react_card"}
                for candidate in result.get("candidates", [])
                if isinstance(candidate, dict)
            ]
        for row in raw_rows:
            row["_sourceReadPage"] = f"query:{index}:initial"
        visible_ids = {str(row.get("product_id") or "").strip() for row in raw_rows}
        query_selection = query_settings.get("categorySelection")
        query_category_path = [
            str(query_selection.get(level, [""])[0])
            for level in ("level1", "level2", "level3")
            if isinstance(query_selection, dict) and isinstance(query_selection.get(level), list) and query_selection.get(level)
        ]
        query_scroll_key = f"selectionSquare:{'/'.join(query_category_path)}" if query_category_path else "selectionSquare"
        try:
            scroll_steps = max(1, min(120, int(settings.get("_candidatePoolScrollSteps", 6))))
        except (TypeError, ValueError):
            scroll_steps = 6
        scroll_positions = settings.get("_candidatePoolScrollPositions")
        if isinstance(scroll_positions, dict):
            query_settings["_candidatePoolScrollPosition"] = scroll_positions.get(
                query_scroll_key, scroll_positions.get("selectionSquare", 0)
            )
        more_rows, scroll_reads, query_scroll_limited = _scroll_selection_square_rows(
            page, query_settings, visible_ids, max_steps=scroll_steps
        )
        scroll_limit_reached = scroll_limit_reached or query_scroll_limited
        raw_rows.extend(more_rows)
        page_reads.extend({"query": index, "scrollKey": query_scroll_key, **entry} for entry in scroll_reads)
        observed_rows += len(raw_rows)
        selection = query_selection
        query_category_path = []
        if isinstance(selection, dict):
            raw_path = [
                str(selection.get(level, [""])[0])
                for level in ("level1", "level2", "level3")
                if isinstance(selection.get(level), list) and selection.get(level)
            ]
            if 1 <= len(raw_path) <= 3 and all(raw_path):
                category_tree = query_settings.get("categoryTree") if isinstance(query_settings.get("categoryTree"), list) else []
                resolved, _ = _resolve_single_category_id(raw_path[-1], category_tree)
                if len(resolved) != len(raw_path) or any(
                    not _category_id_matches(actual, expected)
                    for actual, expected in zip(resolved, raw_path)
                ):
                    raise RuntimeError("选品广场已选类目路径无法在当前目录中唯一核实；结果不计入候选池")
                query_category_path = resolved
        query_source_category = (
            {
                "name": "/".join(query_category_path), "id": query_category_path[-1],
                "categoryIds": query_category_path, "level": len(query_category_path),
                "source": "selectionSquare", "selected": True,
                "selectionConfirmed": True,
            }
            if query_category_path else None
        )
        for row in raw_rows:
            product_id = str(row.get("product_id") or "").strip()
            if not product_id:
                continue
            evidence = _row_category_evidence(
                row, product_id, settings.get("categoryTree", []) if isinstance(settings.get("categoryTree"), list) else [],
                str(row.get("_categorySourceHint") or "material_list"),
            )
            if query_source_category is not None:
                row["_sourceCategory"] = copy.deepcopy(query_source_category)
                row["_sourceCategories"] = [copy.deepcopy(query_source_category)]
                # 已确认的页面查询路径是来源证据，不伪装成商品详情返回的三级类目。
                # 商品行若自带类目，仍单独保留，供冲突检查使用。
            row["_categoryEvidence"] = evidence
            if evidence.get("status") == "known":
                base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
                info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
                info["category"] = dict(zip(("first", "second", "third"), evidence["categoryIds"]))
                base["product_info"] = info
                row["base_model"] = base
            if product_id not in rows_by_id:
                rows_by_id[product_id] = row
                if isinstance(selection, dict):
                    category_selection_by_id[product_id] = selection
            else:
                rows_by_id[product_id] = _merge_candidate_source_rows(rows_by_id[product_id], row)
        if len(rows_by_id) >= raw_target:
            query_limit_reached = True
    for product_id, row in rows_by_id.items():
        if product_id in category_selection_by_id:
            row["_sourceCategorySelection"] = category_selection_by_id[product_id]
    ids = set(rows_by_id)
    query_limit_reached = next_query_offset < len(queries)
    detail = (
        f"{label}读取 material_list {observed_rows} 行，去重后 {len(ids)} 个商品 ID；"
        f"类目模式 {selection_mode}，本轮查询 {next_query_offset - saved_query_offset} 组；"
        f"{'达到本轮原始候选上限后停止' if query_limit_reached else '未触发原始候选上限'}；"
        f"实际滚动 {len(page_reads)} 次，新增响应商品 {sum(item.get('newProductIds', 0) for item in page_reads)} 个。"
    )
    return list(rows_by_id.values()), {
        "sourceKey": "selectionSquare",
        "label": label,
        "status": "read" if ids else "empty",
        "productCount": len(ids),
        "candidateNodeCount": observed_rows,
        "productNodeCount": observed_rows,
        "parsedCandidateCount": len(ids),
        "firstProductIds": sorted(ids)[:3],
        "sourceOutcome": f"读取当前选品广场列表 {len(ids)} 个商品 ID" if ids else "选品广场当前列表未解析到商品 ID",
        "pageListDiagnosis": "当前选品广场列表未解析到商品 ID" if not ids else "",
        "detail": detail,
        "warnings": warnings,
        "pageReads": page_reads,
        "scrollPosition": page_reads[-1].get("scrollTop") if page_reads else 0,
        "scrollPositions": {
            str(item["scrollKey"]): _scroll_position_top(item.get("scrollTop"))
            for item in page_reads if item.get("scrollKey") and _scroll_position_top(item.get("scrollTop")) is not None
        },
        "categoryQueryLimitReached": query_limit_reached,
        "categoryTabNextOffset": next_query_offset,
        "categoryTabTotal": len(queries),
        "categoryTabReadLimitReached": query_limit_reached,
        "scrollLimitReached": scroll_limit_reached,
        "sourceReadLimitReached": query_limit_reached or scroll_limit_reached,
        "productIdFingerprint": hashlib.sha256("|".join(sorted(ids)).encode("utf-8")).hexdigest(),
        "readAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "pagePath": _detail_diagnostic_path(page.url),
    }


def _merge_candidate_source_rows(primary: Dict[str, Any], supplement: Dict[str, Any]) -> Dict[str, Any]:
    """按商品 ID 合并多源字段，保留已取得的非空值并补齐缺失指标。"""
    result = copy.deepcopy(primary)

    def merge(target: Any, extra: Any) -> Any:
        if isinstance(target, dict) and isinstance(extra, dict):
            for key, value in extra.items():
                if key not in target or target[key] is None or target[key] == "":
                    target[key] = copy.deepcopy(value)
                else:
                    target[key] = merge(target[key], value)
            return target
        if isinstance(target, list) and isinstance(extra, list):
            return target if target else copy.deepcopy(extra)
        return target

    return merge(result, supplement)


def _save_category_enrichment_report(report: Dict[str, Any]) -> str:
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    folder = root / "diagnostics" / "selection" / "category-enrichment"
    report_id = f"category-enrichment-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    path = folder / f"{report_id}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        report["reportId"] = report_id
        report["createdAt"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        report["reportPath"] = str(path)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception as error:
        report["reportPath"] = f"未能保存：{type(error).__name__}"
        return str(report["reportPath"])


def _probe_source_card_category(
    page: Page,
    source_key: str,
    row: Dict[str, Any],
    category_tree: Sequence[Any],
) -> Dict[str, Any]:
    """在原账号内只读点击精确商品卡；只返回脱敏诊断，不持久化详情响应。"""
    product_id = str(row.get("product_id") or "").strip()
    base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
    info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    title = str(info.get("name") or row.get("title") or row.get("product_name") or "").strip()
    diagnostic: Dict[str, Any] = {
        "sourceKey": source_key, "productId": product_id,
        "entryKind": "source_card", "entryPath": "", "responses": [],
        "finalPagePaths": [], "status": "entry_unavailable", "categoryIds": [],
    }
    if not product_id or not title:
        diagnostic["reason"] = "来源行缺少可核对的商品 ID 或标题"
        return diagnostic
    source_page: Optional[Page] = None
    try:
        if source_key == "selectionSquare":
            source_page = page
        else:
            browser = page.context.browser
            if browser is None:
                diagnostic["reason"] = "账号浏览器连接不可用"
                return diagnostic
            source_page = _create_background_page(page.context, browser)
            source_page.goto(BAIYING_PRIORITY_SOURCES[source_key][1], wait_until="commit", timeout=20_000)
        diagnostic["entryPath"] = _detail_diagnostic_path(source_page.url)
        prompt_kind, _ = _visible_baiying_prompt(source_page)
        if prompt_kind in {"manual_verification", "login", "account_unavailable"}:
            raise RuntimeError(f"百应来源页账号状态异常：{prompt_kind}")
        candidate = {"productId": product_id, "title": title, "sourceKey": source_key}
        if source_key != "selectionSquare" and not _ensure_priority_source_candidate_visible(
            source_page, source_key, candidate, timeout_ms=8_000
        ):
            diagnostic["reason"] = "来源商品卡未在有限滚动窗口内按精确 ID 定位"
            return diagnostic
        payload, error = _capture_candidate_detail(
            source_page, page.context, candidate, None, None,
            diagnostics=diagnostic, timeout_seconds=12,
        )
        matched_payloads = diagnostic.pop("_matchedPayloads", [])
        category_ids: list[str] = []
        category_error = ""
        for matched_payload in [payload, *matched_payloads]:
            if not isinstance(matched_payload, dict):
                continue
            found_ids, found_error = _detail_category_path(matched_payload, product_id, category_tree)
            if not category_error:
                category_error = found_error
            if len(found_ids) >= 3:
                category_ids = found_ids
                break
        if payload is None and not matched_payloads:
            diagnostic["reason"] = "商品卡详情未取得精确 ID 响应：" + _safe_diagnostic_text(error, 180)
            return diagnostic
        diagnostic["exactResponseSeen"] = True
        diagnostic["categoryIds"] = category_ids
        if len(category_ids) >= 3:
            diagnostic["status"] = "category_from_source_card"
        elif any(item.get("categoryFieldSamples") for item in diagnostic["responses"]):
            diagnostic["status"] = "category_field_unresolved"
            diagnostic["reason"] = category_error
        else:
            diagnostic["status"] = "matched_response_without_category"
            diagnostic["reason"] = category_error
        return diagnostic
    except Exception as error:
        message = str(error).casefold()
        if any(marker in message for marker in (
            "登录", "验证码", "安全验证", "风控", "限流", "11001",
            "account_unavailable", "manual_verification", "target closed",
            "browser has been closed", "context has been closed", "browser disconnected", "websocket", "cdp",
        )):
            raise
        diagnostic["reason"] = f"商品卡只读核验失败：{type(error).__name__}"
        return diagnostic
    finally:
        diagnostic.pop("_matchedPayloads", None)
        if source_page is not None and source_page is not page:
            try:
                source_page.close()
            except Exception:
                pass


def _category_probe_group(row: Dict[str, Any], index: int, total: int) -> str:
    source = row.get("_sourceCategory") if isinstance(row.get("_sourceCategory"), dict) else {}
    source_id = str(source.get("id") or "")
    page_step = str(row.get("_sourceReadPage") or "")
    if source_id or page_step:
        return f"{source_id}|{page_step}"
    # 页面未提供分组信息时，跨列表头、中、尾取样，避免只看前三张卡。
    return f"position:{min(2, index * 3 // max(1, total))}"


def _category_probe_sample_rows(rows: Sequence[Dict[str, Any]], limit: int = 3) -> tuple[list[Dict[str, Any]], int]:
    if not rows or limit <= 0:
        return [], 0
    groups: Dict[str, list[Dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(_category_probe_group(row, index, len(rows)), []).append(row)
    if len(groups) == 1 and len(rows) > limit:
        indexes = [round(index * (len(rows) - 1) / max(1, limit - 1)) for index in range(limit)]
        return [rows[index] for index in indexes], 1
    group_rows_list = list(groups.values())
    group_sample_count = min(limit, len(group_rows_list))
    group_indexes = [round(index * (len(group_rows_list) - 1) / max(1, group_sample_count - 1))
                     for index in range(group_sample_count)]
    sampled = [group_rows_list[index][0] for index in group_indexes]
    if len(sampled) < limit:
        sampled_ids = {str(row.get("product_id") or "") for row in sampled}
        spread_count = min(limit, len(rows))
        spread = [rows[round(index * (len(rows) - 1) / max(1, spread_count - 1))]
                  for index in range(spread_count)]
        for row in [*spread, *rows]:
            if str(row.get("product_id") or "") not in sampled_ids:
                sampled.append(row)
                if len(sampled) >= limit:
                    break
    return sampled, len(groups)


def _collector_candidate_pool_result(
    source_candidates: Dict[str, list[Dict[str, Any]]],
    source_membership: Dict[str, set[str]],
    source_results: list[Dict[str, Any]],
    warnings: list[str],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """按采集条件只读汇总商品；采集账号不执行加车或账号配额操作。"""
    by_product: Dict[str, Dict[str, Any]] = {}
    for source_key in [*_enabled_priority_source_order(settings), "selectionSquare"]:
        for row in source_candidates.get(source_key, []):
            product_id = str(row.get("product_id") or "").strip()
            if not product_id or not product_id.isdigit():
                continue
            row = copy.deepcopy(row)
            base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
            info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
            if not str(info.get("name") or row.get("title") or "").strip():
                row["title"] = product_id
            if product_id in by_product:
                by_product[product_id] = _merge_candidate_source_rows(by_product[product_id], row)
            else:
                by_product[product_id] = copy.deepcopy(row)
    source_order = [*_enabled_priority_source_order(settings), "selectionSquare"]
    rows_by_source = {
        source_key: list(source_candidates.get(source_key, []))
        for source_key in source_order
    }
    candidates, parse_messages, rejected = _candidate_rows_by_source_policy(
        rows_by_source,
        source_membership,
        settings,
        min(2000, len(by_product)),
    )
    for candidate in [*candidates, *rejected]:
        product_id = str(candidate.get("productId") or "")
        sources = [key for key in source_order if product_id in source_membership.get(key, set())]
        candidate["sources"] = sources
        if str(candidate.get("sourceKey") or "") not in sources:
            candidate["sourceKey"] = sources[0] if sources else ""
    accepted_ids = {str(item.get("productId") or "") for item in candidates}
    rejected_ids = {str(item.get("productId") or "") for item in rejected}
    for source_result in source_results:
        source_key = str(source_result.get("sourceKey") or "")
        source_ids = source_membership.get(source_key, set())
        source_result["sourceFilterPolicy"] = _source_candidate_filter_settings(settings, source_key).get(
            "_sourceFilterPolicy", ""
        )
        source_result["localPassedCount"] = len(source_ids.intersection(accepted_ids))
        source_result["localRejectedCount"] = len(source_ids.intersection(rejected_ids))
    excluded_categories = _excluded_category_selection_ids(settings)
    included_categories = _manual_category_selection_ids(settings) if settings.get("selectionMode") == "category" and settings.get("categoryMode") == "manual" else set()
    quota_path = settings.get("_quotaCategoryPath") if isinstance(settings.get("_quotaCategoryPath"), list) else []
    ready: list[Dict[str, Any]] = []
    for candidate in candidates:
        raw_source_categories = by_product.get(str(candidate.get("productId") or ""), {}).get("_sourceCategories")
        source_paths = _verified_source_category_paths(raw_source_categories)
        source_path = next((path for path in source_paths if _source_category_path_satisfies_settings(path, settings)), [])
        if not source_path:
            candidate["thresholdReasons"] = ["来源页面类目筛选未确认或未覆盖当前排除与配额条件；不计入候选池"]
            rejected.append(candidate)
            continue
        candidate["categoryStatus"] = "source_level"
        candidate["sourceCategoryIds"] = source_path
        if excluded_categories and _candidate_matches_manual_categories(candidate, excluded_categories):
            candidate["thresholdReasons"] = ["来源分类属于已排除类目"]
            rejected.append(candidate)
        elif included_categories and not _candidate_matches_manual_categories(candidate, included_categories):
            candidate["thresholdReasons"] = ["来源分类不在选定的类目范围"]
            rejected.append(candidate)
        else:
            ready.append(candidate)
    candidates = ready
    if quota_path:
        matching: list[Dict[str, Any]] = []
        for candidate in candidates:
            ids = candidate.get("categoryIds") if isinstance(candidate.get("categoryIds"), list) else []
            if len(ids) >= len(quota_path) and all(_category_id_matches(str(ids[index]), str(expected))
                                                      for index, expected in enumerate(quota_path)):
                matching.append(candidate)
            elif (candidate.get("categoryStatus") == "source_level" and
                  len(candidate.get("sourceCategoryIds", [])) >= len(quota_path) and
                  all(_category_id_matches(str(candidate["sourceCategoryIds"][index]), str(expected))
                      for index, expected in enumerate(quota_path))):
                matching.append(candidate)
            else:
                candidate["thresholdReasons"] = ["商品类目未能精确匹配本条品类配额"]
                rejected.append(candidate)
        candidates = matching
    for index, candidate in enumerate(candidates):
        product_id = str(candidate.get("productId") or "")
        candidate["categoryStatus"] = "known" if len(candidate.get("categoryIds") or []) == 3 else candidate.get("categoryStatus") or "unknown"
        candidate["leafCategoryId"] = str((candidate.get("categoryIds") or [""])[-1]) if candidate["categoryStatus"] == "known" else ""
        candidate["cardIndex"] = index
        candidate["selected"] = False
        candidate["sourceCategorySelection"] = by_product.get(product_id, {}).get("_sourceCategorySelection", {})
    return {
        "ok": True,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "rejectedCandidates": rejected,
        "warnings": list(dict.fromkeys([*warnings, *parse_messages])),
        "prioritySourceResults": source_results,
        "selectionStrategy": "collector_filtered_pool",
        "sourceProductCount": len(by_product),
        "sourceFilteredCount": len(candidates),
        "sourceRejectedCount": len(rejected),
    }


def _visible_source_candidate_result(
    page: Page,
    source_candidates: Dict[str, list[Dict[str, Any]]],
    source_membership: Dict[str, set[str]],
    source_results: list[Dict[str, Any]],
    selection_square_rows: list[Dict[str, Any]],
    selection_square_result: Dict[str, Any],
    warnings: list[str],
    settings: Dict[str, Any],
    preserve_product_ids: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """三源候选统一去重、补指标，并按各来源的筛选语义处理。"""
    source_order = _enabled_priority_source_order(settings)
    source_labels = {key: value[0] for key, value in BAIYING_PRIORITY_SOURCES.items()}
    source_labels["selectionSquare"] = BAIYING_SELECTION_SQUARE_SOURCE[0]
    source_urls = {key: value[1] for key, value in BAIYING_PRIORITY_SOURCES.items()}
    source_urls["selectionSquare"] = BAIYING_SELECTION_SQUARE_SOURCE[1]
    excluded_category_ids = _excluded_category_selection_ids(settings)
    # 候选类目以已应用的页面筛选及其商品 ID 结果为准。商品详情类目可以
    # 用于发现冲突，但不能成为已确认来源筛选后的额外入池前置条件。
    category_gate_enabled = True
    threshold_names = _enabled_threshold_names(settings)
    category_tree = settings.get("categoryTree", []) if isinstance(settings.get("categoryTree"), list) else []
    detail_skip_ids = {
        str(value) for value in settings.get("_categoryDetailSkipIds", [])
        if isinstance(value, (str, int)) and str(value).isdigit()
    } if isinstance(settings.get("_categoryDetailSkipIds"), list) else set()
    merged_raw: Dict[str, Dict[str, Any]] = {}
    sources_by_id: Dict[str, list[str]] = {}
    category_evidence_by_id: Dict[str, list[Dict[str, Any]]] = {}
    source_categories_by_id: Dict[str, list[Dict[str, Any]]] = {}

    def merge_source_row(source_key: str, source_row: Dict[str, Any]) -> None:
        product_id = str(source_row.get("product_id") or "").strip()
        if not product_id:
            return
        row = copy.deepcopy(source_row)
        evidence = row.get("_categoryEvidence") if isinstance(row.get("_categoryEvidence"), dict) else _row_category_evidence(
            row, product_id, category_tree, "material_list" if source_key == "selectionSquare" else "source_row"
        )
        category_evidence_by_id.setdefault(product_id, []).append(evidence)
        base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
        info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
        if evidence.get("status") == "known":
            info["category"] = dict(zip(("first", "second", "third"), evidence["categoryIds"]))
        else:
            info.pop("category", None)
        base["product_info"] = info
        row["base_model"] = base
        source_categories = row.get("_sourceCategories") if isinstance(row.get("_sourceCategories"), list) else []
        if not source_categories and isinstance(row.get("_sourceCategory"), dict):
            source_categories = [row["_sourceCategory"]]
        for category in source_categories:
            if isinstance(category, dict) and category not in source_categories_by_id.setdefault(product_id, []):
                source_categories_by_id[product_id].append(category)
        if product_id not in merged_raw:
            merged_raw[product_id] = row
        else:
            merged_raw[product_id] = _merge_candidate_source_rows(merged_raw[product_id], row)
        sources_by_id.setdefault(product_id, []).append(source_key)

    for source_key in source_order:
        for row in source_candidates.get(source_key, []):
            merge_source_row(source_key, row)

    selection_square_ids: set[str] = set()
    for row in selection_square_rows:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            continue
        selection_square_ids.add(product_id)
        merge_source_row("selectionSquare", row)

    excluded_level1 = settings.get("excludedCategorySelection", {}).get("level1", []) if isinstance(settings.get("excludedCategorySelection"), dict) else []
    for product_id, row in merged_raw.items():
        evidences = category_evidence_by_id.get(product_id, [])
        exact_paths = {tuple(evidence.get("categoryIds", [])) for evidence in evidences if evidence.get("status") == "known"}
        ambiguous = any(evidence.get("status") == "ambiguous" for evidence in evidences) or len(exact_paths) > 1
        source_categories = source_categories_by_id.get(product_id, [])
        source_roots = {str(item["categoryIds"][0]) for item in source_categories
                        if isinstance(item.get("categoryIds"), list) and item["categoryIds"]}
        source_paths = [item.get("categoryIds", []) for item in source_categories
                        if isinstance(item.get("categoryIds"), list) and item["categoryIds"]]
        if exact_paths and source_paths and not any(
            all(_category_id_matches(path[index], exact[index]) for index in range(min(len(path), len(exact))))
            for path in source_paths for exact in exact_paths
        ):
            ambiguous = True
        row["_sourceCategories"] = source_categories
        row["_categoryAmbiguous"] = ambiguous
        row["_sourceLevelExcluded"] = any(
            _category_id_matches(root, _category_id_scalar(excluded))
            for root in source_roots for excluded in excluded_level1 if _category_id_scalar(excluded)
        )
        base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
        info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
        if len(exact_paths) == 1 and not ambiguous:
            path = next(iter(exact_paths))
            info["category"] = dict(zip(("first", "second", "third"), path))
            row["_categoryEvidence"] = next(evidence for evidence in evidences if evidence.get("status") == "known")
        else:
            info.pop("category", None)
        base["product_info"] = info
        row["base_model"] = base

    def source_category_satisfies_general_exclusions(product_id: str) -> bool:
        """确认该商品来自足以覆盖当前设置的已选页面分类。"""
        paths = _verified_source_category_paths(source_categories_by_id.get(product_id, []))
        return bool(paths) and any(
            _source_category_path_satisfies_settings(path, settings) for path in paths
        )

    raw_rows = list(merged_raw.values())
    raw_ids = set(merged_raw)
    preferred_values = settings.get("_candidatePoolPreferredProductIds")
    preferred_rank = {
        str(product_id): index for index, product_id in enumerate(preferred_values[:2000])
        if str(product_id).isdigit()
    } if settings.get("_candidatePoolCollectOnly") is True and isinstance(preferred_values, list) else {}
    if preserve_product_ids:
        wanted = {str(value).strip() for value in preserve_product_ids if str(value).strip()}
        raw_rows = [row for row in raw_rows if str(row.get("product_id") or "").strip() in wanted]

    category_enrichment: Dict[str, Any] = {
        "candidateCount": len(raw_rows),
        "successCount": 0,
        "sourceLevelCount": 0,
        "failureCount": 0,
        "successes": [],
        "failureDetails": [],
        "detailReadDiagnostics": [],
        "recommendations": [],
        "titleInference": "未使用；低置信度标题推测未进入类目决策",
        "sourceFilterEvidence": [
            {
                "sourceKey": result.get("sourceKey"),
                "status": result.get("status"),
                "categoryTabReads": result.get("categoryTabReads", []),
                "categoryTabTotal": result.get("categoryTabTotal", 0),
                "unmappedCategoryTabs": result.get("unmappedCategoryTabs", 0),
                "sourceCategoryTabs": result.get("sourceCategoryTabs", []),
            }
            for result in source_results if isinstance(result, dict)
        ],
    }
    category_results: Dict[str, Dict[str, Any]] = {}
    category_logic_enabled = settings.get("selectionMode") == "category"
    source_probes: Dict[str, Dict[str, Any]] = {}
    sampled_enrichments: Dict[str, Dict[str, Any]] = {}
    detail_attempted_ids: set[str] = set()
    source_category_unavailable: set[str] = set()
    if category_gate_enabled and settings.get("_candidatePoolCollectOnly") is not True:
        raw_ids_to_check = {str(row.get("product_id") or "").strip() for row in raw_rows}
        for source_key in [*source_order, "selectionSquare"]:
            source_rows = selection_square_rows if source_key == "selectionSquare" else source_candidates.get(source_key, [])
            probe_candidates = [
                source_row for source_row in source_rows
                if str(source_row.get("product_id") or "").strip() in raw_ids_to_check
                and str(source_row.get("product_id") or "").strip() not in detail_skip_ids
                and not merged_raw[str(source_row.get("product_id") or "").strip()].get("_sourceLevelExcluded")
                and not merged_raw[str(source_row.get("product_id") or "").strip()].get("_categoryAmbiguous")
                and not source_category_satisfies_general_exclusions(str(source_row.get("product_id") or "").strip())
                and _row_category_evidence(
                    source_row, str(source_row.get("product_id") or "").strip(), category_tree,
                    "material_list" if source_key == "selectionSquare" else "source_row",
                ).get("status") != "known"
            ]
            sample_rows, group_count = _category_probe_sample_rows(probe_candidates)
            source_probe: Dict[str, Any] = {
                "sourceKey": source_key, "label": source_labels[source_key],
                "sampleLimit": 3, "samples": [], "exactMissingCount": 0,
                "candidateGroupCount": group_count,
                "status": "not_needed" if not sample_rows else "sampling",
            }
            for sample_row in sample_rows:
                sample_id = str(sample_row.get("product_id") or "").strip()
                detail_attempted_ids.add(sample_id)
                card_probe = _probe_source_card_category(page, source_key, sample_row, category_tree)
                category_ids = card_probe.get("categoryIds", [])
                direct_result: Dict[str, Any] = {}
                if len(category_ids) < 3:
                    direct_result = _read_candidate_category_from_detail(
                        page.context, sample_row, sample_id, category_tree
                    )
                    if direct_result.get("fatal"):
                        raise RuntimeError(str(direct_result.get("reason") or "百应详情账号状态异常"))
                    if len(direct_result.get("categoryIds", [])) >= 3:
                        category_ids = direct_result["categoryIds"]
                direct_diagnostics = direct_result.get("diagnostics") if isinstance(direct_result.get("diagnostics"), dict) else {}
                exact_response_seen = bool(card_probe.get("exactResponseSeen")) or bool(
                    direct_diagnostics.get("verifiedResponseCount")
                )
                sample_status = (
                    "category_from_source_card" if card_probe.get("status") == "category_from_source_card"
                    else "category_from_direct_route" if len(category_ids) >= 3
                    else "category_field_unresolved" if card_probe.get("status") == "category_field_unresolved"
                    or direct_diagnostics.get("verifiedCategoryFieldSamples")
                    else "matched_response_without_category" if exact_response_seen
                    else "detail_entry_unavailable"
                )
                sample_diagnostic = {
                    "productId": sample_id, "status": sample_status,
                    "categoryIds": category_ids,
                    "card": card_probe, "direct": direct_diagnostics,
                }
                source_probe["samples"].append(sample_diagnostic)
                if sample_status == "matched_response_without_category":
                    source_probe["exactMissingCount"] += 1
                if len(category_ids) >= 3:
                    sampled_enrichments[sample_id] = {
                        "productId": sample_id, "categoryIds": category_ids,
                        "reason": "", "diagnostics": {"sourceProbe": sample_diagnostic},
                        "categorySource": "react_card" if card_probe.get("status") == "category_from_source_card" else "detail_api",
                    }
                    source_probe["status"] = "category_available"
                    break
                if sample_id not in sampled_enrichments:
                    sampled_enrichments[sample_id] = {
                        "productId": sample_id, "categoryIds": [],
                        "reason": str(direct_result.get("reason") or card_probe.get("reason") or "详情类目未知"),
                        "diagnostics": {"sourceProbe": sample_diagnostic},
                    }
            if source_probe["status"] == "sampling":
                sample_statuses = [item["status"] for item in source_probe["samples"]]
                if len(sample_statuses) == 3 and group_count <= 3 and all(status in {
                    "matched_response_without_category", "category_field_unresolved", "detail_entry_unavailable"
                } for status in sample_statuses):
                    source_probe["status"] = (
                        "category_field_unresolved" if "category_field_unresolved" in sample_statuses
                        else "category_data_unavailable" if "matched_response_without_category" in sample_statuses
                        else "entry_unavailable"
                    )
                    source_category_unavailable.add(source_key)
                    warnings.append(
                        f"{source_labels[source_key]}连续 3 件商品的详情诊断均为 {source_probe['status']}；"
                        "本轮停止重复探测该来源，保留缺额待重试"
                    )
                else:
                    source_probe["status"] = "category_probe_incomplete" if group_count > 3 else "entry_or_category_unresolved"
            source_probes[source_key] = source_probe
            for source_result in source_results:
                if source_result.get("sourceKey") == source_key:
                    source_result["categoryProbeStatus"] = source_probe["status"]
                    source_result["categoryProbeCount"] = len(source_probe["samples"])
                    break
        category_enrichment["sourceProbes"] = source_probes
        category_enrichment["categoryUnavailableSources"] = sorted(source_category_unavailable)

    def read_verified_detail(row: Dict[str, Any], product_id: str) -> Dict[str, Any]:
        sampled = sampled_enrichments.get(product_id)
        if sampled and len(sampled.get("categoryIds", [])) >= 3:
            return sampled
        product_sources = sources_by_id.get(product_id, [])
        if product_sources and all(key in source_category_unavailable for key in product_sources):
            return {
                "productId": product_id, "categoryIds": [],
                "reason": "已尝试来源的详情入口或类目字段连续抽样后仍无法核实；保留缺额待重试",
                "diagnostics": {
                    "sourceCategoryUnavailable": product_sources,
                    **(sampled.get("diagnostics", {}) if sampled else {}),
                },
            }
        if sampled and len(product_sources) == 1:
            return sampled
        last_result: Optional[Dict[str, Any]] = sampled
        for source_key in product_sources:
            if source_key in source_category_unavailable:
                continue
            source_rows = selection_square_rows if source_key == "selectionSquare" else source_candidates.get(source_key, [])
            source_row = next((
                item for item in source_rows
                if str(item.get("product_id") or "").strip() == product_id
            ), None)
            if source_row is not None:
                result = _read_candidate_category_from_detail(page.context, source_row, product_id, category_tree)
                if result.get("fatal") or len(result.get("categoryIds", [])) >= 3:
                    return result
                last_result = result
        return last_result or _read_candidate_category_from_detail(page.context, row, product_id, category_tree)

    pool_candidate_cap = 2_000 if settings.get("_candidatePoolCollectOnly") is True else 200
    try:
        requested_target = max(1, min(pool_candidate_cap, int(settings.get("_candidateDesiredPoolCount", settings.get("candidateLimit", 5)))))
    except (TypeError, ValueError):
        requested_target = 5
    desired_pool_count = min(pool_candidate_cap, requested_target + max(4, requested_target // 2))
    detail_read_budget = min(40, max(12, desired_pool_count * 2))
    pre_candidates, _, pre_rejected = _candidate_rows(
        {"data": {"summary_promotions": raw_rows}},
        {**settings, "_sourceMembership": source_membership}, max(200, requested_target), preserve_product_ids,
    )
    pre_rank = {str(row.get("productId") or ""): index for index, row in enumerate(pre_candidates)}
    pre_blocked_ids = {
        str(row.get("productId") or "") for row in pre_rejected
        if row.get("titleFilterExcluded") or any(
            not str(reason).startswith("缺少") for reason in row.get("thresholdReasons", [])
        )
    }
    raw_rows.sort(key=lambda row: (
        str(row.get("product_id") or "") in pre_blocked_ids,
        not source_category_satisfies_general_exclusions(str(row.get("product_id") or "")),
        preferred_rank.get(str(row.get("product_id") or ""), len(preferred_rank)),
        pre_rank.get(str(row.get("product_id") or ""), len(pre_rank)),
    ))

    def ready_after_detail(row: Dict[str, Any]) -> bool:
        product_id = str(row.get("product_id") or "").strip()
        source_level_ready = source_category_satisfies_general_exclusions(product_id) and not row.get("_sourceLevelExcluded") and not row.get("_categoryAmbiguous")
        if not source_level_ready:
            return False
        ready, _, _ = _candidate_rows(
            {"data": {"summary_promotions": [row]}},
            {**settings, "_sourceMembership": source_membership}, 1,
        )
        if not ready or ready[0].get("pendingThresholds"):
            return False
        return True

    fatal_category_error = ""
    processed_rows: list[Dict[str, Any]] = []
    deferred_ids: list[str] = []
    qualified_count = 0
    for index, row in enumerate(raw_rows, start=1):
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            continue
        if qualified_count >= desired_pool_count:
            deferred_ids.extend(str(item.get("product_id") or "") for item in raw_rows[index - 1:]
                                if str(item.get("product_id") or "") not in detail_skip_ids)
            break
        base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
        product_info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
        source_category = product_info.get("category") if isinstance(product_info.get("category"), dict) else {}
        listed_category_ids = _promotion_row_category_ids(row)
        source_level_excluded = row.get("_sourceLevelExcluded") is True
        category_ambiguous = row.get("_categoryAmbiguous") is True
        existing_metrics = _promotion_row_metric_values(row)
        missing_metrics = [
            metric for metric in threshold_names
            if existing_metrics.get(metric) is None
        ]
        source_category_sufficient = source_category_satisfies_general_exclusions(product_id) and not source_level_excluded and not category_ambiguous
        needs_category_detail = (
            category_gate_enabled and settings.get("_candidatePoolCollectOnly") is not True
            and len(listed_category_ids) < 3 and not source_category_sufficient
        )
        if needs_category_detail and product_id in detail_skip_ids:
            continue
        product_sources = sources_by_id.get(product_id, [])
        source_unavailable = bool(product_sources) and all(key in source_category_unavailable for key in product_sources)
        detail_needed = not fatal_category_error and not source_level_excluded and not category_ambiguous and (needs_category_detail or missing_metrics)
        if product_id in pre_blocked_ids and detail_needed:
            category_results[product_id] = {
                "productId": product_id, "categoryIds": listed_category_ids,
                "reason": "商品行已被明确的标题或指标条件排除，未打开详情",
            }
            detail_needed = False
        if detail_needed and product_id not in sampled_enrichments and not source_unavailable:
            if len(detail_attempted_ids) >= detail_read_budget:
                deferred_ids.append(product_id)
                continue
            detail_attempted_ids.add(product_id)
        if detail_needed:
            enrichment = read_verified_detail(row, product_id)
            category_ids_from_detail = [str(value).strip() for value in enrichment.get("categoryIds", []) if str(value).strip()]
            if len(category_ids_from_detail) >= 3:
                source_paths = _verified_source_category_paths(row.get("_sourceCategories"))
                conflicts_with_source = bool(source_paths) and not any(
                    all(_category_id_matches(str(path[level]), category_ids_from_detail[level])
                        for level in range(min(len(path), 3)))
                    for path in source_paths
                )
                if (len(listed_category_ids) >= 3 and listed_category_ids != category_ids_from_detail[:3]) or conflicts_with_source:
                    category_ambiguous = True
                    row["_categoryAmbiguous"] = True
                    enrichment = {"productId": product_id, "categoryIds": [],
                                  "reason": "已确认的页面分类或来源行与精确详情类目冲突", "status": "ambiguous"}
                else:
                    source_category = dict(zip(("first", "second", "third"), category_ids_from_detail[:3]))
                base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
                product_info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
                if category_ambiguous:
                    product_info.pop("category", None)
                else:
                    product_info["category"] = source_category
                if isinstance(base, dict):
                    base["product_info"] = product_info
                    row["base_model"] = base
            detail_payload = _cached_detail_payload(page.context, product_id)
            _merge_detail_metrics_into_promotion_row(row, detail_payload, product_id)
            if enrichment.get("reason") and not detail_payload:
                row["_detailMetricError"] = str(enrichment.get("reason"))
            listed_category_ids = _promotion_row_category_ids(row)
            category_results[product_id] = enrichment
        if fatal_category_error:
            diagnostics = _new_detail_category_diagnostics(
                _product_detail_url(product_id, row, base, product_info)
            )
            diagnostics["navigationState"] = "not_attempted_after_fatal"
            enrichment = {
                "productId": product_id,
                "categoryIds": [],
                "reason": fatal_category_error,
                "diagnostics": diagnostics,
            }
        else:
            enrichment = category_results.get(product_id)
            if category_ambiguous:
                enrichment = {"productId": product_id, "categoryIds": [], "status": "ambiguous",
                              "reason": "同一商品的来源类目冲突，不能安全归类"}
            elif source_level_excluded:
                enrichment = {"productId": product_id, "categoryIds": [], "status": "excluded_source_level",
                              "reason": "命中已核验的来源一级排除类目"}
            elif source_category_sufficient:
                source_paths = _verified_source_category_paths(source_categories_by_id.get(product_id, []))
                source_path = next((
                    path for path in source_paths
                    if _source_category_path_satisfies_settings(path, settings)
                ), [])
                enrichment = {
                    "productId": product_id,
                    "categoryIds": listed_category_ids if len(listed_category_ids) == 3 else [],
                    "status": "source_level",
                    "reason": "来源页面已按允许类目路径读取并核对商品 ID",
                    "categorySource": "source_category_path",
                    "sourceCategoryIds": source_path,
                }
            elif settings.get("_candidatePoolCollectOnly") is True:
                enrichment = {
                    "productId": product_id, "categoryIds": [],
                    "reason": "来源页面分类筛选未确认或未覆盖当前排除与配额条件；不以商品详情类目代替页面筛选",
                }
            elif enrichment is None:
                if len(listed_category_ids) >= 3:
                    enrichment = {
                        "productId": product_id,
                        "categoryIds": listed_category_ids,
                        "reason": "来源商品行已提供完整三级类目 ID",
                        "categorySource": row.get("_categoryEvidence", {}).get("categorySource", "material_list"),
                    }
                else:
                    enrichment = read_verified_detail(row, product_id)
                    if len(enrichment.get("categoryIds", [])) >= 3:
                        enrichment["categorySource"] = "detail_api"
            if enrichment.get("fatal"):
                fatal_category_error = str(enrichment.get("reason") or "详情页安全状态异常，已停止后续读取")
        category_results[product_id] = enrichment
        detail_diagnostics = enrichment.get("diagnostics") if isinstance(enrichment.get("diagnostics"), dict) else {}
        category_enrichment["detailReadDiagnostics"].append({
            "productId": product_id,
            **detail_diagnostics,
        })
        category_ids = [str(value).strip() for value in enrichment.get("categoryIds", []) if str(value).strip()]
        if len(category_ids) >= 3:
            base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
            product_info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
            product_info["category"] = {
                "first": category_ids[0],
                "second": category_ids[1],
                "third": category_ids[2],
            }
            category_enrichment["successCount"] += 1
            category_enrichment["successes"].append({"productId": product_id, "categoryIds": category_ids[:3]})
        elif enrichment.get("status") == "source_level":
            category_enrichment["sourceLevelCount"] += 1
        else:
            reason = str(enrichment.get("reason") or "详情页未返回完整三级类目 ID")
            category_enrichment["failureCount"] += 1
            category_enrichment["failureDetails"].append({
                "productId": product_id,
                "reason": reason,
            })
        _report_operation_progress(
            progress,
            "只读核验候选条件",
            index=index,
            total=len(raw_rows),
            percent=int(index * 100 / max(1, len(raw_rows))),
            message=(
                f"候选核验 {index}/{len(raw_rows)}：商品 {product_id} 来源筛选路径已确认"
                if source_category_sufficient
                else f"候选核验 {index}/{len(raw_rows)}：商品 {product_id} 来源筛选路径未确认"
            ),
        )
        processed_rows.append(row)
        if ready_after_detail(row):
            qualified_count += 1

    raw_rows = processed_rows
    category_enrichment["processedCount"] = len(raw_rows)
    category_enrichment["detailReadBudget"] = detail_read_budget
    category_enrichment["detailAttemptedProductIds"] = sorted(detail_attempted_ids)
    category_enrichment["detailUnresolvedProductIds"] = [] if settings.get("_candidatePoolCollectOnly") is True else sorted(
        product_id for product_id in detail_attempted_ids
        if len(sampled_enrichments.get(product_id, {}).get("categoryIds", [])) < 3
        and len(category_results.get(product_id, {}).get("categoryIds", [])) < 3
        and len(_promotion_row_category_ids(merged_raw.get(product_id, {}))) < 3
    )
    category_enrichment["deferredCount"] = len(deferred_ids)
    category_enrichment["deferredProductIds"] = deferred_ids[:20]
    category_enrichment["qualifiedBeforeLocalRegistry"] = qualified_count

    try:
        requested = int(settings.get("_candidatePoolLimit", settings.get("candidateLimit", 5)))
    except (TypeError, ValueError):
        requested = 5
    limit = max(1, min(pool_candidate_cap, requested))
    policy_rows_by_source: Dict[str, list[Dict[str, Any]]] = {
        source_key: [] for source_key in [*source_order, "selectionSquare"]
    }
    for row in raw_rows:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            continue
        row_sources = [
            source_key for source_key in [*source_order, "selectionSquare"]
            if source_key in sources_by_id.get(product_id, [])
        ]
        for source_key in row_sources:
            policy_rows_by_source[source_key].append(copy.deepcopy(row))
    candidates, parse_messages, rejected = _candidate_rows_by_source_policy(
        policy_rows_by_source,
        source_membership,
        settings,
        max(200, limit),
        preserve_product_ids,
    )
    for candidate in candidates + rejected:
        product_id = str(candidate.get("productId") or "")
        candidate_sources = [key for key in source_order if key in sources_by_id.get(product_id, [])]
        if "selectionSquare" in sources_by_id.get(product_id, []):
            candidate_sources.append("selectionSquare")
        candidate["sources"] = candidate_sources
        if candidate_sources:
            source_key = str(candidate.get("sourceKey") or "")
            if source_key not in candidate_sources:
                source_key = next((key for key in candidate_sources if key in source_order), "selectionSquare")
            candidate["sourceKey"] = source_key
            candidate["sourceUrl"] = source_urls[source_key]
        candidate["selectionMode"] = "category" if category_logic_enabled else "general"
        category_ids = [_category_id_scalar(value) for value in candidate.get("categoryIds", [])]
        category_ids = category_ids if len(category_ids) == 3 and all(category_ids) else []
        candidate["leafCategoryId"] = category_ids[-1] if len(category_ids) >= 3 else ""
        candidate["sourceCategorySelection"] = {}
        category_result = category_results.get(product_id, {})
        merged_row = merged_raw.get(product_id, {})
        source_categories = merged_row.get("_sourceCategories") if isinstance(merged_row.get("_sourceCategories"), list) else []
        verified_paths = _verified_source_category_paths(source_categories)
        selected_path = next((
            path for path in verified_paths if _source_category_path_satisfies_settings(path, settings)
        ), [])
        source_category = next((
            item for item in source_categories if isinstance(item, dict)
            and item.get("categoryIds") == selected_path
        ), {}) if selected_path else {}
        candidate["sourceCategoryName"] = str(source_category.get("name") or "")
        candidate["sourceCategoryId"] = str(source_category.get("id") or "")
        candidate["sourceCategoryLevel"] = int(source_category.get("level") or 0)
        candidate["sourceCategoryIds"] = source_category.get("categoryIds", [])
        source_level_sufficient = source_category_satisfies_general_exclusions(product_id) and not merged_row.get("_sourceLevelExcluded") and not merged_row.get("_categoryAmbiguous")
        source_category_ids = [str(value) for value in candidate.get("sourceCategoryIds", []) if str(value)]
        candidate["categoryStatus"] = (
            "ambiguous" if merged_row.get("_categoryAmbiguous") else
            "source_level" if source_level_sufficient else "unknown"
        )
        candidate["categorySource"] = str(category_result.get("categorySource") or (
            merged_row.get("_categoryEvidence", {}).get("categorySource", "")
            if candidate["leafCategoryId"] else ""
        ))
        candidate["categoryConfidence"] = (
            "exact" if candidate["leafCategoryId"] else
            "source_level" if source_category else "unresolved"
        )
        if merged_row.get("_sourceLevelExcluded"):
            candidate["leafCategoryId"] = ""
            candidate["categoryStatus"] = "unknown"
            candidate["categoryConfidence"] = "source_level"
        if not source_level_sufficient:
            candidate["categoryError"] = str(category_result.get("reason") or (
                "来源页面分类筛选未能确认，商品详情类目不能代替来源筛选"
            ))

    category_filtered_ids: set[str] = set()
    if category_gate_enabled:
        invalid_category = [candidate for candidate in candidates if candidate.get("categoryStatus") != "source_level"]
        for candidate in invalid_category:
            candidate["analysisRejected"] = True
            reason = str(candidate.get("categoryError") or "来源页面类目筛选未确认")
            candidate["thresholdReasons"] = [
                reason if reason.startswith("命中已核验的来源一级排除类目") else
                f"类目冲突：{reason}" if candidate.get("categoryStatus") == "ambiguous" else
                f"类目未知：{reason}"
            ]
            candidate["status"] = candidate["thresholdReasons"][0]
            candidate["cartAdded"] = False
            candidate["skipped"] = True
            rejected.append(candidate)
            category_filtered_ids.add(str(candidate.get("productId") or ""))
        candidates = [candidate for candidate in candidates if candidate.get("categoryStatus") == "source_level"]
    if category_logic_enabled and settings.get("categoryMode") == "manual":
        selected_category_ids = _manual_category_selection_ids(settings)
        outside_manual_scope = [
            candidate for candidate in candidates
            if not _candidate_matches_manual_categories(candidate, selected_category_ids)
        ]
        for candidate in outside_manual_scope:
            candidate["analysisRejected"] = True
            candidate["thresholdReasons"] = ["不属于手动选择的类目范围"]
            candidate["status"] = candidate["thresholdReasons"][0]
            candidate["cartAdded"] = False
            candidate["skipped"] = True
            rejected.append(candidate)
            category_filtered_ids.add(str(candidate.get("productId") or ""))
        candidates = [candidate for candidate in candidates if _candidate_matches_manual_categories(candidate, selected_category_ids)]
    # 排除类目是当前账号最终生效设置的一部分；通用模式和品类模式都先应用它，
    # 这样账号个性化选品可以在手动/自动品类范围上继续排除不需要的类目。
    excluded_by_category = [
        candidate for candidate in candidates
        if excluded_category_ids and _candidate_matches_manual_categories(candidate, excluded_category_ids)
    ]
    for candidate in excluded_by_category:
        candidate["analysisRejected"] = True
        candidate["thresholdReasons"] = ["命中当前设置中排除的类目"]
        candidate["status"] = candidate["thresholdReasons"][0]
        candidate["cartAdded"] = False
        candidate["skipped"] = True
        rejected.append(candidate)
        category_filtered_ids.add(str(candidate.get("productId") or ""))
    if excluded_by_category:
        excluded_ids = {str(candidate.get("productId") or "") for candidate in excluded_by_category}
        candidates = [candidate for candidate in candidates if str(candidate.get("productId") or "") not in excluded_ids]
    quota_path = settings.get("_quotaCategoryPath") if isinstance(settings.get("_quotaCategoryPath"), list) else []
    if quota_path:
        matching_quota: list[Dict[str, Any]] = []
        for candidate in candidates:
            category_ids = candidate.get("categoryIds") if isinstance(candidate.get("categoryIds"), list) else []
            source_ids = candidate.get("sourceCategoryIds") if isinstance(candidate.get("sourceCategoryIds"), list) else []
            exact_match = len(category_ids) >= len(quota_path) and all(
                _category_id_matches(str(category_ids[index]), str(expected))
                for index, expected in enumerate(quota_path)
            )
            source_match = candidate.get("categoryStatus") == "source_level" and len(source_ids) >= len(quota_path) and all(
                _category_id_matches(str(source_ids[index]), str(expected))
                for index, expected in enumerate(quota_path)
            )
            if exact_match or source_match:
                matching_quota.append(candidate)
            else:
                candidate["analysisRejected"] = True
                candidate["thresholdReasons"] = ["商品类目未能精确匹配本条品类配额"]
                candidate["status"] = candidate["thresholdReasons"][0]
                candidate["cartAdded"] = False
                candidate["skipped"] = True
                rejected.append(candidate)
        candidates = matching_quota
    _candidate_quality_scores(candidates, settings)
    for candidate in candidates:
        base_score = _number(candidate.get("analysisScore")) or 0.0
        sources = set(candidate.get("sources") if isinstance(candidate.get("sources"), list) else [])
        boost = min(10.0, (5.0 if "hotSpot" in sources else 0.0) + (5.0 if "allianceRank" in sources else 0.0))
        candidate["hotOrRankBoost"] = boost
        candidate["unifiedScore"] = round(min(100.0, base_score + boost), 1)

    try:
        target_count = max(1, int(settings.get("candidateLimit", 5)))
    except (TypeError, ValueError):
        target_count = 5
    ranked_for_category = sorted(candidates, key=lambda row: -float(_number(row.get("unifiedScore")) or 0))
    high_score_rows = [row for row in ranked_for_category if _number(row.get("unifiedScore")) is not None and float(row["unifiedScore"]) >= 60]
    if not high_score_rows:
        # 当评分覆盖不足以达到 60 分时，按本批 A-67 排名前 N 个作为高分候选。
        high_score_rows = sorted(
            candidates,
            key=lambda row: (
                -(float(_number(row.get("unifiedScore")) or 0.0)),
                str(row.get("productId") or ""),
            ),
        )[:target_count]
    category_stats: Dict[str, Dict[str, Any]] = {}
    for candidate in high_score_rows:
        source_path = candidate.get("sourceCategoryIds") if isinstance(candidate.get("sourceCategoryIds"), list) else []
        category_id = str(source_path[-1] if len(source_path) == 3 else candidate.get("leafCategoryId") or "")
        if not category_id:
            continue
        row = category_stats.setdefault(category_id, {"categoryId": category_id, "highScoreCount": 0, "scoreTotal": 0.0})
        score = _number(candidate.get("unifiedScore"))
        row["highScoreCount"] += 1
        row["scoreTotal"] += float(score) if score is not None else 0.0
    recommendations = []
    for row in category_stats.values():
        row["averageScore"] = round(row.pop("scoreTotal") / max(1, row["highScoreCount"]), 1)
        row["categoryName"] = _category_name_path(category_tree, str(row["categoryId"]))
        row["basis"] = (
            f"高分商品 {row['highScoreCount']} 个，平均统一分 {row['averageScore']} 分"
        )
        recommendations.append(row)
    recommendations.sort(key=lambda row: (-row["highScoreCount"], -row["averageScore"], row["categoryId"]))
    category_enrichment["recommendationBasis"] = (
        "仅统计统一分不低于 60 的商品；若没有商品达到 60 分，则取统一分排名前 "
        f"{target_count} 个商品。只纳入已确认三级页面筛选路径或商品类目 ID 的商品；类目不靠标题推测。"
    )
    category_enrichment["highScoreProductCount"] = len(high_score_rows)
    category_enrichment["recommendations"] = recommendations
    failure_summary = "; ".join(
        f"{item['productId']}：{item['reason']}"
        for item in category_enrichment["failureDetails"][:8]
    )
    if len(category_enrichment["failureDetails"]) > 8:
        failure_summary += f"；其余 {len(category_enrichment['failureDetails']) - 8} 条见完整日志"
    recommendation_summary = "；".join(
        f"{row.get('categoryName') or row['categoryId']}（ID {row['categoryId']}）：{row['basis']}"
        for row in recommendations[:8]
    ) or "无可推荐类目：没有确认三级筛选路径且达到高分统计条件的商品"
    if category_gate_enabled:
        enrichment_summary = (
            f"来源分类筛选统计：读取 {category_enrichment['candidateCount']} 个商品，"
            f"本轮核验 {category_enrichment['processedCount']} 个，待续核验 {category_enrichment['deferredCount']} 个；"
            f"商品行/详情自带完整类目 {category_enrichment['successCount']}，"
            f"来源筛选路径确认 {category_enrichment['sourceLevelCount']}，未确认来源筛选 {category_enrichment['failureCount']}。"
            + (
                "来源分类未确认："
                + "、".join(source_labels[key] for key in sorted(source_category_unavailable))
                + "；具体原因见来源诊断；缺额保留待重试。"
                if source_category_unavailable else ""
            )
            + (f"未确认明细：{failure_summary}。" if failure_summary else "")
            + f"推荐依据：{recommendation_summary}。"
        )
    else:
        enrichment_summary = (
            f"通用数据模式类目识别统计：读取 {category_enrichment['candidateCount']} 个商品，"
            f"本轮核验 {category_enrichment['processedCount']} 个，待续核验 {category_enrichment['deferredCount']} 个；"
            f"列表/详情含完整三级类目 {category_enrichment['successCount']} 个，"
            f"来源路径已确认 {category_enrichment['sourceLevelCount']} 个，类目仍未知 {category_enrichment['failureCount']} 个；"
            "来源路径不足以判断排除类目的商品仍会淘汰。"
            + (f"通用设置排除类目过滤 {len(excluded_by_category)} 个商品。" if excluded_by_category else "")
            + f"推荐依据：{recommendation_summary}。"
        )
    category_enrichment["summary"] = enrichment_summary
    report_path = _save_category_enrichment_report(category_enrichment)
    category_enrichment["reportPath"] = report_path
    category_enrichment["summary"] = f"{enrichment_summary}完整日志：{report_path}"
    if report_path.lower().endswith(".json"):
        try:
            Path(report_path).write_text(
                json.dumps(category_enrichment, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    _report_operation_progress(
        progress,
        "类目补充与推荐完成",
        index=category_enrichment["candidateCount"],
        total=category_enrichment["candidateCount"],
        percent=100,
        message=enrichment_summary,
    )
    category_rank = {str(row["categoryId"]): index for index, row in enumerate(recommendations)}
    if category_logic_enabled:
        candidates.sort(key=lambda row: (
            preferred_rank.get(str(row.get("productId") or ""), len(preferred_rank)),
            category_rank.get(str((row.get("sourceCategoryIds") or [row.get("leafCategoryId") or ""])[-1]), len(category_rank)),
            -(float(_number(row.get("unifiedScore")) or 0)),
        ))
    else:
        candidates.sort(key=lambda row: (
            preferred_rank.get(str(row.get("productId") or ""), len(preferred_rank)),
            -(float(_number(row.get("unifiedScore")) or 0)),
            str(row.get("productId") or ""),
        ))
    for index, candidate in enumerate(candidates):
        candidate["cardIndex"] = index
        candidate["selected"] = True

    parsed_count = sum(int(item.get("parsedCandidateCount") or 0) for item in source_results)
    source_product_count = len(set().union(*source_membership.values())) if source_membership else 0
    passed_ids = {str(candidate.get("productId") or "") for candidate in candidates}
    rejected_ids = {str(candidate.get("productId") or "") for candidate in rejected}
    for source_result in source_results:
        source_key = str(source_result.get("sourceKey") or "")
        source_ids = source_membership.get(source_key, set())
        source_result["passedCandidateCount"] = len(source_ids.intersection(passed_ids))
        source_result["weightedHitCount"] = len(
            source_ids.intersection(passed_ids).intersection({
                str(candidate.get("productId") or "") for candidate in candidates
                if float(_number(candidate.get("hotOrRankBoost")) or 0) > 0
            })
        )
        source_result["categoryFilteredCount"] = len(source_ids.intersection(category_filtered_ids))
        source_result["thresholdRejectedCount"] = len(source_ids.intersection(rejected_ids))
    category_enrichment["selectionMode"] = "category" if category_logic_enabled else "general"
    category_enrichment["excludedCategoryIds"] = sorted(excluded_category_ids)
    category_enrichment["excludedCategoryCount"] = len(excluded_by_category)
    category_enrichment["sourceStatistics"] = {
        str(item.get("sourceKey") or ""): {
            "collected": int(item.get("productCount") or 0),
            "passed": int(item.get("passedCandidateCount") or 0),
            "weightedHits": int(item.get("weightedHitCount") or 0),
            "categoryFiltered": int(item.get("categoryFilteredCount") or 0),
        }
        for item in source_results
    }
    if report_path.lower().endswith(".json"):
        try:
            Path(report_path).write_text(
                json.dumps(category_enrichment, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    local_warnings = list(warnings)
    local_warnings.append(enrichment_summary)
    if source_product_count and not parsed_count:
        local_warnings.append("可见来源读到了商品 ID，但没有解析到可评分的商品行数据；未回退选品广场，本轮停止候选生成。")
    if deferred_ids:
        local_warnings.append(f"本轮有 {len(deferred_ids)} 个商品待续核验；未将其判为字段缺失或筛选失败。")
    result = {
        "ok": True,
        "candidateCount": len(candidates),
        "candidates": candidates,
        "rejectedCandidates": rejected,
        "warnings": list(dict.fromkeys(local_warnings + parse_messages)),
        "recommendedCategories": recommendations,
        "categoryEnrichment": category_enrichment,
        "selectionStrategy": "visible_priority_sources",
        "sourceProductCount": source_product_count,
        "sourceStatistics": {
            str(item.get("sourceKey") or ""): {
                "collected": int(item.get("productCount") or 0),
                "passed": int(item.get("passedCandidateCount") or 0),
                "weightedHits": int(item.get("weightedHitCount") or 0),
                "categoryFiltered": int(item.get("categoryFilteredCount") or 0),
            }
            for item in source_results
        },
    }
    return _attach_priority_source_results(result, source_results)


class _MaterialListCollector:
    """按请求生命周期收集列表结果，避免在较早请求仍未完成时读取中间响应。"""

    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings
        self.sequence = 0
        self.request_sequences: Dict[int, int] = {}
        self.request_objects: Dict[int, Any] = {}
        self.request_events: list[Dict[str, Any]] = []
        self.request_event_by_key: Dict[int, Dict[str, Any]] = {}
        self.request_started_monotonic: Dict[int, float] = {}
        self.pending: set[int] = set()
        self.successes: list[tuple[int, Any]] = []
        self.errors: list[tuple[int, Any, str]] = []
        self.login_errors: list[tuple[int, Any, str]] = []
        self.failures: list[tuple[int, str]] = []
        self.category_mismatch_count = 0
        self.search_keyword_mismatch_count = 0
        self.last_activity_at = time.time()

    @staticmethod
    def _is_material_request(request: Any) -> bool:
        try:
            parsed = urlparse(str(getattr(request, "url", "")))
            return (
                parsed.hostname == "buyin.jinritemai.com"
                and parsed.path.rstrip("/").endswith("/pc/selection/common/material_list")
            )
        except Exception:
            return False

    def _next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def on_request(self, request: Any) -> None:
        if not self._is_material_request(request):
            return
        key = id(request)
        sequence = self._next_sequence()
        self.request_sequences[key] = sequence
        self.request_objects[key] = request
        self.pending.add(key)
        self.request_started_monotonic[key] = time.monotonic()
        try:
            parsed_url = urlparse(str(getattr(request, "url", "")))
            host = parsed_url.hostname or ""
            path = parsed_url.path[:200]
        except Exception:
            host, path = "", ""
        try:
            method = str(getattr(request, "method", "未知"))[:12]
        except Exception:
            method = "未知"
        event = {
            "sequence": sequence,
            "method": method,
            "host": host,
            "path": path,
            "startedAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "completion": "pending",
            "httpStatus": None,
            "businessCode": None,
            "businessMessage": "",
            "categoryMatched": None,
            "searchKeywordMatched": None,
            "queryMatched": None,
            "failure": "",
        }
        self.request_events.append(event)
        self.request_event_by_key[key] = event
        self.last_activity_at = time.time()

    def _sequence_for_response(self, response: Any) -> int:
        request = getattr(response, "request", None)
        key = id(request) if request is not None else -1
        sequence = self.request_sequences.get(key)
        if sequence is not None:
            return sequence
        # 兼容测试桩及 CDP 订阅开始前已发出的响应：视作当前时点的独立完成响应。
        sequence = self._next_sequence()
        if request is not None and self._is_material_request(request):
            self.request_sequences[key] = sequence
        return sequence

    def on_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        if request is None or not self._is_material_request(request):
            return
        sequence = self._sequence_for_response(response)
        self.last_activity_at = time.time()
        event = self.request_event_by_key.get(id(request)) if request is not None else None
        status = getattr(response, "status", None)
        if callable(status):
            try:
                status = status()
            except Exception:
                status = None
        if event is not None:
            event["httpStatus"] = status if isinstance(status, int) and 100 <= status <= 599 else None
            event["responseAt"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        try:
            result = response.json()
        except Exception:
            self.failures.append((sequence, "material_list 响应体无法解析"))
            if event is not None:
                event["jsonParseError"] = "material_list 响应体无法解析"
            return

        is_matching_category = _response_matches_category(response, self.settings)
        is_matching_search_keyword = _response_matches_search_keyword(response, self.settings)
        is_matching_query = is_matching_category and is_matching_search_keyword
        if event is not None:
            event["categoryMatched"] = is_matching_category
            event["searchKeywordMatched"] = is_matching_search_keyword
            event["queryMatched"] = is_matching_query
            if isinstance(result, dict):
                code = result.get("code")
                event["businessCode"] = str(code)[:40] if code is not None else None
                message = result.get("msg")
                event["businessMessage"] = _safe_diagnostic_text(message, 180) if message else ""
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                rows = data.get("summary_promotions") if isinstance(data.get("summary_promotions"), list) else None
                if rows is not None:
                    event["productCount"] = len(rows)
        if not is_matching_category:
            self.category_mismatch_count += 1
        if not is_matching_search_keyword:
            self.search_keyword_mismatch_count += 1

        if isinstance(result, dict) and result.get("code") not in (None, 0, "0"):
            code = result.get("code")
            diagnostics = _material_list_response_diagnostics(response, result)
            message = str(result.get("msg") or result.get("message") or "")
            is_login_error = (
                str(code) in {"-2", "10001", "401", "403"}
                or re.search(r"未登录|未登陆|登录失效|登陆失效|login required|unauthorized", message, re.I)
            )
            if is_login_error:
                self.login_errors.append((sequence, code, diagnostics))
            # 只有当前类目与关键词的 11001 才进入冷却；旧关键词异步请求会单独记录并丢弃。
            if (str(code) == "11001" and is_matching_query) or (is_matching_query and not is_login_error) or is_login_error:
                self.errors.append((sequence, code, diagnostics))
                if event is not None:
                    event["diagnostic"] = diagnostics
            return
        if is_matching_query:
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, dict) and isinstance(data.get("summary_promotions"), list):
                self.successes.append((sequence, result))

    def _finish_request(self, request: Any, error: Optional[str] = None) -> None:
        key = id(request)
        if self.request_objects.get(key) is not request:
            return
        sequence = self.request_sequences.get(key)
        if sequence is None:
            return
        self.pending.discard(key)
        self.last_activity_at = time.time()
        event = self.request_event_by_key.get(key)
        if event is not None:
            event["completion"] = "failed" if error else "finished"
            event["finishedAt"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
            started = self.request_started_monotonic.get(key)
            if started is not None:
                event["durationMs"] = max(0, round((time.monotonic() - started) * 1000))
        if error:
            safe_error = _safe_diagnostic_text(error, 240)
            self.failures.append((sequence, safe_error))
            if event is not None:
                event["failure"] = safe_error

    def on_request_finished(self, request: Any) -> None:
        self._finish_request(request)

    def on_request_failed(self, request: Any) -> None:
        failure = getattr(request, "failure", None)
        if callable(failure):
            try:
                failure = failure()
            except Exception:
                failure = None
        self._finish_request(request, str(failure or "material_list 请求失败"))

    def mark(self) -> int:
        return self.sequence

    def pending_since(self, mark: int) -> bool:
        return any(self.request_sequences.get(key, 0) > mark for key in self.pending)

    def request_count_since(self, mark: int) -> int:
        """返回标记之后观察到的 material_list 请求数。"""
        return sum(1 for sequence in self.request_sequences.values() if sequence > mark)

    def pending_count_since(self, mark: int) -> int:
        """返回标记之后仍未结束的 material_list 请求数。"""
        return sum(1 for key in self.pending if self.request_sequences.get(key, 0) > mark)

    def events_since(self, mark: int) -> list[Dict[str, Any]]:
        """返回脱敏后的 material_list 生命周期事件，不包含请求参数或请求头。"""
        return [dict(event) for event in self.request_events if int(event.get("sequence") or 0) > mark]

    def latest_error_since(self, mark: int) -> Optional[tuple[int, Any, str]]:
        entries = [entry for entry in self.errors if entry[0] > mark]
        return max(entries, key=lambda entry: entry[0]) if entries else None

    def rate_limit_error_since(self, mark: int) -> Optional[tuple[int, Any, str]]:
        entries = [entry for entry in self.errors if entry[0] > mark and str(entry[1]) == "11001"]
        return max(entries, key=lambda entry: entry[0]) if entries else None

    def login_error_since(self, mark: int) -> Optional[tuple[int, Any, str]]:
        entries = [entry for entry in self.login_errors if entry[0] > mark]
        return max(entries, key=lambda entry: entry[0]) if entries else None

    def latest_success_since(self, mark: int) -> Optional[tuple[int, Any]]:
        entries = [entry for entry in self.successes if entry[0] > mark]
        return max(entries, key=lambda entry: entry[0]) if entries else None

    def latest_failure_since(self, mark: int) -> Optional[tuple[int, str]]:
        entries = [entry for entry in self.failures if entry[0] > mark]
        return max(entries, key=lambda entry: entry[0]) if entries else None

    def latest_outcome_since(self, mark: int) -> tuple[str, Any]:
        error = self.latest_error_since(mark)
        success = self.latest_success_since(mark)
        failure = self.latest_failure_since(mark)
        latest_success_sequence = success[0] if success else 0
        if error and (not success or error[0] > latest_success_sequence):
            return "error", error
        if failure and (not success or failure[0] > latest_success_sequence):
            return "failure", failure
        if success:
            return "success", success
        if error:
            return "error", error
        if failure:
            return "failure", failure
        return "none", None


def _selection_failure_dom_snapshot(page: Page) -> Dict[str, Any]:
    """只采集类目菜单和筛选控件的可见状态，不保存整页文本或浏览器存储。"""
    return page.evaluate("""() => {
      const visible = (node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
      };
      const dropdowns = Array.from(document.querySelectorAll('.aurora-cascader-dropdown'))
        .filter(node => visible(node) && !String(node.className).includes('aurora-slide-up-leave') &&
          !node.classList.contains('aurora-select-dropdown-hidden'))
        .slice(0, 5)
        .map((dropdown, dropdownIndex) => ({
          dropdownIndex,
          columns: Array.from(dropdown.querySelectorAll('.aurora-cascader-menu')).slice(0, 4).map((column, columnIndex) => ({
            columnIndex,
            items: Array.from(column.querySelectorAll('.aurora-cascader-menu-item[data-path-key]')).slice(0, 40).map(item => ({
              id: String(item.getAttribute('data-path-key') || '').slice(0, 100),
              name: String(item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 100),
              checked: item.getAttribute('aria-checked') || '',
              expanded: item.getAttribute('aria-expanded') || ''
            }))
          }))
        }));
      const controls = Array.from(document.querySelectorAll('.merch-filter-cate-cascader-item'))
        .filter(visible).slice(0, 8).map(node => ({
          text: String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
          expanded: node.getAttribute('aria-expanded') || '',
          value: String(node.getAttribute('title') || '').slice(0, 120)
        }));
      return { title: String(document.title || '').slice(0, 120), dropdowns, controls };
    }""")


def _is_buyin_selection_filter_click_timeout(
    page_url: str,
    dom_snapshot: Dict[str, Any],
    error: BaseException,
) -> bool:
    """确认百应选品库 DOM 已就绪且失败确实来自筛选控件点击。"""
    try:
        parsed = urlparse(page_url)
    except Exception:
        return False
    if parsed.hostname != "buyin.jinritemai.com" or not parsed.path.startswith(
        "/dashboard/merch-picking-library"
    ):
        return False
    if not isinstance(dom_snapshot, dict) or str(dom_snapshot.get("title") or "").strip() != "选品库":
        return False

    message = str(error).lower()
    if "locator.click" not in message or ("timeout" not in message and "超时" not in message):
        return False
    filter_targets = (
        "close-circle",
        "searchinput__input",
        "aurora-select-item-option",
        "combine-select-content",
        "筛选选项",
        "筛选控件",
        "自动投千川",
    )
    return any(target in message for target in filter_targets)


def _classify_selection_failure(
    page_url: str,
    login_state: Dict[str, Any],
    dom_snapshot: Dict[str, Any],
    request_events: Sequence[Dict[str, Any]],
    expected_branch_ids: set[str],
    visible_ids: set[str],
    error: BaseException,
) -> str:
    """结合当前页面和 DOM 判断失败阶段，避免登录探测 unknown 覆盖真实筛选错误。"""
    if isinstance(error, CommissionFilterUnconfirmed):
        return "COMMISSION_FILTER_UNCONFIRMED"
    code_11001_events = [event for event in request_events if str(event.get("businessCode")) == "11001"]
    if code_11001_events:
        platform_message = " ".join(str(event.get("businessMessage") or "") for event in code_11001_events)
        if re.search(r"网络(?:不稳定|错误|异常|故障|超时)|网络连接(?:失败|异常)", platform_message, re.I):
            return "BAIYING_NETWORK_ERROR"
        if re.search(r"限流|频控|选品(?:太多|过多)|(?:请求|访问|操作)(?:过多|太多|频繁|太频繁|过于频繁)|rate.?limit|too many requests|frequency control", platform_message, re.I):
            return "BAIYING_RATE_LIMIT"
        return "BAIYING_11001_UNCONFIRMED"
    if "SEARCHBOX_ZERO_EXHAUSTED" in str(error):
        return "SEARCHBOX_ZERO_EXHAUSTED"
    if "SEARCHBOX_CONTROL_EXHAUSTED" in str(error):
        return "SEARCHBOX_CONTROL_EXHAUSTED"
    if _is_buyin_selection_filter_click_timeout(page_url, dom_snapshot, error):
        return "CATEGORY_FILTER_APPLY_FAILED"
    if login_state.get("state") != "logged_in":
        return "LOGIN_OR_PAGE_STATE_UNCONFIRMED"
    if any(event.get("completion") == "pending" for event in request_events):
        return "MATERIAL_LIST_REQUEST_PENDING"
    if expected_branch_ids and expected_branch_ids.isdisjoint(visible_ids):
        return "EXPECTED_CATEGORY_NOT_RENDERED"
    return "CATEGORY_FILTER_APPLY_FAILED"


def _write_selection_failure_diagnostic(
    page: Page,
    settings: Dict[str, Any],
    collector: _MaterialListCollector,
    mark: int,
    error: BaseException,
) -> Dict[str, str]:
    """类目筛选失败时保存脱敏 JSON 与当前视口截图，失败诊断不能覆盖原始异常。"""
    diagnostic_id = f"selection-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    diagnostic_dir = root / "diagnostics" / "selection"
    json_path = diagnostic_dir / f"{diagnostic_id}.json"
    screenshot_path = diagnostic_dir / f"{diagnostic_id}.png"

    try:
        parsed_url = urlparse(page.url)
        safe_url = f"{parsed_url.scheme}://{parsed_url.hostname or ''}{parsed_url.path}"
    except Exception:
        safe_url = ""
    try:
        login_state = _login_page_state(page, "baiying")
    except Exception as state_error:
        login_state = {"state": "unknown", "message": type(state_error).__name__}
    try:
        prompt_kind, prompt_text = _visible_baiying_prompt(page)
        prompt = {"kind": prompt_kind, "text": _safe_diagnostic_text(prompt_text, 300)}
    except Exception as prompt_error:
        prompt = {"kind": "unknown", "text": type(prompt_error).__name__}
    try:
        dom_snapshot = _selection_failure_dom_snapshot(page)
    except Exception as snapshot_error:
        dom_snapshot = {"error": f"DOM 快照读取失败：{type(snapshot_error).__name__}"}
    if not isinstance(dom_snapshot, dict):
        dom_snapshot = {"error": "DOM 快照读取返回了非对象数据", "title": _safe_diagnostic_text(dom_snapshot, 120)}
    try:
        ready_state = str(page.evaluate("() => document.readyState") or "unknown")
    except Exception as ready_error:
        ready_state = f"读取失败：{type(ready_error).__name__}"
    try:
        searchbox_selector_hit_count: Optional[int] = page.locator(SEARCH_KEYWORD_INPUT_SELECTOR).count()
    except Exception:
        searchbox_selector_hit_count = None

    request_events = collector.events_since(mark)
    expected = settings.get("categorySelection") if isinstance(settings.get("categorySelection"), dict) else {}
    expected_ids = {
        str(item)
        for level in ("level1", "level2", "level3")
        for item in (expected.get(level) if isinstance(expected.get(level), list) else [])
        if str(item)
    }
    expected_branch_ids = {
        str(item)
        for level in ("level2", "level3")
        for item in (expected.get(level) if isinstance(expected.get(level), list) else [])
        if str(item)
    }
    visible_ids = {
        str(item.get("id"))
        for dropdown in dom_snapshot.get("dropdowns", []) if isinstance(dropdown, dict)
        for column in dropdown.get("columns", []) if isinstance(column, dict)
        for item in column.get("items", []) if isinstance(item, dict) and item.get("id")
    }
    classification = _classify_selection_failure(
        safe_url,
        login_state,
        dom_snapshot,
        request_events,
        expected_branch_ids,
        visible_ids,
        error,
    )

    payload: Dict[str, Any] = {
        "diagnosticId": diagnostic_id,
        "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "classification": classification,
        "error": _safe_diagnostic_text(error, 1_000),
        "pageUrl": safe_url,
        "pageTitle": str(dom_snapshot.get("title") or "")[:160],
        "readyState": ready_state,
        "searchboxSelector": SEARCH_KEYWORD_INPUT_SELECTOR,
        "searchboxSelectorHitCount": searchbox_selector_hit_count,
        "loginState": login_state,
        "visiblePrompt": prompt,
        "expectedCategoryIds": sorted(expected_ids),
        "expectedBranchCategoryIds": sorted(expected_branch_ids),
        "visibleCategoryIds": sorted(visible_ids),
        "menuSnapshot": dom_snapshot,
        "materialListRequests": request_events,
        "requestCount": collector.request_count_since(mark),
        "pendingRequestCount": collector.pending_count_since(mark),
        "privacy": "未保存 Cookie、请求头、请求体、URL 查询参数或整页文本。",
    }
    try:
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
            payload["screenshotPath"] = str(screenshot_path)
        except Exception as screenshot_error:
            payload["screenshotError"] = type(screenshot_error).__name__
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "diagnosticId": diagnostic_id,
            "classification": classification,
            "jsonPath": str(json_path),
            "screenshotPath": str(screenshot_path) if payload.get("screenshotPath") else "未能保存",
        }
    except Exception as write_error:
        return {
            "diagnosticId": diagnostic_id,
            "classification": classification,
            "jsonPath": "未能保存：" + type(write_error).__name__,
            "screenshotPath": str(screenshot_path) if payload.get("screenshotPath") else "未能保存",
        }



def _listen_to_material_list(page: Page, collector: _MaterialListCollector) -> None:
    page.on("request", collector.on_request)
    page.on("response", collector.on_response)
    page.on("requestfinished", collector.on_request_finished)
    page.on("requestfailed", collector.on_request_failed)


def _remove_material_list_listeners(page: Page, collector: _MaterialListCollector) -> None:
    for event, callback in (
        ("request", collector.on_request),
        ("response", collector.on_response),
        ("requestfinished", collector.on_request_finished),
        ("requestfailed", collector.on_request_failed),
    ):
        try:
            page.remove_listener(event, callback)
        except Exception:
            pass


def _wait_for_material_list_settled(
    page: Page,
    collector: _MaterialListCollector,
    mark: int,
    deadline: float,
    quiet_seconds: float,
) -> str:
    """返回 settled、idle、pending 或 unstable；仅 settled 可消费成功列表。"""
    while time.time() < deadline:
        if _visible_baiying_prompt(page)[0] == "manual_verification":
            raise RuntimeError(
                "百应要求手动完成滑块验证。请在该账号对应的托管 Chrome 中手动完成验证，"
                "然后再次点击自动选品恢复；当前阶段检查点会保留。"
            )
        if collector.rate_limit_error_since(mark):
            return "error"
        if collector.pending_since(mark):
            page.wait_for_timeout(150)
            continue
        if collector.sequence <= mark:
            page.wait_for_timeout(150)
            continue
        if time.time() - collector.last_activity_at >= quiet_seconds:
            return "settled"
        page.wait_for_timeout(150)
    if collector.pending_since(mark):
        return "pending"
    if collector.sequence <= mark:
        return "idle"
    return "unstable"


def _wait_for_material_list_settled_bounded(
    page: Page,
    collector: _MaterialListCollector,
    mark: int,
    *,
    window_seconds: float = 8,
    max_windows: int = 3,
    quiet_seconds: float = 1.2,
) -> str:
    """分窗口继续观察同一 material_list 请求；绝不在它仍 pending 时重发。"""
    status = "idle"
    for _ in range(max_windows):
        status = _wait_for_material_list_settled(
            page, collector, mark, time.time() + window_seconds, quiet_seconds=quiet_seconds
        )
        if status not in {"pending", "unstable"}:
            return status
    return status


def search_baiying_candidates(
    page: Page,
    settings: Dict[str, Any],
    preserve_product_ids: Optional[Sequence[str]] = None,
    max_attempts: int = 2,
) -> Dict[str, Any]:
    """触发百应搜索并等待 material_list 请求全部落地；只有无请求时才允许补发。"""
    collector = _MaterialListCollector(settings)
    _listen_to_material_list(page, collector)
    try:
        prepared_mark = collector.mark()
        _apply_search_keyword(page, settings, collector=collector, collector_mark=prepared_mark)
        search = page.get_by_text("搜 索", exact=True).first
        if search.count() == 0:
            search = page.get_by_text("搜索", exact=True).first
        if search.count() == 0:
            raise RuntimeError("百应选品广场未找到搜索按钮")
        retry_backoff_ms = (2_000,)
        overall_deadline = time.time() + 90
        try:
            attempt_limit = min(2, max(1, int(max_attempts)))
        except (TypeError, ValueError):
            attempt_limit = 5
        attempts_made = 0
        for attempt in range(attempt_limit):
            attempts_made = attempt + 1
            attempt_start = prepared_mark if attempt == 0 else collector.mark()
            search.click(timeout=8_000, no_wait_after=True)
            deadline = min(time.time() + 18, overall_deadline)
            status = _wait_for_material_list_settled(
                page, collector, attempt_start, deadline, quiet_seconds=1.0
            )
            rate_limit_error = collector.rate_limit_error_since(attempt_start)
            if rate_limit_error:
                _mark_page_rate_limited(id(page))
                detail = rate_limit_error[2]
                raise RuntimeError(
                    f"百应 material_list 接口返回业务错误 11001；已停止本次筛选，"
                    f"并进入 {BAIYING_RATE_LIMIT_COOLDOWN_SECONDS} 秒查询冷却以避免连续请求。"
                    f"百应响应诊断：{detail[:320]}"
                )
            outcome, value = collector.latest_outcome_since(attempt_start)
            if status == "settled" and outcome == "success":
                payload = value[1]
                break
            if status in {"pending", "unstable"}:
                raise RuntimeError(
                    "百应 material_list 请求在等待期限内仍未全部完成或结果尚未稳定；"
                    "已停止本次查询，未消费中间候选，也未补发请求。"
                )
            if status == "failure" or outcome in {"error", "failure"}:
                detail = value[2] if outcome == "error" else value[1]
                raise RuntimeError(
                    f"百应自动请求 {attempts_made} 次后仍未返回商品列表："
                    f"{str(detail)[:160]}；已停止自动补发。"
                )
            if attempt + 1 < attempt_limit and time.time() < overall_deadline:
                # 只有本次点击完全没有发出 material_list 请求时，才允许一次短退避补发。
                delay = min(retry_backoff_ms[attempt], int((overall_deadline - time.time()) * 1000))
                if delay > 0:
                    page.wait_for_timeout(delay)
        else:
            payload = None
        if payload is None:
            error = collector.latest_error_since(prepared_mark)
            if error:
                raise RuntimeError(
                    f"百应自动请求 {attempts_made} 次后仍未返回商品列表：{error[2][:160]}"
                )
            expected_ids = sorted(_expected_category_ids(settings))
            if collector.category_mismatch_count and expected_ids:
                raise RuntimeError(
                    f"百应自动请求 {attempts_made} 次后仍未收到与所选类目匹配的商品列表；"
                    f"已忽略 {collector.category_mismatch_count} 个其他类目或未带类目 ID 的响应（目标 ID：{'、'.join(expected_ids)}）"
                )
            raise RuntimeError(f"百应自动请求 {attempts_made} 次后仍未捕获商品列表响应")
    finally:
        _remove_material_list_listeners(page, collector)

    _store_cached_material_list(id(page), _selection_filter_signature(settings), payload)
    return _candidate_result(payload, settings, preserve_product_ids)


def _resolve_saved_category_ids(selected: set[str], known: set[str]) -> tuple[set[str], set[str]]:
    """把旧缓存短 ID 解析为当前带父路径的百应 ID。"""
    resolved: set[str] = set()
    missing: set[str] = set()
    for selected_id in selected:
        selected_tail = selected_id.rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
        matches = {
            known_id for known_id in known
            if known_id == selected_id or known_id.rsplit("__RC_CASCADER_SPLIT__", 1)[-1] == selected_tail
        }
        exact = {known_id for known_id in matches if known_id == selected_id}
        if exact:
            resolved.update(exact)
        elif matches:
            # 旧缓存可能同时留有短 ID 和完整路径 ID；优先采用百应当前的完整路径。
            resolved.add(max(matches, key=len))
        else:
            missing.add(selected_id)
    return resolved, missing


def _category_targets_for_root(
    root_node: Dict[str, Any], selected_second: set[str], selected_third: set[str]
) -> list[tuple[int, Dict[str, Any], Optional[str]]]:
    """保留明确选择的深度；未指定下级时只应用一级入口。"""
    second_nodes = [
        node for node in root_node.get("children", [])
        if isinstance(node, dict) and str(node.get("id", ""))
    ]
    explicit_second = [node for node in second_nodes if str(node.get("id", "")) in selected_second]
    if not explicit_second:
        return [(1, root_node, None)]
    targets: list[tuple[int, Dict[str, Any], Optional[str]]] = []
    for second_node in explicit_second:
        second_id = str(second_node.get("id", ""))
        third_nodes = [
            node for node in second_node.get("children", [])
            if isinstance(node, dict) and str(node.get("id", ""))
        ]
        explicit_third = [node for node in third_nodes if str(node.get("id", "")) in selected_third]
        if explicit_third:
            targets.extend((3, node, second_id) for node in explicit_third)
        else:
            targets.append((2, second_node, None))
    return targets


def _wait_category_item_present(page: Page, category_id: str, timeout_ms: int = 5_000) -> None:
    """等待当前可见级联菜单加载出指定类目，而非仅等待任意类目占位数据。"""
    page.wait_for_function("""(categoryId) => {
      const dropdowns = Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).filter(node =>
        node.offsetWidth && node.offsetHeight && getComputedStyle(node).visibility === 'visible' &&
        !String(node.className).includes('aurora-slide-up-leave') &&
        !node.classList.contains('aurora-select-dropdown-hidden'));
      return dropdowns.some(dropdown =>
        Array.from(dropdown.querySelectorAll('.aurora-cascader-menu-item[data-path-key]')).some(item =>
          item.getAttribute('data-path-key') === categoryId));
    }""", arg=category_id, timeout=timeout_ms, polling=CATEGORY_PAGE_POLLING_MS)


def _category_dropdown_for_item(page: Page, category_id: str) -> Optional[Any]:
    """从多个可见 portal 中定位实际包含目标类目的级联菜单。"""
    dropdowns = page.locator(ACTIVE_CATEGORY_DROPDOWN_SELECTOR)
    target_tail = category_id.rsplit("__RC_CASCADER_SPLIT__", 1)[-1]
    fallback = None
    for dropdown_index in range(dropdowns.count()):
        dropdown = dropdowns.nth(dropdown_index)
        if dropdown.locator(_category_item_selector(category_id)).count():
            return dropdown
        if fallback is None and target_tail and target_tail != category_id:
            matches = dropdown.locator(_category_item_selector(target_tail, suffix=True))
            if matches.count() == 1:
                fallback = dropdown
    return fallback


def _wait_category_item_checked(page: Page, category_id: str, timeout_ms: int = 4_000) -> None:
    """等待百应真实菜单状态变为已勾选，避免用固定短延时读取旧状态。"""
    page.wait_for_function("""(categoryId) => {
      const dropdowns = Array.from(document.querySelectorAll('.aurora-cascader-dropdown')).filter(node =>
        node.offsetWidth && node.offsetHeight && getComputedStyle(node).visibility === 'visible' &&
        !String(node.className).includes('aurora-slide-up-leave') &&
        !node.classList.contains('aurora-select-dropdown-hidden'));
      return dropdowns.some(dropdown =>
        Array.from(dropdown.querySelectorAll('.aurora-cascader-menu-item[data-path-key]')).some(item =>
          item.getAttribute('data-path-key') === categoryId && item.getAttribute('aria-checked') === 'true'));
    }""", arg=category_id, timeout=timeout_ms, polling=CATEGORY_PAGE_POLLING_MS)


def _checked_platform_category_ids(page: Page) -> set[str]:
    values = page.evaluate("""() => Array.from(document.querySelectorAll(
      '.aurora-cascader-menu-item[data-path-key][aria-checked="true"]'))
      .map(item => String(item.getAttribute('data-path-key') || '').trim()).filter(Boolean)""")
    return {str(value) for value in values if str(value)} if isinstance(values, list) else set()


def _wait_platform_categories_unchecked(
    page: Page, category_ids: set[str], timeout_ms: int = 5_000
) -> None:
    """等待清空动作反映到所有可见或隐藏的百应级联 portal。"""
    page.wait_for_function("""(categoryIds) => {
      const wanted = new Set(categoryIds);
      return !Array.from(document.querySelectorAll(
        '.aurora-cascader-menu-item[data-path-key][aria-checked="true"]'))
        .some(item => wanted.has(String(item.getAttribute('data-path-key') || '').trim()));
    }""", arg=sorted(category_ids), timeout=timeout_ms, polling=CATEGORY_PAGE_POLLING_MS)


def _category_path_metadata(tree: Sequence[Any]) -> Dict[str, tuple[str, str, str]]:
    """把当前动态类目树转换为「平台选择 ID → 一级/二级/三级」索引。"""
    result: Dict[str, tuple[str, str, str]] = {}
    for root in tree:
        if not isinstance(root, dict):
            continue
        root_id = str(root.get("id", ""))
        if root_id:
            result[root_id] = (root_id, "", "")
        for second in root.get("children", []):
            if not isinstance(second, dict):
                continue
            second_id = str(second.get("id", ""))
            if root_id and second_id:
                result[second_id] = (root_id, second_id, "")
            for third in second.get("children", []):
                if not isinstance(third, dict):
                    continue
                third_id = str(third.get("id", ""))
                if root_id and second_id and third_id:
                    path_id = _category_path_id(third_id, second_id)
                    result[path_id] = (root_id, second_id, path_id)
    return result


def _category_tree_paths(tree: Sequence[Any]) -> list[tuple[str, str, str]]:
    paths: list[tuple[str, str, str]] = []
    for root in tree:
        if not isinstance(root, dict):
            continue
        first = _category_id_scalar(root.get("id"))
        for second_node in (root.get("children") if isinstance(root.get("children"), list) else []):
            if not isinstance(second_node, dict):
                continue
            second = _category_id_scalar(second_node.get("id"))
            for third_node in (second_node.get("children") if isinstance(second_node.get("children"), list) else []):
                if not isinstance(third_node, dict):
                    continue
                third = _category_id_scalar(third_node.get("id"))
                if first and second and third:
                    paths.append((first, second, _category_path_id(third, second)))
    return paths


def _category_id_matches(actual: str, expected: str) -> bool:
    return actual == expected or actual.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1] == expected.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]


def _normalize_exact_category_path(values: Sequence[Any], category_tree: Sequence[Any]) -> list[str]:
    if len(values) != 3:
        return []
    supplied = [_category_id_scalar(value) for value in values]
    if not all(supplied):
        return []
    paths = _category_tree_paths(category_tree)
    if not paths:
        # 平台明确给出完整三级路径时可以保留；单个末级 ID 不能在没有树时补全。
        return supplied
    matches = {path for path in paths if all(_category_id_matches(path[index], supplied[index]) for index in range(3))}
    return list(next(iter(matches))) if len(matches) == 1 else []


def _resolve_single_category_id(value: Any, category_tree: Sequence[Any]) -> tuple[list[str], int]:
    category_id = _category_id_scalar(value)
    if not category_id:
        return [], 0
    matches: set[tuple[tuple[str, ...], int]] = set()
    for path in _category_tree_paths(category_tree):
        for level in range(1, 4):
            if _category_id_matches(path[level - 1], category_id):
                matches.add((path[:level], level))
    if len(matches) != 1:
        return [], 0
    path, level = next(iter(matches))
    return list(path), level


def _row_category_evidence(row: Dict[str, Any], product_id: str, category_tree: Sequence[Any], source: str) -> Dict[str, Any]:
    """只从精确商品行的类目字段取证；冲突、0 和单个无法映射的 ID 都不放行。"""
    if str(row.get("product_id") or "").strip() != product_id:
        return {"status": "unknown", "categoryIds": [], "reason": "来源商品 ID 不一致"}
    base = row.get("base_model") if isinstance(row.get("base_model"), dict) else {}
    info = base.get("product_info") if isinstance(base.get("product_info"), dict) else {}
    nodes = [("product_info", info), ("base_model", base), ("row", row)]
    candidates: list[tuple[list[str], str]] = []
    field_seen = False
    for prefix, node in nodes:
        for key in ("category", "category_info", "categoryInfo"):
            raw = node.get(key)
            if not isinstance(raw, dict):
                continue
            field_seen = True
            for names in (("first", "second", "third"), ("first_cate_id", "second_cate_id", "third_cate_id"),
                          ("firstCategoryId", "secondCategoryId", "thirdCategoryId")):
                if all(name in raw for name in names):
                    path = _normalize_exact_category_path([raw[name] for name in names], category_tree)
                    if path:
                        candidates.append((path, f"{prefix}.{key}"))
        for key in ("categoryIds", "category_ids", "categoryPath", "category_path"):
            raw = node.get(key)
            if raw is None:
                continue
            field_seen = True
            if isinstance(raw, list):
                path = _normalize_exact_category_path(raw, category_tree)
                if path:
                    candidates.append((path, f"{prefix}.{key}"))
            elif isinstance(raw, str) and key in {"categoryPath", "category_path"}:
                parts = [part.strip() for part in re.split(r"\s*[,/>]\s*", raw) if part.strip()]
                path = _normalize_exact_category_path(parts, category_tree)
                if path:
                    candidates.append((path, f"{prefix}.{key}"))
        for names in (("first_cate_id", "second_cate_id", "third_cate_id"),
                      ("first_category_id", "second_category_id", "third_category_id")):
            if any(name in node for name in names):
                field_seen = True
                path = _normalize_exact_category_path([node.get(name) for name in names], category_tree)
                if path:
                    candidates.append((path, f"{prefix}.{names[0]}"))
        for key in ("category_id", "categoryId", "alli_cate_id", "alliCateId"):
            if key not in node:
                continue
            field_seen = True
            path, level = _resolve_single_category_id(node.get(key), category_tree)
            if level == 3:
                candidates.append((path, f"{prefix}.{key}"))
    distinct = {tuple(path) for path, _ in candidates}
    if len(distinct) > 1:
        return {"status": "ambiguous", "categoryIds": [], "reason": "同一商品行出现互相冲突的三级类目 ID"}
    if len(distinct) == 1:
        path, field = candidates[0]
        return {"status": "known", "categoryIds": path, "categorySource": source,
                "categoryConfidence": "exact", "categoryField": field}
    return {"status": "unknown", "categoryIds": [],
            "reason": "来源类目字段无效或无法唯一映射" if field_seen else "来源商品行未提供类目字段",
            "categoryConfidence": "unresolved"}


def _clear_platform_category_root(
    page: Page, root_id: str, category_ids: set[str]
) -> None:
    """百应通过一级控件里的「不限」清空该控件，不把已选叶子当成可反向切换的复选框。"""
    root = page.locator(f".merch-filter-cate-cascader-item.business_id_{root_id}")
    if not root.count():
        raise RuntimeError(f"清理旧类目时找不到百应一级类目 ID {root_id}")
    control = root.first.locator(".combine-select-content")
    # 清空后再次检查时，按当前一级类目下真实存在的二级 ID 定位弹层。
    # 不使用“最后一个可见 portal”推断当前菜单：百应会保留多个隐藏级联 portal，
    # 重挂载期间通用等待可能命中旧 portal 并超时。
    expected_item_id = next((
        category_id for category_id in sorted(category_ids)
        if category_id != root_id and CATEGORY_PATH_SEPARATOR not in category_id
    ), None)
    if not expected_item_id:
        raise RuntimeError(f"无法找到百应一级类目 {root_id} 下的二级类目 ID，无法安全清理")
    try:
        for attempt in range(2):
            dropdown = _open_category_dropdown(
                page, root_id, control, expected_item_id=expected_item_id
            )
            first_column = dropdown.locator(".aurora-cascader-menu").first
            clear_item = first_column.locator('.aurora-cascader-menu-item[data-path-key=""]')
            if not clear_item.count():
                raise RuntimeError(f"百应一级类目 {root_id} 菜单没有「不限」选项，无法隔离类目查询")
            _click_category_item(clear_item.first)
            # 百应已在点击「不限」时清空状态；直接观察所有 portal 的真实勾选值。
            # 清空后重开菜单会触发异步重建，二级项短暂消失，不能作为清空失败的依据。
            try:
                _wait_platform_categories_unchecked(page, category_ids, timeout_ms=5_000)
            except PlaywrightTimeoutError:
                pass
            remaining = sorted(_checked_platform_category_ids(page) & category_ids)
            if not remaining:
                return
            if attempt == 1:
                raise RuntimeError(
                    f"百应「不限」后一级类目 {root_id} 仍有已选项：{', '.join(remaining[:5])}"
                )
    finally:
        _close_category_dropdown(page, root_id, control)


def _clear_extra_platform_categories(
    page: Page,
    roots_by_id: Dict[str, Dict[str, Any]],
    selected_second: set[str],
    selected_third: set[str],
    full_tree: Sequence[Any],
) -> None:
    known_second = {
        str(second.get("id", ""))
        for root in full_tree if isinstance(root, dict)
        for second in root.get("children", []) if isinstance(second, dict) and str(second.get("id", ""))
    }
    known_third = {
        str(third.get("id", ""))
        for root in full_tree if isinstance(root, dict)
        for second in root.get("children", []) if isinstance(second, dict)
        for third in second.get("children", []) if isinstance(third, dict) and str(third.get("id", ""))
    }
    selected_second, _ = _resolve_saved_category_ids(selected_second, known_second)
    selected_third, _ = _resolve_saved_category_ids(selected_third, known_third)
    desired: set[str] = set()
    for root_id, root in roots_by_id.items():
        for level, node, parent_id in _category_targets_for_root(root, selected_second, selected_third):
            category_id = str(node.get("id", ""))
            desired.add(category_id if level < 3 else _category_path_id(category_id, str(parent_id or "")))

    metadata = _category_path_metadata(full_tree)
    current = _checked_platform_category_ids(page)
    extras = sorted(current - desired)
    unknown = [category_id for category_id in extras if category_id not in metadata]
    if unknown:
        raise RuntimeError(
            "百应页面存在当前类目树无法识别的已选类目：" + ", ".join(unknown[:8]) +
            "；为避免跨类目混入候选，已停止本次查询。"
        )
    roots_to_clear = sorted({metadata[category_id][0] for category_id in extras})
    for root_id in roots_to_clear:
        category_ids = {
            category_id for category_id, path in metadata.items() if path[0] == root_id
        }
        _clear_platform_category_root(page, root_id, category_ids)


def apply_category_settings(
    page: Page, settings: Dict[str, Any], replace_existing: bool = False
) -> list[str]:
    """按保存的精确层级选择，在百应类目级联筛选器中勾选对应节点。"""
    selection = settings.get("categorySelection", {})
    tree = settings.get("categoryTree", [])
    if not isinstance(selection, dict) or not isinstance(tree, list):
        return []
    selected_roots = {str(value) for value in selection.get("level1", []) if str(value)}
    selected_second = {str(value) for value in selection.get("level2", []) if str(value)}
    selected_third = {str(value) for value in selection.get("level3", []) if str(value)}
    if not selected_roots:
        return []

    warnings: list[str] = []
    roots_by_id = {
        str(node.get("id")): node for node in tree
        if isinstance(node, dict) and str(node.get("id", "")) in selected_roots
    }
    known_second = {
        str(child.get("id"))
        for node in tree if isinstance(node, dict)
        for child in node.get("children", []) if isinstance(child, dict) and str(child.get("id", ""))
    }
    known_third = {
        str(grandchild.get("id"))
        for node in tree if isinstance(node, dict)
        for child in node.get("children", []) if isinstance(child, dict)
        for grandchild in child.get("children", []) if isinstance(grandchild, dict) and str(grandchild.get("id", ""))
    }
    selected_second, missing_second = _resolve_saved_category_ids(selected_second, known_second)
    selected_third, missing_third = _resolve_saved_category_ids(selected_third, known_third)
    if missing_second:
        raise RuntimeError(
            f"所选二级类目已不在最新百应目录中：{', '.join(sorted(missing_second))}"
            f"（收到二级目录 {len(known_second)} 项）"
        )
    if missing_third:
        raise RuntimeError(
            f"所选三级类目已不在最新百应目录中：{', '.join(sorted(missing_third))}"
            f"（收到三级目录 {len(known_third)} 项）"
        )
    if replace_existing:
        _clear_extra_platform_categories(
            page, roots_by_id, selected_second, selected_third, tree
        )
    selection_mutated = False
    for root_id in sorted(selected_roots):
        if root_id not in roots_by_id:
            raise RuntimeError(f"所选一级类目 {root_id} 已不在最新百应目录中，请刷新类目后重新保存")
        if not re.fullmatch(r"[\w-]+", root_id):
            raise RuntimeError("百应一级类目 ID 格式不正确")
        root = page.locator(f".merch-filter-cate-cascader-item.business_id_{root_id}")
        if not root.count():
            raise RuntimeError(f"百应页面当前找不到一级类目 ID {root_id}")
        control = root.first.locator(".combine-select-content")
        root_node = roots_by_id[root_id]
        second_nodes = [
            node for node in root_node.get("children", [])
            if isinstance(node, dict) and str(node.get("id", "")) and str(node.get("name", ""))
        ]
        if not second_nodes:
            raise RuntimeError(f"百应一级类目 {root_id} 当前没有已捕获的二级目录；无法确认类目筛选")
        targets = _category_targets_for_root(root_node, selected_second, selected_third)
        if not targets:
            warnings.append(
                f"一级类目「{root_node.get('name', root_id)}」没有可应用的下级类目，已跳过该类目。"
            )
            _close_category_dropdown(page, root_id, control)
            continue
        dropdown: Optional[Any] = None
        second_nodes_by_id = {
            str(node.get("id", "")): node
            for node in root_node.get("children", [])
            if isinstance(node, dict) and str(node.get("id", ""))
        }
        try:
            for target_level, target_node, parent_second_id in targets:
                target_id = str(target_node["id"])
                target_name = str(target_node.get("name", target_id))
                if target_level == 1:
                    # 一级入口若只展开菜单而未应用筛选，外层请求 ID 回读会拒收旧列表。
                    control.click(timeout=8_000)
                    selection_mutated = True
                    continue
                if target_level == 2:
                    if dropdown is None:
                        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=target_id)
                    item = _category_item_by_id(dropdown.locator(".aurora-cascader-menu").first, target_id) if dropdown else None
                    if item is None:
                        raise RuntimeError(f"百应类目「{target_name}」已不在当前页面选项中，请刷新类目")
                    item_checked = _category_item_is_checked(item)
                    if item_checked is None:
                        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=target_id)
                        item = _category_item_by_id(dropdown.locator(".aurora-cascader-menu").first, target_id)
                        item_checked = _category_item_is_checked(item) if item is not None else None
                        if item_checked is None:
                            raise RuntimeError(f"百应二级类目「{target_name}」状态暂时无法确认，未重复点击")
                    if item_checked is False:
                        _click_category_item(item)
                        selection_mutated = True
                        # 百应勾选后会自动收起级联菜单；重开一次核验，并将这个已打开菜单
                        # 留给本一级类目下的下一项复用，避免每项都重复关闭、打开一级菜单。
                        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=target_id)
                        verified = _category_item_by_id(dropdown.locator(".aurora-cascader-menu").first, target_id) if dropdown else None
                        if verified is None:
                            raise RuntimeError(f"百应菜单重新打开后未找到二级类目「{target_name}」")
                        try:
                            _wait_category_item_checked(page, target_id)
                        except PlaywrightTimeoutError:
                            raise RuntimeError(f"百应没有应用二级类目「{target_name}」")
                    continue

                second_id = str(parent_second_id or "")
                second_node = second_nodes_by_id.get(second_id, {})
                second_name = str(second_node.get("name", second_id))
                category_path = " → ".join((
                    f"{root_node.get('name', root_id)}（{root_id}）",
                    f"{second_name}（{second_id}）",
                    f"{target_name}（{target_id}）",
                ))
                try:
                    if dropdown is None:
                        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
                    dropdown, item3 = _open_category_branch_with_retry(
                        page, root_id, second_id, target_id, control, dropdown,
                        allow_second_level_retry=not selection_mutated,
                    )
                    item_checked = _category_item_is_checked(item3)
                    if item_checked is None:
                        dropdown = _open_category_dropdown(page, root_id, control, expected_item_id=second_id)
                        dropdown, item3 = _open_category_branch_with_retry(
                            page, root_id, second_id, target_id, control, dropdown,
                            allow_second_level_retry=not selection_mutated,
                        )
                        item_checked = _category_item_is_checked(item3)
                        if item_checked is None:
                            raise RuntimeError(f"三级类目 ID {target_id} 状态暂时无法确认，未重复点击")
                    if item_checked is False:
                        dropdown, _verified = _click_category_and_confirm(
                            page, root_id, second_id, target_id, control, dropdown, item3
                        )
                        selection_mutated = True
                except Exception as error:
                    raise RuntimeError(
                        f"百应类目路径「{category_path}」应用失败：{error}"
                    ) from error
        finally:
            _close_category_dropdown(page, root_id, control)
    return warnings


def apply_filter_settings(
    page: Page,
    settings: Dict[str, Any],
    preserve_other_filters: bool = False,
    replace_category_selection: bool = False,
) -> list[str]:
    values = _effective_selection_filters(settings)
    warnings: list[str] = []
    group_values: Dict[str, Any] = {}
    for value in values.values() if isinstance(values, dict) else []:
        if isinstance(value, dict):
            group_values.update(value)
    configured_fields = {
        label: configured for label, configured in group_values.items()
        if ((isinstance(configured, list) and any(str(item) for item in configured)) or (configured and not isinstance(configured, list)))
    }
    raw_features = values.get("features", []) if isinstance(values, dict) else []
    selected_features = set(str(item) for item in raw_features) if isinstance(raw_features, list) else {
        str(label) for label, enabled in raw_features.items() if enabled
    } if isinstance(raw_features, dict) else set()
    filter_catalog = settings.get("filterCatalog") if isinstance(settings.get("filterCatalog"), dict) else {}
    catalog = filter_catalog.get("filters") if isinstance(filter_catalog.get("filters"), dict) else None
    feature_catalog = filter_catalog.get("features") if isinstance(filter_catalog.get("features"), list) else None
    signature = _selection_filter_signature(settings)
    applied_signature = _page_applied_filter_signature(page)
    if applied_signature == signature:
        return warnings

    if preserve_other_filters:
        # 同一批次的多个末级类目查询共享完全相同的非类目筛选；百应切换类目会替换 MendelCid，
        # 不需要每次重置整页并重新点选销售额、流量等条件。
        if replace_category_selection:
            warnings.extend(apply_category_settings(page, settings, replace_existing=True))
        else:
            warnings.extend(apply_category_settings(page, settings))
        try:
            page.evaluate("(signature) => { window.__touguangAppliedFilterSettings = signature }", signature)
        except Exception:
            pass
        return warnings

    if (catalog is None or feature_catalog is None) and (configured_fields or selected_features):
        # 兼容未带本地缓存目录的旧客户端；新客户端会把已同步目录随设置传入。
        live_catalog = collect_filter_catalog(page)
        catalog = live_catalog.get("filters", {})
        feature_catalog = live_catalog.get("features", [])
    else:
        catalog = catalog or {}
        feature_catalog = feature_catalog or []

    # 仅当配置发生变化时清理一次旧状态；同一套设置后续预览不再反复重置。
    reset = page.get_by_text("重置筛选", exact=True).last
    if reset.count() and reset.is_visible():
        reset.click(force=True, timeout=3_000, no_wait_after=True)
        # 百应重置会异步清空类目与商品列表；100ms 内再次点击类目可能被重置请求覆盖。
        page.wait_for_timeout(1_000)
    # 空值代表“不增加该筛选”；只操作用户实际配置的字段，避免逐项打开无效筛选器。
    for label, configured in configured_fields.items():
        selected = configured if isinstance(configured, list) else [configured] if configured else []
        selected = [str(item) for item in selected if str(item)]
        if not selected:
            continue
        cached_options = catalog.get(label) if isinstance(catalog, dict) else None
        if label == "佣金区间":
            try:
                _apply_configured_filter_field(page, label, selected, cached_options, warnings)
            except Exception as error:
                if _selection_error_requires_pause(error):
                    raise
                warnings.append(f"百应佣金区间未能应用；继续按商品实际佣金率筛选：{_safe_diagnostic_text(error, 180)}")
                continue
        else:
            _apply_configured_filter_field(page, label, selected, cached_options, warnings)
        page.wait_for_timeout(80)
    try:
        _apply_square_commission_range(page, settings)
    except Exception as error:
        if isinstance(error, CommissionFilterUnconfirmed):
            raise
        if _selection_error_requires_pause(error):
            raise
        warnings.append(f"选品广场佣金区间未确认应用；将按商品实际佣金率在客户端核验：{_safe_diagnostic_text(error, 180)}")
    if feature_catalog:
        states = page.evaluate("""(labels) => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const exactLeaf = (label) => Array.from(document.querySelectorAll('body *')).find((node) => {
            if (clean(node.innerText) !== label || node.children.length > 0) return false;
            const rect = node.getBoundingClientRect();
            const heading = Array.from(document.querySelectorAll('body *')).find((item) =>
              clean(item.innerText) === '特色货品' && item.children.length === 0);
            const end = Array.from(document.querySelectorAll('body *')).find((item) =>
              clean(item.innerText) === '为你推荐以下商品' && item.children.length === 0);
            return rect.width > 0 && (!heading || rect.top >= heading.getBoundingClientRect().top) &&
              (!end || rect.top < end.getBoundingClientRect().top);
          });
          return labels.map((label) => {
            const node = exactLeaf(label);
            if (!node) return { label, found: false };
            const rect = node.getBoundingClientRect();
            let active = false;
            for (let parent = node, i = 0; parent && i < 5; parent = parent.parentElement, i++) {
              const cls = String(parent.className || '').toLowerCase();
              active = active || parent.getAttribute('aria-checked') === 'true' || parent.getAttribute('aria-pressed') === 'true' ||
                parent.getAttribute('data-selected') === 'true' || /active|selected|checked/.test(cls);
            }
            return { label, found: true, active, x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
          });
        }""", feature_catalog)
        for state in states if isinstance(states, list) else []:
            if not isinstance(state, dict) or not state.get("found"):
                warnings.append(f"百应特色货品筛选项「{state.get('label', '')}」当前不可用。")
                continue
            should_select = state.get("label") in selected_features
            if should_select and not bool(state.get("active")):
                page.mouse.click(float(state["x"]), float(state["y"]))
                page.wait_for_timeout(100)

    # 先完成佣金、体验分、带货情况等平台筛选，再逐个应用类目。
    # 百应类目级联会触发一次新的 material_list 请求；把类目放在最后，
    # 可以避免每次切换类目时把前面已经确认的筛选条件重置或覆盖。
    if replace_category_selection:
        warnings.extend(apply_category_settings(page, settings, replace_existing=True))
    else:
        warnings.extend(apply_category_settings(page, settings))
    # 类目级联会异步提交筛选状态。等待其稳定后再触发搜索，避免连续提交导致 11001。
    page.wait_for_timeout(1_200)
    try:
        page.evaluate("(signature) => { window.__touguangAppliedFilterSettings = signature }", signature)
    except Exception:
        pass
    return warnings


def _apply_one_category_and_collect_candidates(
    page: Page,
    settings: Dict[str, Any],
    force_refresh: bool = False,
    preserve_product_ids: Optional[Sequence[str]] = None,
    preserve_other_filters: bool = False,
    replace_category_selection: bool = False,
    include_source_ids: bool = False,
) -> Dict[str, Any]:
    """复用百应筛选响应；等响应短暂稳定后取最后一份，避免重复搜索和中间结果竞态。"""
    signature = _selection_filter_signature(settings)
    page_key = id(page)
    if force_refresh:
        _clear_page_material_cache(page_key)
    current_search_keyword = _read_search_keyword(page)
    search_keyword_matches = current_search_keyword == _configured_search_keyword(settings)
    cached_payload = (
        _get_cached_material_list(page_key, signature)
        if search_keyword_matches else None
    )
    collector = _MaterialListCollector(settings)

    try:
        applied_signature = page.evaluate("() => window.__touguangAppliedFilterSettings || ''")
    except Exception:
        applied_signature = ""
    filters_already_applied = applied_signature == signature and search_keyword_matches
    if cached_payload is not None:
        if applied_signature == signature and not _expected_category_ids(settings):
            rendered = _rendered_material_payload(page)
            if rendered:
                _store_cached_material_list(page_key, signature, rendered)
                return _candidate_result(rendered, settings, preserve_product_ids, include_source_ids)
        # 多类目扫描时，页面只显示最后一个类目。复用同一条件的短时结果，
        # 不再为了预览重置全部筛选并重复逐项操作；加车仍按来源类目强制刷新并核验商品 ID。
        return _candidate_result(cached_payload, settings, preserve_product_ids, include_source_ids)
    cooldown_remaining = _page_rate_limit_remaining(page_key)
    if cooldown_remaining:
        raise RuntimeError(
            f"百应刚返回 11001，当前账号处于自动冷却期，还需约 {cooldown_remaining} 秒；"
            "本次没有再次请求百应，已成功类目的候选缓存仍可复用。"
        )

    _listen_to_material_list(page, collector)
    apply_mark = collector.mark()
    try:
        setattr(page, "_touguang_filter_retry_collector", (collector, apply_mark))
    except Exception:
        pass
    try:
        if not search_keyword_matches:
            _apply_search_keyword(page, settings, collector=collector, collector_mark=apply_mark)
        if preserve_other_filters:
            if replace_category_selection:
                warnings = apply_filter_settings(
                    page, settings, preserve_other_filters=True,
                    replace_category_selection=True,
                )
            else:
                warnings = apply_filter_settings(page, settings, preserve_other_filters=True)
        elif replace_category_selection:
            warnings = apply_filter_settings(page, settings, replace_category_selection=True)
        else:
            warnings = apply_filter_settings(page, settings)
        # 百应筛选控件会触发异步重渲染；在选项变更后重新核对关键词，
        # 避免页面恢复旧的推荐词并将它带入后续 material_list 请求。
        _apply_search_keyword(page, settings, collector=collector, collector_mark=apply_mark)
        collection_status = "idle"
        if not filters_already_applied:
            # pending 时不重复发送 material_list，按三个有界窗口继续等同一请求，避免请求重叠/触发 11001。
            collection_status = _wait_for_material_list_settled_bounded(
                page, collector, apply_mark, window_seconds=8, max_windows=3, quiet_seconds=1.2
            )
            if collection_status == "pending":
                raise RuntimeError(
                    "MATERIAL_LIST_REQUEST_PENDING：筛选后列表请求连续等待 3 个窗口（最多 24 秒）仍未结束；"
                    "未重复发送请求，也未继续候选搜索。"
                )
            if collection_status == "unstable":
                raise RuntimeError(
                    "CATEGORY_FILTER_APPLY_FAILED：筛选后商品列表在 3 个等待窗口（最多 24 秒）仍未稳定；"
                    "未消费中间候选，也未继续候选搜索。"
                )
    except Exception as error:
        request_count = collector.request_count_since(apply_mark)
        pending_count = collector.pending_count_since(apply_mark)
        rate_limit_error = collector.rate_limit_error_since(apply_mark)
        login_error = collector.login_error_since(apply_mark)
        if login_error:
            error = BaiyingLoginRequired(
                "百应列表接口返回用户未登录；当前账号的网页登录态或浏览器 profile 无法确认，"
                "未继续执行佣金确认、类目采集或候选搜索。"
            )
        if rate_limit_error:
            _mark_page_rate_limited(page_key)
            request_status = (
                f"百应应用筛选时 material_list 返回 11001，账号已进入 "
                f"{BAIYING_RATE_LIMIT_COOLDOWN_SECONDS} 秒冷却；观察到 {request_count} 个请求，"
                f"其中 {pending_count} 个尚未结束；未继续执行候选搜索。"
            )
        elif login_error:
            request_status = (
                f"百应应用筛选时 material_list 返回未登录业务错误；观察到 {request_count} 个请求，"
                f"其中 {pending_count} 个尚未结束；未继续执行候选搜索。"
            )
        elif request_count:
            request_status = (
                f"应用筛选阶段已观察到 {request_count} 个 material_list 请求，"
                f"其中 {pending_count} 个尚未结束；未继续执行候选搜索。"
            )
        else:
            request_status = "应用筛选阶段尚未观察到 material_list 请求，未继续执行候选搜索。"
        diagnostic = _write_selection_failure_diagnostic(page, settings, collector, apply_mark, error)
        artifact_status = (
            f"诊断编号 {diagnostic.get('diagnosticId', '未知')}；"
            f"诊断类型 {diagnostic.get('classification', '未知')}；"
            f"诊断文件 {diagnostic.get('jsonPath', '未能保存')}；"
            f"截图 {diagnostic.get('screenshotPath', '未能保存')}。"
        )
        if isinstance(error, CommissionFilterUnconfirmed):
            raise CommissionFilterUnconfirmed(
                f"COMMISSION_FILTER_UNCONFIRMED：选品广场佣金筛选状态不明；{request_status}{artifact_status}"
            ) from error
        raise RuntimeError(f"{error}；{request_status}{artifact_status}") from error
    finally:
        _remove_material_list_listeners(page, collector)
        try:
            delattr(page, "_touguang_filter_retry_collector")
        except Exception:
            pass

    rate_limit_error = collector.rate_limit_error_since(apply_mark)
    if rate_limit_error:
        _mark_page_rate_limited(page_key)
        raise RuntimeError(
            f"百应应用筛选阶段的 material_list 接口返回业务错误 11001；"
            f"当前账号进入 {BAIYING_RATE_LIMIT_COOLDOWN_SECONDS} 秒查询冷却，"
            f"本次不再补发搜索请求。百应响应诊断：{rate_limit_error[2][:320]}"
        )
    login_error = collector.login_error_since(apply_mark)
    if login_error:
        diagnostic = _write_selection_failure_diagnostic(
            page, settings, collector, apply_mark,
            BaiyingLoginRequired("百应列表接口返回用户未登录；未继续执行候选搜索。"),
        )
        raise BaiyingLoginRequired(
            "百应列表接口返回用户未登录；未继续执行候选搜索。"
            f"诊断编号 {diagnostic.get('diagnosticId', '未知')}；"
            f"诊断文件 {diagnostic.get('jsonPath', '未能保存')}。"
        )
    outcome, value = collector.latest_outcome_since(apply_mark)
    if collection_status == "settled" and outcome == "success":
        # 类目筛选存在旧卡片过渡期；以请求体明确包含当前类目 ID 的响应作为候选来源，
        # 不把尚未刷新完成的旧 DOM 卡片归到新类目下。
        payload = value[1]
    else:
        payload = None
    if payload is not None:
        _store_cached_material_list(page_key, signature, payload)
        result = _candidate_result(payload, settings, preserve_product_ids, include_source_ids)
        result["warnings"] = list(dict.fromkeys(warnings + result.get("warnings", [])))
        return result
    if collector.category_mismatch_count and _expected_category_ids(settings):
        warnings.append(
            f"已忽略 {collector.category_mismatch_count} 个未匹配当前类目 ID 的旧商品列表响应，正在按当前筛选重新搜索。"
        )
    if collector.search_keyword_mismatch_count:
        warnings.append(
            f"已忽略 {collector.search_keyword_mismatch_count} 个搜索词与当前设置不一致的商品列表响应，正在按当前搜索词重新查询。"
        )
    if collection_status == "settled" and outcome in {"error", "failure"}:
        detail = value[2] if outcome == "error" else value[1]
        try:
            result = search_baiying_candidates(
                page, settings, preserve_product_ids, max_attempts=2
            )
        except Exception as retry_error:
            if "11001" in str(retry_error):
                raise
            raise RuntimeError(
                "CATEGORY_FILTER_APPLY_FAILED：筛选后的 material_list 已结束但未返回可用结果；"
                f"已在同账号按当前筛选自动重试候选查询，仍未成功。原响应：{str(detail)[:160]}；"
                f"重试原因：{retry_error}"
            ) from retry_error
        result["warnings"] = list(dict.fromkeys(
            warnings + ["筛选后的列表请求未返回可用结果，已在同账号按当前条件自动重试查询。"] +
            result.get("warnings", [])
        ))
        return result
    try:
        result = search_baiying_candidates(page, settings, preserve_product_ids)
    except Exception as error:
        if "11001" in str(error):
            if _page_rate_limit_remaining(page_key) <= 0:
                _mark_page_rate_limited(page_key)
            raise RuntimeError(
                f"{error}；当前账号进入 {BAIYING_RATE_LIMIT_COOLDOWN_SECONDS} 秒自动冷却，"
                "期间重复触发不会继续请求百应。"
            ) from error
        raise
    result["warnings"] = list(dict.fromkeys(warnings + result.get("warnings", [])))
    return result


def _category_query_plan(settings: Dict[str, Any]) -> tuple[list[Dict[str, Any]], list[str]]:
    """纯本地构建类目查询计划，并报告会导致父类目静默扩展的层级冲突。"""
    selection = settings.get("categorySelection")
    tree = settings.get("categoryTree")
    if not isinstance(selection, dict) or not isinstance(tree, list):
        return [settings], []

    selected_roots = {str(value) for value in selection.get("level1", []) if str(value)}
    selected_seconds = {str(value) for value in selection.get("level2", []) if str(value)}
    selected_thirds = {str(value) for value in selection.get("level3", []) if str(value)}
    if not selected_roots:
        return [settings], []

    queries: list[Dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    conflicts: list[str] = []
    matched_roots: set[str] = set()
    matched_seconds: set[str] = set()
    matched_thirds: set[str] = set()

    def selected_id_matches(node_id: str, selected_ids: set[str]) -> bool:
        tail = node_id.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]
        return any(value == node_id or value.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1] == tail for value in selected_ids)

    def add_query(path: tuple[str, ...]) -> None:
        allowed_paths = _allowed_paths_for_requested_category(settings, path)
        if not allowed_paths:
            conflicts.append("已选类目落在排除分支或缺少允许的下级：" + "/".join(path))
        for allowed in allowed_paths:
            if allowed in seen:
                continue
            seen.add(allowed)
            query = dict(settings)
            query["categorySelection"] = {
                level: [allowed[index]] if index < len(allowed) else []
                for index, level in enumerate(("level1", "level2", "level3"))
            }
            queries.append(query)

    for root in tree:
        if not isinstance(root, dict) or str(root.get("id", "")) not in selected_roots:
            continue
        matched_roots.add(str(root.get("id", "")))
        root_id = str(root.get("id", ""))
        seconds = [node for node in root.get("children", []) if isinstance(node, dict) and str(node.get("id", ""))]
        explicit_seconds = [node for node in seconds if selected_id_matches(str(node.get("id", "")), selected_seconds)]
        matched_seconds.update(str(node.get("id", "")) for node in explicit_seconds)
        if not explicit_seconds:
            add_query((root_id,))
            continue
        for second in explicit_seconds:
            thirds = [node for node in second.get("children", []) if isinstance(node, dict) and str(node.get("id", ""))]
            explicit_thirds = [node for node in thirds if selected_id_matches(str(node.get("id", "")), selected_thirds)]
            matched_thirds.update(str(node.get("id", "")) for node in explicit_thirds)
            if explicit_thirds:
                for third in explicit_thirds:
                    add_query((root_id, str(second.get("id", "")), str(third.get("id", ""))))
            else:
                add_query((root_id, str(second.get("id", ""))))

    missing_roots = selected_roots - matched_roots
    if missing_roots:
        conflicts.append("已选一级类目未匹配本机目录：" + "、".join(sorted(missing_roots)))
    missing_seconds = {
        selected_id for selected_id in selected_seconds
        if not selected_id_matches(selected_id, matched_seconds)
    }
    if missing_seconds:
        conflicts.append("已选二级类目未匹配已选一级类目：" + "、".join(sorted(missing_seconds)))
    missing_thirds = {
        selected_id for selected_id in selected_thirds
        if not selected_id_matches(selected_id, matched_thirds)
    }
    if missing_thirds:
        conflicts.append("已选三级类目未匹配已选二级类目：" + "、".join(sorted(missing_thirds)))

    if not queries and not conflicts:
        raise RuntimeError("所选类目在本机百应目录中没有可查询的类目，请先同步类目目录")
    return queries, list(dict.fromkeys(conflicts))


def _category_query_settings(settings: Dict[str, Any]) -> list[Dict[str, Any]]:
    """按用户明确选择的层级查询，并在父子选择不一致时 fail closed。"""
    queries, conflicts = _category_query_plan(settings)
    if conflicts:
        path_by_id: Dict[str, str] = {}

        def index_names(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id", ""))
                if node_id:
                    path_by_id[node_id] = str(node.get("name") or node_id)
                index_names(node.get("children", []))

        index_names(settings.get("categoryTree", []))
        planned_paths: list[str] = []
        for query in queries:
            selected = query.get("categorySelection", {})
            ids = [
                str(selected.get(level, [""])[0])
                for level in ("level1", "level2", "level3")
                if selected.get(level)
            ]
            names = [path_by_id.get(category_id, category_id) for category_id in ids]
            planned_paths.append(" / ".join(f"{name}（{category_id}）" for name, category_id in zip(names, ids)))
        raise RuntimeError(
            "类目层级配置冲突，已在百应请求前停止。按原展开规则会产生 "
            f"{len(queries)} 条查询，类目路径计划：{'；'.join(planned_paths)}。"
            f"问题：{'；'.join(conflicts)}。账号设置未修改。"
        )
    return queries


def _candidate_rank(candidate: Dict[str, Any], settings: Dict[str, Any]) -> tuple[int, int, int, int, float]:
    source_order = _enabled_priority_source_order(settings)
    candidate_sources = {
        str(source) for source in candidate.get("sources", [])
        if str(source) in BAIYING_PRIORITY_SOURCES
    }
    source_rank = next(
        (index for index, source in enumerate(source_order) if source in candidate_sources),
        len(source_order),
    )
    pool_rank = candidate.get("poolRank")
    try:
        normalized_pool_rank = int(pool_rank)
    except (TypeError, ValueError):
        normalized_pool_rank = 99
    analysis_score = _number(candidate.get("analysisScore"))
    trend_rank = (
        0 if analysis_score is not None else
        1 if settings.get("trendPreference") == "上升优先" and not bool(candidate.get("trendRising")) else 0
    )
    score = analysis_score
    if score is None:
        score = candidate.get("saleAmount")
    if score is None:
        score = candidate.get("monthSales")
    try:
        normalized_score = float(score or 0)
    except (TypeError, ValueError):
        normalized_score = 0.0
    try:
        metric_coverage = max(0, int(candidate.get("analysisCoverage") or 0))
    except (TypeError, ValueError):
        metric_coverage = 0
    return source_rank, normalized_pool_rank, trend_rank, -normalized_score, -metric_coverage


def _selection_error_requires_pause(error: BaseException) -> bool:
    """账号/浏览器/限流状态不确定时暂停；普通类目查询错误允许跳过该类目。"""
    if isinstance(error, CommissionFilterUnconfirmed):
        return False
    if isinstance(error, CandidateCartSkip):
        return False
    message = str(error).lower()
    if "searchbox_zero_exhausted" in message or "searchbox_control_exhausted" in message:
        return True
    pause_markers = (
        "11001", "冷却", "限流", "频控", "滑块", "验证码", "安全验证", "风控", "登录",
        "captcha", "verification required", "risk control", "login required", "unauthorized", "rate limit",
        "target page", "target closed", "browser has been closed", "context has been closed",
        "page has been closed", "browser disconnected", "connection closed", "disconnected",
        "websocket", "cdp", "远程调试", "浏览器已关闭", "页面已关闭", "连接中断", "连接失败",
        "net::err_", "request failed", "timed out", "timeout", "超时", "等待期限", "尚未完成",
        "未能稳定", "未结束", "pending", "unstable",
        # 商品身份类错误由候选层隔离；只有账号/浏览器状态错误才升级为暂停。
    )
    if any(marker in message for marker in pause_markers):
        return True
    # Playwright 等非预期运行时异常可能意味着页面状态已改变，不能当作单类目业务错误吞掉。
    return not isinstance(error, RuntimeError)


def apply_settings_and_collect_candidates(
    page: Page,
    settings: Dict[str, Any],
    force_refresh: bool = False,
    preserve_product_ids: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """按当前策略读取百应候选；可将热点趋势、联盟榜单、选品广场当前可见商品统一筛选。"""
    if force_refresh:
        _clear_page_material_cache(id(page))
    settings = copy.deepcopy(settings)
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    if thresholds.get("commissionRate") is not None and settings.get("commissionRateRule") != "minimum":
        raise RuntimeError("旧佣金率设置按上限保存；请核对最低值并重新保存后再采集，检查点不改写")
    if settings.get("_visibleSourcesOnly") is True:
        source_membership, source_warnings, source_results, source_candidates = _collect_priority_source_ids(
            page, settings, progress
        )
        source_config = settings.get("sources") if isinstance(settings.get("sources"), dict) else {}
        collector_source = str(settings.get("_collectorSourceKey") or "")
        if source_config.get("selectionSquareEnabled", True) is False or (collector_source and collector_source != "selectionSquare"):
            selection_square_rows = []
            selection_square_result = {"sourceKey": "selectionSquare", "label": "选品广场", "status": "disabled", "productCount": 0}
        else:
            try:
                selection_square_rows, selection_square_result = _collect_selection_square_rows(page, settings, progress)
            except Exception as error:
                if _selection_error_requires_pause(error):
                    raise
                label, _ = BAIYING_SELECTION_SQUARE_SOURCE
                selection_square_rows = []
                selection_square_result = {
                    "sourceKey": "selectionSquare",
                    "label": label,
                    "status": "skipped",
                    "productCount": 0,
                    "parsedCandidateCount": 0,
                    "detail": f"{type(error).__name__}: {str(error)[:900]}",
                }
                source_warnings.append(f"跳过来源读取：{label}页面读取失败；原因：{selection_square_result['detail']}")
        square_ids = {str(row.get("product_id") or "").strip() for row in selection_square_rows}
        square_ids.discard("")
        source_membership["selectionSquare"] = square_ids
        source_candidates["selectionSquare"] = selection_square_rows
        source_warnings.extend(
            str(value) for value in selection_square_result.get("warnings", []) if str(value)
        )
        source_results.append(selection_square_result)
        if settings.get("_candidatePoolCollectOnly") is True:
            return _visible_source_candidate_result(
                page, source_candidates, source_membership, source_results,
                selection_square_rows, selection_square_result, source_warnings, settings,
                preserve_product_ids, progress
            )
        result = _visible_source_candidate_result(
            page, source_candidates, source_membership, source_results,
            selection_square_rows, selection_square_result, source_warnings, settings,
            preserve_product_ids, progress
        )
        _report_operation_progress(
            progress, "当前可见来源候选完成", index=len(result.get("candidates", [])),
            total=len(result.get("candidates", [])), percent=100,
            message=(
                f"三来源统一筛选得到 {len(result.get('candidates', []))} 个候选；"
                f"选品广场实际滚动 {len(selection_square_result.get('pageReads', []))} 次；"
                f"待续核验 {result.get('categoryEnrichment', {}).get('deferredCount', 0)} 个"
            ),
        )
        return result
    # 先完成纯本地类目计划校验，不能因设置冲突而先访问来源页面。
    category_queries = _category_query_settings(settings)
    if preserve_product_ids:
        # 加车检查点恢复只按已保存商品 ID 重映射，不重复抓热点/榜单页面。
        source_membership, source_warnings, source_results = {}, [], []
    else:
        source_membership, source_warnings, source_results, _source_candidates = _collect_priority_source_ids(page, settings, progress)
    settings["_sourceMembership"] = source_membership
    for query_settings in category_queries:
        query_settings["_sourceMembership"] = source_membership
    if len(category_queries) == 1 and category_queries[0].get("categorySelection") == settings.get("categorySelection"):
        selection = category_queries[0].get("categorySelection")
        _report_operation_progress(
            progress, "查询百应类目", index=1, total=1, percent=0,
            message="正在应用类目和筛选条件并读取候选",
        )
        try:
            result = _apply_one_category_and_collect_candidates(
                page, category_queries[0], force_refresh, preserve_product_ids
            )
        except Exception as error:
            if _selection_error_requires_pause(error):
                raise
            category_ids = [
                str(category_id)
                for level in ("level1", "level2", "level3")
                for category_id in (selection.get(level, []) if isinstance(selection, dict) else [])
                if str(category_id)
            ]
            warning = (
                f"跳过失败类目：{' / '.join(category_ids) or '未指定'}；查询阶段失败：{error}；"
                "该类目未返回候选，未执行加车。"
            )
            return {
                "ok": True,
                "candidateCount": 0,
                "candidates": [],
                "rejectedCandidates": [],
                "warnings": list(dict.fromkeys(source_warnings + [warning])),
                "prioritySourceResults": [dict(item) for item in source_results],
                "prioritySourceMatchStatus": "unavailable",
            }
        for candidate in result.get("candidates", []):
            if not str(candidate.get("productId") or "").strip():
                raise RuntimeError("百应候选缺少商品 ID，商品身份无法唯一确认；已停止后续平台操作")
            candidate["sourceCategorySelection"] = selection
        result["warnings"] = list(dict.fromkeys(source_warnings + result.get("warnings", [])))
        result = _attach_priority_source_results(result, source_results)
        _report_operation_progress(
            progress, "百应类目查询完成", index=1, total=1, percent=100,
            message=f"获取候选 {len(result.get('candidates', []))} 个",
        )
        return result

    limit_value = settings.get("_candidatePoolLimit", settings.get("candidateLimit", 5))
    max_limit = 2_000 if settings.get("_candidatePoolCollectOnly") is True or settings.get("_unifiedSourceCollection") is True else 30 if "_candidatePoolLimit" in settings else 10
    try:
        limit = min(max_limit, max(1, int(limit_value)))
    except (TypeError, ValueError):
        limit = 5
    merged: Dict[str, Dict[str, Any]] = {}
    rejected_by_id: Dict[str, Dict[str, Any]] = {}
    warnings: list[str] = list(source_warnings)
    category_names: Dict[str, str] = {}
    successful_category_queries = 0

    def index_category_names(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            category_id = str(node.get("id") or "")
            if category_id:
                category_names[category_id] = str(node.get("name") or category_id)
            index_category_names(node.get("children", []))

    index_category_names(settings.get("categoryTree", []))
    for index, query_settings in enumerate(category_queries):
        query_selection = query_settings.get("categorySelection", {})
        category_ids = [
            str(category_id)
            for level in ("level1", "level2", "level3")
            for category_id in (query_selection.get(level, []) if isinstance(query_selection, dict) else [])
            if str(category_id)
        ]
        category_label = " / ".join(category_names.get(category_id, category_id) for category_id in category_ids) or "未指定类目"
        _report_operation_progress(
            progress,
            "逐类目查询百应候选",
            index=index + 1,
            total=len(category_queries),
            percent=int(index * 100 / len(category_queries)),
            message=f"{category_label}；已完成 {index}/{len(category_queries)} 个类目",
        )
        try:
            result = _apply_one_category_and_collect_candidates(
                page,
                query_settings,
                force_refresh=force_refresh if index == 0 else False,
                preserve_product_ids=preserve_product_ids,
                preserve_other_filters=(
                    index > 0
                    and _can_reuse_applied_non_category_filters(
                        _page_applied_filter_signature(page),
                        query_settings,
                    )
                ),
                replace_category_selection=len(category_queries) > 1,
            )
        except Exception as error:
            category_label = " / ".join(
                f"{category_names.get(category_id, category_id)}（{category_id}）"
                for category_id in category_ids
            ) or "未指定"
            failure = (
                f"百应多类目查询第 {index + 1}/{len(category_queries)} 项，类目 {category_label} 失败：{error}"
            )
            if _selection_error_requires_pause(error):
                prior_skips = "；此前已跳过：" + "；".join(warnings) if warnings else ""
                raise RuntimeError(f"{failure}；属于账号、限流或页面状态异常，已暂停后续平台请求。{prior_skips}") from error
            warning = f"跳过失败类目：{failure}；继续查询后续类目"
            warnings.append(warning)
            _report_operation_progress(
                progress, "类目查询已跳过，继续后续类目",
                index=index + 1, total=len(category_queries),
                percent=int((index + 1) * 100 / len(category_queries)),
                message=category_label,
            )
            continue
        successful_category_queries += 1
        warnings.extend(str(value) for value in result.get("warnings", []) if str(value))
        source_selection = query_settings.get("categorySelection")
        for rejected_candidate in result.get("rejectedCandidates", []):
            if not isinstance(rejected_candidate, dict):
                continue
            product_id = str(rejected_candidate.get("productId") or "")
            if not product_id:
                continue
            rejected_candidate["sourceCategorySelection"] = source_selection
            previous_rejected = rejected_by_id.get(product_id)
            if previous_rejected is None:
                rejected_by_id[product_id] = rejected_candidate
            else:
                previous_rejected["thresholdReasons"] = list(dict.fromkeys(
                    list(previous_rejected.get("thresholdReasons") or [])
                    + list(rejected_candidate.get("thresholdReasons") or [])
                ))
        for candidate in result.get("candidates", []):
            product_id = str(candidate.get("productId", ""))
            if not product_id:
                raise RuntimeError("百应候选缺少商品 ID，商品身份无法唯一确认；已停止后续平台操作")
            candidate["sourceCategorySelection"] = source_selection
            previous = merged.get(product_id)
            if previous is None or _candidate_rank(candidate, settings) < _candidate_rank(previous, settings):
                merged[product_id] = candidate
        _report_operation_progress(
            progress, "类目查询完成",
            index=index + 1, total=len(category_queries),
            percent=int((index + 1) * 100 / len(category_queries)),
            message=f"{category_label}；已完成 {index + 1}/{len(category_queries)} 个类目",
        )

    ranked = sorted(merged.values(), key=lambda candidate: _candidate_rank(candidate, settings))
    selected = ranked[:limit]
    preserved = {str(value) for value in (preserve_product_ids or []) if str(value)}
    included = {str(candidate.get("productId", "")) for candidate in selected}
    for candidate in ranked:
        product_id = str(candidate.get("productId", ""))
        if product_id in preserved and product_id not in included:
            selected.append(candidate)
            included.add(product_id)
    for index, candidate in enumerate(selected):
        candidate["cardIndex"] = index
        candidate["selected"] = True
    rejected = [candidate for product_id, candidate in rejected_by_id.items() if product_id not in merged]
    final_result = {
        "ok": True,
        "candidateCount": len(selected),
        "candidates": selected,
        "rejectedCandidates": rejected,
        "warnings": list(dict.fromkeys(warnings)),
    }
    match_status = (
        "complete" if successful_category_queries == len(category_queries)
        else "partial" if successful_category_queries
        else "unavailable"
    )
    return _attach_priority_source_results(final_result, source_results, match_status)


def _safe_diagnostic_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    text = re.sub(r"(?im)^\s*(cookie|set-cookie|authorization)\s*:\s*.*$", r"\1: [已脱敏]", text)
    text = re.sub(
        r"(?i)([?&][^=&]*(?:token|secret|signature|cookie|session|authorization)[^=&]*=)[^&\s]+",
        r"\1[已脱敏]",
        text,
    )
    text = re.sub(r"https?://[^\s\"'<>]+", "[链接已省略]", text)
    return text[:limit]


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:100] or "百应商品"


def _product_gallery_urls(payload: Any) -> tuple[str, str, list[str], str]:
    try:
        data = payload.get("data", {})
        product_id = str(data.get("product_id") or "")
        model = data.get("model", {}) if isinstance(data.get("model"), dict) else {}
        product = model.get("product", {}) if isinstance(model.get("product"), dict) else {}
        base = product.get("product_base", {}) if isinstance(product.get("product_base"), dict) else {}
        title = str(base.get("title") or "").strip()
        product_url = _product_detail_url(product_id, data, product, base)
        gallery: list[str] = []
        for key in ("big_imgs", "images", "white_background"):
            values = base.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                url = item if isinstance(item, str) else item.get("url", "") if isinstance(item, dict) else ""
                if isinstance(url, str) and url.startswith("https://") and url not in gallery:
                    gallery.append(url)

        # 百应新版 pack_detail 把当前商品放在 shop_product_data.product_infos，
        # 图片字段改为 main_img/white_img.url_list。按 product_id 精确取当前商品，
        # 不能误用店铺推荐商品的图片。
        shop_data = model.get("shop_product_data", {}) if isinstance(model.get("shop_product_data"), dict) else {}
        product_infos = shop_data.get("product_infos", []) if isinstance(shop_data.get("product_infos"), list) else []
        current_info = next((
            item for item in product_infos
            if isinstance(item, dict) and str(item.get("product_id") or "") == product_id
        ), None)
        if current_info is None and product_infos and isinstance(product_infos[0], dict):
            current_info = product_infos[0]
        if isinstance(current_info, dict):
            if not product_id:
                product_id = str(current_info.get("product_id") or "")
            base_model = current_info.get("base_model", {}) if isinstance(current_info.get("base_model"), dict) else {}
            product_info = base_model.get("product_info", {}) if isinstance(base_model.get("product_info"), dict) else {}
            title = str(product_info.get("name") or title).strip()
            product_url = _product_detail_url(product_id, current_info, base_model, product_info) or product_url
            for key in ("main_img", "white_img"):
                image = product_info.get(key)
                urls = image.get("url_list", []) if isinstance(image, dict) else []
                for url in urls if isinstance(urls, list) else []:
                    if isinstance(url, str) and url.startswith("https://") and url not in gallery:
                        gallery.append(url)
        return product_id, title, gallery[:5], product_url
    except (AttributeError, TypeError):
        return "", "", [], ""


def _product_detail_url(product_id: str, *sources: Any) -> str:
    """读取当前商品的百应返回链接；缺失时生成官方商品详情链接，不采纳其他域名。"""
    product_id = str(product_id or "").strip()
    if not product_id:
        return ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("detail_url", "product_url", "share_url", "share_link", "item_url"):
            value = source.get(key)
            if not isinstance(value, str) or not value.startswith("https://"):
                continue
            try:
                parsed = urlparse(value)
                matches = _url_matches_exact_product_id(parsed, product_id)
                if parsed.hostname == "haohuo.jinritemai.com" and matches:
                    return value
            except ValueError:
                continue
    return f"https://haohuo.jinritemai.com/views/product/item2?id={quote(product_id)}"


def _baiying_category_detail_url(product_id: str, row: Dict[str, Any]) -> str:
    """复用百应商品决策页的已验证入口；推广 ID 缺失时不猜测。"""
    commodity_id = str(row.get("promotion_id") or row.get("commodity_id") or "").strip()
    if not re.fullmatch(r"\d{8,20}", product_id) or not re.fullmatch(r"\d{8,20}", commodity_id):
        return ""
    return (
        "https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting"
        f"?commodity_id={quote(commodity_id)}&id={quote(commodity_id)}&product_id={quote(product_id)}"
        "&commodity_location=1&decision_enter_from=pc.selection_square.recommend_main"
        "&only_query_params=1&universal_append=1"
        "&pre_universal_page_params_id=87e0475e-4976-40dd-83c8-658b76d382de"
    )


def _category_detail_url_matches_product(parsed: Any, product_id: str, commodity_id: str = "") -> bool:
    if getattr(parsed, "hostname", None) == "buyin.jinritemai.com":
        query = parse_qs(str(getattr(parsed, "query", "") or ""))
        return (
            getattr(parsed, "path", "").rstrip("/") == "/dashboard/merch-picking-library/merch-promoting"
            and query.get("product_id") == [product_id]
            and ("commodity_id" not in query or query["commodity_id"] == [commodity_id])
            and ("id" not in query or query["id"] == [commodity_id])
        )
    return getattr(parsed, "hostname", None) == "haohuo.jinritemai.com" and _url_matches_exact_product_id(parsed, product_id)


def _detail_product_sources(payload: Any, expected_product_id: str) -> tuple[str, list[Dict[str, Any]], str]:
    """只从详情响应中提取与候选商品 ID 一致的数据节点。"""
    if not isinstance(payload, dict):
        return "", [], "详情响应不是对象"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    top_product_id = str(data.get("product_id") or data.get("productId") or "").strip()
    if top_product_id and expected_product_id and top_product_id != expected_product_id:
        return top_product_id, [], f"详情响应商品 ID {top_product_id} 与候选 ID {expected_product_id} 不一致"

    product = model.get("product") if isinstance(model.get("product"), dict) else {}
    shop_data = model.get("shop_product_data") if isinstance(model.get("shop_product_data"), dict) else {}
    product_infos = shop_data.get("product_infos") if isinstance(shop_data.get("product_infos"), list) else []
    if product_infos:
        matching = next((
            item for item in product_infos
            if isinstance(item, dict) and str(item.get("product_id") or "") == expected_product_id
        ), None)
        if matching is None:
            returned_ids = [
                str(item.get("product_id") or "") for item in product_infos if isinstance(item, dict)
            ]
            return "", [], "详情响应中没有与候选商品 ID 对应的商品数据节点（返回 ID：" + ", ".join(returned_ids[:5]) + "）"
        base_model = matching.get("base_model") if isinstance(matching.get("base_model"), dict) else {}
        product_info = base_model.get("product_info") if isinstance(base_model.get("product_info"), dict) else {}
        promotion_info = base_model.get("promotion_info") if isinstance(base_model.get("promotion_info"), dict) else {}
        shop_info = base_model.get("shop_info") if isinstance(base_model.get("shop_info"), dict) else {}
        sources = [product_info, promotion_info, shop_info, base_model, matching]
        # pack_detail 的佣金有时只出现在已由顶层商品 ID 核实的 product.product_cos 中。
        if top_product_id == expected_product_id:
            sources.append(product)
        return expected_product_id, sources, ""

    base = product.get("product_base") if isinstance(product.get("product_base"), dict) else {}
    base_model = product.get("base_model") if isinstance(product.get("base_model"), dict) else {}
    product_info = base_model.get("product_info") if isinstance(base_model.get("product_info"), dict) else {}
    promotion_info = base_model.get("promotion_info") if isinstance(base_model.get("promotion_info"), dict) else {}
    shop_info = base_model.get("shop_info") if isinstance(base_model.get("shop_info"), dict) else {}
    actual_product_id = top_product_id or str(product.get("product_id") or base.get("product_id") or "").strip()
    if actual_product_id and expected_product_id and actual_product_id != expected_product_id:
        return actual_product_id, [], f"详情响应商品 ID {actual_product_id} 与候选 ID {expected_product_id} 不一致"
    sources = [product_info, promotion_info, shop_info, base_model, base, product]
    if not any(sources):
        return actual_product_id, [], "详情响应没有可解析的商品数据节点"
    return actual_product_id, sources, ""


def _category_id_scalar(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("id", "category_id", "categoryId", "cate_id", "cateId", "value"):
            if key in value:
                found = _category_id_scalar(value.get(key))
                if found:
                    return found
        return ""
    if isinstance(value, bool) or value is None:
        return ""
    if not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    pattern = r"^\d+(?:" + re.escape(CATEGORY_PATH_SEPARATOR) + r"\d+)*$"
    if not re.fullmatch(pattern, text):
        return ""
    return text if all(int(part) > 0 for part in text.split(CATEGORY_PATH_SEPARATOR)) else ""


def _detail_category_path(
    payload: Any,
    expected_product_id: str,
    category_tree: Sequence[Any],
    identity_verified_by_page: bool = False,
) -> tuple[list[str], str]:
    """从已核对商品 ID 的详情数据里读取三级类目 ID；不从标题推断。"""
    actual_product_id, sources, identity_error = _detail_product_sources(payload, expected_product_id)
    if actual_product_id and actual_product_id != expected_product_id:
        return [], identity_error
    if identity_error and not (identity_verified_by_page and not actual_product_id):
        return [], identity_error
    if actual_product_id != expected_product_id and not identity_verified_by_page:
        return [], f"详情商品 ID {actual_product_id or '缺失'} 与候选 ID 不一致"
    if identity_verified_by_page and not actual_product_id:
        # 只扫描已通过商品 ID 校验的详情页所返回的官方详情接口。
        sources = [payload]

    metadata = _category_path_metadata(category_tree)
    leaf_paths: Dict[str, list[tuple[str, str, str]]] = {}
    for path_id, path in metadata.items():
        if not path[2]:
            continue
        leaf = path_id.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1]
        leaf_paths.setdefault(path_id, []).append(path)
        leaf_paths.setdefault(leaf, []).append(path)

    first_aliases = {
        "first", "firstid", "firstcateid", "firstcategoryid", "firstcid",
        "firstlevelid", "firstlevelcateid", "firstlevelcategoryid",
        "level1", "level1id", "level1cateid", "level1categoryid", "cate1id", "category1id",
    }
    second_aliases = {
        "second", "secondid", "secondcateid", "secondcategoryid", "secondcid",
        "secondlevelid", "secondlevelcateid", "secondlevelcategoryid",
        "level2", "level2id", "level2cateid", "level2categoryid", "cate2id", "category2id",
    }
    third_aliases = {
        "third", "thirdid", "thirdcateid", "thirdcategoryid", "thirdcid",
        "thirdlevelid", "thirdlevelcateid", "thirdlevelcategoryid",
        "level3", "level3id", "level3cateid", "level3categoryid", "cate3id", "category3id",
    }
    single_category_ids: set[str] = set()
    visited: set[int] = set()

    def normalize_path(values: Sequence[str]) -> list[str]:
        return _normalize_exact_category_path(values[-3:], category_tree)

    def walk(node: Any, path: str = "", in_category_scope: bool = False, depth: int = 0) -> list[str]:
        if depth > 9 or not isinstance(node, (dict, list)):
            return []
        identity = id(node)
        if identity in visited:
            return []
        visited.add(identity)
        if isinstance(node, list):
            if in_category_scope:
                values = [_category_id_scalar(item) for item in node]
                if len(values) >= 3 and all(values[-3:]):
                    return normalize_path(values)
            for index, child in enumerate(node[:80]):
                found = walk(child, f"{path}[{index}]", in_category_scope, depth + 1)
                if found:
                    return found
            return []

        key_names = [re.sub(r"[^a-z0-9]", "", str(key).casefold()) for key in node]
        node_scope = in_category_scope or bool(re.search(r"category|cate", path, re.I)) or any(
            re.search(r"category|cate", str(key), re.I) for key in node
        )
        levels: dict[str, str] = {}
        for key, value in node.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in first_aliases:
                levels["first"] = _category_id_scalar(value)
            elif normalized in second_aliases:
                levels["second"] = _category_id_scalar(value)
            elif normalized in third_aliases:
                levels["third"] = _category_id_scalar(value)
            if node_scope and re.search(r"category|cate", str(key), re.I):
                scalar = _category_id_scalar(value)
                if scalar:
                    single_category_ids.add(scalar)
        if node_scope and all(levels.get(level) for level in ("first", "second", "third")):
            normalized_path = normalize_path([levels["first"], levels["second"], levels["third"]])
            if normalized_path:
                return normalized_path

        for key, child in node.items():
            child_scope = node_scope or bool(re.search(r"category|cate", str(key), re.I))
            found = walk(child, f"{path}.{key}" if path else str(key), child_scope, depth + 1)
            if found:
                return found
        return []

    for source in sources:
        found = walk(source)
        if found:
            return found, ""

    resolved_leaf_paths = {
        path for category_id in single_category_ids for path in leaf_paths.get(category_id, [])
    }
    if not resolved_leaf_paths:
        resolved_leaf_paths = {
            path for category_id in single_category_ids
            for path in leaf_paths.get(category_id.rsplit(CATEGORY_PATH_SEPARATOR, 1)[-1], [])
        }
    if len(resolved_leaf_paths) == 1:
        return [str(value) for value in next(iter(resolved_leaf_paths))], ""
    if len(resolved_leaf_paths) > 1:
        return [], "详情页只提供了无法唯一定位三级类目的 ID"
    return [], "详情页未返回完整三级类目 ID"


def _detail_diagnostic_path(url: Any) -> str:
    try:
        return urlparse(str(url or '')).path[:500]
    except ValueError:
        return ''


def _detail_diagnostic_entry_url(url: Any) -> str:
    try:
        parsed = urlparse(str(url or ''))
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return parsed.path[:500]
        return f'{parsed.scheme}://{parsed.hostname}{parsed.path[:500]}'
    except ValueError:
        return ''


def _detail_diagnostic_product_ids(payload: Any) -> list[str]:
    ids: list[str] = []
    seen_nodes: set[int] = set()
    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack and len(seen_nodes) < 3500 and len(ids) < 5:
        node, depth = stack.pop()
        if not isinstance(node, (dict, list)) or depth > 8 or id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))
        if isinstance(node, dict):
            for key in ('product_id', 'productId'):
                value = node.get(key)
                if value is not None and not isinstance(value, (dict, list, bool)):
                    product_id = str(value).strip()
                    if product_id and product_id not in ids:
                        ids.append(product_id[:80])
            stack.extend((child, depth + 1) for child in node.values())
        else:
            stack.extend((child, depth + 1) for child in node[:100])
    return ids


def _detail_request_matches_product(request: Any, expected_product_id: str) -> bool:
    """只判断请求中是否含有精确候选 ID，不记录请求体或凭据。"""
    wanted = str(expected_product_id or "").strip()
    if not wanted:
        return False
    request_url = str(getattr(request, "url", "") or "")
    try:
        query_values = parse_qs(urlparse(request_url).query, keep_blank_values=True)
        if any(str(value).strip() == wanted for values in query_values.values() for value in values):
            return True
    except (TypeError, ValueError):
        pass
    try:
        post_data_json = getattr(request, "post_data_json", None)
        if callable(post_data_json):
            post_data_json = post_data_json()
        stack: list[tuple[Any, int]] = [(post_data_json, 0)]
        seen: set[int] = set()
        while stack and len(seen) < 1500:
            node, depth = stack.pop()
            if depth > 8 or not isinstance(node, (dict, list)) or id(node) in seen:
                continue
            seen.add(id(node))
            values = node.values() if isinstance(node, dict) else node[:100]
            if any(str(value).strip() == wanted for value in values if isinstance(value, (str, int))):
                return True
            stack.extend((value, depth + 1) for value in values)
    except Exception:
        pass
    try:
        post_data = getattr(request, "post_data", None)
        if callable(post_data):
            post_data = post_data()
        if isinstance(post_data, str) and re.search(r"(?<!\d)" + re.escape(wanted) + r"(?!\d)", post_data):
            return True
    except Exception:
        pass
    return False


def _detail_diagnostic_category_fields(payload: Any) -> list[Dict[str, str]]:
    """仅记录响应中类目相关的数字字段，避免保存商品详情原文。"""
    samples: list[Dict[str, str]] = []
    stack: list[tuple[Any, str, int]] = [(payload, "", 0)]
    seen: set[int] = set()
    while stack and len(seen) < 2500 and len(samples) < 16:
        node, path, depth = stack.pop()
        if depth > 8 or not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if re.search(r"category|cate", str(key), re.I):
                    scalar = _category_id_scalar(value)
                    if scalar:
                        samples.append({"field": child_path[:180], "value": scalar[:100]})
                        if len(samples) >= 16:
                            break
                stack.append((value, child_path, depth + 1))
        else:
            stack.extend((value, f"{path}[{index}]", depth + 1) for index, value in enumerate(node[:100]))
    return samples


def _new_detail_category_diagnostics(detail_url: str) -> Dict[str, Any]:
    return {
        'entryUrl': _detail_diagnostic_entry_url(detail_url),
        'finalPagePath': '',
        'navigationState': 'not_started',
        'xhrFetchResponseCount': 0,
        'responses': [],
        'responsesTruncated': False,
        'packDetailListenerHit': False,
        'packDetailResponseCount': 0,
        'detailApiResponseCount': 0,
        'detailApiRequestIdMatchCount': 0,
        'detailApiCategoryFieldSamples': [],
        'verifiedResponseCount': 0,
        'verifiedCategoryFieldSamples': [],
    }


def _read_candidate_category_from_detail(
    context: BrowserContext,
    row: Dict[str, Any],
    expected_product_id: str,
    category_tree: Sequence[Any],
) -> Dict[str, Any]:
    product_id = str(expected_product_id or '').strip()
    base = row.get('base_model') if isinstance(row.get('base_model'), dict) else {}
    product_info = base.get('product_info') if isinstance(base.get('product_info'), dict) else {}
    baiying_detail_url = _baiying_category_detail_url(product_id, row)
    detail_url = baiying_detail_url or _product_detail_url(product_id, row, base, product_info)
    commodity_id = str(row.get("promotion_id") or row.get("commodity_id") or "").strip() if baiying_detail_url else ""
    diagnostics = _new_detail_category_diagnostics(detail_url)
    diagnostics['entryKind'] = 'baiying_promotion' if baiying_detail_url else 'product_share'
    try:
        parsed_url = urlparse(detail_url)
    except ValueError:
        return {'productId': product_id, 'categoryIds': [], 'reason': 'DETAIL_URL_INVALID', 'diagnostics': diagnostics}
    if parsed_url.scheme != 'https' or parsed_url.hostname not in {'haohuo.jinritemai.com', 'buyin.jinritemai.com'}:
        return {'productId': product_id, 'categoryIds': [], 'reason': 'DETAIL_URL_NOT_ALLOWED', 'diagnostics': diagnostics}
    if not _category_detail_url_matches_product(parsed_url, product_id, commodity_id):
        return {'productId': product_id, 'categoryIds': [], 'reason': 'DETAIL_URL_PRODUCT_ID_MISMATCH', 'diagnostics': diagnostics}

    detail_page: Optional[Page] = None
    payloads: list[tuple[Dict[str, Any], bool]] = []
    response_errors: list[str] = []

    def on_detail(response: Any) -> None:
        request = getattr(response, 'request', None)
        resource_type = getattr(request, 'resource_type', '')
        if callable(resource_type):
            try:
                resource_type = resource_type()
            except Exception:
                resource_type = ''
        if str(resource_type).casefold() not in {'xhr', 'fetch'}:
            return

        response_url = str(getattr(response, 'url', '') or '')
        response_path = _detail_diagnostic_path(response_url)
        normalized_response_path = response_path.rstrip('/').casefold()
        is_pack_detail = 'pack_detail' in normalized_response_path
        is_product_detail_api = normalized_response_path == '/ecom/product/detail/h5/sc'
        is_category_detail_response = is_pack_detail or is_product_detail_api
        request_product_id_match = _detail_request_matches_product(request, product_id)
        headers = getattr(response, 'headers', {})
        if callable(headers):
            try:
                headers = headers()
            except Exception:
                headers = {}
        content_type = ''
        if isinstance(headers, dict):
            content_type = next(
                (str(value) for key, value in headers.items() if str(key).casefold() == 'content-type'),
                '',
            )
        content_type = content_type.split(';', 1)[0].strip().lower()[:120]
        status = getattr(response, 'status', None)
        if callable(status):
            try:
                status = status()
            except Exception:
                status = None
        if not isinstance(status, int):
            status = None

        diagnostics['xhrFetchResponseCount'] += 1
        if is_pack_detail:
            diagnostics['packDetailListenerHit'] = True
            diagnostics['packDetailResponseCount'] += 1
        if is_product_detail_api:
            diagnostics['detailApiResponseCount'] += 1
            if request_product_id_match:
                diagnostics['detailApiRequestIdMatchCount'] += 1
        response_record: Dict[str, Any] = {
            'path': response_path,
            'status': status,
            'contentType': content_type,
            'packDetailListenerHit': is_pack_detail,
            'productDetailApi': is_product_detail_api,
            'requestProductIdMatched': request_product_id_match,
            'responseProductIds': [],
        }
        payload: Any = None
        if 'json' in content_type:
            try:
                payload = response.json()
                response_record['jsonParsed'] = True
                response_record['responseProductIds'] = _detail_diagnostic_product_ids(payload)
                if is_category_detail_response:
                    field_samples = _detail_diagnostic_category_fields(payload)
                    response_record['categoryFieldSamples'] = field_samples
                    if not diagnostics['detailApiCategoryFieldSamples'] and field_samples:
                        diagnostics['detailApiCategoryFieldSamples'] = field_samples
            except Exception as error:
                response_record['jsonParsed'] = False
                response_record['jsonParseErrorType'] = type(error).__name__
        relevant_response = is_category_detail_response or request_product_id_match or product_id in response_record['responseProductIds']
        if relevant_response or len(diagnostics['responses']) < 8:
            if len(diagnostics['responses']) < 30:
                diagnostics['responses'].append(response_record)
            else:
                diagnostics['responsesTruncated'] = True
        if not is_category_detail_response:
            return
        if status is not None and not 200 <= status < 300:
            response_errors.append(f'DETAIL_CATEGORY_HTTP_{status}')
            return
        if payload is None:
            response_errors.append('DETAIL_CATEGORY_JSON_UNREADABLE')
            return
        if not isinstance(payload, dict):
            response_errors.append('DETAIL_CATEGORY_NOT_OBJECT')
            return
        try:
            actual_id, _, identity_error = _detail_product_sources(payload, product_id)
        except Exception:
            response_errors.append('DETAIL_CATEGORY_ID_UNREADABLE')
            return
        if not identity_error and actual_id == product_id:
            diagnostics['verifiedResponseCount'] += 1
            field_samples = response_record.get('categoryFieldSamples', [])
            if field_samples and not diagnostics['verifiedCategoryFieldSamples']:
                diagnostics['verifiedCategoryFieldSamples'] = field_samples
            payloads.append((payload, False))
        elif is_product_detail_api and not actual_id:
            # 请求带有目标 ID 不等于响应证明了商品身份；不从无 ID 的响应认定类目。
            response_errors.append('DETAIL_CATEGORY_RESPONSE_PRODUCT_ID_MISSING')
        else:
            response_errors.append('DETAIL_CATEGORY_ID_MISMATCH_OR_MISSING')

    try:
        active_browser = context.browser
        if active_browser is None:
            diagnostics['navigationState'] = 'background_page_unavailable'
            return {
                'productId': product_id, 'categoryIds': [],
                'reason': 'DETAIL_BACKGROUND_PAGE_UNAVAILABLE', 'fatal': True,
                'diagnostics': diagnostics,
            }
        detail_page = _create_background_page(context, active_browser)
        detail_page.on('response', on_detail)
        diagnostics['navigationState'] = 'started'
        try:
            navigation_response = detail_page.goto(detail_url, wait_until='commit', timeout=20_000)
        except Exception as error:
            diagnostics['navigationState'] = 'failed'
            diagnostics['navigationErrorType'] = type(error).__name__
            diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', ''))
            return {
                'productId': product_id,
                'categoryIds': [],
                'reason': f"\u5bfc\u822a\u5931\u8d25 ({type(error).__name__})",
                'diagnostics': diagnostics,
            }
        diagnostics['navigationState'] = 'committed'
        if navigation_response is not None:
            navigation_status = getattr(navigation_response, 'status', None)
            if callable(navigation_status):
                try:
                    navigation_status = navigation_status()
                except Exception:
                    navigation_status = None
            diagnostics['mainDocumentStatus'] = navigation_status if isinstance(navigation_status, int) else None
        diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', ''))
        final_host = urlparse(detail_page.url).hostname
        if final_host not in {'haohuo.jinritemai.com', 'buyin.jinritemai.com'}:
            diagnostics['navigationState'] = 'redirected_outside_allowed_domain'
            return {
                'productId': product_id,
                'categoryIds': [],
                'reason': "\u5bfc\u822a\u5931\u8d25 (\u6700\u7ec8\u9875\u9762\u4e0d\u5728\u5141\u8bb8\u57df\u540d)",
                'diagnostics': diagnostics,
            }
        if not _category_detail_url_matches_product(urlparse(detail_page.url), product_id, commodity_id):
            diagnostics['navigationState'] = 'redirected_to_unmatched_product'
            return {
                'productId': product_id,
                'categoryIds': [],
                'reason': 'DETAIL_FINAL_URL_PRODUCT_ID_MISMATCH',
                'diagnostics': diagnostics,
            }

        deadline = time.monotonic() + DETAIL_CATEGORY_READ_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not payloads:
            prompt_kind, _prompt_text = _visible_baiying_prompt(detail_page)
            if prompt_kind == 'manual_verification':
                diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', ''))
                return {
                    'productId': product_id,
                    'categoryIds': [],
                    'reason': 'DETAIL_MANUAL_VERIFICATION_REQUIRED',
                    'fatal': True,
                    'diagnostics': diagnostics,
                }
            if prompt_kind in {'login', 'account_unavailable'}:
                diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', ''))
                return {
                    'productId': product_id,
                    'categoryIds': [],
                    'reason': 'DETAIL_ACCOUNT_UNAVAILABLE',
                    'fatal': True,
                    'diagnostics': diagnostics,
                }
            detail_page.wait_for_timeout(200)
        diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', ''))
        if not payloads:
            if diagnostics['xhrFetchResponseCount'] == 0:
                reason = "\u65e0\u54cd\u5e94: \u8be6\u60c5\u9875\u672a\u6536\u5230 XHR/fetch \u54cd\u5e94"
            elif diagnostics['detailApiResponseCount']:
                reason = "DETAIL_API_RESPONSE_UNREADABLE_OR_ID_UNMATCHED"
                if response_errors:
                    reason += f" ({response_errors[-1]})"
            elif not diagnostics['packDetailListenerHit']:
                reason = "\u63a5\u53e3\u8def\u5f84\u672a\u547d\u4e2d: \u672a\u76d1\u542c\u5230 pack_detail \u54cd\u5e94"
            else:
                reason = "ID \u4e0d\u4e00\u81f4: pack_detail \u54cd\u5e94\u672a\u63d0\u4f9b\u4e0e\u5019\u9009\u5546\u54c1\u7cbe\u786e\u5339\u914d\u7684 ID"
                if response_errors:
                    reason += f" ({response_errors[-1]})"
            return {'productId': product_id, 'categoryIds': [], 'reason': reason, 'diagnostics': diagnostics}

        last_category_reason = ''
        for payload, identity_verified_by_page in payloads:
            category_ids, last_category_reason = _detail_category_path(
                payload,
                product_id,
                category_tree,
                identity_verified_by_page=identity_verified_by_page,
            )
            # 类目补核验同时为佣金、体验分和其他详情指标保留同一份已核对响应，
            # 后续统一筛选不得再次打开同一商品详情页。
            actual_id, _, identity_error = _detail_product_sources(payload, product_id)
            if actual_id == product_id and not identity_error:
                _cache_detail_payload(context, product_id, payload)
            if category_ids:
                return {
                    'productId': product_id,
                    'categoryIds': category_ids,
                    'reason': '',
                    'diagnostics': diagnostics,
                }
        reason = "\u54cd\u5e94\u65e0\u7c7b\u76ee"
        if last_category_reason:
            reason += f": {last_category_reason}"
        return {'productId': product_id, 'categoryIds': [], 'reason': reason, 'diagnostics': diagnostics}
    except Exception as error:
        diagnostics['finalPagePath'] = _detail_diagnostic_path(getattr(detail_page, 'url', '')) if detail_page else ''
        if diagnostics.get('navigationState') == 'started':
            diagnostics['navigationState'] = 'failed'
            diagnostics['navigationErrorType'] = type(error).__name__
            reason = f"\u5bfc\u822a\u5931\u8d25 ({type(error).__name__})"
        else:
            reason = f"DETAIL_READ_FAILED ({type(error).__name__})"
        return {'productId': product_id, 'categoryIds': [], 'reason': reason, 'diagnostics': diagnostics}
    finally:
        if detail_page is not None:
            try:
                detail_page.close()
            except Exception:
                pass


def _detail_metrics(payload: Any, expected_product_id: str) -> Dict[str, Any]:
    product_id, sources, error = _detail_product_sources(payload, expected_product_id)
    if error:
        return {"productId": product_id, "error": error, "metrics": {}}
    if not product_id:
        return {"productId": "", "error": "详情响应缺少可核对的商品 ID", "metrics": {}}

    product_info = sources[0] if sources and isinstance(sources[0], dict) else {}
    metrics = {
        "salesAmount": _find_metric(sources, ("sale_amount", "sales_amount", "month_gmv", "gmv", "total_sales_amount")),
        "salesCount": _find_metric(sources, ("month_sale", "month_sales", "sales_count", "sale_count", "total_sales_count")),
        "conversionRate": _find_metric(sources, ("conversion_rate", "convert_rate", "conversion_ratio")),
        "views": _find_metric(sources, ("view_count", "views", "browse_count", "browse_num")),
        # 百应 pack_detail 的 product_cos.cos_label.cos.cos_ratio 是百分数（例如 25 表示 25%）。
        "commissionRate": _find_metric(
            sources, ("cos_ratio_text", "commission_rate", "commission_ratio", "cos_ratio")
        ),
        "experienceScore": _find_metric(sources, ("experience_score", "experienceScore", "shop_score", "shopScore", "score")),
    }
    trend = _trend_analysis(product_info)
    return {
        "productId": product_id,
        "metrics": metrics,
        "trend": trend,
        "error": "",
    }


def _cache_detail_payload(context: BrowserContext, product_id: str, payload: Any) -> None:
    if not product_id or not isinstance(payload, dict):
        return
    key = (id(context), product_id)
    _DETAIL_PAYLOAD_CACHE[key] = (time.time(), payload)
    expired = [
        cache_key for cache_key, (captured_at, _) in _DETAIL_PAYLOAD_CACHE.items()
        if time.time() - captured_at > DETAIL_PAYLOAD_CACHE_TTL_SECONDS
    ]
    for cache_key in expired:
        _DETAIL_PAYLOAD_CACHE.pop(cache_key, None)
    while len(_DETAIL_PAYLOAD_CACHE) > 64:
        oldest = min(_DETAIL_PAYLOAD_CACHE, key=lambda cache_key: _DETAIL_PAYLOAD_CACHE[cache_key][0])
        _DETAIL_PAYLOAD_CACHE.pop(oldest, None)


def _cached_detail_payload(context: BrowserContext, product_id: str) -> Optional[Dict[str, Any]]:
    cached = _DETAIL_PAYLOAD_CACHE.get((id(context), product_id))
    if not cached:
        return None
    captured_at, payload = cached
    if time.time() - captured_at > DETAIL_PAYLOAD_CACHE_TTL_SECONDS or not isinstance(payload, dict):
        _DETAIL_PAYLOAD_CACHE.pop((id(context), product_id), None)
        return None
    return payload


def _click_candidate_detail_target(
    title_locator: Any, card_locator: Any, product_id: str, product_title: str
) -> str:
    """按已复核商品 ID 点击商品详情标题；表格行路径排除操作列及加车控件。"""
    expected_id = str(product_id or "").strip()
    if not expected_id:
        raise RuntimeError("打开商品详情前缺少商品 ID")
    try:
        exposed_ids = _candidate_card_product_ids(card_locator)
    except Exception as error:
        raise RuntimeError(f"商品 ID {expected_id} 的卡片身份复核失败，未点击详情") from error
    if exposed_ids and expected_id not in exposed_ids:
        raise RuntimeError(f"商品 ID {expected_id} 与当前详情卡片不匹配，未点击详情")

    card_content = card_locator.locator('[class*="__cardContent___"]')
    visible_content = []
    try:
        for index in range(card_content.count()):
            item = card_content.nth(index)
            if item.is_visible():
                visible_content.append(item)
    except Exception:
        visible_content = []
    target = title_locator
    target_name = "商品标题"
    try:
        is_table_row = bool(card_locator.evaluate(
            "element => element.matches('tr, [role=row], [class*=\"__row___\"]')"
        ))
    except Exception:
        is_table_row = False

    if is_table_row and not visible_content:
        exact_titles = card_locator.get_by_text(str(product_title or ""), exact=True)
        safe_targets: list[tuple[Any, bool]] = []
        for index in range(exact_titles.count()):
            item = exact_titles.nth(index)
            try:
                if not item.is_visible():
                    continue
                assessment = item.evaluate(
                    r"""(element, args) => {
                      const { expectedId, title } = args;
                      const row = element.closest('tr, [role="row"], [class*="__row___"]');
                      if (!row) return { safe: false, productInfo: false };
                      const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                      const cells = Array.from(row.children).filter((cell) =>
                        cell.matches('td, th, [role="cell"], [role="gridcell"], [role="columnheader"]')
                      );
                      const cell = element.closest('td, [role="cell"], [role="gridcell"]');
                      const cellIndex = cell ? cells.indexOf(cell) : -1;
                      const table = row.closest('table');
                      const headerRows = Array.from(table?.querySelectorAll('thead tr') || []);
                      const headerRow = headerRows.length ? headerRows[headerRows.length - 1] : null;
                      const headers = headerRow
                        ? Array.from(headerRow.children).filter((header) =>
                            header.matches('th, td, [role="columnheader"]'))
                        : [];
                      const operationIndexes = new Set(headers.flatMap((header, index) =>
                        /^操作(?:\s|$)/.test(text(header.innerText)) ? [index] : []
                      ));
                      const infoIndexes = new Set(headers.flatMap((header, index) =>
                        /^(商品信息|商品|产品信息)$/.test(text(header.innerText)) ? [index] : []
                      ));
                      const actionPattern = /加入选品车|加选品车|加入购物车|加车|加购/;
                      if (cellIndex >= 0 && operationIndexes.has(cellIndex)) {
                        return { safe: false, productInfo: false };
                      }
                      if (cell && Array.from(cell.querySelectorAll('button, [role="button"]')).some((button) =>
                          actionPattern.test(text(button.innerText) + ' ' + text(button.getAttribute('aria-label'))))) {
                        return { safe: false, productInfo: false };
                      }
                      const actionControl = element.closest('button, [role="button"]');
                      if (actionControl && actionPattern.test(
                          text(actionControl.innerText) + ' ' + text(actionControl.getAttribute('aria-label'))) &&
                          actionControl !== row) {
                        return { safe: false, productInfo: false };
                      }
                      const link = element.closest('a');
                      if (link) {
                        try {
                          const url = new URL(link.href, location.href);
                          const queryValues = ['id', 'product_id', 'productId'].flatMap((key) => url.searchParams.getAll(key));
                          const pathMatch = url.pathname.split('/').filter(Boolean).some((part) => {
                            try { return decodeURIComponent(part) === expectedId; } catch (_) { return part === expectedId; }
                          });
                          const belongsToProduct = queryValues.length
                            ? queryValues.every((value) => value === expectedId)
                            : pathMatch;
                          if (!belongsToProduct) return { safe: false, productInfo: false };
                        } catch (_) { return { safe: false, productInfo: false }; }
                      }
                      return {
                        safe: true,
                        productInfo: cellIndex >= 0 && infoIndexes.has(cellIndex),
                        titleMatches: text(element.innerText) === text(title),
                      };
                    }""",
                    {"expectedId": expected_id, "title": product_title},
                )
                if isinstance(assessment, dict) and assessment.get("safe"):
                    safe_targets.append((item, bool(assessment.get("productInfo"))))
            except Exception:
                continue

        preferred_targets = [item for item, product_info in safe_targets if product_info]
        if len(preferred_targets) == 1:
            target = preferred_targets[0]
            target_name = "联盟榜商品信息列标题"
        elif len(safe_targets) == 1:
            target = safe_targets[0][0]
            target_name = "联盟榜同商品行标题"
        else:
            reason = (
                "同商品行有多个可点击标题，无法确定详情入口"
                if safe_targets else "标题只位于操作列/加车控件，且同商品行没有安全详情入口"
            )
            raise CandidateDetailClickBlockedError(
                f"商品 ID {expected_id} 的联盟榜详情标题无法安全唯一点击（{reason}）"
            )
    elif len(visible_content) == 1:
        content_title = visible_content[0].get_by_text(str(product_title or ""), exact=True)
        if content_title.count() == 1 and content_title.first.is_visible():
            safe_title = content_title.first.evaluate(
                """(element, id) => {
                  if (element.closest('button, [role=button]')) return false;
                  const link = element.closest('a');
                  if (!link) return true;
                  try {
                    const url = new URL(link.href, location.href);
                    const queryValues = ['id', 'product_id', 'productId'].flatMap((key) => url.searchParams.getAll(key));
                    const pathMatch = url.pathname.split('/').filter(Boolean).some((part) => {
                      try { return decodeURIComponent(part) === id; } catch (_) { return part === id; }
                    });
                    return queryValues.length ? queryValues.every((value) => value === id) : pathMatch;
                  } catch (_) { return false; }
                }""",
                expected_id,
            )
            if safe_title:
                target = content_title.first
                target_name = "商品卡片内容区标题"
    if target is title_locator:
        safe_title = title_locator.evaluate(
            """(element, id) => {
              if (element.closest('button, [role=button]')) return false;
              const link = element.closest('a');
              if (!link) return true;
              try {
                const url = new URL(link.href, location.href);
                const queryValues = ['id', 'product_id', 'productId'].flatMap((key) => url.searchParams.getAll(key));
                const pathMatch = url.pathname.split('/').filter(Boolean).some((part) => {
                  try { return decodeURIComponent(part) === id; } catch (_) { return part === id; }
                });
                return queryValues.length ? queryValues.every((value) => value === id) : pathMatch;
              } catch (_) { return false; }
            }""",
            expected_id,
        )
        if not safe_title:
            raise CandidateDetailClickBlockedError(
                f"商品 ID {expected_id} 的标题位于加车或非本商品操作控件内，未点击详情"
            )
    try:
        target.click(timeout=6_000, no_wait_after=True)
    except Exception as error:
        if "intercepts pointer events" in str(error).lower():
            # 百应部分商品卡片的曝光层会盖住标题；身份已按商品 ID 复核，尝试同一标题节点的 DOM click。
            # 响应监听仍会校验 pack_detail 中的商品 ID，避免误读其他商品详情。
            try:
                target.evaluate("(element) => element.click()")
                return f"{target_name}（DOM 点击兜底）"
            except Exception as fallback_error:
                raise CandidateDetailClickBlockedError(
                    f"商品 ID {expected_id} 的详情点击被页面覆盖层拦截，DOM 点击兜底也失败："
                    f"{_safe_diagnostic_text(fallback_error, 350)}"
                ) from fallback_error
        fatal_markers = (
            "target closed", "context has been closed", "browser has been closed",
            "browser disconnected", "connection closed", "websocket", "cdp",
        )
        if any(marker in str(error).lower() for marker in fatal_markers):
            raise RuntimeError(
                f"商品详情点击时浏览器状态异常，无法继续复核：{_safe_diagnostic_text(error, 500)}"
            ) from error
        raise CandidateDetailClickBlockedError(
            f"商品 ID {expected_id} 的{target_name}无法打开详情，本商品可跳过："
            f"{_safe_diagnostic_text(error, 500)}"
        ) from error
    return target_name


def _capture_candidate_detail(
    page: Page,
    context: BrowserContext,
    candidate: Dict[str, Any],
    settings: Optional[Dict[str, Any]],
    progress: Optional[Callable[[Dict[str, Any]], None]],
    diagnostics: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = DETAIL_ANALYSIS_TIMEOUT_SECONDS,
) -> tuple[Optional[Dict[str, Any]], str]:
    """在加车前只读打开候选商品详情，捕获 pack_detail 后恢复选品页。"""
    product_id = str(candidate.get("productId") or "").strip()
    if not product_id:
        raise RuntimeError("商品数据分析缺少商品 ID，已停止后续操作")
    prompt_kind, prompt_text = _visible_baiying_prompt(page)
    if prompt_kind == "manual_verification":
        raise RuntimeError("百应要求手动完成验证；商品分析已停止，未继续加车。")
    if prompt_kind in {"login", "account_unavailable"}:
        raise RuntimeError(f"百应账号状态异常：{prompt_text or prompt_kind}；商品分析已停止。")
    if prompt_kind in {"invoice_eligibility", "cart_error"}:
        raise RuntimeError(f"百应页面存在未确认弹窗：{prompt_text or prompt_kind}；商品分析已停止。")

    source_selection = candidate.get("sourceCategorySelection")
    if (
        settings is not None
        and isinstance(source_selection, dict)
        and any(source_selection.get(level) for level in ("level1", "level2", "level3"))
    ):
        candidate_settings = dict(settings)
        candidate_settings["categorySelection"] = source_selection
        if not _candidate_is_current_for_source(page, candidate, candidate_settings):
            refreshed = _apply_one_category_and_collect_candidates(
                page,
                candidate_settings,
                force_refresh=True,
                preserve_product_ids=[product_id],
                include_source_ids=True,
            )
            match_state, live_candidate, match_reason = _refreshed_candidate_by_id(refreshed, product_id)
            if match_state == "ambiguous":
                raise RuntimeError(match_reason)
            if match_state == "filtered":
                raise CandidateSourceRefreshSkip(
                    f"商品 ID {product_id} 仍在来源类目原始结果中，但刷新后不满足筛选条件：{match_reason}"
                )
            if match_state == "raw_only":
                raise CandidateSourceRefreshSkip(
                    f"商品 ID {product_id} 仍在百应原始类目响应中，但候选数据无法解析；已跳过此商品（{match_reason}）"
                )
            if match_state == "promotion_id_only":
                raise CandidateSourceRefreshSkip(
                    f"商品 ID {product_id} 只匹配到推广 ID，无法确认是同一商品；已跳过（{match_reason}）"
                )
            if match_state == "absent":
                raise CandidateSourceRefreshSkip(
                    f"商品 ID {product_id} 未出现在刷新后的来源类目结果中；已跳过此商品（{match_reason}）"
                )
            if live_candidate is None:
                raise RuntimeError(f"刷新后的候选状态异常，无法确认商品 ID {product_id}")
            candidate["detailCardIndex"] = live_candidate.get("cardIndex", candidate.get("cardIndex"))

    detail_candidate = dict(candidate)
    if "detailCardIndex" in candidate:
        detail_candidate["cardIndex"] = candidate["detailCardIndex"]
    title_locator, detail_card = _locate_candidate_card(page, detail_candidate)
    original_url = page.url
    existing_pages = {id(item) for item in context.pages}
    opened_pages: list[Page] = []
    detail_payloads: list[Dict[str, Any]] = []
    response_errors: list[str] = []
    detail_click_skip_reason = ""

    def on_page(new_page: Page) -> None:
        if id(new_page) not in existing_pages:
            opened_pages.append(new_page)

    def on_detail(response: Any) -> None:
        is_pack_detail = "pack_detail" in str(getattr(response, "url", ""))
        if not is_pack_detail and diagnostics is None:
            return
        try:
            if not is_pack_detail:
                request = getattr(response, "request", None)
                resource_type = getattr(request, "resource_type", "")
                if callable(resource_type):
                    resource_type = resource_type()
                if str(resource_type).casefold() not in {"xhr", "fetch"}:
                    return
                headers = getattr(response, "headers", {})
                if callable(headers):
                    headers = headers()
                if not isinstance(headers, dict) or "json" not in str(headers.get("content-type", "")).casefold():
                    return
            status = getattr(response, "status", None)
            if callable(status):
                status = status()
            payload = response.json()
            if diagnostics is not None:
                response_ids = _detail_diagnostic_product_ids(payload)
                if product_id in response_ids or _detail_request_matches_product(getattr(response, "request", None), product_id):
                    if len(diagnostics["responses"]) < 30:
                        diagnostics["responses"].append({
                            "path": _detail_diagnostic_path(getattr(response, "url", "")),
                            "status": status if isinstance(status, int) else None,
                            "responseProductIds": response_ids,
                            "categoryFieldSamples": _detail_diagnostic_category_fields(payload),
                        })
                    if isinstance(payload, dict) and (status is None or 200 <= status < 300):
                        actual_id, _, identity_error = _detail_product_sources(payload, product_id)
                        if actual_id == product_id and not identity_error:
                            matched = diagnostics.setdefault("_matchedPayloads", [])
                            if len(matched) < 8:
                                matched.append(payload)
            if not is_pack_detail:
                return
            if isinstance(status, int) and not 200 <= status < 300:
                response_errors.append(f"详情响应 HTTP {status}")
                return
            if isinstance(payload, dict):
                response_product_id, _, identity_error = _detail_product_sources(payload, product_id)
                if response_product_id == product_id and not identity_error:
                    detail_payloads.append(payload)
                elif response_product_id or identity_error:
                    response_errors.append(
                        f"忽略了无法匹配当前商品的详情响应（商品 ID {response_product_id or '未提供'}；{identity_error}）"
                    )
        except Exception as error:
            if is_pack_detail:
                response_errors.append(f"详情响应解析失败：{type(error).__name__}")

    context.on("page", on_page)
    context.on("response", on_detail)
    try:
        _click_candidate_detail_target(title_locator, detail_card, product_id, str(candidate.get("title") or ""))
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and not detail_payloads:
            for detail_page in [page, *opened_pages]:
                prompt_kind, prompt_text = _visible_baiying_prompt(detail_page)
                if prompt_kind == "manual_verification":
                    raise RuntimeError("打开商品详情时百应要求手动完成验证；未加车，任务已停止。")
                if prompt_kind in {"login", "account_unavailable"}:
                    raise RuntimeError(f"打开商品详情时百应账号状态异常：{prompt_text or prompt_kind}；任务已停止。")
            page.wait_for_timeout(200)
    except Exception as error:
        if isinstance(error, CandidateDetailClickBlockedError):
            prompt_kind, prompt_text = _visible_baiying_prompt(page)
            if prompt_kind:
                raise RuntimeError(
                    f"商品详情点击被拦截后页面状态不确定（{prompt_text or prompt_kind or page.url}）；已停止后续加车。"
                ) from error
            # 先清理新开的详情页并恢复列表，再按同一商品 ID 复核；通过后只跳过此商品。
            detail_click_skip_reason = str(error)
        elif isinstance(error, RuntimeError):
            raise
        elif _selection_error_requires_pause(error):
            raise RuntimeError(f"商品数据分析遇到账号、限流或页面异常：{error}") from error
        else:
            response_errors.append(f"打开详情失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}")
    finally:
        if diagnostics is not None:
            diagnostics["finalPagePaths"] = list(dict.fromkeys(
                _detail_diagnostic_path(getattr(detail_page, "url", ""))
                for detail_page in [page, *opened_pages]
            ))[:5]
        try:
            context.remove_listener("page", on_page)
        except Exception:
            pass
        try:
            context.remove_listener("response", on_detail)
        except Exception:
            pass
        for detail_page in opened_pages:
            try:
                if not detail_page.is_closed():
                    detail_page.close()
            except Exception:
                pass
        if page.url != original_url:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=15_000)
            except Exception as error:
                raise RuntimeError(f"商品详情分析后无法返回选品列表，已停止加车：{_safe_diagnostic_text(error, 300)}") from error

    if not detail_payloads:
        if page.url != original_url:
            raise RuntimeError(f"商品 ID {product_id} 的详情页未能返回原选品列表，无法继续复核后续商品。")
        try:
            # 只有原列表仍可按目标 ID 唯一定位时，详情点击/响应失败才降级为单品跳过。
            _locate_candidate_card(page, detail_candidate)
        except Exception as verify_error:
            raise RuntimeError(
                f"商品 ID {product_id} 详情失败后无法复核原商品行，需暂停后续操作：{verify_error}"
            ) from verify_error
        candidate["_detailClickSkipped"] = True
        reason = detail_click_skip_reason or "; ".join(response_errors) or (
            f"打开商品详情后 {timeout_seconds} 秒内未捕获到与目标商品 ID 匹配的 pack_detail 响应"
        )
        candidate["_detailClickSkipReason"] = reason
        return None, reason + "；原列表商品 ID 已复核，未加车，已跳过此商品"
    payload = detail_payloads[-1]
    metrics = _detail_metrics(payload, product_id)
    if metrics.get("error"):
        error_text = str(metrics["error"])
        if "不一致" in error_text or "没有与候选商品 ID" in error_text:
            raise RuntimeError(error_text + "；身份无法确认，已停止后续加车。")
        return payload, error_text
    _cache_detail_payload(context, product_id, payload)
    return payload, ""


def _apply_detail_metrics(candidate: Dict[str, Any], payload: Optional[Dict[str, Any]], error: str) -> None:
    product_id = str(candidate.get("productId") or "")
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    data_metrics = candidate.get("dataMetrics") if isinstance(candidate.get("dataMetrics"), dict) else {}
    metric_sources = candidate.get("metricSources") if isinstance(candidate.get("metricSources"), dict) else {}
    if payload is not None:
        detail_id, detail_title, gallery, product_url = _product_gallery_urls(payload)
        if detail_id and detail_id != product_id:
            raise RuntimeError(f"商品详情 ID {detail_id} 与候选 ID {product_id} 不一致，已停止加车。")
        detail = _detail_metrics(payload, product_id)
        if detail.get("error"):
            error = str(detail["error"])
        elif detail.get("productId") == product_id:
            for key, value in (detail.get("metrics") or {}).items():
                if value is not None:
                    data_metrics[key] = value
                    metric_sources[key] = "商品详情"
            trend = detail.get("trend") if isinstance(detail.get("trend"), dict) else {}
            if trend.get("known") is True:
                candidate["trendKnown"] = True
                candidate["trendStatus"] = str(trend.get("status") or "unknown")
                candidate["trendRising"] = trend.get("status") == "rising"
                candidate["trendSlope"] = trend.get("slope")
                candidate["trendPoints"] = int(trend.get("points") or 0)
            candidate["detailProductId"] = detail_id or product_id
            candidate["detailTitle"] = detail_title or str(candidate.get("title") or "")
            candidate["detailGalleryUrls"] = gallery
            candidate["detailProductUrl"] = product_url
    candidate["dataMetrics"] = data_metrics
    candidate["metricSources"] = metric_sources
    candidate["detailCapturedAt"] = captured_at
    candidate["analysisStatus"] = "详情已读取" if payload is not None and not error else "详情数据未完整读取"
    candidate["analysisError"] = error


def _candidate_quality_scores(candidates: list[Dict[str, Any]], settings: Optional[Dict[str, Any]]) -> None:
    weights = {
        "salesAmount": 0.23,
        "salesCount": 0.18,
        "conversionRate": 0.18,
        "views": 0.09,
        "commissionRate": 0.09,
        "experienceScore": 0.08,
        "trend": 0.15,
    }
    if settings is not None and settings.get("trendPreference") != "上升优先":
        weights.pop("trend", None)
    metric_labels = {
        "salesAmount": "销售额",
        "salesCount": "销量",
        "conversionRate": "转化率",
        "views": "浏览量",
        "commissionRate": "佣金率",
        "experienceScore": "体验分",
        "trend": "趋势走势",
    }
    value_lists: Dict[str, list[float]] = {}
    for metric in metric_labels:
        values = []
        for candidate in candidates:
            data = candidate.get("dataMetrics") if isinstance(candidate.get("dataMetrics"), dict) else {}
            value = _number(data.get(metric))
            if value is not None:
                values.append(value)
        value_lists[metric] = values

    def percentile(value: float, values: list[float]) -> Optional[float]:
        if not values:
            return None
        less = sum(1 for item in values if item < value)
        equal = sum(1 for item in values if item == value)
        if len(values) == 1 or len(set(values)) == 1:
            return 50.0
        return ((less + (equal - 1) / 2) / (len(values) - 1)) * 100

    for candidate in candidates:
        data = candidate.get("dataMetrics") if isinstance(candidate.get("dataMetrics"), dict) else {}
        score_total = 0.0
        weight_total = 0.0
        reasons: list[str] = []
        known_count = 0
        for metric, weight in weights.items():
            if metric == "trend":
                if candidate.get("trendKnown") is not True:
                    continue
                trend_status = str(candidate.get("trendStatus") or "unknown")
                score_value = 100.0 if trend_status == "rising" else 50.0 if trend_status == "flat" else 0.0
                reasons.append("走势" + ("上升" if trend_status == "rising" else "持平" if trend_status == "flat" else "下降"))
                known_count += 1
            else:
                value = _number(data.get(metric))
                if value is None:
                    continue
                score_value = percentile(value, value_lists[metric]) or 0.0
                reasons.append(f"{metric_labels[metric]} {value:g}（来源：{(candidate.get('metricSources') or {}).get(metric, '平台列表')}）")
                known_count += 1
            score_total += score_value * weight
            weight_total += weight
        raw_score = score_total / weight_total if weight_total else None
        coverage_ratio = known_count / len(weights) if weights else 0.0
        score = round(raw_score * coverage_ratio, 1) if raw_score is not None else None
        candidate["analysisScore"] = score
        candidate["analysisRawScore"] = round(raw_score, 1) if raw_score is not None else None
        candidate["analysisCoverage"] = known_count
        candidate["analysisMetricCount"] = len(weights)
        missing_metrics = [metric for metric in metric_labels if metric != "trend" and _number(data.get(metric)) is None]
        if "trend" in weights and candidate.get("trendKnown") is not True:
            missing_metrics.append("trend")
        candidate["analysisMissingMetrics"] = missing_metrics
        if score is not None:
            reasons.insert(0, f"候选内综合评分 {score:g}/100（原始相对分 {raw_score:.1f}，覆盖 {known_count}/{len(weights)} 项，未覆盖项按 0 折算）")
        if missing_metrics:
            reasons.insert(1 if score is not None else 0, "未提供实测指标：" + "、".join(metric_labels.get(key, key) for key in missing_metrics))
        estimated_sales_amount = _number(candidate.get("estimatedSalesAmount"))
        if data.get("salesAmount") is None and estimated_sales_amount is not None:
            reasons.append(f"销售额估算值 {estimated_sales_amount:g}（价格×月销，仅作参考，不参与阈值或排序）")
        if candidate.get("sources"):
            reasons.append("热点/榜单来源优先")
        if candidate.get("pendingThresholds"):
            reasons.append("待核验指标：" + "、".join(metric_labels.get(key, key) for key in candidate["pendingThresholds"]))
        if candidate.get("analysisError"):
            reasons.append("详情数据：" + str(candidate["analysisError"]))
        candidate["analysisReasons"] = reasons


def _evaluate_candidate_thresholds(candidate: Dict[str, Any], settings: Dict[str, Any]) -> list[str]:
    thresholds = settings.get("thresholds") if isinstance(settings.get("thresholds"), dict) else {}
    commission_lower, commission_upper = _commission_rate_bounds(settings)
    metric_labels = {
        "salesAmount": "销售额",
        "salesCount": "销量",
        "conversionRate": "转化率",
        "views": "浏览量",
        "commissionRate": "佣金率",
        "experienceScore": "体验分",
    }
    data = candidate.get("dataMetrics") if isinstance(candidate.get("dataMetrics"), dict) else {}
    reasons: list[str] = []
    for metric, threshold in thresholds.items():
        if metric in {"commissionRate", "commissionRateMax"}:
            continue
        if threshold is None or threshold == "":
            continue
        actual = _number(data.get(metric))
        label = metric_labels.get(metric, metric)
        try:
            boundary = float(threshold)
        except (TypeError, ValueError):
            continue
        if actual is None:
            reasons.append(f"缺少{label}实测数据，无法核验阈值 {boundary:g}")
        elif actual < boundary:
            reasons.append(f"{label} {actual:g} 低于设定阈值 {boundary:g}")
    if commission_lower is not None or commission_upper is not None:
        actual = _number(data.get("commissionRate"))
        if actual is None or not math.isfinite(actual) or not 0 <= actual <= 100:
            reasons.append("缺少有效佣金率实测数据，无法核验设定区间")
        elif commission_lower is not None and actual < commission_lower:
            reasons.append(f"佣金率 {actual:g} 低于设定下限 {commission_lower:g}")
        elif commission_upper is not None and actual > commission_upper:
            reasons.append(f"佣金率 {actual:g} 超过设定上限 {commission_upper:g}")
    candidate["thresholdReasons"] = reasons
    candidate["eligible"] = not reasons
    return reasons


def _is_alliance_direct_cart_row(candidate: Dict[str, Any], card_locator: Any) -> bool:
    """联盟榜候选在表格/行容器中时使用同一 productId 行的直接加车语义。"""
    if str(candidate.get("sourceKey") or "") != "allianceRank":
        return False
    try:
        return bool(card_locator.evaluate(
            "element => element.matches('tr, [role=row], [class*=\"__row___\"]') "
            "|| !element.querySelector('[class*=\"__cardContent___\"]')"
        ))
    except Exception:
        return False


def _locate_alliance_row_add_cart_control(
    card_locator: Any, product_id: str
) -> tuple[Optional[Any], str]:
    """在已锚定 productId 的联盟行内唯一定位加车控件；不使用 data-row-key。"""
    expected_id = str(product_id or "").strip()
    exposed_ids = _candidate_card_product_ids(card_locator)
    if not expected_id or expected_id not in exposed_ids:
        raise RuntimeError(
            f"联盟行未能通过商品 ID {expected_id or '缺失'} 复核；拒绝点击行内加车控件。"
        )
    controls = card_locator.locator('button.add-cart, button, [role="button"]')
    class_matches: list[Any] = []
    text_matches: list[Any] = []
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            class_name = str(control.get_attribute("class") or "")
            label = " ".join((control.inner_text() or "").split())
            aria_label = " ".join(str(control.get_attribute("aria-label") or "").split())
            accessible_text = " ".join((label + " " + aria_label).split())
            if re.search(r"已加入|已在选品车|已加车|无库存|缺货|售罄|已下架|不可加|暂不可带", accessible_text):
                continue
            if re.search(r"(?:^|\s)add-cart(?:\s|$)", class_name):
                class_matches.append(control)
            elif re.search(r"加入选品车|加选品车|加入购物车|加车", accessible_text):
                text_matches.append(control)
        except Exception:
            continue
    if len(class_matches) == 1:
        return class_matches[0], "button.add-cart"
    if len(class_matches) > 1:
        return None, "同一商品行有多个可见 button.add-cart 控件"
    if len(text_matches) == 1:
        return text_matches[0], "行内加选品车文案"
    if len(text_matches) > 1:
        return None, "同一商品行有多个文案匹配的加车控件"
    return None, "同一商品行没有可用的加车控件"


def _write_cart_control_failure_diagnostic(
    page: Page,
    candidate: Dict[str, Any],
    reason: str,
    selector_hit_counts: Dict[str, Optional[int]],
) -> Dict[str, str]:
    """记录加车动作前的控件定位失败现场，不记录请求内容或页面文本。"""
    diagnostic_id = f"cart-control-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
    root = Path(os.environ.get("TOUGUANG_DATA_DIR", r"D:\software\投广客户端\data"))
    diagnostic_dir = root / "diagnostics" / "selection"
    json_path = diagnostic_dir / f"{diagnostic_id}.json"
    screenshot_path = diagnostic_dir / f"{diagnostic_id}.png"
    try:
        title = str(page.title() or "")[:160]
    except Exception as error:
        title = f"读取失败：{type(error).__name__}"
    try:
        ready_state = str(page.evaluate("() => document.readyState") or "unknown")
    except Exception as error:
        ready_state = f"读取失败：{type(error).__name__}"
    payload: Dict[str, Any] = {
        "diagnosticId": diagnostic_id,
        "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "classification": "CONTROL_FAILURE",
        "stage": "pre_add_to_cart",
        "productId": str(candidate.get("productId") or ""),
        "reason": _safe_diagnostic_text(reason, 800),
        "url": _searchbox_page_url(page),
        "title": title,
        "readyState": ready_state,
        "selectorHitCounts": selector_hit_counts,
        "privacy": "未保存 Cookie、请求头、请求体、URL 查询参数或整页文本。",
    }
    try:
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
            payload["screenshotPath"] = str(screenshot_path)
        except Exception as error:
            payload["screenshotError"] = type(error).__name__
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        payload["writeError"] = type(error).__name__
    return {
        "url": str(payload.get("url") or ""),
        "title": str(payload.get("title") or ""),
        "readyState": str(payload.get("readyState") or "unknown"),
        "selectorHitCounts": json.dumps(selector_hit_counts, ensure_ascii=False, sort_keys=True),
        "screenshotPath": str(payload.get("screenshotPath") or "未能保存"),
        "jsonPath": str(json_path) if json_path.exists() else "未能保存",
    }


def _safe_locator_count(container: Any, selector: str) -> Optional[int]:
    try:
        return int(container.locator(selector).count())
    except Exception:
        return None


def _cart_control_failure_result(
    page: Page,
    candidate: Dict[str, Any],
    reason: str,
    selector_hit_counts: Dict[str, Optional[int]],
) -> Dict[str, Any]:
    diagnostic = _write_cart_control_failure_diagnostic(page, candidate, reason, selector_hit_counts)
    detail = (
        f"释放原因=控件类失败；URL={diagnostic['url']}, title={diagnostic['title']}, "
        f"readyState={diagnostic['readyState']}, selectorHitCounts={diagnostic['selectorHitCounts']}, "
        f"screenshotPath={diagnostic['screenshotPath']}, jsonPath={diagnostic['jsonPath']}"
    )
    return {
        "productId": str(candidate.get("productId") or ""),
        "title": str(candidate.get("title") or ""),
        "cartAdded": False,
        "cartAttempted": False,
        "cartFailureClass": "CONTROL_FAILURE",
        "controlDiagnosticPath": diagnostic["jsonPath"],
        "skipped": True,
        "cartStatus": "加车前控件定位失败；预留将自动释放",
        "status": f"加入选品车失败：{_safe_diagnostic_text(reason, 500)}；未点击加车；{detail}",
        "error": str(reason),
    }


def _is_cart_control_failure_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "locator", "selector", "strict mode", "button", "control", "timeout", "timed out",
        "定位", "选择器", "控件", "按钮", "命中", "唯一可定位", "没有可用",
    ))


def execute_selected_candidates(
    page: Page,
    context: BrowserContext,
    candidates: list[Dict[str, Any]],
    selected_indexes: list[int],
    download_root: str,
    settings: Optional[Dict[str, Any]] = None,
    stop_after_cart: bool = False,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    analyze_before_cart: bool = False,
    max_selected_products: Optional[int] = None,
) -> list[Dict[str, Any]]:
    completed: list[Dict[str, Any]] = []
    selected_candidates = [
        next((row for row in candidates if row.get("cardIndex") == card_index), None)
        for card_index in selected_indexes
    ]
    selected_candidates = [row for row in selected_candidates if row is not None]
    safe_selected_candidates: list[Dict[str, Any]] = []
    for candidate in selected_candidates:
        matched_word = _title_banned_word(candidate.get("title"), settings)
        if not matched_word:
            safe_selected_candidates.append(candidate)
            continue
        reason = f"商品标题命中屏蔽词「{matched_word}」"
        raw_reasons = candidate.get("thresholdReasons")
        existing_reasons = [str(value) for value in raw_reasons if str(value)] if isinstance(raw_reasons, list) else []
        candidate.update({
            "titleBannedWord": matched_word,
            "titleFilterExcluded": True,
            "analysisStatus": "商品标题命中屏蔽词",
            "thresholdReasons": list(dict.fromkeys([*existing_reasons, reason])),
            "cartAdded": candidate.get("cartAdded") is True,
            "skipped": True,
            "status": reason + "；已停止当前商品的加车及后续处理。",
        })
        completed.append(dict(candidate))
        _report_operation_progress(
            progress, "商品标题屏蔽", index=len(completed), total=len(selected_candidates),
            message=f"{candidate.get('title') or candidate.get('productId')}：{reason}，已跳过。",
            product_id=str(candidate.get("productId") or ""),
            product_title=str(candidate.get("title") or ""),
        )
    selected_candidates = safe_selected_candidates
    selected_indexes = [int(candidate["cardIndex"]) for candidate in selected_candidates]
    if not selected_candidates:
        return completed

    if (
        analyze_before_cart
        and not stop_after_cart
        and settings is not None
        and selected_candidates
        and not any(candidate.get("cartAttempted") is True for candidate in selected_candidates)
    ):
        analysis_target = len(selected_candidates)
        if max_selected_products is not None:
            try:
                analysis_target = max(0, int(max_selected_products))
            except (TypeError, ValueError):
                analysis_target = len(selected_candidates)
        rejected_candidates = []
        eligible_candidates = []
        analyzed_candidates = []
        source_refresh_skipped_count = 0
        business_skip_count = 0
        for position, candidate in enumerate(selected_candidates, start=1):
            if len(eligible_candidates) >= analysis_target:
                break
            candidate["analysisAttempted"] = True
            product_id = str(candidate.get("productId") or "")
            title = str(candidate.get("title") or product_id)
            _report_operation_progress(
                progress,
                "商品数据分析",
                index=position,
                total=len(selected_candidates),
                percent=int((position - 1) * 100 / len(selected_candidates)),
                message=f"打开商品详情核验数据与趋势 {position}/{len(selected_candidates)}：{title}",
                product_id=product_id,
                product_title=title,
            )
            if position > 1:
                page.wait_for_timeout(DETAIL_ANALYSIS_INTERVAL_MS)
            try:
                payload, detail_error = _capture_candidate_detail(
                    page, context, candidate, settings, progress
                )
                _apply_detail_metrics(candidate, payload, detail_error)
                if settings.get("_poolConsumerOnly") is True:
                    candidate["_poolDetailVerified"] = payload is not None and not detail_error
                    category_ids = candidate.get("categoryIds") if isinstance(candidate.get("categoryIds"), list) else []
                    if len(category_ids) != 3 and payload is not None:
                        category_ids, _ = _detail_category_path(
                            payload, product_id,
                            settings.get("categoryTree", []) if isinstance(settings.get("categoryTree"), list) else [],
                        )
                        if len(category_ids) == 3:
                            candidate["categoryIds"] = category_ids
                            candidate["leafCategoryId"] = category_ids[-1]
                            candidate["categoryStatus"] = "known"
            except CandidateCartSkip as error:
                skipped = _candidate_skip_result(candidate, error)
                candidate.update(skipped)
                rejected_candidates.append(candidate)
                business_skip_count += 1
                _report_operation_progress(
                    progress,
                    "商品数据分析",
                    index=position,
                    total=len(selected_candidates),
                    percent=int(position * 100 / len(selected_candidates)),
                    message=skipped["status"],
                    product_id=product_id,
                    product_title=title,
                )
                continue
            except CandidateSourceRefreshSkip as error:
                reason = str(error)
                candidate["analysisRejected"] = True
                candidate["thresholdReasons"] = [reason]
                candidate["cartAdded"] = False
                candidate["skipped"] = True
                candidate["status"] = reason + "；已跳过此商品，继续检查后续候选"
                rejected_candidates.append(candidate)
                source_refresh_skipped_count += 1
                _report_operation_progress(
                    progress,
                    "商品数据分析",
                    index=position,
                    total=len(selected_candidates),
                    percent=int(position * 100 / len(selected_candidates)),
                    message=f"来源类目复核未通过，已跳过并继续：{title}",
                    product_id=product_id,
                    product_title=title,
                )
                continue
            except Exception as error:
                identity_failure = any(
                    marker in str(error) for marker in ("商品 ID", "身份无法", "身份不一致", "无法返回选品列表", "多个包含该商品")
                )
                if identity_failure:
                    reason = f"商品身份/详情恢复未确认：{error}；已隔离当前商品并继续后续候选"
                    candidate["analysisRejected"] = True
                    candidate["thresholdReasons"] = [reason]
                    candidate["status"] = reason
                    candidate["cartAdded"] = False
                    candidate["skipped"] = True
                    rejected_candidates.append(candidate)
                    _report_operation_progress(
                        progress, "商品数据分析", index=position, total=len(selected_candidates),
                        percent=int(position * 100 / len(selected_candidates)), message=reason,
                        product_id=product_id, product_title=title,
                    )
                    continue
                if _selection_error_requires_pause(error):
                    raise RuntimeError(f"商品数据分析失败并触发安全停止：{error}") from error
                _apply_detail_metrics(candidate, None, str(error))
            analyzed_candidates.append(candidate)
            if candidate.pop("_detailClickSkipped", False):
                detail_skip_reason = str(candidate.pop("_detailClickSkipReason", "") or "详情未能成功打开")
                reason = f"{detail_skip_reason}；当前列表与商品 ID 已复核，未加车，已跳过此商品"
                candidate["analysisRejected"] = True
                candidate["thresholdReasons"] = [reason]
                candidate["analysisReasons"] = [reason]
                candidate["status"] = reason + "，继续检查后续候选"
                candidate["cartAdded"] = False
                candidate["skipped"] = True
                rejected_candidates.append(candidate)
                _report_operation_progress(
                    progress,
                    "商品数据分析",
                    index=position,
                    total=len(selected_candidates),
                    percent=int(position * 100 / len(selected_candidates)),
                    message=f"{detail_skip_reason}；已跳过当前商品并继续：{title}",
                    product_id=product_id,
                    product_title=title,
                )
                continue
            reasons = _evaluate_candidate_thresholds(candidate, settings)
            if settings.get("_poolConsumerOnly") is True:
                if candidate.pop("_poolDetailVerified", False) is not True:
                    reasons.append("当前账号商品详情未成功打开并核对；不能加车")
                category_ids = candidate.get("categoryIds") if isinstance(candidate.get("categoryIds"), list) else []
                source_ids = candidate.get("sourceCategoryIds") if isinstance(candidate.get("sourceCategoryIds"), list) else []
                if not _candidate_source_category_satisfies_general_exclusions(candidate, settings):
                    reasons.append("采集时的页面分类路径不足以核对当前账号类目条件；不能加车")
                if len(category_ids) == 3:
                    if source_ids and any(
                        not _category_id_matches(str(category_ids[index]), str(source_ids[index]))
                        for index in range(min(len(source_ids), 3))
                    ):
                        reasons.append("当前账号商品类目与采集时已确认的页面分类冲突")
                    excluded = _excluded_category_selection_ids(settings)
                    if _candidate_matches_manual_categories(candidate, excluded):
                        reasons.append("商品属于当前账号设置的排除类目")
                    if settings.get("selectionMode") == "category" and settings.get("categoryMode") == "manual" and not _candidate_matches_manual_categories(candidate, _manual_category_selection_ids(settings)):
                        reasons.append("商品不属于当前账号手动选择的类目范围")
                candidate["thresholdReasons"] = reasons
                candidate["eligible"] = not reasons
            if reasons:
                candidate["analysisRejected"] = True
                candidate["thresholdReasons"] = reasons
                candidate["status"] = "数据分析未通过筛选：" + "；".join(reasons)
                candidate["cartAdded"] = False
                candidate["skipped"] = True
                rejected_candidates.append(candidate)
            else:
                candidate["analysisRejected"] = False
                eligible_candidates.append(candidate)
        _candidate_quality_scores(analyzed_candidates, settings)
        for candidate in rejected_candidates:
            candidate["analysisReasons"] = list(candidate.get("analysisReasons") or []) + list(
                candidate.get("thresholdReasons") or []
            )
        eligible_candidates.sort(key=lambda row: _candidate_rank(row, settings))
        selected_indexes = [int(candidate["cardIndex"]) for candidate in eligible_candidates]
        completed.extend(rejected_candidates)
        if analyzed_candidates or source_refresh_skipped_count or business_skip_count:
            _report_operation_progress(
                progress,
                "商品数据分析",
                index=min(len(selected_candidates), len(analyzed_candidates) + source_refresh_skipped_count),
                total=len(selected_candidates),
                percent=100,
                message=(
                    f"详情分析 {len(analyzed_candidates)} 个、来源复核跳过 {source_refresh_skipped_count} 个、"
                    f"无库存/错品跳过 {business_skip_count} 个，"
                    f"{len(eligible_candidates)} 个达标；本轮最多加车 {analysis_target} 个"
                ),
            )

    total_selected = len(selected_indexes)
    for position, card_index in enumerate(selected_indexes, start=1):
        if max_selected_products is not None:
            try:
                cart_target = max(0, int(max_selected_products))
            except (TypeError, ValueError):
                cart_target = len(selected_indexes)
            confirmed_cart_count = sum(1 for item in completed if item.get("cartAdded") is True)
            if confirmed_cart_count >= cart_target:
                break
        candidate = next((row for row in candidates if row.get("cardIndex") == card_index), None)
        if candidate is None:
            continue
        product_id = str(candidate.get("productId") or "").strip()
        if not product_id:
            raise RuntimeError("候选商品缺少商品 ID，身份无法唯一确认；已停止加车并暂停当前批次")
        _report_operation_progress(
            progress,
            "核验并处理选中商品",
            index=position,
            total=total_selected,
            message=(
                f"titleDirectCart 准备加车 {position}/{total_selected}：{str(candidate.get('title') or product_id)}"
                if str(candidate.get("sourceKey") or "") == "allianceRank" else
                f"商品 {position}/{total_selected}：{str(candidate.get('title') or product_id)}"
            ),
            product_id=product_id,
            product_title=str(candidate.get("title") or ""),
        )
        platform_notices: list[str] = []
        prompt_kind, prompt_text = _visible_baiying_prompt(page)
        if prompt_kind == "manual_verification":
            completed.append(_manual_verification_result(candidate, cart_attempted=False))
            break
        if prompt_kind == "coupon_recommendation":
            if not _dismiss_baiying_coupon_recommendation(page):
                completed.append({
                    "productId": str(candidate.get("productId") or ""),
                    "title": str(candidate.get("title") or ""),
                    "cartAdded": False,
                    "cartStatus": "推荐弹窗待处理",
                    "status": "百应消费券推荐弹窗已识别，但没有确认关闭；未操作推荐商品，也未继续当前商品。",
                })
                break
            platform_notices.append(
                "已关闭消费券推荐弹窗，未点击推荐商品；加车结果仍按当前商品 ID 核验。"
            )
        elif (
            prompt_kind == "cart_error"
            and _candidate_unavailable_reason(prompt_text)
            and _is_product_specific_cart_rejection(prompt_text)
        ):
            # 上一候选的无库存提示可能仍短暂显示；不把它当作当前候选结果，
            # 尝试关闭后继续核验当前候选，避免把上一商品的 toast 当成整批阻断。
            _dismiss_cart_error_dialog(page)
            platform_notices.append("已识别百应残留的无库存提示，不作为当前候选结果；继续核验当前商品")
        elif prompt_kind in {"invoice_eligibility", "cart_error"}:
            completed.append({
                "productId": str(candidate.get("productId") or ""),
                "title": str(candidate.get("title") or ""),
                "cartAdded": False,
                "cartStatus": "加车状态待核验",
                "status": (
                    "百应页面残留弹窗无法确认属于当前商品；未点击“一键签约”或其他弹窗操作，"
                    f"已停止当前账号任务（{prompt_text}）。"
                ),
            })
            break
        source_selection = candidate.get("sourceCategorySelection")
        product_id = str(candidate.get("productId") or "")
        if (
            settings is not None
            and product_id
            and isinstance(source_selection, dict)
            and any(source_selection.get(level) for level in ("level1", "level2", "level3"))
        ):
            # 合并多个类目结果时，候选可能来自不是当前显示的列表；先检查页面签名和商品 ID。
            # 若候选已在当前来源结果中，直接复用当前卡片，避免再次查询时平台结果变化。
            candidate_settings = dict(settings)
            candidate_settings["categorySelection"] = source_selection
            try:
                if not _candidate_is_current_for_source(page, candidate, candidate_settings):
                    refreshed = _apply_one_category_and_collect_candidates(
                        page, candidate_settings, force_refresh=True, preserve_product_ids=[product_id],
                        include_source_ids=True,
                    )
                    match_state, live_candidate, match_reason = _refreshed_candidate_by_id(refreshed, product_id)
                    if match_state == "ambiguous":
                        raise RuntimeError(match_reason)
                    if match_state == "filtered":
                        raise CandidateSourceRefreshSkip(
                            f"商品 ID {product_id} 仍在来源类目原始结果中，但刷新后不满足筛选条件：{match_reason}"
                        )
                    if match_state == "raw_only":
                        raise CandidateSourceRefreshSkip(
                            f"商品 ID {product_id} 仍在原始类目响应中，但候选解析未生成可用记录（{match_reason}）"
                        )
                    if match_state == "promotion_id_only":
                        raise CandidateSourceRefreshSkip(
                            f"商品 ID {product_id} 只匹配到推广 ID，无法安全确认商品身份；已跳过（{match_reason}）"
                        )
                    if match_state == "absent":
                        raise CandidateSourceRefreshSkip(
                            f"商品 ID {product_id} 未出现在刷新后的来源类目结果中；已跳过（{match_reason}）"
                        )
                    if live_candidate is None:
                        raise RuntimeError(f"刷新后的候选状态异常，无法确认商品 ID {product_id}")
                    live_candidate["sourceCategorySelection"] = source_selection
                    if candidate.get("cartAttempted") is True:
                        live_candidate["cartAttempted"] = True
                    candidate = {**live_candidate, **candidate}
                    candidate["cardIndex"] = live_candidate.get("cardIndex", candidate.get("cardIndex"))
            except Exception as error:
                if isinstance(error, CandidateCartSkip):
                    skipped = _candidate_skip_result(
                        candidate, error, cart_attempted=candidate.get("cartAttempted") is True
                    )
                    completed.append(skipped)
                    _report_operation_progress(
                        progress,
                        "核验并处理选中商品",
                        index=position,
                        total=total_selected,
                        message=skipped["status"],
                        product_id=product_id,
                        product_title=str(candidate.get("title") or ""),
                    )
                    continue
                if isinstance(error, CandidateSourceRefreshSkip):
                    attempted_before = candidate.get("cartAttempted") is True
                    completed.append({
                        "productId": product_id,
                        "title": str(candidate.get("title") or ""),
                        "cartAdded": False,
                        "cartAttempted": attempted_before,
                        "skipped": True,
                        "cartStatus": "状态待核验" if attempted_before else "加车失败",
                        "status": (
                            f"上次曾尝试加车，当前复核信息不足以确认结果；不重试本商品，继续下一候选（{error}）"
                            if attempted_before else
                            f"来源类目复核后当前商品不再可安全加入，已跳过并继续其他商品（{error}）"
                        ),
                    })
                    continue
                if _selection_error_requires_pause(error):
                    raise RuntimeError(
                        f"商品 {product_id} 按来源类目恢复候选失败，属于账号/限流/页面状态异常；"
                        f"已暂停后续平台请求：{error}"
                    ) from error
                attempted_before = candidate.get("cartAttempted") is True
                completed.append({
                    "productId": product_id,
                    "title": str(candidate.get("title") or ""),
                    "cartAdded": False,
                    "cartAttempted": attempted_before,
                    "skipped": True,
                    "cartStatus": "状态待核验" if attempted_before else "加车失败",
                    "status": (
                        f"上次尝试加车后无法按来源类目找到原商品；不重试本商品，继续下一候选（{error}）"
                        if attempted_before else
                        f"加入选品车失败：按来源类目恢复候选时未找到商品，未点击加车；已跳过此商品并继续（{error}）"
                    ),
                })
                continue
        title = str(candidate.get("title") or "")
        try:
            title_locator, card = _locate_candidate_card(page, candidate)
        except Exception as error:
            if isinstance(error, CandidateCartSkip):
                skipped = _candidate_skip_result(
                    candidate, error, cart_attempted=candidate.get("cartAttempted") is True
                )
                completed.append(skipped)
                _report_operation_progress(
                    progress,
                    "核验并处理选中商品",
                    index=position,
                    total=total_selected,
                    message=skipped["status"],
                    product_id=product_id,
                    product_title=title,
                )
                continue
            attempted_before = candidate.get("cartAttempted") is True
            must_pause = _selection_error_requires_pause(error)
            message = str(error).lower()
            control_failure = not attempted_before and not any(marker in message for marker in (
                "11001", "冷却", "限流", "频控", "captcha", "verification", "验证码", "安全验证",
                "login", "unauthorized", "target closed", "browser has been closed", "context has been closed",
                "browser disconnected", "connection closed", "websocket", "cdp", "远程调试", "浏览器已关闭",
            ))
            if control_failure:
                failure = _cart_control_failure_result(
                    page,
                    candidate,
                    str(error),
                    {
                        "candidateCardRows": _safe_locator_count(
                            page, "div[class*='__card___'], tr, [role='row'], [class*='__row___']"
                        ),
                        "addCartButtons": _safe_locator_count(page, "button.add-cart"),
                    },
                )
                if must_pause:
                    failure["status"] += "；页面状态异常，暂停本账号后续平台操作"
            else:
                failure = {
                    "productId": candidate.get("productId"), "title": title,
                    "cartAdded": False, "cartAttempted": attempted_before,
                    **({} if must_pause else {"skipped": True}),
                    "cartStatus": "状态待核验" if attempted_before or must_pause else "加车失败",
                    "status": (
                        f"候选商品无法安全唯一定位；暂停后续操作（{error}）"
                        if must_pause else
                        f"上次尝试加车后无法定位原商品；不重试本商品，已跳过并继续下一个候选（{error}）"
                    ),
                }
            completed.append(failure)
            if must_pause:
                break
            continue
        alliance_direct_cart = _is_alliance_direct_cart_row(candidate, card)
        cart_path = "titleDirectCart" if alliance_direct_cart else "cardAddCart"
        candidate["cartPath"] = cart_path
        already_in_cart = candidate.get("cartAdded") is True
        if not already_in_cart:
            try:
                before_text = " ".join((card.inner_text() or "").split())
                already_in_cart = _candidate_is_in_cart(candidate, before_text)
            except Exception as error:
                completed.append(_cart_control_failure_result(
                    page,
                    candidate,
                    f"读取商品卡片加车状态失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}",
                    {"button.add-cart": _safe_locator_count(card, "button.add-cart")},
                ))
                if _selection_error_requires_pause(error):
                    break
                continue
        if already_in_cart:
            cart_path = "alreadyInCart"
            candidate["cartPath"] = cart_path
            if analyze_before_cart and candidate.get("cartAdded") is not True and candidate.get("cartAttempted") is not True:
                completed.append({
                    "productId": product_id,
                    "title": title,
                    "cartAdded": False,
                    "cartAttempted": False,
                    "alreadyInCart": True,
                    "skipped": True,
                    "cartStatus": "百应选品车已有此商品",
                    "status": "该商品已在百应选品车；不计入本轮新增数量，继续补选其他商品。",
                })
                continue
        if candidate.get("cartAttempted") is True and not already_in_cart:
            completed.append({
                "productId": str(candidate.get("productId") or ""),
                "title": title,
                "cartAdded": False,
                "cartAttempted": True,
                "skipped": True,
                "cartStatus": "状态待核验（本商品已跳过）",
                "status": (
                    "上次已尝试加车但没有得到确定回执；不重复点击本商品，已跳过并继续下一个候选。"
                ),
            })
            continue
        invoice_prompt_seen = False
        cart_attempted = False
        if not already_in_cart:
            if alliance_direct_cart:
                try:
                    button, control_source = _locate_alliance_row_add_cart_control(
                        card, str(candidate.get("productId") or "")
                    )
                except Exception as error:
                    completed.append(_cart_control_failure_result(
                        page, candidate, str(error),
                        {"button.add-cart, button, [role='button']": _safe_locator_count(
                            card, "button.add-cart, button, [role='button']"
                        )},
                    ))
                    continue
                if button is None:
                    completed.append(_cart_control_failure_result(
                        page, candidate, f"titleDirectCart：{control_source}；未点击其他控件",
                        {"button.add-cart, button, [role='button']": _safe_locator_count(
                            card, "button.add-cart, button, [role='button']"
                        )},
                    ))
                    continue
            else:
                try:
                    button = card.locator("button.add-cart").first
                except Exception as error:
                    completed.append(_cart_control_failure_result(
                        page,
                        candidate,
                        f"定位商品卡片加车按钮失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}",
                        {"button.add-cart": _safe_locator_count(card, "button.add-cart")},
                    ))
                    continue
                control_source = "button.add-cart"
            try:
                button_count = 0 if button is None else button.count()
            except Exception as error:
                completed.append(_cart_control_failure_result(
                    page, candidate, f"读取加车按钮命中数失败：{type(error).__name__}: {_safe_diagnostic_text(error, 300)}",
                    {"button.add-cart": _safe_locator_count(card, "button.add-cart")},
                ))
                continue
            if button is None or not button_count:
                completed.append(_cart_control_failure_result(
                    page,
                    candidate,
                    (
                        f"titleDirectCart：{control_source}；未点击加车"
                        if alliance_direct_cart else
                        "商品卡片没有可用的加车按钮；未点击加车"
                    ),
                    {"button.add-cart": button_count},
                ))
                continue
            cart_success: list[str] = []
            cart_errors: list[str] = []
            confirmed_product_rejections: list[str] = []
            manual_verification = False
            unhandled_prompt = ""
            invoice_action_clicked = False

            def on_cart_response(response: Any) -> None:
                if "/selection_cart_pc_api/card/m_add_card" not in str(getattr(response, "url", "")):
                    return
                try:
                    payload = response.json()
                    succeeded, message = _cart_response_outcome(payload)
                    if succeeded:
                        cart_success.append(message)
                    else:
                        cart_errors.append(message)
                        if _is_product_specific_cart_rejection(message):
                            confirmed_product_rejections.append(message)
                except Exception as error:
                    cart_errors.append(_safe_diagnostic_text(error))

            page.on("response", on_cart_response)
            try:
                preexisting_kind, preexisting_text = _visible_baiying_prompt(page)
                if preexisting_kind == "manual_verification":
                    manual_verification = True
                    completed.append(_manual_verification_result(candidate, cart_attempted=False))
                    break
                if preexisting_kind == "coupon_recommendation":
                    if _dismiss_baiying_coupon_recommendation(page):
                        platform_notices.append(
                            "已关闭消费券推荐弹窗，未点击推荐商品；加车结果仍按当前商品 ID 核验。"
                        )
                        title_locator, card = _locate_candidate_card(page, candidate)
                        if alliance_direct_cart:
                            button, control_source = _locate_alliance_row_add_cart_control(
                                card, str(candidate.get("productId") or "")
                            )
                        else:
                            button = card.locator("button.add-cart").first
                            control_source = "button.add-cart"
                        if button is None or not button.count():
                            raise RuntimeError(
                                f"推荐弹窗关闭后原商品没有可用的加车按钮（{control_source}）"
                            )
                    else:
                        unhandled_prompt = "百应消费券推荐弹窗无法安全关闭；未操作推荐商品，也未继续加车。"
                        raise RuntimeError(unhandled_prompt)
                elif preexisting_kind:
                    if preexisting_kind == "cart_error":
                        _dismiss_cart_error_dialog(page)
                    raise RuntimeError(f"百应页面仍有未处理弹窗，无法确认属于当前商品：{preexisting_text}")
                _report_operation_progress(
                    progress,
                    "标题直接加车" if alliance_direct_cart else "加入选品车",
                    index=position,
                    total=total_selected,
                    message=(
                        f"titleDirectCart：已按商品 ID {candidate.get('productId')} 唯一复核当前行，"
                        f"点击本行加车控件（{control_source}）"
                        if alliance_direct_cart else
                        f"按商品 ID {candidate.get('productId')} 点击当前商品卡片加车控件"
                    ),
                    product_id=str(candidate.get("productId") or ""),
                    product_title=title,
                )
                cart_attempted = True
                button.click(timeout=8_000, no_wait_after=True)
                deadline = time.time() + 15
                while time.time() < deadline:
                    page.wait_for_timeout(100)
                    prompt_kind, prompt_text = _visible_baiying_prompt(page)
                    if prompt_kind == "manual_verification":
                        # 即使此前收到 code=0，也要先人工完成验证并在恢复时按商品 ID 复核。
                        cart_success.clear()
                        manual_verification = True
                        break
                    if prompt_kind in {"login", "account_unavailable"}:
                        unhandled_prompt = f"百应账号状态异常：{prompt_text or prompt_kind}"
                        cart_success.clear()
                        raise RuntimeError(unhandled_prompt)
                    if prompt_kind == "invoice_eligibility":
                        invoice_prompt_seen = True
                        if invoice_action_clicked:
                            cart_success.clear()
                            cart_errors.append("发票资质提醒中的“加选品车”已点击，但平台尚未确认加车")
                            break
                        if not _click_invoice_prompt_add_cart(page):
                            cart_success.clear()
                            cart_errors.append("已识别发票资质提醒，但没有确认唯一的“加选品车”按钮；未签约")
                            break
                        invoice_action_clicked = True
                        continue
                    if prompt_kind == "coupon_recommendation":
                        if not _dismiss_baiying_coupon_recommendation(page):
                            cart_success.clear()
                            unhandled_prompt = "百应消费券推荐弹窗无法安全关闭；未操作推荐商品。"
                            cart_errors.append(unhandled_prompt)
                            break
                        platform_notices.append(
                            "已关闭消费券推荐弹窗，未点击推荐商品；加车结果仍按当前商品 ID 核验。"
                        )
                        try:
                            title_locator, card = _locate_candidate_card(page, candidate)
                        except Exception as error:
                            cart_success.clear()
                            unhandled_prompt = f"推荐弹窗关闭后无法重新确认原商品 ID：{error}"
                            cart_errors.append(unhandled_prompt)
                            break
                        continue
                    if prompt_kind == "cart_error":
                        cart_success.clear()
                        cart_errors.append(f"百应页面拒绝加车：{prompt_text}")
                        if _is_product_specific_cart_rejection(prompt_text):
                            confirmed_product_rejections.append(prompt_text)
                        _dismiss_cart_error_dialog(page)
                        break
                    current_text = " ".join((card.inner_text() or "").split())
                    if re.search(r"已加入|已在选品车|已在购物车|已加选品车|已加车|已加购|已添加|加车成功|加入成功|去选品车", current_text):
                        cart_success.append("百应商品卡片已显示加车成功")
                        break
                    if cart_success:
                        # code=0 的响应后留出短暂时间检查百应异步显示的资格/业务失败弹窗。
                        page.wait_for_timeout(800)
                        prompt_kind, prompt_text = _visible_baiying_prompt(page)
                        if prompt_kind == "manual_verification":
                            cart_success.clear()
                            manual_verification = True
                            break
                        if prompt_kind == "invoice_eligibility":
                            invoice_prompt_seen = True
                            if not invoice_action_clicked and _click_invoice_prompt_add_cart(page):
                                invoice_action_clicked = True
                                continue
                            cart_success.clear()
                            cart_errors.append("发票资质提醒后百应未确认加车")
                            break
                        if prompt_kind == "coupon_recommendation":
                            if _dismiss_baiying_coupon_recommendation(page):
                                platform_notices.append(
                                    "已关闭消费券推荐弹窗，未点击推荐商品；加车结果仍按当前商品 ID 核验。"
                                )
                                continue
                            cart_success.clear()
                            cart_errors.append("百应消费券推荐弹窗无法安全关闭")
                            break
                        if prompt_kind == "cart_error":
                            cart_success.clear()
                            cart_errors.append(f"百应页面拒绝加车：{prompt_text}")
                            if _is_product_specific_cart_rejection(prompt_text):
                                confirmed_product_rejections.append(prompt_text)
                            _dismiss_cart_error_dialog(page)
                        break
                if manual_verification:
                    manual_result = _manual_verification_result(candidate, cart_attempted=True)
                    if invoice_prompt_seen:
                        manual_result["platformWarnings"] = [
                            "百应提示该商品带货需要作者具备开具佣金发票资质；软件未签约。"
                        ]
                    if platform_notices:
                        manual_result["platformNotices"] = list(dict.fromkeys(platform_notices))
                    completed.append(manual_result)
                    break
                if not cart_success:
                    reason = cart_errors[-1] if cart_errors else "百应未返回成功状态"
                    if unhandled_prompt:
                        reason = unhandled_prompt
                    raise RuntimeError(reason)
            except Exception as error:
                if manual_verification:
                    completed.append(_manual_verification_result(candidate, cart_attempted=cart_attempted))
                    break
                if isinstance(error, CandidateCartSkip):
                    skipped = _candidate_skip_result(candidate, error, cart_attempted=cart_attempted)
                    if invoice_prompt_seen:
                        skipped["platformWarnings"] = [
                            "百应提示该商品带货需要开具佣金发票资质；软件未签约。"
                        ]
                    if platform_notices:
                        skipped["platformNotices"] = list(dict.fromkeys(platform_notices))
                    completed.append(skipped)
                    _report_operation_progress(
                        progress,
                        "核验并处理选中商品",
                        index=position,
                        total=total_selected,
                        message=skipped["status"],
                        product_id=product_id,
                        product_title=title,
                    )
                    continue
                product_rejection = next((
                    message for message in reversed(confirmed_product_rejections)
                    if _is_product_specific_cart_rejection(message)
                ), "")
                if product_rejection:
                    unavailable = _candidate_unavailable_reason(product_rejection)
                    rejection_reason = unavailable or _safe_diagnostic_text(product_rejection)
                    rejection_status = (
                        f"商品 ID {product_id}：百应提示{rejection_reason}；已跳过此候选并继续下一候选"
                        if unavailable else
                        f"商品 ID {product_id}：百应明确拒绝当前商品加车，已记录并跳过，继续后续商品（{rejection_reason}）"
                    )
                    completed.append({
                        "productId": candidate.get("productId"), "title": title,
                        "cartAdded": False, "cartAttempted": cart_attempted, "skipped": True,
                        "confirmedCartRejection": True,
                        "analysisRejected": bool(unavailable),
                        "thresholdReasons": [rejection_status],
                        "cartStatus": "商品无库存/不可加车，已跳过" if unavailable else "平台明确拒绝此商品",
                        "status": rejection_status,
                    })
                    continue
                failure = {
                    "productId": candidate.get("productId"), "title": title,
                    "cartAdded": False,
                    "cartStatus": "结果待核验（本商品已跳过）" if cart_attempted else "加车失败",
                    "cartAttempted": cart_attempted,
                    "skipped": True,
                    "status": (
                        f"加入选品车失败：请求已发出但未确认结果；本商品不重试，继续下一个候选"
                        f"（{_safe_diagnostic_text(error)}）"
                        if cart_attempted else
                        f"加入选品车失败：未确认百应已加车；已跳过此商品并继续下一个候选"
                        f"（{_safe_diagnostic_text(error)}）"
                    ),
                }
                if invoice_prompt_seen:
                    failure["platformWarnings"] = [
                        "百应提示该商品带货需要作者具备开具佣金发票资质；软件未签约。"
                    ]
                if platform_notices:
                    failure["platformNotices"] = list(dict.fromkeys(platform_notices))
                try:
                    current_prompt_kind, current_prompt_text = _visible_baiying_prompt(page)
                    page_closed = page.is_closed()
                except Exception:
                    current_prompt_kind, current_prompt_text, page_closed = "unknown", "", True
                failure_text = str(error).lower()
                pause_markers = (
                    "11001", "冷却", "限流", "频控", "风控", "captcha", "verification",
                    "login", "unauthorized", "target closed", "context has been closed",
                    "browser has been closed", "browser disconnected", "connection closed",
                    "websocket", "cdp", "远程调试", "浏览器已关闭", "连接中断", "net::err_",
                    "商品 id 不一致", "身份无法唯一确认", "无法按商品 id 唯一定位",
                    "无法返回选品列表", "未能通过商品 id", "拒绝点击行内加车控件",
                )
                pause_for_platform_state = (
                    page_closed or bool(unhandled_prompt) or bool(current_prompt_kind) or
                    any(marker in failure_text for marker in pause_markers)
                )
                if not pause_for_platform_state and not (
                    "timeout" in failure_text or "timed out" in failure_text or "超时" in failure_text
                ):
                    pause_for_platform_state = _selection_error_requires_pause(error)
                if (
                    not cart_attempted and not pause_for_platform_state and
                    not current_prompt_kind and not unhandled_prompt and _is_cart_control_failure_error(error)
                ):
                    failure.update(_cart_control_failure_result(
                        page,
                        candidate,
                        str(error),
                        {"button.add-cart": _safe_locator_count(page, "button.add-cart")},
                    ))
                if pause_for_platform_state:
                    failure["status"] = (
                        f"{failure['status']}；检测到账号、浏览器或页面状态异常，暂停本账号后续平台操作"
                        f"（{current_prompt_text or current_prompt_kind or '状态不确定'}）"
                    )
                    completed.append(failure)
                    break
                completed.append(failure)
                continue
            finally:
                try:
                    page.remove_listener("response", on_cart_response)
                except Exception:
                    pass
        cart_metadata: Dict[str, Any] = {"cartPath": cart_path}
        cart_status_note = (
            "；加车路径：titleDirectCart（同一商品 ID 行内加选品车控件）"
            if alliance_direct_cart and not already_in_cart else ""
        )
        if invoice_prompt_seen:
            warning = "百应提示该商品带货需要作者具备开具佣金发票资质；软件未签约。"
            cart_metadata["platformWarnings"] = [warning]
            cart_status_note += "；平台提醒：带货可能需要开具佣金发票资质"
        if platform_notices:
            cart_metadata["platformNotices"] = list(dict.fromkeys(platform_notices))
            cart_status_note += "；已安全关闭消费券推荐弹窗"
        cart_text = " ".join((card.inner_text() or "").split())

        if stop_after_cart:
            completed.append({
                "productId": str(candidate.get("productId") or ""),
                "commodityId": candidate.get("commodityId"),
                "title": title,
                "productUrl": _product_detail_url(str(candidate.get("productId") or "")),
                "cartAdded": True,
                "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                "status": "已加车；按本次验收要求停止于加车阶段" + cart_status_note,
                **cart_metadata,
            })
            continue

        cached_detail = _cached_detail_payload(context, str(candidate.get("productId") or ""))
        detail_payloads: list[Any] = [cached_detail] if cached_detail is not None else []
        detail_response_errors: list[str] = []
        detail_response_count = [0]

        def on_detail(response: Any) -> None:
            if "pack_detail" not in str(getattr(response, "url", "")):
                return
            detail_response_count[0] += 1
            try:
                detail_payloads.append(response.json())
            except Exception as error:
                detail_response_errors.append(
                    f"pack_detail 响应 JSON 解析失败（{type(error).__name__}: {_safe_diagnostic_text(error)}）"
                )

        context.on("response", on_detail)
        if not detail_payloads:
            try:
                _click_candidate_detail_target(
                    title_locator,
                    card,
                    str(candidate.get("productId") or ""),
                    str(candidate.get("title") or ""),
                )
            except Exception as error:
                try:
                    context.remove_listener("response", on_detail)
                except Exception:
                    pass
                completed.append({
                    "productId": candidate.get("productId"), "title": candidate.get("title"),
                    "productUrl": _product_detail_url(str(candidate.get("productId") or "")),
                    "cartAdded": True,
                    "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                    "status": "加车后无法打开商品详情" + cart_status_note, "error": _safe_diagnostic_text(error),
                    **cart_metadata,
                })
                continue

        prompt_kind, prompt_text = _visible_baiying_prompt(page)
        detail_verification_required = prompt_kind == "manual_verification"
        # 商品决策页首次打开会加载较多分析数据，详情接口可能晚于页面标题出现。
        deadline = time.time() + 45
        while time.time() < deadline and not detail_payloads and not detail_verification_required:
            try:
                page.wait_for_timeout(100)
                prompt_kind, prompt_text = _visible_baiying_prompt(page)
                if prompt_kind == "manual_verification":
                    detail_verification_required = True
                    break
            except Exception:
                time.sleep(0.1)
        try:
            context.remove_listener("response", on_detail)
        except Exception:
            pass
        if detail_verification_required:
            manual_result = {
                "productId": str(candidate.get("productId") or ""),
                "commodityId": candidate.get("commodityId"),
                "title": title,
                "productUrl": _product_detail_url(str(candidate.get("productId") or "")),
                "cartAdded": True,
                "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                "manualAction": "complete_baiying_verification",
                "status": (
                    "商品已确认加车，但百应商品详情页要求手动完成滑块验证。请在该账号托管 Chrome 中完成验证，"
                    "再恢复当前批次；恢复会从商品详情素材阶段继续，不会重复加车。"
                ),
            }
            if cart_metadata:
                manual_result.update(cart_metadata)
            completed.append(manual_result)
            continue
        if not detail_payloads:
            detail_error = "; ".join(detail_response_errors) or (
                "点击商品卡片后 45 秒内未捕获到 pack_detail 响应"
                if detail_response_count[0] == 0
                else "已收到 pack_detail 响应，但没有可解析的 JSON 数据"
            )
            completed.append({
                "productId": candidate.get("productId"), "commodityId": candidate.get("commodityId"),
                "productUrl": _product_detail_url(str(candidate.get("productId") or "")),
                "cartAdded": True,
                "title": candidate.get("title"), "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                "status": "已加车，未取得商品详情数据：" + detail_error + cart_status_note,
                "error": detail_error,
                **cart_metadata,
            })
            continue

        detail_id, detail_title, gallery, product_url = _product_gallery_urls(detail_payloads[-1])
        expected_product_id = str(candidate.get("productId") or "")
        if detail_id and expected_product_id and detail_id != expected_product_id:
            completed.append({
                "productId": expected_product_id,
                "actualProductId": detail_id,
                "commodityId": candidate.get("commodityId"),
                "title": title,
                "cartAdded": False,
                "cartStatus": "商品 ID 不匹配",
                "status": f"加车后详情商品 ID 与候选不一致（候选 {expected_product_id}，详情 {detail_id}）；已停止，未下载素材",
                **cart_metadata,
            })
            continue
        product_id = detail_id or str(candidate.get("productId") or "")
        product_url = product_url or _product_detail_url(product_id)
        title = detail_title or str(candidate.get("title") or "")
        if not gallery:
            gallery_error = "pack_detail 响应已解析，但未找到可用的商品图地址"
            completed.append({
                "productId": product_id, "commodityId": candidate.get("commodityId"), "title": title,
                "productUrl": product_url,
                "cartAdded": True,
                "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                "status": "已加车，百应详情响应未返回可用商品图：" + gallery_error + cart_status_note,
                "error": gallery_error,
                **cart_metadata,
            })
            continue

        output_dir = Path(download_root).expanduser() / datetime.now().strftime("%Y-%m-%d") / _safe_name(title)
        source_dir = output_dir / "商品详情图"
        try:
            source_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            directory_error = (
                f"无法创建商品详情图目录（{type(error).__name__}: "
                f"{_safe_diagnostic_text(error, 500)}）"
            )
            completed.append({
                "productId": product_id,
                "commodityId": candidate.get("commodityId"),
                "title": title,
                "productUrl": product_url,
                "cartAdded": True,
                "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
                "status": "已确认加入选品车，但素材目录创建失败；未下载详情图。" + cart_status_note,
                "outputDir": str(output_dir),
                "imagePaths": [],
                "imageErrors": [directory_error],
                **cart_metadata,
            })
            continue
        image_paths: list[str] = []
        image_errors: list[str] = []
        for index, url in enumerate(gallery):
            try:
                response = context.request.get(url, timeout=30_000)
                if not response.ok:
                    image_errors.append(f"第 {index + 1} 张图片请求失败（HTTP {response.status}）")
                    continue
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not content_type.startswith("image/"):
                    image_errors.append(
                        f"第 {index + 1} 张图片响应类型不是 image（Content-Type: {content_type or '缺失'}）"
                    )
                    continue
                body = response.body()
                if not body:
                    image_errors.append(f"第 {index + 1} 张图片响应内容为空")
                    continue
                if len(body) > 25 * 1024 * 1024:
                    image_errors.append(f"第 {index + 1} 张图片超过 25 MiB 限制（{len(body)} 字节）")
                    continue
                ext = mimetypes.guess_extension(content_type) or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                image_path = source_dir / f"{index + 1:02d}{ext}"
                image_path.write_bytes(body)
                image_paths.append(str(image_path))
            except Exception as error:
                image_errors.append(
                    f"第 {index + 1} 张图片处理异常（{type(error).__name__}: {_safe_diagnostic_text(error, 500)}）"
                )
                continue
        if image_errors and image_paths:
            image_status = f"详情图已回收，另有 {len(image_errors)} 张失败"
        elif image_paths:
            image_status = "详情图已回收"
        else:
            image_status = "已加车，但详情图下载失败"
        completed.append({
            "productId": product_id, "commodityId": candidate.get("commodityId"), "title": title,
            "productUrl": product_url,
            "cartAdded": True,
            "cartStatus": "已在选品车" if already_in_cart else "百应接口已确认加车",
            "status": image_status + cart_status_note,
            "outputDir": str(output_dir), "imagePaths": image_paths,
            "imageErrors": image_errors,
            **cart_metadata,
        })
    analysis_by_product_id = {
        str(candidate.get("productId") or ""): candidate
        for candidate in candidates
        if str(candidate.get("productId") or "")
    }
    for item in completed:
        candidate = analysis_by_product_id.get(str(item.get("productId") or ""))
        if not candidate or item.get("analysisRejected") is True:
            continue
        for key in (
            "dataMetrics", "metricSources", "analysisScore", "analysisCoverage", "analysisMetricCount",
            "analysisRawScore", "analysisMissingMetrics", "analysisReasons", "analysisStatus", "analysisError", "detailCapturedAt", "trendKnown",
            "trendStatus", "trendSlope", "trendPoints", "estimatedSalesAmount", "cartPath",
        ):
            if key in candidate:
                item[key] = candidate[key]
        score = candidate.get("analysisScore")
        if score is not None:
            reasons = [str(reason) for reason in candidate.get("analysisReasons", [])[:4]]
            item["analysisSummary"] = f"候选评分 {score}/100；" + "；".join(reasons)
            status = str(item.get("status") or "")
            if status and "候选评分" not in status:
                item["status"] = status + "；" + item["analysisSummary"]
    if analyze_before_cart and not stop_after_cart and max_selected_products is not None:
        try:
            target = max(0, int(max_selected_products))
        except (TypeError, ValueError):
            target = len(selected_candidates)
        added = sum(1 for item in completed if item.get("cartAdded") is True)
        # 首批详情核验只找够目标数量的达标商品。若后续加车被明确跳过，
        # 再核验尚未分析的备用候选；加车结果不明时保留检查点，避免超额加车。
        uncertain = any(
            item.get("manualAction")
            or (
                item.get("cartAttempted") is True
                and item.get("cartAdded") is not True
                and item.get("confirmedCartRejection") is not True
            )
            or (item.get("cartAdded") is not True and item.get("skipped") is not True)
            for item in completed
        )
        remaining_indexes = [
            int(candidate["cardIndex"]) for candidate in selected_candidates
            if candidate.get("analysisAttempted") is not True
        ]
        if added < target and remaining_indexes and not uncertain:
            completed.extend(execute_selected_candidates(
                page, context, candidates, remaining_indexes, download_root, settings,
                stop_after_cart=stop_after_cart, progress=progress,
                analyze_before_cart=True, max_selected_products=target - added,
            ))
    return completed


def _candidate_is_in_cart(candidate: Dict[str, Any], card_text: str) -> bool:
    """合并百应卡片文案和接口快照判断加车状态，恢复时避免重复加车。"""
    normalized_text = " ".join(card_text.split())
    # 当前卡片明确显示可加入时，优先采用实时 UI 状态；旧检查点可能因平台 code=0
    # 误把被资格拦截的商品标记为 inCart，不能让陈旧快照覆盖页面上的“加选品车”。
    if _candidate_has_add_cart_action_text(normalized_text):
        return False
    if re.search(r"已加入|已在|已加(?:选品)?车|已添加|去选品车", normalized_text):
        return True
    return bool(candidate.get("inCart"))


def _candidate_has_add_cart_action_text(card_text: str) -> bool:
    return bool(re.search(r"(?:^|\s)(?:加选品车|加入选品车)(?:\s|$)", " ".join(str(card_text or "").split())))


def _cart_response_outcome(payload: Any) -> tuple[bool, str]:
    """只有明确成功码且没有业务失败文案时，才将加车响应记为成功。"""
    if not isinstance(payload, dict):
        return False, "百应加车接口没有返回 JSON 对象"
    code = payload.get("code")
    message = " ".join(str(payload.get("msg") or "").split())
    if isinstance(code, bool) or code not in (0, "0"):
        return False, message or f"百应加车接口错误码 {code}"
    if re.search(r"失败|错误|未能|不满足|需先|请先|不允许|无权限|未完成|禁止|拦截", message):
        return False, message or "百应加车接口返回业务失败"
    data = payload.get("data")
    if isinstance(data, dict) and data.get("success") is False:
        return False, message or "百应加车接口 data.success=false"
    return True, message or "百应加车接口成功"


def _is_product_specific_cart_rejection(message: str) -> bool:
    """仅将明确指向当前商品的业务拒绝视为可跳过；账号/资质限制和未知错误仍停批。"""
    normalized = " ".join(str(message or "").split()).lower()
    account_scope_markers = (
        "当前账号", "本账号", "账号未", "账号无", "账号权限", "账号资质", "作者保证金",
        "创作者保证金", "学习任务", "发票资质", "账户未", "账户无", "账号登录", "账户登录",
    )
    if any(marker in normalized for marker in account_scope_markers):
        return False
    if _candidate_unavailable_reason(normalized):
        return True
    product_markers = ("商品", "产品")
    rejection_markers = (
        "不满足", "无权限", "不允许", "已下架", "已失效", "不存在", "暂不可添加",
        "不可带货", "不能添加", "无法加车", "添加失败", "加车失败", "加入失败",
        "不支持推广", "不符合推广", "未达到要求",
    )
    return any(marker in normalized for marker in product_markers) and any(
        marker in normalized for marker in rejection_markers
    )


def _classify_baiying_prompt(text: str) -> str:
    """将已识别的百应弹窗分为验证码、资质提醒、消费券推荐或普通加车错误。"""
    normalized = " ".join(str(text or "").split())
    if re.search(r"请完成下列验证|拖动完成上方拼图|按住左边按钮拖动|滑块验证|安全验证", normalized):
        return "manual_verification"
    if ("有开具发票能力的作者才可带货" in normalized or
            ("发票" in normalized and "作者" in normalized and "带货" in normalized)):
        return "invoice_eligibility"
    if "本品不可用消费券" in normalized and "带消费券商品享大促流量" in normalized:
        return "coupon_recommendation"
    if re.search(
        r"添加失败|加车失败|加入失败|新作者需先完成学习任务|暂不可添加|暂无库存|无库存|库存不足|"
        r"已售罄|售罄|缺货|已下架|暂不可带货|暂不可带|不可带货|不可加车",
        normalized,
    ):
        return "cart_error"
    return ""


def _visible_baiying_prompt(page: Page) -> tuple[str, str]:
    """读取百应当前可见弹窗；不会根据截图外的隐藏文案触发操作。"""
    script = """() => {
          const selector = [
            '.aurora-modal-wrap', '[role="dialog"]', '.semi-modal-wrap', '.semi-modal',
            '.aurora-message-error',
            '.ant-modal-wrap', '[class*="captcha"]', '[class*="Captcha"]',
            '[class*="verify"]', '[class*="Verify"]'
          ].join(',');
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const dialogs = Array.from(document.querySelectorAll(selector))
            .filter(visible)
            .map((node) => (node.innerText || '').replace(/\\s+/g, ' ').trim())
            .filter(Boolean);
          if (dialogs.length) return dialogs;
          const bodyText = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
          if (/请完成下列验证|拖动完成上方拼图|按住左边按钮拖动/.test(bodyText)) {
            return [bodyText.slice(0, 3000)];
          }
          return [];
        }"""
    evaluators: list[Any] = []
    try:
        frames = getattr(page, "frames", None)
        if isinstance(frames, list):
            main_frame = getattr(page, "main_frame", None)
            evaluators.extend(frame for frame in frames if frame is not main_frame)
    except Exception:
        pass
    evaluators.append(page)
    texts: list[str] = []
    for evaluator in evaluators:
        try:
            result = evaluator.evaluate(script)
        except Exception:
            continue
        if isinstance(result, str):
            texts.append(result)
        elif isinstance(result, (list, tuple)):
            texts.extend(str(text) for text in result)
    prompts = [(kind, str(text).strip()) for text in texts
               if (kind := _classify_baiying_prompt(str(text)))]
    for priority in ("manual_verification", "invoice_eligibility", "coupon_recommendation", "cart_error"):
        match = next((prompt for prompt in prompts if prompt[0] == priority), None)
        if match:
            return match
    return "", ""


def _dismiss_baiying_coupon_recommendation(page: Page) -> bool:
    """只关闭准确识别的消费券推荐弹窗；不点击弹窗内商品或推荐按钮。"""
    selector = (
        '.aurora-modal-wrap:visible, [role="dialog"]:visible, .semi-modal-wrap:visible, '
        '.semi-modal:visible, .ant-modal-wrap:visible'
    )
    try:
        dialogs = page.locator(selector)
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            if _classify_baiying_prompt(dialog.inner_text() or "") != "coupon_recommendation":
                continue
            close = dialog.locator(
                'button[aria-label="关闭"], button[aria-label="close"], '
                '[role="button"][aria-label="关闭"], [role="button"][aria-label="close"], '
                '[data-testid*="close"], .aurora-modal-close, .semi-modal-close, .ant-modal-close'
            )
            if close.count() == 1 and close.first.is_visible():
                close = close.first
                close.click(timeout=1_500, no_wait_after=True)
            else:
                # 仅在当前最上层弹窗已被准确分类为消费券推荐后才使用 Escape。
                page.keyboard.press("Escape")
            page.wait_for_timeout(100)
            return _visible_baiying_prompt(page)[0] != "coupon_recommendation"
    except Exception:
        return False
    return False


def _click_invoice_prompt_add_cart(page: Page) -> bool:
    """发票资质提醒中只点击“加选品车”，绝不代用户签约。"""
    selector = (
        '.aurora-modal-wrap:visible, [role="dialog"]:visible, .semi-modal-wrap:visible, '
        '.semi-modal:visible, .ant-modal-wrap:visible'
    )
    try:
        dialogs = page.locator(selector)
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            if _classify_baiying_prompt(dialog.inner_text() or "") != "invoice_eligibility":
                continue
            buttons = dialog.get_by_role("button", name="加选品车", exact=True)
            if buttons.count() != 1:
                return False
            button = buttons.first
            if not button.is_visible():
                return False
            button.click(timeout=3_000, no_wait_after=True)
            return True
    except Exception:
        return False
    return False


def _manual_verification_result(candidate: Dict[str, Any], cart_attempted: bool) -> Dict[str, Any]:
    """创建可恢复的滑块验证检查点；恢复时必须重新按商品 ID 核验选品车。"""
    product_id = str(candidate.get("productId") or "")
    return {
        "productId": product_id,
        "title": str(candidate.get("title") or ""),
        "cartAdded": False,
        "cartAttempted": cart_attempted,
        "cartStatus": "等待手动验证",
        "manualAction": "complete_baiying_verification",
        "status": (
            "百应要求手动完成滑块验证。请在该账号对应的托管 Chrome 中手动完成验证，"
            "然后再次点击自动选品恢复；恢复时会按原商品 ID 复核选品车状态，"
            "确认未加车后才会再次尝试，不会用其他商品替代。"
        ),
    }


def _visible_cart_error_message(page: Page) -> str:
    """读取当前可见的百应加车失败弹窗，防止接口成功码掩盖资格拦截。"""
    try:
        message = page.evaluate("""() => {
          const failure = /添加失败|加车失败|加入失败|新作者需先完成学习任务|暂不可添加|暂无库存|无库存|库存不足|已售罄|售罄|缺货|已下架|暂不可带货|暂不可带|不可带货|不可加车/;
          const visible = (node) => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          return Array.from(document.querySelectorAll('.aurora-modal-wrap, [role="dialog"], .aurora-message-error'))
            .filter(visible)
            .map((node) => (node.innerText || '').replace(/\\s+/g, ' ').trim())
            .find((text) => failure.test(text)) || '';
        }""")
        return str(message or "").strip()
    except Exception:
        return ""


def _dismiss_cart_error_dialog(page: Page) -> None:
    """记录失败原因后关闭资格拦截弹窗，避免后台标签被旧弹窗永久挡住。"""
    labels = ("取 消", "取消", "知道了", "确 定", "确定", "关闭")
    try:
        dialogs = page.locator('.aurora-modal-wrap:visible, [role="dialog"]:visible')
        for index in range(dialogs.count() - 1, -1, -1):
            dialog = dialogs.nth(index)
            text = " ".join((dialog.inner_text() or "").split())
            if not re.search(
                r"添加失败|加车失败|加入失败|新作者需先完成学习任务|暂不可添加|暂无库存|无库存|库存不足|"
                r"已售罄|售罄|缺货|已下架|暂不可带货|暂不可带|不可带货|不可加车",
                text,
            ):
                continue
            for label in labels:
                button = dialog.get_by_text(label, exact=True).last
                if button.count() and button.is_visible():
                    button.click(force=True, timeout=1_500, no_wait_after=True)
                    page.wait_for_timeout(100)
                    return
            page.keyboard.press("Escape")
            return
    except Exception:
        return


def _candidate_is_current_for_source(page: Page, candidate: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    """候选 ID 已在当前同签名结果卡片中时，无需重复应用筛选或请求列表。"""
    try:
        expected_signature = _selection_filter_signature(settings)
        applied_signature = page.evaluate("() => window.__touguangAppliedFilterSettings || ''")
        if applied_signature != expected_signature:
            return False
        _locate_candidate_card(page, candidate)
        return True
    except Exception:
        return False


def _candidate_card_product_ids(card_locator: Any) -> list[str]:
    """仅从商品 ID 专属属性或 React 字段读取卡片身份；不把推广 ID/任意文本当商品 ID。"""
    values = card_locator.evaluate(r"""(root) => {
      const ids = new Set();
      const seen = new Set();
      const stack = [];
      const domNodes = [root, ...Array.from(root.querySelectorAll('*')).slice(0, 120)];
      const canonical = (key) => String(key || '').toLowerCase().replace(/^data-/, '').replace(/[-_]/g, '');
      const isProductIdKey = (key) => ['productid', 'goodsid', 'itemid'].includes(canonical(key));
      for (const node of domNodes) {
        for (const attribute of Array.from(node.attributes || [])) {
          if (isProductIdKey(attribute.name) && String(attribute.value || '').trim()) ids.add(String(attribute.value).trim());
        }
        for (const key of Object.keys(node)) {
          if (key.startsWith('__reactProps$') && node[key] && typeof node[key] === 'object') stack.push(node[key]);
        }
      }
      let visited = 0;
      while (stack.length && visited < 500) {
        const value = stack.pop();
        if (!value || typeof value !== 'object' || seen.has(value)) continue;
        seen.add(value); visited += 1;
        if (Array.isArray(value)) {
          for (const child of value) if (child && typeof child === 'object') stack.push(child);
          continue;
        }
        for (const [key, child] of Object.entries(value)) {
          if (isProductIdKey(key) && (typeof child === 'string' || typeof child === 'number') && String(child).trim()) {
            ids.add(String(child).trim());
          }
          if (child && typeof child === 'object') stack.push(child);
        }
      }
      return Array.from(ids);
    }""")
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _locate_candidate_card(page: Page, candidate: Dict[str, Any]) -> tuple[Any, Any]:
    """先按精确商品 ID 定位来源卡片/表格行；仅 DOM 未暴露 ID 时才接受唯一完整标题回退。"""
    title = str(candidate.get("title") or "").strip()
    product_id = str(candidate.get("productId") or "").strip()
    source_key = str(candidate.get("sourceKey") or "")
    if not title:
        raise RuntimeError("候选商品缺少标题")
    if not product_id:
        raise RuntimeError("候选商品缺少商品 ID，不能安全定位卡片")

    row_selector = (
        BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR
        if source_key == "allianceRank"
        else BAIYING_PRIORITY_ROOT_SELECTOR
    )
    card_locator = page.locator(row_selector)
    id_matches: list[Any] = []
    for index in range(card_locator.count()):
        card = card_locator.nth(index)
        try:
            if not card.is_visible():
                continue
            exposed_ids = _candidate_card_product_ids(card)
        except Exception:
            continue
        if product_id and product_id in exposed_ids:
            id_matches.append(card)
    if len(id_matches) == 1:
        card = id_matches[0]
        skip_reason = _candidate_card_skip_reason(product_id, [card])
        if skip_reason:
            raise CandidateCartSkip(product_id, skip_reason)
        title_locator = card.get_by_text(title, exact=True)
        if title_locator.count() == 1:
            return title_locator.first, card
        return card, card
    if len(id_matches) > 1:
        skip_reason = _candidate_card_skip_reason(product_id, id_matches)
        if skip_reason:
            raise CandidateCartSkip(product_id, skip_reason)
        raise RuntimeError(f"百应列表中商品 ID {product_id} 命中 {len(id_matches)} 个卡片，已停止操作")

    matches: list[tuple[Any, Any]] = []
    title_matches = page.get_by_text(title, exact=True)
    for index in range(title_matches.count()):
        title_locator = title_matches.nth(index)
        card = title_locator.locator(
            "xpath=ancestor-or-self::*[contains(@class, '__card___') or "
            "contains(@class, '__row___') or self::tr or @role='row' or "
            "contains(concat(' ', normalize-space(@class), ' '), ' kol-product-card ') or "
            "contains(@class, 'productRow') or contains(@class, 'goodsList')][1]"
        ).first
        if not card.count():
            continue
        try:
            if not card.is_visible():
                continue
        except Exception:
            continue
        matches.append((title_locator, card))

    if len(matches) == 1:
        match_card = matches[0][1]
        exposed_ids = _candidate_card_product_ids(match_card)
        skip_reason = _candidate_card_skip_reason(product_id, [match_card], title_matched=True)
        if skip_reason:
            raise CandidateCartSkip(product_id, skip_reason)
        if exposed_ids and product_id not in exposed_ids:
            raise CandidateCartSkip(
                product_id,
                f"标题虽唯一，但可见行商品 ID 为 {','.join(exposed_ids[:3])}，与目标商品 ID 不符",
            )
        return matches[0]
    if len(matches) < 1:
        raise RuntimeError("当前列表找不到候选商品卡片")

    identified: list[tuple[Any, Any]] = []
    for pair in matches:
        try:
            contains_id = product_id in _candidate_card_product_ids(pair[1])
        except Exception:
            contains_id = False
        if contains_id:
            identified.append(pair)
    if len(identified) == 1:
        return identified[0]
    skip_reason = _candidate_card_skip_reason(
        product_id, [pair[1] for pair in matches], title_matched=True
    )
    if skip_reason:
        raise CandidateCartSkip(product_id, skip_reason)
    row_id_samples = []
    for _, card in matches[:5]:
        try:
            row_id_samples.extend(_candidate_card_product_ids(card))
        except Exception:
            continue
    evidence = f"；可见行商品 ID 样本={list(dict.fromkeys(row_id_samples))[:5]}" if row_id_samples else ""
    raise RuntimeError(
        f"百应列表存在同名商品，无法按精确商品 ID {product_id} 唯一定位，已停止操作{evidence}"
    )


def _wait_for_priority_source_candidates_ready(
    page: Page,
    source_key: str,
    candidates: Sequence[Dict[str, Any]],
) -> None:
    """加车复核前等待同来源已选商品 ID 出现在可见行中，或等到明确空状态。"""
    source = BAIYING_PRIORITY_SOURCES.get(source_key)
    if source is None:
        raise RuntimeError("未知的百应优先来源，无法等待商品列表就绪")
    label, _ = source
    product_ids = list(dict.fromkeys(
        str(row.get("productId") or "").strip()
        for row in candidates
        if isinstance(row, dict) and str(row.get("productId") or "").strip()
    ))
    if not product_ids:
        raise RuntimeError(f"百应{label}没有可复核的商品 ID；已停止加车。")

    selector = (
        BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR
        if source_key == "allianceRank"
        else BAIYING_PRIORITY_ROOT_SELECTOR
    )
    try:
        state_handle = page.wait_for_function(
            r"""({ selector, targetProductIds, loadingSelector, emptyPattern, sourceKey }) => {
              const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isNavigationElement = (element) => Boolean(element.closest(
                'nav, [role="navigation"], [role="menu"], .auxo-menu, [class*="headerNav"], [class*="menu-bar"]'
              ));
              const isExcludedAllianceRow = (element) => {
                if (isNavigationElement(element)) return true;
                if (element.matches('thead, th, [role="columnheader"]') ||
                    (element.matches('tr, [role="row"]') && element.querySelector('th, [role="columnheader"]'))) return true;
                for (let node = element, level = 0; node && level < 5; node = node.parentElement, level += 1) {
                  const className = String(node.className || '').toLowerCase();
                  if (/productrow|measure|placeholder|skeleton|tablehead|thead|tableheader|headerrow|columnheader/.test(className) ||
                      (level === 0 && className.includes('header')) ||
                      (level === 0 && className.includes('goodslistwrap'))) return true;
                  if (node.matches('thead, th, [role="columnheader"]')) return true;
                }
                return false;
              };
              const targetIds = new Set(targetProductIds.map((value) => String(value).trim()));
              const readUniqueProductIds = (root) => {
                for (let element = root, level = 0; element && level < 6; element = element.parentElement, level += 1) {
                  const ids = new Set();
                  const scan = (value, seen, depth, budget) => {
                    if (!value || typeof value !== 'object' || depth > 8 || budget.count > 2500 || seen.has(value)) return;
                    seen.add(value);
                    budget.count += 1;
                    if (Array.isArray(value)) {
                      for (const item of value) scan(item, seen, depth + 1, budget);
                      return;
                    }
                    const productId = value.product_id ?? value.productId;
                    const hasProductShape = value.product_info || value.productInfo || value.base_model || value.baseModel ||
                      value.promotion_info || value.promotionInfo || value.product_name || value.title || value.name;
                    if (productId != null && hasProductShape && /^\d{5,}$/.test(String(productId).trim())) {
                      ids.add(String(productId).trim());
                    }
                    for (const [key, child] of Object.entries(value)) {
                      if (key === '__proto__' || key === 'ownerDocument' || key === 'stateNode') continue;
                      scan(child, seen, depth + 1, budget);
                    }
                  };
                  for (const key of Object.keys(element)) {
                    if (key.startsWith('__reactProps$')) scan(element[key], new WeakSet(), 0, { count: 0 });
                  }
                  if (ids.size) return ids;
                }
                return new Set();
              };
              const counts = new Map(Array.from(targetIds, (id) => [id, 0]));
              const roots = Array.from(document.querySelectorAll(selector)).filter((root) =>
                visible(root) && !isNavigationElement(root) &&
                (sourceKey !== 'allianceRank' || !isExcludedAllianceRow(root))
              );
              for (const root of roots) {
                for (const id of readUniqueProductIds(root)) {
                  if (counts.has(id)) counts.set(id, counts.get(id) + 1);
                }
              }
              const loadingVisible = Array.from(document.querySelectorAll(loadingSelector)).some(visible);
              const loadingText = /正在加载|加载中|loading/i.test(document.body?.innerText || '');
              if (loadingVisible || loadingText) return false;
              if (Array.from(counts.values()).some((count) => count > 1)) return 'duplicate';
              if (Array.from(counts.values()).every((count) => count === 1)) return 'ready';
              const emptyPatternMatcher = new RegExp(emptyPattern);
              const bodyHasEmptyText = emptyPatternMatcher.test(document.body?.innerText || '');
              const hasExplicitEmptyState = bodyHasEmptyText && Array.from(document.querySelectorAll('body *')).some((element) => {
                if (!visible(element)) return false;
                const text = (element.innerText || '').replace(/\s+/g, ' ').trim();
                if (!text || text.length > 80 || !emptyPatternMatcher.test(text)) return false;
                return Array.from(element.children).every((child) =>
                  !emptyPatternMatcher.test((child.innerText || '').replace(/\s+/g, ' ').trim())
                );
              });
              return hasExplicitEmptyState ? 'empty' : false;
            }""",
            arg={
                "selector": selector,
                "targetProductIds": product_ids,
                "loadingSelector": BAIYING_PRIORITY_ACTIVE_LOADING_SELECTOR,
                "emptyPattern": BAIYING_PRIORITY_EMPTY_PATTERN,
                "sourceKey": source_key,
            },
            timeout=BAIYING_PRIORITY_READY_TIMEOUT_MS,
            polling=500,
        )
        state = state_handle.json_value()
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"百应{label}在 {BAIYING_PRIORITY_READY_TIMEOUT_MS // 1000} 秒内未显示所选商品的唯一商品 ID，"
            "也未出现明确空状态；已停止加车。"
        ) from error

    if state == "duplicate":
        raise RuntimeError(f"百应{label}可见行中有商品 ID 重复，无法安全复核；已停止加车。")
    if state == "empty":
        raise RuntimeError(f"百应{label}页面显示列表为空，所选商品无法复核；已停止加车。")
    if state != "ready":
        raise RuntimeError(f"百应{label}列表就绪状态无法确认；已停止加车。")


def _candidate_card_is_available(page: Page, candidate: Dict[str, Any]) -> bool:
    deadline = time.time() + 5
    while True:
        try:
            _locate_candidate_card(page, candidate)
            return True
        except RuntimeError as error:
            if "当前列表找不到候选商品卡片" not in str(error) or time.time() >= deadline:
                return False
            # material_list 响应可能先于 React 卡片渲染；短暂等待 DOM 更新，
            # 避免把正常渲染延迟误判成候选失效并再次请求平台。
            page.wait_for_timeout(200)


def _scroll_priority_source_once(page: Page, source_key: str, reset: bool = False) -> Dict[str, Any]:
    """向来源页的真实滚动容器移动一次；虚拟列表不会同时渲染整个候选池。"""
    row_selector = (
        BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR
        if source_key == "allianceRank" else BAIYING_PRIORITY_ROOT_SELECTOR
    )
    container_selector = (
        BAIYING_PRIORITY_SOURCE_LIST_SELECTORS.get(source_key)
        if source_key == "hotSpot" else
        '[class*="infinite-scroller"], [class*="virtual-list"], [class*="virtualList"], '
        '[class*="scroll-container"], [class*="table-body"], [role="grid"], tbody'
    )
    try:
        value = page.evaluate(
            r"""({ containerSelector, rowSelector, reset }) => {
              const visible = (element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' && style.visibility !== 'hidden';
              };
              const canScroll = (element) => element && visible(element) &&
                element.scrollHeight > element.clientHeight + 20;
              let container = Array.from(document.querySelectorAll(containerSelector))
                .find(canScroll);
              if (!container) {
                const rows = Array.from(document.querySelectorAll(rowSelector)).filter(visible);
                for (const row of rows) {
                  for (let node = row.parentElement, level = 0; node && level < 8; node = node.parentElement, level += 1) {
                    if (canScroll(node)) { container = node; break; }
                  }
                  if (container) break;
                }
              }
              if (!container) return { moved: false, atEnd: true, found: false };
              const before = Number(container.scrollTop || 0);
              const maxTop = Math.max(0, Number(container.scrollHeight || 0) - Number(container.clientHeight || 0));
              if (reset) container.scrollTop = 0;
              else {
                const step = Math.max(240, Math.floor(Number(container.clientHeight || 480) * 0.85));
                container.scrollTop = Math.min(maxTop, before + step);
              }
              try { container.dispatchEvent(new Event('scroll', { bubbles: true })); } catch (_) {}
              const after = Number(container.scrollTop || 0);
              return { moved: after !== before, atEnd: after >= maxTop - 2, found: true, top: after, maxTop };
            }""",
            {"containerSelector": container_selector, "rowSelector": row_selector, "reset": reset},
        )
        return value if isinstance(value, dict) else {"moved": False, "atEnd": True, "found": False}
    except Exception:
        return {"moved": False, "atEnd": True, "found": False}


def _visible_source_candidate_skip(
    candidate: Dict[str, Any], label: str, reason: str
) -> Dict[str, Any]:
    product_id = str(candidate.get("productId") or "")
    title = str(candidate.get("title") or product_id)
    return {
        "productId": product_id,
        "commodityId": candidate.get("commodityId"),
        "title": title,
        "cartAdded": False,
        "cartAttempted": False,
        "skipped": True,
        "cartStatus": "来源页未渲染，未点击加车",
        "status": f"百应{label}当前滚动区未渲染商品 ID {product_id}；{reason}，未点击加车，已跳过并继续后续候选",
    }


def _ensure_priority_source_candidate_visible(
    page: Page,
    source_key: str,
    candidate: Dict[str, Any],
    timeout_ms: int = 12_000,
) -> bool:
    """把一个候选滚动到当前 DOM，再交给精确商品 ID 定位器。"""
    product_id = str(candidate.get("productId") or "").strip()
    if not product_id:
        return False
    row_selector = (
        BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR
        if source_key == "allianceRank" else BAIYING_PRIORITY_ROOT_SELECTOR
    )

    def visible_match() -> bool:
        try:
            rows = page.locator(row_selector)
            for index in range(min(rows.count(), 250)):
                card = rows.nth(index)
                try:
                    if not card.is_visible() or product_id not in _candidate_card_product_ids(card):
                        continue
                    _locate_candidate_card(page, candidate)
                    return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    if visible_match():
        return True
    deadline = time.monotonic() + max(1_000, timeout_ms) / 1000
    reset_done = False
    stalled = 0
    for _ in range(100):
        if time.monotonic() >= deadline:
            break
        state = _scroll_priority_source_once(page, source_key, reset=not reset_done)
        reset_done = True
        page.wait_for_timeout(250)
        if visible_match():
            return True
        if not state.get("moved"):
            stalled += 1
            if stalled >= 3:
                break
        else:
            stalled = 0
    return False


def _exclude_unrendered_selected_candidates(
    page: Page,
    candidates: Sequence[Dict[str, Any]],
    selected_ids: Sequence[str],
) -> tuple[list[Dict[str, Any]], list[str]]:
    wanted = {str(value) for value in selected_ids if str(value)}
    unavailable: list[str] = []
    retained: list[Dict[str, Any]] = []
    for candidate in candidates:
        product_id = str(candidate.get("productId", ""))
        if product_id in wanted and not _candidate_card_is_available(page, candidate):
            unavailable.append(product_id)
            continue
        retained.append(candidate)
    return retained, unavailable


def selected_product_ids(candidates: Sequence[Dict[str, Any]], selected_indexes: Sequence[int]) -> list[str]:
    selected = set(int(index) for index in selected_indexes)
    return list(dict.fromkeys(
        str(row.get("productId", "")) for row in candidates
        if isinstance(row, dict) and row.get("cardIndex") in selected and str(row.get("productId", ""))
    ))


def remap_selected_candidate_indexes(
    candidates: Sequence[Dict[str, Any]], selected_ids: Sequence[str]
) -> tuple[list[int], list[str]]:
    wanted = list(dict.fromkeys(str(value) for value in selected_ids if str(value)))
    by_id: Dict[str, list[int]] = {}
    for row in candidates:
        product_id = str(row.get("productId", "")) if isinstance(row, dict) else ""
        card_index = row.get("cardIndex") if isinstance(row, dict) else None
        if product_id and isinstance(card_index, int):
            by_id.setdefault(product_id, []).append(card_index)
    # 只有一个 ID 对应唯一当前卡片时才允许恢复；重复行不能静默覆盖成最后一条。
    unique = {
        product_id: indexes[0]
        for product_id, indexes in by_id.items()
        if len(indexes) == 1
    }
    return [unique[value] for value in wanted if value in unique], [value for value in wanted if value not in unique]


def _search_pool_product_on_account(page: Page, candidate: Dict[str, Any], settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在消费账号的选品广场按商品 ID 搜索，只接受平台响应与可见卡片双重确认。"""
    product_id = str(candidate.get("productId") or "").strip()
    if not product_id.isdigit():
        raise RuntimeError("候选池商品 ID 无效；未执行加车")
    field = page.locator('.searchInput__input input[role="combobox"]')
    field.first.wait_for(state="visible", timeout=15_000)
    if field.count() != 1:
        raise RuntimeError("当前账号百应搜索框不唯一；未执行加车")
    buttons = page.get_by_text("搜 索", exact=True)
    if buttons.count() != 1:
        buttons = page.get_by_text("搜索", exact=True)
    if buttons.count() != 1:
        raise RuntimeError("当前账号百应搜索按钮不唯一；未执行加车")
    def is_network_11001_message(message: Any) -> bool:
        text = str(message or "")
        return bool(re.search(r"网络(?:不稳定|错误|异常|故障|超时)|网络连接(?:失败|异常)", text)) and not bool(
            re.search(r"限流|频控|选品(?:太多|过多)|(?:请求|访问|操作)(?:过多|太多|频繁|太频繁|过于频繁)", text)
        )

    terms = (product_id, f"https://haohuo.jinritemai.com/views/product/item2?id={product_id}")
    for term in terms:
        def query_term_once() -> list[Dict[str, Any]]:
            observations: list[Dict[str, Any]] = []
            last_response = [0.0]

            def on_response(response: Any) -> None:
                parsed = urlparse(str(getattr(response, "url", "")))
                if parsed.hostname != "buyin.jinritemai.com" or "material_list" not in parsed.path:
                    return
                try:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        return
                    if str(payload.get("code")) == "11001":
                        raw_message = payload.get("msg") or payload.get("message")
                        message = raw_message.strip() if isinstance(raw_message, str) else ""
                        # 无法在截断文本中排除后半段频控提示时，按原因未确认处理。
                        safe_message = message if len(message) <= 160 else "平台提示过长，未完整记录"
                        observations.append({
                            "businessCode": "11001", "message": safe_message,
                            "diagnostic": _material_list_response_diagnostics(
                                response, {**payload, "msg": safe_message or "未提供"}
                            ),
                        })
                    elif response.status != 200 or payload.get("code") not in (None, 0, "0"):
                        observations.append({"failed": True})
                    else:
                        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                        rows = data.get("summary_promotions")
                        if isinstance(rows, list):
                            observations.append({"rows": [row for row in rows if isinstance(row, dict) and
                                str(row.get("product_id") or "").strip() == product_id]})
                        else:
                            observations.append({"formatUnknown": True})
                    last_response[0] = time.monotonic()
                except Exception:
                    observations.append({"failed": True})
                    last_response[0] = time.monotonic()

            page.on("response", on_response)
            try:
                field.fill(term, timeout=5_000)
                if field.input_value(timeout=2_000).strip() != term:
                    raise RuntimeError("百应搜索框没有保留当前商品 ID；未执行加车")
                buttons.click(timeout=8_000, no_wait_after=True)
                started = time.monotonic()
                while time.monotonic() - started < 15:
                    page.wait_for_timeout(250)
                    if observations and time.monotonic() - started >= 2 and time.monotonic() - last_response[0] >= 1:
                        break
            finally:
                page.remove_listener("response", on_response)
            return observations

        observations = query_term_once()
        response_11001 = [item for item in observations if item.get("businessCode") == "11001"]
        network_11001 = bool(response_11001) and all(
            is_network_11001_message(item.get("message")) for item in response_11001
        )
        first_11001_diagnostic = response_11001[-1]["diagnostic"] if network_11001 else ""
        if network_11001:
            # 仅在平台明确提示网络错误时，对同一商品 ID 做一次短等待重查；仍未确认就保留检查点。
            page.wait_for_timeout(5_000)
            prompt_kind, prompt_text = _visible_baiying_prompt(page)
            if prompt_kind:
                raise RuntimeError(f"当前账号百应状态待处理（{prompt_text or prompt_kind}）；未重查商品 ID")
            observations = query_term_once()
        response_11001 = [item for item in observations if item.get("businessCode") == "11001"]
        if response_11001:
            blocking_response = next(
                (item for item in response_11001 if not is_network_11001_message(item.get("message"))),
                response_11001[-1],
            )
            raise RuntimeError(
                f"百应商品搜索返回 11001；{blocking_response['diagnostic']}；已停止当前账号请求"
            )
        if network_11001 and not any("rows" in item for item in observations):
            raise RuntimeError(
                f"百应商品搜索返回 11001；{first_11001_diagnostic}；"
                "短间隔重查结果未确认；已停止当前账号请求"
            )
        if not observations or all(item.get("failed") for item in observations):
            return None
        if any(item.get("formatUnknown") for item in observations):
            return None
        matches = [row for item in observations for row in item.get("rows", [])]
        promotion_ids = {str(row.get("promotion_id") or row.get("commodity_id") or "").strip() for row in matches}
        promotion_ids.discard("")
        if len(promotion_ids) > 1:
            return None
        if not matches or not promotion_ids:
            continue
        raw = matches[-1]
        broad = {**settings, "thresholds": {}, "selectionMode": "general", "filters": {}}
        parsed, _, _ = _candidate_rows({"data": {"summary_promotions": [raw]}}, broad, 1)
        if not parsed:
            return None
        live = parsed[0]
        live["cardIndex"] = candidate["cardIndex"]
        live["productId"] = product_id
        live["commodityId"] = next(iter(promotion_ids))
        live["sourceKey"] = "selectionSquare"
        live["sources"] = list(candidate.get("sources") or [])
        live["sourceCategorySelection"] = {}
        live_category = _promotion_row_category_ids(raw)
        live["categoryIds"] = live_category if len(live_category) == 3 else []
        live["leafCategoryId"] = live_category[-1] if len(live_category) == 3 else ""
        live["categoryStatus"] = "known" if len(live_category) == 3 else "unknown"
        try:
            _locate_candidate_card(page, live)
        except (CandidateCartSkip, RuntimeError):
            return None
        return live
    return None


def _execute_pool_consumer_candidates(
    context: BrowserContext,
    candidates: list[Dict[str, Any]],
    selected_indexes: list[int],
    download_root: str,
    settings: Dict[str, Any],
    max_selected_products: Optional[int],
    progress: Optional[Callable[[Dict[str, Any]], None]],
) -> tuple[list[Dict[str, Any]], list[str], str]:
    selected = [row for row in candidates if row.get("cardIndex") in set(selected_indexes)]
    if len(selected) != len(set(selected_indexes)):
        raise RuntimeError("候选池消费快照不完整；未执行加车")
    target = max(0, int(max_selected_products)) if max_selected_products is not None else len(selected)
    completed: list[Dict[str, Any]] = []
    untouched_product_ids: list[str] = []
    retryable_11001_reason = ""
    page = _create_selection_work_page(context)
    try:
        url = BAIYING_SELECTION_SQUARE_SOURCE[1]
        page.goto(url, wait_until="commit", timeout=45_000)
        actual = urlparse(page.url)
        expected = urlparse(url)
        if actual.hostname != expected.hostname or actual.path.rstrip("/") != expected.path.rstrip("/"):
            raise RuntimeError("当前账号百应选品广场未正常打开；请检查登录和选品权限")
        search_settings = copy.deepcopy(settings)
        # 池内商品已由采集账号读取；消费账号只需在未收窄的页面上按精确 ID
        # 找到可见商品，再用自身详情和实际指标逐项核验。沿用采集时的页面筛选
        # 会隐藏池内商品，并可能在搜索前被无关控件阻断。
        search_settings["filters"] = {}
        search_settings["filterCatalog"] = {}
        search_settings["thresholds"] = {}
        search_settings["categorySelection"] = {"level1": [], "level2": [], "level3": []}
        search_settings["searchKeyword"] = ""
        filter_warnings = apply_filter_settings(page, search_settings)
        if filter_warnings:
            raise RuntimeError("当前账号百应搜索页未能清理旧筛选；保留检查点：" + "；".join(filter_warnings[:3]))
        for position, candidate in enumerate(selected, start=1):
            if sum(row.get("cartAdded") is True for row in completed) >= target:
                untouched_product_ids.extend(str(row.get("productId") or "") for row in selected[position - 1:])
                break
            product_id = str(candidate.get("productId") or "")
            _report_operation_progress(progress, "候选池按商品 ID 核验", index=position, total=len(selected),
                                       message=f"当前账号按商品 ID 查找 {position}/{len(selected)}", product_id=product_id)
            prompt_kind, prompt_text = _visible_baiying_prompt(page)
            if prompt_kind in {"manual_verification", "login", "account_unavailable", "invoice_eligibility"}:
                raise RuntimeError(f"当前账号百应状态待处理（{prompt_text or prompt_kind}）；保留检查点")
            try:
                live = _search_pool_product_on_account(page, candidate, settings)
            except RuntimeError as error:
                if not str(error).startswith("百应商品搜索返回 11001"):
                    raise
                # 搜索响应发生在本商品加车之前；本商品和后续商品都未触发加车。
                # 返回已处理商品的检查点，避免上层把整个预占批次误记为结果不明。
                retryable_11001_reason = str(error)
                completed.append({
                    **candidate,
                    "cartAdded": candidate.get("cartAdded") is True,
                    "cartAttempted": candidate.get("cartAttempted") is True,
                    "skipped": True,
                    # 旧字段名由主进程用来核对加车前检查点；11001 不再等同于已证实限流。
                    "retryableRateLimit": True,
                    "status": retryable_11001_reason,
                })
                untouched_product_ids.extend(str(row.get("productId") or "") for row in selected[position - 1:])
                break
            if live is None:
                completed.append(_visible_source_candidate_skip(candidate, "选品广场 ID 搜索", "当前账号未能唯一确认该商品 ID 与可见卡片"))
                continue
            for key in ("sourceCategoryIds", "sourceCategoryId", "sourceCategoryName", "sourceCategoryLevel", "categorySource"):
                if candidate.get(key):
                    live[key] = copy.deepcopy(candidate[key])
            if live.get("categoryStatus") != "known" and live.get("sourceCategoryIds"):
                live["categoryStatus"] = "source_level"
            if candidate.get("cartAttempted") is True:
                live["cartAttempted"] = True
            if candidate.get("cartAdded") is True:
                live["cartAdded"] = True
            try:
                item_results = execute_selected_candidates(
                    page, context, [live], [int(live["cardIndex"])], download_root,
                    settings, progress=progress, analyze_before_cart=live.get("cartAdded") is not True,
                    max_selected_products=1,
                )
                completed.extend(item_results)
                if any(item.get("cartAttempted") is True and item.get("cartAdded") is not True and
                       item.get("skipped") is not True for item in item_results):
                    break
            except Exception as error:
                if _selection_error_requires_pause(error):
                    raise
                completed.append(_visible_source_candidate_skip(candidate, "选品广场 ID 搜索", f"当前商品核验异常（{_safe_diagnostic_text(error, 240)}）"))
    finally:
        try:
            page.close()
        except Exception:
            pass
    return completed, [product_id for product_id in untouched_product_ids if product_id], retryable_11001_reason


def _execute_visible_source_candidates(
    context: BrowserContext,
    candidates: list[Dict[str, Any]],
    selected_indexes: list[int],
    download_root: str,
    settings: Dict[str, Any],
    max_selected_products: Optional[int],
    progress: Optional[Callable[[Dict[str, Any]], None]],
) -> list[Dict[str, Any]]:
    """逐个来源回到其当前可见商品行，按精确商品 ID 详情核验并加车。"""
    selected = [row for row in candidates if row.get("cardIndex") in set(selected_indexes)]
    if len(selected) != len(set(selected_indexes)):
        raise RuntimeError("当前可见来源候选快照不完整；已停止加车。")
    allowed_sources = _enabled_priority_source_order(settings) + ["selectionSquare"]
    selected_by_source: Dict[str, list[Dict[str, Any]]] = {}
    for candidate in selected:
        source_key = str(candidate.get("sourceKey") or "")
        if source_key not in allowed_sources:
            raise RuntimeError(f"候选商品 {candidate.get('productId')} 缺少当前启用来源标识；已停止加车。")
        if not _candidate_source_category_satisfies_general_exclusions(candidate, settings):
            raise RuntimeError(f"候选商品 {candidate.get('productId')} 缺少已核实的来源分类路径；已停止加车。")
        selected_by_source.setdefault(source_key, []).append(candidate)

    try:
        target = max(1, int(max_selected_products)) if max_selected_products is not None else len(selected)
    except (TypeError, ValueError):
        target = len(selected)
    completed: list[Dict[str, Any]] = []
    for source_key in allowed_sources:
        group = selected_by_source.get(source_key, [])
        if not group:
            continue
        label, url = BAIYING_SELECTION_SQUARE_SOURCE if source_key == "selectionSquare" else BAIYING_PRIORITY_SOURCES[source_key]
        source_page = _create_selection_work_page(context)
        try:
            _report_operation_progress(
                progress, "回到百应来源商品行", index=len(completed), total=len(selected),
                message=f"打开{label}当前页，逐件按商品 ID 复核；不翻页、不搜索",
            )
            source_page.goto(url, wait_until="commit", timeout=45_000)
            parsed = urlparse(source_page.url)
            if parsed.hostname != "buyin.jinritemai.com" or parsed.path.rstrip("/") != urlparse(url).path.rstrip("/"):
                raise RuntimeError(f"百应{label}来源页身份不匹配；未操作候选商品。")
            prompt_kind, prompt_text = _visible_baiying_prompt(source_page)
            if prompt_kind:
                raise RuntimeError(f"百应{label}来源页存在待处理状态（{prompt_text or prompt_kind}）；已停止加车。")
            if source_key == "selectionSquare":
                query_settings = copy.deepcopy(settings)
                source_selection = next((row.get("sourceCategorySelection") for row in group
                                         if isinstance(row.get("sourceCategorySelection"), dict)
                                         and any(row["sourceCategorySelection"].get(level) for level in ("level1", "level2", "level3"))), None)
                if isinstance(source_selection, dict):
                    query_settings["categorySelection"] = source_selection
                query_settings["_platformCommissionRateBounds"] = list(_commission_rate_bounds(settings))
                query_settings["thresholds"] = {}
                query_settings["_candidatePoolLimit"] = 200
                query_settings["candidateLimit"] = 200
                _apply_one_category_and_collect_candidates(source_page, query_settings, force_refresh=True)
            # 来源页使用虚拟滚动列表时，不要求备用候选同时出现在 DOM。
            # 每个候选单独滚动到可见位置，再按商品 ID 复核和处理。
            for candidate in group:
                remaining = max(0, target - sum(1 for row in completed if row.get("cartAdded") is True))
                if remaining <= 0:
                    break
                product_id = str(candidate.get("productId") or "")
                if not _ensure_priority_source_candidate_visible(source_page, source_key, candidate):
                    completed.append(_visible_source_candidate_skip(
                        candidate, label, "当前页面未能在有限滚动窗口内唯一复核"
                    ))
                    continue
                try:
                    group_result = execute_selected_candidates(
                        source_page,
                        context,
                        candidates,
                        [int(candidate["cardIndex"])],
                        download_root,
                        settings,
                        progress=progress,
                        analyze_before_cart=not candidate.get("cartAdded") is True,
                        max_selected_products=1,
                    )
                except Exception as error:
                    # 单个候选的来源页渲染/控件问题只跳过当前商品；
                    # 只有已发出平台请求或账号/页面状态异常才由下层保留待核查并中止。
                    if _selection_error_requires_pause(error):
                        raise
                    group_result = [_visible_source_candidate_skip(
                        candidate, label, f"当前商品处理异常（{_safe_diagnostic_text(error, 240)}）"
                    )]
                completed.extend(group_result)
        finally:
            try:
                source_page.close()
            except Exception:
                pass
    return completed


def run_selection_command(
    page: Page,
    context: BrowserContext,
    command: Dict[str, Any],
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    settings = command.get("settings")
    if not isinstance(settings, dict):
        raise RuntimeError("没有收到有效的选品设置")
    visible_sources_only = settings.get("_visibleSourcesOnly") is True
    category_logic_enabled = settings.get("selectionMode") == "category"
    manual_category_ids = _manual_category_selection_ids(settings) if category_logic_enabled and settings.get("categoryMode") == "manual" else set()
    if category_logic_enabled and settings.get("categoryMode") == "manual" and not manual_category_ids:
        raise RuntimeError("手动选类目模式尚未选择任何类目；请先选择类目或切回自动推荐。")
    snapshot = command.get("candidateSnapshot")
    using_snapshot = command.get("previewOnly") is not True and isinstance(snapshot, list)
    # 恢复快照仍需先完成纯本地类目校验，避免无效设置触发任何页面读取。
    if using_snapshot and not visible_sources_only:
        _category_query_settings(settings)
    prompt_kind, prompt_text = _visible_baiying_prompt(page)
    if prompt_kind == "manual_verification":
        raise RuntimeError(
            "百应要求手动完成滑块验证。请在该账号对应的托管 Chrome 中手动完成验证，"
            "然后再次点击自动选品恢复；当前阶段检查点会保留，恢复时按原商品 ID 核验。"
        )
    if prompt_kind == "invoice_eligibility":
        raise RuntimeError(
            "百应页面存在发票资质提醒，但尚未确认弹窗对应的商品；未点击“一键签约”或“加选品车”，已停止。"
        )
    if prompt_kind == "coupon_recommendation" and not using_snapshot:
        raise RuntimeError(
            "百应页面存在消费券推荐弹窗，但本次还没有商品 ID 检查点；为避免误操作其他商品，"
            "没有关闭弹窗或继续请求。请先在百应页面确认原商品，再重新预览。"
        )
    # 恢复流程可能携带候选快照并跳过普通候选查询；仍须先在本地校验类目计划，
    # 避免不一致的父类目设置绕过保护后直接进入加车。
    if using_snapshot:
        _report_operation_progress(progress, "恢复选品检查点", message="核对已保存的原商品 ID")
        candidates = [
            item for item in snapshot
            if isinstance(item, dict) and str(item.get("productId", "")) and str(item.get("title", ""))
            and isinstance(item.get("cardIndex"), int)
        ]
        if category_logic_enabled and settings.get("categoryMode") == "manual" and settings.get("_poolConsumerOnly") is not True:
            outside_manual_scope = [
                candidate for candidate in candidates
                if not _candidate_matches_manual_categories(candidate, manual_category_ids)
            ]
            if outside_manual_scope:
                raise RuntimeError("恢复检查点中有商品不属于当前手动选择的类目范围；请保留原设置恢复，或重新预览后创建新批次。")
        result = {"ok": True, "candidateCount": len(candidates), "candidates": candidates, "warnings": []}
    else:
        try:
            _report_operation_progress(progress, "应用选品条件", message="按已保存设置筛选百应候选")
            result = apply_settings_and_collect_candidates(page, settings, progress=progress)
        except Exception as error:
            raise RuntimeError(f"应用筛选并获取百应候选失败：{error}") from error

    # 页面缓存和历史检查点可能来自旧版规则；在预览展示及所有执行路径前统一复核标题。
    current_candidates = result.get("candidates", [])
    if not isinstance(current_candidates, list):
        current_candidates = []
    candidates, newly_rejected = _screen_candidate_titles(
        [candidate for candidate in current_candidates if isinstance(candidate, dict)], settings
    )
    existing_rejected = result.get("rejectedCandidates", [])
    if not isinstance(existing_rejected, list):
        existing_rejected = []
    rejected_by_id = {
        str(item.get("productId") or ""): item
        for item in existing_rejected if isinstance(item, dict) and str(item.get("productId") or "")
    }
    blocked_indexes: set[int] = set()
    for candidate in newly_rejected:
        card_index = candidate.get("cardIndex")
        if isinstance(card_index, int):
            blocked_indexes.add(card_index)
        product_id = str(candidate.get("productId") or "")
        if product_id and product_id in rejected_by_id:
            rejected_by_id[product_id].update(candidate)
        elif product_id:
            rejected_by_id[product_id] = candidate
        else:
            existing_rejected.append(candidate)
    result["candidates"] = candidates
    result["candidateCount"] = len(candidates)
    result["rejectedCandidates"] = list(rejected_by_id.values())
    title_rejections = [item for item in result["rejectedCandidates"] if item.get("titleFilterExcluded") is True]
    provided = command.get("selectedIndexes")
    requested_indexes = (
        [int(index) for index in provided if isinstance(index, (int, float))]
        if isinstance(provided, list) else None
    )
    requested_index_set = set(requested_indexes or [])
    for item in newly_rejected:
        card_index = item.get("cardIndex")
        if isinstance(card_index, int) and card_index in requested_index_set:
            item["titleFilterWasSelected"] = True
    # Keep the merged rejection objects in sync with the selection marker above.
    for item in title_rejections:
        if item.get("titleFilterWasSelected") is not True:
            item["titleFilterWasSelected"] = any(
                candidate.get("productId") == item.get("productId") and candidate.get("titleFilterWasSelected") is True
                for candidate in newly_rejected
            )
    if command.get("previewOnly") is True:
        _report_operation_progress(
            progress, "候选预览完成", index=len(result.get("candidates", [])),
            total=len(result.get("candidates", [])), percent=100,
            message=f"候选预览完成，共 {len(result.get('candidates', []))} 个",
        )
        return result

    if requested_indexes is not None:
        selected = [index for index in requested_indexes if index not in blocked_indexes]
        selected_title_rejections = [item for item in title_rejections if item.get("titleFilterWasSelected") is True]
    else:
        selected = [int(row["cardIndex"]) for row in candidates if row.get("selected") is True]
        selected_title_rejections = [item for item in title_rejections if item.get("titleFilterWasSelected") is True]
    if not selected and selected_title_rejections:
        result["completed"] = selected_title_rejections
        result["analysisAttemptedCount"] = 0
        result["completionCount"] = len(selected_title_rejections)
        return result
    if not selected and not candidates and title_rejections:
        result["completed"] = title_rejections
        result["analysisAttemptedCount"] = 0
        result["completionCount"] = len(title_rejections)
        return result
    if not selected and candidates:
        raise RuntimeError("没有选择候选商品")
    if command.get("recoverExistingCart") is True:
        selected_rows = [row for row in candidates if row.get("cardIndex") in selected]
        if len(selected_rows) != len(set(selected)) or not selected_rows or any(
            row.get("cartAdded") is not True for row in selected_rows
        ):
            raise RuntimeError("详情恢复只允许处理检查点中已确认加车的原商品；本次未点击加车。")
    _report_operation_progress(
        progress,
        "准备处理选中商品",
        index=0,
        total=len(selected),
        percent=0,
        message=f"已选择 {len(selected)} 个商品，准备逐个核验和处理",
    )

    cart_attempted_ids = {
        str(row.get("productId") or "") for row in candidates
        if isinstance(row, dict) and row.get("cartAttempted") is True and str(row.get("productId") or "")
    }
    cart_confirmed_ids = {
        str(row.get("productId") or "") for row in candidates
        if isinstance(row, dict) and row.get("cartAdded") is True and str(row.get("productId") or "")
    }

    def preserve_cart_markers(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        for row in rows:
            product_id = str(row.get("productId") or "")
            if product_id in cart_attempted_ids:
                row["cartAttempted"] = True
            if product_id in cart_confirmed_ids:
                row["cartAdded"] = True
        return rows

    prompt_kind, prompt_text = _visible_baiying_prompt(page)
    if prompt_kind == "manual_verification":
        raise RuntimeError(
            "百应要求手动完成滑块验证。请在该账号对应的托管 Chrome 中手动完成验证，"
            "然后再次点击自动选品恢复；当前阶段检查点会保留，恢复时按原商品 ID 核验。"
        )
    if prompt_kind == "invoice_eligibility":
        raise RuntimeError(
            "百应页面存在发票资质提醒，但尚未确认弹窗对应的商品；未点击“一键签约”或“加选品车”，已停止。"
        )
    if prompt_kind == "coupon_recommendation":
        if not _dismiss_baiying_coupon_recommendation(page):
            raise RuntimeError("已识别百应消费券推荐弹窗，但无法安全关闭；未点击推荐商品，任务已保留。")
        selected_candidates = [row for row in candidates if row.get("cardIndex") in selected]
        if len(selected_candidates) != len(set(selected)):
            raise RuntimeError("消费券弹窗关闭后，不能按本次选中索引确认原商品；已停止操作。")
        for candidate in selected_candidates:
            try:
                _locate_candidate_card(page, candidate)
            except Exception as error:
                raise RuntimeError(
                    f"消费券弹窗关闭后，无法按原商品 ID {candidate.get('productId')} 复核商品卡片；未点击推荐商品。"
                ) from error

    if visible_sources_only and selected:
        executor = _execute_pool_consumer_candidates if settings.get("_poolConsumerOnly") is True else _execute_visible_source_candidates
        execution = executor(
            context,
            candidates,
            selected,
            str(command.get("downloadPath", "")),
            settings,
            command.get("maxSelectedProducts"),
            progress,
        )
        if settings.get("_poolConsumerOnly") is True:
            completed, untouched_product_ids, retryable_11001_reason = execution
            result["preCartUntouchedProductIds"] = untouched_product_ids
            if retryable_11001_reason:
                # 保留主进程既有字段协议，避免把网络提示的安全停点误判为未结算预占。
                result["poolConsumerRateLimit"] = retryable_11001_reason
        else:
            completed = execution
        result["completed"] = completed
        result["analysisAttemptedCount"] = sum(
            1 for candidate in candidates if candidate.get("analysisAttempted") is True
        )
        result["completionCount"] = len(completed)
        _report_operation_progress(
            progress, "当前可见来源商品处理完成", index=len(completed), total=len(selected), percent=100,
            message=f"处理 {len(completed)} 个热点/榜单来源候选",
        )
        return result

    if using_snapshot and selected:
        original_ids = selected_product_ids(candidates, selected)
        selected_id_set = set(original_ids)
        selected_candidates = [
            row for row in candidates if str(row.get("productId", "")) in selected_id_set
        ]
        source_paths_available = bool(selected_candidates) and all(
            isinstance(candidate.get("sourceCategorySelection"), dict)
            and any(candidate["sourceCategorySelection"].get(level) for level in ("level1", "level2", "level3"))
            for candidate in selected_candidates
        )
        cards_ready = source_paths_available or (len(original_ids) == len(set(selected)) and all(
            _candidate_card_is_available(page, candidate) for candidate in selected_candidates
        ))
        if command.get("refreshCandidatesById") is True or not cards_ready:
            # 从加车检查点恢复时，先刷新百应当前状态：请求可能已成功但客户端来不及保存结果；
            # 重新套用原筛选并按商品 ID 重映射，避免重复加车或把旧 cardIndex 用到新列表。
            refreshed = apply_settings_and_collect_candidates(
                page,
                settings,
                force_refresh=True,
                preserve_product_ids=original_ids,
            )
            live_candidates = preserve_cart_markers(refreshed.get("candidates", []))
            remapped, missing = remap_selected_candidate_indexes(live_candidates, original_ids)
            live_candidates, unavailable = _exclude_unrendered_selected_candidates(page, live_candidates, original_ids)
            if unavailable:
                missing = list(dict.fromkeys(missing + unavailable))
                remapped, _ = remap_selected_candidate_indexes(live_candidates, original_ids)
            if missing:
                # 应用类目/筛选时百应会先返回中间列表，再返回搜索结果。仅当第一份
                # 列表缺少原 ID 时，再点一次页面“搜索”并等待响应稳定；仍按原 ID
                # 恢复，不用排名靠前的其他商品替代。
                searched = search_baiying_candidates(page, settings, preserve_product_ids=original_ids)
                searched_candidates = preserve_cart_markers(searched.get("candidates", []))
                searched_indexes, still_missing = remap_selected_candidate_indexes(searched_candidates, original_ids)
                searched_candidates, unavailable = _exclude_unrendered_selected_candidates(page, searched_candidates, original_ids)
                if unavailable:
                    still_missing = list(dict.fromkeys(still_missing + unavailable))
                    searched_indexes, _ = remap_selected_candidate_indexes(searched_candidates, original_ids)
                if still_missing:
                    raise RuntimeError(
                        "恢复候选时百应当前列表中仍无法唯一确认原商品 ID（缺失或重复）：" + ", ".join(still_missing) +
                        "；为避免误加其他商品，已停止加车并保留任务。请在百应确认该商品仍符合当前筛选条件。"
                    )
                refreshed = searched
                live_candidates = searched_candidates
                remapped = searched_indexes
            if not remapped:
                raise RuntimeError("恢复候选后没有可安全定位的原商品，已停止加车并保留任务")
            candidates = live_candidates
            selected = remapped
            result = refreshed
    if command.get("recoverExistingCart") is True:
        selected_rows = [row for row in candidates if row.get("cardIndex") in selected]
        if not selected_rows or any(row.get("cartAdded") is not True for row in selected_rows):
            raise RuntimeError("详情恢复未能确认检查点中的原商品已加车；本次未点击加车。")
    try:
        completed = execute_selected_candidates(
            page, context, candidates, selected, str(command.get("downloadPath", "")), settings,
            stop_after_cart=bool(command.get("stopAfterCart", False)),
            progress=progress,
            analyze_before_cart=bool(command.get("analyzeBeforeCart", False)),
            max_selected_products=command.get("maxSelectedProducts"),
        )
    except Exception as error:
        raise RuntimeError(f"加入选品车或回收商品详情失败：{error}") from error
    result["completed"] = [*selected_title_rejections, *completed]
    analysis_attempted_count = sum(
        1 for candidate in candidates if candidate.get("analysisAttempted") is True
    )
    result["analysisAttemptedCount"] = analysis_attempted_count
    result["completionCount"] = len(result["completed"])
    _report_operation_progress(
        progress,
        "选中商品处理结果已返回",
        index=analysis_attempted_count,
        total=analysis_attempted_count,
        percent=100,
        message=f"详情核验 {analysis_attempted_count} 个候选；返回 {len(completed)} 个商品结果",
    )
    return result


def inspect_selection_page(profile_path: Path) -> int:
    """只读检查百应选品广场的筛选控件及当前动态选项。"""
    try:
        port = read_port(profile_path)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("Chrome CDP 会话没有默认浏览器上下文")
            page = find_selection_page(contexts[0], browser=browser)
            if page is None:
                raise RuntimeError(
                    f"托管百应选品页等待 {SELECTION_PAGE_READY_TIMEOUT_MS // 1000} 秒后仍未就绪；"
                    "请确认已登录并能正常打开选品广场。"
                )
            emit(collect_filter_catalog(page))
        return 0
    except Exception as error:
        emit({"ok": False, "error": str(error)})
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="客户端托管 Chrome 的 Playwright CDP 桥接")
    parser.add_argument("command", choices=("watch", "inspect", "inspect-selection"))
    parser.add_argument("--profile", required=True, help="Chrome 用户数据目录")
    args = parser.parse_args(argv)
    profile_path = Path(args.profile).expanduser().resolve()
    if args.command == "watch":
        return run_watch(profile_path)
    if args.command == "inspect-selection":
        return inspect_selection_page(profile_path)
    return run_inspect(profile_path)


if __name__ == "__main__":
    raise SystemExit(main())
