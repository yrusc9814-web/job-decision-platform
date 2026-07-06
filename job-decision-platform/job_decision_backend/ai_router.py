"""AI Router System — multi-provider adapter layer with fallback support.

Providers:
  - openai: OpenAI-compatible API (OpenAI, Azure OpenAI, NVIDIA, etc.)
  - deepseek: DeepSeek native API (OpenAI-compatible format)
  - custom: Custom HTTP endpoint

All adapters return unified output:
  {
    "score": number,
    "summary": string,
    "skills": [],
    "match_level": "low | medium | high",
    "provider": string,
    "model": string,
    "raw_response": dict
  }
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger('ai_router')


# ────────────────────────────────────────────────────────────
# Unified output schema
# ────────────────────────────────────────────────────────────

UNIFIED_SCHEMA = {
    'score': 0,
    'summary': '',
    'skills': [],
    'match_level': 'low',
    'match_reason': '',
    'risk_flags': [],
}


def _standardize_result(parsed: dict[str, Any] | Any, raw_text: str = '') -> dict[str, Any]:
    """Normalize AI response to unified schema."""
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
    skills = result.get('skills') or result.get('strengths') or []
    if not isinstance(skills, list):
        skills = [str(skills)]
    match_level = str(result.get('match_level') or result.get('matchLevel') or 'low').lower()
    if match_level not in ('low', 'medium', 'high'):
        match_level = 'low'
    result.update({
        'score': score,
        'summary': summary,
        'skills': [str(s) for s in skills],
        'match_level': match_level,
        'match_reason': match_reason,
        'risk_flags': risk_flags,
    })
    return result


def _extract_ai_text(data: Any) -> str:
    """Extract text content from OpenAI-compatible response."""
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


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────
# AI Adapter Interface
# ────────────────────────────────────────────────────────────

class AIAdapter:
    """Base adapter interface."""

    provider_name: str = 'base'

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = (config.get('base_url') or '').strip().rstrip('/')
        self.model = (config.get('model') or '').strip()
        self.api_key = (config.get('api_key') or config.get('key_ref') or '').strip()
        self.timeout = int(config.get('timeout') or 120)

    def call(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        raise NotImplementedError

    def list_models(self) -> dict[str, Any]:
        """Best-effort model discovery for OpenAI-compatible providers."""
        if not self.base_url:
            raise ValueError('base_url is required')
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        req = urlrequest.Request(
            f'{self.base_url}/models',
            headers=headers,
            method='GET',
        )
        t0 = time.time()
        with urlrequest.urlopen(req, timeout=min(self.timeout, 30)) as resp:
            response_data = json.loads(resp.read().decode('utf-8'))
        elapsed = int((time.time() - t0) * 1000)
        items = response_data.get('data') if isinstance(response_data, dict) else None
        models = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get('id'):
                    models.append(str(item.get('id')))
        return {
            'ok': True,
            'models': models,
            'elapsed_ms': elapsed,
            'raw_response': response_data,
        }

    def _make_openai_compatible_request(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Shared OpenAI-compatible HTTP call logic."""
        body = json.dumps({
            'model': self.model,
            'messages': messages,
            'temperature': kwargs.get('temperature', 0.2),
            'stream': False,
        }, ensure_ascii=False).encode('utf-8')
        req = urlrequest.Request(
            f'{self.base_url}/chat/completions',
            data=body,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}',
            },
            method='POST',
        )
        t0 = time.time()
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            response_data = json.loads(resp.read().decode('utf-8'))
        elapsed = time.time() - t0
        text = _extract_ai_text(response_data)
        parsed = _safe_json_loads(text) if text else None
        if not isinstance(parsed, dict):
            parsed = {'summary': text[:1000] if text else 'AI returned empty response'}
        result = _standardize_result(parsed, text)
        result['provider'] = self.provider_name
        result['model'] = self.model
        result['elapsed_ms'] = int(elapsed * 1000)
        result['raw_response'] = response_data
        return result


# ────────────────────────────────────────────────────────────
# Concrete Adapters
# ────────────────────────────────────────────────────────────

