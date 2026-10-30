create table if not exists public.signals (
  id           bigint generated always as identity primary key,
  symbol       text        not null,
  direction    text        not null check (direction in ('CALL','PUT')),
  strategy     text        not null,
  timeframe    text        not null default '1m',
  entry_price  numeric     not null,
  rsi          numeric,
  ema_fast     numeric,
  ema_slow     numeric,
  strength     int,
  candle_time  timestamptz not null,
  expires_at   timestamptz not null,
  status       text        not null default 'pending'
               check (status in ('pending','win','loss','draw','expired')),
  close_price  numeric,
  created_at   timestamptz not null default now(),
  unique (symbol, candle_time)
);

create index if not exists signals_created_idx on public.signals (created_at desc);

alter table public.signals enable row level security;

drop policy if exists "public read signals" on public.signals;
create policy "public read signals"
  on public.signals for select
  to anon, authenticated
  using (true);

alter publication supabase_realtime add table public.signals;
