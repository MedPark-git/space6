"""MedPark-Meeting: Python / PostgreSQL. No SQLite fallback."""
import collections
import base64
import binascii
import fcntl
import datetime as dt
import io
import json
import logging
import os
import random
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

import psycopg2
from psycopg2.extras import Json, RealDictCursor
import requests
from flask import Flask, jsonify, request, session, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from analysis_prompt import ANALYSIS_PROMPT_VERSION, MEETING_ANALYSIS_PROMPT, CONCLUSION_SUMMARY_PROMPT
from minutes_format import FORMAT_VERSION, presentation
from excel_preview import EXCEL_IMAGE_VERSION
from audio_processing import (MAX_AUDIO_BYTES, AUDIO_DIRECT_LIMIT,
    AudioProcessingError, prepare_audio_parts, prepare_persistent_parts,
    RESUME_SEGMENT_SECONDS, transcoder_ready)
from analysis_jobs import JobStore, LeaseLost, ensure_columns, PIPELINE_VERSION, RETENTION_DAYS

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv('DATA_DIR', '/app/user_data'))
DATA.mkdir(parents=True, exist_ok=True)
secret_path = DATA / '.session_secret'
try:
    fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(secrets.token_hex(32))
except FileExistsError:
    pass
app = Flask(__name__, static_folder='static')
app.secret_key = os.getenv('SECRET_KEY') or secret_path.read_text().strip()
app.config.update(MAX_CONTENT_LENGTH=MAX_AUDIO_BYTES + 4 * 1024 * 1024,
    MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('LOCAL_HTTP') != '1',
    PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12))
app.json.ensure_ascii = False
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CATEGORIES = ['HR', '국내사업', '해외사업', '결산']
_schema_ready = False
_schema_lock = threading.Lock()
_login_attempts = collections.defaultdict(collections.deque)
_settings_lock = threading.Lock()
_analysis_limit = threading.BoundedSemaphore(2)
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='meeting-ai')
_part_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='meeting-transcript')
_cleanup_lock = threading.Lock()
_maintenance_started = False
AUDIO_UPLOAD_CHUNK_BYTES = 512 * 1024


class UserError(Exception):
    def __init__(self, message, status=400, code='INVALID_REQUEST'):
        self.message, self.status, self.code = message, status, code


def _connect():
    dsn = os.getenv('DATABASE_URL')
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=8)
    required = ['DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD']
    if not all(os.getenv(k) for k in required):
        raise UserError('PostgreSQL 연결 설정을 확인해 주세요.', 503, 'DB_NOT_CONFIGURED')
    return psycopg2.connect(host=os.environ['DB_HOST'],
        port=int(os.getenv('DB_PORT', '5432')), dbname=os.environ['DB_NAME'],
        user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], connect_timeout=8)


@contextmanager
def connect():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS meetings (
                    id UUID PRIMARY KEY, title TEXT NOT NULL,
                    category TEXT NOT NULL CHECK (category IN ('HR','국내사업','해외사업','결산')),
                    meeting_date DATE NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','confirmed')),
                    payload JSONB NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    confirmed_at TIMESTAMPTZ)''')
                cur.execute('CREATE INDEX IF NOT EXISTS meetings_filter_idx ON meetings(status, category, meeting_date DESC)')
                cur.execute('''CREATE TABLE IF NOT EXISTS meeting_revisions (
                    meeting_id UUID NOT NULL REFERENCES meetings(id), revision INTEGER NOT NULL,
                    payload JSONB NOT NULL, saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(meeting_id, revision))''')
                cur.execute('''CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id UUID PRIMARY KEY, state TEXT NOT NULL, stage TEXT NOT NULL,
                    result JSONB, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
                ensure_columns(cur)
                # Link legacy confirmed work before removing raw analysis data.
                cur.execute('''UPDATE analysis_jobs j SET confirmed_at=coalesce(j.confirmed_at,m.confirmed_at,now()),
                    expires_at=now() FROM meetings m WHERE m.status='confirmed'
                    AND m.payload->>'analysis_job_id'=j.id::text''')
                cur.execute('''UPDATE meetings SET payload=payload-'source_text'-'transcript'-'analysis_job_id'
                    WHERE status='confirmed' AND (payload ? 'source_text' OR payload ? 'transcript'
                    OR payload ? 'analysis_job_id')''')
                cur.execute('''UPDATE meeting_revisions r SET payload=r.payload-'source_text'-'transcript'-'analysis_job_id'
                    FROM meetings m WHERE r.meeting_id=m.id AND m.status='confirmed'
                    AND (r.payload ? 'source_text' OR r.payload ? 'transcript' OR r.payload ? 'analysis_job_id')''')
        _schema_ready = True
        start_maintenance()


def settings():
    try:
        return json.loads((DATA / 'ai-settings.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def ai_key():
    return os.getenv('OPENAI_API_KEY') or settings().get('api_key', '')


@app.before_request
def guard():
    if not request.path.startswith('/api/'):
        return
    if request.path != '/api/session' and not session.get('authenticated'):
        raise UserError('접속 비밀번호를 입력해 주세요.', 401, 'LOGIN_REQUIRED')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('csrf', secrets.token_hex(24))):
            raise UserError('접속 시간이 만료되었습니다. 새로고침 후 다시 시도해 주세요.', 403, 'CSRF_INVALID')


@app.after_request
def response_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    resp.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.errorhandler(UserError)
def user_error(exc):
    return jsonify(error=exc.message, code=exc.code), exc.status


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc, HTTPException):
        if exc.code == 413:
            return jsonify(error='녹음파일은 100 MB 이하로 업로드해 주세요.', code='FILE_TOO_LARGE'), 413
        return jsonify(error='요청한 페이지나 작업을 찾을 수 없습니다.', code='HTTP_ERROR'), exc.code
    logging.exception('Request failed')
    return jsonify(error='처리 중 오류가 발생했습니다. 입력 내용은 유지됩니다. 잠시 후 다시 시도해 주세요.', code='SERVER_ERROR'), 500


@app.get('/')
def home():
    return send_file(BASE / 'templates/index.html')


@app.get('/static/favicon.svg')
def favicon():
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="11" fill="#14634e"/><path d="M9 29V11h4l7 10 7-10h4v18h-5V19l-6 8-6-8v10z" fill="white"/></svg>',200,{'Content-Type':'image/svg+xml'})


@app.get('/health')
def health():
    try:
        init_schema()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT 1')
        return jsonify(status='ok', app='MedPark-Meeting', database='postgresql',
            analysis_prompt_version=ANALYSIS_PROMPT_VERSION,
            excel_format_version=FORMAT_VERSION,
            excel_image_version=EXCEL_IMAGE_VERSION,
            max_audio_upload_bytes=MAX_AUDIO_BYTES,
            audio_transcoder_ready=transcoder_ready(), analysis_pipeline_version=PIPELINE_VERSION,
            audio_checkpoint_minutes=RESUME_SEGMENT_SECONDS // 60,
            analysis_resumable=True)
    except Exception:
        return jsonify(status='starting', app='MedPark-Meeting', database='postgresql'), 503


