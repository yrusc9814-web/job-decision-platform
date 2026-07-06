from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path('/Users/vantawork/Documents/Ai/Claude code')
BACKEND_DIR = BASE_DIR / 'job_decision_backend'
DATA_DIR = BASE_DIR / 'data'
DB_PATH = DATA_DIR / 'job_decision.db'
SCHEMA_PATH = BACKEND_DIR / 'schema.sql'
ENV_PATH = BACKEND_DIR / '.env'

_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        env[key] = value
    return env


@contextmanager
def connect_db(db_path: Path | str = DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(db_path: Path | str = DB_PATH) -> None:
    BACKEND_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f'missing schema file: {SCHEMA_PATH}')
    schema = SCHEMA_PATH.read_text(encoding='utf-8')
    with connect_db(db_path) as conn:
        conn.executescript(schema)
        conn.execute(
            'INSERT OR IGNORE INTO app_state(state_key, state_json, updated_at) VALUES (?, ?, ?)',
            ('migration_done', json.dumps({'done': False}, ensure_ascii=False), now_iso()),
        )
        # ── Migration: add new columns to existing ai_configs table ──
        existing_cols = {row['name'] for row in conn.execute('PRAGMA table_info(ai_configs)').fetchall()}
        migrations = [
            ('api_key', "ALTER TABLE ai_configs ADD COLUMN api_key TEXT NOT NULL DEFAULT ''"),
            ('is_active', "ALTER TABLE ai_configs ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))"),
            ('fallback_providers', "ALTER TABLE ai_configs ADD COLUMN fallback_providers TEXT NOT NULL DEFAULT ''"),
            ('display_name', "ALTER TABLE ai_configs ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, sql in migrations:
            if col_name not in existing_cols:
                try:
                    conn.execute(sql)
                except Exception:
                    pass  # Column may already exist from a prior partial migration


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_state(state_key: str, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect_db(db_path) as conn:
        row = conn.execute('SELECT * FROM app_state WHERE state_key = ?', (state_key,)).fetchone()
        return row_to_dict(row)


def set_state(state_key: str, state_json: dict[str, Any], db_path: Path | str = DB_PATH) -> dict[str, Any]:
    payload = json.dumps(state_json, ensure_ascii=False)
    with connect_db(db_path) as conn:
        conn.execute(
            '''
            INSERT INTO app_state(state_key, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
              state_json = excluded.state_json,
              updated_at = excluded.updated_at
            ''',
            (state_key, payload, now_iso()),
        )
        row = conn.execute('SELECT * FROM app_state WHERE state_key = ?', (state_key,)).fetchone()
        return row_to_dict(row) or {}


def get_active_resume(db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect_db(db_path) as conn:
        row = conn.execute('SELECT * FROM resumes WHERE is_active = 1 ORDER BY updated_at DESC, id DESC LIMIT 1').fetchone()
        return row_to_dict(row)


def upsert_resume(payload: dict[str, Any], db_path: Path | str = DB_PATH) -> dict[str, Any]:
    now = now_iso()
    file_hash = payload.get('file_hash') or ''
    name = payload.get('name') or '未命名简历'
    content_text = payload.get('content_text') or ''
    file_name = payload.get('file_name') or ''
    is_active = 1 if payload.get('is_active') else 0
    with connect_db(db_path) as conn:
        if is_active:
            conn.execute('UPDATE resumes SET is_active = 0, updated_at = ? WHERE is_active = 1', (now,))
        conn.execute(
            '''
            INSERT INTO resumes(name, content_text, file_name, file_hash, created_at, updated_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_hash) DO UPDATE SET
              name = excluded.name,
              content_text = excluded.content_text,
              file_name = excluded.file_name,
              updated_at = excluded.updated_at,
              is_active = excluded.is_active
            ''',
            (name, content_text, file_name, file_hash, now, now, is_active),
        )
        if is_active:
            conn.execute('UPDATE resumes SET is_active = 0 WHERE file_hash <> ?', (file_hash,))
            conn.execute('UPDATE resumes SET is_active = 1, updated_at = ? WHERE file_hash = ?', (now, file_hash))
        row = conn.execute('SELECT * FROM resumes WHERE file_hash = ? LIMIT 1', (file_hash,)).fetchone()
        return row_to_dict(row) or {}


def list_jobs(filters: dict[str, Any] | None = None, db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    filters = filters or {}
    where = ['1=1']
    params: list[Any] = []
    if filters.get('source'):
        where.append('source = ?')
        params.append(filters['source'])
    if filters.get('status'):
        where.append('status = ?')
        params.append(filters['status'])
    if filters.get('crawl_date'):
        where.append('crawl_date = ?')
        params.append(filters['crawl_date'])
    if filters.get('keyword'):
        kw = f"%{filters['keyword']}%"
        where.append('(company LIKE ? OR title LIKE ? OR location LIKE ? OR description LIKE ?)')
        params.extend([kw, kw, kw, kw])
    sql = f'''SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id DESC'''
    limit = int(filters.get('page_size') or 200)
    offset = int(filters.get('offset') or 0)
    sql += ' LIMIT ? OFFSET ?'
    params.extend([limit, offset])
    with connect_db(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_job(job_id: int, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect_db(db_path) as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        return row_to_dict(row)


def _job_item_value(item: dict[str, Any], *keys: str, default: Any = '') -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ''):
            return value
    return default


def _job_text(value: Any) -> str:
    if value in (None, ''):
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _build_source_job_id(source: str, item: dict[str, Any], raw_index: int) -> str:
    explicit = _job_item_value(item, 'source_job_id', 'job_id', 'id', 'dedupeKey', 'sourceUrl', 'source_url', 'job_url', 'url')
    if explicit not in (None, ''):
        return str(explicit)
    title = str(item.get('title') or item.get('jobName') or item.get('name') or '')
    company = str(item.get('company') or '')
    salary = str(item.get('salary') or '')
    collected = str(item.get('collectedAt') or item.get('firstCollectedAt') or item.get('listCollectedAt') or '')
    candidate = '|'.join(part for part in [source, company, title, salary, collected] if part)
    return candidate or f'{source}-{raw_index}'


def upsert_jobs_batch(source: str, crawl_date: str, items: Iterable[dict[str, Any]], db_path: Path | str = DB_PATH) -> dict[str, int]:
    inserted = 0
    updated = 0
    raw_count = 0
    deduped = 0
    now = now_iso()
    with connect_db(db_path) as conn:
        conn.execute(
            '''
            INSERT INTO crawl_batches(crawl_date, source, raw_count, deduped_count, candidate_count, qualified_count, rejected_count, created_at, notes)
            VALUES (?, ?, 0, 0, 0, 0, 0, ?, '')
            ON CONFLICT(crawl_date, source) DO UPDATE SET
              created_at = excluded.created_at
            ''',
            (crawl_date, source, now),
        )
        for item in items:
            raw_count += 1
            if not isinstance(item, dict):
                continue
            item_source = str(item.get('source') or source or 'local')
            source_job_id = _build_source_job_id(item_source, item, raw_count)
            job_detail = item.get('jobDetail') if isinstance(item.get('jobDetail'), dict) else {}
            company_info = item.get('companyInfo') if isinstance(item.get('companyInfo'), dict) else item.get('company_info')
            job_url = _job_text(_job_item_value(item, 'job_url', 'sourceUrl', 'source_url', 'url', default=job_detail.get('sourceUrl') or ''))
            lookup = conn.execute(
                'SELECT id FROM jobs WHERE source = ? AND source_job_id = ?',
                (item_source, source_job_id),
            ).fetchone()
            description = _job_text(_job_item_value(
                item,
                'description',
                'summary',
                'matchDescription',
                default='\n\n'.join(str(part) for part in [job_detail.get('description'), job_detail.get('responsibilities'), job_detail.get('requirements')] if part),
            ))
            payload = {
                'source': item_source,
                'source_job_id': source_job_id,
                'company': _job_text(item.get('company') or (company_info or {}).get('fullName') if isinstance(company_info, dict) else item.get('company')),
                'title': _job_text(_job_item_value(item, 'title', 'jobName', 'name')),
                'location': _job_text(_job_item_value(item, 'location', 'region', 'city')),
                'salary': _job_text(item.get('salary')),
                'experience': _job_text(_job_item_value(item, 'experience', 'workExperience')),
                'education': _job_text(item.get('education')),
                'job_url': job_url,
                'description': description,
                'company_info': _job_text(company_info),
                'raw_json': json.dumps(item, ensure_ascii=False),
                'first_seen_at': _job_text(_job_item_value(item, 'first_seen_at', 'firstCollectedAt', 'listCollectedAt', 'collectedAt', default=now)),
                'last_seen_at': now,
                'crawl_date': crawl_date,
                'status': item.get('status') or 'pending',
                'score': item.get('score') or item.get('finalScore') or item.get('matchScore') or item.get('preScore') or 0,
                'is_duplicate': 1 if item.get('is_duplicate') else 0,
                'updated_at': now,
            }
            if lookup:
                updated += 1
                conn.execute(
                    '''
                    UPDATE jobs SET
                      company = ?, title = ?, location = ?, salary = ?, experience = ?, education = ?,
                      job_url = ?, description = ?, company_info = ?, raw_json = ?, last_seen_at = ?,
                      crawl_date = ?, status = ?, score = ?, is_duplicate = ?, updated_at = ?
                    WHERE id = ?
                    ''',
                    (
                        payload['company'], payload['title'], payload['location'], payload['salary'], payload['experience'],
                        payload['education'], payload['job_url'], payload['description'], payload['company_info'],
                        payload['raw_json'], payload['last_seen_at'], payload['crawl_date'], payload['status'],
                        payload['score'], payload['is_duplicate'], payload['updated_at'], lookup['id'],
                    ),
                )
            else:
                inserted += 1
                conn.execute(
                    '''
                    INSERT INTO jobs(source, source_job_id, company, title, location, salary, experience, education, job_url,
                                     description, company_info, raw_json, first_seen_at, last_seen_at, crawl_date, status, score,
                                     is_duplicate, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        payload['source'], payload['source_job_id'], payload['company'], payload['title'], payload['location'],
                        payload['salary'], payload['experience'], payload['education'], payload['job_url'], payload['description'],
                        payload['company_info'], payload['raw_json'], payload['first_seen_at'], payload['last_seen_at'],
                        payload['crawl_date'], payload['status'], payload['score'], payload['is_duplicate'], now, now,
                    ),
                )
        deduped = inserted + updated
        conn.execute(
            'UPDATE crawl_batches SET raw_count = ?, deduped_count = ?, candidate_count = ?, qualified_count = ?, rejected_count = ? WHERE crawl_date = ? AND source = ?',
            (raw_count, deduped, deduped, 0, 0, crawl_date, source),
        )
    return {'inserted': inserted, 'updated': updated, 'raw_count': raw_count, 'deduped_count': deduped}


def update_job_status(job_id: int, payload: dict[str, Any], db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    fields = []
    values: list[Any] = []
    for key in ('status', 'score', 'is_duplicate'):
        if key in payload:
            fields.append(f'{key} = ?')
            values.append(payload[key])
    if not fields:
        return get_job(job_id, db_path)
    fields.append('updated_at = ?')
    values.append(now_iso())
    values.append(job_id)
    with connect_db(db_path) as conn:
        conn.execute(f'UPDATE jobs SET {", ".join(fields)} WHERE id = ?', values)
        row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        return row_to_dict(row)


def list_crawl_batches(filters: dict[str, Any] | None = None, db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    filters = filters or {}
    where = ['1=1']
    params: list[Any] = []
    if filters.get('source'):
        where.append('source = ?')
        params.append(filters['source'])
    if filters.get('crawl_date'):
        where.append('crawl_date = ?')
        params.append(filters['crawl_date'])
    with connect_db(db_path) as conn:
        rows = conn.execute(f'SELECT * FROM crawl_batches WHERE {" AND ".join(where)} ORDER BY created_at DESC, id DESC', params).fetchall()
        return [dict(row) for row in rows]


def upsert_ai_config(payload: dict[str, Any], db_path: Path | str = DB_PATH) -> dict[str, Any]:
    now = now_iso()
    provider = payload.get('provider') or 'local'
    requested_active = bool(payload.get('is_active'))
    with connect_db(db_path) as conn:
        if requested_active:
            conn.execute('UPDATE ai_configs SET is_active = 0, updated_at = ? WHERE provider != ?', (now, provider))
        conn.execute(
            '''
            INSERT INTO ai_configs(provider, base_url, model, key_ref, api_key, enabled, is_active, fallback_providers, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
              base_url = excluded.base_url,
              model = excluded.model,
              key_ref = excluded.key_ref,
              api_key = excluded.api_key,
              enabled = excluded.enabled,
              is_active = excluded.is_active,
              fallback_providers = excluded.fallback_providers,
              display_name = excluded.display_name,
              updated_at = excluded.updated_at
            ''',
            (
                provider,
                payload.get('base_url') or '',
                payload.get('model') or '',
                payload.get('key_ref') or '',
                payload.get('api_key') or '',
                1 if payload.get('enabled', True) else 0,
                1 if requested_active else 0,
                payload.get('fallback_providers') or '',
                payload.get('display_name') or '',
                now,
                now,
            ),
        )
        if requested_active:
            conn.execute('UPDATE ai_configs SET is_active = 1, enabled = 1, updated_at = ? WHERE provider = ?', (now, provider))
        row = conn.execute('SELECT * FROM ai_configs WHERE provider = ?', (provider,)).fetchone()
        return row_to_dict(row) or {}


def get_ai_config(provider: str = 'local', db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    with connect_db(db_path) as conn:
        row = conn.execute('SELECT * FROM ai_configs WHERE provider = ?', (provider,)).fetchone()
        return row_to_dict(row)


def list_ai_configs(db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    with connect_db(db_path) as conn:
        rows = conn.execute('SELECT * FROM ai_configs ORDER BY is_active DESC, enabled DESC, id ASC').fetchall()
        return [dict(r) for r in rows]


def set_active_provider(provider: str, db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    now = now_iso()
    with connect_db(db_path) as conn:
        existing = conn.execute('SELECT * FROM ai_configs WHERE provider = ?', (provider,)).fetchone()
        if not existing:
            return None
        conn.execute('UPDATE ai_configs SET is_active = 0, updated_at = ?', (now,))
        conn.execute('UPDATE ai_configs SET is_active = 1, enabled = 1, updated_at = ? WHERE provider = ?', (now, provider))
        row = conn.execute('SELECT * FROM ai_configs WHERE provider = ?', (provider,)).fetchone()
        return row_to_dict(row)


def get_active_ai_config(db_path: Path | str = DB_PATH) -> dict[str, Any] | None:
    """Return the active AI config, falling back to ENV vars if no DB config is active."""
    with connect_db(db_path) as conn:
        row = conn.execute('SELECT * FROM ai_configs WHERE is_active = 1 AND enabled = 1 LIMIT 1').fetchone()
        if row:
            return row_to_dict(row)
        # Fallback: first enabled config
        row = conn.execute('SELECT * FROM ai_configs WHERE enabled = 1 ORDER BY id ASC LIMIT 1').fetchone()
        return row_to_dict(row) if row else None


def insert_analysis(payload: dict[str, Any], db_path: Path | str = DB_PATH) -> dict[str, Any]:
    now = now_iso()
    result_json = json.dumps(payload.get('result_json') or {}, ensure_ascii=False)
    with connect_db(db_path) as conn:
        conn.execute(
            '''
            INSERT INTO analyses(resume_id, job_id, analysis_type, score, result_json, summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resume_id, job_id, analysis_type) DO UPDATE SET
              score = excluded.score,
              result_json = excluded.result_json,
              summary = excluded.summary,
              updated_at = excluded.updated_at
            ''',
            (
                payload.get('resume_id'),
                payload.get('job_id'),
                payload.get('analysis_type') or 'general',
                payload.get('score') or 0,
                result_json,
                payload.get('summary') or '',
                now,
                now,
            ),
        )
        row = conn.execute(
            'SELECT * FROM analyses WHERE resume_id = ? AND job_id = ? AND analysis_type = ?',
            (payload.get('resume_id'), payload.get('job_id'), payload.get('analysis_type') or 'general'),
        ).fetchone()
        return row_to_dict(row) or {}


def list_analyses(filters: dict[str, Any] | None = None, db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    filters = filters or {}
    where = ['1=1']
    params: list[Any] = []
    if filters.get('resume_id') is not None:
        where.append('resume_id = ?')
        params.append(filters['resume_id'])
    if filters.get('job_id') is not None:
        where.append('job_id = ?')
        params.append(filters['job_id'])
    if filters.get('analysis_type'):
        where.append('analysis_type = ?')
        params.append(filters['analysis_type'])
    with connect_db(db_path) as conn:
        rows = conn.execute(f'SELECT * FROM analyses WHERE {" AND ".join(where)} ORDER BY updated_at DESC, id DESC', params).fetchall()
        return [dict(row) for row in rows]
