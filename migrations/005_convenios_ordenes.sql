-- Fase 6: el convenio, la orden y la comisión — el modelo de negocio, en la base.
--
-- Hasta acá la app terminaba en la firma del asesor: la propuesta quedaba `approved` y
-- no pasaba nada más. El cliente entendía dónde iba su plata y ahí se acababa el
-- producto. Faltaba lo que sostiene el negocio: Brokeate es el INTERMEDIARIO entre el
-- inversionista y el banco. La app es gratis para el cliente; quien paga es el banco,
-- una prima por cada inversión que entra por acá.
--
-- Tres piezas, y cada una cierra una objeción concreta que nos hicieron:
--
--   1. El CONVENIO (institutions.convenio_*). El catálogo no es "todos los bancos del
--      mundo": es "los bancos con los que tenemos convenio". Es la respuesta a "¿por qué
--      no me aparece Interactive Brokers si ahí gano más?" — porque no hay convenio,
--      todavía. `institution_type` deja crecer el catálogo a entidades que NO son bancos
--      (brokers internacionales) sin que se confundan con las reguladas localmente.
--
--   2. La COMISIÓN (commission_policies). Ver el comentario largo de esa tabla: es la
--      pieza anti-sesgo, y es estructural, no una promesa.
--
--   3. La ORDEN (investment_orders + investment_order_items). Materializa una propuesta
--      YA FIRMADA en N instrucciones, una por banco. Dos pasos: 'sent' → 'confirmed'.
--
-- Nota sobre qué NO hace esta migración: no mueve dinero. La ejecución contra la banca
-- real es una integración con datos sensibles que este proyecto no tiene ni finge tener.
-- `investment_orders.is_simulated` nace en `true` y la app lo dice en pantalla. Lo que sí
-- es real es todo lo demás: la regla de quién puede cursar una orden, contra qué
-- instituciones, y cuánto se cobra por ella.


-- ===========================================================================
-- 1. El convenio
-- ===========================================================================

-- Por qué un CHECK y no un enum: la lista de tipos va a crecer (casas de valores,
-- fintechs, cooperativas de segundo piso) y agregar un valor a un enum en Postgres es
-- una migración con candado; ampliar un CHECK es un ALTER que corre en milisegundos.
-- `institutions.credit_rating` ya vive con la misma lógica y no ha dolido.
ALTER TABLE public.institutions
  ADD COLUMN IF NOT EXISTS institution_type text NOT NULL DEFAULT 'banco'
    CHECK (institution_type IN ('banco', 'cooperativa', 'broker_internacional'));

-- Sin convenio no se puede cursar una orden (lo aplica fn_valida_convenio_item más
-- abajo). Nace en `false` a propósito: un banco que alguien agregue al catálogo mañana
-- NO queda habilitado para recibir plata por el solo hecho de existir en la tabla.
ALTER TABLE public.institutions
  ADD COLUMN IF NOT EXISTS convenio_activo boolean NOT NULL DEFAULT false;

ALTER TABLE public.institutions
  ADD COLUMN IF NOT EXISTS convenio_desde date;

COMMENT ON COLUMN public.institutions.convenio_activo IS
  'Hay convenio vigente con esta institución. Sin él, aparece en el comparador pero no '
  'puede recibir una orden: el catálogo informa, el convenio habilita.';


-- ===========================================================================
-- 2. La comisión
-- ===========================================================================

-- ESTA TABLA ES LA RESPUESTA A "¿CÓMO SÉ QUE NO ME RECOMIENDAS AL QUE MÁS TE PAGA?".
--
-- La objeción es legítima y no se contesta con una promesa: en el momento en que
-- Brokeate cobra por convenio, tiene un incentivo para empujar al banco que mejor le
-- paga. Decir "no lo hacemos" no es verificable. Lo que sí es verificable es que NO SE
-- PUEDA hacer:
--
--   - No hay `institution_id` en esta tabla. La comisión no depende del banco porque no
--     hay dónde escribir un banco. No es que no lo hagamos: no hay columna.
--   - `UNIQUE (rules_version_id)` → UNA sola tasa por versión de reglas. No pueden
--     coexistir dos comisiones distintas.
--
-- El resultado es que a Brokeate le da exactamente igual cuál de los bancos con
-- convenio elija el cliente: cobra lo mismo. La recomendación no puede estar sesgada por
-- la comisión porque la comisión es constante, y eso lo garantiza el esquema, no
-- nuestra buena fe.
--
-- Cuelga de `rules_versions` (y no de una tabla propia de versiones) porque es
-- exactamente la misma disciplina que ya rige el puntaje: una regla publicada, con
-- versión, que las decisiones ya tomadas conservan aunque mañana cambie. `advisor_reviews`
-- ya guarda su `rules_version_id`; las órdenes guardan el suyo y además congelan los bps.
CREATE TABLE IF NOT EXISTS public.commission_policies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rules_version_id uuid NOT NULL REFERENCES public.rules_versions(id),

  -- En puntos básicos y no en porcentaje: 0,50% en `numeric` invita a un redondeo a
  -- medio camino; 50 bps es un entero y no hay nada que redondear.
  -- El techo de 500 bps (5%) no es decorativo: una prima de intermediación por encima de
  -- eso dejaría de ser "muy pequeñita" y el CHECK obliga a discutirlo antes de subirla.
  comision_bps     int  NOT NULL CHECK (comision_bps >= 0 AND comision_bps <= 500),

  -- Por qué esta tasa y no otra. Se muestra al cliente junto con la cifra.
  rationale        text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT commission_policies_una_por_version UNIQUE (rules_version_id)
);