@app.route('/api/session', methods=['GET', 'POST', 'DELETE'])
def auth():
    if request.method == 'DELETE':
        session.clear()
        return jsonify(ok=True)
    if request.method == 'POST':
        address = request.remote_addr or 'unknown'
        attempts = _login_attempts[address]
        now = time.monotonic()
        while attempts and now - attempts[0] > 300:
            attempts.popleft()
        if len(attempts) >= 10:
            raise UserError('비밀번호 입력 횟수를 초과했습니다. 5분 후 다시 시도해 주세요.', 429)
        password = str((request.get_json(silent=True) or {}).get('password', ''))
        env_password = os.getenv('SITE_PASSWORD')
        valid = secrets.compare_digest(password, env_password) if env_password else False
        if not env_password:
            try:
                valid = check_password_hash(json.loads((BASE / 'bootstrap-auth.json').read_text())['password_hash'], password)
            except (FileNotFoundError, KeyError):
                pass
        if not valid:
            attempts.append(now)
            raise UserError('접속 비밀번호가 맞지 않습니다.', 401, 'INVALID_PASSWORD')
        attempts.clear()
        session.clear()
        session['authenticated'] = True
        session.permanent = True
    session.setdefault('csrf', secrets.token_hex(24))
    return jsonify(authenticated=bool(session.get('authenticated')), csrf=session['csrf'],
        ai_configured=bool(ai_key()) if session.get('authenticated') else False,
        categories=CATEGORIES, app='MedPark-Meeting')


def clean_text(value, name, maximum, required=False):
    if not isinstance(value, str):
        raise UserError(f'{name} 형식을 확인해 주세요.')
    value = value.replace('\x00', '').strip()
    if required and not value:
        raise UserError(f'{name}을(를) 입력해 주세요.')
    if len(value) > maximum:
        raise UserError(f'{name}은(는) {maximum:,}자 이하로 입력해 주세요.')
    return value


def clean_payload(data, final=False):
    if not isinstance(data, dict):
        raise UserError('회의록 입력 형식을 확인해 주세요.')
    result = {}
    for key, name, maximum, required in [
        ('title','회의 제목',200,True),('category','카테고리',20,True),
        ('meeting_date','회의일자',10,True),('author','부서 / 작성자',150,False),
        ('reporter','보고자',100,False),('source_text','실제 회의내용',200000,False),
        ('transcript','녹취 전사',200000,False),
        ('discussion','회의내용',100000,False),('notes','특이사항',30000,False)]:
        result[key] = clean_text(data.get(key, ''), name, maximum, required)
    if result['category'] not in CATEGORIES:
        raise UserError('카테고리를 선택해 주세요.')
    try:
        dt.date.fromisoformat(result['meeting_date'])
    except ValueError:
        raise UserError('회의일자를 확인해 주세요.')
    duration = data.get('duration', '')
    if duration in ('', None):
        result['duration'] = ''
    else:
        try:
            result['duration'] = int(duration)
            if not 1 <= result['duration'] <= 1440:
                raise ValueError()
        except (ValueError, TypeError):
            raise UserError('회의시간은 1~1,440분으로 입력해 주세요.')
    attendees = data.get('attendees', [])
    conclusions = data.get('conclusions', [])
    if not isinstance(attendees, list) or len(attendees) > 200:
        raise UserError('참석자는 최대 200명까지 추가할 수 있습니다.')
    if not isinstance(conclusions, list) or len(conclusions) > 100:
        raise UserError('결론 및 추진사항은 최대 100개까지 입력할 수 있습니다.')
    result['attendees'] = list(dict.fromkeys(clean_text(v, '참석자', 100, True) for v in attendees))
    result['conclusions'] = [clean_text(v, '결론 및 추진사항', 10000) for v in conclusions if isinstance(v, str) and v.strip()]
    if data.get('analysis_job_id'):
        try:
            result['analysis_job_id'] = str(uuid.UUID(str(data['analysis_job_id'])))
        except ValueError:
            raise UserError('분석 작업 정보를 확인해 주세요.')
    result=presentation(result)
    if final and not (result['discussion'] or result['conclusions'] or result['notes']):
        raise UserError('확정할 회의록 내용을 작성해 주세요.')
    return result


def serialize(row):
    return dict(presentation(row['payload']), id=str(row['id']), status=row['status'], revision=row['revision'],
        created_at=row['created_at'].isoformat(), updated_at=row['updated_at'].isoformat(),
        confirmed_at=row['confirmed_at'].isoformat() if row['confirmed_at'] else None)


def get_meeting(meeting_id):
    try:
        meeting_id = str(uuid.UUID(meeting_id))
    except ValueError:
        raise UserError('회의록을 찾을 수 없습니다.', 404)
    init_schema()
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM meetings WHERE id=%s', (meeting_id,))
            row = cur.fetchone()
    if not row:
        raise UserError('회의록을 찾을 수 없습니다.', 404)
    return row


@app.get('/api/meetings')
def list_meetings():
    init_schema()
    clauses, params = ["status='confirmed'"], []
    if request.args.get('category'):
        if request.args['category'] not in CATEGORIES:
            raise UserError('카테고리를 확인해 주세요.')
        clauses.append('category=%s')
        params.append(request.args['category'])
    keyword = request.args.get('q', '').strip()[:200]
    if keyword:
        clauses.append("(title ILIKE %s ESCAPE E'\\\\' OR payload::text ILIKE %s ESCAPE E'\\\\')")
        escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        params.extend(['%' + escaped + '%'] * 2)
    for key, op in [('from', '>='), ('to', '<=')]:
        if request.args.get(key):
            try:
                date = dt.date.fromisoformat(request.args[key])
            except ValueError:
                raise UserError('조회 기간을 확인해 주세요.')
            clauses.append('meeting_date ' + op + ' %s')
            params.append(date)
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        raise UserError('페이지를 확인해 주세요.')
    sort = {'date_desc': 'meeting_date DESC, updated_at DESC', 'date_asc': 'meeting_date ASC, updated_at DESC',
        'title': 'title ASC, meeting_date DESC', 'category': 'category ASC, meeting_date DESC'}.get(request.args.get('sort'), 'meeting_date DESC, updated_at DESC')
    where = ' AND '.join(clauses)
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT count(*) AS n FROM meetings WHERE ' + where, params)
            total = cur.fetchone()['n']
            cur.execute('SELECT id,title,category,meeting_date,revision,updated_at, '
                "payload->'attendees' AS attendees, payload->>'author' AS author FROM meetings WHERE "
                + where + ' ORDER BY ' + sort + ' LIMIT 20 OFFSET %s', params + [(page-1)*20])
            rows = cur.fetchall()
    return jsonify(items=[dict(r, id=str(r['id']), meeting_date=r['meeting_date'].isoformat(),
        updated_at=r['updated_at'].isoformat()) for r in rows], total=total, page=page, page_size=20)


@app.get('/api/meetings/<meeting_id>')
def detail(meeting_id):
    return jsonify(serialize(get_meeting(meeting_id)))


