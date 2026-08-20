from __future__ import annotations

import pytest

from nika_core.research.pagination import (
    PaginationDiscovery,
    PaginationPolicy,
    discover_html_pagination,
    discover_json_pagination,
    extend_pagination_frontier,
)


def test_html_pagination_uses_rel_next_and_same_origin_only() -> None:
    discovery = discover_html_pagination(
        "https://example.com/list?page=1",
        """
        <html><head><link rel="next" href="/list?page=2#top"></head>
        <body>
          <a href="/ignored?page=2">not pagination</a>
          <a rel="NEXT" href="https://other.example/list?page=2">foreign</a>
        </body></html>
        """,
    )

    assert discovery.next_urls == ("https://example.com/list?page=2",)


def test_html_pagination_deduplicates_exact_urls_and_respects_page_link_bound() -> None:
    policy = PaginationPolicy(max_discovered_links_per_page=2)
    discovery = discover_html_pagination(
        "https://example.com/list/",
        """
        <a rel="next" href="?page=2"></a>
        <link rel="next" href="?page=2#fragment">
        <a rel="next" href="?page=3"></a>
        <a rel="next" href="?page=4"></a>
        """,
        policy=policy,
    )

    assert discovery.next_urls == (
        "https://example.com/list/?page=2",
        "https://example.com/list/?page=3",
    )


def test_json_pagination_checks_only_explicit_next_fields() -> None:
    discovery = discover_json_pagination(
        "https://example.com/api/items?page=1",
        '{"items": [{"url": "/not-next"}], "next": "/api/items?page=2"}',
    )

    assert discovery.next_urls == ("https://example.com/api/items?page=2",)


def test_json_pagination_rejects_invalid_utf8_bytes() -> None:
    with pytest.raises(UnicodeDecodeError):
        discover_json_pagination("https://example.com/api", b"\xff")


def test_frontier_is_bounded_and_loop_safe() -> None:
    policy = PaginationPolicy(max_pages=4)
    discovery = PaginationDiscovery(
        page_url="https://example.com/2",
        next_urls=(
            "https://example.com/1",
            "https://example.com/3",
            "https://example.com/4",
            "https://example.com/5",
        ),
    )

    frontier = extend_pagination_frontier(
        visited_urls=("https://example.com/1", "https://example.com/2"),
        queued_urls=("https://example.com/3",),
        discovery=discovery,
        policy=policy,
    )

    assert frontier == ("https://example.com/3", "https://example.com/4")


def test_pagination_policy_rejects_unbounded_or_empty_configuration() -> None:
    with pytest.raises(ValueError, match="max_pages"):
        PaginationPolicy(max_pages=0)
    with pytest.raises(ValueError, match="max_discovered"):
        PaginationPolicy(max_discovered_links_per_page=0)
    with pytest.raises(ValueError, match="json_next_fields"):
        PaginationPolicy(json_next_fields=())
