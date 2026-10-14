"""FastAPI 主应用"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, Response

sys.path.insert(0, str(Path(__file__).parent / "src"))

from config import get_config
from failover import FailoverClient, RouteRule

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    client = FailoverClient(config)
    app.state.client = client
    logger.info(f"API 聚合网关启动，策略={config.settings.failover_strategy}，"
                f"提供商={len(config.providers)}个，路由规则={len(config.routes)}条")
    yield
    logger.info("服务关闭")


app = FastAPI(
    title="API Failover Gateway",
    description="借鉴 CodexPlusPlus 的 API 聚合故障切换网关",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    client = app.state.client
    return {
        "service": "API Failover Gateway v2",
        "strategy": client.config.settings.failover_strategy,
        "providers": client.get_status(),
        "routes": [
            {"pattern": r.pattern, "provider": r.provider_name, "priority": r.priority}
            for r in client.config.routes
        ],
    }


@app.get("/health")
async def health():
    client = app.state.client
    statuses = client.get_status()
    healthy = [s for s in statuses if s["enabled"] and s["consecutive_failures"] == 0]
    return {
        "status": "healthy" if len(healthy) > 0 else "degraded",
        "total": len(statuses),
        "healthy": len(healthy),
        "strategy": client.config.settings.failover_strategy,
        "providers": statuses,
    }


@app.post("/v1/chat/completions")
@app.get("/v1/chat/completions")
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    client = app.state.client

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

    skip_headers = {
        "host", "transfer-encoding", "content-length", "connection",
        "keep-alive", "upgrade", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "expect",
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip_headers}

    # 提取 conversation_id（从 header 或生成）
    conversation_id = (
        request.headers.get("x-conversation-id")
        or request.headers.get("X-Conversation-Id")
        or str(uuid.uuid4())[:8]
    )

    # 提取 model
    model = None
    if body:
        try:
            import json
            body_json = json.loads(body)
            model = body_json.get("model")
        except Exception:
            pass

    try:
        resp = await client.forward_request(
            method=request.method,
            path="/" + path,
            headers=headers,
            body=body,
            conversation_id=conversation_id,
            model=model,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    exclude_headers = {
        "transfer-encoding", "content-encoding", "content-length",
        "connection", "keep-alive", "upgrade",
    }
    response_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in exclude_headers
    }

    is_stream = (
        request.query_params.get("stream") == "true"
        or "text/event-stream" in (resp.headers.get("content-type") or "")
    )

    if is_stream:
        return StreamingResponse(
            content=resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=response_headers,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type", "application/json"),
    )