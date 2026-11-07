#!/usr/bin/env python3
"""Tool-free Z.ai and OpenRouter direct HTTPS API worker adapter for run_workflow.py.

Provider boundary only: build a bounded prompt from the complete task contract plus the current
declared files (reusing devin_worker helpers), read the local private credential file at request
time only, enforce fixed official HTTPS endpoints with zero redirects and no ambient endpoint or
proxy overrides, validate OpenRouter live catalog zero pricing (`:free` model + max_price=0 +
allow_fallbacks=false) or Z.ai explicit free-model allowlist (`glm-4.5-flash`), require exact
model match, zero tool calls, normal stop completion, and internally consistent native token usage
(without double-counting cached or reasoning subsets), and only then atomically apply the
model-authored UTF-8 bytes to declared paths with precondition checks and rollback.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent

_gate_spec = importlib.util.spec_from_file_location('jev_review_gate_for_free_api', SCRIPT_DIR / 'jev_review_gate.py')
gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(gate)

_devin_spec = importlib.util.spec_from_file_location('devin_worker_for_free_api', SCRIPT_DIR / 'devin_worker.py')
devin = importlib.util.module_from_spec(_devin_spec)
_devin_spec.loader.exec_module(devin)

WORKER_ENV = devin.WORKER_ENV
DEFAULT_WORKER = devin.DEFAULT_WORKER
SUPPORTED_WORKERS = gate.WORKFLOW_WORKER_PROVIDERS
FREE_API_PROVIDERS = gate.FREE_API_PROVIDERS
FREE_API_PROVIDER_ORDER = ('zai', 'openrouter')
CONTEXT_ENV = devin.CONTEXT_ENV
DEFAULT_CONTEXT_MODE = devin.DEFAULT_CONTEXT_MODE
SUPPORTED_CONTEXT_MODES = devin.SUPPORTED_CONTEXT_MODES

ZAI_MODEL_ENV = 'ZAI_MODEL'
OPENROUTER_MODEL_ENV = 'OPENROUTER_MODEL'
ZAI_API_KEY_FILE_ENV = 'ZAI_API_KEY_FILE'
OPENROUTER_API_KEY_FILE_ENV = 'OPENROUTER_API_KEY_FILE'

ZAI_DEFAULT_MODEL = gate.ZAI_DEFAULT_MODEL
ZAI_FREE_MODELS = gate.ZAI_FREE_MODELS
ZAI_OVERLOADED_MODELS = gate.ZAI_OVERLOADED_MODELS
ZAI_CHAT_ENDPOINT = gate.ZAI_CHAT_ENDPOINT
OPENROUTER_CHAT_ENDPOINT = gate.OPENROUTER_CHAT_ENDPOINT
OPENROUTER_MODELS_ENDPOINT = gate.OPENROUTER_MODELS_ENDPOINT
FREE_API_FINISH_REASONS = gate.FREE_API_FINISH_REASONS
FREE_API_USAGE_KEYS = gate.FREE_API_USAGE_KEYS

MAX_CREDENTIAL_BYTES = 4096
MAX_HTTP_RESPONSE_BYTES = 2_000_000
MAX_CATALOG_BYTES = 8_000_000
MAX_INITIAL_WORKER_EXECUTIONS = 1
MAX_EXTRA_WORKER_EXECUTIONS = 2
MAX_TOTAL_WORKER_EXECUTIONS = MAX_INITIAL_WORKER_EXECUTIONS + MAX_EXTRA_WORKER_EXECUTIONS

ZAI_QUOTA_ATTESTATION = (
    'Z.ai free-model allowlist verified against official pricing; a successful request does not prove numeric remaining quota'
)
ZAI_USAGE_BASIS = (
    'One fresh tool-free Z.ai HTTPS chat.completions response; prompt_tokens and completion_tokens are native '
    'totals (cached_tokens and reasoning_tokens are subsets, never added twice); successful request does not prove numeric remaining quota'
)
OPENROUTER_USAGE_BASIS = (
    'One fresh tool-free OpenRouter HTTPS chat.completions response verified against live /api/v1/models zero-price '
    'catalog with max_price=0 and allow_fallbacks=false; prompt_tokens and completion_tokens are native totals '
    '(cached_tokens and reasoning_tokens are subsets, never added twice)'
)
EFFORT_NOTE_ZAI = 'not applicable: Z.ai direct API worker is never an AGY High reviewer and never labelled high effort'
EFFORT_NOTE_OPENROUTER = (
    'not applicable: OpenRouter direct API worker is never an AGY High reviewer and never labelled high effort'
)

FORBIDDEN_RAW_KEY_ENVS = (
    'ZAI_API_KEY',
    'OPENROUTER_API_KEY',
    'OPENAI_API_KEY',
)
AMBIENT_OVERRIDE_ENVS = (
    'ZAI_BASE_URL',
    'ZAI_API_BASE',
    'OPENROUTER_BASE_URL',
    'OPENROUTER_API_BASE',
    'OPENAI_BASE_URL',
    'OPENAI_API_BASE',
    'HTTP_PROXY',
    'HTTPS_PROXY',
    'ALL_PROXY',
    'http_proxy',
    'https_proxy',
    'all_proxy',
)
ALLOWED_ENDPOINTS = {
    'zai': (ZAI_CHAT_ENDPOINT,),
    'openrouter': (OPENROUTER_CHAT_ENDPOINT, OPENROUTER_MODELS_ENDPOINT),
}
ZERO_MAX_PRICE = {'prompt': 0, 'completion': 0, 'request': 0}
REQUIRED_CATALOG_PRICE_KEYS = ('prompt', 'completion', 'request')
OPTIONAL_CATALOG_PRICE_KEYS = (
    'image',
    'web_search',
    'internal_reasoning',
    'input_cache_read',
    'input_cache_write',
    'audio',
)

AVAILABILITY_ERROR_CODES = {'1302', '1303', '1305', '429', '502', '503', '504'}
AVAILABILITY_PATTERNS = (
    'rate limit',
    'rate_limit',
    'ratelimit',
    'too many requests',
    'quota',
    'overload',
    'overloaded',
    '1305',
    'temporarily unavailable',
    'service unavailable',
    'bad gateway',
    'gateway timeout',
    'timed out',
    'timeout',
    'capacity',
    'server busy',
    'try again later',
    'insufficient credits',
)
PERMISSION_DENIAL_PATTERNS = (
    'permission check failed',
    'user denied permission',
    'permission denied',
    'tool denied',
    'denied by hook',
    'blocked by hook',
)
AUTH_ERROR_PATTERNS = (
    'invalid api key',
    'invalid_api_key',
    'unauthorized',
    'authentication failed',
    'not authenticated',
    'invalid token',
    'expired token',
    'invalid credential',
)
PRINTABLE_KEY_RE = re.compile(r'\A[\x21-\x7e]{8,512}\Z')


class FreeApiBlocked(ValueError):
    """Fail-closed condition for Z.ai / OpenRouter direct API worker adapter."""

    def __init__(self, message: str, *, category: str = 'terminal_proof', http_status: int | None = None):
        super().__init__(message)
        self.category = category
        self.http_status = http_status


class FreeApiPartialMutation(FreeApiBlocked):
    """Rollback could not restore every replaced file; workspace may hold unverified bytes."""

    def __init__(self, message: str):
        super().__init__(message, category='terminal_proof')


# Shared helpers re-exported from devin_worker for source scanning, prompt building, JSON validation and atomic apply.
sha256_bytes = devin.sha256_bytes
resolve_context_mode = devin.resolve_context_mode
validate_scope_paths = devin.validate_scope_paths
safe_target = devin.safe_target
read_regular_nofollow = devin.read_regular_nofollow
read_private_regular_file = devin.read_private_regular_file
scan_sources = devin.scan_sources
build_feedback = devin.build_feedback
build_prompt = devin.build_prompt
unwrap_generation_json = devin.unwrap_generation_json
def parse_generation(response_text: str, scope_files: list[str]) -> tuple[str, dict[str, bytes]]:
    """Strict raw JSON {handoff, files} without fences; keys must be exact declared paths; contents bounded and secret-free."""
    if not isinstance(response_text, str):
        raise devin.DevinBlocked('API response must be a string')
    if len(response_text) > devin.MAX_RESPONSE_CHARS:
        raise devin.DevinBlocked(f'Devin response exceeds {devin.MAX_RESPONSE_CHARS} chars')
    if '\x1b' in response_text:
        raise devin.DevinBlocked('Devin response contains unexpected terminal control sequence')
    try:
        obj = gate.strict_json(response_text)
    except ValueError as exc:
        raise devin.DevinBlocked(
            'API response must be one strict raw JSON object (markdown fences, duplicate keys, trailing data or prose rejected)'
        ) from exc
    if not isinstance(obj, dict) or set(obj) != {'handoff', 'files'}:
        raise devin.DevinBlocked('Devin response must contain exactly handoff and files')
    return devin.parse_generation(response_text, scope_files)
apply_files = devin.apply_files
capture_workspace_snapshot = devin.capture_workspace_snapshot
restore_workspace_snapshot = devin.restore_workspace_snapshot
parse_zai_model = gate.parse_zai_model
parse_openrouter_model = gate.parse_openrouter_model


def resolve_worker(value: str | None = None) -> str:
    """Per-invocation WORKFLOW_WORKER (agy default when unset/empty); unknown values fail before any call."""
    raw = os.environ.get(WORKER_ENV) if value is None else value
    worker = raw or DEFAULT_WORKER
    if worker not in SUPPORTED_WORKERS:
        raise ValueError('BLOCKED: unsupported WORKFLOW_WORKER; allowed: ' + ', '.join(SUPPORTED_WORKERS))
    return worker


def resolve_zai_model(value: str | None = None) -> str:
    """Resolve Z.ai free model (`glm-4.5-flash` default; `glm-4.7-flash` and paid models rejected)."""
    raw = os.environ.get(ZAI_MODEL_ENV) if value is None else value
    try:
        return parse_zai_model(raw)
    except ValueError as exc:
        raise FreeApiBlocked(str(exc), category='terminal_proof') from exc


def resolve_openrouter_model(value: str | None = None) -> tuple[str, str, str]:
    """Resolve explicit OpenRouter `<vendor>/<slug>:free` model (no default, no paid fallback)."""
    raw = os.environ.get(OPENROUTER_MODEL_ENV) if value is None else value
    try:
        vendor, slug = parse_openrouter_model(raw)
    except ValueError as exc:
        raise FreeApiBlocked(str(exc), category='terminal_proof') from exc
    return f'{vendor}/{slug}', vendor, slug


def _scrub_secret(value: object, secret: str | None = None):
    """Sanitize structured data and scrub any occurrence of the loaded secret string or its parts."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            clean_k = _scrub_secret(k, secret)
            if isinstance(k, str) and gate.SECRET_NAME.fullmatch(k):
                out[clean_k] = '<redacted>'
            else:
                out[clean_k] = _scrub_secret(v, secret)
        return out
    if isinstance(value, list):
        return [_scrub_secret(item, secret) for item in value]
    if isinstance(value, str):
        text = value
        if secret:
            text = text.replace(secret, '<redacted>')
            if '.' in secret:
                for part in secret.split('.'):
                    if len(part) >= 8:
                        text = text.replace(part, '<redacted>')
        return gate.redact(text)
    return value


