import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from mining_server.infrastructure import task_models
from mining_server.infrastructure.auth import models as auth_models
from mining_server.infrastructure.database import Base
from mining_server.infrastructure.documents import models as document_models
from mining_server.infrastructure.market import models as market_models
from mining_server.infrastructure.news import models as news_models
from mining_server.infrastructure.settings import Settings

models = (task_models, auth_models, document_models, market_models, news_models)
target_metadata = Base.metadata
configuration = Settings()


def run_migrations_offline() -> None:
    context.configure(
        url=configuration.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(configuration.database_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
