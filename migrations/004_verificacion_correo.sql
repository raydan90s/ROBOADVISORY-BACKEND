-- Fase 5: el correo del registro tiene que EXISTIR.
--
-- Hasta ahora `profiles.email` era una cadena cualquiera: el registro aceptaba
-- "asdf@asdf.asdf" y nacía una cuenta con la que nadie podía contactar al dueño. Eso
-- rompe dos cosas que sí importan: (1) no hay forma de recuperar una contraseña si el
-- buzón no es real, y (2) un tercero puede registrar el correo de otra persona.
--
-- Un formato válido (EmailStr en Pydantic) prueba que la cadena PARECE un correo, no
-- que alguien lo lea. Lo único que prueba eso es mandar un secreto ahí y pedir que lo
-- devuelvan: el código de seis dígitos de `auth_codes`.
--
-- Mismo mecanismo, dos propósitos:
--   - 'email_verification' → la cuenta nace bloqueada; el código la activa (y deja
--     logueado al usuario, así el registro no pierde el hilo).
--   - 'password_reset'     → probar que tienes el buzón ES la autorización para
--     cambiar la contraseña sin conocer la anterior.
--
-- El código va en claro, igual que `whatsapp_link_codes.code`: vive 15 minutos, muere
-- al usarse o al quinto intento fallido, y quien pueda leer esta tabla ya entró como
-- `postgres` — a esa altura ya tiene el `password_hash`, así que hashear el código no
-- compraría nada.


-- NULL = correo sin verificar. Nullable y no un boolean con default para que la fila
-- guarde CUÁNDO se verificó: es el rastro que pide la auditoría.
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;

-- Las cuentas que ya existen (seed: juan@demo.ec, asesor@demo.ec, y lo que haya creado
-- el registro viejo) nacieron antes de que existiera esta regla. Se dan por verificadas:
-- si no, el login de la demo se cae en seco y nadie puede entrar a arreglarlo.
UPDATE public.profiles
   SET email_verified_at = now()
 WHERE email_verified_at IS NULL;


CREATE TABLE IF NOT EXISTS public.auth_codes (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  purpose    text NOT NULL CHECK (purpose IN ('email_verification', 'password_reset')),
  code       text NOT NULL CHECK (code ~ '^[0-9]{6}$'),
  -- Seis dígitos son un millón de combinaciones: sin un tope de intentos, un script las
  -- prueba todas en minutos. Con 5, la probabilidad de acertar a ciegas es 5 en 1e6.
  attempts   int  NOT NULL DEFAULT 0,
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- UN código vivo por cuenta y propósito. Sin esto, pedir "reenviar" tres veces dejaría
-- tres códigos válidos y el verificador tendría que elegir uno: el reenvío mata al
-- anterior (used_at = now()) y este índice garantiza que la elección nunca es ambigua.
CREATE UNIQUE INDEX IF NOT EXISTS auth_codes_vivo
  ON public.auth_codes (profile_id, purpose) WHERE used_at IS NULL;

CREATE INDEX IF NOT EXISTS auth_codes_profile
  ON public.auth_codes (profile_id, created_at DESC);