def _check_ambient_environment() -> None:
    """Reject ambient endpoint/proxy overrides and raw API key env vars."""
    for env_name in AMBIENT_OVERRIDE_ENVS:
        val = os.environ.get(env_name)
        if val is not None and str(val).strip():
            raise FreeApiBlocked(
                f'BLOCKED: ambient endpoint/proxy override env {env_name} is forbidden; use fixed official HTTPS endpoints only',
                category='terminal_proof',
            )
    for env_name in FORBIDDEN_RAW_KEY_ENVS:
        val = os.environ.get(env_name)
        if val is not None and str(val).strip():
            raise FreeApiBlocked(
                f'BLOCKED: raw API key in environment variable {env_name} is forbidden; use a private owned credential file',
                category='auth',
            )


def resolve_credential_path(provider: str, credential_path: Path | str | None = None) -> Path:
    """Resolve local credential file path for `zai` or `openrouter` without reading its contents yet."""
    if provider not in FREE_API_PROVIDERS:
        raise FreeApiBlocked(f'unsupported free API provider: {provider!r}', category='terminal_proof')
    if credential_path is not None:
        return Path(credential_path).expanduser()
    env_name = ZAI_API_KEY_FILE_ENV if provider == 'zai' else OPENROUTER_API_KEY_FILE_ENV
    env_val = os.environ.get(env_name)
    if env_val is not None:
        if not env_val.strip() or env_val != env_val.strip():
            raise FreeApiBlocked(f'BLOCKED: {env_name} is empty or whitespace-padded', category='auth')
        return Path(env_val).expanduser()
    router_dir = Path.home() / '.config' / 'ai-router'
    dedicated = router_dir / (f'{provider}.key')
    if os.path.lexists(dedicated):
        return dedicated
    shared = router_dir / 'credentials.json'
    return shared


def _extract_key_from_bytes(
    raw_bytes: bytes, provider: str, *, depth: int = 0
) -> str:
    if depth > 1:
        raise FreeApiBlocked(f'{provider} credential file indirection exceeds 1 hop', category='auth')
    try:
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise FreeApiBlocked(f'{provider} credential file is not valid UTF-8', category='auth') from exc
    stripped = text.strip()
    if not stripped:
        raise FreeApiBlocked(f'{provider} credential file is empty', category='auth')
    if stripped.startswith('{'):
        try:
            parsed = gate.strict_json(stripped)
        except ValueError as exc:
            raise FreeApiBlocked(f'{provider} credential JSON is malformed', category='auth') from exc
        if not isinstance(parsed, dict):
            raise FreeApiBlocked(f'{provider} credential JSON must be an object', category='auth')
        provider_entry = parsed.get(provider)
        if isinstance(provider_entry, dict):
            target_obj = provider_entry
        else:
            target_obj = parsed
        key_file_ref = target_obj.get(f'{provider}_api_key_file') or target_obj.get('key_file') or target_obj.get('api_key_file')
        if isinstance(key_file_ref, str) and key_file_ref.strip():
            ref_path = Path(key_file_ref.strip()).expanduser()
            ref_bytes = read_private_regular_file(
                ref_path, label=f'{provider} referenced credential file', max_bytes=MAX_CREDENTIAL_BYTES
            )
            return _extract_key_from_bytes(ref_bytes, provider, depth=depth + 1)
        candidate = (
            target_obj.get(f'{provider}_api_key')
            or target_obj.get('api_key')
            or (provider_entry if isinstance(provider_entry, str) else None)
        )
        if not isinstance(candidate, str) or not candidate.strip():
            raise FreeApiBlocked(f'{provider} credential JSON missing key for {provider}', category='auth')
        key = candidate.strip()
    else:
        lines = [line.strip() for line in stripped.splitlines() if line.strip() and not line.strip().startswith('#')]
        if len(lines) != 1:
            raise FreeApiBlocked(f'{provider} credential file must contain a single key line', category='auth')
        key = lines[0]
    if not PRINTABLE_KEY_RE.fullmatch(key):
        raise FreeApiBlocked(f'{provider} credential key is malformed or contains whitespace/control chars', category='auth')
    return key


