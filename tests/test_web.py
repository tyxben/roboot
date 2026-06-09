"""Tests for tools/web.py — SSRF guard, HTML→text, fetch/search formatting.

No real network: _get is monkeypatched with canned responses, and SSRF
rejection cases use IP-literals / localhost so getaddrinfo stays offline.
"""

from __future__ import annotations

import pytest

from tools import web


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/",
        "http://127.0.0.1:8765/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_validate_rejects_unsafe(url):
    ok, reason = web._validate_url(url)
    assert ok is False
    assert reason


def test_validate_accepts_public_ip():
    # IP literal → getaddrinfo returns it without DNS; globally routable.
    ok, reason = web._validate_url("http://93.184.216.34/")
    assert ok is True, reason


def test_validate_rejects_missing_host():
    ok, _ = web._validate_url("http:///path")
    assert ok is False


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------


def test_html_to_text_strips_script_style():
    html = (
        "<html><head><style>.x{}</style></head><body>"
        "<script>var x=1</script><h1>Title</h1><p>Para one</p>"
        "<p>Para two</p></body></html>"
    )
    txt = web._html_to_text(html)
    assert "Title" in txt and "Para one" in txt and "Para two" in txt
    assert "var x" not in txt and ".x{}" not in txt


def test_ddg_results_parse_and_unwrap():
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg='
        "https%3A%2F%2Fexample.com%2Fpage&rut=abc\">Example Title</a>"
        '<a class="result__a" href="https://direct.example.org/">Direct</a>'
    )
    p = web._DDGResults()
    p.feed(html)
    assert ("Example Title", "https://example.com/page") in p.results
    assert ("Direct", "https://direct.example.org/") in p.results


# ---------------------------------------------------------------------------
# web_fetch / web_search (network mocked at _get)
# ---------------------------------------------------------------------------


async def test_web_fetch_rejects_internal():
    out = await web.web_fetch("http://127.0.0.1:8765/api/sessions")
    assert "SSRF" in out or "非公网" in out


async def test_web_fetch_empty():
    assert "不能为空" in await web.web_fetch("")


async def test_web_fetch_html_to_text(monkeypatch):
    async def fake_get(url, data=None):
        return 200, {"content-type": "text/html"}, "<p>Hello</p><p>World</p>"

    monkeypatch.setattr(web, "_get", fake_get)
    out = await web.web_fetch("http://93.184.216.34/")
    assert "HTTP 200" in out and "Hello" in out and "World" in out


async def test_web_fetch_plain_passthrough(monkeypatch):
    async def fake_get(url, data=None):
        return 200, {"content-type": "application/json"}, '{"ok": true}'

    monkeypatch.setattr(web, "_get", fake_get)
    out = await web.web_fetch("http://93.184.216.34/api")
    assert '{"ok": true}' in out


async def test_web_search_parses(monkeypatch):
    canned = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fpy.org%2F">Python</a>'
        '<a class="result__a" href="https://docs.example/">Docs</a>'
    )

    async def fake_get(url, data=None):
        assert "duckduckgo" in url
        assert data == {"q": "python latest"}
        return 200, {"content-type": "text/html"}, canned

    monkeypatch.setattr(web, "_get", fake_get)
    out = await web.web_search("python latest", limit=5)
    assert "Python" in out and "https://py.org/" in out
    assert "Docs" in out


async def test_web_search_empty_query():
    assert "不能为空" in await web.web_search("")
