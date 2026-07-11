-- =====================================================================
-- ROBO-ADVISORY — MIGRACIÓN + CATÁLOGO BANCARIO + SEED DE DEMO
-- Correr DESPUÉS de schema.sql, en Supabase -> SQL Editor.
-- Es RE-EJECUTABLE: resetea el catálogo y los datos de demo, y los vuelve
-- a sembrar. No toca la estructura creada por schema.sql.
--
-- Por qué existe este archivo:
--   El catálogo original de schema.sql eran instrumentos bursátiles (ETFs,
--   bonos). El documento del reto (docs/Ejemplo_de_Robo_Advisor.md) pide
--   explícitamente productos que "un banco puede comercializar directamente,
--   en lugar de limitarse a instrumentos bursátiles": Depósitos a Plazo Fijo
--   y Fondos de Inversión. Este script hace ese pivote.
--
-- Qué agrega, sección por sección:
--   A) Instituciones financieras con calificación de riesgo (AAA…BB).
--   B) Monto de inversión en USD (el esquema solo tenía porcentajes).
--   C) Regla versionada: qué calificación mínima admite cada perfil.
--   D) Columnas de evidencia anti-alucinación en llm_interactions.
--   E) Reset del catálogo viejo.
--   F) Catálogo bancario nuevo + 5 preguntas + umbrales.
--   G) 3 casos de demo, incluido el de Juan Pérez tal como está en el doc.
--   H) Vistas de conveniencia.
--   I) Verificación (todo debe decir OK).
--
-- Credenciales demo (todas): password = demo1234
-- =====================================================================


-- =====================================================================
-- A. INSTITUCIONES FINANCIERAS Y SU CALIFICACIÓN DE RIESGO
-- =====================================================================
-- El diferenciador que aporta docs/Institución_financiera_y_calificacion_de_riessgo.md:
-- el robo-advisor no solo evalúa al cliente, también la solidez de la entidad
-- donde va el dinero. Es un criterio objetivo, explicable y — sobre todo —
-- un SEGUNDO CATÁLOGO CERRADO que el LLM no puede inventar.
--
-- rating_tier es la clave del diseño: ordenar 'AAA-' vs 'AA+' como texto es
-- frágil. Un entero ordenable permite escribir la regla como una comparación
-- (`rating_tier <= max_rating_tier`) y testearla con un solo assert.

create table if not exists public.institutions (
    id             uuid primary key default gen_random_uuid(),
    code           text not null unique,          -- 'PICHINCHA'
    name           text not null,                 -- 'Banco Pichincha'
    credit_rating  text not null,                 -- 'AAA', 'AAA-', 'AA+', 'AA'
    -- 1=AAA · 2=AAA- · 3=AA+ · 4=AA · 5=AA- · 6=A · 7=BBB · 8=BB o inferior
    rating_tier    integer not null check (rating_tier between 1 and 8),
    rating_source  text,                          -- calificadora que la emitió
    rating_date    date,                          -- las calificaciones caducan: hay que fecharlas
    is_active      boolean not null default true
);

comment on table public.institutions is
'Entidades emisoras de los productos. La calificación es REFERENCIAL y fechada: se muestra siempre con su fuente y fecha, nunca como dato vivo.';
comment on column public.institutions.rating_tier is
'Calificación normalizada a entero ordenable. Menor = más seguro. Permite expresar la regla de elegibilidad como una comparación testeable.';


-- =====================================================================
-- B. MONTO DE INVERSIÓN
-- =====================================================================
-- El ejemplo del reto muestra "60% (USD 12.000)", no solo "60%". Sin el monto
-- la propuesta se ve académica; con el monto se ve como un producto real.
-- El monto se guarda también en la propuesta (snapshot inmutable): si el
-- cliente vuelve a perfilarse con otro monto, la propuesta vieja no cambia.

alter table public.profiling_sessions
    add column if not exists amount numeric(14,2) check (amount is null or amount > 0);

alter table public.proposals
    add column if not exists total_amount numeric(14,2);

alter table public.proposal_items
    add column if not exists amount numeric(14,2);

comment on column public.proposal_items.amount is
'USD asignados = percentage * proposals.total_amount / 100. Calculado por Postgres, NUNCA por el LLM. Entra al set de números permitidos del guardarraíl.';


-- Producto bancario, no instrumento bursátil.
alter table public.instruments
    add column if not exists institution_id uuid references public.institutions(id),
    add column if not exists product_type   text
        check (product_type is null or product_type in ('deposito_plazo', 'fondo_inversion')),
    add column if not exists term_days      integer,   -- null en fondos (liquidez variable)
    add column if not exists min_amount     numeric(14,2);

comment on column public.instruments.min_amount is
'Monto mínimo de acceso. Habilita una bandera determinista para el asesor: "el monto asignado a X queda bajo el mínimo".';


-- =====================================================================
-- C. REGLA VERSIONADA DE ELEGIBILIDAD POR CALIFICACIÓN
-- =====================================================================
-- Esta tabla ES la regla del documento:
--   Conservador -> solo AAA / AAA-      (tier <= 2)
--   Moderado    -> hasta AA+ / AA       (tier <= 4)
--   Agresivo    -> abierto, con aviso   (tier <= 8)
--
-- Va versionada (rules_version_id) porque el track exige reglas "visibles y
-- versionadas". Es un TECHO, no una meta: que un perfil agresivo *pueda*
-- acceder a una AA no obliga a la plantilla a usarla.

