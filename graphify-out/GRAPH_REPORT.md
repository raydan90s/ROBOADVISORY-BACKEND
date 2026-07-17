# Graph Report - d:\GitHub\ROBOADVISORY-BACKEND  (2026-07-16)

## Corpus Check
- 68 files · ~56,859 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 952 nodes · 2217 edges · 46 communities (42 shown, 4 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 78 edges (avg confidence: 0.56)
- Token cost: 51,718 input · 0 output

## Community Hubs (Navigation)
- Cartera y perfil del inversionista
- Autenticación y acceso a datos
- Catálogo de tasas y elegibilidad
- Revisión del asesor y auditoría
- Orquestación del chat del agente
- Guardarraíles anti-alucinación
- Explicación del portafolio con Gemini
- Proveedor LLM intercambiable
- Arquitectura del proyecto (README)
- Cotizaciones de mercado y roles
- Consultas SQL y tests de scoring
- Prompts de sistema del agente
- Rutas del agente A/B/C/D
- Vinculación de cuenta WhatsApp
- Formato de salida de WhatsApp
- Esquema de base de datos
- Tests de roles y permisos
- Grafo de estados LangGraph
- Webhook entrante de WhatsApp
- Feed de noticias GNews
- Tests de edición de asignación
- Wrapper cacheado de Alpha Vantage
- Tests de edición de perfil
- Datos semilla y vistas SQL
- Contexto permitido por ruta
- Source chips y fuentes citadas
- Transporte TwiML de WhatsApp
- Firma de webhook Twilio
- Arranque de la API y pool
- Tests de subcuentas y capital
- Configuración y entorno
- Ayudas de autenticación en tests
- Tests del feed de noticias
- Tests de perfilamiento
- Normalización de teléfono E.164
- Migración de subcuentas
- Migración de WhatsApp
- Migración de verificación de correo
- Dependencias de correo saliente

## God Nodes (most connected - your core abstractions)
1. `CurrentUser` - 53 edges
2. `get_connection()` - 40 edges
3. `validar()` - 29 edges
4. `ContextoPermitido` - 25 edges
5. `fetch_all()` - 24 edges
6. `fetch_one()` - 23 edges
7. `NivelRiesgo` - 23 edges
8. `MarketQuote` - 22 edges
9. `DatosExplicacion` - 21 edges
10. `FeedResponse` - 20 edges

## Surprising Connections (you probably didn't know these)
- `Stack LangGraph + langchain-core` --implements--> `Agente conversacional LangGraph (3 rutas + rechazo)`  [INFERRED]
  requirements.txt → README.md
- `cuenta_desechable()` --calls--> `get_connection()`  [EXTRACTED]
  tests/test_editar_asignacion.py → src/config/database.py
- `cuenta_desechable()` --calls--> `get_connection()`  [EXTRACTED]
  tests/test_editar_perfil.py → src/config/database.py
- `cuenta_desechable()` --calls--> `get_connection()`  [EXTRACTED]
  tests/test_roles.py → src/config/database.py
- `cuenta_desechable()` --calls--> `get_connection()`  [EXTRACTED]
  tests/test_subcuentas.py → src/config/database.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Flujo anti-alucinación: motor determinista → LLM redacta → guardarraíl valida → fallback** — readme_principio_anti_alucinacion, readme_contexto_permitido, readme_agent_graph, readme_perfilamiento, readme_tests_contra_base_real [EXTRACTED 1.00]
- **Las tres rutas del agente comparten router, guardarraíl y escritura solo a llm_interactions** — readme_ruta_a_bancario, readme_ruta_b_mixto, readme_ruta_c_externo, readme_contencion_escritura, readme_contexto_permitido [EXTRACTED 1.00]
- **Disciplina compartida ante cuotas gratuitas: caché de 1 h + respaldo determinista** — readme_market_data_wrapper, readme_feed_service, requirements_cachetools, render_yaml_secretos_sync_false [INFERRED 0.85]

## Communities (46 total, 4 thin omitted)

### Community 0 - "Cartera y perfil del inversionista"
Cohesion: 0.05
Nodes (91): _investor_de_sesion(), El inversionista tal como quedó en ESA sesión (no la más reciente).      Impor, _allocations_de(), _asignado(), create_investor_profile(), _datos_explicacion(), editar_asignacion(), editar_perfil() (+83 more)

### Community 1 - "Autenticación y acceso a datos"
Cohesion: 0.06
Nodes (70): fetch_one(), get_connection(), Connection, Presta una conexión del pool y hace commit/rollback automático.          with ge, Ejecuta un SELECT y devuelve la primera fila, o None si no hay ninguna., _consumir_codigo(), _emitir_codigo(), _fallar() (+62 more)

### Community 2 - "Catálogo de tasas y elegibilidad"
Cohesion: 0.06
Nodes (65): elegir_recomendado(), listar_tasas(), Catálogo de tasas: lectura pura sobre instruments + institutions.  No toca el, Elegible para el perfil (o sin perfil todavía) y con el monto mínimo cubierto., La opción que el motor recomienda. **Decide Python sobre las filas, nunca el LLM, Las tasas del catálogo, con la elegibilidad del perfil de quien consulta., _viable(), CatalogoTasas (+57 more)

### Community 3 - "Revisión del asesor y auditoría"
Cohesion: 0.08
Nodes (54): _banderas(), _cabecera(), _instrumentos_del_catalogo(), _lineas_de(), listar_auditoria(), listar_cola(), obtener_detalle(), Any (+46 more)

### Community 4 - "Orquestación del chat del agente"
Cohesion: 0.07
Nodes (54): _archivar_simulacion(), _calificaciones_validas(), _capital(), _cargar_historial(), _catalogo(), chat(), _contexto_agente(), _datos_de_sesion() (+46 more)

### Community 5 - "Guardarraíles anti-alucinación"
Cohesion: 0.07
Nodes (44): extraer_numeros(), _normalizar_numero(), _plano(), Decimal, Guardarraíles anti-alucinación. Se escriben ANTES que el LLM porque son el contr, Rechaza el texto si cita un número que no salió de la base., Rechaza cantidades escritas en palabras: en letras, un número no se puede verifi, Rechaza el texto si promete rentabilidad o niega el riesgo. (+36 more)

### Community 6 - "Explicación del portafolio con Gemini"
Cohesion: 0.11
Nodes (34): AssetAllocation, Una línea del portafolio: producto + emisor + calificación + USD asignados., contexto_permitido(), DatosExplicacion, Explicacion, explicacion_determinista(), fuentes(), _generar_con_gemini() (+26 more)

### Community 7 - "Proveedor LLM intercambiable"
Cohesion: 0.09
Nodes (33): RuntimeError, _api_key_viva(), _config_activa(), _config_valor(), _crear_anthropic(), _crear_google(), crear_llm(), _crear_openai() (+25 more)

### Community 8 - "Arquitectura del proyecto (README)"
Cohesion: 0.09
Nodes (32): Agente conversacional LangGraph (3 rutas + rechazo), Regla de capas: rutas sin SQL, controllers sin HTTP, Auditoría (/api/audit, v_audit_timeline), BROKEATE-APP (Expo / React Native, repo paraguas), Brokeate Backend (Robo-Advisory API), BROKEATE-WEB (Vite + React DOM, Vercel), Comparador de tasas (/api/catalog/rates), Contención de escritura de las rutas B/C (+24 more)

### Community 9 - "Cotizaciones de mercado y roles"
Cohesion: 0.12
Nodes (26): HTTPAuthorizationCredentials, obtener_cotizaciones(), obtener_historico(), Orquesta el wrapper cacheado de Alpha Vantage (`services/market_data.py`)., get_current_user(), _no_autenticado(), HTTPException, Dependencias de FastAPI para proteger endpoints.      @router.get("/queue", depe (+18 more)

### Community 10 - "Consultas SQL y tests de scoring"
Cohesion: 0.11
Nodes (22): execute(), fetch_all(), Any, Ejecuta un SELECT y devuelve todas las filas como dicts., Ejecuta INSERT/UPDATE/DELETE. Si la sentencia lleva RETURNING, devuelve esa fila, Toda plantilla suma exactamente 100%.  Un assert. Si una plantilla sumara 95, el, test_toda_plantilla_suma_100(), ⭐ La recomendación respeta un criterio objetivo de solidez del emisor.  "Ningún (+14 more)

### Community 11 - "Prompts de sistema del agente"
Cohesion: 0.13
Nodes (25): _bloque_cotizaciones(), _bloque_datos(), build_system_prompt(), build_system_prompt_asesoria(), build_system_prompt_externo(), build_system_prompt_mixto(), ContextoAgente, _explicacion_asesoria_determinista() (+17 more)

### Community 12 - "Rutas del agente A/B/C/D"
Cohesion: 0.12
Nodes (24): asesoria_node(), _explicacion_noticias_determinista(), _llamar_llm(), mercado_node(), mixto_node(), noticias_node(), Ruta C: 100% Alpha Vantage. NUNCA lee ni cita el catálogo del banco.      Cont, Ruta B: datos del banco + Alpha Vantage. Solo lectura de ambos, igual que Ruta A (+16 more)

### Community 13 - "Vinculación de cuenta WhatsApp"
Cohesion: 0.16
Nodes (19): crear_codigo(), desvincular(), estado(), _generar_codigo(), El asistente por WhatsApp: quién escribe, qué se le contesta.  Reusa el MISMO, Un código de seis dígitos, único entre los que están vivos.      `secrets`, no, La app pide un código para que el usuario lo escriba por WhatsApp., ¿Esta cuenta tiene un WhatsApp vinculado? El teléfono vuelve enmascarado. (+11 more)

### Community 14 - "Formato de salida de WhatsApp"
Cohesion: 0.11
Nodes (21): _codigo_del_mensaje(), Extrae el código de «VINCULAR 123456» (tolerando guiones, espacios y mayúsculas), formatear(), Normaliza el texto a lo que WhatsApp sabe pintar. Idempotente y sin estado., El canal de WhatsApp: la puerta pública y el alcance del agente.  El webhook es, ⭐ Se ven como ruido en un globo de chat; salen como comillas rectas., WhatsApp no renderiza markdown: un '**' se lee literal, con los dos asteriscos., En MULTILINE un `\\s*` final se traga el salto y fusiona las dos líneas. (+13 more)

### Community 15 - "Esquema de base de datos"
Cohesion: 0.23
Nodes (20): public.advisor_reviews, public.allocation_template_items, public.allocation_templates, public.audit_log, public.auth_sessions, public.institutions, public.instruments, public.llm_interactions (+12 more)

### Community 16 - "Tests de roles y permisos"
Cohesion: 0.10
Nodes (18): cuenta_desechable(), Un investor llamando /api/advisor/* recibe 403.  Es el test que justifica `requi, La cartera y el perfilamiento de otro cliente son datos ajenos, aunque se sepa s, Sin token no se lee la cartera de nadie. Era el agujero: bastaba conocer un id., El contrapeso: si el guardia bloqueara también al dueño, la app no serviría., Revisar carteras ajenas es el trabajo del asesor (HU3)., Cuentas que el test crea y que el test borra.      Sin esto, cada corrida de la, El cuestionario es del inversionista. Un asesor llamándolo recibe 403. (+10 more)

### Community 17 - "Grafo de estados LangGraph"
Cohesion: 0.18
Nodes (19): AgentState, _clasificar_ruta(), _construir_grafo(), _fuera_de_alcance(), guardrail_node(), qa_node(), El agente conversacional como grafo de estados (LangGraph).  Responde al crite, Valida el texto contra el conjunto permitido de la ruta. Si falla, prepara el re (+11 more)

### Community 18 - "Webhook entrante de WhatsApp"
Cohesion: 0.13
Nodes (19): Request, Response, _canjear_codigo(), _link_activo(), procesar_mensaje(), Any, Connection, El perfil dueño de este número, o None si el número no está vinculado.      Es (+11 more)

### Community 19 - "Feed de noticias GNews"
Cohesion: 0.20
Nodes (16): FeedResponse, NoticiaFeed, BaseModel, Modelos del feed de noticias (GET /api/feed)., Una noticia citada: titular + fuente + fecha + link. La app no redacta noticias., get_feed(), Feed de noticias financieras. Solo I/O HTTP: la lógica vive en feed_service., _bloque_titulares_prompt() (+8 more)

### Community 20 - "Tests de edición de asignación"
Cohesion: 0.17
Nodes (17): cabeceras_de(), cuenta_desechable(), _editar(), propuesta(), El inversionista arma su propia mezcla de fondos — dentro de las reglas.  PUT, Un conservador nuevo con su propuesta recién generada, lista para editar., ⭐ La mezcla nueva reemplaza a la plantilla, con los USD calculados en Postgres., ⭐ DPF_LOJA_360 tiene la mejor tasa y la peor calificación (AA, tier 4):     el (+9 more)

### Community 21 - "Wrapper cacheado de Alpha Vantage"
Cohesion: 0.24
Nodes (15): AsyncClient, _es_error_de_cuota(), HistoricalPoint, HistoricalSeries, _mock_de(), _mock_historico(), obtener_cotizacion(), obtener_historico() (+7 more)

### Community 22 - "Tests de edición de perfil"
Cohesion: 0.18
Nodes (15): _asesor(), conservador(), cuenta_desechable(), _editar_perfil(), El inversionista corrige sus respuestas — y con eso reabre la revisión.  PUT /ap, Un conservador nuevo con su propuesta generada, listo para reperfilarse., ⭐ De conservador (5) a agresivo (15): el desglose que devuelve ya trae lo nuevo., ⭐ La propuesta cambia de productos (los del agresivo) y el monto se conserva. (+7 more)

### Community 23 - "Datos semilla y vistas SQL"
Cohesion: 0.27
Nodes (13): public.institutions, public.instruments, public.llm_interactions, public.profile_institution_rules, public.profiling_sessions, public.proposal_items, public.proposals, public.v_advisor_review_queue (+5 more)

### Community 24 - "Contexto permitido por ruta"
Cohesion: 0.18
Nodes (14): contexto_permitido_agente(), contexto_permitido_asesoria(), contexto_permitido_mercado(), contexto_permitido_noticias(), ItemCatalogo, Un producto del catálogo, con si el perfil del inversionista puede tomarlo., Una sesión de inversión del mismo inversionista (para comparar)., Extiende el conjunto citable de la propuesta con el catálogo y las subcuentas. (+6 more)

### Community 25 - "Source chips y fuentes citadas"
Cohesion: 0.19
Nodes (13): fuentes_citadas(), fuentes_citadas_mercado(), fuentes_citadas_noticias(), _norm(), Any, Corre el grafo para un turno y devuelve el estado final.      `contexto` es to, Sin tildes y en minúsculas, para comparar menciones sin depender de la ortografí, Source chips DINÁMICOS: solo las fuentes que ESTA respuesta realmente mencionó. (+5 more)

### Community 26 - "Transporte TwiML de WhatsApp"
Cohesion: 0.17
Nodes (12): _partir(), La capa de transporte de WhatsApp: firma, formato de salida y teléfonos.  Todo l, Parte el texto en globos, cortando en saltos de línea antes que a media palabra., El XML que Twilio espera como respuesta al webhook.      `escape` no es decorati, twiml(), La garantía vive en la salida, no en la confianza de que el LLM obedezca el prom, ⭐ Un '&' sin escapar rompe el XML y Twilio no entrega NADA: silencio, sin error., WhatsApp corta a los 1600; partimos nosotros para elegir dónde. (+4 more)

### Community 27 - "Firma de webhook Twilio"
Cohesion: 0.18
Nodes (12): firma_esperada(), firma_valida(), La firma que Twilio habría calculado para este request.      El algoritmo es de, True si `firma` (header X-Twilio-Signature) corresponde a este request.      La, Si esto falla, el algoritmo está mal y NINGÚN mensaje real entraría., ⭐ El ataque que importa: cambiar el remitente para leer la cartera de otro., Un token vacío no puede significar 'pasa todo': significa 'no puedo verificar'., Si TWILIO_WEBHOOK_URL no coincide con la de la consola, nada valida — a propósit (+4 more)

### Community 28 - "Arranque de la API y pool"
Cohesion: 0.24
Nodes (9): ConnectionPool, FastAPI, get_pool(), Pool de conexiones a Postgres (Supabase) vía psycopg 3.  Nos conectamos por Post, Pool único por proceso. Se abre perezosamente en el primer uso., health(), lifespan(), Punto de entrada de la API. Levanta con:  uvicorn src.main:app --reload (+1 more)

### Community 29 - "Tests de subcuentas y capital"
Cohesion: 0.29
Nodes (10): _crear_subcuenta(), cuenta_desechable(), _fijar_capital(), ⭐ El motor de subcuentas: un capital total repartido en varias subcuentas.  `t, Bajar el capital total por debajo de lo ya asignado dejaría un sin_asignar negat, Cuentas que el test crea y que el test borra (ver test_roles.py)., ⭐ USD 40.000 -> 20k/10k/10k caben; una cuarta subcuenta de USD 1 ya no., test_el_capital_se_reparte_en_subcuentas_sin_pasarse() (+2 more)

### Community 30 - "Configuración y entorno"
Cohesion: 0.22
Nodes (6): BaseSettings, get_settings(), Carga y valida las variables de entorno una sola vez para toda la app., Cacheado: el .env se lee una única vez por proceso., Settings, Configuración común de los tests.  Los tests de scoring/elegibilidad corren **co

### Community 31 - "Ayudas de autenticación en tests"
Cohesion: 0.20
Nodes (9): codigo_pendiente(), Crear una cuenta usable desde un test, ahora que el registro no entrega el token, El código de 6 dígitos que está vivo para ese correo. Falla si no hay ninguno., ⭐ El gate: la contraseña correcta no alcanza si el correo nunca se probó.      S, El código es un secreto, no un formulario: adivinarlo no abre la cuenta., El self-signup crea investors. Pedir 'advisor' en el body no cambia nada.      E, test_el_rol_no_es_negociable_desde_el_cliente(), test_sin_verificar_el_correo_no_se_entra() (+1 more)

### Community 32 - "Tests del feed de noticias"
Cohesion: 0.22
Nodes (5): El feed de noticias degrada con gracia: sin key (o con GNews caído) sirve el re, Con o sin key: siempre hay noticias, y cada una trae titular, fuente y link., Si no hay GNEWS_API_KEY, `fuente_datos` debe decir "respaldo": el front avisa, test_cada_tema_responde_noticias_citadas(), test_sin_key_el_respaldo_se_declara_como_tal()

### Community 33 - "Tests de perfilamiento"
Cohesion: 0.25
Nodes (8): TestClient, Any, Registra un inversionista desechable, verifica su correo y devuelve el TokenResp, registrar_verificado(), ⭐ El id del cuestionario ES el del login.      Antes se creaba un `profiles` nue, test_el_perfilamiento_se_adjunta_al_usuario_del_token(), cabeceras(), Un inversionista nuevo, logueado, listo para declarar capital.      Registrar

### Community 34 - "Normalización de teléfono E.164"
Cohesion: 0.40
Nodes (5): normalizar_telefono(), whatsapp:+593 99 999 9999' → '+593999999999'. None si no es un E.164 creíble., Lo que no se puede normalizar no se busca en la base: se descarta., test_normalizar_telefono_quita_el_canal_y_el_formato(), test_un_telefono_que_no_es_e164_no_pasa()

## Knowledge Gaps
- **13 isolated node(s):** `public.profiles`, `public.profiling_sessions`, `public.whatsapp_links`, `public.whatsapp_link_codes`, `public.llm_interactions` (+8 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_connection()` connect `Autenticación y acceso a datos` to `Cartera y perfil del inversionista`, `Revisión del asesor y auditoría`, `Orquestación del chat del agente`, `Consultas SQL y tests de scoring`, `Vinculación de cuenta WhatsApp`, `Tests de roles y permisos`, `Webhook entrante de WhatsApp`, `Tests de edición de asignación`, `Tests de edición de perfil`, `Arranque de la API y pool`, `Tests de subcuentas y capital`?**
  _High betweenness centrality (0.117) - this node is a cross-community bridge._
- **Why does `CurrentUser` connect `Cartera y perfil del inversionista` to `Autenticación y acceso a datos`, `Catálogo de tasas y elegibilidad`, `Revisión del asesor y auditoría`, `Orquestación del chat del agente`, `Cotizaciones de mercado y roles`, `Vinculación de cuenta WhatsApp`, `Webhook entrante de WhatsApp`, `Feed de noticias GNews`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `validar()` connect `Catálogo de tasas y elegibilidad` to `Guardarraíles anti-alucinación`, `Explicación del portafolio con Gemini`, `Formato de salida de WhatsApp`, `Grafo de estados LangGraph`, `Contexto permitido por ruta`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `ContextoPermitido` (e.g. with `AgentState` and `ContextoAgente`) actually correct?**
  _`ContextoPermitido` has 8 INFERRED edges - model-reasoned connections that need verification._
- **What connects `public.profiles`, `public.profiling_sessions`, `public.whatsapp_links` to the rest of the system?**
  _13 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Cartera y perfil del inversionista` be split into smaller, more focused modules?**
  _Cohesion score 0.053303471444568866 - nodes in this community are weakly interconnected._
- **Should `Autenticación y acceso a datos` be split into smaller, more focused modules?**
  _Cohesion score 0.06293965198074787 - nodes in this community are weakly interconnected._