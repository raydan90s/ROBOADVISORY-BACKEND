-- 002_subcuentas.sql — subcuentas del inversionista
--
-- Una subcuenta YA existía: se llama `profiling_session`. Tiene dueño (investor_id),
-- monto (amount), perfil (risk_profile_id) y puntaje (total_score), y
-- `create_investor_profile` siempre insertó una nueva en cada llamada — nunca actualizó
-- la existente. Lo único que le faltaba era un nombre y un techo de capital.
--
-- De ahí que esta migración sea aditiva y corta: dos columnas y una columna más en una
-- vista que ya existía. El motor determinista —scoring, umbrales, plantillas,
-- elegibilidad, guardarraíles— no se toca. Es idempotente: se puede correr dos veces.
--
-- ORDEN: va DESPUÉS de schema.sql y seed.sql. `seed.sql` recrea
-- `v_advisor_review_queue` sin `subaccount_name`, así que cada vez que se re-siembre la
-- base hay que volver a correr esta migración — si no, la cola del asesor deja de decir
-- de qué subcuenta viene cada propuesta.

alter table public.profiling_sessions
    add column if not exists subaccount_name text;

alter table public.profiles
    add column if not exists total_capital numeric;

-- El techo de capital no puede ser cero ni negativo: "no declaró capital" es NULL, que
-- es distinto de "declaró cero". La app dibuja los dos casos distinto.
alter table public.profiles
    drop constraint if exists profiles_total_capital_check;

alter table public.profiles
    add constraint profiles_total_capital_check
    check (total_capital is null or total_capital > 0);

-- La cola del asesor (HU3) tiene que decir de qué subcuenta viene cada propuesta: sin
-- eso, tres propuestas del mismo cliente son tres filas indistinguibles.
drop view if exists public.v_advisor_review_queue cascade;

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
    s.subaccount_name,
    rp.name         as risk_profile_name,
    inv.id          as investor_id,
    inv.full_name   as investor_name,
    inv.cedula_ruc
from public.proposals p
join public.profiling_sessions s  on s.id = p.session_id
join public.profiles inv          on inv.id = s.investor_id
left join public.risk_profiles rp on rp.id = s.risk_profile_id
where p.status = 'pending_review';
