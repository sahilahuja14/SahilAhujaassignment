# Employee Attendance and Analytics API

This is a FastAPI and MongoDB implementation of the employee attendance assignment. It supports employee creation and listing, punch-in and punch-out, manual attendance regularization with audit history, analytics endpoints backed by MongoDB aggregation pipelines, and an explain endpoint for checking the main query plans.

The implementation follows the supplied `openapi.yaml` contract and the storage rules in `DATA_MODEL.md`. Employee identity is based on `emp_code`, not MongoDB `_id`. Instants are stored as MongoDB datetimes and returned through the API as epoch milliseconds. Attendance dates and shift times are interpreted in IST, including overnight-shift behavior.

## What was implemented

- Employee creation with duplicate protection through a unique `emp_code` index.
- Employee listing with department filtering, stable sorting, and contract-compliant pagination.
- Punch-in with IST attendance-date calculation, presence-status validation, late-minute calculation, and race-safe insertion.
- Punch-out with latest-open-record lookup, 24-hour validation, work-hour calculation, overtime calculation, and half-day calculation.
- Manual regularization with recomputation of derived fields and an append-only audit trail in `history`.
- Attendance listing using MongoDB-side filtering, sorting, skip, and limit.
- Monthly employee analytics.
- Department monthly summary that counts employees even when they have no logs.
- Late-comer leaderboard using MongoDB ranking so ties are handled correctly.
- Department trend endpoint with daily gap filling and a seven-day moving average.
- Admin explain endpoint for the main list and analytics query plans.

## Run locally

Create and activate a virtual environment, then install the dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set the MongoDB connection values. On Windows PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

For MongoDB Atlas, use the Atlas connection string as `MONGO_URI`. Do not commit `.env` if it contains a real URI or password.

Load the provided sample data:

```bash
python sample_seed.py
```

Start the API:

```bash
uvicorn app.main:app --port 8000
```

## Useful checks

Compile the application:

```bash
python -m py_compile app\main.py
```

Check readiness:

```bash
curl http://localhost:8000/health
```

Try sample analytics:

```bash
curl "http://localhost:8000/analytics/departments/summary?month=2026-07"
curl "http://localhost:8000/analytics/leaderboard/late?month=2026-07&limit=2"
```

Check an explain endpoint:

```bash
curl "http://localhost:8000/admin/explain/attendance_list?date_from=2026-07-06&date_to=2026-07-14"
```

## Verification performed

The app was checked with `python -m py_compile app\main.py`. It was also connected to MongoDB, loaded with the provided sample data, and smoke-tested through the main employee, attendance, analytics, and explain endpoints. The explain checks for the five required explain targets returned index-backed plans with `IXSCAN` present and `COLLSCAN` absent on the tested database.

The sample database was left with the original seeded counts after temporary write-flow testing: 6 employee documents and 9 attendance log documents.
