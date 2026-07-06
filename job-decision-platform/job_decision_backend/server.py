from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from database import (
    DB_PATH,
    BACKEND_DIR,
    DATA_DIR,
    ensure_schema,
    get_active_resume,
    get_ai_config,
    get_active_ai_config,
    list_ai_configs,
    set_active_provider,
    get_job,
    get_state,
    insert_analysis,
    list_analyses,
    list_crawl_batches,
    list_jobs,
    set_state,
    upsert_ai_config,
    upsert_jobs_batch,
    upsert_resume,
    update_job_status,
)
from ai_router import AIAdapterFactory, AIRouter, resolve_ai_config

BASE_DIR = Path('/Users/vantawork/Documents/Ai/Claude code')
HTML_PATH = BASE_DIR / '求职决策台-8.html'
JOB_TRACKER_DIR = Path('/Users/vantawork/Documents/Ai/Hermes/job-tracker')
ENV_PATH = BACKEND_DIR / '.env'


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV = _load_env()
HOST = ENV.get('JOB_DECISION_HOST', '127.0.0.1')
PORT = int(ENV.get('JOB_DECISION_PORT', '8787'))
APP_MODE = ENV.get('JOB_DECISION_APP_MODE', 'local')
DEFAULT_PROVIDER = ENV.get('JOB_DECISION_AI_PROVIDER', 'local')
DEFAULT_BASE_URL = ENV.get('JOB_DECISION_AI_BASE_URL', '')
DEFAULT_MODEL = ENV.get('JOB_DECISION_AI_MODEL', '')
DEFAULT_KEY_REF = ENV.get('JOB_DECISION_AI_KEY_REF', '')
DEFAULT_API_KEY = ENV.get('JOB_DECISION_AI_API_KEY') or os.environ.get('JOB_DECISION_AI_API_KEY', '')

ensure_schema(DB_PATH)
app = FastAPI(title='Job Decision Backend', version='1.0.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.mount('/static', StaticFiles(directory=str(BASE_DIR), html=False), name='static')
if JOB_TRACKER_DIR.exists():
    app.mount('/job-tracker', StaticFiles(directory=str(JOB_TRACKER_DIR), html=False), name='job_tracker')



class StatePayload(BaseModel):
    state_key: str = Field(..., min_length=1)
    state_json: dict[str, Any] = Field(default_factory=dict)


class ResumePayload(BaseModel):
    name: str = '未命名简历'
    content_text: str = ''
    file_name: str = ''
    file_hash: str = ''
    is_active: bool = True


class JobsImportPayload(BaseModel):
    source: str
    crawl_date: str
    items: list


class JobStatusPayload(BaseModel):
    status: Optional[str] = None
    score: Optional[float] = None
    is_duplicate: Optional[bool] = None


class AIConfigPayload(BaseModel):
    provider: str = 'local'
    base_url: str = ''
    model: str = ''
    key_ref: str = ''
    api_key: str = ''
    enabled: bool = True
    is_active: bool = False
    fallback_providers: str = ''
    display_name: str = ''


class AnalyzePayload(BaseModel):
    resume_id: Optional[int] = None
    job_id: Optional[int] = None
    analysis_type: str = 'general'
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get('/', response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding='utf-8'))

@app.get('/api/health')
def api_health() -> dict[str, Any]:
    return {
        'ok': True,
        'app_mode': APP_MODE,
        'host': HOST,
        'port': PORT,
        'db_path': str(DB_PATH),
        'html_path': str(HTML_PATH),
    }


@app.get('/api/state')
def api_get_state(state_key: str = Query(default='ui_state')) -> dict[str, Any]:
    row = get_state(state_key, DB_PATH)
    state = {}
    if row and row.get('state_json'):
        parsed = _safe_json_loads(row.get('state_json') or '')
        state = parsed if isinstance(parsed, dict) else {}
    return {'ok': True, 'state': state, 'row': row}


@app.post('/api/state')
def api_set_state(payload: StatePayload) -> dict[str, Any]:
    row = set_state(payload.state_key, payload.state_json, DB_PATH)
    return {'ok': True, 'state': row}


@app.get('/api/resume/active')
def api_get_resume_active() -> dict[str, Any]:
    row = get_active_resume(DB_PATH)
    return {'ok': True, 'resume': row}


@app.post('/api/resume')
def api_post_resume(payload: ResumePayload) -> dict[str, Any]:
    row = upsert_resume(payload.model_dump(), DB_PATH)
    return {'ok': True, 'resume': row}


