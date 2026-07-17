-- ===========================================================================
-- 006 — La comisión la paga el inversionista, y sale de su inversión
-- ===========================================================================
--
-- QUÉ CAMBIA Y POR QUÉ.
--
-- Hasta la 005 el modelo era "la institución le paga a Brokeate una prima de
-- intermediación y el cliente no paga nada". Suena bien y es falso: ningún banco va a
-- regalar plata por una orden que igual iba a recibir. Un producto que le cuenta al
-- cliente que su asesoría es gratis está mintiendo sobre quién la paga, y este proyecto
-- lleva cinco migraciones sosteniendo lo contrario.
--
-- El modelo nuevo es el que se puede decir en voz alta: **el inversionista paga 4,5% del
-- total de su subcuenta, y ese 4,5% sale de su inversión.** Pone 10.000, se cobran 450,
-- se reparten 9.550 entre los bancos. No hay un tercero pagando por él.
--
-- QUÉ NO CAMBIA (a propósito):
--
--   - `commission_policies` sigue SIN columna de institución y con su UNIQUE por versión
--     de reglas. Que ahora pague el cliente no debilita esa garantía, la hace más
--     necesaria: si la comisión pudiera variar por banco, Brokeate tendría un incentivo
--     para empujar al que más le deja — y ahora sería con la plata del cliente.
--   - Las órdenes ya cursadas conservan sus `comision_bps` congelados. Esta migración no
--     toca una sola fila de `investment_orders`: quien invirtió bajo el modelo viejo
--     siguió pagando lo que decía su comprobante ese día.
--
-- LO QUE ESTA MIGRACIÓN AGREGA es la cifra que antes no hacía falta: cuánto se invierte
-- DE VERDAD. Mientras la comisión la pagaba el banco, `total_amount` era a la vez lo que
-- el cliente ponía y lo que llegaba a las instituciones. Ahora son dos números distintos
-- y el segundo no existía en ninguna parte.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. El neto de la orden
-- ---------------------------------------------------------------------------

-- GENERATED y no escrito, por lo de siempre: ninguna cifra de plata la escribe Python.
--
-- Ojo con la duplicación de `round(total_amount * comision_bps / 10000, 2)`: está repetida
-- de `comision_total` y NO se puede factorizar. Postgres prohíbe que una columna GENERATED
-- referencie a otra columna GENERATED, así que la única alternativa sería que el neto lo
-- calculara alguien más — que es exactamente lo que no queremos. Duplicar la expresión es
-- el precio de que las dos cifras salgan de la misma fila y no puedan contradecirse.
ALTER TABLE public.investment_orders
  ADD COLUMN IF NOT EXISTS monto_invertido numeric
    GENERATED ALWAYS AS (
      total_amount - round(total_amount * comision_bps::numeric / 10000, 2)
    ) STORED;

COMMENT ON COLUMN public.investment_orders.total_amount IS
  'Lo que el cliente compromete: el total de su subcuenta. NO es lo que llega a los '
  'bancos — de acá sale la comisión. Ver monto_invertido.';

COMMENT ON COLUMN public.investment_orders.comision_total IS
  'Lo que el INVERSIONISTA le paga a Brokeate: 4,5% de total_amount. Se descuenta de su '
  'inversión, no se cobra aparte.';

COMMENT ON COLUMN public.investment_orders.monto_invertido IS
  'Lo que efectivamente se reparte entre las instituciones: total_amount - comision_total. '
  'Es el número que importa para el rendimiento del cliente.';


-- ---------------------------------------------------------------------------
-- 2. El neto de cada línea
-- ---------------------------------------------------------------------------

-- La misma cuenta, una vez por banco. `amount` sigue siendo la porción BRUTA de la línea
-- (total × %), tal como la copia el controlador desde `proposal_items`, y su `comision`
-- sigue siendo `amount × bps` — o sea que nada de lo que ya existía cambia de significado.
-- Lo que se agrega es el resto: lo que queda para ese banco después de la comisión.
--
-- Que la comisión se prorratee por línea en vez de salir toda de la primera no es un
-- detalle contable: si saliera de una sola, esa institución recibiría menos de su
-- porcentaje y el donut de la propuesta dejaría de describir la cartera real.
ALTER TABLE public.investment_order_items
  ADD COLUMN IF NOT EXISTS monto_invertido numeric
    GENERATED ALWAYS AS (
      amount - round(amount * comision_bps::numeric / 10000, 2)
    ) STORED;

