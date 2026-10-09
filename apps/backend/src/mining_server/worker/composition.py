from mining_server.application.tasks import HandlerRegistry
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings


def create_registry(database: Database, settings: Settings) -> HandlerRegistry:
    from mining_server.application.documents import (
        register_handlers as register_documents,
    )
    from mining_server.application.market import register_handlers as register_market
    from mining_server.application.news import register_handlers as register_news

    registry = HandlerRegistry()
    register_documents(registry, database, settings)
    register_market(registry, database, settings)
    register_news(registry, database, settings)
    return registry
