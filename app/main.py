from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routers import admin

app = FastAPI(
    title="Kitchen Image Pipeline MVP",
    description="Lightweight MVP for kitchen design mistakes image pipeline",
    version="0.3",
)

# app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(admin.router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
