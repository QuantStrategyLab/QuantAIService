"""Short inference through documented OAuth and API structured-output routes."""
from __future__ import annotations

import base64
import json
import re

from .gemini import _strict_json
from .runtime import ProviderReply, SubmissionRejected
from .transport import PublicHTTPS, json_body

_MODEL_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_VALID_IMAGE_MIMES = {'image/png', 'image/jpeg', 'image/webp'}
_VALID_REASONING_EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}
_MAX_IMAGE_MATERIALS = 3
_MAX_IMAGE_BASE64_CHARS = 300_000


def _validate_api_settings(label, *, model, api_usage_authorized, max_output_tokens, timeout_seconds):
    if api_usage_authorized is not True:
        raise ValueError(f'explicit {label} API usage authorization is required')
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise ValueError(f'a fixed {label} model identifier is required')
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
        raise ValueError('invalid inference output limit')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 180:
        raise ValueError('invalid inference timeout')


def _require_secret(client, message):
    secret = client()
    if not isinstance(secret, str) or not secret or '\r' in secret or '\n' in secret:
        raise SubmissionRejected(message)
    return secret


def _matches_image_magic(mime_type, decoded):
    if mime_type == 'image/png':
        return decoded.startswith(b'\x89PNG\r\n\x1a\n')
    if mime_type == 'image/jpeg':
        return decoded.startswith(b'\xff\xd8\xff')
    if mime_type == 'image/webp':
        return len(decoded) >= 12 and decoded[:4] == b'RIFF' and decoded[8:12] == b'WEBP'
    return False


def _validate_image_material(material, *, bounded):
    if set(material) != {'type', 'mime_type', 'base64'} or material['mime_type'] not in _VALID_IMAGE_MIMES:
        raise SubmissionRejected('unsupported image material')
    encoded = material['base64']
    if not isinstance(encoded, str) or not encoded:
        raise SubmissionRejected('invalid image material')
    if bounded and len(encoded) > _MAX_IMAGE_BASE64_CHARS:
        raise SubmissionRejected('image material exceeds size limit')
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise SubmissionRejected('invalid image material') from None
    if not decoded:
        raise SubmissionRejected('empty image material')
    if bounded and not _matches_image_magic(material['mime_type'], decoded):
        raise SubmissionRejected('invalid image material')


def _build_content(request, *, image_part, text_part, bounded_images=True):
    content = [text_part(request['objective'])]
    image_count = 0
    for material in request.get('materials', []):
        if isinstance(material, dict) and material.get('type') == 'image':
            image_count += 1
            if bounded_images and image_count > _MAX_IMAGE_MATERIALS:
                raise SubmissionRejected('too many image materials')
            _validate_image_material(material, bounded=bounded_images)
            content.append(image_part(material))
        else:
            try:
                serialized = json.dumps(material, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError):
                raise SubmissionRejected('invalid material') from None
            content.append(text_part('Supplied material (data):\n' + serialized))
    return content


def _failed_reply(task_id, verification='unavailable'):
    return {'task_id': task_id, 'status': 'failed', 'output': None, 'model_verification': verification}


def _extract_usage(usage, field_names):
    if not isinstance(usage, dict):
        return {}
    return {
        out_key: value
        for in_key, out_key in field_names
        if type(value := usage.get(in_key)) is int and value >= 0
    }


def _validate_request_model_and_effort(request, expected_model):
    if request['model'] != expected_model:
        raise SubmissionRejected('model is not configured for this provider')
    if 'reasoning_effort' in request:
        effort = request['reasoning_effort']
        if not isinstance(effort, str) or effort not in _VALID_REASONING_EFFORTS:
            raise SubmissionRejected('unsupported reasoning_effort')


def _parse_openai_response(raw_body):
    try:
        text = raw_body.decode('utf-8')
    except UnicodeError:
        raise RuntimeError('invalid inference receipt') from None
    if text.lstrip().startswith('{'):
        try:
            response = _strict_json(text)
        except (ValueError, TypeError):
            raise RuntimeError('invalid inference receipt') from None
        if not isinstance(response, dict):
            raise RuntimeError('invalid inference receipt')
        return True, response
    terminal = None
    # Parse SSE records, including multi-line data. Never accept deltas as completion.
    for record in text.replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip(' ') for line in record.split('\n') if line.startswith('data:'))
        if not data or data == '[DONE]':
            continue
        try:
            event = _strict_json(data)
        except (ValueError, TypeError):
            raise RuntimeError('invalid inference receipt') from None
        if not isinstance(event, dict):
            raise RuntimeError('invalid inference receipt')
        if event.get('type') in {'response.completed', 'response.failed', 'response.incomplete'}:
            if terminal is not None:
                raise RuntimeError('ambiguous terminal inference events')
            terminal = event
    if terminal is None:
        raise RuntimeError('inference ended without a terminal event')
    if terminal['type'] != 'response.completed':
        return False, None
    response = terminal.get('response')
    if not isinstance(response, dict):
        raise RuntimeError('invalid inference receipt')
    return True, response


