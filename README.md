# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne Software Engineer Trainee assignment. All application code is in `app/main.py`.

## Requirements and configuration

- Python 3.11+, Git, and MongoDB 6.0 or newer.
- Set **both** `MONGO_URI` and `MONGO_DB` in your environment or an untracked local `.env` file. There is no hard-coded application fallback. Environment variables take priority over `.env` values.
- `requirements.txt` bounds major dependency versions so the project does not silently jump to a future incompatible major release.
- The same application supports a local/Docker `mongodb://` URI and an Atlas `mongodb+srv://` URI. One running instance uses one selected database at a time; the two deployments do not sync automatically.

## Option A: local MongoDB with Docker

Start Docker Desktop / Docker Engine, then run:

```powershell
docker volume create hrone-mongo-data
docker run -d --name hrone-mongodb --restart unless-stopped -p 127.0.0.1:27017:27017 -v hrone-mongo-data:/data/db mongo:7.0
$env:MONGO_URI = 'mongodb://127.0.0.1:27017'
$env:MONGO_DB = 'attendance_db'
```

If the `hrone-mongodb` container already exists but is stopped, use `docker start hrone-mongodb` instead of creating it again. The port is bound to loopback for local development; do not expose an unauthenticated MongoDB container publicly.

## Option B: free MongoDB Atlas

Create a free MongoDB 6.0+ cluster, add a database user, and allow your current IP under Network Access. From **Connect → Drivers → Python**, copy the Atlas URI and set it as `MONGO_URI`; set `MONGO_DB` to `attendance_db` or your chosen database name.

```powershell
$env:MONGO_URI = 'mongodb+srv://<DB_USER>:<URL_ENCODED_PASSWORD>@<CLUSTER_HOST>/?retryWrites=true&w=majority'
$env:MONGO_DB = 'attendance_db'
```

Replace the placeholders with the URI from your own Atlas cluster. URL-encode reserved characters in credentials; never commit or share the completed URI. `pymongo[srv]` installs the DNS support needed for Atlas SRV URIs.

For macOS/Linux, use `export MONGO_URI='…'` and `export MONGO_DB='attendance_db'` in place of the PowerShell assignments. To switch between Docker and Atlas, stop the API, change `MONGO_URI`, and restart it. A local `.env` file is also supported but must remain untracked.

## Run the API

In **Windows PowerShell**, from the repository root (in the same shell where you set `MONGO_URI` / `MONGO_DB`):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

On **macOS/Linux**, use `python3 -m venv .venv`, `source .venv/bin/activate`, then `pip install -r requirements.txt` and `uvicorn app.main:app --port 8000` in the shell where the MongoDB environment variables are set.

`GET /health` pings MongoDB and reports `503` if the database or required indexes are unavailable. Interactive API docs are at `http://localhost:8000/docs`.

## Tests

With dependencies installed, run the database-independent rules, API smoke, contract and pipeline-shape tests:

```bash
python -m unittest discover -s tests -v
```

To also run the seven real-MongoDB integration tests, set `HRONE_TEST_MONGO_URI` to a **disposable** MongoDB 6.0+ deployment before running the same command. The tests create and drop a uniquely named database, so do not point the variable at a deployment where your account lacks permission to create/drop databases or at any data you need to keep. A disposable local Docker MongoDB is recommended. These tests verify actual aggregation results, concurrent database updates, and `executionStats` plans for `IXSCAN` / absence of `COLLSCAN`; they are skipped when the variable is unset.

For the Docker instance above, in Windows PowerShell run:

```powershell
$env:HRONE_TEST_MONGO_URI = 'mongodb://127.0.0.1:27017'
python -m unittest discover -s tests -v
```

On macOS/Linux, use:

```bash
export HRONE_TEST_MONGO_URI='mongodb://127.0.0.1:27017'
python -m unittest discover -s tests -v
```

For Atlas, set `HRONE_TEST_MONGO_URI` to the same Atlas URI only if the cluster/user is disposable and permits dropping databases; otherwise leave the integration tests disabled and use a separate test cluster. Never commit the URI.

## Submission notes

The repository includes `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, `DECISIONS.md`, and tests. Do not commit `.env` files, secrets, a Dockerfile, virtual environments, or data dumps. Review `DECISIONS.md` and be ready to explain and modify the implementation during the live discussion.
