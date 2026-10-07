# -*- coding: utf-8 -*-
"""sse-market-assist 服务入口：FastAPI + LangChain，监听 0.0.0.0:8080（平台 pod 内，公网经 host nginx:23012 -> frps:13012）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api import router
from app.config import settings
from app.db import ensure_assist_responses_table
from app.qdrant_store import ensure_collection


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_collection()
    ensure_assist_responses_table()
    yield


app = FastAPI(title="sse-market-assist", version="0.1.0", lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.listen_host, port=settings.listen_port, log_level="info")
