-- FORWARD OS: the backend's one connection to the team's Canva account.
-- Run once in Supabase: SQL Editor, paste, Run. Safe to run again.
--
-- Why a table and not a Railway variable: Canva's refresh token can be used
-- only ONCE. Every refresh returns a new one, which must be saved before the
-- old one is gone. A Railway variable cannot be rewritten by the app.
--
-- Nobody but the backend may read these rows: they hold the key to the team's
-- Canva designs. Row level security is ON and there are NO policies, so the
-- app's public key (anon) and signed-in users get nothing. The backend uses
-- the service role, which is not subject to row level security.

create table if not exists public.canva_connection (
  id                 int primary key default 1 check (id = 1),  -- one row only
  refresh_token      text not null,
  access_token       text,
  access_expires_at  timestamptz,
  scope              text,
  canva_user_id      text,
  canva_team_id      text,
  connected_at       timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- Short-lived record of a connect attempt in progress (PKCE). The secret half
-- (code_verifier) must stay on the server; the browser only ever sees `state`.
create table if not exists public.canva_oauth_pending (
  state          text primary key,
  code_verifier  text not null,
  created_at     timestamptz not null default now()
);

alter table public.canva_connection     enable row level security;
alter table public.canva_oauth_pending  enable row level security;

revoke all on public.canva_connection    from anon, authenticated;
revoke all on public.canva_oauth_pending from anon, authenticated;

-- Last line of this file. If the SQL Editor shows it ran without an error,
-- everything above ran.
select 'canva tables ready' as result;
