-- MomentumForge: trades table for syncing user trades from Railway backend

create table if not exists public.trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  symbol text not null,
  side text not null,
  qty numeric not null,
  price numeric not null,
  pnl numeric,
  executed_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (user_id, symbol, executed_at)
);

alter table public.trades enable row level security;

create policy "Users can view own trades"
  on public.trades for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can insert own trades"
  on public.trades for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users can update own trades"
  on public.trades for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create index if not exists trades_user_id_idx on public.trades (user_id);
create index if not exists trades_executed_at_idx on public.trades (executed_at desc);