@app.post('/api/meetings')
def save_meeting():
    data = request.get_json(silent=True) or {}
    confirmed = data.get('status') == 'confirmed'
    payload = clean_payload(data, final=confirmed)
    analysis_job_id = payload.get('analysis_job_id') if confirmed else None
    stored_payload = dict(payload)
    if confirmed:
        for key in ('source_text','transcript','analysis_job_id'):
            stored_payload.pop(key, None)
    try:
        meeting_id = str(uuid.UUID(data['id'])) if data.get('id') else str(uuid.uuid4())
        expected = int(data.get('revision') or 0)
    except (ValueError, TypeError):
        raise UserError('회의록 저장 정보를 확인해 주세요.')
    init_schema()
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM meetings WHERE id=%s FOR UPDATE', (meeting_id,))
            old = cur.fetchone()
            if old and old['revision'] != expected:
                raise UserError('다른 창에서 수정된 회의록입니다. 현재 내용을 복사한 뒤 목록에서 다시 열어 주세요.',409,'REVISION_CONFLICT')
            if not old and expected:
                raise UserError('원본 회의록을 찾을 수 없습니다.',404)
            if old and old['status'] == 'confirmed' and not confirmed:
                raise UserError('확정된 회의록의 수정은 다시 확정하여 저장해 주세요.',409)
            revision = (old['revision'] if old else 0) + 1
            status = 'confirmed' if confirmed else 'draft'
            cur.execute('''INSERT INTO meetings(id,title,category,meeting_date,status,payload,revision,confirmed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title, category=EXCLUDED.category,
                meeting_date=EXCLUDED.meeting_date,status=EXCLUDED.status,payload=EXCLUDED.payload,
                revision=EXCLUDED.revision,updated_at=now(),confirmed_at=EXCLUDED.confirmed_at RETURNING *''',
                (meeting_id,stored_payload['title'],stored_payload['category'],stored_payload['meeting_date'],status,Json(stored_payload),revision,confirmed))
            row = cur.fetchone()
            cur.execute('INSERT INTO meeting_revisions(meeting_id,revision,payload) VALUES(%s,%s,%s)',
                (meeting_id,revision,Json(dict(stored_payload,status=status))))
            if analysis_job_id:
                cur.execute('''UPDATE analysis_jobs SET confirmed_at=now(),expires_at=now()
                    WHERE id=%s AND state='succeeded' AND (lease_until IS NULL OR lease_until<=now())''',
                    (analysis_job_id,))
    response = serialize(row)
    if analysis_job_id:
        response['analysis_deleted'] = purge_confirmed_analysis(analysis_job_id)
        response['audio_deleted'] = response['analysis_deleted']
    return jsonify(response)


@app.get('/api/meetings/<meeting_id>/download')
def download_saved(meeting_id):
    row = get_meeting(meeting_id)
    return export_response(row['payload'])


@app.get('/api/meetings/<meeting_id>/image')
def image_saved(meeting_id):
    row = get_meeting(meeting_id)
    if row['status'] != 'confirmed':
        raise UserError('확정된 회의록만 이미지로 볼 수 있습니다.', 409, 'MEETING_NOT_CONFIRMED')
    from exporter import export_meeting, ExcelCapacityError
    from excel_preview import render_workbook_svg
    try:
        image = render_workbook_svg(export_meeting(row['payload']))
    except ExcelCapacityError as exc:
        raise UserError(str(exc), 422, 'EXCEL_CELL_LIMIT')
    return app.response_class(image, mimetype='image/svg+xml', headers={
        'Content-Disposition': 'inline',
        'Cache-Control': 'private, no-store',
    })


@app.get('/api/meetings/<meeting_id>/image.png')
def image_png_saved(meeting_id):
    row = get_meeting(meeting_id)
    if row['status'] != 'confirmed':
        raise UserError('확정된 회의록만 PNG로 다운로드할 수 있습니다.',409,'MEETING_NOT_CONFIRMED')
    from exporter import export_meeting, ExcelCapacityError
    from excel_preview import render_workbook_svg
    import cairosvg
    try:
        png=cairosvg.svg2png(bytestring=render_workbook_svg(export_meeting(row['payload'])))
    except ExcelCapacityError as exc:
        raise UserError(str(exc),422,'EXCEL_CELL_LIMIT')
    name=''.join(ch for ch in row['payload']['title'] if ch.isalnum() or ch in ' -_')[:70]
    return send_file(io.BytesIO(png),as_attachment=True,
        download_name=f"MedPark_회의록_이미지_{row['payload']['meeting_date']}_{name}.png",
        mimetype='image/png')


@app.post('/api/download')
def download_current():
    payload = clean_payload(request.get_json(silent=True) or {}, final=True)
    return export_response(payload)


def export_response(payload):
    from exporter import export_meeting, ExcelCapacityError
    try:
        return excel_response(export_meeting(payload),payload)
    except ExcelCapacityError as exc:
        raise UserError(str(exc),422,'EXCEL_CELL_LIMIT')


