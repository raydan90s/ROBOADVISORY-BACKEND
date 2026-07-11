"""Punto de entrada de la API. Levanta con:  uvicorn src.main:app --reload"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, HTTPException, status
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware

from src.config.database import fetch_one, get_pool
from src.config.settings import settings
from src.routes.advisor_routes import router as advisor_router
from src.routes.audit_routes import router as audit_router
from src.routes.auth_routes import router as auth_router
from src.routes.investor_routes import router as investor_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    pool = get_pool()  # abre el pool al arrancar: si el .env está mal, falla aquí y no en el primer request
    yield
    pool.close()


app = FastAPI(
    title="Robo-Advisory API",
    description="Backend del asesor financiero automatizado.",
    version="0.1.0",
    lifespan=lifespan,
)

# --- CORS: necesario para que Expo / React Native Web consuma la API ---
# En producción reemplaza "*" por la lista real de orígenes (variable CORS_ORIGINS).
# Ojo: allow_credentials=True es incompatible con allow_origins=["*"] en navegadores.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(investor_router)
app.include_router(advisor_router)
app.include_router(audit_router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Hace un SELECT real: si responde, la conexión a la base está viva."""
    try:
        fetch_one("select 1 as ok")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Sin conexión a la base de datos: {exc}",
        ) from exc

    return {
        "status": "ok",
        "env": settings.APP_ENV,
        "database": "conexión exitosa a la base de datos",
    }


# Registra aquí los routers nuevos (portafolios, mercado, chat del asesor...):
# from src.routes.market_routes import router as market_router
# app.include_router(market_router)