class ChatGPTPlanProvider:
    mode = 'api'

    def __init__(self, token_client, *, plan_usage_authorized=False, transport=None):
        if plan_usage_authorized is not True:
            raise ValueError('registered ChatGPT plan-usage authorization is required')
        self._token_client = token_client
        self._transport = transport or PublicHTTPS(timeout=60, max_response_bytes=4_000_000)

    def submit(self, request, *, task_id, owner):
        token = _require_secret(self._token_client, 'approved OAuth client has no access token')
        # No tools, shell, repository mutation, previous_response_id or paid API fallback.
        content = _build_content(
            request,
            image_part=lambda m: {'type': 'input_image', 'image_url': 'data:' + m['mime_type'] + ';base64,' + m['base64']},
            text_part=lambda text: {'type': 'input_text', 'text': text},
            bounded_images=False,
        )
        body = {'model': request['model'], 'input': [{'role': 'user', 'content': content}],
                'store': False, 'stream': True,
                'text': {'format': {'type': 'json_schema', 'name': 'task_result',
                                     'schema': request['output_schema'], 'strict': True}}}
        reply = self._transport.request('https://api.openai.com/v1/responses',
            body=json_body(body), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                                          'Accept': 'text/event-stream'})
        if reply.status in {400, 401, 403, 404, 422, 429}:
            raise SubmissionRejected('inference was rejected')
        if reply.status != 200:
            raise RuntimeError('inference acceptance is unknown')
        ok, response = _parse_openai_response(reply.body)
        if not ok or response.get('status') != 'completed':
            return _failed_reply(task_id)
        if response.get('model') != request['model']:
            return _failed_reply(task_id, 'mismatch' if response.get('model') else 'unavailable')
        fragments = []
        for item in response.get('output', []):
            if item.get('type') == 'message':
                for part in item.get('content', []):
                    if part.get('type') == 'output_text':
                        fragments.append(part.get('text', ''))
                    elif part.get('type') == 'refusal':
                        return _failed_reply(task_id, 'provider_reported')
        output = json.loads(''.join(fragments))
        safe_usage = _extract_usage(
            response.get('usage', {}),
            (('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens'), ('total_tokens', 'total_tokens')),
        )
        return {'task_id': task_id, 'response_id': response.get('id'), 'status': 'completed', 'output': output,
                'model': response['model'], 'model_verification': 'provider_reported', 'usage': safe_usage}

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        # Synchronous inference has already returned; no claim that a lost request was cancelled.
        return None


