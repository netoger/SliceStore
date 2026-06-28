# service/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from database import get_pool, close_pool
from storage import ensure_bucket
from routers import slices, versions, search, gcode


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    await ensure_bucket()
    print("[SliceStore] Сервис запущен. БД и хранилище подключены.")
    yield
    await close_pool()
    print("[SliceStore] Сервис остановлен.")


app = FastAPI(
    title="SliceStore API",
    description="Сервис хранения данных слайсинга и штриховки для 3D-печати",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router)
app.include_router(gcode.router)
app.include_router(slices.router)
app.include_router(versions.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "slicestore"}