COMMENT ON TABLE public.commission_policies IS
  'La prima que el banco le paga a Brokeate. UNA por versión de reglas y sin columna de '
  'institución: la comisión no puede depender del banco, y por eso la recomendación no '
  'puede estar sesgada por ella.';


-- ===========================================================================
-- 3. La orden
-- ===========================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type t
                 JOIN pg_namespace n ON n.oid = t.typnamespace
                 WHERE t.typname = 'order_status' AND n.nspname = 'public') THEN
    -- 'sent'      → salió de la app hacia el banco; nadie ha confirmado nada.
    -- 'confirmed' → el banco acusó recibo y devolvió su referencia.
    -- 'failed'    → el banco la rechazó. La plata no se movió y la propuesta queda como
    --               estaba: se puede reintentar (por eso `failed` no es terminal).
    CREATE TYPE public.order_status AS ENUM ('sent', 'confirmed', 'failed');
  END IF;
END
$$;


CREATE TABLE IF NOT EXISTS public.investment_orders (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- UNA orden por propuesta. Sin esto, dos taps seguidos en "Invertir ahora" (o dos
  -- pestañas) cursarían la misma cartera dos veces. Es la misma clase de bug que el
  -- trigger de capital de la migración 002, y se cierra igual: en la base, no en un `if`.
  proposal_id uuid NOT NULL UNIQUE REFERENCES public.proposals(id),
  investor_id uuid NOT NULL REFERENCES public.profiles(id),

  -- El asesor que FIRMÓ la propuesta de la que nace esta orden, y la revisión concreta
  -- que lo prueba. La comisión es suya: es quien respondió con su nombre por esto.
  -- Nullable porque una revisión vieja pudo perder el rastro; la orden no se cae por eso.
  advisor_id  uuid REFERENCES public.profiles(id),
  review_id   uuid REFERENCES public.advisor_reviews(id),

  -- Congelados al momento de cursar, igual que `advisor_reviews.rules_version_id`: si
  -- mañana la comisión sube a 75 bps, esta orden sigue diciendo lo que se cobró el día
  -- que se cursó. Un JOIN a commission_policies mentiría con el tiempo.
  rules_version_id uuid NOT NULL REFERENCES public.rules_versions(id),
  comision_bps     int  NOT NULL CHECK (comision_bps >= 0 AND comision_bps <= 500),

  total_amount numeric NOT NULL CHECK (total_amount > 0),

  -- GENERATED: la comisión no la escribe Python ni el LLM ni el front — la deriva
  -- Postgres de dos columnas de esta misma fila. Es literalmente imposible guardar una
  -- comisión que no salga de `total_amount * comision_bps`. Mismo principio que los USD
  -- de `proposal_items`, llevado hasta el final.
  comision_total numeric GENERATED ALWAYS AS
    (round(total_amount * comision_bps::numeric / 10000, 2)) STORED,

  status       public.order_status NOT NULL DEFAULT 'sent',

  -- Nace en `true` y la app lo muestra. El día que exista integración real, esta columna
  -- distingue las órdenes de verdad de las de la demo sin tener que adivinar por fecha.
  is_simulated boolean NOT NULL DEFAULT true,

  created_at   timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz
);

CREATE INDEX IF NOT EXISTS investment_orders_investor
  ON public.investment_orders (investor_id, created_at DESC);

-- El feed del asesor lee por fecha: lo más nuevo primero.
CREATE INDEX IF NOT EXISTS investment_orders_feed
  ON public.investment_orders (created_at DESC);