COMMENT ON COLUMN public.investment_order_items.amount IS
  'La porción bruta de esta línea (total de la orden × %). Lo que llega al banco es '
  'monto_invertido: de acá sale la parte de comisión que le toca a esta línea.';

COMMENT ON COLUMN public.investment_order_items.monto_invertido IS
  'Lo que llega a ESTE banco: amount - comision.';

-- CUÁL DE LOS DOS NETOS MANDA. La suma de los netos por línea puede diferir de
-- `investment_orders.monto_invertido` en centavos: son N redondeos a 2 decimales contra
-- uno solo. Manda el de la orden — es el que se deriva de lo único que el cliente
-- aceptó ("4,5% de tu total") y es el que se le muestra. El desglose por línea informa
-- cómo se repartió; no redefine cuánto se cobró. Es el mismo criterio que ya rige para
-- `comision_total` vs. la suma de `comision` (ver test_ordenes.py, que tolera 1 centavo).


-- ---------------------------------------------------------------------------
-- 3. El techo de la comisión
-- ---------------------------------------------------------------------------

-- El CHECK de 500 bps se queda EXACTAMENTE donde estaba, pero su comentario ya no aplica:
-- decía que por encima de 5% "dejaría de ser una prima muy pequeñita". Ya no es una prima
-- ni es pequeñita — es el precio del servicio, y el techo ahora significa otra cosa: que
-- subirlo requiere discutirlo y escribir una migración, no cambiar una fila de seed.
--
-- Se deja en 500 y no se sube "por si acaso": 450 de 500 es justamente la clase de margen
-- que obliga a que la próxima subida sea una conversación.
COMMENT ON COLUMN public.investment_orders.comision_bps IS
  'La tasa congelada el día que se cursó la orden, en puntos básicos. Congelada y no un '
  'JOIN a commission_policies: si mañana la comisión sube, esta orden sigue diciendo lo '
  'que su comprobante prometió.';

COMMENT ON TABLE public.commission_policies IS
  'Lo que el INVERSIONISTA le paga a Brokeate por cursar su cartera. UNA por versión de '
  'reglas y sin columna de institución: la comisión no puede depender del banco, y por eso '
  'la recomendación no puede estar sesgada por ella. Que ahora la pague el cliente hace esa '
  'garantía más importante, no menos.';


-- ---------------------------------------------------------------------------
-- 4. Las vistas
-- ---------------------------------------------------------------------------

-- Se recrean para exponer el neto. Sin esto la app tendría el dato en la tabla y no en el
-- payload — y la única forma de pintarlo sería restando en el front, que es la regla que
-- este proyecto no rompe.

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
       o.monto_invertido,
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
       it.monto_invertido  AS item_monto_invertido,
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


-- El aviso del asesor. `monto_invertido` y no solo `total_amount`: "cursó USD 10.000" y
-- "entraron USD 9.550 a tres bancos" son dos hechos distintos y el asesor necesita los dos.
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
       o.monto_invertido,
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


-- `monto_intermediado` pasa a sumar el NETO. Es lo que de verdad se colocó en bancos; el
-- bruto incluiría la comisión, o sea que Brokeate se estaría contando a sí mismo como
-- dinero intermediado.
DROP VIEW IF EXISTS public.v_advisor_commissions;

CREATE VIEW public.v_advisor_commissions AS
SELECT o.advisor_id,
       adv.full_name              AS advisor_name,
       count(*)                   AS ordenes,
       count(*) FILTER (WHERE o.status = 'confirmed') AS ordenes_confirmadas,
       sum(o.monto_invertido)     AS monto_intermediado,
       sum(o.comision_total) FILTER (WHERE o.status = 'confirmed') AS comision_ganada
  FROM public.investment_orders o
  LEFT JOIN public.profiles adv ON adv.id = o.advisor_id
 GROUP BY o.advisor_id, adv.full_name;

COMMIT;
