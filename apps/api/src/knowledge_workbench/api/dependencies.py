from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_workbench.application.ports.object_storage import ObjectStorage
from knowledge_workbench.config import Settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_object_storage(request: Request) -> ObjectStorage:
    return request.app.state.object_storage  # type: ignore[no-any-return]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
StorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
