-- 2026-10-17  Open To Close: a local record of Transactions' checklist, with WHEN each box was first seen ticked.
--
-- Why: Open To Close tells us only whether a task is ticked (completed true / null).
-- It gives no date, no name, no due date (probe, Oct 16-17, 2026). The Deal Progress
-- tracker in FORWARD OS needs "done when" and "open how long", so the backend polls
-- Open To Close (otc_poller.py) and records what it sees.
--
-- Written only by the backend (service role). FORWARD OS and the Command Center read
-- with the anon / authenticated keys, so both get SELECT, nothing else.
--
-- Safe to run more than once. Last line: notify pgrst, 'reload schema';

create table if not exists public.otc_properties (
  id               bigint primary key,          -- Open To Close property id
  address          text,
  status           text,                        -- contract_status as Open To Close prints it
  client_type      text,                        -- Seller / Buyer
  agent_name       text,
  team_user_name   text,
  otc_created      text,                        -- as Open To Close sends it
  first_seen_at    timestamptz not null default now(),
  last_seen_at     timestamptz not null default now(),
  tasks_polled_at  timestamptz,                 -- last time its tasks were read
  task_count       int,
  done_count       int
);

create table if not exists public.otc_tasks (
  property_id            bigint not null,
  task_id                bigint not null,
  title                  text,
  position               int,                   -- order Open To Close lists them in
  otc_created            timestamptz,
  completed              boolean not null default false,
  completed_seen_at      timestamptz,           -- first poll that saw it ticked
  completed_before_tracking boolean not null default false,  -- already ticked when first seen: date unknown
  reopened_at            timestamptz,           -- last time a tick was seen removed
  first_seen_at          timestamptz not null default now(),
  last_seen_at           timestamptz not null default now(),
  removed_at             timestamptz,           -- no longer in Open To Close
  primary key (property_id, task_id)
);
create index if not exists otc_tasks_property_idx on public.otc_tasks (property_id, position);

alter table public.otc_properties enable row level security;
alter table public.otc_tasks      enable row level security;

revoke all on public.otc_properties from anon, authenticated;
revoke all on public.otc_tasks      from anon, authenticated;
grant select on public.otc_properties to anon, authenticated;
grant select on public.otc_tasks      to anon, authenticated;

drop policy if exists "otc_properties_read" on public.otc_properties;
create policy "otc_properties_read" on public.otc_properties for select to anon, authenticated using (true);
drop policy if exists "otc_tasks_read" on public.otc_tasks;
create policy "otc_tasks_read" on public.otc_tasks for select to anon, authenticated using (true);

notify pgrst, 'reload schema';
