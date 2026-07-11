"""Punto de entrada de la API. Levanta con:  uvicorn src.main:app --reload"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.settings import settings
from src.routes.investor_routes import router as investor_router

app = FastAPI(
    title="Robo-Advisory API",
    description="Backend del asesor financiero automatizado.",
    version="0.1.0",
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

app.include_router(investor_router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}


# Registra aquí los routers nuevos (portafolios, mercado, chat del asesor...):
# from src.routes.market_routes import router as market_router
# app.include_router(market_router)
