"""
MediTrack — Clinic Management System (Flask).

PROVIDED to LKS 2027 City-Level participants (MediTrack Cloud Jam). You do NOT
need to change this code — your job is to deploy it on AWS (ECS Fargate) and wire
it to the AWS services through environment variables.

The app is a small but realistic clinic system: patients, doctors, appointments,
medical records, prescriptions and a medicine inventory, plus a statistics
dashboard, a daily report, Redis caching, S3 document upload and SNS alerts.

Environment variables (set on the ECS task definition):
  DB_HOST        RDS PostgreSQL endpoint
  DB_SECRET_ARN  Secrets Manager secret with {username,password,dbname}
  CACHE_HOST     ElastiCache Redis primary endpoint (optional; app degrades gracefully)
  DOCS_BUCKET    S3 bucket for uploaded documents
  SNS_TOPIC_ARN  SNS topic ARN for notifications
  RX_QUEUE_URL   SQS queue URL for the async prescription/pharmacy queue
  AWS_REGION     region (default us-east-1)
  PORT           listen port (default 8080)

Key endpoints:
  GET  /health                     ALB health check (200 when DB reachable)
  GET  /                           Dashboard UI
  GET  /api/stats                  Aggregate counters (cached in Redis when available)
  GET  /api/doctors                List doctors
  GET  /api/patients               List/search patients (?q=)
  POST /api/patients               Register a patient
  GET  /api/patients/<id>          Patient detail + visit history
  GET/POST /api/appointments       List (?date=,?status=) / create (+SNS)
  PUT  /api/appointments/<id>/status   Advance status
  POST /api/records                Add a medical record (+ optional prescription)
  GET  /api/inventory              Medicine stock
  POST /api/inventory/<id>/dispense    Dispense stock (decrement)
  GET  /api/reports/daily?date=    Daily report
  POST /api/documents              Upload a document to S3
  GET  /api/queue/stats            Pharmacy queue depth (SQS ApproximateNumberOfMessages)
  POST /api/queue/process          Pharmacy worker: pull a batch from SQS and fulfil it

Asynchronous pharmacy flow (SQS):
  When a medical record is created with prescription lines, each line is pushed
  to the SQS prescription queue (RX_QUEUE_URL) instead of being dispensed inline.
  A pharmacy worker (POST /api/queue/process, or a separate consumer) later pulls
  messages, decrements medicine stock, and deletes them. Messages that fail
  repeatedly are moved to the dead-letter queue by the queue's redrive policy.
"""
import base64
import json
import os
import uuid
from datetime import date, datetime

