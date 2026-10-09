# webapp_trial — Local Testing Commands

Three processes, three terminals, run from the project root unless noted.

## 1. Backend API (FastAPI / uvicorn) — main file: `webapp_trial/backend/api.py`

```powershell
.\.venv\Scripts\Activate.ps1
cd webapp_trial\backend
python -m uvicorn api:app --reload --port 8800
```

API at `http://localhost:8800`, docs at `http://localhost:8800/docs`.

`webapp_trial/backend/main.py` is an older, in-memory-jobs version kept for reference —
it is **not** the file actually run; `api.py` (SQLite-backed) is the current one.

## 2. Worker (SQLite job queue) — main file: `webapp_trial/backend/worker.py`

```powershell
.\.venv\Scripts\Activate.ps1
cd webapp_trial\backend
python worker.py
```

Polls the SQLite job store (`db.py`) and claims queued jobs; run one instance per available
Claude API rate-limit slot. Options: `--poll 2.0` (custom interval), `--once` (drain queue then exit).

## 3. Frontend (Vite / React) — main file: `webapp_trial/frontend/src/` (entry `index.html`)

```powershell
cd webapp_trial\frontend
npm run dev
```

UI at `http://localhost:5173` (proxies `/api` to the backend port from `.env`).

## Notes

- Backend and worker both need the project `.venv` activated (`requirements-trial.txt`).
- Frontend production build: `npm run build` → output in `webapp_trial/frontend/dist/`.
- Ports come from the project-root `.env` (`API_PORT`, `UI_URL`) — override per-process with env vars if needed.