create table if not exists public.profile_institution_rules (
    id                uuid primary key default gen_random_uuid(),
    rules_version_id  uuid not null references public.rules_versions(id) on delete cascade,
    risk_profile_id   uuid not null references public.risk_profiles(id) on delete cascade,
    max_rating_tier   integer not null check (max_rating_tier between 1 and 8),
    rationale         text not null,
    unique (rules_version_id, risk_profile_id)
);

comment on table public.profile_institution_rules is
'Calificación mínima de institución admitida por perfil, por versión de reglas. Visible en la app y verificable con un test.';


-- =====================================================================
-- D. EVIDENCIA ANTI-ALUCINACIÓN EN llm_interactions
-- =====================================================================
-- Sin estas columnas no hay dónde guardar las fuentes que cita el agente
-- (los "source chips") ni el resultado del guardarraíl numérico. Son la
-- evidencia literal del criterio de evaluación #3.

alter table public.llm_interactions
    add column if not exists thread_id        text,
    -- {"sources":[{"table":"proposal_items","record_id":"…","label":"DPF Pichincha · 60% · USD 12.000"}]}
    add column if not exists metadata         jsonb,
    -- null = mensaje de user/system. true/false = veredicto del validador.
    add column if not exists guardrail_passed boolean,
    -- >0 = el guardarraíl rechazó el texto y se reintentó.
    add column if not exists retry_count      integer not null default 0,
    add column if not exists model            text;

create index if not exists idx_llm_interactions_thread  on public.llm_interactions(thread_id);
create index if not exists idx_llm_interactions_session on public.llm_interactions(session_id);
create index if not exists idx_instruments_institution  on public.instruments(institution_id);

alter table public.institutions              enable row level security;
alter table public.profile_institution_rules enable row level security;


-- =====================================================================
-- E. RESET — borra el catálogo bursátil y los datos de demo
-- =====================================================================
-- Destructivo a propósito y en orden de FK. Es lo que hace que este script
-- se pueda correr las veces que haga falta durante el hackathon.
-- NO borra: rules_versions, risk_profiles, ni los usuarios demo de schema.sql.

delete from public.llm_interactions;
delete from public.audit_log;
delete from public.advisor_reviews;
delete from public.proposal_items;
delete from public.proposals;
delete from public.profiling_answers;
delete from public.profiling_sessions;
delete from public.allocation_template_items;
delete from public.allocation_templates;
delete from public.profile_institution_rules;
delete from public.scoring_rules;
delete from public.question_options;
delete from public.questions;
delete from public.profile_thresholds;
delete from public.instruments;
delete from public.institutions;
-- `is distinct from` y no `<>`: hay inversionistas de prueba creados por la API
-- con email null, y `null <> '...'` es null, no true — no se borrarían.
delete from public.profiles
where role = 'investor' and email is distinct from 'inversionista@demo.ec';


-- =====================================================================
-- F. CATÁLOGO Y REGLAS (versión v1)
-- =====================================================================

-- --- F.0 Usuarios demo: password bcrypt (venían en null en schema.sql) ---
update public.profiles
set password_hash = '$2b$12$g89ivN3PJW7nh0i/UGrAXOAd.HFMzTRIpCWhb.RR37ZhpSgIi7Cuu'  -- demo1234
where email = 'inversionista@demo.ec';

update public.profiles
set password_hash = '$2b$12$K/y2uCSRazPTOgR5ekvq..y.Ub.1cqPl4nwA7eLkWP6GhlFlSYceS'  -- demo1234
where email = 'asesor@demo.ec';

-- Red de seguridad por si schema.sql no dejó estas filas
insert into public.rules_versions (version_label, description, is_active)
values ('v1', 'Reglas iniciales de perfilamiento — catálogo bancario', true)
on conflict (version_label) do nothing;

insert into public.risk_profiles (code, name, description) values
  ('conservador', 'Conservador', 'Prioriza preservación de capital'),
  ('moderado',    'Moderado',    'Balance entre riesgo y crecimiento'),
  ('agresivo',    'Agresivo',    'Prioriza crecimiento, tolera volatilidad')
on conflict (code) do nothing;