class OpenAIAPIProvider:
    mode = 'api'

    def __init__(self, key_client, *, model, api_usage_authorized=False,
                 max_output_tokens=4096, timeout_seconds=90, transport=None):
        _validate_api_settings(
            'OpenAI', model=model, api_usage_authorized=api_usage_authorized,
            max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
        )
        self._key_client, self._model = key_client, model
        self._max_output_tokens, self._timeout_seconds = max_output_tokens, timeout_seconds
        self._transport = transport or PublicHTTPS(timeout=timeout_seconds, max_response_bytes=4_000_000)

    def submit(self, request, *, task_id, owner):
        _validate_request_model_and_effort(request, self._model)
        content = _build_content(
            request,
            image_part=lambda m: {'type': 'input_image', 'image_url': 'data:' + m['mime_type'] + ';base64,' + m['base64']},
            text_part=lambda text: {'type': 'input_text', 'text': text},
            bounded_images=True,
        )
        key = _require_secret(self._key_client, 'configured OpenAI API key is unavailable')
        body = {
            'model': self._model,
            'input': [{'role': 'user', 'content': content}],
            'store': False,
            'max_output_tokens': self._max_output_tokens,
            'text': {'format': {'type': 'json_schema', 'name': 'task_result',
                                 'schema': request['output_schema'], 'strict': True}},
        }
        if 'reasoning_effort' in request:
            body['reasoning'] = {'effort': request['reasoning_effort']}
        timeout = min(self._timeout_seconds, request.get('timeout_seconds', self._timeout_seconds))
        reply = self._transport.request(
            'https://api.openai.com/v1/responses',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
            body=json_body(body), timeout=timeout,
        )
        if reply.status in {400, 401, 403, 404, 422, 429}:
            raise SubmissionRejected('inference was rejected')
        if reply.status != 200:
            raise RuntimeError('inference acceptance is unknown')
        ok, response = _parse_openai_response(reply.body)
        if not ok or response.get('status') != 'completed':
            return _failed_reply(task_id)
        if response.get('model') != self._model:
            return _failed_reply(task_id, 'mismatch' if response.get('model') else 'unavailable')
        output_items = response.get('output', [])
        if not isinstance(output_items, list):
            return _failed_reply(task_id, 'provider_reported')
        fragments = []
        for item in output_items:
            if not isinstance(item, dict) or item.get('type') != 'message':
                continue
            parts = item.get('content', [])
            if not isinstance(parts, list):
                return _failed_reply(task_id, 'provider_reported')
            for part in parts:
                if not isinstance(part, dict):
                    return _failed_reply(task_id, 'provider_reported')
                if part.get('type') == 'refusal':
                    return _failed_reply(task_id, 'provider_reported')
                if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                    fragments.append(part['text'])
        if not fragments:
            return _failed_reply(task_id, 'provider_reported')
        try:
            output = _strict_json(''.join(fragments))
        except (ValueError, TypeError):
            return _failed_reply(task_id, 'provider_reported')
        if not isinstance(output, dict):
            return _failed_reply(task_id, 'provider_reported')
        safe_usage = _extract_usage(
            response.get('usage', {}),
            (('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens'), ('total_tokens', 'total_tokens')),
        )
        result = {
            'task_id': task_id,
            'status': 'completed',
            'output': output,
            'model': self._model,
            'model_verification': 'provider_reported',
            'usage': safe_usage,
        }
        if isinstance(response.get('id'), str):
            result['response_id'] = response['id']
        return result

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        return None


class AnthropicAPIProvider:
    mode = 'api'

    def __init__(self, key_client, *, model, api_usage_authorized=False,
                 max_output_tokens=4096, timeout_seconds=90, transport=None):
        _validate_api_settings(
            'Anthropic', model=model, api_usage_authorized=api_usage_authorized,
            max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
        )
        self._key_client, self._model = key_client, model
        self._max_output_tokens, self._timeout_seconds = max_output_tokens, timeout_seconds
        self._transport = transport or PublicHTTPS(timeout=timeout_seconds, max_response_bytes=4_000_000)

    def submit(self, request, *, task_id, owner):
        _validate_request_model_and_effort(request, self._model)
        content = _build_content(
            request,
            image_part=lambda m: {'type': 'image', 'source': {'type': 'base64', 'media_type': m['mime_type'], 'data': m['base64']}},
            text_part=lambda text: {'type': 'text', 'text': text},
            bounded_images=True,
        )
        key = _require_secret(self._key_client, 'configured Anthropic API key is unavailable')
        body = {
            'model': self._model,
            'max_tokens': self._max_output_tokens,
            'messages': [{'role': 'user', 'content': content}],
            'tools': [{
                'name': 'record_result',
                'description': 'Emit structured JSON output matching the schema.',
                'input_schema': request['output_schema'],
            }],
            'tool_choice': {'type': 'tool', 'name': 'record_result'},
        }
        timeout = min(self._timeout_seconds, request.get('timeout_seconds', self._timeout_seconds))
        reply = self._transport.request(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01'},
            body=json_body(body), timeout=timeout,
        )
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
        if response.get('model') != self._model:
            return _failed_reply(task_id, 'mismatch' if response.get('model') else 'unavailable')
        if response.get('stop_reason') != 'tool_use':
            return _failed_reply(task_id, 'provider_reported')
        blocks = response.get('content', [])
        if not isinstance(blocks, list):
            return _failed_reply(task_id, 'provider_reported')
        tool_uses = [
            block for block in blocks
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'record_result'
        ]
        if len(tool_uses) != 1 or not isinstance(tool_uses[0].get('input'), dict):
            return _failed_reply(task_id, 'provider_reported')
        safe_usage = _extract_usage(
            response.get('usage', {}),
            (('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens')),
        )
        return {
            'task_id': task_id,
            'status': 'completed',
            'output': tool_uses[0]['input'],
            'model': self._model,
            'model_verification': 'provider_reported',
            'usage': safe_usage,
        }

    def poll(self, handle):
        return ProviderReply(handle['status'], handle.get('output'))

    def cancel(self, handle):
        return None
