"""APRIL Moxie HTTP boundary; run one worker on port 5053."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings
from app.core.service import AgentService
from app.router.moxie import router


class RuntimeMiddleware:
    def __init__(self, app, service):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        with self.service.scope.activate():
            if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                if not self.service.started or self.service.stopping:
                    await JSONResponse({"detail": "Agent is not accepting work"}, status_code=503)(scope, receive, send)
                    return
            await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, *, service: AgentService | None = None) -> FastAPI:
    service = service if service is not None else AgentService(settings)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="APRIL Moxie Agent", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(RuntimeMiddleware, service=service)
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "april-robots-agent-moxie"}

    @app.get("/status")
    async def status():
        return service.status()

    @app.get("/ready")
    async def ready():
        body = service.status()
        return JSONResponse(body, status_code=200 if body["ready"] else 503)

    return app


app = create_app()
