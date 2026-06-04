# Supabase & PostgreSQL Conventions
# ~/.claude/rules/supabase.md

This rule covers the **optional database layer** behind a memory backend MCP. The
companion [`memory-persistor`](https://github.com/effecet/memory-persistor) project —
a PostgreSQL-backed memory MCP with thermal decay and a knowledge graph — uses exactly
this pattern, so if you wire it up these conventions apply. Equally usable for any
Postgres/Supabase project.

## Connection

- **Primary:** Supabase pooler at port 6543 (transaction mode via Supavisor)
- **Fallback:** Local Docker Postgres at localhost:5432 (offline/dev)
- Never hardcode `DATABASE_URL` — read it from an env file (`.env`, `.env.supabase`, etc.)
- Use `DOTENV_CONFIG_PATH` in `.mcp.json` to switch between environments
- Auto-detect remote connections and enable SSL (`rejectUnauthorized: false` for the pooler)
- The direct host (`db.<your-project-ref>.supabase.co`) is IPv6-only — use the pooler (`aws-0-<region>.pooler.supabase.com`) for IPv4 networks

## Migrations

- Use Drizzle for schema changes: `npx drizzle-kit generate` then `npx drizzle-kit migrate`
- Name migrations descriptively — they appear in `drizzle/` as numbered SQL files
- Test migrations against local Docker before applying to Supabase
- For Supabase-only changes (RLS, pg_cron), use the Supabase MCP `execute_sql` or dashboard

## Row-Level Security (RLS)

- Enabled on all tables by default
- `anon` role: read-only access
- `postgres` role: full access (used by the MCP server via service key)
- Test RLS policies locally before deploying

## pg_cron

Known constraints (Supabase free tier, pg_cron 1.6):
- `run_job()` function does NOT exist — cannot trigger jobs manually
- No `timezone` column in `cron.job` — all schedules are UTC
- `jobname` column NOT present in `cron.job_run_details` — join on `jobid`
- Jobs run as the `postgres` role

## Data Patterns

- Use `RETURNING *` for insert/update to get the full row back
- Prefer `ON CONFLICT ... DO UPDATE` for upserts
- Use `gen_random_uuid()` for UUIDs (built-in, no extension needed)
- Timestamp columns: `DEFAULT now()` with `timestamptz` type
- Index strategy: GIN for full-text search, GiST for trigram similarity, B-tree for exact lookups

## Free Tier Limits

- 500MB storage
- Pauses after 7 days of inactivity (regular MCP calls prevent this)
- Shared compute — avoid expensive queries during peak hours

## Never

- Hardcode `DATABASE_URL` in code or config files
- Commit any `.env*` file that contains connection strings
- Use the direct host (IPv6) for application connections — use the pooler
- Bypass RLS with the `service_role` key outside of server-side code
- Run DDL through the pooler (use a direct connection or the Supabase dashboard)
