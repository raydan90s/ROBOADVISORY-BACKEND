"""Modelos del feed de noticias (GET /api/feed)."""

from pydantic import BaseModel, Field


class NoticiaFeed(BaseModel):
    """Una noticia citada: titular + fuente + fecha + link. La app no redacta noticias."""

    titulo: str
    descripcion: str | None = None
    url: str
    # URL de la imagen del artículo (la da GNews). None en el respaldo: el front
    # pinta entonces el visual del tema, nunca una imagen que no sea de la noticia.
    imagen: str | None = None
    fuente: str
    # ISO 8601. None en el respaldo: una noticia de referencia no finge ser de hoy.
    fecha: str | None = None
    tema: str


class FeedResponse(BaseModel):
    tema: str
    # "gnews" = en vivo · "respaldo" = titulares de referencia (sin key o API caída).
    # El front lo usa para avisar "modo sin conexión" en vez de fingir tiempo real.
    fuente_datos: str
    actualizado_en: str
    noticias: list[NoticiaFeed] = Field(default_factory=list)
