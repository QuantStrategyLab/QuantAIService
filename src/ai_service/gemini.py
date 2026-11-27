"""Opt-in synchronous text inference; native Dot/Grok remain agent providers.

No SDK, automatic retries, model fallback, tools, URLs in materials, or changed
safety settings. The runtime still owns project/model admission and output
validation. An API key is separate from ChatGPT/Grok subscription authority.
"""
from __future__ import annotations

import json
import re

from .model_policy import MODEL_PATTERN, TIER_KEYS
from .runtime import ProviderReply, SubmissionRejected
from .transport import PublicHTTPS, json_body

_DATED_SUFFIX = r'-(?:\d{4}-\d{2}-\d{2}|\d{8})'


def matches_model(configured: str, reported: object) -> bool:
    """Accept an exact model identifier or a provider-dated snapshot of it."""
    if not isinstance(configured, str) or not isinstance(reported, str) or not configured:
        return False
    if reported == configured:
        return True
    return re.fullmatch(re.escape(configured) + _DATED_SUFFIX, reported) is not None


def validate_tier_models(tier_models: object, *, label: str = 'provider') -> dict[str, list[str]] | None:
    """Validate optional per-tier API model candidate lists."""
    if tier_models is None:
        return None
    if not isinstance(tier_models, dict) or not tier_models or set(tier_models) - TIER_KEYS:
        raise ValueError(f'invalid {label} tier_models mapping')
    normalized: dict[str, list[str]] = {}
    for tier, models in tier_models.items():
        if (not isinstance(models, list) or not 1 <= len(models) <= 8
                or any(not isinstance(m, str) or not MODEL_PATTERN.fullmatch(m) for m in models)):
            raise ValueError(f'invalid {label} tier_models entries')
        normalized[tier] = list(models)
    return normalized


def _strict_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('ambiguous JSON')
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError('nonstandard JSON')

    return json.loads(data, object_pairs_hook=object_pairs, parse_constant=reject_constant)


class GeminiProvider:
    mode = 'api'
    provider_kind = 'gemini_api'

    def __init__(self, key_client, *, model, api_usage_authorized=False,
                 max_output_tokens=1024, timeout_seconds=60, transport=None,
                 tier_models=None):
        if api_usage_authorized is not True:
            raise ValueError('explicit Gemini API usage authorization is required')
        if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
            raise ValueError('a fixed Gemini model identifier is required')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
            raise ValueError('invalid inference output limit')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 180:
            raise ValueError('invalid inference timeout')
        self._key_client, self._model = key_client, model
        self._tier_models = validate_tier_models(tier_models, label='Gemini')
        self._max_output_tokens, self._timeout_seconds = max_output_tokens, timeout_seconds
        self._transport = transport or PublicHTTPS(timeout=timeout_seconds, max_response_bytes=4_000_000)

    def _expected_model(self, request):
        model = request['model']
        if model == self._model:
            return model
        tier = request.get('complexity')
        if self._tier_models and tier in self._tier_models and model in self._tier_models[tier]:
            return model
        raise SubmissionRejected('model is not configured for this provider')

    @staticmethod
    def _failed(task_id, verification='unavailable'):
        return {'task_id': task_id, 'status': 'failed', 'output': None,
                'model_verification': verification}

    def submit(self, request, *, task_id, owner):
        expected_model = self._expected_model(request)
        parts = [{'text': request['objective']}]
        for material in request.get('materials', []):
            if isinstance(material, dict) and material.get('type') == 'image':
                raise SubmissionRejected('this provider accepts text materials only')
            parts.append({'text': 'Supplied material (data):\n' + json.dumps(material, ensure_ascii=False, allow_nan=False)})
        key = self._key_client()
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise SubmissionRejected('configured Gemini API key is unavailable')
        generation_config = {
            'candidateCount': 1, 'maxOutputTokens': self._max_output_tokens,
            'responseMimeType': 'application/json',
            'responseJsonSchema': request['output_schema'],
        }
        effort = request.get('reasoning_effort')
        budgets = {'low': 0, 'medium': 1024, 'high': 8192}
        if isinstance(effort, str) and effort in budgets:
            if re.match(r'^gemini-[34][.-]', expected_model):
                named = ('LOW' if effort == 'medium' and re.match(r'^gemini-3-pro(?:-|$)', expected_model)
                         else effort.upper())
                generation_config['thinkingConfig'] = {'thinkingLevel': named}
            else:
                budget = (128 if effort == 'low' and re.match(r'^gemini-2\.5-pro(?:-|$)', expected_model)
                          else budgets[effort])
                generation_config['thinkingConfig'] = {'thinkingBudget': budget}
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': generation_config}
        timeout = min(self._timeout_seconds, request.get('timeout_seconds', self._timeout_seconds))
        reply = self._transport.request(
            'https://generativelanguage.googleapis.com/v1beta/models/' + expected_model + ':generateContent',
            headers={'Content-Type': 'application/json', 'x-goog-api-key': key},
            body=json_body(body), timeout=timeout)
        if reply.status in {400, 401, 403, 404, 422, 429}:
            raise SubmissionRejected('inference was rejected')
        if reply.status != 200:
            raise RuntimeError('inference acceptance is unknown')
        try:
            response = _strict_json(reply.body)
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError('invalid inference receipt') from None
        if not isinstance(response, dict):
            raise RuntimeError('invalid inference receipt')
        if response.get('promptFeedback', {}).get('blockReason'):
            return self._failed(task_id)
        if not matches_model(expected_model, response.get('modelVersion')):
            return self._failed(task_id, 'mismatch' if response.get('modelVersion') else 'unavailable')
        candidates = response.get('candidates', [])
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            return self._failed(task_id, 'provider_reported')
        candidate = candidates[0]
        if candidate.get('finishReason') != 'STOP':
            return self._failed(task_id, 'provider_reported')
        result_parts = candidate.get('content', {}).get('parts', [])
        if not isinstance(result_parts, list) or not result_parts:
            return self._failed(task_id, 'provider_reported')
        fragments = []
        for part in result_parts:
            if not isinstance(part, dict) or not isinstance(part.get('text'), str) or set(part) - {'text', 'thought', 'thoughtSignature'}:
                return self._failed(task_id, 'provider_reported')
            if part.get('thought') is not True:
                fragments.append(part['text'])
        try:
            output = _strict_json(''.join(fragments))
        except (ValueError, TypeError):
            return self._failed(task_id, 'provider_reported')
        usage = response.get('usageMetadata', {})
        safe_usage = {}
        if isinstance(usage, dict):
            for provider_name, safe_name in (('promptTokenCount', 'input_tokens'),
                                             ('candidatesTokenCount', 'output_tokens'),
                                             ('totalTokenCount', 'total_tokens')):
                value = usage.get(provider_name)
                if type(value) is int and value >= 0:
                    safe_usage[safe_name] = value
        return {'task_id': task_id, 'status': 'completed', 'output': output,
                'model': expected_model, 'model_verification': 'provider_reported', 'usage': safe_usage}

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        # Synchronous result already returned; a lost receipt is never claimed cancelled.
        return None
