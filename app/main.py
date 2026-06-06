import asyncio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from app.routers import admin, videos, mistakes, candidates, assets, jobs
from app.services.job_runner import background_worker

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем фоновый воркер
    worker_task = asyncio.create_task(background_worker())
    yield
    # Очистка при завершении
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="Kitchen Image Pipeline MVP",
    description="Lightweight MVP for kitchen design mistakes image pipeline",
    version="0.4",
    lifespan=lifespan
)

# app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(admin.router)
app.include_router(videos.router, prefix="/api")
app.include_router(mistakes.router, prefix="/api")
app.include_router(candidates.router, prefix="/api")
app.include_router(assets.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")

@app.get("/health")
async def health_check():
    return {"status": "ok"}
