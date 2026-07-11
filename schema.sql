-- =====================================================================
-- ROBO-ADVISORY — ESQUEMA FINAL COMPLETO (correr este único archivo)
-- Hackathon Agentes Financieros IA — Track 3
-- Motor: PostgreSQL (Supabase) | Arquitectura: RN/Web -> FastAPI -> Supabase
-- Incluye: tablas + índices + vistas + RLS lock-down + seed + multi-cliente
-- Uso: Supabase -> SQL Editor -> pegar todo -> Run
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. EXTENSIONES
-- ---------------------------------------------------------------------
create extension if not exists "pgcrypto";   -- gen_random_uuid()

-- ---------------------------------------------------------------------
-- 1. TIPOS ENUM
-- ---------------------------------------------------------------------
create type user_role         as enum ('investor', 'advisor');
create type proposal_status   as enum ('pending_review', 'approved', 'edited', 'rejected');
create type review_decision   as enum ('approved', 'edited', 'rejected');
create type risk_level        as enum ('bajo', 'medio', 'alto');

-- ---------------------------------------------------------------------
-- 2. USUARIOS (auth manejado por el backend FastAPI)
-- ---------------------------------------------------------------------
create table public.profiles (
    id            uuid primary key default gen_random_uuid(),
    auth_user_id  uuid unique,              -- opcional: enlazar a auth.users si algún día usan Supabase Auth
    role          user_role not null,
    full_name     text not null,
    cedula_ruc    text unique,              -- validación de formato la hace FastAPI/tests
    email         text unique,
    password_hash text,                     -- login demo gestionado por FastAPI (bcrypt); null si no aplica
    is_active     boolean not null default true,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

comment on table public.profiles is 'Usuario (inversionista o asesor). Autenticación gestionada por el backend Python.';

-- ---------------------------------------------------------------------
-- 3. CATÁLOGO: REGLAS DE PERFILAMIENTO (versionadas)
-- ---------------------------------------------------------------------
create table public.rules_versions (
    id              uuid primary key default gen_random_uuid(),
    version_label   text not null unique,        -- ej. 'v1', 'v2'
    description     text,
    is_active       boolean not null default true,
    created_at      timestamptz not null default now()
);

create table public.questions (
    id           uuid primary key default gen_random_uuid(),
    code         text not null unique,            -- ej. 'objetivo', 'horizonte', 'tolerancia'
    text         text not null,
    order_index  integer not null default 0,
    is_active    boolean not null default true
);

create table public.question_options (
    id           uuid primary key default gen_random_uuid(),
    question_id  uuid not null references public.questions(id) on delete cascade,
    code         text not null,                   -- ej. 'corto_plazo'
    label        text not null,
    order_index  integer not null default 0,
    unique (question_id, code)
);

create table public.scoring_rules (
    id                  uuid primary key default gen_random_uuid(),
    rules_version_id    uuid not null references public.rules_versions(id) on delete cascade,
    question_option_id  uuid not null references public.question_options(id) on delete cascade,
    points              integer not null,
    unique (rules_version_id, question_option_id)
);

comment on table public.scoring_rules is 'Puntos por opción de respuesta, por versión de reglas. Fuente de verdad del puntaje (nunca el LLM).';

-- ---------------------------------------------------------------------
-- 4. CATÁLOGO: PERFILES DE RIESGO Y UMBRALES (versionados)
-- ---------------------------------------------------------------------
create table public.risk_profiles (
    id           uuid primary key default gen_random_uuid(),
    code         text not null unique,   -- 'conservador' | 'moderado' | 'agresivo'
    name         text not null,
    description  text
);

create table public.profile_thresholds (
    id                 uuid primary key default gen_random_uuid(),
    rules_version_id   uuid not null references public.rules_versions(id) on delete cascade,
    risk_profile_id    uuid not null references public.risk_profiles(id) on delete cascade,
    min_score          integer not null,
    max_score          integer not null,
    unique (rules_version_id, risk_profile_id),
    check (max_score >= min_score)
);

comment on table public.profile_thresholds is 'Rango de puntaje que determina el perfil, por versión de reglas.';

-- ---------------------------------------------------------------------
-- 5. CATÁLOGO: INSTRUMENTOS Y PLANTILLAS DE ASIGNACIÓN
-- ---------------------------------------------------------------------
create table public.instruments (
    id               uuid primary key default gen_random_uuid(),
    code             text not null unique,        -- ej. 'ETF_SP500'
    name             text not null,
    asset_class      text not null,               -- 'renta_fija' | 'renta_variable' | 'etf' | 'bono' ...
    risk_class       risk_level not null,
    expected_return  numeric(6,3),                 -- ficticio, solo demo
    description      text,
    is_active        boolean not null default true
);

create table public.allocation_templates (
    id                 uuid primary key default gen_random_uuid(),
    rules_version_id   uuid not null references public.rules_versions(id) on delete cascade,
    risk_profile_id    uuid not null references public.risk_profiles(id) on delete cascade,
    name               text not null,
    expected_risk      risk_level not null,
    created_at         timestamptz not null default now(),
    unique (rules_version_id, risk_profile_id)
);

create table public.allocation_template_items (
    id            uuid primary key default gen_random_uuid(),
    template_id   uuid not null references public.allocation_templates(id) on delete cascade,
    instrument_id uuid not null references public.instruments(id) on delete restrict,
    percentage    numeric(5,2) not null check (percentage > 0 and percentage <= 100),
    unique (template_id, instrument_id)
);

comment on table public.allocation_template_items is 'Composición fija por instrumento de cada plantilla (% determinista, no generado por el LLM).';

-- ---------------------------------------------------------------------
-- 6. TRANSACCIONAL: SESIÓN DE PERFILAMIENTO (HU1)
-- ---------------------------------------------------------------------
create table public.profiling_sessions (
    id                uuid primary key default gen_random_uuid(),
    investor_id       uuid not null references public.profiles(id) on delete cascade,
    rules_version_id  uuid not null references public.rules_versions(id),
    total_score       integer,
    risk_profile_id   uuid references public.risk_profiles(id),
    created_at        timestamptz not null default now(),
    completed_at      timestamptz
);

create table public.profiling_answers (
    id           uuid primary key default gen_random_uuid(),
    session_id   uuid not null references public.profiling_sessions(id) on delete cascade,
    question_id  uuid not null references public.questions(id),
    option_id    uuid not null references public.question_options(id),
    points_awarded integer not null,
    answered_at  timestamptz not null default now(),
    unique (session_id, question_id)
);

comment on table public.profiling_answers is 'Respuesta + puntos otorgados por pregunta. Permite mostrar al usuario cómo influyó cada respuesta.';

-- ---------------------------------------------------------------------
-- 7. TRANSACCIONAL: PROPUESTA DE PORTAFOLIO (HU2)
-- ---------------------------------------------------------------------
create table public.proposals (
    id               uuid primary key default gen_random_uuid(),
    session_id       uuid not null references public.profiling_sessions(id) on delete cascade,
    template_id      uuid not null references public.allocation_templates(id),
    expected_risk    risk_level not null,
    explanation      text,              -- texto legible generado a partir de datos deterministas
    status           proposal_status not null default 'pending_review',
    created_at       timestamptz not null default now()
);

create table public.proposal_items (
    id            uuid primary key default gen_random_uuid(),
    proposal_id   uuid not null references public.proposals(id) on delete cascade,
    instrument_id uuid not null references public.instruments(id),
    percentage    numeric(5,2) not null check (percentage > 0 and percentage <= 100),
    unique (proposal_id, instrument_id)
);

comment on table public.proposals is 'Snapshot de la propuesta generada; no ejecuta órdenes ni promete rentabilidad.';

-- ---------------------------------------------------------------------
-- 8. TRANSACCIONAL: REVISIÓN DEL ASESOR (HU3)
-- ---------------------------------------------------------------------
create table public.advisor_reviews (
    id                 uuid primary key default gen_random_uuid(),
    proposal_id        uuid not null references public.proposals(id) on delete cascade,
    advisor_id         uuid not null references public.profiles(id),
    decision           review_decision not null,
    comments           text,
    rules_version_id   uuid not null references public.rules_versions(id),
    edited_allocation  jsonb,             -- snapshot del portafolio si decision = 'edited'
    decided_at         timestamptz not null default now()
);

comment on table public.advisor_reviews is 'Decisión del asesor con fecha, versión de reglas y responsable — log de auditoría de negocio.';

-- ---------------------------------------------------------------------
-- 9. AUDITORÍA GENÉRICA (diferenciador extra)
-- ---------------------------------------------------------------------
create table public.audit_log (
    id           uuid primary key default gen_random_uuid(),
    entity_type  text not null,          -- 'profiling_session' | 'proposal' | 'advisor_review'
    entity_id    uuid not null,
    actor_id     uuid references public.profiles(id),
    action       text not null,          -- 'created' | 'viewed' | 'updated' | 'approved' | ...
    metadata     jsonb,
    created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- 10. (OPCIONAL) LOG DE INTERACCIONES CON EL LLM
-- ---------------------------------------------------------------------
create table public.llm_interactions (
    id            uuid primary key default gen_random_uuid(),
    session_id    uuid references public.profiling_sessions(id) on delete set null,
    proposal_id   uuid references public.proposals(id) on delete set null,
    role          text not null,          -- 'system' | 'user' | 'assistant'
    content       text not null,
    created_at    timestamptz not null default now()
);

comment on table public.llm_interactions is 'Evidencia de que el LLM solo conversa/explica; los números vienen de las tablas deterministas.';

-- =====================================================================
-- 11. ÍNDICES
-- =====================================================================
create index idx_question_options_question   on public.question_options(question_id);
create index idx_scoring_rules_version       on public.scoring_rules(rules_version_id);
create index idx_profiling_sessions_investor on public.profiling_sessions(investor_id);
create index idx_profiling_answers_session   on public.profiling_answers(session_id);
create index idx_proposals_session           on public.proposals(session_id);
create index idx_proposal_items_proposal     on public.proposal_items(proposal_id);
create index idx_advisor_reviews_proposal    on public.advisor_reviews(proposal_id);
create index idx_audit_log_entity            on public.audit_log(entity_type, entity_id);
create index idx_allocation_items_template   on public.allocation_template_items(template_id);

-- =====================================================================
-- 12. TRIGGER updated_at (solo profiles por ahora)
-- =====================================================================
create or replace function public.set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trg_profiles_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

-- =====================================================================
-- 13. VISTAS DE CONVENIENCIA
-- =====================================================================

-- Cola de revisión para el panel del asesor
create or replace view public.v_advisor_review_queue as
select
    p.id            as proposal_id,
    p.status,
    p.expected_risk,
    p.explanation,
    p.created_at    as proposal_created_at,
    s.id            as session_id,
    s.total_score,
    rp.name         as risk_profile_name,
    inv.full_name   as investor_name,
    inv.cedula_ruc
from public.proposals p
join public.profiling_sessions s on s.id = p.session_id
join public.profiles inv         on inv.id = s.investor_id
left join public.risk_profiles rp on rp.id = s.risk_profile_id
where p.status = 'pending_review';

-- Resumen de propuesta para el inversionista
create or replace view public.v_investor_proposal_summary as
select
    s.investor_id,
    s.id           as session_id,
    s.total_score,
    rp.name        as risk_profile_name,
    p.id           as proposal_id,
    p.status,
    p.expected_risk,
    p.explanation,
    pi.instrument_id,
    i.name         as instrument_name,
    pi.percentage
from public.profiling_sessions s
join public.risk_profiles rp   on rp.id = s.risk_profile_id
join public.proposals p        on p.session_id = s.id
join public.proposal_items pi  on pi.proposal_id = p.id
join public.instruments i      on i.id = pi.instrument_id;

-- =====================================================================
-- 14. ROW LEVEL SECURITY (modo lock-down)
-- =====================================================================
-- El backend FastAPI se conecta con la service_role key (o el connection
-- string de Postgres), que BYPASEA RLS. Activamos RLS en todas las tablas
-- SIN crear políticas: así, cualquier intento de acceso directo desde un
-- cliente con la anon key es rechazado. FastAPI es el único punto de
-- entrada a los datos — la autorización por rol (inversionista/asesor)
-- se aplica en la capa de API.

alter table public.profiles                  enable row level security;
alter table public.profiling_sessions        enable row level security;
alter table public.profiling_answers         enable row level security;
alter table public.proposals                 enable row level security;
alter table public.proposal_items            enable row level security;
alter table public.advisor_reviews           enable row level security;
alter table public.audit_log                 enable row level security;
alter table public.llm_interactions          enable row level security;
alter table public.rules_versions            enable row level security;
alter table public.questions                 enable row level security;
alter table public.question_options          enable row level security;
alter table public.scoring_rules             enable row level security;
alter table public.risk_profiles             enable row level security;
alter table public.profile_thresholds        enable row level security;
alter table public.instruments               enable row level security;
alter table public.allocation_templates      enable row level security;
alter table public.allocation_template_items enable row level security;

-- (Sin políticas = nadie con anon/authenticated key puede leer o escribir.
--  Si en el futuro la app móvil usara Supabase directamente, aquí se
--  agregarían políticas basadas en auth.uid().)

-- =====================================================================
-- 15. SEED DATA MÍNIMO (rules_v1, perfiles, catálogo demo)
-- =====================================================================

-- Usuarios demo (el password_hash real lo genera FastAPI con bcrypt;
-- estos placeholders se pueden actualizar con: update profiles set password_hash = ...)
insert into public.profiles (role, full_name, cedula_ruc, email, password_hash) values
  ('investor', 'Inversionista Demo', '0999999999', 'inversionista@demo.ec', null),
  ('advisor',  'Asesor Demo',        '0888888888', 'asesor@demo.ec',        null);

insert into public.rules_versions (version_label, description, is_active)
values ('v1', 'Reglas iniciales de perfilamiento', true);

insert into public.risk_profiles (code, name, description) values
  ('conservador', 'Conservador', 'Prioriza preservación de capital'),
  ('moderado',    'Moderado',    'Balance entre riesgo y crecimiento'),
  ('agresivo',    'Agresivo',    'Prioriza crecimiento, tolera volatilidad');

-- Preguntas
insert into public.questions (code, text, order_index) values
  ('objetivo',   '¿Cuál es tu objetivo principal de inversión?', 1),
  ('horizonte',  '¿Cuál es tu horizonte de inversión?', 2),
  ('tolerancia', '¿Cómo reaccionarías ante una caída del 15% en tu portafolio?', 3);

-- Opciones + puntajes v1 (usando CTE para mantener el script auto-contenido)
with q as (
  select id, code from public.questions
),
opt as (
  insert into public.question_options (question_id, code, label, order_index)
  select q.id, v.code, v.label, v.order_index
  from (values
    ('objetivo',   'preservar',   'Preservar mi capital', 1),
    ('objetivo',   'balancear',   'Balancear riesgo y crecimiento', 2),
    ('objetivo',   'crecer',      'Maximizar crecimiento', 3),
    ('horizonte',  'corto',       'Menos de 2 años', 1),
    ('horizonte',  'medio',       '2 a 5 años', 2),
    ('horizonte',  'largo',       'Más de 5 años', 3),
    ('tolerancia', 'vender',      'Vendería todo de inmediato', 1),
    ('tolerancia', 'esperar',     'Esperaría a que se recupere', 2),
    ('tolerancia', 'comprar_mas', 'Compraría más aprovechando el precio bajo', 3)
  ) as v(qcode, code, label, order_index)
  join q on q.code = v.qcode
  returning id, question_id, code
)
insert into public.scoring_rules (rules_version_id, question_option_id, points)
select rv.id, opt.id,
  case opt.code
    when 'preservar'   then 1  when 'balancear'   then 2  when 'crecer'       then 3
    when 'corto'       then 1  when 'medio'       then 2  when 'largo'        then 3
    when 'vender'      then 1  when 'esperar'     then 2  when 'comprar_mas'  then 3
  end
from opt, (select id from public.rules_versions where version_label = 'v1') rv;

-- Umbrales de perfil (rango 3-9 posible con 3 preguntas de 1-3 pts)
insert into public.profile_thresholds (rules_version_id, risk_profile_id, min_score, max_score)
select rv.id, rp.id, v.min_score, v.max_score
from public.rules_versions rv
cross join (values
  ('conservador', 3, 4),
  ('moderado',    5, 7),
  ('agresivo',    8, 9)
) as v(code, min_score, max_score)
join public.risk_profiles rp on rp.code = v.code
where rv.version_label = 'v1';

-- Catálogo de instrumentos (ficticio)
insert into public.instruments (code, name, asset_class, risk_class, expected_return, description) values
  ('BONO_GOB',     'Bono Gobierno EC 5Y',      'renta_fija',      'bajo',  4.500, 'Instrumento de deuda soberana, ficticio'),
  ('FONDO_LIQ',    'Fondo de Liquidez',        'renta_fija',      'bajo',  3.200, 'Fondo de bajo riesgo, alta liquidez'),
  ('ETF_BONOS',    'ETF Renta Fija Global',    'etf',             'medio', 5.100, 'ETF diversificado en bonos'),
  ('ETF_SP500',    'ETF S&P 500',              'etf',             'alto',  9.800, 'ETF indexado a renta variable EEUU'),
  ('ACC_TECH',     'Canasta Acciones Tech',    'renta_variable',  'alto', 12.500, 'Canasta ficticia de acciones tecnológicas');

-- Plantillas de asignación por perfil (v1)
with rv as (select id from public.rules_versions where version_label = 'v1')
insert into public.allocation_templates (rules_version_id, risk_profile_id, name, expected_risk)
select rv.id, rp.id, v.name, v.expected_risk::risk_level
from rv, public.risk_profiles rp
join (values
  ('conservador', 'Plantilla Conservadora', 'bajo'),
  ('moderado',    'Plantilla Moderada',     'medio'),
  ('agresivo',    'Plantilla Agresiva',     'alto')
) as v(code, name, expected_risk) on rp.code = v.code;

-- Composición de cada plantilla
with t as (
  select at.id as template_id, rp.code as profile_code
  from public.allocation_templates at
  join public.risk_profiles rp on rp.id = at.risk_profile_id
),
i as (select id, code from public.instruments)
insert into public.allocation_template_items (template_id, instrument_id, percentage)
select t.template_id, i.id, v.pct
from (values
  ('conservador', 'BONO_GOB',  50.00),
  ('conservador', 'FONDO_LIQ', 20.00),
  ('conservador', 'ETF_BONOS', 30.00),
  ('moderado',    'BONO_GOB',  20.00),
  ('moderado',    'ETF_BONOS', 40.00),
  ('moderado',    'ETF_SP500', 40.00),
  ('agresivo',    'ETF_BONOS', 20.00),
  ('agresivo',    'ETF_SP500', 50.00),
  ('agresivo',    'ACC_TECH',  30.00)
) as v(profile_code, instrument_code, pct)
join t on t.profile_code = v.profile_code
join i on i.code = v.instrument_code;

-- =====================================================================
-- FIN DEL SCRIPT
-- =====================================================================

-- =====================================================================
-- MIGRACIÓN v2.1 — Soporte multi-cliente (móvil hoy, web después)
-- Aditiva: se corre DESPUÉS de schema_v2.sql sin afectar lo existente.
-- =====================================================================

-- Plataforma desde la que se origina cada sesión/evento
create type client_platform as enum ('mobile', 'web', 'api', 'other');

-- ---------------------------------------------------------------------
-- Sesiones de autenticación (refresh tokens) por dispositivo/cliente
-- ---------------------------------------------------------------------
create table public.auth_sessions (
    id                 uuid primary key default gen_random_uuid(),
    profile_id         uuid not null references public.profiles(id) on delete cascade,
    refresh_token_hash text not null unique,     -- hash del refresh token (nunca el token en claro)
    platform           client_platform not null default 'other',
    user_agent         text,
    ip_address         inet,
    created_at         timestamptz not null default now(),
    expires_at         timestamptz not null,
    revoked_at         timestamptz               -- null = sesión activa
);

create index idx_auth_sessions_profile on public.auth_sessions(profile_id);
create index idx_auth_sessions_active  on public.auth_sessions(profile_id)
    where revoked_at is null;

comment on table public.auth_sessions is
'Sesiones de login por cliente (móvil/web). Permite revocar dispositivos y auditar accesos.';

-- ---------------------------------------------------------------------
-- Plataforma de origen en auditoría e interacciones LLM
-- ---------------------------------------------------------------------
alter table public.audit_log
    add column platform client_platform not null default 'other';

alter table public.llm_interactions
    add column platform client_platform not null default 'other';

-- ---------------------------------------------------------------------
-- RLS lock-down (consistente con el resto del esquema)
-- ---------------------------------------------------------------------
alter table public.auth_sessions enable row level security;

-- =====================================================================
-- FIN
-- =====================================================================
