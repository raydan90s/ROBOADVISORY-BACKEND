-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.profiles (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  auth_user_id uuid UNIQUE,
  role USER-DEFINED NOT NULL,
  full_name text NOT NULL,
  cedula_ruc text UNIQUE,
  email text UNIQUE,
  password_hash text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT profiles_pkey PRIMARY KEY (id)
);
CREATE TABLE public.rules_versions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  version_label text NOT NULL UNIQUE,
  description text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT rules_versions_pkey PRIMARY KEY (id)
);
CREATE TABLE public.questions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  text text NOT NULL,
  order_index integer NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT questions_pkey PRIMARY KEY (id)
);
CREATE TABLE public.question_options (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  question_id uuid NOT NULL,
  code text NOT NULL,
  label text NOT NULL,
  order_index integer NOT NULL DEFAULT 0,
  CONSTRAINT question_options_pkey PRIMARY KEY (id),
  CONSTRAINT question_options_question_id_fkey FOREIGN KEY (question_id) REFERENCES public.questions(id)
);
CREATE TABLE public.scoring_rules (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  rules_version_id uuid NOT NULL,
  question_option_id uuid NOT NULL,
  points integer NOT NULL,
  CONSTRAINT scoring_rules_pkey PRIMARY KEY (id),
  CONSTRAINT scoring_rules_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id),
  CONSTRAINT scoring_rules_question_option_id_fkey FOREIGN KEY (question_option_id) REFERENCES public.question_options(id)
);
CREATE TABLE public.risk_profiles (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  description text,
  CONSTRAINT risk_profiles_pkey PRIMARY KEY (id)
);
CREATE TABLE public.profile_thresholds (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  rules_version_id uuid NOT NULL,
  risk_profile_id uuid NOT NULL,
  min_score integer NOT NULL,
  max_score integer NOT NULL,
  CONSTRAINT profile_thresholds_pkey PRIMARY KEY (id),
  CONSTRAINT profile_thresholds_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id),
  CONSTRAINT profile_thresholds_risk_profile_id_fkey FOREIGN KEY (risk_profile_id) REFERENCES public.risk_profiles(id)
);
CREATE TABLE public.instruments (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  asset_class text NOT NULL,
  risk_class USER-DEFINED NOT NULL,
  expected_return numeric,
  description text,
  is_active boolean NOT NULL DEFAULT true,
  institution_id uuid,
  product_type text CHECK (product_type IS NULL OR (product_type = ANY (ARRAY['deposito_plazo'::text, 'fondo_inversion'::text]))),
  term_days integer,
  min_amount numeric,
  CONSTRAINT instruments_pkey PRIMARY KEY (id),
  CONSTRAINT instruments_institution_id_fkey FOREIGN KEY (institution_id) REFERENCES public.institutions(id)
);
CREATE TABLE public.allocation_templates (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  rules_version_id uuid NOT NULL,
  risk_profile_id uuid NOT NULL,
  name text NOT NULL,
  expected_risk USER-DEFINED NOT NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT allocation_templates_pkey PRIMARY KEY (id),
  CONSTRAINT allocation_templates_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id),
  CONSTRAINT allocation_templates_risk_profile_id_fkey FOREIGN KEY (risk_profile_id) REFERENCES public.risk_profiles(id)
);
CREATE TABLE public.allocation_template_items (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  template_id uuid NOT NULL,
  instrument_id uuid NOT NULL,
  percentage numeric NOT NULL CHECK (percentage > 0::numeric AND percentage <= 100::numeric),
  CONSTRAINT allocation_template_items_pkey PRIMARY KEY (id),
  CONSTRAINT allocation_template_items_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.allocation_templates(id),
  CONSTRAINT allocation_template_items_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id)
);
CREATE TABLE public.profiling_sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  investor_id uuid NOT NULL,
  rules_version_id uuid NOT NULL,
  total_score integer,
  risk_profile_id uuid,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  completed_at timestamp with time zone,
  amount numeric CHECK (amount IS NULL OR amount > 0::numeric),
  CONSTRAINT profiling_sessions_pkey PRIMARY KEY (id),
  CONSTRAINT profiling_sessions_investor_id_fkey FOREIGN KEY (investor_id) REFERENCES public.profiles(id),
  CONSTRAINT profiling_sessions_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id),
  CONSTRAINT profiling_sessions_risk_profile_id_fkey FOREIGN KEY (risk_profile_id) REFERENCES public.risk_profiles(id)
);
CREATE TABLE public.profiling_answers (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  question_id uuid NOT NULL,
  option_id uuid NOT NULL,
  points_awarded integer NOT NULL,
  answered_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT profiling_answers_pkey PRIMARY KEY (id),
  CONSTRAINT profiling_answers_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.profiling_sessions(id),
  CONSTRAINT profiling_answers_question_id_fkey FOREIGN KEY (question_id) REFERENCES public.questions(id),
  CONSTRAINT profiling_answers_option_id_fkey FOREIGN KEY (option_id) REFERENCES public.question_options(id)
);
CREATE TABLE public.proposals (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  template_id uuid NOT NULL,
  expected_risk USER-DEFINED NOT NULL,
  explanation text,
  status USER-DEFINED NOT NULL DEFAULT 'pending_review'::proposal_status,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  total_amount numeric,
  CONSTRAINT proposals_pkey PRIMARY KEY (id),
  CONSTRAINT proposals_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.profiling_sessions(id),
  CONSTRAINT proposals_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.allocation_templates(id)
);
CREATE TABLE public.proposal_items (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  proposal_id uuid NOT NULL,
  instrument_id uuid NOT NULL,
  percentage numeric NOT NULL CHECK (percentage > 0::numeric AND percentage <= 100::numeric),
  amount numeric,
  CONSTRAINT proposal_items_pkey PRIMARY KEY (id),
  CONSTRAINT proposal_items_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES public.proposals(id),
  CONSTRAINT proposal_items_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES public.instruments(id)
);
CREATE TABLE public.advisor_reviews (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  proposal_id uuid NOT NULL,
  advisor_id uuid NOT NULL,
  decision USER-DEFINED NOT NULL,
  comments text,
  rules_version_id uuid NOT NULL,
  edited_allocation jsonb,
  decided_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT advisor_reviews_pkey PRIMARY KEY (id),
  CONSTRAINT advisor_reviews_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES public.proposals(id),
  CONSTRAINT advisor_reviews_advisor_id_fkey FOREIGN KEY (advisor_id) REFERENCES public.profiles(id),
  CONSTRAINT advisor_reviews_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id)
);
CREATE TABLE public.audit_log (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  entity_type text NOT NULL,
  entity_id uuid NOT NULL,
  actor_id uuid,
  action text NOT NULL,
  metadata jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  platform USER-DEFINED NOT NULL DEFAULT 'other'::client_platform,
  CONSTRAINT audit_log_pkey PRIMARY KEY (id),
  CONSTRAINT audit_log_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.profiles(id)
);
CREATE TABLE public.llm_interactions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid,
  proposal_id uuid,
  role text NOT NULL,
  content text NOT NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  platform USER-DEFINED NOT NULL DEFAULT 'other'::client_platform,
  thread_id text,
  metadata jsonb,
  guardrail_passed boolean,
  retry_count integer NOT NULL DEFAULT 0,
  model text,
  CONSTRAINT llm_interactions_pkey PRIMARY KEY (id),
  CONSTRAINT llm_interactions_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.profiling_sessions(id),
  CONSTRAINT llm_interactions_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES public.proposals(id)
);
CREATE TABLE public.auth_sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL,
  refresh_token_hash text NOT NULL UNIQUE,
  platform USER-DEFINED NOT NULL DEFAULT 'other'::client_platform,
  user_agent text,
  ip_address inet,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  expires_at timestamp with time zone NOT NULL,
  revoked_at timestamp with time zone,
  CONSTRAINT auth_sessions_pkey PRIMARY KEY (id),
  CONSTRAINT auth_sessions_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.profiles(id)
);
CREATE TABLE public.institutions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  credit_rating text NOT NULL,
  rating_tier integer NOT NULL CHECK (rating_tier >= 1 AND rating_tier <= 8),
  rating_source text,
  rating_date date,
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT institutions_pkey PRIMARY KEY (id)
);
CREATE TABLE public.profile_institution_rules (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  rules_version_id uuid NOT NULL,
  risk_profile_id uuid NOT NULL,
  max_rating_tier integer NOT NULL CHECK (max_rating_tier >= 1 AND max_rating_tier <= 8),
  rationale text NOT NULL,
  CONSTRAINT profile_institution_rules_pkey PRIMARY KEY (id),
  CONSTRAINT profile_institution_rules_rules_version_id_fkey FOREIGN KEY (rules_version_id) REFERENCES public.rules_versions(id),
  CONSTRAINT profile_institution_rules_risk_profile_id_fkey FOREIGN KEY (risk_profile_id) REFERENCES public.risk_profiles(id)
);