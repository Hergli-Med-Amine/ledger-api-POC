import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import create_engine

from ledger_api.api import errors, wallets


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engine = create_engine(os.environ["DATABASE_URL"])
    yield
    app.state.engine.dispose()


app = FastAPI(title="ledger-api", version="0.1.0", lifespan=lifespan)
errors.install(app)
app.include_router(wallets.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
