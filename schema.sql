-- Pega esto en el SQL Editor de Supabase antes de correr la API.

create table if not exists public.investors (
    id                uuid primary key default gen_random_uuid(),
    nombre            text        not null,
    email             text,
    edad              int,
    horizonte_anios   int,
    monto_inicial     numeric,

    respuestas_riesgo jsonb       not null default '{}'::jsonb,
    puntaje_riesgo    int         not null default 0,
    perfil_riesgo     text        not null default 'moderado',
    estado_propuesta  text        not null default 'pendiente',

    created_at        timestamptz not null default now()
);

-- El backend usa la service_role key, que ignora RLS. Si más adelante llamas a
-- Supabase directo desde la app móvil, activa RLS y escribe políticas aquí.
-- alter table public.investors enable row level security;
