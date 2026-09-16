"""Temporary, token-protected bridge for verified project migration."""
import base64
import csv
import hashlib
import hmac
import io
import os
from pathlib import Path

from flask import Response, abort, jsonify, request, send_file
from psycopg2 import sql

from app import DATA, app, connect

_MAX_CHUNK = 1024 * 1024


@app.get("/_internal/migration/ping")
def migration_ping():
    return jsonify(status="ready")


@app.get("/_internal/migration/auth-check")
def migration_auth_check():
    expected = os.getenv("MIGRATION_TOKEN", "")
    supplied = request.headers.get("X-Migration-Token", "")
    authorization = request.headers.get("Authorization", "")
    if not supplied and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    return jsonify(
        expected_present=bool(expected),
        supplied_present=bool(supplied),
        matches=bool(expected) and hmac.compare_digest(expected, supplied),
        expected_sha256=hashlib.sha256(expected.encode()).hexdigest() if expected else "",
        supplied_sha256=hashlib.sha256(supplied.encode()).hexdigest() if supplied else "",
    )


def _authorized():
    expected = os.getenv("MIGRATION_TOKEN", "")
    supplied = request.headers.get("X-Migration-Token", "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            supplied = authorization[7:].strip()
    if not supplied:
        supplied = request.cookies.get("migration_token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _require_auth():
    if not _authorized():
        abort(404)


def _safe_path(relative):
    relative = str(relative or "").replace("\\", "/").lstrip("/")
    root = Path(DATA).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        abort(400)
    return target


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT table_name FROM information_schema.tables
               WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                 AND table_type='BASE TABLE'
               ORDER BY table_name"""
        )
        return [row[0] for row in cur.fetchall()]


def _table_counts(conn):
    counts = {}
    with conn.cursor() as cur:
        for table in _table_names(conn):
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
            counts[table] = int(cur.fetchone()[0])
    return counts


@app.get("/_internal/migration/manifest")
def migration_manifest():
    _require_auth()
    files = []
    root = Path(DATA).resolve()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".migration-part"):
            stat = path.stat()
            files.append({
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "sha256": _sha256(path),
            })
    with connect() as conn:
        counts = _table_counts(conn)
    return jsonify(
        tables=counts,
        files=files,
        file_count=len(files),
        file_bytes=sum(item["size"] for item in files),
    )


@app.get("/_internal/migration/database")
def migration_database_export():
    _require_auth()
    exported = []
    with connect() as conn:
        for table in _table_names(conn):
            buffer = io.StringIO()
            with conn.cursor() as cur:
                query = sql.SQL("COPY {} TO STDOUT WITH (FORMAT CSV, HEADER TRUE)").format(
                    sql.Identifier(table)
                )
                cur.copy_expert(query.as_string(conn), buffer)
            raw = buffer.getvalue().encode("utf-8")
            exported.append({
                "table": table,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "csv_base64": base64.b64encode(raw).decode("ascii"),
            })
        counts = _table_counts(conn)
    return jsonify(tables=exported, counts=counts)


@app.post("/_internal/migration/database")
def migration_database_import():
    _require_auth()
    payload = request.get_json(force=True)
    tables = payload.get("tables") or []
    names = [item.get("table") for item in tables]
    if not names or any(not name for name in names):
        abort(400)
    with connect() as conn:
        existing = set(_table_names(conn))
        if set(names) != existing:
            return jsonify(error="table_set_mismatch", expected=sorted(existing), received=sorted(names)), 409
        with conn.cursor() as cur:
            joined = sql.SQL(", ").join(sql.Identifier(name) for name in names)
            cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(joined))
            for item in tables:
                raw = base64.b64decode(item["csv_base64"], validate=True)
                if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                    raise ValueError("database export checksum mismatch")
                query = sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV, HEADER TRUE)").format(
                    sql.Identifier(item["table"])
                )
                cur.copy_expert(query.as_string(conn), io.StringIO(raw.decode("utf-8")))
        conn.commit()
        counts = _table_counts(conn)
    return jsonify(status="restored", tables=counts)


@app.get("/_internal/migration/file")
def migration_file_download():
    _require_auth()
    path = _safe_path(request.args.get("path"))
    if not path.is_file() or path.is_symlink():
        abort(404)
    return send_file(path, as_attachment=False, conditional=True)


@app.put("/_internal/migration/file")
def migration_file_upload():
    _require_auth()
    path = _safe_path(request.args.get("path"))
    try:
        offset = int(request.args.get("offset", "0"))
        total = int(request.args.get("total", "-1"))
    except ValueError:
        abort(400)
    expected_hash = (request.args.get("sha256") or "").lower()
    chunk = request.get_data(cache=False)
    if len(chunk) > _MAX_CHUNK or offset < 0 or total < 0 or offset + len(chunk) > total:
        abort(413)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(path) + ".migration-part")
    if path.exists() and path.is_file() and path.stat().st_size == total and _sha256(path) == expected_hash:
        return jsonify(received=total, complete=True)
    received = part.stat().st_size if part.exists() else 0
    if offset < received:
        return jsonify(received=received, complete=received == total)
    if offset != received:
        return jsonify(error="offset_mismatch", received=received), 409
    with open(part, "ab") as handle:
        handle.write(chunk)
    received += len(chunk)
    complete = received == total
    if complete:
        if _sha256(part) != expected_hash:
            part.unlink(missing_ok=True)
            return jsonify(error="checksum_mismatch"), 422
        os.replace(part, path)
    return jsonify(received=received, complete=complete)


@app.get("/_internal/migration/count-at-least/<table>/<int:threshold>")
def migration_count_probe(table, threshold):
    with connect() as conn:
        names = set(_table_names(conn))
        if table not in names:
            abort(404)
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
            count = int(cur.fetchone()[0])
    return ("", 204) if count >= threshold else ("", 404)


@app.get("/_internal/migration/count-probe")
def migration_count_probe_query():
    table = request.args.get("table", "")
    try:
        threshold = int(request.args.get("threshold", "0"))
    except ValueError:
        abort(400)
    with connect() as conn:
        names = set(_table_names(conn))
        if table not in names:
            abort(404)
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
            count = int(cur.fetchone()[0])
    return ("", 204) if count >= threshold else ("", 404)


@app.get("/_internal/migration/verify")
def migration_verify():
    _require_auth()
    with connect() as conn:
        counts = _table_counts(conn)
    return jsonify(tables=counts)
