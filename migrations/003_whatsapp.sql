-- Fase 4: el asistente por WhatsApp.
--
-- El bot habla de dinero: montos, cartera, perfil. Así que la pregunta que resuelve
-- esta migración no es "cómo guardo mensajes" sino **quién está escribiendo**. Un
-- número de teléfono no es una identidad: llega en el webhook de Twilio y cualquiera
-- puede escribir. Por eso el número no se "asocia" al vuelo con un email ni una cédula
-- (datos que un tercero puede conocer), sino con un CÓDIGO DE UN SOLO USO que solo se
-- puede leer estando ya autenticado dentro de la app.
--
-- El flujo, entonces:
--   1. El usuario, logueado en la app, pide un código (POST /api/whatsapp/link-code).
--   2. Escribe «VINCULAR 123456» al número de WhatsApp del banco.
--   3. El webhook canjea el código: nace la fila en whatsapp_links y recién ahí ese
--      teléfono puede preguntar por esa cuenta.
--
-- Un código vive 10 minutos y muere al usarse. Un teléfono apunta a una sola cuenta
-- (y una cuenta tiene un solo teléfono activo): los índices únicos parciales de abajo
-- lo imponen en la base, no en Python — un `if` en la app no sobrevive a dos webhooks
-- concurrentes.

-- 'whatsapp' como origen de primera clase en la auditoría: `llm_interactions.platform`
-- ya distingue mobile/web/api, y las conversaciones del bot tienen que ser
-- distinguibles de las del chat de la app para el criterio de trazabilidad.
ALTER TYPE public.client_platform ADD VALUE IF NOT EXISTS 'whatsapp';


CREATE TABLE IF NOT EXISTS public.whatsapp_links (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id   uuid NOT NULL REFERENCES public.profiles(id),
  -- Formato E.164 sin el prefijo "whatsapp:" de Twilio: +593999999999. Normalizar acá
  -- (y no en cada consulta) es lo que hace que el índice único sirva de algo.
  phone_e164   text NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  linked_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  revoked_at   timestamptz
);

-- Un teléfono activo → UNA cuenta. Es la garantía de que el bot nunca le muestre a un
-- número la cartera de otra persona. Parcial (solo los vivos) para que un número
-- desvinculado pueda volver a vincularse mañana, quizá a otra cuenta.
CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_links_phone_activo
  ON public.whatsapp_links (phone_e164) WHERE revoked_at IS NULL;

-- Y una cuenta → UN teléfono activo. Si el usuario cambia de número, revoca y vuelve
-- a vincular; no se acumulan dos números leyendo la misma cartera.
CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_links_profile_activo
  ON public.whatsapp_links (profile_id) WHERE revoked_at IS NULL;


CREATE TABLE IF NOT EXISTS public.whatsapp_link_codes (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL REFERENCES public.profiles(id),
  code       text NOT NULL CHECK (code ~ '^[0-9]{6}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  -- El teléfono que finalmente lo canjeó. Deja el rastro de quién entró con qué código.
  used_by_phone text
);

-- Dos usuarios no pueden tener el mismo código vivo al mismo tiempo: si pasara, el
-- webhook no sabría a qué cuenta vincular el teléfono y elegiría una. El índice hace
-- que el segundo INSERT falle y el generador reintente con otro código.
CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_link_codes_vivo
  ON public.whatsapp_link_codes (code) WHERE used_at IS NULL;

CREATE INDEX IF NOT EXISTS whatsapp_link_codes_profile
  ON public.whatsapp_link_codes (profile_id, created_at DESC);
