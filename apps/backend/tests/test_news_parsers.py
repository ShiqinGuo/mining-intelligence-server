from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mining_contracts.domain.news import SourceState
from pydantic import ValidationError

from mining_server.application.news import NewsService
from mining_server.domain.core import DomainError
from mining_server.domain.documents import PublicBytesResponse
from mining_server.domain.news import FeedBodyPolicy, Source, SourceKind, SourceRules
from mining_server.infrastructure.news.parsers import (
    canonical_url,
    discover,
    parse_article,
    parse_feed_article,
)
from mining_server.infrastructure.news.seeds import defaults


def source(kind=SourceKind.RSS, policy=FeedBodyPolicy.WEBPAGE) -> Source:
    return Source(
        id=uuid4(),
        revision=1,
        name="Test publisher",
        publisher="Test publisher",
        kind=kind,
        url="https://example.com/feed",
        rules=SourceRules(
            feed_body_policy=policy,
            allowed_domains=["example.com"],
            timezone="Australia/Perth",
            list_selector=".story",
            article_selector="article",
        ),
        next_due_at=datetime.now(UTC),
        cursor=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_rss_normalizes_and_deduplicates_urls_and_keeps_publication_time():
    raw = b"<rss><channel><item><title>Mine expansion</title><link>https://example.com/story?utm_source=rss</link><pubDate>Thu, 08 Oct 2026 07:30:00 +0800</pubDate></item><item><title>Duplicate</title><link>https://example.com/story</link></item><item><title>Unknown publication</title><link>https://example.com/undated</link></item></channel></rss>"
    items = discover(raw, source())
    assert len(items) == 2
    assert str(items[0].url) == "https://example.com/story"
    assert items[0].published_at == datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
    assert items[1].published_at is None


def test_html_rejects_off_domain_links_without_following_navigation():
    raw = b'<div class="story"><a href="javascript:void(0)">Navigation</a></div><div class="story"><a href="https://attacker.test/story">Other</a></div><div class="story"><a href="/mine">Mine</a><time datetime="2026-10-08T07:00:00">Date</time></div>'
    items = discover(raw, source(SourceKind.HTML_LIST))
    assert len(items) == 1
    assert str(items[0].url) == "https://example.com/mine"
    assert items[0].published_at == datetime(2026, 10, 7, 23, tzinfo=UTC)


def test_invalid_explicit_source_timestamp_does_not_default_to_fetch_time():
    with pytest.raises(DomainError):
        discover(
            b"<rss><channel><item><title>Mine</title><link>https://example.com/mine</link><pubDate>unknown explicit date</pubDate></item></channel></rss>",
            source(),
        )


def test_article_body_excludes_navigation_and_unknown_date_stays_unknown():
    content = parse_article(
        b"<nav>Not article evidence</nav><h1>Mine expansion</h1><article>The mining company announced a new spodumene processing plant.</article>",
        "https://example.com/mine",
        source(),
        datetime.now(UTC),
    )
    assert "Not article" not in content.content
    assert content.published_at is None
    assert len(content.content_hash) == 64


def test_article_access_challenge_is_an_error():
    with pytest.raises(DomainError):
        parse_article(
            b"<h1>Challenge</h1><article>Checking your browser. Please enable javascript before viewing this page.</article>",
            "https://example.com/mine",
            source(),
            datetime.now(UTC),
        )


def test_url_keeps_business_query_and_rejects_credentials():
    assert (
        canonical_url("https://EXAMPLE.com/story?id=12&utm_source=test#section")
        == "https://example.com/story?id=12"
    )
    with pytest.raises(DomainError):
        canonical_url("https://secret@example.com/story")


@pytest.mark.parametrize(
    "field,value",
    [
        ("timezone", "Unknown/Timezone"),
        ("timezone", "/absolute/timezone"),
        ("link_pattern", "["),
        ("list_selector", "a["),
        ("article_selector", "div["),
        ("title_selector", "h1["),
        ("time_selector", "time["),
    ],
)
def test_invalid_source_configuration_fails_at_construction(field, value):
    with pytest.raises(ValidationError) as caught:
        SourceRules.model_validate({"allowed_domains": ["example.com"], field: value})
    assert caught.value.errors()[0]["loc"] == (field,)


def full_feed_source() -> Source:
    return source(policy=FeedBodyPolicy.FEED_FULL_TEXT)


def feed(body: str) -> bytes:
    return (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
        "<title>PLS corporate update</title><link>https://example.com/story?utm_source=rss</link>"
        "<pubDate>Wed, 07 Oct 2026 07:30:00 +0800</pubDate>"
        "<description>A short summary only.</description>"
        + body
        + "</item></channel></rss>"
    ).encode()


def test_feed_body_retains_publication_provenance_and_removes_boilerplate():
    selected = full_feed_source()
    raw = feed(
        "<content:encoded><![CDATA[<p>PLS announced a corporate financing update with no new mine resource estimate.</p><script>danger</script><style>noise</style><nav>Navigation</nav><p>The post PLS corporate update appeared first on Mining.com.au.</p>]]></content:encoded>"
    )
    item = discover(raw, selected)[0]
    result = parse_feed_article(item, selected, datetime.now(UTC))
    assert (
        result.content
        == "PLS announced a corporate financing update with no new mine resource estimate."
    )
    assert result.published_at == datetime(2026, 10, 6, 23, 30, tzinfo=UTC)
    assert str(result.url) == "https://example.com/story"
    assert result.source_id == selected.id
    assert result.publisher == selected.publisher
    assert result.project is None
    assert result.feed_body is None
    assert (
        parse_feed_article(item, selected, datetime.now(UTC)).content_hash
        == result.content_hash
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "<content:encoded> </content:encoded>",
        "<content:encoded>Short excerpt</content:encoded>",
        "<content:encoded>Checking your browser. Please enable javascript before viewing this page.</content:encoded>",
    ],
)
def test_full_feed_policy_rejects_missing_incomplete_and_challenge_body(body):
    selected = full_feed_source()
    with pytest.raises(DomainError):
        for item in discover(feed(body), selected):
            parse_feed_article(item, selected, datetime.now(UTC))


