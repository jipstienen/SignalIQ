# Portfolio Intelligence Platform

Backend-driven portfolio intelligence system for private equity teams.

## Stack

- Backend: FastAPI + PostgreSQL (SQLAlchemy)
- Frontend: Next.js (minimal UI)
- Auth: Firebase token verification hooks

## Structure

- `backend/` API, data model, scoring pipeline, feedback loop, delivery jobs
- `frontend/` minimal dashboard/settings/history UI

## Recommended: full stack in Docker (hot reload)

One command runs **Postgres**, **FastAPI** (`uvicorn --reload`), and **Next.js** (`next dev`). Edits on disk show up without reinstalling dependencies.

**Requirements:** Docker CLI (install **Docker Desktop for Mac**, open it once, wait until it says “Docker is running”).

If you see `sh: docker: command not found`:

1. Install: [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/) (Apple Silicon or Intel), **or** with Homebrew: `brew install --cask docker` then open **Docker** from Applications.
2. Start Docker Desktop and wait until the whale icon is steady.
3. In a **new** terminal, run `docker --version` — it should print a version. If not, quit Terminal, reopen, and try again.

From the repo root:

```bash
npm run dev:docker
```

Then open:

- **App:** [http://localhost:3000](http://localhost:3000)  
- **API:** [http://localhost:8000](http://localhost:8000) (links to `/docs`, `/redoc`, `openapi.json`)

Optional env (shell or `.env` in repo root):

```bash
export NEXT_PUBLIC_USER_TOKEN='<your-user-uuid>'
export CONTEXT_PROVIDER=ollama
export NEWSAPI_KEY=...
npm run dev:docker
```

Stop: `Ctrl+C` or `npm run dev:docker:down`.

The frontend calls the API at `**http://localhost:8000**` (browser → host, not container-to-container).

### No Docker (native dev)

Use this until Docker is installed. From repo root:

```bash
ulimit -n 10240   # optional; reduces “too many open files” on macOS
npm install       # once, at repo root (for concurrently)
npm run dev
```

That runs the API on **[http://127.0.0.1:8011](http://127.0.0.1:8011)** and the app on **[http://localhost:3000](http://localhost:3000)**. Set `frontend/.env.local` to `NEXT_PUBLIC_API_URL=http://localhost:8011` (see **Run Frontend** below).

## Run Backend

```bash
cd /Users/jipstienen/SignalIQ
docker compose up -d postgres

cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

If you already run Postgres locally, you can skip `docker compose up -d postgres`.

To enable live ingestion from NewsAPI, add this in `backend/.env`:

```bash
NEWSAPI_KEY=your_newsapi_key
```

To enable AI context generation, set one provider in `backend/.env`:

```bash
# Option 1: free local testing via Ollama
CONTEXT_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b

# Option 2: OpenAI API
CONTEXT_PROVIDER=openai
OPENAI_API_KEY=your_openai_key
CONTEXT_MODEL=gpt-4.1-mini
```

If `CONTEXT_PROVIDER=fallback`, no LLM is used.

## Run Frontend

```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 NEXT_PUBLIC_USER_TOKEN=<user_uuid> npm run dev
```

Use `Authorization: Bearer <user_uuid>` for local dev requests.