"""故障切换核心逻辑 — 借鉴 CodexPlusPlus RelayRotationSelector 设计"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from src.config import AppConfig, Provider

logger = logging.getLogger(__name__)


class RouteRule:
    """单条 model 路由规则"""

    def __init__(self, pattern: str, provider_name: str, priority: int):
        self.pattern = pattern.strip().lower()
        self.provider_name = provider_name
        self.priority = priority

    def matches(self, model: str) -> bool:
        model = model.strip().lower()
        if not self.pattern or not model:
            return False
        if "*" not in self.pattern:
            return self.pattern == model
        segments = self.pattern.split("*")
        pos = 0
        for i, seg in enumerate(segments):
            if not seg:
                continue
            if i == 0:
                if not model[pos:].startswith(seg):
                    return False
                pos += len(seg)
            elif i == len(segments) - 1:
                if not model.endswith(seg) or len(model) - len(seg) < pos:
                    return False
                pos = len(model)
            else:
                rel = model[pos:].find(seg)
                if rel < 0:
                    return False
                pos += rel + len(seg)
        return True


class FailoverSelector:
    """
    借鉴 CodexPlusPlus RelayRotationSelector：
      failover     — 全局指针，成功不动，失败才移到下一个成员
      conversation — 每个会话独立绑定，同一会话不漂移
      round_robin  — 每次请求轮询（兼容旧行为）
    """

    def __init__(self, config: AppConfig, routes: List[RouteRule]):
        self.config = config
        self.routes = routes
        self._failover_index: int = 0
        self._rr_index: int = 0
        self._conversation_bound: Dict[str, int] = {}

    def _active_providers(self) -> List[Provider]:
        return [p for p in self.config.providers if p.enabled]

    def select(
        self,
        strategy: str,
        conversation_id: Optional[str],
        model: Optional[str],
    ) -> Tuple[Provider, str]:
        providers = self._active_providers()
        if not providers:
            raise RuntimeError("没有可用的 API 提供商")

        # 1. model 路由匹配（最高优先级）
        route_result = self._try_route(providers, model)
        if route_result is not None:
            return route_result

        # 2. 策略选择
        if strategy == "failover":
            provider = providers[self._failover_index]
            return provider, "failover"

        if strategy == "conversation":
            cid = conversation_id or "default"
            if cid not in self._conversation_bound:
                self._conversation_bound[cid] = self._failover_index
            idx = self._conversation_bound[cid] % len(providers)
            return providers[idx], "conversation"

        # round_robin（默认）
        idx = self._rr_index % len(providers)
        self._rr_index = (idx + 1) % len(providers)
        return providers[idx], "round_robin"

    def on_success(self, strategy: str, conversation_id: Optional[str]):
        pass

    def on_failure(self, strategy: str, conversation_id: Optional[str]):
        providers = self._active_providers()
        if not providers:
            return
        if strategy == "failover":
            self._failover_index = (self._failover_index + 1) % len(providers)
        elif strategy == "conversation" and conversation_id:
            idx = (self._conversation_bound.get(conversation_id, 0) + 1) % len(providers)
            self._conversation_bound[conversation_id] = idx
        else:
            self._rr_index = (self._rr_index + 1) % len(providers)

    def _try_route(
        self, providers: List[Provider], model: Optional[str]
    ) -> Optional[Tuple[Provider, str]]:
        if not self.routes or not model:
            return None
        sorted_routes = sorted(self.routes, key=lambda r: r.priority, reverse=True)
        for route in sorted_routes:
            if route.matches(model):
                for p in providers:
                    if p.name == route.provider_name:
                        logger.info(f"[route] model={model} -> {route.provider_name} (p={route.priority})")
                        return p, "route"
        return None

    def get_status(self) -> List[Dict]:
        return [
            {
                "name": p.name,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "consecutive_failures": p.consecutive_failures,
                "last_status_code": p.last_status_code,
                "last_error": p.last_error,
                "last_checked_at": p.last_checked_at,
            }
            for p in self.config.providers
        ]


class FailoverClient:
    """API 聚合故障切换客户端（对外门面）"""

    def __init__(self, config: AppConfig):
        self.config = config
        # 将配置中的路由规则转为本地 RouteRule 实例
        routes = []
        for r in (config.routes or []):
            routes.append(RouteRule(r.pattern, r.provider_name, r.priority))
        self.selector = FailoverSelector(config, routes)

    async def forward_request(
        self,
        method: str,
        path: str,
        headers: dict,
        body,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> httpx.Response:
        strategy = self.config.settings.failover_strategy
        providers = [p for p in self.config.providers if p.enabled]
        if not providers:
            raise RuntimeError("没有可用的 API 提供商")

        provider, reason = self.selector.select(strategy, conversation_id, model)
        logger.info(f"[{provider.name}] 选中(reason={reason}) 请求 {path} model={model}")

        url = provider.base_url.rstrip("/") + path
        req_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "transfer-encoding")}
        req_headers["authorization"] = f"Bearer {provider.api_key}"
        req_headers.pop("content-length", None)
        req_headers.pop("transfer-encoding", None)

        timeout = httpx.Timeout(timeout=provider.timeout or self.config.settings.request_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.send(
                    request=client.build_request(method, url, headers=req_headers, content=body),
                )
                if resp.status_code < 500:
                    provider.consecutive_failures = 0
                    provider.last_status_code = resp.status_code
                    provider.last_checked_at = time.time()
                    self.selector.on_success(strategy, conversation_id)
                    logger.info(f"[{provider.name}] 成功 ({resp.status_code})")
                    return resp
                else:
                    raise httpx.HTTPStatusError(
                        f"上游返回 {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
        except Exception as e:
            provider.consecutive_failures += 1
            provider.last_error = str(e)[:200]
            provider.last_checked_at = time.time()
            self.selector.on_failure(strategy, conversation_id)
            logger.warning(f"[{provider.name}] 失败: {e}")
            if reason != "route":
                next_provider, next_reason = self.selector.select(strategy, conversation_id, model)
                if next_provider.name != provider.name:
                    logger.info(f"[{next_provider.name}] 故障切换，重试 {path}")
                    return await self._retry_with(next_provider, method, path, headers, body)
            raise RuntimeError(f"提供商 [{provider.name}] 请求失败: {e}")

    async def _retry_with(self, provider: Provider, method: str, path: str,
                          headers: dict, body) -> httpx.Response:
        url = provider.base_url.rstrip("/") + path
        req_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "transfer-encoding")}
        req_headers["authorization"] = f"Bearer {provider.api_key}"
        req_headers.pop("content-length", None)
        req_headers.pop("transfer-encoding", None)
        timeout = httpx.Timeout(timeout=provider.timeout or self.config.settings.request_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.send(
                request=client.build_request(method, url, headers=req_headers, content=body),
            )
            if resp.status_code < 500:
                provider.consecutive_failures = 0
                provider.last_status_code = resp.status_code
                provider.last_checked_at = time.time()
                logger.info(f"[{provider.name}] 切换后成功 ({resp.status_code})")
                return resp
            raise httpx.HTTPStatusError(
                f"上游返回 {resp.status_code}",
                request=resp.request,
                response=resp,
            )

    def get_status(self) -> List[Dict]:
        return self.selector.get_status()