def load_api_credential(provider: str, credential_path: Path | str | None = None) -> tuple[str, dict[str, object]]:
    """Read and validate private owned local credential file at request time; never persist the secret."""
    _check_ambient_environment()
    path = resolve_credential_path(provider, credential_path)
    try:
        raw_bytes = read_private_regular_file(
            path, label=f'{provider} credential file', max_bytes=MAX_CREDENTIAL_BYTES
        )
    except devin.DevinBlocked as exc:
        raise FreeApiBlocked(str(exc), category='auth') from exc
    key = _extract_key_from_bytes(raw_bytes, provider)
    proof_meta = {
        'credential_source_status': 'VERIFIED_PRIVATE_FILE',
        'credential_file_basename': path.name,
        'credential_key_sha256_prefix': hashlib.sha256(key.encode('utf-8')).hexdigest()[:12],
    }
    return key, proof_meta


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Forbid all HTTP redirects so Authorization headers and payloads never leave fixed official endpoints."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FreeApiBlocked(
            f'HTTP redirect ({code}) rejected; refusing to forward Authorization header or follow redirect',
            category='terminal_proof',
            http_status=int(code),
        )

    def http_error_301(self, req, fp, code, msg, headers):
        return self.redirect_request(req, fp, code, msg, headers, None)

    def http_error_302(self, req, fp, code, msg, headers):
        return self.redirect_request(req, fp, code, msg, headers, None)

    def http_error_303(self, req, fp, code, msg, headers):
        return self.redirect_request(req, fp, code, msg, headers, None)

    def http_error_307(self, req, fp, code, msg, headers):
        return self.redirect_request(req, fp, code, msg, headers, None)

    def http_error_308(self, req, fp, code, msg, headers):
        return self.redirect_request(req, fp, code, msg, headers, None)


def validate_fixed_endpoint(provider: str, url: str) -> str:
    """Ensure URL is an exact allowlisted official HTTPS endpoint with no userinfo, query, or fragment."""
    allowed = ALLOWED_ENDPOINTS.get(provider, ())
    if not isinstance(url, str) or url not in allowed:
        raise FreeApiBlocked(f'endpoint {url!r} is not an allowlisted official {provider} HTTPS endpoint', category='terminal_proof')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FreeApiBlocked(f'endpoint {url!r} failed strict HTTPS validation', category='terminal_proof')
    return url


def classify_failure(
    reason: str,
    *,
    http_status: int | None = None,
    error_code: str | int | None = None,
    default_category: str = 'terminal_proof',
) -> str:
    """Classify an adapter failure into availability, auth, permission_denial, or terminal_proof."""
    low = (reason or '').lower()
    code_str = str(error_code).strip() if error_code is not None else ''
    if any(pat in low for pat in PERMISSION_DENIAL_PATTERNS):
        return 'permission_denial'
    if http_status in (401, 403) or any(pat in low for pat in AUTH_ERROR_PATTERNS):
        return 'auth'
    if (
        http_status in (429, 500, 502, 503, 504)
        or code_str in AVAILABILITY_ERROR_CODES
        or any(pat in low for pat in AVAILABILITY_PATTERNS)
    ):
        return 'availability'
    return default_category


def is_availability_fallback_eligible(record: object) -> bool:
    """True only when a worker attempt failed cleanly due to provider availability/rate-limit/quota/overload."""
    if not isinstance(record, dict):
        return False
    if record.get('ok') is not False or record.get('blocked') is not True:
        return False
    if record.get('tool_denied') is not False or record.get('partial_mutation') is not False:
        return False
    files_written = record.get('files_written')
    if not isinstance(files_written, list) or len(files_written) != 0:
        return False
    reason = record.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        return False
    return record.get('error_category') == 'availability'

