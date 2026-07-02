#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TriBrief 核心函数单元测试。运行：python -m pytest test_brief.py -v"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import pytest

# 导入被测模块（修改 sys.path 以确保能找到 brief.py）
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brief import (  # type: ignore[import-not-found]
    _normalize_url,
    _normalize_title,
    _levenshtein_ratio,
    _strip_html,
    _extract_json,
    _is_retryable,
    _env_substitute,
    _parse_published,
    dedupe,
    Item,
)


# ==================================================================
# _normalize_url
# ==================================================================
class TestNormalizeURL:
    def test_strips_query_and_fragment(self):
        assert _normalize_url("https://example.com/path?utm=123") == \
            "https://example.com/path"

    def test_strips_trailing_slash(self):
        assert _normalize_url("https://example.com/path/") == \
            "https://example.com/path"

    def test_lowercases_scheme_and_host(self):
        assert _normalize_url("HTTPS://Example.COM/Path") == \
            "https://example.com/path"

    def test_handles_relative_url(self):
        result = _normalize_url("example.com/path")
        assert "example.com/path" in result


# ==================================================================
# _normalize_title
# ==================================================================
class TestNormalizeTitle:
    def test_strips_punctuation_and_whitespace(self):
        result = _normalize_title("Hello, World! How are you?")
        assert result == "helloworldhowareyou"

    def test_preserves_cjk(self):
        result = _normalize_title("苹果发布M4芯片")
        assert "苹果发布m4芯片" in result.lower()

    def test_handles_empty(self):
        assert _normalize_title("") == ""


# ==================================================================
# _levenshtein_ratio
# ==================================================================
class TestLevenshteinRatio:
    def test_identical_strings(self):
        assert _levenshtein_ratio("hello", "hello") == 1.0

    def test_completely_different(self):
        assert _levenshtein_ratio("abc", "xyz") < 0.5

    def test_one_edit_apart(self):
        # "hello" (5) vs "hallo" (5), 1 edit → ratio = 1 - 1/5 = 0.8
        ratio = _levenshtein_ratio("hello", "hallo")
        assert ratio == 0.8

    def test_length_mismatch_returns_zero(self):
        # "hi" (2) vs "hello world this is long" (26), min/max < 0.6
        assert _levenshtein_ratio("hi", "hello world this is long") == 0.0

    def test_empty_string(self):
        assert _levenshtein_ratio("", "hello") == 0.0
        assert _levenshtein_ratio("hello", "") == 0.0


# ==================================================================
# _strip_html
# ==================================================================
class TestStripHTML:
    def test_strips_tags(self):
        assert _strip_html("<p>Hello <b>World</b></p>") == "Hello World"

    def test_unescapes_entities(self):
        result = _strip_html("A &amp; B &lt; C")
        assert "&" in result
        assert "<" in result

    def test_handles_empty_and_none(self):
        assert _strip_html("") == ""
        assert _strip_html(None) == ""


# ==================================================================
# _extract_json
# ==================================================================
class TestExtractJSON:
    def test_plain_array(self):
        assert _extract_json('[{"id":1}]', "array") == [{"id": 1}]

    def test_markdown_code_block(self):
        result = _extract_json('```json\n[{"id":1}]\n```', "array")
        assert result == [{"id": 1}]

    def test_code_block_without_lang(self):
        result = _extract_json('```\n[{"id":2}]\n```', "array")
        assert result == [{"id": 2}]

    def test_surrounding_text(self):
        result = _extract_json('Here is the result: [{"id":3}] Thanks!', "array")
        assert result == [{"id": 3}]

    def test_object(self):
        result = _extract_json('{"key":"value"}', "object")
        assert result == {"key": "value"}


# ==================================================================
# _is_retryable
# ==================================================================
class TestIsRetryable:
    def test_429_is_retryable(self):
        assert _is_retryable(429) is True

    def test_5xx_is_retryable(self):
        for code in (500, 502, 503, 504):
            assert _is_retryable(code) is True

    def test_4xx_not_retryable(self):
        for code in (400, 401, 403, 404):
            assert _is_retryable(code) is False

    def test_200_not_retryable(self):
        assert _is_retryable(200) is False


# ==================================================================
# _env_substitute
# ==================================================================
class TestEnvSubstitute:
    def test_simple_replacement(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        assert _env_substitute("${MY_VAR}") == "hello"

    def test_falls_back_to_extra_vars(self):
        result = _env_substitute("${RSSHUB_BASE}/path", extra_vars={"RSSHUB_BASE": "http://localhost:1200"})
        assert result == "http://localhost:1200/path"

    def test_env_overrides_extra_vars(self, monkeypatch):
        monkeypatch.setenv("RSSHUB_BASE", "http://prod:1200")
        result = _env_substitute("${RSSHUB_BASE}/path", extra_vars={"RSSHUB_BASE": "http://localhost:1200"})
        assert result == "http://prod:1200/path"

    def test_recursive_in_dict(self, monkeypatch):
        monkeypatch.setenv("SMTP_USER", "user@test.com")
        result = _env_substitute({"email": {"user": "${SMTP_USER}"}})
        assert result["email"]["user"] == "user@test.com"

    def test_keeps_unresolved(self):
        result = _env_substitute("${NONEXISTENT_VAR}")
        assert "${NONEXISTENT_VAR}" in result


# ==================================================================
# dedupe
# ==================================================================
class TestDedupe:
    def _item(self, title: str, url: str, summary: str = "") -> Item:
        return Item(title=title, url=url, summary_raw=summary,
                    published=None, source_name="test", region="global",
                    track_hint="chip")

    def test_url_dedup_keeps_longer_summary(self):
        items = [
            self._item("T1", "https://a.com/news", "short"),
            self._item("T1", "https://a.com/news", "longer summary here"),
        ]
        result = dedupe(items)
        assert len(result) == 1
        assert result[0].summary_raw == "longer summary here"

    def test_exact_title_match(self):
        items = [
            self._item("Same Title", "https://a.com/1"),
            self._item("Same Title", "https://b.com/2"),
        ]
        result = dedupe(items)
        assert len(result) == 1

    def test_containment_match(self):
        items = [
            self._item("NVIDIA Announces H200 GPU with Record Performance", "https://a.com/1"),
            self._item("NVIDIA Announces H200 GPU", "https://b.com/2"),
        ]
        result = dedupe(items)
        assert len(result) == 1

    def test_levenshtein_catches_similar_titles(self):
        items = [
            self._item("Google DeepMind Introduces New Robot Policy", "https://a.com/1"),
            self._item("Google DeepMind Introduces Novel Robot Policy", "https://b.com/2"),
        ]
        result = dedupe(items)
        # "New" vs "Novel" — one char difference, ratio should be very high
        assert len(result) == 1

    def test_different_articles_kept_separate(self):
        items = [
            self._item("Apple Announces M4 Chip", "https://a.com/1"),
            self._item("Tesla Bot Gen 3 Unveiled at AI Day", "https://b.com/2"),
        ]
        result = dedupe(items)
        assert len(result) == 2


# ==================================================================
# _parse_published
# ==================================================================
class TestParsePublished:
    def test_returns_none_for_empty(self):
        from unittest.mock import MagicMock
        entry = MagicMock()
        # feedparser entries use .get() which returns None for missing keys
        entry.get = lambda k, default=None: None
        assert _parse_published(entry) is None
