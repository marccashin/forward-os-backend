-- FORWARD OS app-wide settings. Run once in Supabase: SQL Editor, paste, Run.
-- Holds: os_videos (Training Center tutorials), market_report_links,
-- market_reports_meta (monthly report run record).
-- Replaces writes to property_notes under placeholder property ids, which that
-- table's foreign key rejects, so none of those saves ever succeeded.

create table if not exists public.os_settings (
  key         text primary key,
  value       jsonb not null,
  updated_at  timestamptz not null default now(),
  updated_by  text
);

alter table public.os_settings enable row level security;

-- The OS front end uses the anon key (same access model as property_notes).
drop policy if exists "os_settings read"   on public.os_settings;
drop policy if exists "os_settings insert" on public.os_settings;
drop policy if exists "os_settings update" on public.os_settings;
create policy "os_settings read"   on public.os_settings for select to anon, authenticated using (true);
create policy "os_settings insert" on public.os_settings for insert to anon, authenticated with check (true);
create policy "os_settings update" on public.os_settings for update to anon, authenticated using (true) with check (true);

grant select, insert, update on public.os_settings to anon, authenticated;
