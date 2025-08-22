from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
import os, json, time, pandas as pd, asyncio, sqlalchemy as sa

ALERTS_SINK = os.getenv("ALERTS_SINK","/app/data/alerts.csv")
LATEST_SINK  = os.getenv("LATEST_SINK","/app/data/latest.csv")
THRESHOLD_FILE = os.getenv("THRESHOLD_FILE","/app/data/threshold.json")
DB_URL = os.getenv("DB_URL")

app = FastAPI(title="Fraud API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

engine=None
if DB_URL:
    try:
        engine=sa.create_engine(DB_URL, pool_pre_ping=True)
        with engine.connect() as c: c.execute(sa.text("SELECT 1"))
    except Exception as e:
        engine=None

@app.get("/health")
def health(): return {"ok":True}

@app.get("/threshold")
def get_threshold():
    try:
        with open(THRESHOLD_FILE) as f: return json.load(f)
    except Exception: return {"threshold":0.5}

@app.post("/threshold/{value}")
def set_threshold(value: float):
    os.makedirs(os.path.dirname(THRESHOLD_FILE), exist_ok=True)
    with open(THRESHOLD_FILE,"w") as f: json.dump({"threshold": float(value)}, f)
    return {"ok":True,"threshold": float(value)}

@app.get("/counts")
def get_counts():
    try:
        # Count total lines in CSV files (excluding headers)
        tx_count = 0
        alert_count = 0
        
        if os.path.exists(LATEST_SINK):
            with open(LATEST_SINK, 'r') as f:
                tx_count = sum(1 for line in f) - 1  # subtract header
        
        if os.path.exists(ALERTS_SINK):
            with open(ALERTS_SINK, 'r') as f:
                alert_count = sum(1 for line in f) - 1  # subtract header
        
        return {
            "total_transactions": max(0, tx_count),
            "total_alerts": max(0, alert_count)
        }
    except Exception:
        return {"total_transactions": 0, "total_alerts": 0}

@app.get("/alerts")
def alerts(limit: int = 200):
    if engine:
        with engine.connect() as conn:
            df = pd.read_sql(sa.text("SELECT ts, prob, amount, shap FROM alerts ORDER BY ts DESC LIMIT :lim"), conn, params={"lim":limit})
    else:
        df = pd.read_csv(ALERTS_SINK).tail(limit)
    return json.loads(df.to_json(orient="records"))

@app.get("/transactions")
def transactions(limit: int = 200):
    try:
        df = pd.read_csv(LATEST_SINK).tail(limit)
        return json.loads(df.to_json(orient="records"))
    except Exception:
        return []

async def sse_file(path, interval=1.0):
    last_size = 0
    while True:
        try:
            size = os.path.getsize(path)
            if size != last_size:
                last_size = size
                yield {"event":"update","data": json.dumps({"ts": time.time()})}
        except FileNotFoundError:
            pass
        await asyncio.sleep(interval)

@app.get("/stream/alerts")
async def stream_alerts(request: Request):
    return EventSourceResponse(sse_file(ALERTS_SINK, 0.7))

@app.get("/stream/transactions")
async def stream_txn(request: Request):
    return EventSourceResponse(sse_file(LATEST_SINK, 0.5))