def test_webpage_policy_does_not_consume_rss_summary_or_body():
    item = discover(feed("<content:encoded>Ignored body</content:encoded>"), source())[
        0
    ]
    assert item.feed_body is None
    assert item.summary == "A short summary only."


@pytest.mark.parametrize(
    "body",
    [
        '<content type="html">&lt;p&gt;Mining company announced a processing plant expansion and funding update.&lt;/p&gt;</content>',
        '<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Mining company announced a processing plant expansion and funding update.</p></div></content>',
    ],
)
def test_atom_content_is_supported_without_inventing_publication(body):
    raw = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Mine</title><link href="https://example.com/story"/>'
        + body
        + "</entry></feed>"
    ).encode()
    selected = full_feed_source()
    result = parse_feed_article(discover(raw, selected)[0], selected, datetime.now(UTC))
    assert "processing plant expansion" in result.content
    assert result.published_at is None


async def test_collection_uses_feed_body_when_webpage_is_unavailable():
    selected = full_feed_source()

    class FeedOnlyHttp:
        def __init__(self):
            self.requests: list[str] = []

        async def get(self, url: str, max_bytes: int) -> PublicBytesResponse:
            self.requests.append(url)
            if url != str(selected.url):
                raise AssertionError("Article webpage is unavailable")
            return PublicBytesResponse(
                data=feed(
                    "<content:encoded><![CDATA[<p>PLS announced a corporate financing update with no new mine resource estimate.</p>]]></content:encoded>"
                ),
                final_url=url,
                content_type="application/rss+xml",
            )

    http = FeedOnlyHttp()
    service = NewsService(None, None, None, http)
    batch = await service.collect_content(selected)
    repeated = await service.collect_content(selected)
    assert http.requests == [str(selected.url), str(selected.url)]
    assert batch.discovered == 1
    assert batch.articles[0].content == repeated.articles[0].content
    assert batch.articles[0].url == repeated.articles[0].url
    assert batch.articles[0].content_hash == repeated.articles[0].content_hash
    assert batch.articles[0].published_at == datetime(2026, 10, 6, 23, 30, tzinfo=UTC)


def test_replacement_defaults_are_explicit_full_feeds_without_project_tags():
    selected = defaults()
    feeds = [
        item
        for item in selected
        if item.rules.feed_body_policy == FeedBodyPolicy.FEED_FULL_TEXT
    ]
    assert {str(item.url) for item in feeds} == {
        "https://mining.com.au/feed/?s=PLS",
        "https://im-mining.com/feed/",
    }
    assert all(
        item.state == SourceState.ENABLED
        and item.rules.project is None
        and item.max_items == 10
        and item.backfill_days == 7
        for item in feeds
    )
    assert {item.name for item in selected if item.state == SourceState.DISABLED} == {
        "PLS news",
        "Australian Mining",
        "Mining Weekly lithium",
    }


def test_feed_policy_rejects_unknown_policy_and_non_rss_source():
    with pytest.raises(ValidationError):
        SourceRules(allowed_domains=["example.com"], feed_body_policy="summary")
    with pytest.raises(ValidationError):
        source(SourceKind.HTML_LIST, FeedBodyPolicy.FEED_FULL_TEXT)