-- --- F.1 Instituciones y su calificación -----------------------------
-- ⚠️ Datos REFERENCIALES tomados de docs/Institución_financiera_y_calificacion_de_riessgo.md.
--    Se muestran siempre con calificadora y fecha. La app NO los presenta como
--    calificación vigente en tiempo real: eso sería exactamente el tipo de dato
--    inventado que el criterio #3 penaliza.
insert into public.institutions (code, name, credit_rating, rating_tier, rating_source, rating_date) values
  ('PICHINCHA',   'Banco Pichincha',    'AAA',  1, 'BankWatch Ratings',        date '2026-06-30'),
  ('GUAYAQUIL',   'Banco Guayaquil',    'AAA',  1, 'PCR (Pacific Credit Rating)', date '2026-06-30'),
  ('PRODUBANCO',  'Produbanco',         'AAA',  1, 'Class International Rating',  date '2026-06-30'),
  ('BOLIVARIANO', 'Banco Bolivariano',  'AAA',  1, 'Global Ratings',           date '2026-06-30'),
  ('AUSTRO',      'Banco del Austro',   'AAA',  1, 'BankWatch Ratings',        date '2026-06-30'),
  ('MACHALA',     'Banco Machala',      'AAA-', 2, 'PCR (Pacific Credit Rating)', date '2026-06-30'),
  ('SOLIDARIO',   'Banco Solidario',    'AAA-', 2, 'Class International Rating',  date '2026-06-30'),
  ('VISIONFUND',  'Banco VisionFund',   'AA+',  3, 'Global Ratings',           date '2026-06-30'),
  ('LOJA',        'Banco Loja',         'AA',   4, 'BankWatch Ratings',        date '2026-06-30'),
  ('CAPITAL',     'Banco Capital',      'AA',   4, 'PCR (Pacific Credit Rating)', date '2026-06-30');


-- --- F.2 Productos bancarios ----------------------------------------
-- Tasas dentro del rango que cita el documento (DPF entre 4,5% y 9,7%).
-- Ficticias/referenciales, solo demo. Ningún texto de la app las promete.
--
-- Fíjate en DPF_LOJA_360: la MEJOR tasa (9,4%) la ofrece la institución con
-- la PEOR calificación (AA). Ese trade-off es lo que hace que la regla de
-- elegibilidad se vea trabajando en pantalla: el perfil conservador no puede
-- tocarla, el agresivo sí.
insert into public.instruments
    (code, name, asset_class, risk_class, expected_return, description,
     institution_id, product_type, term_days, min_amount)
select v.code, v.name, v.asset_class, v.risk_class::risk_level, v.expected_return, v.description,
       inst.id, v.product_type, v.term_days, v.min_amount
from (values
  ('DPF_PICHINCHA_180',  'Depósito a Plazo Fijo 180 días', 'renta_fija', 'bajo',   5.800,
   'Póliza de acumulación a 180 días. Tasa fija pactada al inicio.',
   'PICHINCHA',   'deposito_plazo',  180,  500.00),

  ('DPF_PICHINCHA_360',  'Depósito a Plazo Fijo 360 días', 'renta_fija', 'bajo',   7.200,
   'Póliza de acumulación a 360 días. Preserva el capital con rentabilidad fija.',
   'PICHINCHA',   'deposito_plazo',  360,  500.00),

  ('DPF_GUAYAQUIL_360',  'Depósito a Plazo Fijo 360 días', 'renta_fija', 'bajo',   6.900,
   'Póliza a 360 días. Tasa de interés previamente establecida.',
   'GUAYAQUIL',   'deposito_plazo',  360,  500.00),

  ('DPF_PRODUBANCO_720', 'Depósito a Plazo Fijo 720 días', 'renta_fija', 'bajo',   8.100,
   'Póliza a 720 días. Mayor plazo, mejor tasa, sin liquidez intermedia.',
   'PRODUBANCO',  'deposito_plazo',  720, 1000.00),

  ('DPF_LOJA_360',       'Depósito a Plazo Fijo 360 días', 'renta_fija', 'medio',  9.400,
   'Póliza a 360 días con tasa superior. Emisor con calificación AA: mayor rendimiento a cambio de mayor riesgo de contraparte.',
   'LOJA',        'deposito_plazo',  360,  500.00),

  ('FONDO_LIQUIDEZ',     'Fondo de Liquidez',              'renta_fija', 'bajo',   4.500,
   'Fondo de disponibilidad inmediata. Para la porción del capital que puede necesitarse antes del plazo.',
   'PICHINCHA',   'fondo_inversion', null,  100.00),

  ('FONDO_RENTA_FIJA',   'Fondo de Renta Fija',            'renta_fija', 'bajo',   5.500,
   'Cartera diversificada de instrumentos de deuda, administrada por profesionales.',
   'PRODUBANCO',  'fondo_inversion', null,  100.00),

  ('FONDO_BALANCEADO',   'Fondo Balanceado',               'mixto',      'medio',  8.300,
   'Cartera diversificada que combina renta fija y variable. Busca crecimiento con volatilidad controlada.',
   'GUAYAQUIL',   'fondo_inversion', null,  100.00),

  ('FONDO_CRECIMIENTO',  'Fondo de Crecimiento',           'renta_variable', 'alto', 11.500,
   'Cartera orientada a renta variable. Mayor potencial de rentabilidad y mayor volatilidad.',
   'BOLIVARIANO', 'fondo_inversion', null,  500.00)
) as v(code, name, asset_class, risk_class, expected_return, description,
       institution_code, product_type, term_days, min_amount)
join public.institutions inst on inst.code = v.institution_code;


