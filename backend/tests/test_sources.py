"""Tavily search behavior without external requests."""
import json
import threading
import time

import httpx
import pytest

from app.schemas import QuestionSpec, utcnow
from app.sources import online_search


def current_question() -> QuestionSpec:
    return QuestionSpec(question="未来三个月该事项会如何发展？", mode="scenario", as_of=utcnow())


class FakeResponse:
    def __init__(self, results):
        self.results = results

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_bytes(self):
        yield json.dumps({"results": self.results}).encode()


def test_online_search_is_concurrent_but_keeps_query_order(monkeypatch, tmp_path):
    barrier = threading.Barrier(3)
    results = {
        "first": [
            {"url": "https://example.org/first", "title": "First", "content": "first text"},
            {"url": "https://example.org/shared", "title": "Shared first", "content": "first shared text"},
        ],
        "second": [
            {"url": "https://example.org/shared", "title": "Shared second", "content": "second shared text"},
            {"url": "https://example.org/second", "title": "Second", "content": "second text"},
        ],
        "third": [{"url": "https://example.org/third", "title": "Third", "content": "third text"}],
    }

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 25

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, method, url, json):
            assert url == "https://api.tavily.com/search"
            barrier.wait(timeout=2)
            time.sleep({"first": .06, "second": .03, "third": 0}[json["query"]])
            return FakeResponse(results[json["query"]])

    monkeypatch.setattr("app.sources.config.TAVILY_API_KEY", "test-only")
    monkeypatch.setattr("app.sources.httpx.Client", FakeClient)
    evidence = online_search(current_question(), tmp_path, ["first", "second", "third", "ignored"])

    urls = [str(item.source_url) for item in evidence]
    assert set(urls) == {
        "https://example.org/first", "https://example.org/shared",
        "https://example.org/second", "https://example.org/third",
    }
    assert [item.id for item in evidence] == ["E001", "E002", "E003", "E004"]
    shared = next(item for item in evidence if str(item.source_url).endswith("/shared"))
    assert shared.title == "Shared first"
    assert set(shared.query_ids) == {"R001", "R002"}
    snapshot = json.loads((tmp_path / shared.snapshot_path).read_text())
    assert snapshot["metadata"]["query_ids"] == ["R001", "R002"]


def test_online_search_uses_successful_queries_and_reports_total_failure(monkeypatch, tmp_path):
    fail_all = False

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, method, url, json):
            if fail_all or json["query"] == "fail":
                raise httpx.ConnectError("offline")
            return FakeResponse([{"url": "https://example.org/working", "content": "working text"}])

    monkeypatch.setattr("app.sources.config.TAVILY_API_KEY", "test-only")
    monkeypatch.setattr("app.sources.httpx.Client", FakeClient)
    evidence = online_search(current_question(), tmp_path, ["fail", "working"])
    assert len(evidence) == 1
    assert evidence[0].excerpt == "working text"

    fail_all = True
    with pytest.raises(RuntimeError, match="Tavily 在线检索全部失败.*第 1 条.*第 2 条"):
        online_search(current_question(), tmp_path, ["fail", "working"])
