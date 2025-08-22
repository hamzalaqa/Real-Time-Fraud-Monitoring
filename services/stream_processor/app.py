import os, json, csv, time, logging
from datetime import datetime
import numpy as np, joblib, requests
from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable
import shap
import sqlalchemy as sa

from featurizer import to_features, FEATURE_NAMES
from threshold import load_threshold

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

BROKER = os.getenv("KAFKA_BROKER","kafka:9092")
TOPIC = os.getenv("TOPIC_TRANSACTIONS","transactions")
ALERTS_SINK = os.getenv("ALERTS_SINK","/app/data/alerts.csv")
LATEST_SINK = os.getenv("LATEST_SINK","/app/data/latest.csv")
SLACK = os.getenv("SLACK_WEBHOOK_URL","").strip()
MODEL_PATH = os.getenv("MODEL_PATH","/app/model.pkl")
DB_URL = os.getenv("DB_URL","")

def ensure_csv(path, headers):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path,"w",newline="", encoding='utf-8') as f: 
            csv.writer(f).writerow(headers)

ensure_csv(ALERTS_SINK, ["ts","prob","Amount",*FEATURE_NAMES,"top_features_json"])
ensure_csv(LATEST_SINK, ["ts","prob","Amount",*FEATURE_NAMES,"is_alert"])

model = joblib.load(MODEL_PATH)
try:
    explainer = shap.TreeExplainer(model)
    SHAP_OK=True
except Exception as e:
    logger.warning("SHAP unavailable: %s", e); SHAP_OK=False

engine=None
if DB_URL:
    try:
        engine=sa.create_engine(DB_URL, pool_pre_ping=True)
        with engine.connect() as c: c.execute(sa.text("SELECT 1"))
        logger.info("DB connected")
    except Exception as e:
        logger.warning("DB connect failed: %s", e); engine=None

def create_consumer_with_retry():
    max_retries = 30
    retry_interval = 5
    
    for attempt in range(max_retries):
        try:
            logger.info("Attempting to connect to Kafka at %s (attempt %d)", BROKER, attempt + 1)
            consumer = KafkaConsumer(
                TOPIC, bootstrap_servers=[BROKER],
                value_deserializer=lambda m: json.loads(m.decode()),
                auto_offset_reset="latest", enable_auto_commit=True,
                group_id="fraud_processor",
                request_timeout_ms=30000,
                retry_backoff_ms=1000
            )
            logger.info("Successfully connected to Kafka")
            return consumer
        except NoBrokersAvailable:
            logger.warning("Kafka not available yet. Retrying in %d seconds...", retry_interval)
            time.sleep(retry_interval)
        except Exception as e:
            logger.error("Error connecting to Kafka: %s. Retrying in %d seconds...", e, retry_interval)
            time.sleep(retry_interval)
    
    raise RuntimeError(f"Failed to connect to Kafka after {max_retries} attempts")

consumer = create_consumer_with_retry()

def top_shap(x):
    if not SHAP_OK: return []
    vals = explainer.shap_values(x.reshape(1,-1))
    sv = vals if isinstance(vals,np.ndarray) else vals[1]
    sv = sv[0]
    idx = np.argsort(np.abs(sv))[::-1][:5]
    return [{"feature":FEATURE_NAMES[i],"contribution":float(sv[i])} for i in idx]

def slack(prob, amount, ts, top):
    if not SLACK: return
    try:
        drivers = ", ".join(f"{d['feature']}={d['contribution']:.3f}" for d in top)
        requests.post(SLACK, data=json.dumps({"text": f":rotating_light: FRAUD {prob:.2f} | amt={amount:.2f} | ts={ts} | {drivers}"}))
    except Exception as e:
        logger.warning("slack failed: %s", e)

logger.info("processor running...")
for msg in consumer:
    rec = msg.value
    ts = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    x = to_features(rec)
    if hasattr(model,"predict_proba"): prob=float(model.predict_proba(x.reshape(1,-1))[0,1])
    else: prob=float(model.predict(x.reshape(1,-1))[0])
    amount = float(rec.get("Amount",0.0))
    th = load_threshold()
    is_alert = prob >= th

    with open(LATEST_SINK,"a",newline="", encoding='utf-8') as f:
        csv.writer(f).writerow([ts,prob,amount,*[rec.get(n,"") for n in FEATURE_NAMES], int(is_alert)])

    if is_alert:
        top = top_shap(x)
        with open(ALERTS_SINK,"a",newline="", encoding='utf-8') as f:
            csv.writer(f).writerow([ts,prob,amount,*[rec.get(n,"") for n in FEATURE_NAMES], json.dumps(top)])
        slack(prob, amount, ts, top)
        if engine:
            try:
                with engine.begin() as conn:
                    conn.execute(sa.text("""
                        INSERT INTO alerts (ts, prob, amount, features, shap)
                        VALUES (:ts, :prob, :amount, :features, :shap)
                    """), {"ts":ts, "prob":prob, "amount":amount,
                           "features": json.dumps({k: rec.get(k) for k in FEATURE_NAMES}),
                           "shap": json.dumps(top)})
            except Exception as e:
                logger.warning("DB insert failed: %s", e)
