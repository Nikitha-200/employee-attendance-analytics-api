# Employee Attendance & Analytics API

FastAPI service for employee attendance, manual corrections with history, and MongoDB-backed analytics.

## Run

Requires Python 3.11+ and MongoDB 6.0+. Set `MONGO_URI` and `MONGO_DB` in the environment, then run:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

The service creates its indexes at startup. `python sample_seed.py` loads the sample records into the configured database.

## Notes

The implementation follows `openapi.yaml`. Starter findings and design explanations are in `REVIEW.md` and `DECISIONS.md`.