-- --- F.3 Cuestionario: 5 preguntas puntuables ------------------------
-- El monto NO es una de ellas: es un parámetro de la sesión, no una respuesta
-- que sume puntos. Un cliente con USD 200.000 no es por eso más agresivo.
--
-- Las 5 salen del cuestionario del documento del reto:
--   "¿Cuál es su objetivo?"                      -> objetivo
--   "¿Durante cuánto tiempo?"                    -> horizonte
--   "¿Necesitará disponer del dinero antes?"     -> liquidez     (nueva)
--   "¿Qué tan cómodo se siente con el riesgo?"   -> tolerancia
--   "¿Qué prefiere?"                             -> preferencia  (nueva)
insert into public.questions (code, text, order_index) values
  ('objetivo',    '¿Cuál es tu objetivo principal de inversión?',            1),
  ('horizonte',   '¿Durante cuánto tiempo puedes mantener la inversión?',    2),
  ('liquidez',    '¿Necesitarás disponer del dinero antes de que termine el plazo?', 3),
  ('tolerancia',  '¿Cómo reaccionarías ante una caída del 15% en tu portafolio?',    4),
  ('preferencia', '¿Qué prefieres?',                                          5);

insert into public.question_options (question_id, code, label, order_index)
select q.id, v.code, v.label, v.order_index
from (values
  ('objetivo',    'preservar',   'Preservar mi capital',                              1),
  ('objetivo',    'balancear',   'Balancear seguridad y crecimiento',                 2),
  ('objetivo',    'crecer',      'Hacer crecer mis ahorros',                          3),

  ('horizonte',   'corto',       'Menos de 2 años',                                   1),
  ('horizonte',   'medio',       'De 2 a 5 años',                                     2),
  ('horizonte',   'largo',       'Más de 5 años',                                     3),

  ('liquidez',    'si_probable', 'Sí, es probable que lo necesite',                   1),
  ('liquidez',    'tal_vez',     'Tal vez una parte',                                 2),
  ('liquidez',    'no',          'No, puedo mantenerlo hasta el final',               3),

  ('tolerancia',  'vender',      'Vendería todo de inmediato',                        1),
  ('tolerancia',  'esperar',     'Esperaría a que se recupere',                       2),
  ('tolerancia',  'comprar_mas', 'Compraría más aprovechando el precio bajo',         3),

  ('preferencia', 'seguridad',            'Seguridad ante todo',                      1),
  ('preferencia', 'seguridad_rentable',   'Seguridad con una rentabilidad competitiva', 2),
  ('preferencia', 'maxima_rentabilidad',  'La máxima rentabilidad posible',           3)
) as v(q_code, code, label, order_index)
join public.questions q on q.code = v.q_code;

-- Puntos por opción (v1). ESTA es la fuente de verdad del puntaje: nunca el LLM.
insert into public.scoring_rules (rules_version_id, question_option_id, points)
select rv.id, o.id, v.points
from (values
  ('objetivo',    'preservar',           1),
  ('objetivo',    'balancear',           2),
  ('objetivo',    'crecer',              3),
  ('horizonte',   'corto',               1),
  ('horizonte',   'medio',               2),
  ('horizonte',   'largo',               3),
  ('liquidez',    'si_probable',         1),
  ('liquidez',    'tal_vez',             2),
  ('liquidez',    'no',                  3),
  ('tolerancia',  'vender',              1),
  ('tolerancia',  'esperar',             2),
  ('tolerancia',  'comprar_mas',         3),
  ('preferencia', 'seguridad',           1),
  ('preferencia', 'seguridad_rentable',  2),
  ('preferencia', 'maxima_rentabilidad', 3)
) as v(q_code, o_code, points)
join public.questions q        on q.code = v.q_code
join public.question_options o on o.question_id = q.id and o.code = v.o_code
cross join (select id from public.rules_versions where version_label = 'v1') rv;


-- --- F.4 Umbrales -----------------------------------------------------
-- 5 preguntas × 1–3 puntos = rango 5 a 15.
--   Conservador  5–8  ·  Moderado  9–12  ·  Agresivo  13–15
-- Calibrados contra el caso del documento: Juan Pérez suma 12 -> Moderado. ✓
insert into public.profile_thresholds (rules_version_id, risk_profile_id, min_score, max_score)
select rv.id, rp.id, v.min_score, v.max_score
from (values
  ('conservador',  5,  8),
  ('moderado',     9, 12),
  ('agresivo',    13, 15)
) as v(code, min_score, max_score)
join public.risk_profiles rp on rp.code = v.code
cross join (select id from public.rules_versions where version_label = 'v1') rv;


-- --- F.5 Elegibilidad por calificación de institución ------------------
insert into public.profile_institution_rules
    (rules_version_id, risk_profile_id, max_rating_tier, rationale)
select rv.id, rp.id, v.max_tier, v.rationale
from (values
  ('conservador', 2,
   'Solo instituciones AAA o AAA-. El perfil prioriza la preservación del capital, así que el riesgo de contraparte debe ser el mínimo disponible.'),
  ('moderado',    4,
   'Se admiten instituciones hasta AA+ / AA. Permite acceder a mejores tasas asumiendo un riesgo de contraparte moderado, coherente con el perfil.'),
  ('agresivo',    8,
   'Sin restricción por calificación, siempre que el nivel de riesgo se explique de forma expresa en la propuesta.')
) as v(code, max_tier, rationale)
join public.risk_profiles rp on rp.code = v.code
cross join (select id from public.rules_versions where version_label = 'v1') rv;