def _default_http_transport(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_seconds: int,
    max_bytes: int,
    redaction_value: str | None = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(req, timeout=max(1, int(timeout_seconds))) as resp:
            status = int(getattr(resp, 'status', None) or resp.getcode() or 200)
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FreeApiBlocked(f'HTTP response from {url} exceeds {max_bytes} bytes', category='terminal_proof')
            return status, data
    except FreeApiBlocked:
        raise
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        if status in (301, 302, 303, 307, 308):
            raise FreeApiBlocked(
                f'HTTP redirect ({status}) rejected; refusing to forward Authorization header or follow redirect',
                category='terminal_proof',
                http_status=status,
            ) from None
        raw_err = b''
        try:
            if exc.fp is not None:
                raw_err = exc.fp.read(65536)
        except OSError:
            raw_err = b''
        err_text = _scrub_secret(raw_err.decode('utf-8', errors='replace'), redaction_value)
        err_code = None
        err_msg = err_text.strip() or str(exc.reason or f'HTTP {status}')
        try:
            err_json = gate.strict_json(err_text)
            if isinstance(err_json, dict):
                err_obj = err_json.get('error')
                if isinstance(err_obj, dict):
                    err_code = err_obj.get('code')
                    err_msg = str(err_obj.get('message') or err_msg)
                elif isinstance(err_obj, str):
                    err_msg = err_obj
        except ValueError:
            pass
        err_msg = str(_scrub_secret(err_msg, redaction_value))[:400]
        category = classify_failure(
            err_msg,
            http_status=status,
            error_code=err_code,
            default_category='auth' if status in (401, 403) else ('availability' if status in (429, 500, 502, 503, 504) else 'terminal_proof'),
        )
        raise FreeApiBlocked(
            f'HTTP {status} from {url}: {err_msg}',
            category=category,
            http_status=status,
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        safe_msg = str(_scrub_secret(str(exc), redaction_value))[:300]
        category = classify_failure(safe_msg, default_category='availability')
        raise FreeApiBlocked(f'network/timeout error calling {url}: {safe_msg}', category=category) from None


def _is_zero_price_value(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value) and value == 0
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        try:
            num = float(s)
        except ValueError:
            return False
        return math.isfinite(num) and num == 0.0
    return False


def validate_openrouter_catalog_model(
    catalog_payload: object, requested_model: str
) -> dict[str, object]:
    """Verify exact `:free` model exists in OpenRouter catalog and has strictly zero prompt/completion/request prices."""
    vendor, slug = parse_openrouter_model(requested_model)
    expected_id = f'{vendor}/{slug}'
    if not isinstance(catalog_payload, dict):
        raise FreeApiBlocked('OpenRouter catalog response must be a JSON object', category='terminal_proof')
    data = catalog_payload.get('data')
    if not isinstance(data, list) or not data:
        raise FreeApiBlocked('OpenRouter catalog response missing non-empty data list', category='terminal_proof')
    matches = [item for item in data if isinstance(item, dict) and item.get('id') == expected_id]
    if not matches:
        raise FreeApiBlocked(
            f'OpenRouter model {expected_id!r} not found in live catalog',
            category='terminal_proof',
        )
    if len(matches) > 1:
        raise FreeApiBlocked(
            f'OpenRouter catalog contains duplicate entries for {expected_id!r}',
            category='terminal_proof',
        )
    entry = matches[0]
    pricing = entry.get('pricing')
    if not isinstance(pricing, dict):
        raise FreeApiBlocked(
            f'OpenRouter catalog entry for {expected_id!r} missing pricing object',
            category='terminal_proof',
        )
    for req_key in REQUIRED_CATALOG_PRICE_KEYS:
        if req_key == 'request' and req_key not in pricing:
            continue
        if req_key not in pricing or not _is_zero_price_value(pricing.get(req_key)):
            raise FreeApiBlocked(
                f'OpenRouter catalog price for {expected_id!r} field {req_key!r} is non-zero or invalid: {pricing.get(req_key)!r}',
                category='terminal_proof',
            )
    for opt_key in OPTIONAL_CATALOG_PRICE_KEYS:
        if opt_key in pricing and pricing[opt_key] is not None and not _is_zero_price_value(pricing[opt_key]):
            raise FreeApiBlocked(
                f'OpenRouter catalog price for {expected_id!r} optional field {opt_key!r} is non-zero: {pricing.get(opt_key)!r}',
                category='terminal_proof',
            )
    for k, v in pricing.items():
        if k not in REQUIRED_CATALOG_PRICE_KEYS and k not in OPTIONAL_CATALOG_PRICE_KEYS and v is not None:
            if not _is_zero_price_value(v):
                raise FreeApiBlocked(
                    f'OpenRouter catalog price for {expected_id!r} field {k!r} is non-zero: {v!r}',
                    category='terminal_proof',
                )
    return {
        'id': expected_id,
        'provider_id': vendor,
        'model_id': slug,
        'catalog_endpoint': OPENROUTER_MODELS_ENDPOINT,
        'pricing': {'prompt': '0', 'completion': '0', 'request': '0'},
        'raw_pricing': {k: str(v) for k, v in sorted(pricing.items()) if v is not None},
        'request_price_assurance': 'catalog_zero' if 'request' in pricing else 'server_zero_cap_required',
        'verified_zero_price': True,
    }


def _openrouter_json_messages(prompt: str) -> list[dict[str, str]]:
    """Build OpenRouter messages with a strict JSON system rule and one runtime-serialized example."""
    example_response = json.dumps(
        {
            'handoff': 'ILLUSTRATIVE EXAMPLE ONLY: follow the final user prompt and its actual DECLARED PATHS.',
            'files': {
                '__example_only__/illustrative.py': 'GREETING = "hello"\nRETURN_CODE = 0\n',
            },
        }
    )
    return [
        {
            'role': 'system',
            'content': (
                'Respond with ONLY one strict JSON object matching '
                '{"handoff":"brief string","files":{"path":"utf8 file content"}}. '
                'Do not include Markdown code fences, prose, a second JSON object, or any extra trailing closing brace. '
                'Serialize UTF-8 file contents into JSON strings exactly once (single-layer JSON escaping for newlines '
                'and double quotes). The assistant example below is illustrative only; ignore its example path and '
                'example code, and follow the final user message actual DECLARED PATHS and requirements.'
            ),
        },
        {
            'role': 'assistant',
            'content': example_response,
        },
        {
            'role': 'user',
            'content': prompt,
        },
    ]


def build_chat_request_payload(
    provider: str, model: str, prompt: str
) -> dict[str, object]:
    """Build tool-free chat.completions request payload with zero-price and no-fallback constraints."""
    if provider == 'zai':
        verified_model = resolve_zai_model(model)
        return {
            'model': verified_model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0,
            'max_tokens': 4096,
            'stream': False,
            'thinking': {'type': 'disabled'},
            'response_format': {'type': 'json_object'},
        }
    if provider == 'openrouter':
        verified_model, _vendor, _slug = resolve_openrouter_model(model)
        return {
            'model': verified_model,
            'messages': _openrouter_json_messages(prompt),
            'temperature': 0,
            'max_tokens': 4096,
            'stream': False,
            'reasoning': {'enabled': False},
            'response_format': {'type': 'json_object'},
            'max_price': dict(ZERO_MAX_PRICE),
            'provider': {
                'allow_fallbacks': False,
                'require_parameters': True,
                'max_price': dict(ZERO_MAX_PRICE),
            },
        }
    raise FreeApiBlocked(f'unsupported free API provider: {provider!r}', category='terminal_proof')


def _strict_nonneg_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _parse_native_usage(provider, raw_response):
    label = 'Z.ai' if provider == 'zai' else ('OpenRouter' if provider == 'openrouter' else str(provider))
    if not isinstance(raw_response, dict):
        raise FreeApiBlocked(f'{label} response must be a JSON object', category='terminal_proof')
    usage_raw = raw_response.get('usage')
    if not isinstance(usage_raw, dict):
        raise FreeApiBlocked(f'{label} response missing usage object', category='terminal_proof')
    prompt_tokens = usage_raw.get('prompt_tokens')
    completion_tokens = usage_raw.get('completion_tokens')
    total_tokens = usage_raw.get('total_tokens')
    if not (
        _strict_nonneg_int(prompt_tokens)
        and _strict_nonneg_int(completion_tokens)
        and _strict_nonneg_int(total_tokens)
        and prompt_tokens > 0
        and completion_tokens > 0
    ):
        raise FreeApiBlocked(f'{label} usage tokens missing, non-integer, or non-positive', category='terminal_proof')
    if total_tokens != prompt_tokens + completion_tokens:
        raise FreeApiBlocked(
            f'{label} usage total_tokens ({total_tokens}) != prompt_tokens ({prompt_tokens}) + completion_tokens ({completion_tokens})',
            category='terminal_proof',
        )

    # Cached tokens are a subset of prompt_tokens; reasoning tokens are a subset of completion_tokens.
    # Never add them to prompt_tokens + completion_tokens a second time.
    prompt_details = usage_raw.get('prompt_tokens_details')
    if prompt_details is not None and not isinstance(prompt_details, dict):
        raise FreeApiBlocked(f'{label} usage.prompt_tokens_details malformed', category='terminal_proof')
    top_cached = usage_raw.get('cached_tokens')
    nested_cached = prompt_details.get('cached_tokens') if isinstance(prompt_details, dict) else None
    if top_cached is not None and nested_cached is not None and top_cached != nested_cached:
        raise FreeApiBlocked(f'{label} usage cached_tokens disagrees with prompt_tokens_details.cached_tokens', category='terminal_proof')
    cached_tokens = nested_cached if nested_cached is not None else (top_cached if top_cached is not None else 0)
    if not _strict_nonneg_int(cached_tokens) or cached_tokens > prompt_tokens:
        raise FreeApiBlocked(
            f'{label} usage cached_tokens ({cached_tokens!r}) invalid or exceeds prompt_tokens ({prompt_tokens})',
            category='terminal_proof',
        )

    comp_details = usage_raw.get('completion_tokens_details')
    if comp_details is not None and not isinstance(comp_details, dict):
        raise FreeApiBlocked(f'{label} usage.completion_tokens_details malformed', category='terminal_proof')
    top_reasoning = usage_raw.get('reasoning_tokens')
    nested_reasoning = comp_details.get('reasoning_tokens') if isinstance(comp_details, dict) else None
    if top_reasoning is not None and nested_reasoning is not None and top_reasoning != nested_reasoning:
        raise FreeApiBlocked(
            f'{label} usage reasoning_tokens disagrees with completion_tokens_details.reasoning_tokens',
            category='terminal_proof',
        )
    reasoning_tokens = nested_reasoning if nested_reasoning is not None else (top_reasoning if top_reasoning is not None else 0)
    if not _strict_nonneg_int(reasoning_tokens) or reasoning_tokens > completion_tokens:
        raise FreeApiBlocked(
            f'{label} usage reasoning_tokens ({reasoning_tokens!r}) invalid or exceeds completion_tokens ({completion_tokens})',
            category='terminal_proof',
        )

    return (
        usage_raw,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        cached_tokens,
        reasoning_tokens,
    )


def validate_chat_completion_response(
    provider: str,
    requested_model: str,
    raw_response: object,
) -> tuple[dict[str, object], str, dict[str, object]]:
    """Validate chat.completions response object, model identity, zero tools, stop reason, and token usage."""
    label = 'Z.ai' if provider == 'zai' else 'OpenRouter'
    if not isinstance(raw_response, dict):
        raise FreeApiBlocked(f'{label} response must be a JSON object', category='terminal_proof')
    if raw_response.get('error'):
        err = raw_response['error']
        err_msg = err.get('message') if isinstance(err, dict) else str(err)
        err_code = err.get('code') if isinstance(err, dict) else None
        cat = classify_failure(str(err_msg or ''), error_code=err_code, default_category='terminal_proof')
        raise FreeApiBlocked(f'{label} response contains error: {err_msg}', category=cat)
    request_id = raw_response.get('id')
    if not isinstance(request_id, str) or not request_id.strip():
        raise FreeApiBlocked(f'{label} response missing non-empty id', category='terminal_proof')
    actual_model = raw_response.get('model')
    if not isinstance(actual_model, str) or actual_model != requested_model:
        raise FreeApiBlocked(
            f'{label} response model mismatch: requested {requested_model!r}, got {actual_model!r}',
            category='terminal_proof',
        )
    choices = raw_response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise FreeApiBlocked(f'{label} response must contain exactly one choice object', category='terminal_proof')
    choice = choices[0]
    if choice.get('error'):
        err = choice['error']
        err_msg = err.get('message') if isinstance(err, dict) else str(err)
        err_code = err.get('code') if isinstance(err, dict) else None
        cat = classify_failure(str(err_msg or ''), error_code=err_code, default_category='terminal_proof')
        raise FreeApiBlocked(f'{label} choice contains error: {err_msg}', category=cat)
    finish_reason = choice.get('finish_reason')
    if finish_reason not in FREE_API_FINISH_REASONS:
        raise FreeApiBlocked(
            f'{label} choice finish_reason must be in {FREE_API_FINISH_REASONS}, got {finish_reason!r}',
            category='terminal_proof',
        )
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise FreeApiBlocked(f'{label} choice message missing or role is not assistant', category='terminal_proof')
    tool_calls = message.get('tool_calls')
    function_call = message.get('function_call')
    if (tool_calls is not None and tool_calls != []) or function_call is not None:
        raise FreeApiBlocked(f'{label} response attempted tool or function call in tool-free worker', category='terminal_proof')
    if message.get('refusal') is not None:
        raise FreeApiBlocked(f'{label} response reported a refusal', category='terminal_proof')
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        raise FreeApiBlocked(f'{label} assistant message content is empty or non-string', category='terminal_proof')
    if devin.has_provider_error(content):
        try:
            envelope = gate.strict_json(content)
        except ValueError:
            envelope = None
        valid_generation = (
            isinstance(envelope, dict)
            and set(envelope) == {'handoff', 'files'}
            and isinstance(envelope['handoff'], str)
            and bool(envelope['handoff'].strip())
            and isinstance(envelope['files'], dict)
            and bool(envelope['files'])
            and all(
                isinstance(name, str) and bool(name.strip()) and isinstance(source, str)
                for name, source in envelope['files'].items()
            )
        )
        if not valid_generation:
            cat = classify_failure(content, default_category='terminal_proof')
            raise FreeApiBlocked(f'{label} assistant content reports provider error', category=cat)

    (
        usage_raw,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        cached_tokens,
        reasoning_tokens,
    ) = _parse_native_usage(provider, raw_response)

    native_cost = None
    if provider == 'openrouter':
        for cost_field in ('cost', 'total_cost'):
            for container in (usage_raw, raw_response):
                if cost_field in container and container[cost_field] is not None:
                    val = container[cost_field]
                    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val != 0:
                        raise FreeApiBlocked(
                            f'OpenRouter response reported non-zero or invalid {cost_field}: {val!r}',
                            category='terminal_proof',
                        )
                    native_cost = 0

    normalized_usage = {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'total_tokens': total_tokens,
        'cached_tokens': cached_tokens,
        'reasoning_tokens': reasoning_tokens,
        'total_steps': 1,
    }
    sanitized_response = {
        'id': request_id,
        'provider': provider,
        'model': actual_model,
        'choices': [
            {
                'index': 0,
                'finish_reason': finish_reason,
                'message': {'role': 'assistant', 'content': content},
            }
        ],
        'usage': dict(normalized_usage),
    }
    if provider == 'openrouter':
        sanitized_response['native_cost'] = native_cost
    meta = {
        'request_id': request_id,
        'actual_generation_model': actual_model,
        'finish_reason': finish_reason,
        'usage': normalized_usage,
        'native_cost': native_cost,
    }
    return meta, content, sanitized_response


def run_free_api_attempt(
    *,
    provider: str,
    workspace: Path,
    attempt_dir: Path,
    contract: str,
    scope_files: list[str],
    verify_cmds: list[str],
    timeout_seconds: int,
    feedback: str | None = None,
    open_questions: list[str] | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
    context_mode: str | None = None,
    model: str | None = None,
    credential_path: Path | str | None = None,
    http_transport: Callable[..., tuple[int, bytes]] | None = None,
) -> dict[str, object]:
    """Run one fresh tool-free Z.ai or OpenRouter direct API generation and atomically apply verified files."""
    if provider not in FREE_API_PROVIDERS:
        raise ValueError(f'BLOCKED: unsupported free API provider: {provider!r}')
    attempt_dir = Path(attempt_dir).resolve()
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    transport = http_transport or _default_http_transport
    usage_basis = ZAI_USAGE_BASIS if provider == 'zai' else OPENROUTER_USAGE_BASIS
    effort_note = EFFORT_NOTE_ZAI if provider == 'zai' else EFFORT_NOTE_OPENROUTER
    endpoint = ZAI_CHAT_ENDPOINT if provider == 'zai' else OPENROUTER_CHAT_ENDPOINT
    result_filename = f'{provider}-result.json'
    request_filename = f'{provider}-request.json'
    response_filename = f'{provider}-response.json'
    catalog_filename = 'openrouter-catalog-proof.json'

    base: dict[str, object] = {
        'ok': False,
        'blocked': True,
        'reason': None,
        'error_category': None,
        'provider': provider,
        'requested_model': None,
        'model': None,
        'provider_id': None,
        'model_id': None,
        'effort_verification': effort_note,
        'request_id': None,
        'session_id': None,
        'conversation_id': None,
        'exit_code': None,
        'http_status': None,
        'proof_status': 'UNVERIFIED',
        'tool_calls': None,
        'tool_denied': False,
        'redirects_followed': 0,
        'endpoint': endpoint,
        'catalog_endpoint': OPENROUTER_MODELS_ENDPOINT if provider == 'openrouter' else None,
        'catalog_pricing': None,
        'request_max_price': dict(ZERO_MAX_PRICE) if provider == 'openrouter' else None,
        'allow_fallbacks': False if provider == 'openrouter' else None,
        'pricing_verified': False,
        'free_tier_only': False,
        'native_cost': None,
        'quota_attestation': ZAI_QUOTA_ATTESTATION if provider == 'zai' else None,
        'credential_source_status': 'UNVERIFIED',
        'response': '',
        'files_written': [],
        'written_hashes': {},
        'usage': {},
        'usage_basis': usage_basis,
        'usage_updates_observed': 0,
        'duration_seconds': 0.0,
        'partial_mutation': False,
        'context_mode': None,
        'prompt_chars': None,
        'request_sha256': None,
        'response_sha256': None,
        'catalog_sha256': None,
        'final_response_sha256': None,
        'generation_steps': 0,
        'finish_reason': None,
    }

    secret_holder: list[str | None] = [None]

    def blocked(reason: str, *, category: str = 'terminal_proof', **extra: object) -> dict[str, object]:
        safe_reason = str(_scrub_secret(reason, secret_holder[0]))
        record = _scrub_secret({**base, **extra, 'reason': safe_reason, 'error_category': category}, secret_holder[0])
        gate.save(attempt_dir / result_filename, record)
        return record

    try:
        mode = resolve_context_mode(context_mode)
        base['context_mode'] = mode
        if provider == 'zai':
            req_model = resolve_zai_model(model)
            prov_id, mod_id = 'zai', req_model
        else:
            req_model, prov_id, mod_id = resolve_openrouter_model(model)
        base['requested_model'] = req_model
        base['provider_id'] = prov_id
        base['model_id'] = mod_id

        validate_fixed_endpoint(provider, endpoint)
        if provider == 'openrouter':
            validate_fixed_endpoint(provider, OPENROUTER_MODELS_ENDPOINT)

        workspace = Path(workspace).resolve()
        if devin._is_within(attempt_dir, workspace) or devin._is_within(workspace, attempt_dir):
            return blocked(
                f'{provider} artifact directory must be outside the target workspace',
                category='terminal_proof',
            )
        sources = scan_sources(workspace, scope_files)
        prompt = build_prompt(
            contract,
            scope_files,
            sources,
            verify_cmds,
            feedback=feedback,
            open_questions=open_questions,
            attempt=attempt,
            max_attempts=max_attempts,
            context_mode=mode,
        )
        base['prompt_chars'] = len(prompt)
        (attempt_dir / f'{provider}-prompt.txt').write_text(prompt, encoding='utf-8')
        api_key, cred_meta = load_api_credential(provider, credential_path)
        secret_holder[0] = api_key
        base['credential_source_status'] = cred_meta['credential_source_status']
    except FreeApiBlocked as exc:
        return blocked(str(exc), category=exc.category, http_status=exc.http_status)
    except devin.DevinBlocked as exc:
        cat = classify_failure(str(exc), default_category='terminal_proof')
        return blocked(str(exc), category=cat)
    except OSError:
        return blocked(f'cannot prepare isolated {provider} request', category='terminal_proof')

    t0 = time.monotonic()
    catalog_sha256: str | None = None
    catalog_pricing: dict[str, str] | None = None
    try:
        if provider == 'openrouter':
            cat_header_value = f'Bearer {api_key}'
            cat_headers = {
                'Accept': 'application/json',
            }
            cat_headers.update(Authorization=cat_header_value)
            cat_status, cat_bytes = transport(
                OPENROUTER_MODELS_ENDPOINT,
                method='GET',
                headers=cat_headers,
                body=None,
                timeout_seconds=timeout_seconds,
                max_bytes=MAX_CATALOG_BYTES,
                redaction_value=api_key,
            )
            if cat_status != 200:
                cat_category = classify_failure(
                    f'OpenRouter catalog HTTP {cat_status}',
                    http_status=cat_status,
                    default_category='terminal_proof',
                )
                return blocked(
                    f'OpenRouter catalog returned HTTP {cat_status}',
                    category=cat_category,
                    http_status=cat_status,
                )
            try:
                catalog_obj = gate.strict_json(cat_bytes.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                raise FreeApiBlocked('OpenRouter catalog JSON is malformed', category='terminal_proof') from exc
            catalog_proof = validate_openrouter_catalog_model(catalog_obj, req_model)
            catalog_proof_clean = _scrub_secret(catalog_proof, api_key)
            catalog_proof_bytes = (
                json.dumps(catalog_proof_clean, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
            ).encode('utf-8')
            (attempt_dir / catalog_filename).write_bytes(catalog_proof_bytes)
            catalog_sha256 = sha256_bytes(catalog_proof_bytes)
            catalog_pricing = dict(catalog_proof['pricing'])
            base['catalog_sha256'] = catalog_sha256
            base['catalog_pricing'] = catalog_pricing
            base['pricing_verified'] = True
            base['free_tier_only'] = True
        else:
            base['pricing_verified'] = True
            base['free_tier_only'] = True

        request_payload = build_chat_request_payload(provider, req_model, prompt)
        sanitized_request = _scrub_secret(request_payload, api_key)
        if gate.contains_secret(sanitized_request) or (api_key and api_key in json.dumps(sanitized_request)):
            raise FreeApiBlocked(f'possible credential in {provider} request payload', category='terminal_proof')
        request_artifact_bytes = (
            json.dumps(sanitized_request, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
        ).encode('utf-8')
        (attempt_dir / request_filename).write_bytes(request_artifact_bytes)
        request_sha256 = sha256_bytes(request_artifact_bytes)
        base['request_sha256'] = request_sha256

        req_body = json.dumps(request_payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        req_header_value = f'Bearer {api_key}'
        req_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        req_headers.update(Authorization=req_header_value)
        http_status, resp_bytes = transport(
            endpoint,
            method='POST',
            headers=req_headers,
            body=req_body,
            timeout_seconds=timeout_seconds,
            max_bytes=MAX_HTTP_RESPONSE_BYTES,
            redaction_value=api_key,
        )
        duration = round(time.monotonic() - t0, 2)
        base['duration_seconds'] = duration
        base['http_status'] = http_status
        if http_status != 200:
            cat = classify_failure(
                f'{provider} HTTP {http_status}',
                http_status=http_status,
                default_category='terminal_proof',
            )
            return blocked(
                f'{provider} chat.completions returned HTTP {http_status}',
                category=cat,
                http_status=http_status,
                exit_code=1,
            )
        try:
            resp_text = resp_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FreeApiBlocked(f'{provider} response is not valid UTF-8', category='terminal_proof', http_status=http_status) from exc
        if api_key and api_key in resp_text:
            raise FreeApiBlocked(f'{provider} response echoed raw API credential; refusing to retain or apply', category='terminal_proof')
        try:
            raw_resp_obj = gate.strict_json(resp_text)
        except ValueError as exc:
            raise FreeApiBlocked(
                f'{provider} response JSON is malformed or contains duplicate keys',
                category='terminal_proof',
                http_status=http_status,
            ) from exc

        if gate.contains_secret(raw_resp_obj):
            raise FreeApiBlocked('response contains secret-like material', category='terminal_proof')
        raw_resp_bytes = (
            json.dumps(raw_resp_obj, indent=2, sort_keys=True, allow_nan=False) + '\n'
        ).encode('utf-8')
        (Path(attempt_dir) / response_filename).write_bytes(raw_resp_bytes)
        base['response_sha256'] = sha256_bytes(raw_resp_bytes)
        try:
            (
                _usage_raw,
                _prompt_tokens,
                _completion_tokens,
                _total_tokens,
                _cached_tokens,
                _reasoning_tokens,
            ) = _parse_native_usage(provider, raw_resp_obj)
        except FreeApiBlocked:
            base['usage'] = {}
            base['usage_updates_observed'] = 0
        else:
            base['usage'] = {
                'prompt_tokens': _prompt_tokens,
                'completion_tokens': _completion_tokens,
                'total_tokens': _total_tokens,
                'cached_tokens': _cached_tokens,
                'reasoning_tokens': _reasoning_tokens,
                'total_steps': 1,
            }
            base['usage_updates_observed'] = 1
        meta, final_text, sanitized_resp = validate_chat_completion_response(provider, req_model, raw_resp_obj)
        sanitized_resp_clean = _scrub_secret(sanitized_resp, api_key)
        if gate.contains_secret(sanitized_resp_clean):
            raise FreeApiBlocked(f'possible credential in {provider} response', category='terminal_proof')
        response_artifact_bytes = (
            json.dumps(sanitized_resp_clean, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
        ).encode('utf-8')
        (attempt_dir / response_filename).write_bytes(response_artifact_bytes)
        response_sha256 = sha256_bytes(response_artifact_bytes)
        final_response_sha256 = sha256_bytes(final_text.encode('utf-8'))
        base.update(
            request_id=meta['request_id'],
            session_id=meta['request_id'],
            conversation_id=meta['request_id'],
            response_sha256=response_sha256,
            final_response_sha256=final_response_sha256,
            usage=meta['usage'],
            usage_updates_observed=1,
            generation_steps=1,
            finish_reason=meta['finish_reason'],
            native_cost=meta['native_cost'],
        )
    except FreeApiBlocked as exc:
        duration = round(time.monotonic() - t0, 2)
        return blocked(
            str(exc),
            category=exc.category,
            http_status=exc.http_status if exc.http_status is not None else base.get('http_status'),
            duration_seconds=duration,
            exit_code=1,
        )

    try:
        if api_key and api_key in final_text:
            raise FreeApiBlocked(f'possible credential in {provider} generation output', category='terminal_proof')
        handoff, files = parse_generation(final_text, scope_files)
        if api_key and (api_key in handoff or any(api_key.encode('utf-8') in data for data in files.values())):
            raise FreeApiBlocked(f'possible credential in {provider} handoff or file output', category='terminal_proof')
        preconditions = {str(s['path']): s['sha256'] for s in sources}
        written = apply_files(workspace, files, preconditions, recovery_dir=attempt_dir / 'recovery')
    except devin.DevinPartialMutation as exc:
        return blocked(str(exc), category='terminal_proof', partial_mutation=True, exit_code=1)
    except (FreeApiBlocked, devin.DevinBlocked) as exc:
        return blocked(str(exc), category='terminal_proof', exit_code=1)
    except OSError:
        return blocked('atomic application failed; every replaced file was restored and verified', category='terminal_proof', exit_code=1)

    worker_proof: dict[str, object] = {
        'provider': provider,
        'requested_model': req_model,
        'actual_generation_model': meta['actual_generation_model'],
        'provider_id': prov_id,
        'model_id': mod_id,
        'endpoint': endpoint,
        'catalog_endpoint': OPENROUTER_MODELS_ENDPOINT if provider == 'openrouter' else None,
        'catalog_pricing': catalog_pricing if provider == 'openrouter' else None,
        'request_max_price': dict(ZERO_MAX_PRICE) if provider == 'openrouter' else None,
        'allow_fallbacks': False if provider == 'openrouter' else None,
        'pricing_verified': True,
        'free_tier_only': True,
        'native_cost': meta['native_cost'],
        'quota_attestation': ZAI_QUOTA_ATTESTATION if provider == 'zai' else None,
        'credential_source_status': 'VERIFIED_PRIVATE_FILE',
        'request_id': meta['request_id'],
        'exit_code': 0,
        'http_status': 200,
        'tool_calls': 0,
        'tool_denied': False,
        'redirects_followed': 0,
        'generation_steps': 1,
        'finish_reason': meta['finish_reason'],
        'request_sha256': base['request_sha256'],
        'response_sha256': base['response_sha256'],
        'catalog_sha256': catalog_sha256,
        'final_response_sha256': base['final_response_sha256'],
        'usage': dict(meta['usage']),
    }

    record = _scrub_secret(
        {
            **base,
            'ok': True,
            'blocked': False,
            'reason': None,
            'error_category': None,
            'model': meta['actual_generation_model'],
            'exit_code': 0,
            'http_status': 200,
            'proof_status': 'VERIFIED',
            'tool_calls': 0,
            'tool_denied': False,
            'redirects_followed': 0,
            'response': handoff,
            'files_written': sorted(written),
            'written_hashes': written,
            'worker_proof': worker_proof,
        },
        api_key,
    )
    gate.save(attempt_dir / result_filename, record)
    return record


class CoordinatorRetryLedger:
    """Task-wide initial-plus-two-extra-worker-execution ledger across coordinator-directed provider switches.

    Preserves the original workspace snapshot across provider switches and reworks, prefers Z.ai
    (`glm-4.5-flash`) then explicitly pinned OpenRouter (`<vendor>/<slug>:free`) on availability/quota
    errors only, and terminates immediately on permission denial, auth failure, or model/proof mismatch.
    """

    def __init__(
        self,
        workspace: Path,
        scope_files: list[str],
        *,
        max_extra_executions: int = MAX_EXTRA_WORKER_EXECUTIONS,
    ):
        if type(max_extra_executions) is not int or not 0 <= max_extra_executions <= MAX_EXTRA_WORKER_EXECUTIONS:
            raise ValueError(f'max_extra_executions must be an int in 0..{MAX_EXTRA_WORKER_EXECUTIONS}')
        self.workspace = Path(workspace).resolve()
        self.scope_files = validate_scope_paths(scope_files)
        self.max_extra_executions = max_extra_executions
        self.max_total_executions = MAX_INITIAL_WORKER_EXECUTIONS + max_extra_executions
        self.baseline_snapshot = capture_workspace_snapshot(self.workspace, self.scope_files)
        self.baseline_hashes = {
            rel: entry['sha256'] for rel, entry in self.baseline_snapshot.items()
        }
        self.attempts: list[dict[str, object]] = []

    @property
    def executions_used(self) -> int:
        return len(self.attempts)

    @property
    def extra_executions_used(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def remaining_executions(self) -> int:
        return max(0, self.max_total_executions - len(self.attempts))

    def restore_baseline(self) -> dict[str, str | None]:
        """Restore workspace to the original baseline snapshot before a coordinator-directed fallback."""
        restored = restore_workspace_snapshot(self.workspace, self.baseline_snapshot)
        if restored != self.baseline_hashes:
            raise FreeApiBlocked('workspace baseline snapshot hashes did not match after restore', category='terminal_proof')
        return restored

    def can_switch_provider(self, last_record: dict[str, object], next_provider: str) -> tuple[bool, str | None]:
        if any(not isinstance(a, dict) or a.get('terminal') is not False for a in self.attempts):
            return False, 'prior terminal attempt in retry ledger forbids provider switch'
        if next_provider not in FREE_API_PROVIDERS:
            return False, f'unsupported fallback provider: {next_provider!r}'
        if self.executions_used >= self.max_total_executions:
            return False, f'retry ledger exhausted ({self.executions_used}/{self.max_total_executions} worker executions used)'
        if not is_availability_fallback_eligible(last_record):
            cat = last_record.get('error_category') if isinstance(last_record, dict) else 'unknown'
            return False, f'provider fallback forbidden for non-availability failure category: {cat!r}'
        return True, None

    def record_attempt(self, provider: str, record: dict[str, object]) -> None:
        if self.executions_used >= self.max_total_executions:
            raise FreeApiBlocked('task-wide worker execution ledger exceeded', category='terminal_proof')
        self.attempts.append(
            {
                'terminal': not (is_availability_fallback_eligible(record) or (isinstance(record, dict) and record.get('ok') is True and record.get('blocked') is False and record.get('tool_denied') is False and record.get('partial_mutation') is False and isinstance(record.get('files_written'), list) and record.get('proof_status') == 'VERIFIED')),
                'attempt_number': self.executions_used + 1,
                'provider': provider,
                'model': record.get('model') or record.get('requested_model'),
                'ok': bool(record.get('ok')),
                'error_category': record.get('error_category'),
                'reason': record.get('reason'),
                'usage': record.get('usage') if isinstance(record.get('usage'), dict) else {},
            }
        )

    def summary(self) -> dict[str, object]:
        return {
            'max_total_executions': self.max_total_executions,
            'max_extra_executions': self.max_extra_executions,
            'executions_used': self.executions_used,
            'extra_executions_used': self.extra_executions_used,
            'remaining_executions': self.remaining_executions,
            'baseline_hashes': dict(self.baseline_hashes),
            'attempts': list(self.attempts),
        }


def run_coordinator_free_api_with_fallback(
    *,
    workspace: Path,
    output_dir: Path,
    contract: str,
    scope_files: list[str],
    verify_cmds: list[str],
    openrouter_model: str,
    zai_model: str = ZAI_DEFAULT_MODEL,
    timeout_seconds: int = 120,
    context_mode: str | None = None,
    zai_credential_path: Path | str | None = None,
    openrouter_credential_path: Path | str | None = None,
    http_transport: Callable[..., tuple[int, bytes]] | None = None,
    ledger: CoordinatorRetryLedger | None = None,
) -> dict[str, object]:
    """Coordinator-directed Z.ai -> OpenRouter fallback on availability error within existing retry ledger."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    active_ledger = ledger or CoordinatorRetryLedger(workspace, scope_files)
    if any(not isinstance(a, dict) or a.get('terminal') is not False for a in active_ledger.attempts):
        return {
            'ok': False,
            'blocked': True,
            'selected_provider': None,
            'reason': 'prior terminal attempt in retry ledger forbids further execution',
            'ledger': active_ledger.summary(),
            'worker_result': None,
            'fallback_used': False,
        }
    if active_ledger.executions_used >= active_ledger.max_total_executions:
        return {
            'ok': False,
            'blocked': True,
            'selected_provider': None,
            'reason': 'retry ledger exhausted before worker execution',
            'ledger': active_ledger.summary(),
            'worker_result': None,
        }

    zai_attempt_num = active_ledger.executions_used + 1
    zai_dir = output_dir / f'attempt-{zai_attempt_num}-zai'
    zai_record = run_free_api_attempt(
        provider='zai',
        workspace=workspace,
        attempt_dir=zai_dir,
        contract=contract,
        scope_files=scope_files,
        verify_cmds=verify_cmds,
        timeout_seconds=timeout_seconds,
        attempt=zai_attempt_num,
        max_attempts=active_ledger.max_total_executions,
        context_mode=context_mode,
        model=zai_model,
        credential_path=zai_credential_path,
        http_transport=http_transport,
    )
    active_ledger.record_attempt('zai', zai_record)
    if zai_record.get('ok') is True:
        return {
            'ok': True,
            'blocked': False,
            'selected_provider': 'zai',
            'fallback_used': False,
            'reason': None,
            'ledger': active_ledger.summary(),
            'worker_result': zai_record,
        }

    allowed, deny_reason = active_ledger.can_switch_provider(zai_record, 'openrouter')
    if not allowed:
        return {
            'ok': False,
            'blocked': True,
            'selected_provider': 'zai',
            'fallback_used': False,
            'reason': f"{zai_record.get('reason')}; {deny_reason}",
            'ledger': active_ledger.summary(),
            'worker_result': zai_record,
        }

    active_ledger.restore_baseline()
    or_attempt_num = active_ledger.executions_used + 1
    or_dir = output_dir / f'attempt-{or_attempt_num}-openrouter'
    or_record = run_free_api_attempt(
        provider='openrouter',
        workspace=workspace,
        attempt_dir=or_dir,
        contract=contract,
        scope_files=scope_files,
        verify_cmds=verify_cmds,
        timeout_seconds=timeout_seconds,
        attempt=or_attempt_num,
        max_attempts=active_ledger.max_total_executions,
        context_mode=context_mode,
        model=openrouter_model,
        credential_path=openrouter_credential_path,
        http_transport=http_transport,
    )
    active_ledger.record_attempt('openrouter', or_record)
    return {
        'ok': bool(or_record.get('ok')),
        'blocked': not bool(or_record.get('ok')),
        'selected_provider': 'openrouter',
        'fallback_used': True,
        'reason': or_record.get('reason'),
        'ledger': active_ledger.summary(),
        'worker_result': or_record,
    }