class OpenAIAdapter(AIAdapter):
    """OpenAI / OpenAI-compatible provider (OpenAI, NVIDIA, Azure, etc.)."""
    provider_name = 'openai'

    def call(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        return self._make_openai_compatible_request(messages, **kwargs)


class DeepSeekAdapter(AIAdapter):
    """DeepSeek native API (uses OpenAI-compatible format)."""
    provider_name = 'deepseek'

    def call(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        return self._make_openai_compatible_request(messages, **kwargs)


class CustomAdapter(AIAdapter):
    """Custom HTTP endpoint — configurable URL and headers."""
    provider_name = 'custom'

    def call(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        # Try OpenAI-compatible format first
        try:
            return self._make_openai_compatible_request(messages, **kwargs)
        except Exception:
            raise


# ────────────────────────────────────────────────────────────
# Adapter Factory
# ────────────────────────────────────────────────────────────

class AIAdapterFactory:
    """Factory for creating AI adapters based on provider type."""

    _registry: dict[str, type[AIAdapter]] = {
        'openai': OpenAIAdapter,
        'deepseek': DeepSeekAdapter,
        'custom': CustomAdapter,
        'openai-compatible': OpenAIAdapter,  # alias
        'nvidia': OpenAIAdapter,  # NVIDIA uses OpenAI-compatible format
        'local': CustomAdapter,
    }

    @classmethod
    def register(cls, provider: str, adapter_class: type[AIAdapter]) -> None:
        cls._registry[provider] = adapter_class

    @classmethod
    def create(cls, config: dict[str, Any]) -> AIAdapter:
        provider = (config.get('provider') or 'custom').lower().strip()
        adapter_class = cls._registry.get(provider, CustomAdapter)
        return adapter_class(config)

    @classmethod
    def supported_providers(cls) -> list[str]:
        return sorted(cls._registry.keys())


# ────────────────────────────────────────────────────────────
# Router Service
# ────────────────────────────────────────────────────────────

class AIRouter:
    """AI Router — selects active provider, supports fallback, logs all decisions."""

    def __init__(self, active_config: dict[str, Any] | None = None, fallback_configs: list[dict[str, Any]] | None = None):
        self.active_config = active_config
        self.fallback_configs = fallback_configs or []
        self.trace: list[dict[str, Any]] = []

    def _log(self, event: str, **kwargs) -> None:
        entry = {'event': event, 'timestamp': time.time(), **kwargs}
        self.trace.append(entry)
        logger.info(f'[AI-ROUTER] {event} | {kwargs}')

    def call(self, messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
        """Route AI call through active provider with fallback."""
        all_configs = []
        if self.active_config:
            all_configs.append(self.active_config)
        all_configs.extend(self.fallback_configs)

        if not all_configs:
            self._log('error', reason='no_provider_configured')
            return {
                'ok': False,
                'error': 'No AI provider configured',
                'trace': self.trace,
            }

        last_error = None
        for i, config in enumerate(all_configs):
            provider = config.get('provider') or 'custom'
            is_fallback = i > 0
            self._log(
                'provider_selected' if not is_fallback else 'fallback_triggered',
                provider=provider,
                model=config.get('model'),
                base_url=config.get('base_url', '')[:80],
                runtime_key_source=config.get('runtime_key_source') or '',
                has_api_key=bool(config.get('api_key')),
            )

            try:
                self._log('request_start', provider=provider)
                adapter = AIAdapterFactory.create(config)
                result = adapter.call(messages, **kwargs)
                self._log('request_success', provider=provider, elapsed_ms=result.get('elapsed_ms'))
                return {
                    'ok': True,
                    'status': 'ok',
                    'provider': result.get('provider'),
                    'model': result.get('model'),
                    'result': result,
                    'trace': self.trace,
                }
            except urlerror.HTTPError as exc:
                detail = exc.read().decode('utf-8', errors='replace')[:300]
                last_error = f'{provider} HTTP {exc.code}: {detail}'
                self._log('request_fail', provider=provider, error=last_error)
            except Exception as exc:
                last_error = f'{provider}: {str(exc)[:200]}'
                self._log('request_fail', provider=provider, error=last_error)

        self._log('all_providers_failed', last_error=last_error)
        return {
            'ok': False,
            'error': last_error or 'All providers failed',
            'trace': self.trace,
        }


# ────────────────────────────────────────────────────────────
# Helper: resolve config from DB + ENV
# ────────────────────────────────────────────────────────────

def resolve_ai_config(db_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge DB config with ENV vars. ENV vars take precedence for runtime credentials."""
    provider_hint = ((db_config or {}).get('provider') or '').strip().lower()
    runtime_key_sources = [
        ('JOB_DECISION_AI_API_KEY', os.environ.get('JOB_DECISION_AI_API_KEY', '')),
    ]
    if provider_hint == 'nvidia':
        runtime_key_sources.insert(0, ('NVIDIA_API_KEY', os.environ.get('NVIDIA_API_KEY', '')))
    elif provider_hint == 'deepseek':
        runtime_key_sources.insert(0, ('DEEPSEEK_API_KEY', os.environ.get('DEEPSEEK_API_KEY', '')))
    elif provider_hint in ('openai', 'openai-compatible', 'custom'):
        runtime_key_sources.insert(0, ('OPENAI_API_KEY', os.environ.get('OPENAI_API_KEY', '')))
    env_key_name = ''
    env_key = ''
    for key_name, key_value in runtime_key_sources:
        if key_value:
            env_key_name = key_name
            env_key = key_value
            break
    env_base_url = os.environ.get('JOB_DECISION_AI_BASE_URL', '')
    env_model = os.environ.get('JOB_DECISION_AI_MODEL', '')

    if db_config:
        config = dict(db_config)
        config['runtime_key_source'] = env_key_name
        config['api_key'] = env_key or (config.get('api_key') or '').strip()
        # ENV overrides DB for runtime endpoint/model
        if env_base_url:
            config['base_url'] = env_base_url
        if env_model:
            config['model'] = env_model
        # Determine provider type
        provider = (config.get('provider') or 'custom').lower()
        base_url = (config.get('base_url') or '').lower()
        if 'deepseek' in base_url or provider == 'deepseek':
            config['provider'] = 'deepseek'
        elif 'openai.com' in base_url or provider == 'openai':
            config['provider'] = 'openai'
        elif 'nvidia' in base_url or provider == 'nvidia':
            config['provider'] = 'nvidia'
        else:
            config['provider'] = provider or 'custom'
        return config

    # No DB config — use ENV only
    return {
        'provider': 'openai-compatible',
        'base_url': env_base_url,
        'model': env_model,
        'api_key': env_key,
        'runtime_key_source': env_key_name,
    }
