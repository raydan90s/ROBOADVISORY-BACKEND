# Robo-Advisory API

Backend del asesor financiero automatizado (hackathon — track Robo-Advisory Financiero).
FastAPI + Supabase (PostgreSQL) + un agente de IA sobre Gemini/LangGraph.

El usuario responde un test de riesgo desde la app (Expo/React Native), el backend
calcula su puntaje, lo clasifica en un perfil y un agente de IA le propone un
portafolio de inversión.

---

## Cómo levantar el server

### 1. Entorno virtual e instalación

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # si PowerShell lo bloquea: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install -r requirements.txt
```

### 2. Configurar el `.env`

Copia `.env.example` a `.env` y llénalo:

| Variable | De dónde sale |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_KEY` | La **service_role** key, *no* la `anon`. El backend necesita saltarse RLS. |
| `GEMINI_API_KEY` | Google AI Studio |
| `GEMINI_MODEL` | `gemini-2.0-flash` está bien para el hackathon |
| `CORS_ORIGINS` | `*` mientras desarrollas; dominios reales en producción |

La `service_role` key da acceso total a la base de datos: vive solo en el backend
y nunca se commitea (`.env` está en `.gitignore`).

### 3. Crear la tabla en Supabase

Pega el contenido de `schema.sql` en el SQL Editor de Supabase y ejecútalo. Crea la
tabla `investors`.

### 4. Correr

```powershell
uvicorn src.main:app --reload
```

- Docs interactivas: <http://127.0.0.1:8000/docs> — desde ahí puedes probar los
  endpoints sin escribir código.
- Healthcheck: <http://127.0.0.1:8000/health>

**Para probar desde un celular físico con Expo:** `localhost` apunta al teléfono, no
a tu PC. Levanta con `--host 0.0.0.0` y apunta la app a la IP local de tu máquina
(`http://192.168.x.x:8000`).

---

## Cómo funciona

### Flujo completo

1. La app envía las respuestas del test de riesgo a `POST /api/investor/profile`.
2. El **controller** calcula el `puntaje_riesgo` (0-100) y lo clasifica en un
   `perfil_riesgo` (`conservador` / `moderado` / `agresivo`). El puntaje se calcula
   en el backend, nunca lo manda el cliente.
3. El perfil se guarda en Supabase con `estado_propuesta = "pendiente"`.
4. La app pide `GET /api/investor/{id}/portfolio`.
5. El controller lee el perfil y se lo pasa al **agente de IA**, que devuelve la
   propuesta de portafolio (allocations + resumen en lenguaje natural).
6. El estado pasa a `lista` y la propuesta vuelve a la app.

### Estructura

```
src/
├── config/       settings.py (variables de entorno) · database.py (cliente Supabase)
├── models/       esquemas Pydantic: Investor, PortfolioProposal, AssetAllocation
├── controllers/  lógica de negocio: scoring de riesgo, lectura/escritura en Supabase
├── routes/       APIRouters de FastAPI — solo I/O HTTP, sin lógica
├── services/     el agente de IA (Gemini + LangGraph)
└── main.py       arranque de la app, CORS, registro de routers
```

La regla es que **las rutas no saben de Supabase y el controller no sabe de HTTP**
(salvo para lanzar `HTTPException`). Si cambias de base de datos, tocas `config/` y
`controllers/`; las rutas y los modelos quedan igual.

### Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/investor/profile` | Guarda el perfil y calcula el puntaje de riesgo |
| `GET` | `/api/investor/{id}` | Devuelve el perfil |
| `GET` | `/api/investor/{id}/portfolio` | Devuelve la propuesta de portafolio |
| `GET` | `/health` | Healthcheck |

Ejemplo de body para `POST /api/investor/profile`:

```json
{
  "nombre": "Ana Torres",
  "email": "ana@example.com",
  "edad": 29,
  "horizonte_anios": 10,
  "monto_inicial": 5000,
  "respuestas_riesgo": {
    "pregunta_1": 4,
    "pregunta_2": 5,
    "pregunta_3": 3,
    "pregunta_4": 4,
    "pregunta_5": 5
  }
}
```

---

## Dónde va la lógica dura del hackathon

Dos archivos concentran todo lo que hay que construir. El resto es plomería.

### 1. Reglas del test de riesgo → `src/controllers/investor_controller.py`

Arriba del archivo están `PESOS_PREGUNTAS`, `VALOR_MAX_RESPUESTA` y los umbrales
`UMBRAL_CONSERVADOR` / `UMBRAL_MODERADO`. La fórmula actual es un promedio ponderado
normalizado a 0-100 en `calcular_puntaje_riesgo()`. Si el reto define un scoring
propio (penalizaciones cruzadas, descalificadores, etc.), ese es el único lugar que
hay que tocar — todo lo demás consume el resultado.

Las claves de `PESOS_PREGUNTAS` deben coincidir con las que manda el frontend en
`respuestas_riesgo`.

### 2. El agente de IA → `src/services/ai_agent.py`

Hoy `generate_portfolio_proposal(investor) -> PortfolioProposal` es un **mock**:
devuelve un portafolio de referencia fijo según el perfil (`PORTAFOLIOS_BASE`).

Ahí va la máquina de estados de LangGraph. El plan sugerido, documentado en el
docstring de la función:

1. `analizar_perfil` — interpreta puntaje y contexto del usuario
2. `buscar_mercado` — tool call: precios, noticias, tasas (opcional)
3. `construir_cartera` — Gemini con structured output → `list[AssetAllocation]`
4. `validar` — que los pesos sumen 100 y respeten el perfil; si falla, loop de vuelta al 3
5. `explicar` — redacta el `resumen_ia` en lenguaje simple para la app

**Mantén la firma `Investor -> PortfolioProposal`** y podrás reescribir el interior
del archivo entero sin tocar controllers ni rutas.

### Deuda técnica conocida

La propuesta se **regenera en cada GET**. Con el mock da igual, pero cuando el agente
sea real (lento y con costo por token) hay que persistirla: crea una tabla
`portfolios` y devuélvela de caché si `estado_propuesta == "lista"`.