-- --- F.6 Plantillas de asignación -------------------------------------
insert into public.allocation_templates (rules_version_id, risk_profile_id, name, expected_risk)
select rv.id, rp.id, v.name, v.expected_risk::risk_level
from (values
  ('conservador', 'Plantilla Conservadora', 'bajo'),
  ('moderado',    'Plantilla Moderada',     'medio'),
  ('agresivo',    'Plantilla Agresiva',     'alto')
) as v(code, name, expected_risk)
join public.risk_profiles rp on rp.code = v.code
cross join (select id from public.rules_versions where version_label = 'v1') rv;

-- La composición.
-- MODERADA = 60% Depósito a Plazo Fijo + 40% Fondo de Inversión: es EXACTAMENTE
-- la propuesta del documento del reto. Con un monto de USD 20.000 da USD 12.000
-- y USD 8.000, las mismas cifras del ejemplo. El caso de Juan Pérez se reproduce
-- sin forzar nada, y por eso sirve como test dorado.
insert into public.allocation_template_items (template_id, instrument_id, percentage)
select at.id, i.id, v.pct
from (values
  -- Conservadora: todo AAA (tier 1). Cumple su propia regla de elegibilidad.
  ('conservador', 'DPF_PICHINCHA_360',  50.00),
  ('conservador', 'FONDO_RENTA_FIJA',   30.00),
  ('conservador', 'FONDO_LIQUIDEZ',     20.00),

  -- Moderada: el 60/40 del documento.
  ('moderado',    'DPF_PICHINCHA_360',  60.00),
  ('moderado',    'FONDO_BALANCEADO',   40.00),

  -- Agresiva: aquí sí entra la institución AA (mejor tasa, mayor riesgo de
  -- contraparte). Es la única plantilla cuya regla lo permite.
  ('agresivo',    'DPF_LOJA_360',       20.00),
  ('agresivo',    'FONDO_BALANCEADO',   30.00),
  ('agresivo',    'FONDO_CRECIMIENTO',  50.00)
) as v(profile_code, instrument_code, pct)
join public.risk_profiles rp   on rp.code = v.profile_code
join public.allocation_templates at on at.risk_profile_id = rp.id
join public.instruments i      on i.code = v.instrument_code;


-- =====================================================================
-- G. SEED TRANSACCIONAL — 3 CASOS DE DEMO
-- =====================================================================
-- El "Inversionista Demo" de schema.sql se deja SIN perfilar a propósito:
-- es la cuenta con la que se graba el flujo en vivo del video.
--
--   Juan Pérez     USD 20.000   Moderado    12/15   EN REVISIÓN  <- el caso del documento
--   Andrea Salinas USD 50.000   Agresivo    15/15   EN REVISIÓN
--   Carlos Ruiz    USD  8.000   Conservador  5/15   APROBADA     <- alimenta Auditoría
--
-- Los puntajes NO están escritos a mano: el script los LEE de scoring_rules,
-- igual que hará el backend. Los montos en USD tampoco: los calcula Postgres
-- a partir del porcentaje. Si mañana cambian los puntos de una opción, el seed
-- sigue siendo coherente — esa es la prueba, dentro del propio seed, de que la
-- fuente de verdad son las reglas y no un número inventado.

do $$
declare
    v_rv_id       uuid;
    v_advisor_id  uuid;
    v_investor_id uuid;
    v_session_id  uuid;
    v_proposal_id uuid;
    v_profile_id  uuid;
    v_template_id uuid;
    v_risk        risk_level;
    v_total       integer;
    v_caso        record;
    v_resp        record;
