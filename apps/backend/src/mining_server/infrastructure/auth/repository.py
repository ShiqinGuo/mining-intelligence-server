from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.infrastructure.auth.models import ModelConnection


class AuthRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def current(self, lock: bool = False) -> ModelConnection | None:
        statement = select(ModelConnection).where(ModelConnection.slot == 1)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)
