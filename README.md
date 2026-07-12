# Robo-Advisory API

Backend del asesor financiero automatizado (hackathon — track Robo-Advisory
Financiero). FastAPI + Postgres directo sobre Supabase + un agente conversacional
sobre LangGraph (Gemini / OpenAI / Anthropic, intercambiable) + un wrapper cacheado
de Alpha Vantage para mercados externos.

El usuario responde un test de riesgo desde la app (Expo/React Native). El backend
calcula su puntaje **en la base** (nunca en el cliente ni en el LLM), lo clasifica en
un perfil de riesgo y arma una propuesta de portafolio con el catálogo del banco.
Puede repartir su capital en varias **subcuentas** (una por objetivo), conversar con
un asistente sobre sus datos o sobre mercados externos, y un asesor humano revisa
cada propuesta antes de que sea definitiva.

---

## Cómo levantar el server

### 1. Entorno virtual e instalación

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # si PowerShell lo bloquea: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install -r requirements.txt
```

Requiere Python 3.12 (los paquetes con extensión nativa — `psycopg[binary]`,
`pydantic-core` — no publican wheels para versiones más nuevas en Windows, y
compilarlos exige Rust + MSVC).

### 2. Configurar el `.env`

Copia `.env.example` a `.env` y llénalo:

| Variable | De dónde sale |
|---|---|
| `DATABASE_URL` | Supabase → Connect → **Session pooler** (no la "Direct connection": esta red suele ser IPv4 y esa solo publica IPv6) |
| `JWT_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `AI_PROVIDER` | `google` \| `openai` \| `anthropic` — cuál usa el agente. Cambiar de modelo es cambiar solo esta variable (ver [`llm_provider.py`](src/services/llm_provider.py)) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Google AI Studio. Ojo con la cuota por modelo: ver el comentario en [`settings.py`](src/config/settings.py) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Opcionales; solo si `AI_PROVIDER` los usa |
| `ALPHA_VANTAGE_API_KEY` | [alphavantage.co](https://www.alphavantage.co/support/#api-key) (free tier: 25 req/día). Sin key, el ticker y el chat de mercados sirven cotizaciones simuladas — nunca se rompen |
| `CORS_ORIGINS` | `*` mientras desarrollas; dominios reales en producción |

Entramos a Postgres directo como el rol `postgres` (bypassea el RLS de
`schema.sql`), no por la API REST de Supabase — por eso no hay `SUPABASE_KEY`.

### 3. Crear el esquema en Supabase

Pega `schema.sql` en el SQL Editor de Supabase y ejecútalo (crea todas las tablas
y vistas). Después aplica las migraciones en orden, la más reciente es
[`migrations/002_subcuentas.sql`](migrations/002_subcuentas.sql) (agrega
`profiles.total_capital`, `profiling_sessions.subaccount_name` y el trigger que
valida el capital de las subcuentas). `seed.sql` carga el catálogo de instrumentos,
las reglas de scoring y las cuentas demo.

### 4. Correr

```powershell
uvicorn src.main:app --reload
```

- Docs interactivas: <http://127.0.0.1:8000/docs>
- Healthcheck: <http://127.0.0.1:8000/health> — hace un `select 1` real: si responde
  200, la conexión a la base está viva.

**Para probar desde un celular físico con Expo:** `localhost` apunta al teléfono, no
a tu PC. Levanta con `--host 0.0.0.0` y apunta la app (`EXPO_PUBLIC_API_BASE_URL`) a
la IP local de tu máquina (`http://192.168.x.x:8000`).

### 5. Tests

```powershell
pytest -q
```

Corren contra la base real (la que sembró `seed.sql`): validar el motor determinista
contra un mock no probaría nada, porque el motor *es* la base. Sin `DATABASE_URL` se
saltan en vez de fallar (ver `tests/conftest.py`).

---

## Arquitectura

```
src/
├── config/       settings.py (env vars, una sola vez) · database.py (pool de psycopg)
├── models/       esquemas Pydantic — el contrato de cada endpoint
├── controllers/  lógica de negocio: SQL, reglas, orquestación
├── routes/       APIRouters de FastAPI — solo I/O HTTP, sin lógica
├── services/     el motor de IA: ai_agent, agent_graph (LangGraph), guardrails,
│                 llm_provider, market_data (Alpha Vantage)
└── main.py       arranque, CORS, registro de routers
```

Regla del proyecto: **las rutas no saben de SQL y los controllers no saben de HTTP**
(salvo para lanzar `HTTPException`). Los `services/` no saben de ninguno de los dos:
`ai_agent`/`agent_graph` reciben datos ya armados y devuelven texto.

### El principio anti-alucinación (criterio de evaluación #3)

Ningún número que el LLM escribe nace en el LLM. `scoring_rules`,
`profile_thresholds` y `allocation_template_items` calculan puntaje, perfil y
porcentajes en Postgres; el LLM solo los **redacta**. Cada texto generado se valida
contra un `ContextoPermitido` (`services/guardrails.py`) — el conjunto cerrado de
números, productos, emisores y calificaciones que ese texto tiene derecho a citar.
Si el modelo inventa algo, el texto se **descarta** (no se corrige), se reintenta una
vez con los motivos del rechazo, y si vuelve a fallar se cae a una plantilla
determinista construida directamente desde los datos — nunca se le muestra al
usuario un número que nadie de la base respalda.

---

## Funcionalidades

### 1. Perfilamiento y propuesta (HU1 / HU2)

El cliente responde el cuestionario (`GET /questions` lo sirve la base, no está
hardcodeado en el front). `POST /profile` puntúa las respuestas contra
`scoring_rules`, asigna un `risk_profile` según `profile_thresholds`, y
`GET /{id}/portfolio` genera (la primera vez) o devuelve la propuesta: la plantilla
de asignación del perfil (`allocation_templates`) materializada con los montos en
USD que calcula Postgres, más una explicación en lenguaje natural.

### 2. Subcuentas y capital

Una subcuenta **es** una sesión de perfilamiento con nombre propio: el cliente
declara un capital total (`POST /capital`) y lo reparte en N subcuentas
(`POST /profile` con `subaccount_name`), cada una con su propio cuestionario, perfil
y propuesta. La regla de oro — *una subcuenta no puede superar el capital sin
asignar* — se aplica **en un trigger de Postgres**
(`fn_valida_capital_subcuenta`, migración 002), no en Python: bloquea la fila de
`profiles` con `for update`, así que dos subcuentas creadas a la vez para el mismo
cliente se serializan en la base en vez de colarse las dos por una condición de
carrera. `GET /{id}/subaccounts` devuelve el reparto completo (capital total,
asignado, sin asignar) — esas tres cifras las suma SQL, nunca el front.

### 3. Revisión del asesor (HU3)

Todo el router `/api/advisor/*` exige rol `advisor`. La cola (`GET /queue`) lista lo
que espera decisión; el detalle (`GET /proposals/{id}`) trae banderas deterministas
(monto bajo el mínimo de acceso, puntaje al borde del umbral) para que el asesor
decida con contexto, no a ciegas. `POST /proposals/{id}/review` aprueba, edita o
rechaza — la decisión queda con fecha, versión de reglas y responsable
(`advisor_reviews`), y una propuesta ya revisada no se puede revisar dos veces
(`for update` + chequeo de estado → 409).

### 4. Agente conversacional (LangGraph)

`POST /api/agent/chat` corre un grafo de estados corto y auditable
(`services/agent_graph.py`) que clasifica cada mensaje en una de tres rutas, más un
rechazo:

```
entrada → router ─┬─(A: bancario)→ qa ──────┐
                   ├─(B: mixto)  → mixto ────┤
                   ├─(C: externo)→ mercado ──┼→ guardrail ─┬─(ok)──────────→ FIN
                   │                         │             ├─(falla,1 vez)→ (misma ruta)
                   └─(fuera de alcance)───────────────────→│             └─(reincide)───→ fallback → FIN
                                                            (refuse) ──────────────────────────────→ FIN
```

- **Ruta A (bancario)** — usa exclusivamente los datos del inversionista que salen
  de Postgres: su perfil, su propuesta, el catálogo del banco marcando qué puede o
  no tomar, y sus otras subcuentas (para comparar).
- **Ruta B (mixto)** — A + cotizaciones de Alpha Vantage, para preguntas que
  comparan el banco con mercados externos ("¿cómo se compara mi depósito con el
  bitcoin?").
- **Ruta C (externo)** — 100% Alpha Vantage (acciones, forex, cripto, índices). El
  `ContextoPermitido` de esta ruta es **cero** contexto del banco: si el modelo
  intenta mencionar un producto del catálogo, el guardarraíl lo rechaza igual que
  rechazaría un número inventado.
- **Rechazo** — predicciones de mercado ("¿va a subir el bitcoin?"), órdenes de
  compra/venta y tareas ajenas (traducir, programar, etc.) siguen bloqueadas en las
  tres rutas: dar una cotización actual no es lo mismo que predecirla.

**Contención**: las rutas B y C nunca insertan en `proposals` ni `proposal_items` —
solo leen (Alpha Vantage + el contexto ya cargado del banco) y devuelven texto. La
única escritura que hace cualquier ruta es el historial de chat
(`llm_interactions`, con `thread_id` = la sesión), igual para las tres. El disclaimer
*"simulación educativa... NO están en el catálogo del banco"* es obligatorio en B y
C — va en el prompt, y si el modelo no lo incluye se anexa igual antes de responder.

El proveedor de IA (Gemini / OpenAI / Anthropic) se elige con `AI_PROVIDER` en el
`.env`, o por request con el campo `provider` — `GET /api/agent/providers` expone
el catálogo (sin exponer las keys) para el selector del front.

### 5. Mercados externos (Alpha Vantage)

`GET /api/market/quotes?symbols=BTCUSD,XAUUSD,...` (`services/market_data.py`) es
el wrapper que alimenta el ticker del front y las Rutas B/C del agente:

- **Caché en memoria de 1 hora** (`cachetools.TTLCache`) — la cuota gratuita de
  Alpha Vantage es de 25 requests/día; sin caché, el ticker (refrescando cada
  ~45s) y cada turno de chat la agotarían en minutos.
- **Respaldo simulado**: si Alpha Vantage responde `Note`/`Information`/
  `Error Message` (rate limit o símbolo no soportado en el free tier — es el caso
  conocido de `JPN225`) o la llamada falla, se sirve una cotización de referencia
  fija. El ticker y el chat nunca se quedan en blanco ni muestran un error crudo.
- Cubre los 5 símbolos del ticker (`BTCUSD`, `XAUUSD`, `JPN225`, `SPY`, `EURUSD`),
  extensible agregando una entrada a `_SYMBOL_CONFIG`.

### 6. Comparador de tasas

`GET /api/catalog/rates` — lectura pura sobre `instruments`/`institutions` con la
elegibilidad del perfil de quien consulta (misma regla que
`v_institution_eligibility`, así el comparador nunca puede contradecir a los tests
estrella). Con `?monto=` y `?plazo_dias=`, Postgres devuelve además el interés
estimado por producto.

### 7. Auditoría

`GET /api/audit` — el timeline completo (`v_audit_timeline`): quién hizo qué, cuándo
y con qué versión de reglas. Alimenta la pantalla de auditoría del asesor.

---

## Endpoints

| Método | Ruta | Rol | Qué hace |
|---|---|---|---|
| `POST` | `/api/auth/register` | público | Alta de un inversionista (nunca crea asesores) |
| `POST` | `/api/auth/login` | público | Devuelve el JWT |
| `GET` | `/api/auth/me` | autenticado | El usuario del token |
| `GET` | `/api/investor/questions` | público | El cuestionario (preguntas + opciones) |
| `POST` | `/api/investor/profile` | investor | Perfila (y opcionalmente crea una subcuenta) |
| `POST` | `/api/investor/capital` | investor | Fija el capital total y devuelve el reparto |
| `GET` | `/api/investor/{id}/subaccounts` | dueño/asesor | Subcuentas + capital repartido |
| `GET` | `/api/investor/{id}/portfolio` | dueño/asesor | La propuesta (la genera la primera vez). `?session_id=` elige la subcuenta |
| `GET` | `/api/investor/{id}/breakdown` | dueño/asesor | Desglose respuesta → puntos → umbral |
| `GET` | `/api/investor/{id}` | dueño/asesor | El perfil |
| `GET` | `/api/advisor/queue` | advisor | Propuestas pendientes de revisión |
| `GET` | `/api/advisor/proposals/{id}` | advisor | Detalle + banderas deterministas |
| `POST` | `/api/advisor/proposals/{id}/review` | advisor | Aprueba / edita / rechaza |
| `GET` | `/api/agent/providers` | autenticado | Proveedores de IA disponibles |
| `POST` | `/api/agent/chat` | autenticado | Un turno del asistente (3 rutas, ver arriba) |
| `GET` | `/api/market/quotes` | autenticado | Cotizaciones externas cacheadas |
| `GET` | `/api/catalog/rates` | autenticado | Comparador de tasas con elegibilidad |
| `GET` | `/api/audit` | advisor | Timeline de auditoría |
| `GET` | `/health` | público | Healthcheck (consulta real a la base) |

> Nota: el front (`RoboAdvisorApp`) también llama a `POST /api/agent/simulador` y
> `PUT /api/investor/proposals/{id}/allocation`, que **no existen todavía** en este
> backend — quedaron en el trabajo de otra rama sin mergear. El Comparador/Simulador
> del front fallará hasta que se implementen.

---

## Despliegue

- **Backend (Render)**: blueprint en [`render.yaml`](render.yaml) — `pip install -r
  requirements.txt` como build, `uvicorn src.main:app --host 0.0.0.0 --port $PORT`
  como start, healthcheck en `/health`. Variables secretas a setear en el dashboard
  (o vía API): `DATABASE_URL`, `JWT_SECRET`, `GEMINI_API_KEY`, `ALPHA_VANTAGE_API_KEY`,
  `CORS_ORIGINS`. `autoDeploy` está en `yes` sobre la rama `MiguelsBackend`.
- **Frontend (Vercel)**: ver el README de `RoboAdvisorApp`.

---

## Estructura de las migraciones

`schema.sql` es la base completa (todas las tablas, vistas y tipos ENUM).
`migrations/` son los cambios incrementales aplicados **después** de sembrar esa
base — hoy solo [`002_subcuentas.sql`](migrations/002_subcuentas.sql). Al clonar el
proyecto de cero, `schema.sql` + `seed.sql` + las migraciones en orden dejan la base
al día.