begin
    select id into v_rv_id      from public.rules_versions where version_label = 'v1';
    select id into v_advisor_id from public.profiles       where email = 'asesor@demo.ec';

    if v_rv_id is null or v_advisor_id is null then
        raise exception 'Falta rules_version v1 o el usuario asesor@demo.ec. ¿Corriste schema.sql primero?';
    end if;

    for v_caso in
        select * from (values
            ('Juan Pérez',     '0912345678', 'juan@demo.ec',   20000.00,
             'crecer',    'medio', 'no',          'esperar',     'seguridad_rentable',  'pending_review'),
            ('Andrea Salinas', '0923456789', 'andrea@demo.ec', 50000.00,
             'crecer',    'largo', 'no',          'comprar_mas', 'maxima_rentabilidad', 'pending_review'),
            ('Carlos Ruiz',    '0934567890', 'carlos@demo.ec',  8000.00,
             'preservar', 'corto', 'si_probable', 'vender',      'seguridad',           'approved')
        ) as c(nombre, cedula, email, monto,
               r_objetivo, r_horizonte, r_liquidez, r_tolerancia, r_preferencia, estado)
    loop
        -- 1) El inversionista (password demo1234, igual que el resto)
        insert into public.profiles (role, full_name, cedula_ruc, email, password_hash)
        values ('investor', v_caso.nombre, v_caso.cedula, v_caso.email,
                '$2b$12$g89ivN3PJW7nh0i/UGrAXOAd.HFMzTRIpCWhb.RR37ZhpSgIi7Cuu')
        returning id into v_investor_id;

        -- 2) Sesión atada a la versión de reglas activa, con el monto a invertir
        insert into public.profiling_sessions (investor_id, rules_version_id, amount)
        values (v_investor_id, v_rv_id, v_caso.monto)
        returning id into v_session_id;

        -- 3) Respuestas: los PUNTOS se leen de scoring_rules
        v_total := 0;
        for v_resp in
            select q.id as question_id, o.id as option_id, sr.points
            from (values
                ('objetivo',    v_caso.r_objetivo),
                ('horizonte',   v_caso.r_horizonte),
                ('liquidez',    v_caso.r_liquidez),
                ('tolerancia',  v_caso.r_tolerancia),
                ('preferencia', v_caso.r_preferencia)
            ) as a(q_code, o_code)
            join public.questions q        on q.code = a.q_code
            join public.question_options o on o.question_id = q.id and o.code = a.o_code
            join public.scoring_rules sr   on sr.question_option_id = o.id
                                          and sr.rules_version_id = v_rv_id
        loop
            insert into public.profiling_answers (session_id, question_id, option_id, points_awarded)
            values (v_session_id, v_resp.question_id, v_resp.option_id, v_resp.points);
            v_total := v_total + v_resp.points;
        end loop;

        -- 4) Perfil: el umbral lo decide profile_thresholds
        select rp.id into v_profile_id
        from public.profile_thresholds pt
        join public.risk_profiles rp on rp.id = pt.risk_profile_id
        where pt.rules_version_id = v_rv_id
          and v_total between pt.min_score and pt.max_score;

        if v_profile_id is null then
            raise exception 'El puntaje % de % no cae en ningún umbral. Revisa profile_thresholds.',
                v_total, v_caso.nombre;
        end if;

        update public.profiling_sessions
        set total_score = v_total, risk_profile_id = v_profile_id,
            completed_at = now() - interval '2 hours'
        where id = v_session_id;

        -- 5) Propuesta = snapshot de la plantilla + los montos en USD
        select at.id, at.expected_risk into v_template_id, v_risk
        from public.allocation_templates at
        where at.rules_version_id = v_rv_id and at.risk_profile_id = v_profile_id;

        insert into public.proposals
            (session_id, template_id, expected_risk, total_amount, status, created_at)
        values (v_session_id, v_template_id, v_risk, v_caso.monto,
                v_caso.estado::proposal_status, now() - interval '2 hours')
        returning id into v_proposal_id;

        -- El USD de cada línea lo calcula Postgres. El LLM jamás toca este número.
        insert into public.proposal_items (proposal_id, instrument_id, percentage, amount)
        select v_proposal_id, ati.instrument_id, ati.percentage,
               round(v_caso.monto * ati.percentage / 100, 2)
        from public.allocation_template_items ati
        where ati.template_id = v_template_id;

        -- 6) Explicación determinista (la que usa el sistema si Gemini falla o
        --    si el guardarraíl rechaza su texto). Solo cita números reales.
        update public.proposals
        set explanation = (
            select format(
                'Hola %s: tu perfil es %s con %s de 15 puntos, calculado con las reglas v1. ' ||
                'Sobre un monto de USD %s te proponemos una cartera de riesgo %s: %s. ' ||
                'Todos los emisores cumplen la calificación mínima que tu perfil admite. ' ||
                'Esta propuesta no constituye una orden de compra ni una promesa de rentabilidad, ' ||
                'y será revisada por un asesor autorizado antes de considerarse final.',
                v_caso.nombre,
                rp.name,
                v_total,
                -- Separador de miles con punto (USD 20.000), como en el documento del
                -- reto y como se escribe en Ecuador. to_char da coma; se reemplaza.
                replace(to_char(v_caso.monto, 'FM999,999,990'), ',', '.'),
                v_risk,
                string_agg(
                    format('%s%% (USD %s) en %s de %s (%s)',
                           -- 'FM990.99' sobre 60.00 devuelve "60." con un punto colgando.
                           trim(trailing '.' from to_char(pi.percentage, 'FM999990.99')),
                           replace(to_char(pi.amount, 'FM999,999,990'), ',', '.'),
                           i.name, inst.name, inst.credit_rating),
                    '; ' order by pi.percentage desc)
            )
            from public.proposal_items pi
            join public.instruments i    on i.id = pi.instrument_id
            join public.institutions inst on inst.id = i.institution_id
            join public.risk_profiles rp on rp.id = v_profile_id
            where pi.proposal_id = v_proposal_id
            group by rp.name
        )
        where id = v_proposal_id;

        insert into public.audit_log (entity_type, entity_id, actor_id, action, metadata, platform, created_at)
        values ('proposal', v_proposal_id, v_investor_id, 'created',
                jsonb_build_object('puntaje', v_total, 'monto', v_caso.monto, 'rules_version', 'v1'),
                'mobile', now() - interval '2 hours');

        -- 7) Solo Carlos ya fue revisado: alimenta la pantalla de Auditoría
        if v_caso.estado = 'approved' then
            insert into public.advisor_reviews
                (proposal_id, advisor_id, decision, comments, rules_version_id, decided_at)
            values (v_proposal_id, v_advisor_id, 'approved',
                    'Perfil coherente: horizonte corto, necesidad probable de liquidez y baja tolerancia. Cartera conservadora en emisores AAA. Aprobada sin cambios.',
                    v_rv_id, now() - interval '1 hour');

            insert into public.audit_log (entity_type, entity_id, actor_id, action, metadata, platform, created_at)
            values ('advisor_review', v_proposal_id, v_advisor_id, 'approved',
                    jsonb_build_object('decision', 'approved', 'rules_version', 'v1'),
                    'web', now() - interval '1 hour');
        end if;

        -- 8) Rastro del agente para Juan: llm_interactions no arranca vacía y
        --    los "source chips" tienen un ejemplo real que renderizar.
        if v_caso.cedula = '0912345678' then
            insert into public.llm_interactions
                (session_id, proposal_id, role, content, thread_id, metadata,
                 guardrail_passed, model, platform)
            values
                (v_session_id, v_proposal_id, 'user',
                 '¿Por qué me recomiendan un depósito a plazo fijo y no solo el fondo?',
                 v_session_id::text, null, null, null, 'mobile'),

                (v_session_id, v_proposal_id, 'assistant',
                 'Porque tu perfil es Moderado (12 de 15 puntos) y la plantilla moderada destina 60% a un depósito a plazo fijo, que preserva el capital con una tasa pactada de antemano. El 40% restante va al Fondo Balanceado, que es el componente de crecimiento. Ambos emisores tienen calificación AAA, la máxima que exige tu perfil.',
                 v_session_id::text,
                 jsonb_build_object(
                     'sources', jsonb_build_array(
                         jsonb_build_object('table', 'proposal_items',            'record_id', v_proposal_id,
                                            'label', 'DPF Pichincha · 60% · USD 12.000'),
                         jsonb_build_object('table', 'profile_thresholds',        'record_id', v_rv_id,
                                            'label', 'reglas v1 · umbral moderado 9–12'),
                         jsonb_build_object('table', 'profile_institution_rules', 'record_id', v_rv_id,
                                            'label', 'moderado · admite hasta AA')),
                     'tool_calls', jsonb_build_array('get_mi_propuesta', 'get_reglas')),
                 true, 'gemini-2.0-flash', 'mobile');
        end if;
    end loop;
