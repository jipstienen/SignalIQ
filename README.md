# Portfolio Intelligence Platform

Backend-driven portfolio intelligence system for private equity teams.

## Stack

- Backend: FastAPI + PostgreSQL (SQLAlchemy)
- Frontend: Next.js (minimal UI)
- Auth: Firebase token verification hooks

## Structure

- `backend/` API, data model, scoring pipeline, feedback loop, delivery jobs
- `frontend/` minimal dashboard/settings/history UI

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