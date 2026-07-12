"""Feed de noticias financieras. Solo I/O HTTP: la lógica vive en feed_service.

Autenticado como el resto de la app; no hay dueño que validar (todos los usuarios
ven el mismo feed, igual que el ticker de mercados).
"""

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.dependencies.auth import get_current_user
from src.models.auth import CurrentUser
from src.models.feed import FeedResponse
from src.services.feed_service import TEMA_DEFAULT, TEMAS, obtener_feed

router = APIRouter(prefix="/api/feed", tags=["feed"])


@router.get(
    "",
    response_model=FeedResponse,
    summary="Noticias financieras por tema (gnews.io, cacheadas 1h, con respaldo)",
)
async def get_feed(
    tema: str = Query(
        TEMA_DEFAULT,
        description=f"Uno de: {', '.join(TEMAS)}.",
    ),
    _usuario: CurrentUser = Depends(get_current_user),
) -> FeedResponse:
    tema = tema.strip().lower()
    if tema not in TEMAS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tema desconocido: '{tema}'. Usa uno de: {', '.join(TEMAS)}.",
        )
    return await obtener_feed(tema)
