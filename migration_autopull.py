"""Temporary server-to-server migration runner for the space_02 clone."""
import base64
import hashlib
import io
import json
import os
import threading
from pathlib import Path

import requests
from flask import jsonify
from psycopg2 import sql

from migration_bridge import DATA, _sha256, _table_counts, _table_names, app, connect

_STATE = {"status": "idle", "files_done": 0, "files_total": 0, "bytes_done": 0, "bytes_total": 0}
_LOCK = threading.Lock()


def _set(**values):
    with _LOCK:
        _STATE.update(values)


def _restore_database(payload):
    tables = payload.get("tables") or []
    names = [item.get("table") for item in tables]
    with connect() as conn:
        existing = set(_table_names(conn))
        if set(names) != existing:
            raise RuntimeError("database table set mismatch")
        with conn.cursor() as cur:
            joined = sql.SQL(", ").join(sql.Identifier(name) for name in names)
            cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(joined))
            for item in tables:
                raw = base64.b64decode(item["csv_base64"], validate=True)
                if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                    raise RuntimeError("database checksum mismatch")
                query = sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV, HEADER TRUE)").format(
                    sql.Identifier(item["table"])
                )
                cur.copy_expert(query.as_string(conn), io.StringIO(raw.decode("utf-8")))
        conn.commit()
        return _table_counts(conn)


def _target_path(relative):
    root = Path(DATA).resolve()
    target = (root / str(relative).replace("\\", "/").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError("invalid migration path")
    return target


def _run():
    source = os.getenv("MIGRATION_SOURCE_URL", "").rstrip("/")
    token = os.getenv("MIGRATION_TOKEN", "")
    if not source or not token:
        _set(status="disabled")
        return
    headers = {"Cookie": f"migration_token={token}"}
    try:
        _set(status="reading_source")
        manifest_response = requests.get(
            f"{source}/_internal/migration/manifest", headers=headers, timeout=300
        )
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        _set(
            status="restoring_database",
            files_total=int(manifest["file_count"]),
            bytes_total=int(manifest["file_bytes"]),
        )
        database_response = requests.get(
            f"{source}/_internal/migration/database", headers=headers, timeout=300
        )
        database_response.raise_for_status()
        restored_counts = _restore_database(database_response.json())
        if restored_counts != manifest["tables"]:
            raise RuntimeError("database row counts differ after restore")

        _set(status="copying_files")
        root = Path(DATA).resolve()
        expected_paths = set()
        for index, item in enumerate(manifest["files"], 1):
            relative = item["path"]
            expected_paths.add(relative)
            target = _target_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == int(item["size"]) and _sha256(target) == item["sha256"]:
                _set(files_done=index, bytes_done=_STATE["bytes_done"] + int(item["size"]))
                continue
            response = requests.get(
                f"{source}/_internal/migration/file",
                headers=headers,
                params={"path": relative},
                stream=True,
                timeout=300,
            )
            response.raise_for_status()
            part = Path(str(target) + ".migration-part")
            digest = hashlib.sha256()
            size = 0
            with open(part, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    _set(bytes_done=_STATE["bytes_done"] + len(chunk))
            if size != int(item["size"]) or digest.hexdigest() != item["sha256"]:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"file checksum mismatch: {relative}")
            os.replace(part, target)
            _set(files_done=index)

        actual = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.name.endswith(".migration-part"):
                rel = path.relative_to(root).as_posix()
                actual[rel] = (path.stat().st_size, _sha256(path))
        expected = {item["path"]: (int(item["size"]), item["sha256"]) for item in manifest["files"]}
        if actual != expected:
            raise RuntimeError("file manifest differs after restore")
        with connect() as conn:
            final_counts = _table_counts(conn)
        if final_counts != manifest["tables"]:
            raise RuntimeError("database row counts differ during final verification")
        _set(status="verified", tables=final_counts, files_done=len(expected), bytes_done=sum(v[0] for v in expected.values()))
    except Exception as exc:
        _set(status="failed", error=str(exc)[:500])


@app.get("/_internal/migration/status")
def migration_status():
    with _LOCK:
        payload = dict(_STATE)
    code = 200 if payload.get("status") == "verified" else 500 if payload.get("status") == "failed" else 202
    return jsonify(payload), code


if os.getenv("MIGRATION_AUTO_PULL") == "1":
    threading.Thread(target=_run, name="meeting-migration", daemon=True).start()