def excel_response(content, data):
    name = ''.join(ch for ch in data['title'] if ch.isalnum() or ch in ' -_')[:70]
    return send_file(io.BytesIO(content), as_attachment=True,
        download_name=f"MedPark_회의록_{data['meeting_date']}_{name}.xlsx",
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def upstream_error(resp):
    if resp.status_code in (401,403):
        raise UserError('AI 인증키 또는 모델 사용 권한을 확인해 주세요.', 502, 'AI_AUTH_ERROR')
    if resp.status_code == 429:
        if quota_error(resp):
            raise UserError('OpenAI 잔액 또는 프로젝트 사용 한도에 도달했습니다. 충전·한도를 확인한 뒤 이어서 분석해 주세요. 완료 구간은 보관됩니다.',502,'AI_QUOTA_EXCEEDED')
        raise UserError('AI 요청이 일시적으로 몰렸습니다. 잠시 후 이어서 분석해 주세요. 완료 구간은 보관됩니다.',502,'AI_RATE_LIMIT')
    if resp.status_code >= 400:
        raise UserError('AI 처리에 실패했습니다. 녹음 형식 또는 입력 내용을 확인하고 다시 시도해 주세요.',502,'AI_UPSTREAM_ERROR')


def quota_error(resp):
    try:
        err = resp.json().get('error', {})
        values = {str(err.get('code','')), str(err.get('type',''))}
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(values & {'insufficient_quota','billing_hard_limit_reached',
        'billing_not_active','organization_spend_limit_exceeded','project_spend_limit_exceeded',
        'organization_usage_limit_exceeded','credits_exhausted'})


def retry_delay(resp, attempt):
    """Respect Retry-After as a minimum; defer instead of shortening long waits."""
    delay = 3 * (2 ** attempt)
    if resp is not None:
        raw = resp.headers.get('Retry-After')
        if raw:
            try:
                delay = max(0, float(raw))
            except (TypeError, ValueError):
                try:
                    delay = max(0, (parsedate_to_datetime(raw) - dt.datetime.now(dt.timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
    return delay + random.uniform(0.1, 0.8)


def request_with_retry(send, progress=None, label='AI 분석'):
    for attempt in range(3):
        resp = None
        network_error = None
        try:
            resp = send()
        except (requests.Timeout, requests.ConnectionError) as exc:
            network_error = exc
        if network_error is None:
            transient = resp.status_code in (408,409,429,500,502,503,504)
            if not transient or quota_error(resp):
                upstream_error(resp)
                return resp
        if attempt == 2:
            if network_error:
                raise network_error
            upstream_error(resp)
        delay = retry_delay(resp, attempt)
        if delay > 60:
            upstream_error(resp)
        if progress:
            progress(f'{label} 응답 대기 · {int(delay)+1}초 후 자동 재시도 ({attempt+1}/2)')
        logging.warning('%s retry %s (status=%s)', label, attempt+1,
            resp.status_code if resp is not None else type(network_error).__name__)
        if resp is not None:
            resp.close()
        time.sleep(delay)


@app.route('/api/settings/ai', methods=['GET','POST'])
def ai_settings():
    if request.method == 'GET':
        return jsonify(configured=bool(ai_key()), environment_managed=bool(os.getenv('OPENAI_API_KEY')))
    if os.getenv('OPENAI_API_KEY'):
        raise UserError('서버 환경변수에 설정된 AI 인증키를 사용 중입니다.')
    key = clean_text((request.get_json(silent=True) or {}).get('api_key',''), 'API 인증키', 1024, True)
    try:
        resp = requests.get('https://api.openai.com/v1/models/' + os.getenv('OPENAI_TEXT_MODEL','gpt-4.1-mini'),
            headers={'Authorization':'Bearer '+key}, timeout=(10,30))
        upstream_error(resp)
    except requests.RequestException:
        raise UserError('AI 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',502)
    with _settings_lock:
        fd, temporary = tempfile.mkstemp(dir=DATA)
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump({'api_key': key}, fh)
            os.replace(temporary, DATA / 'ai-settings.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return jsonify(configured=True)


def analysis_context(form):
    """Forward complete validated meeting metadata, including legacy clients."""
    context = {key:clean_text(form.get(key,''), label, maximum) for key,label,maximum in (
        ('title','회의 제목',200),('category','카테고리',20),('meeting_date','회의일자',10),
        ('duration','회의시간',4),('author','부서 / 작성자',150),('reporter','보고자',100))}
    if context['meeting_date']:
        try:
            date = dt.date.fromisoformat(context['meeting_date'])
        except ValueError:
            raise UserError('회의일자를 확인해 주세요.')
        context['meeting_weekday'] = '월화수목금토일'[date.weekday()]
    if context['duration']:
        try:
            if not 1 <= int(context['duration']) <= 1440:
                raise ValueError()
        except ValueError:
            raise UserError('회의시간은 1~1,440분으로 입력해 주세요.')
    raw = form.get('attendees_json')
    if raw is not None:
        try:
            attendees = json.loads(raw)
        except (TypeError, ValueError):
            raise UserError('참석자 형식을 확인해 주세요.')
    else:
        attendees = [name.strip() for name in form.get('attendees','').split(',') if name.strip()]
    if not isinstance(attendees,list) or len(attendees) > 200:
        raise UserError('참석자는 최대 200명까지 추가할 수 있습니다.')
    context['attendees'] = list(dict.fromkeys(clean_text(name,'참석자',100,True) for name in attendees))
    return context


def conclusion_summary_fits(items):
    """Each semantic summary must fit one visible Excel row without cutting."""
    from exporter import emphasis_lines
    return (isinstance(items,list) and len(items) <= 100
        and all(isinstance(item,str) and item.strip() for item in items)
        and all('\n' not in item and len(emphasis_lines(item,70)) == 1 for item in items))


def summarize_conclusions(key, discussion, conclusions, progress=None):
    """Summarize meaning with AI; never masquerade whitespace edits as a summary."""
    messages=[{'role':'system','content':CONCLUSION_SUMMARY_PROMPT},
        {'role':'user','content':json.dumps({'회의내용':discussion,'기존 결론':conclusions},ensure_ascii=False)}]
    schema={'type':'object','properties':{'conclusions':{'type':'array','items':{'type':'string'}}},
        'required':['conclusions'],'additionalProperties':False}
    try:
        for attempt in range(2):
            resp=request_with_retry(lambda: requests.post('https://api.openai.com/v1/chat/completions',
                headers={'Authorization':'Bearer '+key},
                json={'model':os.getenv('OPENAI_TEXT_MODEL','gpt-4.1-mini'),'store':False,
                    'temperature':0.2,'max_completion_tokens':1000,'messages':messages,
                    'response_format':{'type':'json_schema','json_schema':{'name':'meeting_conclusion_summary','strict':True,'schema':schema}}},
                timeout=(15,90)),progress,'결론 요약')
            upstream_error(resp)
            choice=resp.json()['choices'][0]
            if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'):
                raise UserError('결론 요약이 완성되지 않았습니다. 기존 내용은 유지됩니다. 다시 시도해 주세요.',502,'SUMMARY_INCOMPLETE')
            output=json.loads(choice['message']['content'])
            items=output.get('conclusions')
            if conclusion_summary_fits(items):
                return [item.strip() for item in items]
            messages.append({'role':'assistant','content':choice['message']['content']})
            messages.append({'role':'user','content':'일부 항목이 한 행 분량을 넘거나 줄바꿈을 포함합니다. 서로 다른 결론·추진사항은 별도 항목으로 유지하고, 각 항목만 완결된 한 문장으로 더 압축해 주세요. 항목당 한글 약 35~45자를 목표로 하며 내용을 잘라내지 마세요.'})
        raise UserError('일부 결론을 한 줄로 요약하지 못했습니다. 기존 내용은 유지됩니다. 다시 시도해 주세요.',502,'SUMMARY_TOO_LONG')
    except requests.Timeout:
        raise UserError('결론 요약 응답이 지연되었습니다. 기존 내용은 유지됩니다. 다시 시도해 주세요.',504,'AI_TIMEOUT')
    except requests.RequestException:
        raise UserError('AI 서버 연결이 원활하지 않습니다. 기존 결론은 유지됩니다.',502,'AI_CONNECTION_ERROR')
    except (KeyError,ValueError,TypeError):
        raise UserError('결론 요약 응답을 읽을 수 없습니다. 기존 내용은 유지됩니다.',502,'AI_FORMAT_ERROR')


@app.post('/api/conclusions/summarize')
def summarize_current_conclusions():
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        raise UserError('요약할 회의내용을 확인해 주세요.')
    discussion=clean_text(data.get('discussion',''),'회의내용',100000)
    if not discussion:
        discussion=clean_text(data.get('source_text',''),'실제 회의내용',200000)
    items=data.get('conclusions',[])
    if not isinstance(items,list) or len(items)>100:
        raise UserError('결론 및 추진사항 형식을 확인해 주세요.')
    items=[clean_text(item,'결론 및 추진사항',10000) for item in items]
    if not discussion and not any(items):
        raise UserError('요약할 회의내용 또는 기존 결론을 입력해 주세요.')
    if len(discussion)+sum(map(len,items))>200000:
        raise UserError('요약 자료를 합쳐 200,000자 이하로 입력해 주세요.')
    key=ai_key()
    if not key:
        raise UserError('AI 연결 설정에 인증키를 입력해 주세요.',503,'AI_NOT_CONFIGURED')
    if not _analysis_limit.acquire(blocking=False):
        raise UserError('현재 분석 중인 작업이 있습니다. 잠시 후 다시 시도해 주세요.',429,'ANALYSIS_BUSY')
    try:
        return jsonify(conclusions=summarize_conclusions(key,discussion,items))
    finally:
        _analysis_limit.release()


def job_store():
    return JobStore(connect)


def job_directory(job_id):
    return DATA / 'analysis-work' / str(uuid.UUID(str(job_id)))


def lock_job(job_id):
    directory = job_directory(job_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (directory / '.worker.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        raise UserError('이 분석은 이미 처리 중입니다. 진행 상태를 확인해 주세요.',409,'ANALYSIS_RUNNING')


def remove_job_audio(job_id):
    directory = job_directory(job_id)
    for path in directory.glob('recording.*'):
        path.unlink(missing_ok=True)
    shutil.rmtree(directory / 'parts', ignore_errors=False) if (directory / 'parts').exists() else None


def purge_confirmed_analysis(job_id):
    """Delete audio, segment files, transcript, input and job record after confirm."""
    handle = None
    try:
        row = job_store().get(job_id)
        if not row:
            shutil.rmtree(job_directory(job_id), ignore_errors=True)
            return True
        if not row.get('confirmed_at'):
            return False
        handle = lock_job(job_id)
        remove_job_audio(job_id)
        deleted = job_store().delete_confirmed(job_id)
        handle.close();handle = None
        if deleted:
            shutil.rmtree(job_directory(job_id), ignore_errors=True)
        return deleted
    except Exception:
        logging.exception('Confirmed meeting saved; analysis cleanup pending for %s',job_id)
        return False
    finally:
        if handle:
            handle.close()


def cleanup_expired_jobs():
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        store = job_store()
        for job_id in store.confirmed_ids():
            purge_confirmed_analysis(job_id)
        for job_id in store.expired():
            handle = None
            try:
                handle = lock_job(job_id)
                if store.delete_expired(job_id):
                    shutil.rmtree(job_directory(job_id), ignore_errors=True)
            except UserError:
                pass
            finally:
                if handle:
                    handle.close()
        uploads=DATA/'audio-uploads'
        if uploads.exists():
            cutoff=time.time()-24*60*60
            for directory in uploads.iterdir():
                if directory.is_dir() and directory.stat().st_mtime<cutoff:
                    shutil.rmtree(directory,ignore_errors=True)
    except Exception:
        logging.exception('Unable to clean expired analysis jobs')
    finally:
        _cleanup_lock.release()


def start_maintenance():
    global _maintenance_started
    if _maintenance_started:
        return
    _maintenance_started = True
    def maintain():
        while True:
            cleanup_expired_jobs()
            time.sleep(900)
    threading.Thread(target=maintain,daemon=True,name='analysis-retention').start()


def start_saved_job(job_id):
    if not _analysis_limit.acquire(blocking=False):
        raise UserError('다른 회의록을 분석 중입니다. 잠시 후 이어서 분석해 주세요.',429,'ANALYSIS_BUSY')
    handle = None
    claimed = None
    try:
        handle = lock_job(job_id)
        claimed = job_store().claim(job_id)
        if not claimed:
            raise UserError('이미 진행 중이거나 완료된 분석입니다. 진행 상태를 다시 확인해 주세요.',409,'ANALYSIS_RUNNING')
        _workers.submit(run_saved_analysis,claimed,ai_key(),handle)
    except Exception:
        if claimed:
            job_store().update(job_id,claimed['lease_token'],state='failed',
                error='작업을 시작하지 못했습니다. 이어서 분석을 눌러 주세요.',error_code='WORKER_START_ERROR')
        if handle:
            handle.close()
        _analysis_limit.release()
        raise


@app.post('/api/analyze')
def analyze():
    if not ai_key():
        raise UserError('AI 연결 설정에서 OpenAI API 인증키를 등록해 주세요.',503,'AI_NOT_CONFIGURED')
    init_schema()
    cleanup_expired_jobs()
    original = clean_text(request.form.get('source_text',''), '실제 회의내용', 200000)
    saved = clean_text(request.form.get('transcript',''), '녹취 전사', 200000)
    context = analysis_context(request.form)
    job_id = str(uuid.uuid4())
    directory = job_directory(job_id)
    created = False
    try:
        payload = {'original':original,'saved_transcript':saved,'context':context,
            'pipeline_version':PIPELINE_VERSION,'audio_name':'','mime':''}
        audio = request.files.get('audio')
        upload_id = request.form.get('audio_upload_id','')
        if upload_id:
            try:
                upload_id = str(uuid.UUID(upload_id))
            except ValueError:
                raise UserError('업로드한 녹음파일을 찾을 수 없습니다.',404,'UPLOAD_NOT_FOUND')
            upload_dir = DATA / 'audio-uploads' / upload_id
            try:
                meta = json.loads((upload_dir / 'meta.json').read_text())
                suffix = Path(meta['filename']).suffix.lower()
                if suffix not in ('.mp3','.mp4','.mpeg','.mpga','.m4a','.wav','.webm'):
                    raise ValueError()
                uploaded = upload_dir / 'recording.bin'
                if uploaded.stat().st_size != int(meta['size']):
                    raise UserError('녹음파일 업로드가 완료되지 않았습니다. 다시 시도해 주세요.',409,'UPLOAD_INCOMPLETE')
            except (FileNotFoundError,KeyError,ValueError,json.JSONDecodeError):
                raise UserError('업로드한 녹음파일을 찾을 수 없습니다. 다시 올려 주세요.',404,'UPLOAD_NOT_FOUND')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / ('recording'+suffix)
            os.replace(uploaded,path)
            shutil.rmtree(upload_dir,ignore_errors=True)
            payload.update(audio_name=path.name,mime=meta.get('mime') or 'application/octet-stream',
                filename=clean_text(meta['filename'],'파일명',255))
        if audio and audio.filename:
            suffix = Path(audio.filename).suffix.lower()
            if suffix not in ('.mp3','.mp4','.mpeg','.mpga','.m4a','.wav','.webm'):
                raise UserError('지원되는 녹음파일 형식은 MP3, M4A, WAV, MP4, MPEG, MPGA, WEBM입니다.')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / ('recording'+suffix)
            audio.save(path)
            os.chmod(path,0o600)
            if not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
                raise UserError('비어 있지 않은 100 MB 이하의 녹음파일을 선택해 주세요.',413,'FILE_TOO_LARGE')
            payload.update(audio_name=path.name,mime=audio.mimetype or 'application/octet-stream',
                filename=clean_text(audio.filename,'파일명',255))
        if not (original or payload['audio_name'] or saved):
            raise UserError('녹음파일 또는 실제 회의내용을 입력해 주세요.')
        job_store().create(job_id,payload)
        created = True
        try:
            start_saved_job(job_id)
        except UserError as exc:
            # The upload is durable even when all workers are occupied.
            if exc.code != 'ANALYSIS_BUSY':
                raise
        return jsonify(job_id=job_id,state='queued'),202
    except Exception:
        if not created:
            shutil.rmtree(directory,ignore_errors=True)
        raise


@app.post('/api/audio-uploads')
def create_audio_upload():
    data=request.get_json(silent=True) or {}
    filename=clean_text(data.get('filename',''),'파일명',255)
    size=data.get('size')
    try:size=int(size)
    except (TypeError,ValueError):raise UserError('녹음파일 크기를 확인할 수 없습니다.')
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise UserError('비어 있지 않은 100 MB 이하의 녹음파일을 선택해 주세요.',413,'FILE_TOO_LARGE')
    if Path(filename).suffix.lower() not in ('.mp3','.mp4','.mpeg','.mpga','.m4a','.wav','.webm'):
        raise UserError('지원되는 녹음파일 형식을 선택해 주세요.')
    upload_id=str(uuid.uuid4());directory=DATA/'audio-uploads'/upload_id
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    (directory/'meta.json').write_text(json.dumps({'filename':filename,'size':size,'mime':str(data.get('mime',''))}))
    return jsonify(upload_id=upload_id,chunk_size=AUDIO_UPLOAD_CHUNK_BYTES),201


@app.post('/api/audio-uploads/<upload_id>/chunk')
def append_audio_upload(upload_id):
    try:upload_id=str(uuid.UUID(upload_id))
    except ValueError:raise UserError('업로드 대상을 찾을 수 없습니다.',404)
    directory=DATA/'audio-uploads'/upload_id
    try:meta=json.loads((directory/'meta.json').read_text())
    except (FileNotFoundError,json.JSONDecodeError):raise UserError('업로드 대상을 찾을 수 없습니다.',404)
    data=request.form
    try:chunk=base64.b64decode(data.get('chunk',''),validate=True)
    except (binascii.Error,ValueError,TypeError):raise UserError('업로드 구간을 읽을 수 없습니다.',400,'UPLOAD_CHUNK_INVALID')
    if not chunk or len(chunk)>AUDIO_UPLOAD_CHUNK_BYTES:
        raise UserError('업로드 구간의 크기를 확인해 주세요.',413,'UPLOAD_CHUNK_SIZE')
    target=directory/'recording.bin';offset=target.stat().st_size if target.exists() else 0
    try:expected=int(data.get('offset','-1'))
    except ValueError:expected=-1
    if expected!=offset:raise UserError('업로드 순서가 맞지 않습니다. 다시 시도해 주세요.',409,'UPLOAD_OFFSET')
    if offset+len(chunk)>int(meta['size']):raise UserError('업로드 용량을 초과했습니다.',413,'FILE_TOO_LARGE')
    with target.open('ab') as handle:handle.write(chunk)
    return jsonify(received=offset+len(chunk),complete=offset+len(chunk)==int(meta['size']))


def job_update(job_id, **fields):
    """Legacy non-durable transcription helper, kept for existing integrations."""
    with connect() as conn:
        with conn.cursor() as cur:
            keys = list(fields)
            values = [Json(fields[k]) if k == 'result' else fields[k] for k in keys]
            cur.execute('UPDATE analysis_jobs SET '+','.join(k+'=%s' for k in keys)+',updated_at=now() WHERE id=%s',values+[job_id])


def public_job(row, include_result=True):
    payload, checkpoint = row.get('input') or {}, row.get('checkpoint') or {}
    result = row.get('result') or {}
    if isinstance(result.get('result'),dict):
        result=dict(result,result=presentation(result['result']))
    complete = checkpoint.get('transcript_complete',result.get('transcript_complete',not payload or not payload.get('audio_name')))
    total = checkpoint.get('total_parts',result.get('total_parts',0))
    done = len(checkpoint.get('segments',{})) if 'segments' in checkpoint else result.get('completed_parts',0)
    state = row['state']
    if state in ('running','queued') and not row.get('lease_active'):
        state = 'interrupted'
    resumable = bool(payload and row.get('retained') and state in ('failed','interrupted'))
    reason = ''
    if resumable and payload.get('audio_name') and not complete:
        if not (job_directory(row['id']) / payload['audio_name']).is_file():
            resumable = False
            reason = '보관된 원본이 없습니다. 파일을 다시 올려 주세요.'
    if not payload and state != 'succeeded':
        reason = '이전 버전의 분석은 구간별 재개 정보가 없습니다. 녹음파일을 다시 올려 주세요.'
    answer = dict(job_id=str(row['id']),state=state,stage=row['stage'],error=row.get('error'),
        error_code=row.get('error_code'),resumable=resumable,resume_unavailable_reason=reason,
        completed_parts=done,total_parts=total,checkpoint_minutes=RESUME_SEGMENT_SECONDS//60,
        title=payload.get('context',{}).get('title') or payload.get('filename') or '회의록 분석',
        created_at=row['created_at'].isoformat(),expires_at=row['expires_at'].isoformat() if row.get('expires_at') else None)
    if include_result:
        answer.update(result=result,transcript=result.get('transcript',''),transcript_complete=complete,
            context=payload.get('context',{}),source_text=payload.get('original',''))
    return answer


@app.get('/api/analysis')
def recent_analysis():
    init_schema()
    cleanup_expired_jobs()
    return jsonify(items=[public_job(row,False) for row in job_store().recent()])


@app.get('/api/analysis/<job_id>')
def analysis_status(job_id):
    try:
        job_id = str(uuid.UUID(job_id))
    except ValueError:
        raise UserError('분석 작업을 찾을 수 없습니다.',404)
    init_schema()
    row = job_store().get(job_id)
    if not row or not row.get('retained'):
        raise UserError('분석 결과의 보관 기간이 지났거나 작업을 찾을 수 없습니다.',404,'ANALYSIS_NOT_FOUND')
    return jsonify(public_job(row))


@app.delete('/api/analysis/<job_id>')
def delete_analysis(job_id):
    try:job_id=str(uuid.UUID(job_id))
    except ValueError:raise UserError('분석 작업을 찾을 수 없습니다.',404)
    init_schema()
    row=job_store().get(job_id)
    if not row:raise UserError('이미 삭제되었거나 작업을 찾을 수 없습니다.',404,'ANALYSIS_NOT_FOUND')
    if row.get('lease_active'):
        raise UserError('현재 분석 중인 작업은 완료 또는 중단 후 삭제할 수 있습니다.',409,'ANALYSIS_RUNNING')
    if not job_store().delete_user_job(job_id):
        raise UserError('현재 작업을 삭제할 수 없습니다. 목록을 새로고침해 주세요.',409,'ANALYSIS_DELETE_CONFLICT')
    shutil.rmtree(job_directory(job_id),ignore_errors=True)
    return jsonify(deleted=True)


@app.post('/api/analysis/<job_id>/resume')
def resume_analysis(job_id):
    try:
        job_id = str(uuid.UUID(job_id))
    except ValueError:
        raise UserError('분석 작업을 찾을 수 없습니다.',404)
    if not ai_key():
        raise UserError('AI 연결 설정에서 인증키를 확인해 주세요.',503,'AI_NOT_CONFIGURED')
    init_schema()
    row = job_store().get(job_id)
    if not row or not row.get('retained'):
        raise UserError('분석 보관 기간이 지났습니다. 파일을 다시 올려 주세요.',410,'ANALYSIS_EXPIRED')
    status = public_job(row)
    if status['state'] in ('succeeded','running','queued'):
        return jsonify(job_id=job_id,state=status['state']),200
    if not status['resumable']:
        raise UserError(status['resume_unavailable_reason'] or '다시 업로드해 주세요.',409,'ANALYSIS_NOT_RESUMABLE')
    start_saved_job(job_id)
    return jsonify(job_id=job_id,state='running'),202


def run_saved_analysis(row,key,handle):
    job_id, token = str(row['id']), row['lease_token']
    store = job_store()
    finished = threading.Event()
    lease_lost = threading.Event()
    def heartbeat():
        while not finished.wait(15):
            try:
                store.heartbeat(job_id,token)
            except Exception:
                lease_lost.set()
                logging.exception('Analysis heartbeat lost for %s',job_id)
                return
    pulse = threading.Thread(target=heartbeat,daemon=True,name='analysis-lease')
    pulse.start()
    def update(**fields):
        if lease_lost.is_set():
            raise LeaseLost()
        store.update(job_id,token,**fields)
    try:
        with app.app_context():
            result = run_checkpointed_pipeline(row,key,update)
        update(state='succeeded',stage='분석을 완료했습니다. 확인 후 확정해 주세요.',result=result)
        # Keep recordings until the meeting is confirmed, or retention expires.
    except LeaseLost:
        logging.warning('Analysis lease ended for %s; saved segments retained',job_id)
    except Exception as exc:
        if isinstance(exc,AudioProcessingError):
            exc = UserError(str(exc),422,'AUDIO_PROCESSING_ERROR')
        elif isinstance(exc,requests.Timeout):
            exc = UserError('AI 응답 시간이 초과되었습니다. 완료된 구간부터 이어서 분석해 주세요.',504,'AI_TIMEOUT')
        elif isinstance(exc,requests.RequestException):
            exc = UserError('AI 연결이 중단되었습니다. 완료된 구간부터 이어서 분석해 주세요.',502,'AI_CONNECTION_ERROR')
        if not isinstance(exc,UserError):
            logging.exception('Analysis job %s failed',job_id)
            exc = UserError('분석 처리 중 오류가 발생했습니다. 완료된 구간부터 이어서 분석해 주세요.',500,'ANALYSIS_ERROR')
        try:
            update(state='failed',stage='분석이 중단되었습니다. 완료 구간은 저장되었습니다.',
                error=exc.message,error_code=exc.code)
        except Exception:
            logging.exception('Unable to record analysis failure for %s',job_id)
    finally:
        finished.set()
        pulse.join(timeout=2)
        handle.close()
        _analysis_limit.release()


def transcribe_part(headers,part,mime,previous,progress,index):
    if not 0 < part.stat().st_size <= AUDIO_DIRECT_LIMIT:
        raise UserError('전송할 음성 구간의 용량을 확인해 주세요.',422,'AUDIO_PART_TOO_LARGE')
    data = {'model':os.getenv('OPENAI_TRANSCRIBE_MODEL','whisper-1'),
        'language':'ko','response_format':'json'}
    if previous:
        data['prompt'] = previous[-200:]
    def send():
        # Reopen for every retry; never send an exhausted file handle.
        with part.open('rb') as fh:
            return requests.post('https://api.openai.com/v1/audio/transcriptions',headers=headers,
                data=data,files={'file':('recording'+part.suffix,fh,mime)},timeout=(15,600))
    resp = request_with_retry(send,progress,f'{index+1}번 음성 구간')
    try:
        return clean_text(resp.json()['text'],'전사 결과',200000)
    except (KeyError,ValueError,TypeError):
        raise UserError('음성 전사 응답을 읽지 못했습니다. 해당 구간부터 이어서 분석해 주세요.',502,'AI_FORMAT_ERROR')
    finally:
        resp.close()


def run_checkpointed_pipeline(row,key,update):
    payload = row['input']
    checkpoint = dict(row.get('checkpoint') or {})
    segments = dict(checkpoint.get('segments') or {})
    checkpoint['segments'] = segments
    write_lock = threading.Lock()
    def stage(message):
        with write_lock:
            update(stage=message)
    def save():
        # Partial text is a contiguous prefix; out-of-order parts remain in DB.
        texts = []
        for index in range(checkpoint.get('total_parts',0)):
            if str(index) not in segments:
                break
            texts.append(segments[str(index)])
        transcript = '\n\n'.join(texts).strip()
        with write_lock:
            update(checkpoint=checkpoint,result={'transcript':transcript,
                'transcript_complete':checkpoint.get('transcript_complete',False),
                'completed_parts':len(segments),'total_parts':checkpoint.get('total_parts',0)},
                stage=f"녹음 전사 {len(segments)}/{checkpoint.get('total_parts',0)} 구간 저장 완료")
    if payload.get('audio_name') and not checkpoint.get('transcript_complete'):
        directory = job_directory(row['id'])
        parts = prepare_persistent_parts(directory / payload['audio_name'],directory / 'parts',stage)
        if checkpoint.get('total_parts') not in (None,len(parts)):
            raise UserError('저장된 음성 구간 정보가 맞지 않습니다. 원본으로 새 분석을 시작해 주세요.',409)
        checkpoint['total_parts'] = len(parts)
        checkpoint['transcript_complete'] = False
        if any(not k.isdigit() or not 0<=int(k)<len(parts) for k in segments):
            raise UserError('저장된 전사 구간을 확인할 수 없습니다.',409)
        save()
        pending = [i for i in range(len(parts)) if str(i) not in segments]
        active = {}
        failure = None
        try:
            while pending or active:
                while pending and len(active)<2 and failure is None:
                    index = pending.pop(0)
                    part,mime = parts[index]
                    previous = segments.get(str(index-1),'')
                    future = _part_workers.submit(transcribe_part,{'Authorization':'Bearer '+key},
                        part,mime,previous,stage,index)
                    active[future] = index
                if not active:
                    break
                stage(f'음성 전사 중 · {len(segments)}/{len(parts)} 구간 저장 · '
                    + ', '.join(str(i+1) for i in sorted(active.values()))+'번 처리')
                done,_ = wait(active,return_when=FIRST_COMPLETED)
                for future in done:
                    index = active.pop(future)
                    try:
                        text = future.result()
                        if sum(len(t)+2 for t in segments.values())+len(text)>200000:
                            raise UserError('전체 전사 결과가 200,000자를 초과했습니다. 녹음을 나누어 분석해 주세요.',400,'TRANSCRIPT_TOO_LONG')
                        segments[str(index)] = text
                        save()
                    except Exception as exc:
                        failure = failure or exc
                # Finish and save already-running parts even if a sibling fails.
                if failure:
                    pending.clear()
            if failure:
                raise failure
        finally:
            # Avoid releasing the file lock while a part still owns a file handle.
            if active:
                wait(active)
        checkpoint['transcript_complete'] = True
        checkpoint['transcript'] = '\n\n'.join(segments[str(i)] for i in range(len(parts))).strip()
        save()
    elif not payload.get('audio_name'):
        checkpoint.update(transcript_complete=True,transcript=payload.get('saved_transcript',''))
        with write_lock:
            update(checkpoint=checkpoint,result={'transcript':checkpoint['transcript'],'transcript_complete':True})
    transcript = checkpoint.get('transcript','')
    if payload.get('audio_name') and not transcript:
        raise UserError('녹음에서 회의내용을 확인하지 못했습니다. 원본 재생 상태를 확인해 주세요.',422,'NO_SPEECH')
    stage('전체 전사 저장 완료 · 회의록 양식으로 내용을 정리하고 있습니다.')
    return perform_analysis(key,payload.get('original',''),None,'',payload.get('context',{}),
        saved_transcript=transcript,progress=stage)


def transcribe_audio(headers, file_path, mime, job_id=None):
    """Transcribe size-safe parts in order; never summarize an incomplete recording."""
    def progress(message):
        if job_id:
            job_update(job_id,stage=message)

    try:
        with prepare_audio_parts(file_path,mime,progress=progress) as parts:
            texts=[]
            total=len(parts)
            for index,(part,part_mime) in enumerate(parts,1):
                if not 0 < part.stat().st_size <= AUDIO_DIRECT_LIMIT:
                    raise UserError('전송할 녹음 구간의 용량을 확인할 수 없습니다. 원본 파일로 다시 시도해 주세요.',422,'AUDIO_PART_TOO_LARGE')
                progress(f'녹음파일을 문자로 변환하고 있습니다. ({index}/{total} 구간)')
                data={'model':os.getenv('OPENAI_TRANSCRIBE_MODEL','whisper-1'),
                    'language':'ko','response_format':'json'}
                if texts and texts[-1]:
                    data['prompt']=texts[-1][-200:]
                with part.open('rb') as fh:
                    resp = requests.post('https://api.openai.com/v1/audio/transcriptions',headers=headers,
                        data=data,files={'file':('recording'+part.suffix,fh,part_mime)},timeout=(15,240))
                upstream_error(resp)
                text=clean_text(resp.json().get('text',''),'전사 결과',200000)
                candidate='\n\n'.join([*texts,text]).strip()
                if len(candidate)>200000:
                    raise UserError('전체 전사 결과가 200,000자를 초과했습니다. 녹음파일을 나누어 분석해 주세요.',400,'TRANSCRIPT_TOO_LONG')
                texts.append(text)
                if job_id:
                    job_update(job_id,stage=f'녹음 전사 {index}/{total} 구간 완료',
                        result={'transcript':candidate,'transcript_complete':False,
                            'completed_parts':index,'total_parts':total})
            transcript='\n\n'.join(text for text in texts if text).strip()
            if not transcript:
                raise UserError('녹음에서 회의내용을 확인하지 못했습니다. 다른 파일 또는 수기 내용을 입력해 주세요.',502)
            if job_id:
                job_update(job_id,stage='전체 전사를 완료했습니다. 회의록 양식으로 정리하고 있습니다.',
                    result={'transcript':transcript,'transcript_complete':True,
                        'completed_parts':total,'total_parts':total})
            return transcript
    except AudioProcessingError as exc:
        raise UserError(str(exc),422,'AUDIO_PROCESSING_ERROR')


def perform_analysis(key,original,file_path,mime,context,saved_transcript='',job_id=None,progress=None):
    transcript = saved_transcript
    headers = {'Authorization':'Bearer '+key}
    try:
        if file_path:
            transcript = transcribe_audio(headers,file_path,mime,job_id)
        source = ('[녹취 전사]\n'+transcript+'\n\n' if transcript else '') + ('[입력 내용]\n'+original if original and original!=transcript else '')
        if not source.strip():
            raise UserError('녹음파일 또는 실제 회의내용을 입력해 주세요.')
        if len(source) > 200000:
            raise UserError('전사와 입력 내용을 합쳐 200,000자 이하로 분석할 수 있습니다.')
        schema = {'type':'object','properties':{
            'title':{'type':'string'},'conclusions':{'type':'array','items':{'type':'string'}},
            'discussion':{'type':'string'},'notes':{'type':'string'}},
            'required':['title','conclusions','discussion','notes'],'additionalProperties':False}
        resp = request_with_retry(lambda: requests.post('https://api.openai.com/v1/chat/completions',headers=headers,
            json={'model':os.getenv('OPENAI_TEXT_MODEL','gpt-4.1-mini'),'store':False,'temperature':0.2,'max_completion_tokens':12000,
                'messages':[{'role':'system','content':MEETING_ANALYSIS_PROMPT},
                    {'role':'user','content':json.dumps({'회의정보':context,'회의자료':source},ensure_ascii=False)}],
                'response_format':{'type':'json_schema','json_schema':{'name':'meeting_minutes','strict':True,'schema':schema}}},
            timeout=(15,300)),progress,'회의록 정리')
        upstream_error(resp)
        choice = resp.json()['choices'][0]
        if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
            raise UserError('분석이 완성되지 않았습니다. 내용을 나누거나 원문을 확인한 뒤 다시 시도해 주세요.',502)
        result = json.loads(choice['message']['content'])
        result['title'] = clean_text(result.get('title',''), '분석 제목',200)
        result['discussion'] = clean_text(result.get('discussion',''), '분석 내용',100000)
        result['notes'] = clean_text(result.get('notes',''), '분석 특이사항',30000)
        if not isinstance(result.get('conclusions'),list) or len(result['conclusions'])>100:
            raise UserError('AI 결과 형식을 확인할 수 없습니다. 다시 분석해 주세요.',502)
        result['conclusions'] = [clean_text(v,'분석 추진사항',10000) for v in result['conclusions']]
        if not conclusion_summary_fits(result['conclusions']):
            if progress:progress('결론 및 추진사항을 항목별 한 줄로 요약하고 있습니다.')
            result['conclusions']=summarize_conclusions(key,result['discussion'],result['conclusions'],progress)
        return dict(result=presentation(result),source_text=source,transcript=transcript)
    except requests.Timeout:
        raise UserError('AI 응답 시간이 초과되었습니다. 입력 내용은 유지되며 다시 시도할 수 있습니다.',504,'AI_TIMEOUT')
    except requests.RequestException:
        raise UserError('AI 서버 연결이 원활하지 않습니다. 잠시 후 다시 시도해 주세요.',502,'AI_CONNECTION_ERROR')
    except (KeyError, ValueError, TypeError):
        raise UserError('AI 응답을 읽을 수 없습니다. 다시 시도해 주세요.',502,'AI_FORMAT_ERROR')


@app.post('/api/internal/database-restore')
def internal_database_restore():
    """One-time, token-protected PostgreSQL restore path used during migration."""
    restore_token = os.getenv('RESTORE_TOKEN', '')
    supplied_token = request.headers.get('X-Restore-Token', '')
    if not restore_token or not secrets.compare_digest(restore_token, supplied_token):
        return jsonify({'error': 'not found'}), 404
    if request.content_length is None or request.content_length > 2 * 1024 * 1024:
        return jsonify({'error': 'invalid restore file'}), 413
    dump = request.get_data(cache=False, as_text=True)
    if 'CREATE TABLE public.meetings' not in dump:
        return jsonify({'error': 'invalid dump'}), 400
    start = dump.find('SET statement_timeout = 0;', dump.find('\\connect medprk_'))
    end = dump.find('\\connect postgres', start)
    if start < 0 or end < 0:
        return jsonify({'error': 'database section not found'}), 400
    section = dump[start:end]
    with _connect() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
                lines = section.splitlines()
                sql_lines = []
                index = 0
                while index < len(lines):
                    line = lines[index]
                    if line.startswith('COPY ') and line.endswith(' FROM stdin;'):
                        if sql_lines:
                            cur.execute('\n'.join(sql_lines))
                            sql_lines = []
                        copy_sql = line
                        index += 1
                        rows = []
                        while index < len(lines) and lines[index] != '\\.':
                            rows.append(lines[index])
                            index += 1
                        cur.copy_expert(copy_sql, io.StringIO('\n'.join(rows) + '\n'))
                    elif not line.startswith('\\'):
                        sql_lines.append(line)
                    index += 1
                if sql_lines:
                    cur.execute('\n'.join(sql_lines))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return jsonify({'error': type(exc).__name__, 'detail': str(exc)[:1000]}), 500
    global _schema_ready
    _schema_ready = False
    init_schema()
    return jsonify({'status': 'restored'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','8000')))
