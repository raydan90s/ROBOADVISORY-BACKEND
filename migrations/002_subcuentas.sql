-- Fase 3: soporte de subcuentas.
--
-- Un inversionista declara un capital total (profiles.total_capital) y lo reparte en
-- N subcuentas: cada una es una fila de profiling_sessions con su propio nombre
-- (subaccount_name) y su propio monto (la columna `amount`, que ya existía).
--
-- Regla de oro: "una subcuenta no puede superar el capital sin asignar" se aplica en
-- un trigger, no en Python. Si dos pestañas crean subcuentas para el mismo
-- inversionista al mismo tiempo, un `if` en la aplicación leería "hay espacio" en
-- ambas antes de que ninguna escriba (condición de carrera clásica). El trigger cierra
-- esa ventana bloqueando la fila de `profiles` con `for update`: la segunda
-- transacción espera a que la primera termine y recién ahí ve el total ya
-- actualizado.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS total_capital numeric
    CHECK (total_capital IS NULL OR total_capital > 0);

ALTER TABLE public.profiling_sessions
  ADD COLUMN IF NOT EXISTS subaccount_name text;

CREATE OR REPLACE FUNCTION public.fn_valida_capital_subcuenta()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_total_capital numeric;
  v_asignado_sin_esta numeric;
BEGIN
  -- Sesiones sin monto (HU1/HU2 fuera del flujo de subcuentas) no tienen contra qué
  -- validar: se dejan pasar, igual que se comportaban antes de esta migración.
  IF NEW.amount IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT total_capital INTO v_total_capital
  FROM public.profiles
  WHERE id = NEW.investor_id
  FOR UPDATE;

  -- Sin total_capital declarado (POST /api/investor/capital nunca se llamó) tampoco
  -- hay límite contra qué comparar: son las cuentas de siempre, sin subcuentas.
  IF v_total_capital IS NULL THEN
    RETURN NEW;
  END IF;

  -- El DEFAULT de `id` ya corrió antes de que el trigger vea NEW, así que
  -- `id <> NEW.id` excluye esta misma fila tanto en INSERT como en UPDATE.
  SELECT COALESCE(SUM(amount), 0) INTO v_asignado_sin_esta
  FROM public.profiling_sessions
  WHERE investor_id = NEW.investor_id
    AND id <> NEW.id;

  IF v_asignado_sin_esta + NEW.amount > v_total_capital THEN
    RAISE EXCEPTION
      'La subcuenta (USD %) supera el capital sin asignar (USD % de % declarados).',
      NEW.amount, v_total_capital - v_asignado_sin_esta, v_total_capital;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_valida_capital_subcuenta ON public.profiling_sessions;

CREATE TRIGGER trg_valida_capital_subcuenta
  BEFORE INSERT OR UPDATE OF amount, investor_id ON public.profiling_sessions
  FOR EACH ROW
  EXECUTE FUNCTION public.fn_valida_capital_subcuenta();

-- La cola del asesor (Fase 4) también necesita saber de qué subcuenta salió cada
-- propuesta. `CREATE OR REPLACE VIEW` no permite insertar una columna nueva salvo al
-- final exacto de la lista existente, así que se recrea entera (sin dependientes,
-- verificado contra pg_depend) en vez de arriesgarse a un orden que no calce.
DROP VIEW IF EXISTS public.v_advisor_review_queue;

CREATE VIEW public.v_advisor_review_queue AS
SELECT p.id AS proposal_id,
    p.status,
    p.expected_risk,
    p.total_amount,
    p.explanation,
    p.created_at AS proposal_created_at,
    s.id AS session_id,
    s.total_score,
    rp.name AS risk_profile_name,
    inv.id AS investor_id,
    inv.full_name AS investor_name,
    inv.cedula_ruc,
    s.subaccount_name
   FROM proposals p
     JOIN profiling_sessions s ON s.id = p.session_id
     JOIN profiles inv ON inv.id = s.investor_id
     LEFT JOIN risk_profiles rp ON rp.id = s.risk_profile_id
  WHERE p.status = 'pending_review'::proposal_status;
