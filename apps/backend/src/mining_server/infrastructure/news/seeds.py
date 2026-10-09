from dataclasses import dataclass

from mining_contracts.domain.news import SourceState

from mining_server.domain.news import (
    FeedBodyPolicy,
    SourceCreate,
    SourceKind,
    SourceRules,
)


@dataclass(frozen=True)
class SourceSeedPolicy:
    announcement_items: int = 30
    announcement_backfill_days: int = 30
    slower_interval_minutes: int = 120
    feed_items: int = 10
    feed_backfill_days: int = 7


def defaults() -> list[SourceCreate]:
    return [
        SourceCreate(
            name="PLS news",
            state=SourceState.DISABLED,
            publisher="PLS",
            kind=SourceKind.HTML_LIST,
            url="https://www.pls.com/news",
            rules=SourceRules(
                allowed_domains=["pls.com"],
                link_pattern=r"/news/.+",
                article_selector="article, main",
                timezone="Australia/Perth",
            ),
        ),
        SourceCreate(
            name="PLS ASX announcements",
            publisher="PLS",
            kind=SourceKind.DOCUMENT_URLS,
            url="https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode=PLS&timeframe=D&period=M6",
            max_items=SourceSeedPolicy().announcement_items,
            backfill_days=SourceSeedPolicy().announcement_backfill_days,
            rules=SourceRules(
                allowed_domains=["asx.com.au"],
                list_selector="table tbody tr",
                link_pattern=r"/displayAnnouncement\.do\?",
                time_selector="td:first-child",
                date_format="%d/%m/%Y %I:%M %p",
                document_url_selector="input[name=pdfURL]",
                timezone="Australia/Sydney",
            ),
        ),
        SourceCreate(
            name="Sigma Lithium RSS",
            publisher="Sigma Lithium",
            kind=SourceKind.RSS,
            url="https://sigmalithiumresources.com/feed/",
            rules=SourceRules(
                allowed_domains=["sigmalithiumresources.com", "sigmalithiumcorp.com"],
                article_selector=".b2iNewsItemBodyDiv, .elementor-widget-theme-post-content, article, .entry-content, main",
                time_selector="time[datetime]",
                timezone="America/Sao_Paulo",
                project="Grota do Cirilo",
            ),
        ),
        SourceCreate(
            name="Australian Mining",
            state=SourceState.DISABLED,
            publisher="Australian Mining",
            kind=SourceKind.HTML_LIST,
            url="https://www.australianmining.com.au/",
            interval_minutes=SourceSeedPolicy().slower_interval_minutes,
            rules=SourceRules(
                allowed_domains=["australianmining.com.au"],
                link_pattern=r"australianmining\.com\.au/[a-z0-9-]+/$",
                article_selector="article, .entry-content, main",
                timezone="Australia/Sydney",
            ),
        ),
        SourceCreate(
            name="Mining Weekly lithium",
            state=SourceState.DISABLED,
            publisher="Mining Weekly",
            kind=SourceKind.HTML_LIST,
            url="https://www.miningweekly.com/page/lithium",
            interval_minutes=SourceSeedPolicy().slower_interval_minutes,
            rules=SourceRules(
                allowed_domains=["miningweekly.com"],
                link_pattern=r"/article/",
                article_selector=".article-content, article, main",
                timezone="Africa/Johannesburg",
            ),
        ),
        SourceCreate(
            name="Mining.com.au PLS RSS",
            publisher="Mining.com.au",
            kind=SourceKind.RSS,
            url="https://mining.com.au/feed/?s=PLS",
            max_items=SourceSeedPolicy().feed_items,
            backfill_days=SourceSeedPolicy().feed_backfill_days,
            rules=SourceRules(
                allowed_domains=["mining.com.au"],
                feed_body_policy=FeedBodyPolicy.FEED_FULL_TEXT,
                timezone="Australia/Perth",
            ),
        ),
        SourceCreate(
            name="International Mining RSS",
            publisher="International Mining",
            kind=SourceKind.RSS,
            url="https://im-mining.com/feed/",
            max_items=SourceSeedPolicy().feed_items,
            backfill_days=SourceSeedPolicy().feed_backfill_days,
            rules=SourceRules(
                allowed_domains=["im-mining.com"],
                feed_body_policy=FeedBodyPolicy.FEED_FULL_TEXT,
                timezone="UTC",
            ),
        ),
    ]