end $$;


-- =====================================================================
-- H. VISTAS DE CONVENIENCIA
-- =====================================================================

-- Se dropean, no se reemplazan: `create or replace view` exige que las columnas
-- existentes conserven nombre y posición, y aquí se agregan columnas intermedias
-- (amount, institución, calificación). Con `replace` falla con
-- "cannot change name of view column".
drop view if exists public.v_advisor_review_queue      cascade;
drop view if exists public.v_investor_proposal_summary cascade;
drop view if exists public.v_profiling_breakdown       cascade;
drop view if exists public.v_audit_timeline            cascade;
drop view if exists public.v_template_integrity        cascade;
drop view if exists public.v_institution_eligibility   cascade;

-- Cola del asesor (HU3). Ahora con monto.
create view public.v_advisor_review_queue as
select
    p.id            as proposal_id,
    p.status,
    p.expected_risk,
    p.total_amount,
    p.explanation,
    p.created_at    as proposal_created_at,
    s.id            as session_id,
    s.total_score,
    rp.name         as risk_profile_name,
    inv.id          as investor_id,
    inv.full_name   as investor_name,
    inv.cedula_ruc
from public.proposals p
join public.profiling_sessions s  on s.id = p.session_id
join public.profiles inv          on inv.id = s.investor_id
left join public.risk_profiles rp on rp.id = s.risk_profile_id
where p.status = 'pending_review';

-- Propuesta del inversionista (HU2). Una fila por producto, con emisor y calificación.
create view public.v_investor_proposal_summary as
select
    s.investor_id,
    s.id            as session_id,
    s.total_score,
    rp.name         as risk_profile_name,
    p.id            as proposal_id,
    p.status,
    p.expected_risk,
    p.total_amount,
    p.explanation,
    i.code          as instrument_code,
    i.name          as instrument_name,
    i.product_type,
    i.risk_class,
    i.expected_return,
    i.term_days,
    pi.percentage,
    pi.amount,
    inst.name          as institution_name,
    inst.credit_rating as institution_rating,
    inst.rating_source as institution_rating_source,
    inst.rating_date   as institution_rating_date
from public.profiling_sessions s
join public.risk_profiles rp   on rp.id = s.risk_profile_id
join public.proposals p        on p.session_id = s.id
join public.proposal_items pi  on pi.proposal_id = p.id
join public.instruments i      on i.id = pi.instrument_id
join public.institutions inst  on inst.id = i.institution_id;

-- HU1, criterio 3: "el usuario entiende cómo influyó cada respuesta".
-- Esta vista ES la pantalla ComoSeCalculoPage. El front solo la pinta.
create view public.v_profiling_breakdown as
select
    s.id             as session_id,
    s.investor_id,
    s.total_score,
    s.amount,
    rv.version_label as rules_version,
    rp.code          as risk_profile_code,
    rp.name          as risk_profile_name,
    q.order_index,
    q.code           as question_code,
    q.text           as question_text,
    o.code           as option_code,
    o.label          as option_label,
    a.points_awarded,
    pt.min_score     as profile_min_score,
    pt.max_score     as profile_max_score,
    pir.max_rating_tier,
    pir.rationale    as institution_rule
