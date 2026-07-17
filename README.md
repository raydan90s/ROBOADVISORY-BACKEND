# Brokeate — Backend (Robo-Advisory API)

Backend del asesor financiero automatizado (Hackathon de Agentes Financieros IA —
Track 3: Robo-Advisory). FastAPI + Postgres directo sobre Supabase + un agente
conversacional sobre LangGraph (Gemini / OpenAI / Anthropic, intercambiable) + un
wrapper cacheado de Alpha Vantage para mercados externos y de GNews para el feed.

El usuario responde un test de riesgo desde la app o la web. El backend calcula su
puntaje **en la base** (nunca en el cliente ni en el LLM), lo clasifica en un perfil de
riesgo y arma una propuesta de portafolio con el catálogo del banco. Puede repartir su
capital en varias **subcuentas** (una por objetivo), conversar con un asistente sobre
sus datos o sobre mercados externos (también por WhatsApp), y un asesor humano revisa
cada propuesta antes de que sea definitiva. Una vez firmada, el cliente la **cursa** con
un botón: la cartera se convierte en una instrucción por banco, y el banco paga una prima
por cada inversión intermediada — ver [El modelo de negocio](#el-modelo-de-negocio-en-la-base).

## Los tres repositorios

| Repo | Qué es | Despliegue |
|---|---|---|
| **[BROKEATE-APP](https://github.com/raydan90s/BROKEATE-APP)** | App móvil (Expo / React Native). Es además el paraguas: trae este repo y el web como submódulos. | [APK](https://expo.dev/accounts/alatacompany/projects/RoboAdvisorApp/builds/3759fec8-8b58-4de2-bf78-8dfe21d00e53) |
| **[BROKEATE-BACKEND](https://github.com/raydan90s/BROKEATE-BACKEND)** (este) | Esta API. | AWS |
| **[BROKEATE-WEB](https://github.com/raydan90s/BROKEATE-WEB)** | Frontend web (Vite + React DOM). | [Vercel](https://brokeate-web.vercel.app) |

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
| `GNEWS_API_KEY` | [gnews.io](https://gnews.io) (free tier: 100 req/día). Alimenta el feed de noticias, cacheado 1 h y con respaldo |
| `TWILIO_AUTH_TOKEN`, `TWILIO_WEBHOOK_URL` | Bot de WhatsApp. El webhook es público (Twilio no manda JWT): lo que lo autentica es la firma `X-Twilio-Signature`, y se calcula sobre `TWILIO_WEBHOOK_URL`, que debe coincidir **carácter por carácter** con la URL de la consola de Twilio |
| `SMTP_*` | Gmail, para verificar el correo del registro y resetear contraseñas. `SMTP_PASSWORD` es una *Contraseña de aplicación* de 16 caracteres, no la de la cuenta. Vacío + `APP_ENV=development` → el código no se envía, se imprime en el log |
| `CORS_ORIGINS` | `*` mientras desarrollas; dominios reales en producción |

Entramos a Postgres directo como el rol `postgres` (bypassea el RLS de
`schema.sql`), no por la API REST de Supabase — por eso no hay `SUPABASE_KEY`.

### 3. Crear el esquema en Supabase

Pega `schema.sql` en el SQL Editor de Supabase y ejecútalo (crea todas las tablas
y vistas). Después aplica las migraciones **en orden**, la más reciente es
[`migrations/005_convenios_ordenes.sql`](migrations/005_convenios_ordenes.sql) (convenios
por institución, la política de comisión y las órdenes de inversión con sus dos triggers).
Recién ahí corre `seed.sql`, que carga el catálogo de instrumentos, las reglas de scoring,
los convenios, la tasa de comisión y las cuentas demo — necesita las columnas que agregan
las migraciones, así que **el orden importa**: `schema.sql` → `migrations/*` → `seed.sql`.

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

### El modelo de negocio, en la base

Brokeate es el **intermediario** entre el inversionista y el banco. La app es gratis para
el cliente; quien paga es el banco, una prima por cada inversión que entra por acá. El
asesor es de Brokeate y es **uno solo para todos los bancos** — no hay un asesor por
institución: por eso puede comparar entre emisores sin representar a ninguno.

Ese modelo no vive en una diapositiva: vive en `migrations/005_convenios_ordenes.sql`, y
son cuatro reglas que la base **aplica**, no que nosotros prometamos.

| La regla | Cómo se garantiza | Qué pasa si se intenta violar |
|---|---|---|
| Una propuesta no se invierte hasta que un asesor la firme | Trigger `fn_valida_orden_firmada` (con `for update` sobre `proposals`, serializa contra `revisar_propuesta`) | `RAISE EXCEPTION` — aunque el insert venga de un script, no de la API |
| La comisión es la misma para todos los bancos con convenio | `commission_policies` **no tiene columna de institución** + `UNIQUE (rules_version_id)` | No hay dónde escribir una tasa por banco; una segunda tasa viola el UNIQUE |
| No se cursa plata a un banco sin convenio | Trigger `fn_valida_convenio_item` | `RAISE EXCEPTION` con el nombre de la institución |
| Una propuesta se invierte una sola vez | `UNIQUE (investment_orders.proposal_id)` | El controller lo traduce a un 409 con el comprobante |

**Por qué la segunda fila importa tanto.** En el momento en que cobramos por convenio,
"¿me recomiendas al banco que más te paga?" es una pregunta legítima, y "no lo hacemos" no
es una respuesta verificable. Que **no se pueda** sí lo es: la comisión no puede depender
del emisor porque no existe la columna donde escribirlo. A Brokeate le da igual cuál de
los bancos con convenio elija el cliente. Eso lo garantiza el esquema, no nuestra buena fe
— y `test_ordenes.py` falla si alguien agrega esa columna.

Además, ninguna cifra de plata de estas tablas la escribe Python: `investment_orders
.comision_total` y `investment_order_items.comision` son columnas **GENERATED**, derivadas
de `total_amount * comision_bps`. Es el mismo principio de `proposal_items`, llevado hasta
el final.

**El catálogo y el convenio son listas distintas.** `institutions` informa (el comparador
muestra la tasa de cualquiera); `convenio_activo` habilita (solo esas pueden recibir una
orden). Es la respuesta a "¿por qué no me aparece Interactive Brokers, si ahí gano más?":
no porque lo escondamos, sino porque no hay convenio. `institution_type` ('banco' |
'cooperativa' | 'broker_internacional') deja crecer el catálogo a entidades que no son
bancos regulados localmente sin que se confundan con los que sí lo son.

### La ejecución es simulada, y el sistema lo dice

`services/bank_gateway.py` responde como respondería el canal del banco —una referencia
por instrucción— pero **no mueve dinero**: no hay convenio firmado ni credenciales del
cliente en la banca del emisor, y este proyecto no finge que los haya. La honestidad es
estructural: `investment_orders.is_simulated` nace en `true`, viaja al cliente y la app lo
pinta en pantalla.

Lo que **sí** es real es todo lo demás: quién puede cursar una orden, contra qué
instituciones, cuánto se cobra, y quién respondió por ella. El día que exista integración
real, `bank_gateway.cursar()` es la única función que cambia.

Las referencias son deterministas (sembradas con el id de la línea, no con el reloj):
misma orden, misma referencia. Igual que el respaldo de `market_data` — la demo no cambia
de números entre el ensayo y la función.

### La IA sigue sin poder ejecutar

Que ahora exista un botón "Invertir ahora" no le abre ninguna puerta al modelo. Se sostiene
solo, por arquitectura y no por disciplina:

- El **agente** devuelve texto; `agent_graph` no llama endpoints, y sus rutas B/C ni
  siquiera pueden escribir en `proposals`.
- El **bot de WhatsApp** se autentica con la firma de Twilio, no con un JWT de
  inversionista — y `POST /invest` exige `require_role(Rol.INVESTOR)`. Mover plata por
  WhatsApp es imposible porque no hay token que presentar, no porque hayamos decidido que
  no.
- El guardarraíl sigue rechazando el léxico que empuja a ejecutar (`guardrails._PROHIBIDO`).

Lo único que cursa una orden es una persona autenticada tocando un botón, sobre una
propuesta que otra persona firmó.

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

**Forzar la Ruta C**: `AgentChatRequest.symbols` es la señal explícita del botón
"Recomendación de Mercados (IA)" del simulador de mercados del front — si viene,
el router NO clasifica el mensaje: la ruta es C para esos símbolos exactos, sin
depender de que el texto contenga las palabras que el clasificador reconoce.

### 5. Mercados externos (Alpha Vantage)

`services/market_data.py` es el wrapper que alimenta el ticker y el simulador de
mercados del front, y las Rutas B/C del agente. Dos endpoints:

- **`GET /api/market/quotes?symbols=BTCUSD,XAUUSD,...`** — cotización en tiempo
  real (`CURRENCY_EXCHANGE_RATE` para forex/cripto/metales, `GLOBAL_QUOTE` para
  acciones/ETFs).
- **`GET /api/market/history?symbol=&days=`** — serie diaria para gráficos
  (`DIGITAL_CURRENCY_DAILY` para cripto, `FX_DAILY` para forex/metales,
  `TIME_SERIES_DAILY` para acciones), 5 a 100 días.

Ambos comparten las mismas dos reglas:

- **Caché en memoria de 1 hora** (`cachetools.TTLCache`, una caché separada por
  endpoint) — la cuota gratuita de Alpha Vantage es de 25 requests/día; sin caché,
  el ticker (refrescando cada ~45s) y cada turno de chat la agotarían en minutos.
- **Respaldo simulado**: si Alpha Vantage responde `Note`/`Information`/
  `Error Message` (rate limit o símbolo no soportado en el free tier — es el caso
  conocido de `JPN225`) o la llamada falla, se sirve un dato de referencia. Para
  cotizaciones es un valor fijo; para el histórico, una caminata aleatoria
  **determinista** (sembrada con el símbolo, no con el reloj: la misma curva en
  cada request, para que la demo no cambie de forma entre una recarga y otra). El
  ticker, el gráfico y el chat nunca se quedan en blanco ni muestran un error crudo.
- Cubren los 5 símbolos del ticker (`BTCUSD`, `XAUUSD`, `JPN225`, `SPY`, `EURUSD`),
  extensible agregando una entrada a `_SYMBOL_CONFIG` / `_HISTORY_CONFIG`.

### 6. Comparador de tasas

`GET /api/catalog/rates` — lectura pura sobre `instruments`/`institutions` con la
elegibilidad del perfil de quien consulta (misma regla que
`v_institution_eligibility`, así el comparador nunca puede contradecir a los tests
estrella). Con `?monto=` y `?plazo_dias=`, Postgres devuelve además el interés
estimado por producto.

### 7. Auditoría

`GET /api/audit` — el timeline completo (`v_audit_timeline`): quién hizo qué, cuándo
y con qué versión de reglas. Alimenta la pantalla de auditoría del asesor.

### 8. Feed de noticias

`GET /api/feed?tema=` — noticias financieras por tema (`services/feed_service.py`,
sobre gnews.io). Misma disciplina que el wrapper de mercados: caché de 1 hora y
respaldo cuando la cuota gratuita (100 req/día) se agota, para que la pantalla nunca
quede en blanco. Todos los usuarios ven el mismo feed: no hay dueño que validar.

### 9. Bot de WhatsApp (Twilio)

El inversionista vincula su número desde la app (`POST /api/whatsapp/link-code` genera
un código de un solo uso) y luego conversa con el mismo agente por WhatsApp. El webhook
(`POST /api/whatsapp/webhook`) es **público** — Twilio no manda JWT: lo que lo autentica
es la firma `X-Twilio-Signature`, validada contra `TWILIO_AUTH_TOKEN` y la URL exacta de
`TWILIO_WEBHOOK_URL`. El bot corre siempre sobre el proveedor de `WHATSAPP_AI_PROVIDER`
(donde hay cuota), sin depender del selector del front.

---

## Endpoints

| Método | Ruta | Rol | Qué hace |
|---|---|---|---|
| `POST` | `/api/auth/register` | público | Alta de un inversionista (nunca crea asesores) |
| `POST` | `/api/auth/login` | público | Devuelve el JWT |
| `POST` | `/api/auth/verify-email` | público | Confirma el código enviado al correo |
| `POST` | `/api/auth/resend-code` | público | Reenvía el código de verificación |
| `POST` | `/api/auth/forgot-password` | público | Manda el código de reseteo |
| `POST` | `/api/auth/reset-password` | público | Cambia la contraseña con ese código |
| `GET` | `/api/auth/me` | autenticado | El usuario del token |
| `GET` | `/api/investor/questions` | público | El cuestionario (preguntas + opciones) |
| `POST` | `/api/investor/profile` | investor | Perfila (y opcionalmente crea una subcuenta) |
| `POST` | `/api/investor/capital` | investor | Fija el capital total y devuelve el reparto |
| `PUT` | `/api/investor/proposals/{id}/allocation` | investor | El inversionista arma su mezcla: agrega, quita o repondera fondos elegibles |
| `PUT` | `/api/investor/sessions/{id}/profile` | investor | Corrige sus respuestas: se re-puntúa y vuelve a revisión |
| `GET` | `/api/investor/{id}/subaccounts` | dueño/asesor | Subcuentas + capital repartido |
| `GET` | `/api/investor/{id}/portfolio` | dueño/asesor | La propuesta (la genera la primera vez). `?session_id=` elige la subcuenta |
| `GET` | `/api/investor/{id}/breakdown` | dueño/asesor | Desglose respuesta → puntos → umbral |
| `GET` | `/api/investor/{id}` | dueño/asesor | El perfil |
| `POST` | `/api/investor/proposals/{id}/invest` | investor | **«Invertir ahora»**: cursa la propuesta firmada como N instrucciones bancarias (nace `sent`) |
| `POST` | `/api/investor/orders/{id}/confirm` | investor | Acuse del banco: cada línea recibe su referencia. Idempotente |
| `GET` | `/api/investor/orders/{id}` | dueño/asesor | El comprobante |
| `GET` | `/api/investor/proposals/{id}/order` | dueño/asesor | La orden de una propuesta, o `null` si todavía no se invirtió |
| `GET` | `/api/catalog/convenios` | autenticado | Con qué instituciones hay convenio y cuánto cobra Brokeate por intermediar |
| `GET` | `/api/advisor/queue` | advisor | Propuestas pendientes de revisión |
| `GET` | `/api/advisor/proposals/{id}` | advisor | Detalle + banderas deterministas |
| `POST` | `/api/advisor/proposals/{id}/review` | advisor | Aprueba / edita / rechaza |
| `GET` | `/api/advisor/orders` | advisor | Quién acaba de invertir: el aviso que dispara la llamada |
| `GET` | `/api/advisor/commissions` | advisor | Lo intermediado y lo facturado, por asesor |
| `GET` | `/api/agent/providers` | autenticado | Proveedores de IA disponibles |
| `POST` | `/api/agent/chat` | autenticado | Un turno del asistente (3 rutas, ver arriba) |
| `POST` | `/api/agent/simulador` | autenticado | Recomendación sobre la simulación en pantalla: **el motor elige, la IA solo explica** |
| `GET` | `/api/market/quotes` | autenticado | Cotizaciones externas cacheadas |
| `GET` | `/api/market/history` | autenticado | Serie diaria de un símbolo (para gráficos), cacheada |
| `GET` | `/api/catalog/rates` | autenticado | Comparador de tasas con elegibilidad |
| `GET` | `/api/feed` | autenticado | Noticias financieras por tema (GNews, cacheadas 1 h, con respaldo) |
| `GET` | `/api/audit` | advisor | Timeline de auditoría |
| `POST` | `/api/whatsapp/webhook` | público (firmado) | Mensaje entrante de Twilio; lo autentica `X-Twilio-Signature`, no un JWT |
| `POST` | `/api/whatsapp/link-code` | autenticado | Código de un solo uso para vincular el WhatsApp |
| `GET` | `/api/whatsapp/status` | autenticado | ¿Esta cuenta tiene un WhatsApp vinculado? |
| `DELETE` | `/api/whatsapp/link` | autenticado | Desvincula el WhatsApp |
| `GET` | `/health` | público | Healthcheck (consulta real a la base) |

---

## Despliegue

**Backend → AWS.** `uvicorn src.main:app --host 0.0.0.0 --port $PORT`, con `pip install
-r requirements.txt` como build y el healthcheck en `/health`. Todas las variables de
entorno (`DATABASE_URL`, `JWT_SECRET`, las keys de IA, `ALPHA_VANTAGE_API_KEY`,
`GNEWS_API_KEY`, `TWILIO_*`, `SMTP_*`, `CORS_ORIGINS`) se configuran **en el servidor**,
nunca en el repo. En producción `CORS_ORIGINS` lista los dominios reales de la web y la
app, no `*`.

Los clientes ([app](https://github.com/raydan90s/BROKEATE-APP) y
[web](https://github.com/raydan90s/BROKEATE-WEB)) apuntan a esa URL con
`EXPO_PUBLIC_API_BASE_URL` / `VITE_API_BASE_URL`.

> [`render.yaml`](render.yaml) quedó de un despliegue anterior en Render; sigue siendo
> una referencia útil de qué variables hay que setear, pero **el deploy vivo es AWS**.

---

## Estructura de las migraciones

`schema.sql` es la base completa (todas las tablas, vistas y tipos ENUM).
`migrations/` son los cambios incrementales aplicados **después** de crear esa base:

| Migración | Qué agrega |
|---|---|
| [`002_subcuentas.sql`](migrations/002_subcuentas.sql) | `profiles.total_capital`, `profiling_sessions.subaccount_name` y el trigger que valida el capital de las subcuentas |
| [`003_whatsapp.sql`](migrations/003_whatsapp.sql) | `whatsapp_links`, los códigos de vínculo y `'whatsapp'` como origen en la auditoría |
| [`004_verificacion_correo.sql`](migrations/004_verificacion_correo.sql) | `auth_codes` y `profiles.email_verified_at` |
| [`005_convenios_ordenes.sql`](migrations/005_convenios_ordenes.sql) | Convenios (`institutions.convenio_activo`, `institution_type`), `commission_policies`, `investment_orders`/`_items` y los dos triggers del modelo de negocio |

Al clonar el proyecto de cero: `schema.sql` → las migraciones en orden → `seed.sql`.