@app.get('/api/jobs')
def api_get_jobs(
    source: Optional[str] = None,
    status: Optional[str] = None,
    crawl_date: Optional[str] = None,
    keyword: Optional[str] = None,
    page_size: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    jobs = list_jobs({'source': source, 'status': status, 'crawl_date': crawl_date, 'keyword': keyword, 'page_size': page_size, 'offset': offset}, DB_PATH)
    return {'ok': True, 'items': jobs, 'count': len(jobs)}


@app.post('/api/jobs/import')
def api_import_jobs(payload: JobsImportPayload) -> dict[str, Any]:
    result = upsert_jobs_batch(payload.source, payload.crawl_date, payload.items, DB_PATH)
    return {'ok': True, **result}


@app.get('/api/jobs/{job_id}')
def api_get_job(job_id: int) -> dict[str, Any]:
    row = get_job(job_id, DB_PATH)
    if not row:
        raise HTTPException(status_code=404, detail='job not found')
    return {'ok': True, 'job': row}


@app.patch('/api/jobs/{job_id}/status')
def api_patch_job_status(job_id: int, payload: JobStatusPayload) -> dict[str, Any]:
    row = update_job_status(job_id, payload.model_dump(exclude_none=True), DB_PATH)
    if not row:
        raise HTTPException(status_code=404, detail='job not found')
    return {'ok': True, 'job': row}


@app.get('/api/crawl-batches')
def api_get_crawl_batches(crawl_date: Optional[str] = None, source: Optional[str] = None) -> dict[str, Any]:
    rows = list_crawl_batches({'crawl_date': crawl_date, 'source': source}, DB_PATH)
    return {'ok': True, 'items': rows, 'count': len(rows)}


@app.get('/api/ai/config')
def api_get_ai_config(provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
    row = get_ai_config(provider, DB_PATH)
    if row is None:
        row = {
            'provider': provider,
            'base_url': DEFAULT_BASE_URL,
            'model': DEFAULT_MODEL,
            'key_ref': DEFAULT_KEY_REF,
            'api_key': '',
            'enabled': 1,
            'is_active': 0,
            'fallback_providers': '',
            'display_name': '',
        }
    safe = _sanitize_ai_config(row)
    return {'ok': True, 'config': safe}


@app.post('/api/ai/config')
def api_post_ai_config(payload: AIConfigPayload) -> dict[str, Any]:
    row = upsert_ai_config(payload.model_dump(), DB_PATH)
    safe = _sanitize_ai_config(row)
    return {'ok': True, 'config': safe}


@app.get('/api/ai/providers')
def api_get_providers() -> dict[str, Any]:
    """List all AI provider configs."""
    configs = list_ai_configs(DB_PATH)
    safe_configs = [_sanitize_ai_config(c) for c in configs]
    active = get_active_ai_config(DB_PATH)
    return {
        'ok': True,
        'providers': safe_configs,
        'active_provider': active.get('provider') if active else None,
        'supported_types': AIAdapterFactory.supported_providers(),
    }


@app.post('/api/ai/providers/activate')
def api_activate_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """Set active provider by provider name."""
    provider = payload.get('provider')
    if not provider:
        raise HTTPException(status_code=400, detail='provider is required')
    row = set_active_provider(provider, DB_PATH)
    if not row:
        raise HTTPException(status_code=404, detail=f'provider "{provider}" not found')
    safe = _sanitize_ai_config(row)
    return {'ok': True, 'config': safe}


@app.post('/api/ai/providers/test')
def api_test_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """Test connection to a provider with a minimal request."""
    provider = payload.get('provider')
    if not provider:
        raise HTTPException(status_code=400, detail='provider is required')
    config = get_ai_config(provider, DB_PATH)
    if not config:
        raise HTTPException(status_code=404, detail=f'provider "{provider}" not found')
    resolved = resolve_ai_config(config)
    if not resolved.get('base_url') or not resolved.get('model'):
        return {'ok': False, 'error': 'Provider missing base_url or model'}
    if not (resolved.get('api_key') or '').strip():
        return {
            'ok': False,
            'status': 'missing_runtime_key',
            'provider': provider,
            'runtime_key_source': resolved.get('runtime_key_source') or None,
            'error': 'Provider runtime API key is not available',
        }
    # Minimal test request
    test_messages = [
        {'role': 'system', 'content': 'Respond with: {"score":100,"summary":"connection test ok","skills":[],"match_level":"high"}'},
        {'role': 'user', 'content': 'test'},
    ]
    try:
        adapter = AIAdapterFactory.create(resolved)
        result = adapter.call(test_messages, temperature=0.0)
        return {
            'ok': True,
            'status': 'ok',
            'provider': result.get('provider'),
            'model': result.get('model'),
            'runtime_key_source': resolved.get('runtime_key_source') or None,
            'elapsed_ms': result.get('elapsed_ms'),
            'score': result.get('score'),
            'summary': result.get('summary'),
        }
    except Exception as exc:
        return {
            'ok': False,
            'status': 'provider_error',
            'provider': provider,
            'runtime_key_source': resolved.get('runtime_key_source') or None,
            'error': str(exc)[:300],
        }


@app.get('/api/ai/providers/{provider}/models')
def api_get_provider_models(provider: str) -> dict[str, Any]:
    config = get_ai_config(provider, DB_PATH)
    if not config:
        raise HTTPException(status_code=404, detail=f'provider "{provider}" not found')
    resolved = resolve_ai_config(config)
    if not resolved.get('base_url'):
        return {'ok': False, 'provider': provider, 'models': [], 'error': 'Provider missing base_url'}
    try:
        adapter = AIAdapterFactory.create(resolved)
        result = adapter.list_models()
        return {
            'ok': True,
            'provider': provider,
            'models': result.get('models') or [],
            'elapsed_ms': result.get('elapsed_ms'),
        }
    except Exception as exc:
        return {'ok': False, 'provider': provider, 'models': [], 'error': str(exc)[:300]}


def _sanitize_ai_config(row: dict[str, Any] | None) -> dict[str, Any]:
    """Remove api_key from config for API responses."""
    if not row:
        return {}
    safe = dict(row)
    # Never expose api_key in responses — only show masked
    raw_key = safe.get('api_key') or safe.get('key_ref') or ''
    safe['api_key_masked'] = (raw_key[:4] + '****' + raw_key[-4:]) if len(raw_key) > 8 else ('****' if raw_key else '')
    safe['api_key'] = ''
    safe['key_ref'] = ''
    return safe


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_ai_text(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data or '')
    choices = data.get('choices')
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get('message') if isinstance(first, dict) else None
        if isinstance(message, dict) and message.get('content'):
            return str(message.get('content'))
        if isinstance(first, dict) and first.get('text'):
            return str(first.get('text'))
    content = data.get('content')
    if isinstance(content, list):
        return '\n'.join(str(part.get('text') or part.get('value') or '') for part in content if isinstance(part, dict))
    return str(data.get('response') or data.get('text') or data.get('message') or data.get('result') or '')


def _build_analysis_prompt(resume: dict[str, Any] | None, job: dict[str, Any] | None, analysis_type: str, extra_payload: dict[str, Any]) -> list[dict[str, str]]:
    resume_text = (resume or {}).get('content_text') or ''
    job_text = '\n'.join([
        f"岗位：{(job or {}).get('title') or ''}",
        f"公司：{(job or {}).get('company') or ''}",
        f"地点：{(job or {}).get('location') or ''}",
        f"薪资：{(job or {}).get('salary') or ''}",
        f"描述：{(job or {}).get('description') or ''}",
        f"公司信息：{(job or {}).get('company_info') or ''}",
    ])
    return [
        {
            'role': 'system',
            'content': '你是求职决策台的本地后端分析器。只输出 JSON，不要输出 Markdown。字段：score(0-100数字)、summary(中文短摘要)、strengths(数组)、risks(数组)、next_steps(数组)。不得包含任何 API Key 或密钥。',
        },
        {
            'role': 'user',
            'content': json.dumps({
                'analysis_type': analysis_type,
                'resume': resume_text,
                'job': job_text,
                'payload': {k: v for k, v in (extra_payload or {}).items() if k != 'ai_config'},
            }, ensure_ascii=False),
        },
    ]


def _standardize_analysis_result(parsed: dict[str, Any] | Any, raw_text: str = '') -> dict[str, Any]:
    if isinstance(parsed, dict):
        result = dict(parsed)
    else:
        result = {}
        raw_text = raw_text or str(parsed or '')
    summary = str(result.get('summary') or result.get('message') or result.get('text') or raw_text or '').strip()
    match_reason = str(result.get('match_reason') or result.get('matchReason') or result.get('reason') or '').strip()
    raw_risks = result.get('risk_flags') or result.get('riskFlags') or result.get('risks') or []
    if isinstance(raw_risks, list):
        risk_flags = [str(item).strip() for item in raw_risks if str(item).strip()]
    elif raw_risks:
        risk_flags = [str(raw_risks).strip()]
    else:
        risk_flags = []
    score = result.get('score') if isinstance(result.get('score'), (int, float)) else 0
    result.update({
        'score': score,
        'summary': summary,
        'match_reason': match_reason,
        'risk_flags': risk_flags,
    })
    return result


@app.post('/api/ai/analyze')
def api_post_ai_analyze(payload: AnalyzePayload) -> dict[str, Any]:
    base_url = (os.environ.get('JOB_DECISION_AI_BASE_URL') or DEFAULT_BASE_URL).strip().rstrip('/')
    model = (os.environ.get('JOB_DECISION_AI_MODEL') or DEFAULT_MODEL).strip()
    api_key = (os.environ.get('JOB_DECISION_AI_API_KEY') or DEFAULT_API_KEY).strip()
    missing = [name for name, value in [
        ('JOB_DECISION_AI_API_KEY', api_key),
        ('JOB_DECISION_AI_BASE_URL', base_url),
        ('JOB_DECISION_AI_MODEL', model),
    ] if not value]
    if missing:
        raise HTTPException(status_code=503, detail={'error': 'AI backend env not configured', 'missing': missing})
    if payload.resume_id is None or payload.job_id is None:
        raise HTTPException(status_code=400, detail='resume_id and job_id are required')
    resume = get_active_resume(DB_PATH)
    if not resume:
        raise HTTPException(status_code=400, detail='No active resume found — upload a resume first')
    job = get_job(payload.job_id, DB_PATH)
    if not job:
        raise HTTPException(status_code=404, detail='job not found')
    messages = _build_analysis_prompt(resume, job, payload.analysis_type, payload.payload)
    body = json.dumps({'model': model, 'messages': messages, 'temperature': 0.2, 'stream': False}, ensure_ascii=False).encode('utf-8')
    req = urlrequest.Request(
        f'{base_url}/chat/completions',
        data=body,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
        method='POST',
    )
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            response_data = json.loads(resp.read().decode('utf-8'))
    except urlerror.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:500]
        raise HTTPException(status_code=502, detail={'error': 'AI provider HTTP error', 'status': exc.code, 'detail': detail}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={'error': 'AI provider request failed', 'detail': str(exc)[:300]}) from exc
    text = _extract_ai_text(response_data)
    parsed = _safe_json_loads(text) if text else None
    if not isinstance(parsed, dict):
        parsed = {'summary': text[:1000] if text else 'AI returned empty response'}
    parsed = _standardize_analysis_result(parsed, text)
    score = parsed.get('score') if isinstance(parsed.get('score'), (int, float)) else 0
    result_json = {
        'status': 'ok',
        'provider': 'openai-compatible',
        'model': model,
        'analysis_type': payload.analysis_type,
        'result': parsed,
    }
    row = insert_analysis({
        'resume_id': payload.resume_id,
        'job_id': payload.job_id,
        'analysis_type': payload.analysis_type,
        'score': score,
        'summary': str(parsed.get('summary') or text or '')[:1000],
        'result_json': result_json,
    }, DB_PATH)
    return {'ok': True, 'analysis': row, 'result': parsed}


class GenericAnalyzePayload(BaseModel):
    analysis_type: str = 'capability_match'
    messages: list[dict[str, str]] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


@app.post('/api/ai/analyze-generic')
def api_post_ai_analyze_generic(payload: GenericAnalyzePayload) -> dict[str, Any]:
    """Generic AI analysis — routes through AI Router with fallback support.

    Uses active DB provider config, falling back to ENV vars.
    Frontend must NOT send API keys.
    """
    # Resolve active config from DB, merge with ENV
    db_config = get_active_ai_config(DB_PATH)
    resolved = resolve_ai_config(db_config)

    if not resolved.get('base_url') or not resolved.get('model'):
        missing = [name for name, value in [
            ('JOB_DECISION_AI_BASE_URL', resolved.get('base_url') or ''),
            ('JOB_DECISION_AI_MODEL', resolved.get('model') or ''),
        ] if not value]
        raise HTTPException(status_code=503, detail={'error': 'AI backend not configured', 'missing': missing})

    # Build fallback configs from fallback_providers string
    fallback_configs = []
    if db_config and db_config.get('fallback_providers'):
        for fb_provider in [p.strip() for p in db_config['fallback_providers'].split(',') if p.strip()]:
            fb_config = get_ai_config(fb_provider, DB_PATH)
            if fb_config:
                fallback_configs.append(resolve_ai_config(fb_config))

    router = AIRouter(active_config=resolved, fallback_configs=fallback_configs)
    messages = payload.messages or _build_analysis_prompt(None, None, payload.analysis_type, payload.payload)
    route_result = router.call(messages, temperature=0.2)

    if not route_result.get('ok'):
        raise HTTPException(status_code=502, detail={
            'error': 'AI router all providers failed',
            'last_error': route_result.get('error'),
            'trace': route_result.get('trace'),
        })

    result = route_result['result']
    parsed = _standardize_analysis_result(result, result.get('summary', ''))
    return {
        'ok': True,
        'status': 'ok',
        'provider': route_result.get('provider'),
        'model': route_result.get('model'),
        'analysis_type': payload.analysis_type,
        'result': parsed,
        'trace': route_result.get('trace'),
    }


@app.get('/api/analyses')
def api_get_analyses(resume_id: Optional[int] = None, job_id: Optional[int] = None, analysis_type: Optional[str] = None) -> dict[str, Any]:
    rows = list_analyses({'resume_id': resume_id, 'job_id': job_id, 'analysis_type': analysis_type}, DB_PATH)
    return {'ok': True, 'items': rows, 'count': len(rows)}


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level='info')


if __name__ == '__main__':
    main()
