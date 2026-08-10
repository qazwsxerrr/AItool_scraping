# Local RSSHub for X sources

This directory runs a local RSSHub instance for the `x` source group in `AItool_scraping`.

## Setup

```bash
cd rsshub-local
cp .env.example .env
# edit .env and set TWITTER_AUTH_TOKEN
# optionally set PROXY_URI if X is not reachable directly
docker compose up -d
```

## Verify RSSHub

```bash
curl "http://127.0.0.1:1200/twitter/keyword/AI%20agent"
curl "http://127.0.0.1:1200/twitter/user/OpenAI"
```

## Connect the Python project

Set this in the project root `.env`:

```env
RSSHUB_BASE_URL=http://127.0.0.1:1200
```

Then run from the project root:

```bash
uv run python scripts/run_fetch_once.py --source x_account_openai --limit 5 --force
uv run python scripts/run_intel_once.py --source x_account_openai --limit 5 --force
```

RSSHub responses enter the same `transport: rsshub` route as other RSS/Atom
feeds. The registry owns the route URL and the fetch job owns persistence; no
separate normalize/prefilter scripts are required.