import boto3
import psycopg2
from flask import Flask, jsonify, request, render_template
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_HOST = os.environ.get("DB_HOST", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
CACHE_HOST = os.environ.get("CACHE_HOST", "")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
RX_QUEUE_URL = os.environ.get("RX_QUEUE_URL", "")

_secrets = boto3.client("secretsmanager", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_sns = boto3.client("sns", region_name=REGION)
_sqs = boto3.client("sqs", region_name=REGION)

_db_cache = None
_redis = None


# --------------------------------------------------------------------------- infra helpers
def db_creds():
    global _db_cache
    if _db_cache:
        return _db_cache
    raw = _secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]
    _db_cache = json.loads(raw)
    return _db_cache


def get_conn():
    c = db_creds()
    return psycopg2.connect(
        host=DB_HOST, port=5432,
        dbname=c.get("dbname", "meditrack"),
        user=c["username"], password=c["password"],
        connect_timeout=5,
    )


def redis_client():
    """Return a Redis client if CACHE_HOST is set and redis is installed, else None.
    The app must keep working even when the cache is unavailable."""
    global _redis
    if _redis is not None or not CACHE_HOST:
        return _redis
    try:
        import redis  # optional dependency
        _redis = redis.Redis(host=CACHE_HOST, port=6379, socket_connect_timeout=2,
                             socket_timeout=2, decode_responses=True)
        _redis.ping()
    except Exception:  # noqa: BLE001
        _redis = None
    return _redis


def cache_get(key):
    r = redis_client()
    if not r:
        return None
    try:
        v = r.get(key)
        return json.loads(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def cache_set(key, value, ttl=30):
    r = redis_client()
    if not r:
        return
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        pass


def notify(subject, message):
    if SNS_TOPIC_ARN:
        try:
            _sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
        except Exception:  # noqa: BLE001
            pass


def enqueue_rx(job):
    """Push a prescription job onto the pharmacy SQS queue. Returns True if the
    message was sent. The app degrades gracefully when RX_QUEUE_URL is unset."""
    if not RX_QUEUE_URL:
        return False
    try:
        _sqs.send_message(QueueUrl=RX_QUEUE_URL, MessageBody=json.dumps(job, default=str))
        return True
    except Exception:  # noqa: BLE001
        return False


def queue_depth():
    """ApproximateNumberOfMessages on the pharmacy queue, or None if unavailable."""
    if not RX_QUEUE_URL:
        return None
    try:
        a = _sqs.get_queue_attributes(
            QueueUrl=RX_QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return {
            "available": int(a.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- schema + seed
def init_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS doctors (
        id SERIAL PRIMARY KEY, full_name VARCHAR(150) NOT NULL,
        specialization VARCHAR(100), room VARCHAR(20), active BOOLEAN DEFAULT TRUE);
    CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY, medical_no VARCHAR(20) UNIQUE NOT NULL,
        full_name VARCHAR(150) NOT NULL, birth_date DATE, gender VARCHAR(10),
        phone VARCHAR(30), address TEXT, created_at TIMESTAMP NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS appointments (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
        doctor_id INTEGER REFERENCES doctors(id),
        scheduled_at TIMESTAMP NOT NULL, complaint TEXT,
        status VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED',
        created_at TIMESTAMP NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS medical_records (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
        appointment_id INTEGER REFERENCES appointments(id),
        diagnosis TEXT, notes TEXT, document_key VARCHAR(255),
        created_at TIMESTAMP NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS medicines (
        id SERIAL PRIMARY KEY, name VARCHAR(150) NOT NULL, unit VARCHAR(30),
        stock INTEGER NOT NULL DEFAULT 0, reorder_level INTEGER NOT NULL DEFAULT 10);
    CREATE TABLE IF NOT EXISTS prescriptions (
        id SERIAL PRIMARY KEY, record_id INTEGER REFERENCES medical_records(id) ON DELETE CASCADE,
        medicine_id INTEGER REFERENCES medicines(id), quantity INTEGER NOT NULL DEFAULT 1,
        dosage VARCHAR(120));
    CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(scheduled_at);
    CREATE INDEX IF NOT EXISTS idx_rec_patient ON medical_records(patient_id);
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute("SELECT COUNT(*) FROM doctors")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO doctors (full_name, specialization, room) VALUES "
                    "('dr. Sari Wijaya','General','R-101'),"
                    "('dr. Budi Santoso','Pediatrics','R-102'),"
                    "('dr. Rina Kusuma','Dentistry','R-103'),"
                    "('dr. Agus Pratama','Internal Medicine','R-104')")
                cur.execute(
                    "INSERT INTO patients (medical_no, full_name, birth_date, gender, phone, address) VALUES "
                    "('MRN-0001','Andi Saputra','1990-05-12','M','081200000001','Jl. Melati 1'),"
                    "('MRN-0002','Dewi Lestari','1985-11-03','F','081200000002','Jl. Mawar 2'),"
                    "('MRN-0003','Rizky Ramadhan','2001-01-20','M','081200000003','Jl. Anggrek 3')")
                cur.execute(
                    "INSERT INTO medicines (name, unit, stock, reorder_level) VALUES "
                    "('Paracetamol 500mg','tablet',500,50),"
                    "('Amoxicillin 500mg','capsule',200,40),"
                    "('ORS sachet','sachet',300,30),"
                    "('Vitamin C 500mg','tablet',400,50)")
        conn.commit()
    finally:
        conn.close()


def rows(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- routes
@app.route("/health")
def health():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return jsonify(status="healthy", db="connected",
                       cache="connected" if redis_client() else "disabled",
                       queue="enabled" if RX_QUEUE_URL else "disabled"), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify(status="unhealthy", error=str(exc)), 503


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def stats():
    cached = cache_get("stats")
    if cached:
        cached["_cache"] = "hit"
        return jsonify(cached)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM patients"); patients = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM doctors WHERE active"); doctors = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM appointments WHERE DATE(scheduled_at)=CURRENT_DATE")
            today_appts = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM appointments WHERE status='SCHEDULED'")
            scheduled = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM medicines WHERE stock <= reorder_level")
            low_stock = cur.fetchone()[0]
        data = {"patients": patients, "doctors": doctors, "appointments_today": today_appts,
                "scheduled": scheduled, "low_stock_medicines": low_stock, "_cache": "miss"}
        cache_set("stats", data, ttl=15)
        return jsonify(data)
    finally:
        conn.close()


@app.route("/api/doctors")
def doctors():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name, specialization, room, active FROM doctors ORDER BY id")
            return jsonify(doctors=rows(cur))
    finally:
        conn.close()


@app.route("/api/patients", methods=["GET", "POST"])
def patients():
    conn = get_conn()
    try:
        if request.method == "POST":
            d = request.get_json(force=True, silent=True) or {}
            if not d.get("medical_no") or not d.get("full_name"):
                return jsonify(error="medical_no and full_name are required"), 400
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO patients (medical_no, full_name, birth_date, gender, phone, address) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (d["medical_no"], d["full_name"], d.get("birth_date"), d.get("gender"),
                     d.get("phone"), d.get("address")))
                pid = cur.fetchone()[0]
            conn.commit()
            return jsonify(id=pid, message="patient created"), 201
        q = (request.args.get("q") or "").strip()
        with conn.cursor() as cur:
            if q:
                cur.execute("SELECT id, medical_no, full_name, gender, phone FROM patients "
                            "WHERE full_name ILIKE %s OR medical_no ILIKE %s ORDER BY id",
                            (f"%{q}%", f"%{q}%"))
            else:
                cur.execute("SELECT id, medical_no, full_name, gender, phone FROM patients ORDER BY id")
            return jsonify(patients=rows(cur))
    finally:
        conn.close()


@app.route("/api/patients/<int:pid>")
def patient_detail(pid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM patients WHERE id=%s", (pid,))
            r = rows(cur)
            if not r:
                return jsonify(error="patient not found"), 404
            patient = r[0]
            cur.execute("SELECT id, doctor_id, scheduled_at, status, complaint FROM appointments "
                        "WHERE patient_id=%s ORDER BY scheduled_at DESC", (pid,))
            appts = rows(cur)
            cur.execute("SELECT id, diagnosis, notes, document_key, created_at FROM medical_records "
                        "WHERE patient_id=%s ORDER BY created_at DESC", (pid,))
            records = rows(cur)
        return jsonify(patient=patient, appointments=appts, records=records)
    finally:
        conn.close()


@app.route("/api/appointments", methods=["GET", "POST"])
def appointments():
    conn = get_conn()
    try:
        if request.method == "POST":
            d = request.get_json(force=True, silent=True) or {}
            if not d.get("patient_id") or not d.get("doctor_id") or not d.get("scheduled_at"):
                return jsonify(error="patient_id, doctor_id, scheduled_at are required"), 400
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM appointments WHERE doctor_id=%s AND scheduled_at=%s "
                            "AND status<>'CANCELLED'", (d["doctor_id"], d["scheduled_at"]))
                if cur.fetchone()[0] > 0:
                    return jsonify(error="doctor already booked at that time"), 409
                cur.execute("INSERT INTO appointments (patient_id, doctor_id, scheduled_at, complaint) "
                            "VALUES (%s,%s,%s,%s) RETURNING id",
                            (d["patient_id"], d["doctor_id"], d["scheduled_at"], d.get("complaint")))
                aid = cur.fetchone()[0]
            conn.commit()
            notify("MediTrack appointment", f"Appointment #{aid} created for patient {d['patient_id']}.")
            return jsonify(id=aid, status="SCHEDULED", message="appointment created"), 201
        clauses, args = [], []
        if request.args.get("date"):
            clauses.append("DATE(scheduled_at)=%s"); args.append(request.args["date"])
        if request.args.get("status"):
            clauses.append("status=%s"); args.append(request.args["status"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, patient_id, doctor_id, scheduled_at, status, complaint "
                        f"FROM appointments {where} ORDER BY scheduled_at", args)
            return jsonify(appointments=rows(cur))
    finally:
        conn.close()


@app.route("/api/appointments/<int:aid>/status", methods=["PUT"])
def appointment_status(aid):
    d = request.get_json(force=True, silent=True) or {}
    new = d.get("status")
    if new not in ("SCHEDULED", "CONFIRMED", "COMPLETED", "CANCELLED"):
        return jsonify(error="invalid status"), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE appointments SET status=%s WHERE id=%s", (new, aid))
        conn.commit()
        return jsonify(id=aid, status=new)
    finally:
        conn.close()


@app.route("/api/records", methods=["POST"])
def add_record():
    d = request.get_json(force=True, silent=True) or {}
    if not d.get("patient_id"):
        return jsonify(error="patient_id is required"), 400
    queued = []
    doc_key = None
    if d.get("document_base64"):
        if not DOCS_BUCKET:
            return jsonify(error="DOCS_BUCKET not configured"), 500
        doc_key = f"records/{d['patient_id']}/{uuid.uuid4()}.pdf"
        _s3.put_object(Bucket=DOCS_BUCKET, Key=doc_key,
                       Body=base64.b64decode(d["document_base64"]), ContentType="application/pdf")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO medical_records (patient_id, appointment_id, diagnosis, notes, document_key) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (d["patient_id"], d.get("appointment_id"), d.get("diagnosis"),
                         d.get("notes"), doc_key))
            rid = cur.fetchone()[0]
            # optional prescription lines: [{medicine_id, quantity, dosage}]
            for line in d.get("prescriptions", []):
                cur.execute("INSERT INTO prescriptions (record_id, medicine_id, quantity, dosage) "
                            "VALUES (%s,%s,%s,%s) RETURNING id",
                            (rid, line.get("medicine_id"), line.get("quantity", 1), line.get("dosage")))
                presc_id = cur.fetchone()[0]
                # Fulfilment is asynchronous: push the line to the pharmacy queue
                # instead of decrementing stock inline. A worker processes it later.
                queued.append(enqueue_rx({
                    "prescription_id": presc_id, "record_id": rid,
                    "medicine_id": line.get("medicine_id"),
                    "quantity": int(line.get("quantity", 1)),
                    "patient_id": d["patient_id"],
                }))
        conn.commit()
        return jsonify(id=rid, document_key=doc_key,
                       prescriptions_queued=sum(1 for q in queued if q),
                       message="record added"), 201
    finally:
        conn.close()


@app.route("/api/inventory")
def inventory():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, unit, stock, reorder_level, "
                        "(stock <= reorder_level) AS low FROM medicines ORDER BY name")
            return jsonify(medicines=rows(cur))
    finally:
        conn.close()


@app.route("/api/inventory/<int:mid>/dispense", methods=["POST"])
def dispense(mid):
    d = request.get_json(force=True, silent=True) or {}
    qty = int(d.get("quantity", 1))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, stock, reorder_level FROM medicines WHERE id=%s", (mid,))
            r = cur.fetchone()
            if not r:
                return jsonify(error="medicine not found"), 404
            name, stock, reorder = r
            if stock < qty:
                return jsonify(error="insufficient stock", stock=stock), 409
            new_stock = stock - qty
            cur.execute("UPDATE medicines SET stock=%s WHERE id=%s", (new_stock, mid))
        conn.commit()
        if new_stock <= reorder:
            notify("MediTrack low stock", f"Medicine '{name}' is low: {new_stock} left (reorder at {reorder}).")
        return jsonify(id=mid, stock=new_stock, low=new_stock <= reorder)
    finally:
        conn.close()


@app.route("/api/reports/daily")
def daily_report():
    day = request.args.get("date", date.today().isoformat())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM appointments WHERE DATE(scheduled_at)=%s "
                        "GROUP BY status", (day,))
            by_status = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM appointments WHERE DATE(scheduled_at)=%s", (day,))
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM medical_records WHERE DATE(created_at)=%s", (day,))
            visits = cur.fetchone()[0]
        return jsonify(date=day, total_appointments=total, visits_recorded=visits, by_status=by_status)
    finally:
        conn.close()


@app.route("/api/queue/stats")
def queue_stats():
    """Report the pharmacy queue depth so operators can see the backlog."""
    depth = queue_depth()
    if depth is None:
        return jsonify(enabled=False, message="RX_QUEUE_URL not configured"), 200
    return jsonify(enabled=True, **depth)


@app.route("/api/queue/process", methods=["POST"])
def queue_process():
    """Pharmacy worker: pull up to 10 prescription jobs from SQS, decrement the
    medicine stock for each, then delete the message. Messages that keep failing
    are moved to the dead-letter queue by the queue's redrive policy."""
    if not RX_QUEUE_URL:
        return jsonify(error="RX_QUEUE_URL not configured"), 500
    try:
        resp = _sqs.receive_message(QueueUrl=RX_QUEUE_URL, MaxNumberOfMessages=10,
                                    WaitTimeSeconds=1, VisibilityTimeout=30)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"sqs receive failed: {exc}"), 502
    msgs = resp.get("Messages", [])
    processed, low_stock = 0, []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for m in msgs:
                try:
                    job = json.loads(m["Body"])
                    mid, qty = job.get("medicine_id"), int(job.get("quantity", 1))
                    cur.execute("SELECT name, stock, reorder_level FROM medicines WHERE id=%s", (mid,))
                    r = cur.fetchone()
                    if r:
                        name, stock, reorder = r
                        new_stock = max(stock - qty, 0)
                        cur.execute("UPDATE medicines SET stock=%s WHERE id=%s", (new_stock, mid))
                        if new_stock <= reorder:
                            low_stock.append(name)
                    # delete the message once fulfilled
                    _sqs.delete_message(QueueUrl=RX_QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
                    processed += 1
                except Exception:  # noqa: BLE001 — leave it for redelivery / DLQ
                    pass
        conn.commit()
    finally:
        conn.close()
    for name in low_stock:
        notify("MediTrack low stock", f"Medicine '{name}' is low after pharmacy processing.")
    return jsonify(received=len(msgs), processed=processed, low_stock=low_stock)


@app.route("/api/documents", methods=["POST"])
def documents():
    d = request.get_json(force=True, silent=True) or {}
    if not DOCS_BUCKET:
        return jsonify(error="DOCS_BUCKET not configured"), 500
    key = f"docs/{uuid.uuid4()}.bin"
    _s3.put_object(Bucket=DOCS_BUCKET, Key=key, Body=base64.b64decode(d.get("content_base64", "")))
    return jsonify(stored=key), 201


if __name__ == "__main__":
    try:
        init_schema()
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("init_schema skipped: %s", exc)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
