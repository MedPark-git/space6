"""PostgreSQL checkpoints and fenced worker leases for resumable analyses."""
import uuid
from psycopg2.extras import Json, RealDictCursor

LEASE_SECONDS = 120
RETENTION_DAYS = 7
PIPELINE_VERSION = '20260911-resume-v1'


def ensure_columns(cur):
    # Additive migration. Existing meeting records and legacy job results survive.
    cur.execute('''ALTER TABLE analysis_jobs
        ADD COLUMN IF NOT EXISTS input JSONB,
        ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS lease_token UUID,
        ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS error_code TEXT,
        ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ''')
    cur.execute("UPDATE analysis_jobs SET expires_at=created_at + interval '7 days' WHERE expires_at IS NULL")
    cur.execute('CREATE INDEX IF NOT EXISTS analysis_jobs_expiry_idx ON analysis_jobs(expires_at)')


class LeaseLost(Exception):
    pass


class JobStore:
    def __init__(self, connect):
        self.connect = connect

    def create(self, job_id, payload):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO analysis_jobs(id,state,stage,input,expires_at)
                VALUES(%s,'queued','분석을 준비하고 있습니다.',%s,now()+interval '7 days')''',
                (job_id, Json(payload)))

    def get(self, job_id):
        with self.connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''SELECT *, coalesce(lease_until>now(),false) AS lease_active,
                expires_at>now() AS retained FROM analysis_jobs WHERE id=%s''', (job_id,))
            return cur.fetchone()

    def recent(self):
        with self.connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''SELECT *, coalesce(lease_until>now(),false) AS lease_active,
                expires_at>now() AS retained FROM analysis_jobs
                WHERE expires_at>now() AND confirmed_at IS NULL
                ORDER BY created_at DESC LIMIT 20''')
            return cur.fetchall()

    def claim(self, job_id):
        token = str(uuid.uuid4())
        with self.connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('''UPDATE analysis_jobs SET state='running', lease_token=%s,
                lease_until=now()+(%s * interval '1 second'), error=NULL,error_code=NULL,
                attempts=attempts+1,updated_at=now(),stage='저장된 진행 상태에서 분석을 준비합니다.'
                WHERE id=%s AND state IN ('queued','running','failed') AND input IS NOT NULL
                AND expires_at>now() AND (lease_until IS NULL OR lease_until<=now())
                RETURNING *''', (token, LEASE_SECONDS, job_id))
            return cur.fetchone()

    def update(self, job_id, token, **fields):
        allowed = {'state','stage','checkpoint','result','error','error_code'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job fields')
        values = [Json(v) if k in ('checkpoint','result') else v for k,v in fields.items()]
        sets = [k+'=%s' for k in fields]
        if fields.get('state') in ('failed','succeeded'):
            sets.append('lease_until=NULL')
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('UPDATE analysis_jobs SET '+','.join(sets)+',updated_at=now() '
                "WHERE id=%s AND lease_token=%s AND state='running'",
                values+[job_id,token])
            if cur.rowcount != 1:
                raise LeaseLost()

    def heartbeat(self, job_id, token):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE analysis_jobs SET lease_until=now()+(%s*interval '1 second')
                WHERE id=%s AND lease_token=%s AND state='running' ''',
                (LEASE_SECONDS,job_id,token))
            if cur.rowcount != 1:
                raise LeaseLost()

    def expired(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT id FROM analysis_jobs WHERE expires_at<=now()
                AND (lease_until IS NULL OR lease_until<=now()) LIMIT 100''')
            return [str(row[0]) for row in cur.fetchall()]

    def confirmed_ids(self):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT id FROM analysis_jobs WHERE confirmed_at IS NOT NULL
                AND (lease_until IS NULL OR lease_until<=now()) LIMIT 100''')
            return [str(row[0]) for row in cur.fetchall()]

    def delete_confirmed(self, job_id):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''DELETE FROM analysis_jobs WHERE id=%s AND confirmed_at IS NOT NULL
                AND (lease_until IS NULL OR lease_until<=now())''', (job_id,))
            return cur.rowcount == 1

    def delete_expired(self, job_id):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''DELETE FROM analysis_jobs WHERE id=%s AND expires_at<=now()
                AND (lease_until IS NULL OR lease_until<=now())''', (job_id,))
            return cur.rowcount == 1

    def delete_user_job(self, job_id):
        """Delete an unconfirmed retained job unless a worker still owns it."""
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute('''DELETE FROM analysis_jobs WHERE id=%s AND confirmed_at IS NULL
                AND (lease_until IS NULL OR lease_until<=now()) RETURNING id''', (job_id,))
            return cur.fetchone() is not None
