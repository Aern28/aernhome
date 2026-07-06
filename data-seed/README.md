# data-seed/

Git-tracked reference copies of files that live at runtime under `/data/`
(bind-mounted from `C:/projects/aernhome/data` on the host), which is itself
gitignored since it holds live databases and host-specific state.

## services.json

The monitored-service definitions for the dashboard's `/api/health` cards.
This is what ships as the **built-in default** — identical to the
`DEFAULT_SERVICES` fallback baked into `app.py`.

- The app actually reads `/data/services.json` at startup, not this file.
- On a fresh host, if `/data/services.json` doesn't exist yet, the app writes
  it from its built-in defaults (same content as this file) and loads from
  there — see `load_services()` in `app.py`.
- To add, remove, or reconfigure a monitored service day-to-day: edit the
  live `/data/services.json` on the host and `docker restart
  aernhome-dashboard` — no code change or image rebuild needed.
- To reset a host's config back to defaults: copy this file over
  `/data/services.json` and restart.
- If `/data/services.json` exists but fails to parse (bad JSON), the app logs
  a warning and falls back to its in-memory built-in defaults for that run;
  it never crashes and never overwrites the bad file automatically.

See the `_comment` key inside `services.json` for the full field-by-field
schema.