-- Una línea por instrumento, y por lo tanto por banco: una cartera diversificada en tres
-- instituciones son tres instrucciones distintas, cada una con su referencia y su propio
-- destino. Que la diversificación se vea como N órdenes y no como una es el punto: es lo
-- que un depósito a plazo en una sola ventanilla no puede hacer.
CREATE TABLE IF NOT EXISTS public.investment_order_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id       uuid NOT NULL REFERENCES public.investment_orders(id) ON DELETE CASCADE,
  instrument_id  uuid NOT NULL REFERENCES public.instruments(id),

  -- Denormalizado desde `instruments.institution_id` a propósito: es contra ESTA columna
  -- que corre la validación de convenio, y un instrumento podría cambiar de emisor. La
  -- orden tiene que recordar a qué banco se mandó, no a cuál apunta hoy el catálogo.
  institution_id uuid REFERENCES public.institutions(id),

  amount         numeric NOT NULL CHECK (amount > 0),
  percentage     numeric NOT NULL CHECK (percentage > 0 AND percentage <= 100),

  comision_bps   int NOT NULL CHECK (comision_bps >= 0 AND comision_bps <= 500),
  comision       numeric GENERATED ALWAYS AS
    (round(amount * comision_bps::numeric / 10000, 2)) STORED,

  -- La devuelve el banco al confirmar. Nula mientras la orden está 'sent': es
  -- precisamente lo que distingue "mandada" de "acusada".
  bank_reference text,
  status         public.order_status NOT NULL DEFAULT 'sent',
  confirmed_at   timestamptz
);

CREATE INDEX IF NOT EXISTS investment_order_items_order
  ON public.investment_order_items (order_id);


-- ===========================================================================
-- 4. Las dos reglas que sostienen el pitch, como triggers
-- ===========================================================================

-- "Una propuesta no es real hasta que una persona la firma."
--
-- Esa frase era hasta hoy una convención: nada impedía que un endpoint nuevo, un script o
-- un bug cursara una orden sobre una propuesta en revisión. Acá deja de ser una
-- convención y pasa a ser una restricción de la base.
--
-- El `FOR UPDATE` sobre `proposals` es el que hace que esto no sea decorativo: serializa
-- contra `revisar_propuesta`, que toma el mismo candado (`for update of p`). Sin él, un
-- asesor rechazando y un cliente invirtiendo al mismo tiempo podrían cruzarse y dejar una
-- orden cursada sobre una propuesta rechazada.
CREATE OR REPLACE FUNCTION public.fn_valida_orden_firmada()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_status public.proposal_status;
BEGIN
  SELECT status INTO v_status
  FROM public.proposals
  WHERE id = NEW.proposal_id
  FOR UPDATE;

  IF v_status IS NULL THEN
    RAISE EXCEPTION 'No existe la propuesta %.', NEW.proposal_id;
  END IF;

  -- 'edited' cuenta: el asesor la firmó, y además la corrigió con su nombre. Es MÁS
  -- revisada que una aprobada tal cual, no menos.
  IF v_status NOT IN ('approved', 'edited') THEN
    RAISE EXCEPTION
      'La propuesta % está en estado "%": una orden solo puede nacer de una propuesta que un asesor firmó.',
      NEW.proposal_id, v_status;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_valida_orden_firmada ON public.investment_orders;

CREATE TRIGGER trg_valida_orden_firmada
  BEFORE INSERT OR UPDATE OF proposal_id ON public.investment_orders
  FOR EACH ROW
  EXECUTE FUNCTION public.fn_valida_orden_firmada();


-- "No se cursa plata a un banco con el que no tenemos convenio."
--
-- El catálogo y el convenio son cosas distintas: un banco puede estar en el comparador
-- (para que el cliente vea la tasa) y no poder recibir una orden. Esta es la regla que
-- convierte el modelo de negocio en algo que el sistema aplica en vez de algo que
-- contamos en una diapositiva.
CREATE OR REPLACE FUNCTION public.fn_valida_convenio_item()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_convenio boolean;
  v_nombre   text;
BEGIN
  -- Instrumentos sin emisor (los genéricos del catálogo viejo) no tienen convenio contra
  -- qué validar: pasan, igual que se comportaban antes de esta migración.
  IF NEW.institution_id IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT convenio_activo, name INTO v_convenio, v_nombre
  FROM public.institutions
  WHERE id = NEW.institution_id;

  IF NOT COALESCE(v_convenio, false) THEN
    RAISE EXCEPTION
      'No hay convenio vigente con %: su producto puede compararse, pero no puede recibir una orden.',
      COALESCE(v_nombre, NEW.institution_id::text);
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_valida_convenio_item ON public.investment_order_items;