from public.profiling_sessions s
join public.rules_versions rv          on rv.id = s.rules_version_id
join public.profiling_answers a        on a.session_id = s.id
join public.questions q                on q.id = a.question_id
join public.question_options o         on o.id = a.option_id
left join public.risk_profiles rp      on rp.id = s.risk_profile_id
left join public.profile_thresholds pt on pt.rules_version_id = s.rules_version_id
                                      and pt.risk_profile_id  = s.risk_profile_id
left join public.profile_institution_rules pir on pir.rules_version_id = s.rules_version_id
                                              and pir.risk_profile_id  = s.risk_profile_id;

-- HU3, criterio 3: "cada decisión queda registrada con fecha, versión y responsable".
create view public.v_audit_timeline as
select
    al.id, al.created_at, al.entity_type, al.entity_id,
    al.action, al.platform, al.metadata,
    actor.full_name as actor_name,
    actor.role      as actor_role
from public.audit_log al
left join public.profiles actor on actor.id = al.actor_id
order by al.created_at desc;

-- Test 1: toda plantilla suma exactamente 100%.
create view public.v_template_integrity as
select
    at.id            as template_id,
    at.name,
    rv.version_label as rules_version,
    sum(ati.percentage)          as total_percentage,
    (sum(ati.percentage) = 100)  as is_valid
from public.allocation_templates at
join public.rules_versions rv             on rv.id = at.rules_version_id
join public.allocation_template_items ati on ati.template_id = at.id
group by at.id, at.name, rv.version_label;

-- Test 2 ⭐: todo producto de una plantilla proviene de una institución cuya
-- calificación cumple la regla del perfil. Un solo assert demuestra que la
-- recomendación tiene un criterio objetivo y verificable.
create view public.v_institution_eligibility as
select
    at.name          as template_name,
    rp.code          as risk_profile_code,
    i.code           as instrument_code,
    inst.name        as institution_name,
    inst.credit_rating,
    inst.rating_tier,
    pir.max_rating_tier,
    (inst.rating_tier <= pir.max_rating_tier) as is_eligible
from public.allocation_templates at
join public.risk_profiles rp              on rp.id = at.risk_profile_id
join public.allocation_template_items ati on ati.template_id = at.id
join public.instruments i                 on i.id = ati.instrument_id
join public.institutions inst             on inst.id = i.institution_id
join public.profile_institution_rules pir on pir.rules_version_id = at.rules_version_id
                                         and pir.risk_profile_id  = at.risk_profile_id;


-- =====================================================================
-- I. VERIFICACIÓN — todo debe decir OK
-- =====================================================================

select 'usuarios con password' as chequeo,
       count(*)::text || ' de 5' as valor,
       case when count(*) = 5 then 'OK' else 'FALTA' end as estado
from public.profiles where password_hash is not null

union all
select 'plantillas suman 100%',
       count(*) filter (where is_valid)::text || ' de ' || count(*)::text,
       case when count(*) = 3 and count(*) = count(*) filter (where is_valid)
            then 'OK' else 'ERROR' end
from public.v_template_integrity

union all
select '⭐ elegibilidad por calificación',
       count(*) filter (where is_eligible)::text || ' de ' || count(*)::text || ' productos',
       case when count(*) = count(*) filter (where is_eligible) then 'OK' else 'ERROR' end
from public.v_institution_eligibility

union all
select '⭐ caso Juan Pérez = Moderado 12',
       coalesce(max(s.total_score)::text, '—') || ' pts · ' || coalesce(max(rp.name), '—'),
       case when max(s.total_score) = 12 and max(rp.code) = 'moderado' then 'OK' else 'ERROR' end
from public.profiling_sessions s
join public.profiles p        on p.id = s.investor_id
join public.risk_profiles rp  on rp.id = s.risk_profile_id
where p.cedula_ruc = '0912345678'

union all
select '⭐ Juan: USD 12.000 + USD 8.000',
       string_agg(replace(to_char(pi.amount, 'FM999,999,990'), ',', '.'), ' + ' order by pi.amount desc),
       case when sum(pi.amount) = 20000 and count(*) = 2 then 'OK' else 'ERROR' end
from public.proposal_items pi
join public.proposals pr        on pr.id = pi.proposal_id
join public.profiling_sessions s on s.id = pr.session_id
join public.profiles p          on p.id = s.investor_id
where p.cedula_ruc = '0912345678'

union all
select 'cola del asesor (pending)',
       count(*)::text || ' propuestas',
       case when count(*) = 2 then 'OK' else 'FALTA' end
from public.v_advisor_review_queue

union all
select 'decisiones de asesor',
       count(*)::text || ' revisiones',
       case when count(*) >= 1 then 'OK' else 'FALTA' end
from public.advisor_reviews

union all
select 'eventos de auditoría',
       count(*)::text || ' eventos',
       case when count(*) >= 4 then 'OK' else 'FALTA' end
from public.v_audit_timeline

union all
select 'interacciones del LLM',
       count(*)::text || ' turnos',
       case when count(*) >= 2 then 'OK' else 'FALTA' end
from public.llm_interactions;

-- =====================================================================
-- FIN
-- =====================================================================
