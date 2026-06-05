from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routers import admin, videos, mistakes, candidates, assets, jobs

app = FastAPI(
    title="Kitchen Image Pipeline MVP",
    description="Lightweight MVP for kitchen design mistakes image pipeline",
    version="0.3",
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