CREATE TRIGGER trg_valida_convenio_item
  BEFORE INSERT OR UPDATE OF institution_id ON public.investment_order_items
  FOR EACH ROW
  EXECUTE FUNCTION public.fn_valida_convenio_item();


-- ===========================================================================
-- 5. Vistas
-- ===========================================================================

-- El comprobante: la orden con sus líneas, cada una con su banco y su referencia.
DROP VIEW IF EXISTS public.v_investment_order_summary;

CREATE VIEW public.v_investment_order_summary AS
SELECT o.id                AS order_id,
       o.proposal_id,
       o.investor_id,
       inv.full_name       AS investor_name,
       o.advisor_id,
       adv.full_name       AS advisor_name,
       o.status            AS order_status,
       o.is_simulated,
       o.total_amount,
       o.comision_bps,
       o.comision_total,
       o.created_at,
       o.confirmed_at,
       rv.version_label    AS rules_version,
       it.id               AS item_id,
       ins.code            AS instrument_code,
       ins.name            AS instrument_name,
       inst.name           AS institution_name,
       inst.credit_rating  AS institution_rating,
       inst.institution_type,
       it.amount,
       it.percentage,
       it.comision         AS item_comision,
       it.bank_reference,
       it.status           AS item_status,
       it.confirmed_at     AS item_confirmed_at
  FROM public.investment_orders o
  JOIN public.profiles inv           ON inv.id = o.investor_id
  JOIN public.rules_versions rv      ON rv.id = o.rules_version_id
  LEFT JOIN public.profiles adv      ON adv.id = o.advisor_id
  JOIN public.investment_order_items it ON it.order_id = o.id
  JOIN public.instruments ins        ON ins.id = it.instrument_id
  LEFT JOIN public.institutions inst ON inst.id = it.institution_id;


-- El aviso del asesor: "Miguel acaba de cursar USD 5.000 en 3 bancos".
-- Una fila por orden (las líneas se agregan acá), porque lo que el asesor necesita ver de
-- un vistazo es el hecho, no el detalle.
DROP VIEW IF EXISTS public.v_advisor_order_feed;

CREATE VIEW public.v_advisor_order_feed AS
SELECT o.id            AS order_id,
       o.proposal_id,
       o.investor_id,
       inv.full_name   AS investor_name,
       inv.email       AS investor_email,
       inv.cedula_ruc,
       s.subaccount_name,
       rp.name         AS risk_profile_name,
       o.advisor_id,
       o.status,
       o.is_simulated,
       o.total_amount,
       o.comision_total,
       o.created_at,
       o.confirmed_at,
       count(it.id)                        AS lineas,
       count(DISTINCT it.institution_id)   AS instituciones,
       string_agg(DISTINCT inst.name, ', ' ORDER BY inst.name) AS instituciones_nombres
  FROM public.investment_orders o
  JOIN public.profiles inv              ON inv.id = o.investor_id
  JOIN public.proposals p               ON p.id = o.proposal_id
  JOIN public.profiling_sessions s      ON s.id = p.session_id
  LEFT JOIN public.risk_profiles rp     ON rp.id = s.risk_profile_id
  JOIN public.investment_order_items it ON it.order_id = o.id
  LEFT JOIN public.institutions inst    ON inst.id = it.institution_id
 GROUP BY o.id, inv.full_name, inv.email, inv.cedula_ruc, s.subaccount_name, rp.name;


-- Lo que Brokeate factura, y cuánto de eso le corresponde a cada asesor. Es la pantalla
-- que le contesta al gerente del banco "¿y esto qué me cuesta?" con una cifra que sale de
-- la base, no de una proyección en Excel.
DROP VIEW IF EXISTS public.v_advisor_commissions;

CREATE VIEW public.v_advisor_commissions AS
SELECT o.advisor_id,
       adv.full_name              AS advisor_name,
       count(*)                   AS ordenes,
       count(*) FILTER (WHERE o.status = 'confirmed') AS ordenes_confirmadas,
       sum(o.total_amount)        AS monto_intermediado,
       sum(o.comision_total) FILTER (WHERE o.status = 'confirmed') AS comision_ganada
  FROM public.investment_orders o
  LEFT JOIN public.profiles adv ON adv.id = o.advisor_id
 GROUP BY o.advisor_id, adv.full_name;
