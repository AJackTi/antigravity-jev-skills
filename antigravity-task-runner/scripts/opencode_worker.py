#!/usr/bin/env python3
"""Tool-free OpenCode1 content-generation worker adapter for run_workflow.py.

Provider boundary only: require an explicit exact `OPENCODE_MODEL` (`<providerID>/<modelID>`,
never fabricated), build a bounded prompt from the complete task contract plus current declared
files via `devin_worker.py`, run ONE fresh OpenCode1 session (`--pure run --format json` followed
by `--pure export <sessionID>`) in a new isolated artifact cwd and isolated XDG config/data/state/cache
directories (without repurposing `HOME`), scrub ambient OpenCode config/server/endpoint overrides,
enforce a deny-all permission config with default plugins/external skills/project config disabled,
validate both the JSON event stream and exported session transcript, and only then mechanically apply
the model-authored UTF-8 bytes via `devin_worker.apply_files`. OpenCode proof is never labelled as
`agy` proof, `request-review`, `SUCCESS` status, or `high` effort.
"""
from __future__ import annotations

from collections.abc import Mapping
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

SCRIPT_DIR = Path(__file__).resolve().parent
_gate_spec = importlib.util.spec_from_file_location('jev_review_gate_for_opencode', SCRIPT_DIR / 'jev_review_gate.py')
gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(gate)

_devin_spec = importlib.util.spec_from_file_location('devin_worker_for_opencode', SCRIPT_DIR / 'devin_worker.py')
devin = importlib.util.module_from_spec(_devin_spec)
_devin_spec.loader.exec_module(devin)

WORKER_ENV = devin.WORKER_ENV
DEFAULT_WORKER = devin.DEFAULT_WORKER
SUPPORTED_WORKERS = gate.WORKFLOW_WORKER_PROVIDERS
CONTEXT_ENV = devin.CONTEXT_ENV
DEFAULT_CONTEXT_MODE = devin.DEFAULT_CONTEXT_MODE
SUPPORTED_CONTEXT_MODES = devin.SUPPORTED_CONTEXT_MODES
OPENCODE_MODEL_ENV = 'OPENCODE_MODEL'
OPENCODE_BIN_ENV = 'OPENCODE_BIN'

MAX_FILE_BYTES = devin.MAX_FILE_BYTES
MAX_TOTAL_SOURCE_BYTES = devin.MAX_TOTAL_SOURCE_BYTES
MAX_PROMPT_CHARS = devin.MAX_PROMPT_CHARS
MAX_RESPONSE_CHARS = devin.MAX_RESPONSE_CHARS
MAX_EXPORT_BYTES = devin.MAX_EXPORT_BYTES
MAX_CONFIG_BYTES = devin.MAX_CONFIG_BYTES
MAX_HANDOFF_CHARS = devin.MAX_HANDOFF_CHARS
MAX_FEEDBACK_CHARS = devin.MAX_FEEDBACK_CHARS

PRELAUNCH_CONFIG_FILENAME = 'opencode-config.prelaunch.json'
POSTRUN_CONFIG_FILENAME = 'opencode-config.postrun.json'
NATIVE_LOG_FILENAME = 'opencode-native.log'
OPENCODE_SCHEMA_URL = 'https://opencode.ai/config.json'
DEFAULT_RUN_TITLE = 'opencode-worker'
DISABLED_OPENCODE_AGENTS = ('title', 'summary', 'compaction')
AUXILIARY_LOG_RE = re.compile(
    r'(?i)\b(?:small=true|agent=(?:title|summary|compaction)|mode=(?:title|summary|compaction))\b'
)
LLM_SERVICE_LOG_RE = re.compile(r'\bservice=llm\b')
LLM_STREAM_CALL_RE = re.compile(r'\bservice=llm\b[^\n]*\bstream\b')
LOG_PROVIDER_RE = re.compile(r'\bproviderID=([^\s]+)')
LOG_MODEL_RE = re.compile(r'\bmodelID=([^\s]+)')
OPENCODE_FINISH_REASONS = ('stop', 'end_turn', 'completed')
OPENCODE_BAD_FINISH_REASONS = (
    'tool-calls',
    'tool_calls',
    'length',
    'max_tokens',
    'error',
    'cancelled',
    'canceled',
    'aborted',
    'content-filter',
    'content_filter',
    'unknown',
)
OPENCODE_METRIC_KEYS = (
    'input_tokens',
    'output_tokens',
    'reasoning_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'total_tokens',
    'total_steps',
)
UNKNOWN_USAGE: dict[str, str] = {key: 'unknown' for key in OPENCODE_METRIC_KEYS}
USAGE_BASIS = (
    'OpenCode1 native step_finish and session export token counters of one fresh isolated session '
    '(never cumulative or resumed); tokens.input excludes cache.read/cache.write and tokens.output excludes reasoning, '
    'so all five categories are disjoint and total_tokens = input_tokens + output_tokens + reasoning_tokens '
    '+ cache_read_tokens + cache_write_tokens summed once'
)
OPENCODE_USAGE_BASIS = USAGE_BASIS
EFFORT_NOTE = 'not applicable: OpenCode worker adapter exposes no effort claim; never labelled high or request-review'

KNOWN_OPENCODE_PERMISSIONS = (
    'read',
    'edit',
    'glob',
    'grep',
    'list',
    'bash',
    'task',
    'skill',
    'lsp',
    'todoread',
    'todowrite',
    'webfetch',
    'websearch',
    'codesearch',
    'external_directory',
    'doom_loop',
    'question',
    '*',
)
SCRUB_ENV_PREFIXES = ('OPENCODE_',)
SCRUB_ENV_KEYS = (
    'OPENAI_BASE_URL',
    'ANTHROPIC_BASE_URL',
    'GEMINI_BASE_URL',
    'GOOGLE_GEMINI_BASE_URL',
    'OPENROUTER_BASE_URL',
    'OLLAMA_HOST',
)
FORBIDDEN_ARGV_FLAGS = (
    '--standalone',
    '--auto',
    '--dangerously-skip-permissions',
    '--accept-edits',
    'upgrade',
)
ALLOWED_STREAM_EVENT_TYPES = frozenset({'step_start', 'text', 'reasoning', 'step_finish'})
ALLOWED_ASSISTANT_PART_TYPES = frozenset({'step-start', 'step_start', 'text', 'reasoning', 'step-finish', 'step_finish'})
FORBIDDEN_PART_KEYS = frozenset({'tool', 'tool_calls', 'tool_call', 'callID', 'patch', 'command', 'subtask'})


class OpenCodeBlocked(devin.DevinBlocked, ValueError):
    """Fail-closed condition for the OpenCode adapter; never applied, never retried silently."""


class OpenCodePartialMutation(devin.DevinPartialMutation, OpenCodeBlocked):
    """Rollback could not restore every replaced file; workspace may hold OpenCode bytes."""


# Reuse Devin's bounded source/prompt/JSON/apply helpers directly rather than duplicating them.
sha256_bytes = devin.sha256_bytes
resolve_context_mode = devin.resolve_context_mode
validate_scope_paths = devin.validate_scope_paths
safe_target = devin.safe_target
read_regular_nofollow = devin.read_regular_nofollow
scan_sources = devin.scan_sources
build_feedback = devin.build_feedback
build_prompt = devin.build_prompt
unwrap_generation_json = devin.unwrap_generation_json
parse_generation = devin.parse_generation
apply_files = devin.apply_files
has_provider_error = devin.has_provider_error
read_opencode_config_file = devin.read_devin_config_file


def _nonneg_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _pos_int(value: object) -> bool:
    return type(value) is int and value > 0


def resolve_worker(value: str | None = None) -> str:
    """Per-invocation WORKFLOW_WORKER (agy default when unset/empty); unknown values fail before any call."""
    raw = os.environ.get(WORKER_ENV) if value is None else value
    worker = raw or DEFAULT_WORKER
    if worker not in SUPPORTED_WORKERS:
        raise ValueError('BLOCKED: unsupported WORKFLOW_WORKER; allowed: ' + ', '.join(SUPPORTED_WORKERS))
    return worker


def resolve_opencode_model(value: object = None) -> tuple[str, str, str]:
    """Validate explicit exact `<providerID>/<modelID>` from arg or OPENCODE_MODEL (never defaulted)."""
    raw = os.environ.get(OPENCODE_MODEL_ENV) if value is None else value
    try:
        provider_id, model_id = gate.parse_opencode_model(raw)
    except ValueError as exc:
        raise OpenCodeBlocked(str(exc)) from exc
    return f'{provider_id}/{model_id}', provider_id, model_id


def resolve_opencode_executable(explicit_bin: str | None = None) -> str:
    """Resolve OpenCode CLI binary via explicit OPENCODE_BIN (fail closed if invalid) or standard PATH locations."""
    raw_bin = os.environ.get(OPENCODE_BIN_ENV) if explicit_bin is None else explicit_bin
    if raw_bin is not None and str(raw_bin) != '':
        if not isinstance(raw_bin, str) or raw_bin != raw_bin.strip() or '\x00' in raw_bin:
            raise FileNotFoundError('required executable unavailable: invalid OPENCODE_BIN')
        candidate = Path(raw_bin).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise FileNotFoundError(f'required executable unavailable: {raw_bin}')
    candidates: list[Path] = []
    found = shutil.which('opencode')
    if found:
        candidates.append(Path(found))
    candidates.extend((
        Path('/opt/homebrew/bin/opencode'),
        Path('/usr/local/bin/opencode'),
        Path.home() / '.opencode/bin/opencode',
        Path.home() / '.local/bin/opencode',
    ))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError('required executable unavailable: opencode')


def build_opencode_permission() -> dict[str, str]:
    """Ordered deny-all permission map with wildcard `*` last so native rule evaluation cannot widen deny-all."""
    return {key: 'deny' for key in KNOWN_OPENCODE_PERMISSIONS}


def build_opencode_disabled_agents() -> dict[str, dict[str, bool]]:
    """Disable automatic title, summary, and compaction sub-generations so no hidden LLM call occurs."""
    return {name: {'disable': True} for name in DISABLED_OPENCODE_AGENTS}


def build_opencode_config(model: object = None) -> dict[str, object]:
    """Credential-free per-session OpenCode1 config: exact model, disabled auxiliary agents, no plugins/MCP/instructions/share/autoupdate, deny-all tools."""
    full_model, _, _ = resolve_opencode_model(model)
    return {
        '$schema': OPENCODE_SCHEMA_URL,
        'model': full_model,
        'small_model': full_model,
        'autoupdate': False,
        'share': 'disabled',
        'snapshot': False,
        'plugin': [],
        'mcp': {},
        'instructions': [],
        'agent': build_opencode_disabled_agents(),
        'permission': build_opencode_permission(),
    }


def validate_opencode_config(cfg: object, expected_model: object = None) -> list[str]:
    problems: list[str] = []
    try:
        full_model, _, _ = resolve_opencode_model(expected_model)
    except OpenCodeBlocked as exc:
        return [str(exc)]
    if not isinstance(cfg, dict):
        return ['OpenCode config must be an object']
    if cfg.get('$schema') != OPENCODE_SCHEMA_URL:
        problems.append('OpenCode config must set $schema to ' + OPENCODE_SCHEMA_URL)
    if type(cfg.get('model')) is not str or cfg.get('model') != full_model:
        problems.append('OpenCode config must pin model to ' + full_model)
    if type(cfg.get('small_model')) is not str or cfg.get('small_model') != full_model:
        problems.append('OpenCode config must pin small_model to ' + full_model)
    if cfg.get('autoupdate') is not False:
        problems.append('OpenCode config must disable autoupdate')
    if type(cfg.get('share')) is not str or cfg.get('share') != 'disabled':
        problems.append('OpenCode config must set share to "disabled"')
    if cfg.get('snapshot') is not False:
        problems.append('OpenCode config must disable snapshot')
    if cfg.get('plugin') != []:
        problems.append('OpenCode config must set plugin to []')
    if cfg.get('mcp') != {}:
        problems.append('OpenCode config must set mcp to {}')
    if cfg.get('instructions') != []:
        problems.append('OpenCode config must set instructions to []')
    agent_cfg = cfg.get('agent')
    if not (
        isinstance(agent_cfg, dict)
        and set(agent_cfg.keys()) == set(DISABLED_OPENCODE_AGENTS)
        and all(
            isinstance(agent_cfg.get(name), dict)
            and set(agent_cfg[name].keys()) == {'disable'}
            and agent_cfg[name].get('disable') is True
            for name in DISABLED_OPENCODE_AGENTS
        )
    ):
        problems.append('OpenCode config must disable title, summary, and compaction agents')
    perm = cfg.get('permission')
    if not (
        isinstance(perm, dict)
        and list(perm.keys()) == list(KNOWN_OPENCODE_PERMISSIONS)
        and perm.get('*') == 'deny'
        and all(type(v) is str and v == 'deny' for v in perm.values())
    ):
        problems.append('OpenCode config must deny every known tool permission and end with "*": "deny"')
    expected_keys = {
        '$schema',
        'model',
        'small_model',
        'autoupdate',
        'share',
        'snapshot',
        'plugin',
        'mcp',
        'instructions',
        'agent',
        'permission',
    }
    if set(cfg.keys()) != expected_keys:
        problems.append('OpenCode config contains unexpected or missing top-level keys')
    if any(isinstance(k, str) and devin.CONFIG_FORBIDDEN_KEY_RE.search(k) for k in devin._config_keys(cfg)) or gate.contains_secret(cfg):
        problems.append('OpenCode config must not contain credentials, auth or endpoint fields')
    return problems


def validate_postrun_opencode_config(
    cfg: object, expected_model: object = None, prelaunch_config: object = None
) -> list[str]:
    """Validate persisted post-run OpenCode config: must equal canonical and prelaunch config with no widening."""
    try:
        full_model, _, _ = resolve_opencode_model(expected_model)
    except OpenCodeBlocked as exc:
        return [str(exc)]
    if not isinstance(cfg, dict):
        return ['OpenCode config must be an object']
    canonical = build_opencode_config(full_model)
    if prelaunch_config is None:
        expected = canonical
    elif (
        not isinstance(prelaunch_config, dict)
        or validate_opencode_config(prelaunch_config, expected_model=full_model)
        or not devin._strict_equal(prelaunch_config, canonical)
    ):
        return ['OpenCode prelaunch config reference is invalid or modified']
    else:
        expected = prelaunch_config
    problems = validate_opencode_config(cfg, expected_model=full_model)
    if not devin._strict_equal(cfg, expected):
        msg = 'OpenCode post-run config must preserve exact prelaunch protected fields'
        if msg not in problems:
            problems.append(msg)
    return problems


def _build_config_proof(
    *,
    status: str,
    prelaunch_sha256: str | None,
    postrun_sha256: str | None,
    protected_equal: bool,
) -> dict[str, object]:
    return {
        'status': status,
        'prelaunch_sha256': prelaunch_sha256,
        'postrun_sha256': postrun_sha256,
        'prelaunch_config_sha256': prelaunch_sha256,
        'postrun_config_sha256': postrun_sha256,
        'prelaunch_artifact': PRELAUNCH_CONFIG_FILENAME,
        'postrun_artifact': POSTRUN_CONFIG_FILENAME if postrun_sha256 is not None else None,
        'protected_fields_equal': protected_equal,
        'protected_config_equal': protected_equal,
    }


def build_isolated_env(
    base_env: Mapping[str, str] | None,
    *,
    attempt_dir: Path,
    xdg_root: Path,
    config_path: Path,
    config_obj: dict[str, object],
) -> tuple[dict[str, str], dict[str, object]]:
    """Construct isolated XDG + OpenCode1 env without repurposing HOME and scrubbing ambient overrides."""
    source_env = dict(os.environ if base_env is None else base_env)
    home_raw = source_env.get('HOME') or str(Path.home())
    home_path = Path(home_raw).resolve()
    attempt_resolved = Path(attempt_dir).resolve()
    xdg_resolved = Path(xdg_root).resolve()
    if (
        home_path == attempt_resolved
        or devin._is_within(home_path, attempt_resolved)
        or home_path == xdg_resolved
        or devin._is_within(home_path, xdg_resolved)
    ):
        raise OpenCodeBlocked('OpenCode isolation must use XDG variables without repurposing HOME into the attempt directory')

    scrubbed_keys: list[str] = []
    clean_env: dict[str, str] = {}
    for key, val in source_env.items():
        if any(key.startswith(prefix) for prefix in SCRUB_ENV_PREFIXES) or key in SCRUB_ENV_KEYS:
            scrubbed_keys.append(key)
            continue
        clean_env[key] = str(val)

    xdg_config_home = xdg_resolved / 'config'
    xdg_data_home = xdg_resolved / 'data'
    xdg_state_home = xdg_resolved / 'state'
    xdg_cache_home = xdg_resolved / 'cache'
    opencode_config_dir = xdg_config_home / 'opencode'
    for directory in (xdg_config_home, xdg_data_home, xdg_state_home, xdg_cache_home, opencode_config_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    config_content = json.dumps(config_obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    permission_content = json.dumps(config_obj['permission'], ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if gate.contains_secret(config_content) or gate.contains_secret(permission_content):
        raise OpenCodeBlocked('OpenCode inline config/permission must not contain secrets')

    clean_env['HOME'] = str(home_raw)
    clean_env['XDG_CONFIG_HOME'] = str(xdg_config_home)
    clean_env['XDG_DATA_HOME'] = str(xdg_data_home)
    clean_env['XDG_STATE_HOME'] = str(xdg_state_home)
    clean_env['XDG_CACHE_HOME'] = str(xdg_cache_home)
    clean_env['OPENCODE_CONFIG'] = str(config_path)
    clean_env['OPENCODE_CONFIG_DIR'] = str(opencode_config_dir)
    clean_env['OPENCODE_CONFIG_CONTENT'] = config_content
    clean_env['OPENCODE_PERMISSION'] = permission_content
    clean_env['OPENCODE_DISABLE_DEFAULT_PLUGINS'] = 'true'
    clean_env['OPENCODE_DISABLE_EXTERNAL_SKILLS'] = 'true'
    clean_env['OPENCODE_DISABLE_PROJECT_CONFIG'] = 'true'
    clean_env['OPENCODE_DISABLE_AUTOCOMPACT'] = 'true'
    clean_env['OPENCODE_DISABLE_PRUNE'] = 'true'
    clean_env['OPENCODE_DISABLE_TERMINAL_TITLE'] = 'true'

    isolation_summary: dict[str, object] = {
        'home_repurposed': False,
        'xdg_config_home': str(xdg_config_home),
        'xdg_data_home': str(xdg_data_home),
        'xdg_state_home': str(xdg_state_home),
        'xdg_cache_home': str(xdg_cache_home),
        'opencode_config': str(config_path),
        'opencode_config_dir': str(opencode_config_dir),
        'disable_default_plugins': True,
        'disable_external_skills': True,
        'disable_project_config': True,
        'disable_autocompact': True,
        'disable_prune': True,
        'disable_terminal_title': True,
        'scrubbed_env_keys': sorted(scrubbed_keys),
    }
    return clean_env, isolation_summary


def build_run_argv(
    executable: str,
    full_model: str,
    prompt: str,
    *,
    title: str = DEFAULT_RUN_TITLE,
) -> list[str]:
    resolve_opencode_model(full_model)
    if not isinstance(title, str) or not title.strip() or title != title.strip() or '\x00' in title:
        raise OpenCodeBlocked('OpenCode run requires a non-empty deterministic --title')
    argv = [executable, '--pure', 'run', '--format', 'json', '--model', full_model, '--title', title, prompt]
    if any(flag in argv[:-1] for flag in FORBIDDEN_ARGV_FLAGS):
        raise OpenCodeBlocked('OpenCode run argv contains forbidden flag')
    return argv


def build_export_argv(executable: str, session_id: str) -> list[str]:
    if not isinstance(session_id, str) or not session_id.strip() or session_id != session_id.strip():
        raise OpenCodeBlocked('OpenCode export requires a non-empty session_id')
    argv = [executable, '--pure', 'export', session_id]
    if any(flag in argv for flag in FORBIDDEN_ARGV_FLAGS):
        raise OpenCodeBlocked('OpenCode export argv contains forbidden flag')
    return argv


def _parse_tokens_block(tokens: object, *, label: str) -> dict[str, int]:
    """Validate OpenCode token counters; all five categories are disjoint and sum once to total_tokens."""
    if not isinstance(tokens, dict):
        raise OpenCodeBlocked(f'{label} missing or not an object')
    allowed_keys = {'input', 'output', 'reasoning', 'cache', 'total'}
    required_keys = {'input', 'output', 'reasoning', 'cache'}
    if not required_keys <= set(tokens.keys()) or not set(tokens.keys()) <= allowed_keys:
        raise OpenCodeBlocked(f'{label} has missing or unexpected token keys')
    cache = tokens.get('cache')
    if not isinstance(cache, dict) or set(cache.keys()) != {'read', 'write'}:
        raise OpenCodeBlocked(f'{label}.cache must contain exact keys read and write')
    in_tok = tokens.get('input')
    out_tok = tokens.get('output')
    reas_tok = tokens.get('reasoning')
    c_read = cache.get('read')
    c_write = cache.get('write')
    if not all(_nonneg_int(v) for v in (in_tok, out_tok, reas_tok, c_read, c_write)):
        raise OpenCodeBlocked(f'{label} counters must be non-negative integers (bools/floats/nulls rejected)')
    expected_total = int(in_tok) + int(out_tok) + int(reas_tok) + int(c_read) + int(c_write)
    if 'total' in tokens:
        if not _nonneg_int(tokens.get('total')):
            raise OpenCodeBlocked(f'{label}.total must be a non-negative integer when present')
        if int(tokens['total']) != expected_total:
            raise OpenCodeBlocked(
                f'{label}.total ({tokens["total"]}) disagrees with disjoint five-category sum ({expected_total})'
            )
    return {
        'input_tokens': int(in_tok),
        'output_tokens': int(out_tok),
        'reasoning_tokens': int(reas_tok),
        'cache_read_tokens': int(c_read),
        'cache_write_tokens': int(c_write),
        'total_tokens': expected_total,
    }


def _aggregate_step_tokens(step_tokens: list[dict[str, int]], *, label: str) -> dict[str, int]:
    if not step_tokens:
        raise OpenCodeBlocked(f'{label} has no verified generation token steps')
    in_tok = sum(item['input_tokens'] for item in step_tokens)
    out_tok = sum(item['output_tokens'] for item in step_tokens)
    reas_tok = sum(item['reasoning_tokens'] for item in step_tokens)
    c_read = sum(item['cache_read_tokens'] for item in step_tokens)
    c_write = sum(item['cache_write_tokens'] for item in step_tokens)
    if in_tok <= 0 or out_tok <= 0:
        raise OpenCodeBlocked(f'{label} input_tokens and output_tokens must be positive')
    total_tok = in_tok + out_tok + reas_tok + c_read + c_write
    if any(
        item.get(
            'total_tokens',
            item['input_tokens']
            + item['output_tokens']
            + item['reasoning_tokens']
            + item['cache_read_tokens']
            + item['cache_write_tokens'],
        )
        != (
            item['input_tokens']
            + item['output_tokens']
            + item['reasoning_tokens']
            + item['cache_read_tokens']
            + item['cache_write_tokens']
        )
        for item in step_tokens
    ):
        raise OpenCodeBlocked(f'{label} step total_tokens disagrees with disjoint five-category sum')
    return {
        'input_tokens': in_tok,
        'output_tokens': out_tok,
        'reasoning_tokens': reas_tok,
        'cache_read_tokens': c_read,
        'cache_write_tokens': c_write,
        'total_tokens': total_tok,
        'total_steps': len(step_tokens),
    }


def extract_partial_usage(run_stdout: str = '', export_stdout: str = '') -> dict[str, int | str]:
    """Extract partial non-negative token counters on failure; any missing or unverified field stays 'unknown'."""
    partial: dict[str, int | str] = dict(UNKNOWN_USAGE)
    observed: list[dict[str, object]] = []
    if isinstance(run_stdout, str) and run_stdout.strip():
        for raw_line in run_stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                ev = gate.strict_json(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get('type') == 'step_finish':
                part = ev.get('part')
                if isinstance(part, dict) and isinstance(part.get('tokens'), dict):
                    observed.append(part['tokens'])
    if not observed and isinstance(export_stdout, str) and export_stdout.strip():
        try:
            exp = gate.strict_json(export_stdout)
        except ValueError:
            exp = None
        if isinstance(exp, dict) and isinstance(exp.get('messages'), list):
            for msg in exp['messages']:
                if isinstance(msg, dict) and isinstance(msg.get('info'), dict) and msg['info'].get('role') == 'assistant':
                    if isinstance(msg['info'].get('tokens'), dict):
                        observed.append(msg['info']['tokens'])
    if len(observed) == 1:
        tok = observed[0]
        if _nonneg_int(tok.get('input')) and tok['input'] > 0:
            partial['input_tokens'] = tok['input']
        if _nonneg_int(tok.get('output')) and tok['output'] > 0:
            partial['output_tokens'] = tok['output']
        if _nonneg_int(tok.get('reasoning')):
            partial['reasoning_tokens'] = tok['reasoning']
        cache = tok.get('cache')
        if isinstance(cache, dict):
            if _nonneg_int(cache.get('read')):
                partial['cache_read_tokens'] = cache['read']
            if _nonneg_int(cache.get('write')):
                partial['cache_write_tokens'] = cache['write']
    return partial


def _check_model_fields(
    obj: dict[str, object],
    *,
    expected_provider_id: str,
    expected_model_id: str,
    full_model: str,
    label: str,
    require_present: bool,
) -> bool:
    found = False
    has_prov = 'providerID' in obj or 'provider_id' in obj
    has_mod = 'modelID' in obj or 'model_id' in obj
    if has_prov or has_mod:
        prov_val = obj.get('providerID', obj.get('provider_id'))
        mod_val = obj.get('modelID', obj.get('model_id'))
        if prov_val != expected_provider_id or mod_val != expected_model_id:
            raise OpenCodeBlocked(
                f'{label} model mismatch: expected {full_model!r}, got provider={prov_val!r} model={mod_val!r}'
            )
        found = True
    if 'model' in obj and obj.get('model') is not None:
        m_val = obj.get('model')
        if isinstance(m_val, dict):
            if m_val.get('providerID') != expected_provider_id or m_val.get('modelID') != expected_model_id:
                raise OpenCodeBlocked(f'{label}.model mismatch: expected {full_model!r}, got {m_val!r}')
            found = True
        elif isinstance(m_val, str):
            if m_val != full_model:
                raise OpenCodeBlocked(f'{label}.model mismatch: expected {full_model!r}, got {m_val!r}')
            found = True
        else:
            raise OpenCodeBlocked(f'{label}.model has malformed type')
    if require_present and not found:
        raise OpenCodeBlocked(f'{label} is missing required providerID/modelID proof')
    return found


def _has_auxiliary_generation_marker(obj: dict[str, object]) -> bool:
    if obj.get('small') is True or obj.get('isSmall') is True:
        return True
    for field in ('agent', 'mode'):
        val = obj.get(field)
        if isinstance(val, str) and val.strip().lower() in DISABLED_OPENCODE_AGENTS:
            return True
    summary_val = obj.get('summary')
    if summary_val is True or (isinstance(summary_val, dict) and bool(summary_val)):
        return True
    return False


def validate_native_logs(
    xdg_root: Path,
    *,
    expected_provider_id: str,
    expected_model_id: str,
    expected_steps: int | None = None,
    save_log_to: Path | None = None,
) -> dict[str, object]:
    """Inspect isolated OpenCode native log files for auxiliary small/title/summary LLM calls omitted from step counters."""
    log_dir = Path(xdg_root) / 'data/opencode/log'
    if not os.path.lexists(log_dir):
        return {'log_files': 0, 'llm_stream_calls': 0, 'auxiliary_generation_detected': False}
    if os.path.islink(log_dir) or not log_dir.is_dir():
        raise OpenCodeBlocked('OpenCode native log directory is a symlink or non-directory')

    chunks: list[str] = []
    log_files = 0
    for entry in sorted(log_dir.iterdir()):
        log_files += 1
        raw_bytes = read_regular_nofollow(entry)
        if len(raw_bytes) > MAX_EXPORT_BYTES:
            raise OpenCodeBlocked('OpenCode native log exceeds bounded size')
        chunks.append(raw_bytes.decode('utf-8', errors='replace'))

    combined = '\n'.join(chunks)
    if save_log_to is not None and log_files > 0:
        save_log_to.write_text(gate.redact(combined), encoding='utf-8')
    if gate.contains_secret(combined):
        raise OpenCodeBlocked('possible credential in OpenCode native log')
    if AUXILIARY_LOG_RE.search(combined):
        raise OpenCodeBlocked(
            'OpenCode native log shows auxiliary title/summary/small-model generation omitted from export token counters'
        )

    llm_stream_calls = 0
    for raw_line in combined.splitlines():
        if not LLM_SERVICE_LOG_RE.search(raw_line):
            continue
        prov_m = LOG_PROVIDER_RE.search(raw_line)
        mod_m = LOG_MODEL_RE.search(raw_line)
        if prov_m and prov_m.group(1) != expected_provider_id:
            raise OpenCodeBlocked(
                f'OpenCode native log service=llm providerID {prov_m.group(1)!r} mismatches {expected_provider_id!r}'
            )
        if mod_m and mod_m.group(1) != expected_model_id:
            raise OpenCodeBlocked(
                f'OpenCode native log service=llm modelID {mod_m.group(1)!r} mismatches {expected_model_id!r}'
            )
        if LLM_STREAM_CALL_RE.search(raw_line):
            llm_stream_calls += 1

    if expected_steps is not None and llm_stream_calls > expected_steps:
        raise OpenCodeBlocked(
            f'OpenCode native log records {llm_stream_calls} LLM stream calls for {expected_steps} exported step(s); '
            'native usage omits an actual call'
        )
    return {
        'log_files': log_files,
        'llm_stream_calls': llm_stream_calls,
        'auxiliary_generation_detected': False,
    }


def validate_run_events(stdout_text: str, expected_model: object = None) -> tuple[dict[str, object], str]:
    """Validate OpenCode1 `run --format json` JSONL event stream."""
    full_model, provider_id, model_id = resolve_opencode_model(expected_model)
    if not isinstance(stdout_text, str) or not stdout_text.strip():
        raise OpenCodeBlocked('OpenCode run stdout is empty')
    if '\x1b' in stdout_text:
        raise OpenCodeBlocked('OpenCode run stdout contains unexpected terminal control sequence')

    events: list[dict[str, object]] = []
    for idx, raw_line in enumerate(stdout_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = gate.strict_json(line)
        except ValueError as exc:
            raise OpenCodeBlocked(f'malformed JSON or duplicate keys in OpenCode run event line {idx}') from exc
        if not isinstance(ev, dict):
            raise OpenCodeBlocked(f'OpenCode run event line {idx} is not a JSON object')
        events.append(ev)

    if not events:
        raise OpenCodeBlocked('OpenCode run produced no JSON events')

    session_id: str | None = None
    step_starts = 0
    step_tokens: list[dict[str, int]] = []
    finish_reasons: list[str] = []
    text_chunks: list[str] = []

    for idx, ev in enumerate(events, start=1):
        ev_type = ev.get('type')
        if ev_type not in ALLOWED_STREAM_EVENT_TYPES:
            raise OpenCodeBlocked(f'forbidden or error OpenCode run event type at event {idx}: {ev_type!r}')
        if ev.get('error') is not None or ev.get('tool_denied') is True or ev.get('denied') is True:
            raise OpenCodeBlocked(f'OpenCode run event {idx} reports an error or denial')
        if set(ev.keys()) & FORBIDDEN_PART_KEYS:
            raise OpenCodeBlocked(f'OpenCode run event {idx} contains forbidden tool/patch fields')
        if _has_auxiliary_generation_marker(ev):
            raise OpenCodeBlocked(
                f'OpenCode run event {idx} reports auxiliary title/summary/small-model generation omitted from main usage'
            )
        sid = ev.get('sessionID')
        if not isinstance(sid, str) or not sid.strip() or sid != sid.strip():
            raise OpenCodeBlocked(f'OpenCode run event {idx} missing valid sessionID')
        if session_id is None:
            session_id = sid
        elif sid != session_id:
            raise OpenCodeBlocked(f'OpenCode run event {idx} sessionID mismatch: {sid!r} != {session_id!r}')

        _check_model_fields(
            ev,
            expected_provider_id=provider_id,
            expected_model_id=model_id,
            full_model=full_model,
            label=f'run event {idx}',
            require_present=False,
        )

        part = ev.get('part')
        if not isinstance(part, dict):
            raise OpenCodeBlocked(f'OpenCode run event {idx} missing part object')
        if part.get('error') is not None or part.get('tool_denied') is True or part.get('denied') is True:
            raise OpenCodeBlocked(f'OpenCode run event {idx} part reports an error or denial')
        if set(part.keys()) & FORBIDDEN_PART_KEYS:
            raise OpenCodeBlocked(f'OpenCode run event {idx} part contains forbidden tool/patch fields')
        if _has_auxiliary_generation_marker(part):
            raise OpenCodeBlocked(
                f'OpenCode run event {idx} part reports auxiliary title/summary/small-model generation omitted from main usage'
            )
        if 'sessionID' in part and part.get('sessionID') != session_id:
            raise OpenCodeBlocked(f'OpenCode run event {idx} part.sessionID mismatches event sessionID')

        _check_model_fields(
            part,
            expected_provider_id=provider_id,
            expected_model_id=model_id,
            full_model=full_model,
            label=f'run event {idx} part',
            require_present=False,
        )

        ptype = part.get('type')
        if ev_type == 'step_start':
            if ptype not in ('step-start', 'step_start'):
                raise OpenCodeBlocked(f'OpenCode run step_start event {idx} has unexpected part.type {ptype!r}')
            step_starts += 1
        elif ev_type == 'text':
            if ptype != 'text' or not isinstance(part.get('text'), str):
                raise OpenCodeBlocked(f'OpenCode run text event {idx} missing string text')
            text_chunks.append(part['text'])
        elif ev_type == 'reasoning':
            if ptype != 'reasoning' or not isinstance(part.get('text', ''), str):
                raise OpenCodeBlocked(f'OpenCode run reasoning event {idx} malformed')
            if has_provider_error(part.get('text', '')):
                raise OpenCodeBlocked(f'OpenCode run reasoning event {idx} reports provider error/denial/quota')
        elif ev_type == 'step_finish':
            if ptype not in ('step-finish', 'step_finish'):
                raise OpenCodeBlocked(f'OpenCode run step_finish event {idx} has unexpected part.type {ptype!r}')
            reason = part.get('reason')
            if not isinstance(reason, str) or reason in OPENCODE_BAD_FINISH_REASONS or reason not in OPENCODE_FINISH_REASONS:
                raise OpenCodeBlocked(f'OpenCode run step_finish {idx} has non-stop reason: {reason!r}')
            finish_reasons.append(reason)
            step_tokens.append(_parse_tokens_block(part.get('tokens'), label=f'run event {idx} step_finish.tokens'))

    if events[0].get('type') != 'step_start' or events[-1].get('type') != 'step_finish':
        raise OpenCodeBlocked('OpenCode run stream must begin with step_start and end with step_finish')
    if step_starts <= 0 or len(step_tokens) != step_starts:
        raise OpenCodeBlocked('OpenCode run stream has unbalanced step_start and step_finish events')

    final_text = ''.join(text_chunks)
    if not final_text.strip():
        raise OpenCodeBlocked('OpenCode run stream produced empty text response')

    usage = _aggregate_step_tokens(step_tokens, label='OpenCode run stream')
    return {
        'session_id': session_id,
        'generation_steps': len(step_tokens),
        'finish_reason': finish_reasons[-1],
        'usage': usage,
        'event_count': len(events),
    }, final_text


def validate_export(
    export_text: str,
    *,
    expected_session_id: str,
    expected_model: object = None,
    expected_title: str | None = None,
) -> tuple[dict[str, object], str]:
    """Validate OpenCode1 `export <sessionID>` JSON transcript against session_id and exact provider/model."""
    full_model, provider_id, model_id = resolve_opencode_model(expected_model)
    if not isinstance(expected_session_id, str) or not expected_session_id.strip():
        raise OpenCodeBlocked('OpenCode export validation requires expected_session_id')
    if not isinstance(export_text, str) or not export_text.strip():
        raise OpenCodeBlocked('OpenCode export output is empty')
    if len(export_text.encode('utf-8', errors='replace')) > MAX_EXPORT_BYTES:
        raise OpenCodeBlocked('OpenCode export exceeds bounded size')
    try:
        export_obj = gate.strict_json(export_text)
    except ValueError as exc:
        raise OpenCodeBlocked('malformed OpenCode export JSON (duplicate keys or invalid JSON)') from exc
    if not isinstance(export_obj, dict):
        raise OpenCodeBlocked('OpenCode export must be a JSON object')

    info = export_obj.get('info')
    if not isinstance(info, dict) or info.get('id') != expected_session_id:
        raise OpenCodeBlocked('OpenCode export info.id does not match run sessionID')
    if info.get('error') is not None:
        raise OpenCodeBlocked('OpenCode export session info reports an error')
    if _has_auxiliary_generation_marker(info):
        raise OpenCodeBlocked('OpenCode export session info reports auxiliary title/summary/small-model generation')
    if 'title' in info and info.get('title') is not None:
        if not isinstance(info.get('title'), str) or not info['title'].strip():
            raise OpenCodeBlocked('OpenCode export session info.title must be a non-empty string when present')
        if expected_title is not None and info['title'] != expected_title:
            raise OpenCodeBlocked(
                f'OpenCode export session info.title {info["title"]!r} does not match deterministic --title {expected_title!r} '
                '(auxiliary title generation omitted from main usage)'
            )

    messages = export_obj.get('messages')
    if not isinstance(messages, list) or not messages:
        raise OpenCodeBlocked('OpenCode export messages missing or empty')

    user_count = 0
    assistant_messages: list[dict[str, object]] = []
    assistant_tokens: list[dict[str, int]] = []
    assistant_text_chunks: list[str] = []
    finish_reasons: list[str] = []

    for m_idx, msg in enumerate(messages, start=1):
        if not isinstance(msg, dict):
            raise OpenCodeBlocked(f'OpenCode export message {m_idx} malformed')
        m_info = msg.get('info')
        m_parts = msg.get('parts')
        if not isinstance(m_info, dict) or not isinstance(m_parts, list) or not m_parts:
            raise OpenCodeBlocked(f'OpenCode export message {m_idx} missing info or parts')
        if m_info.get('sessionID') != expected_session_id:
            raise OpenCodeBlocked(f'OpenCode export message {m_idx} sessionID mismatch')
        if m_info.get('error') is not None:
            raise OpenCodeBlocked(f'OpenCode export message {m_idx} reports an error')
        if _has_auxiliary_generation_marker(m_info):
            raise OpenCodeBlocked(
                f'OpenCode export message {m_idx} reports auxiliary title/summary/small-model generation omitted from main usage'
            )

        role = m_info.get('role')
        if role not in ('user', 'assistant'):
            raise OpenCodeBlocked(f'OpenCode export message {m_idx} has unexpected role {role!r}')

        if role == 'user':
            if assistant_messages:
                raise OpenCodeBlocked('unexpected post-generation user message in OpenCode export')
            user_count += 1
            _check_model_fields(
                m_info,
                expected_provider_id=provider_id,
                expected_model_id=model_id,
                full_model=full_model,
                label=f'export user message {m_idx}',
                require_present=False,
            )
            for p_idx, part in enumerate(m_parts, start=1):
                if not isinstance(part, dict) or part.get('type') != 'text':
                    raise OpenCodeBlocked(f'OpenCode export user message {m_idx} part {p_idx} must be text')
                if 'sessionID' in part and part.get('sessionID') != expected_session_id:
                    raise OpenCodeBlocked(f'OpenCode export user message {m_idx} part {p_idx} sessionID mismatch')
                if part.get('error') is not None or (set(part.keys()) & FORBIDDEN_PART_KEYS) or _has_auxiliary_generation_marker(part):
                    raise OpenCodeBlocked(f'OpenCode export user message {m_idx} part {p_idx} has error or tool/auxiliary field')
        else:
            _check_model_fields(
                m_info,
                expected_provider_id=provider_id,
                expected_model_id=model_id,
                full_model=full_model,
                label=f'export assistant message {m_idx}',
                require_present=True,
            )
            finish = m_info.get('finish')
            if not isinstance(finish, str) or finish in OPENCODE_BAD_FINISH_REASONS or finish not in OPENCODE_FINISH_REASONS:
                raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} has invalid finish reason: {finish!r}')
            finish_reasons.append(finish)

            time_obj = m_info.get('time')
            if isinstance(time_obj, dict) and 'completed' in time_obj:
                comp = time_obj.get('completed')
                if type(comp) not in (int, float) or comp <= 0:
                    raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} lacks completed timestamp')

            msg_tok = _parse_tokens_block(m_info.get('tokens'), label=f'export assistant message {m_idx} info.tokens')
            part_step_tokens: list[dict[str, int]] = []
            msg_text_parts: list[str] = []

            for p_idx, part in enumerate(m_parts, start=1):
                if not isinstance(part, dict):
                    raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} part {p_idx} malformed')
                if 'sessionID' in part and part.get('sessionID') != expected_session_id:
                    raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} part {p_idx} sessionID mismatch')
                if part.get('error') is not None or part.get('tool_denied') is True or part.get('denied') is True:
                    raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} part {p_idx} reports error/denial')
                if set(part.keys()) & FORBIDDEN_PART_KEYS:
                    raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} part {p_idx} contains tool/patch fields')
                if _has_auxiliary_generation_marker(part):
                    raise OpenCodeBlocked(
                        f'OpenCode export assistant message {m_idx} part {p_idx} reports auxiliary title/summary/small-model generation'
                    )
                _check_model_fields(
                    part,
                    expected_provider_id=provider_id,
                    expected_model_id=model_id,
                    full_model=full_model,
                    label=f'export assistant message {m_idx} part {p_idx}',
                    require_present=False,
                )
                ptype = part.get('type')
                if ptype not in ALLOWED_ASSISTANT_PART_TYPES:
                    raise OpenCodeBlocked(
                        f'unexpected or tool part type in OpenCode export assistant message {m_idx}: {ptype!r}'
                    )
                if ptype == 'text':
                    if not isinstance(part.get('text'), str):
                        raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} text part {p_idx} non-string')
                    msg_text_parts.append(part['text'])
                elif ptype == 'reasoning':
                    if not isinstance(part.get('text', ''), str) or has_provider_error(part.get('text', '')):
                        raise OpenCodeBlocked(f'OpenCode export assistant message {m_idx} reasoning part {p_idx} invalid')
                elif ptype in ('step-finish', 'step_finish'):
                    p_reason = part.get('reason')
                    if (
                        not isinstance(p_reason, str)
                        or p_reason in OPENCODE_BAD_FINISH_REASONS
                        or p_reason not in OPENCODE_FINISH_REASONS
                    ):
                        raise OpenCodeBlocked(
                            f'OpenCode export assistant message {m_idx} step-finish part {p_idx} reason invalid: {p_reason!r}'
                        )
                    part_step_tokens.append(
                        _parse_tokens_block(
                            part.get('tokens'),
                            label=f'export assistant message {m_idx} step-finish part {p_idx} tokens',
                        )
                    )

            if part_step_tokens:
                summed_parts = _aggregate_step_tokens(
                    part_step_tokens,
                    label=f'export assistant message {m_idx} step-finish parts',
                )
                for key in (
                    'input_tokens',
                    'output_tokens',
                    'reasoning_tokens',
                    'cache_read_tokens',
                    'cache_write_tokens',
                    'total_tokens',
                ):
                    if summed_parts[key] != msg_tok[key]:
                        raise OpenCodeBlocked(
                            f'OpenCode export assistant message {m_idx} info.tokens disagrees with step-finish part tokens'
                        )
                assistant_tokens.extend(part_step_tokens)
            else:
                assistant_tokens.append(msg_tok)

            msg_text = ''.join(msg_text_parts)
            if m_idx < len(messages) and has_provider_error(msg_text):
                raise OpenCodeBlocked('intermediate OpenCode assistant message reports provider error/denial/quota')
            assistant_text_chunks.append(msg_text)
            assistant_messages.append(msg)

    if user_count <= 0 or not assistant_messages or messages[-1].get('info', {}).get('role') != 'assistant':
        raise OpenCodeBlocked('OpenCode export must contain user prompt and end with an assistant message')

    export_final_text = ''.join(assistant_text_chunks)
    if not export_final_text.strip():
        raise OpenCodeBlocked('OpenCode export assistant response text is empty')

    usage = _aggregate_step_tokens(assistant_tokens, label='OpenCode export session')
    proof = {
        'provider': 'opencode',
        'session_id': expected_session_id,
        'requested_model': full_model,
        'actual_generation_model': full_model,
        'provider_id': provider_id,
        'model_id': model_id,
        'generation_steps': usage['total_steps'],
        'finish_reason': finish_reasons[-1],
        'tool_calls': 0,
        'tool_denied': False,
        'usage': usage,
    }
    return proof, export_final_text


def run_opencode_attempt(
    *,
    workspace: Path,
    attempt_dir: Path,
    contract: str,
    scope_files: list[str],
    verify_cmds: list[str],
    timeout_seconds: int,
    model: object = None,
    requested_model: object = None,
    title: str | None = None,
    executable_override: str | None = None,
    feedback: str | None = None,
    open_questions: list[str] | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
    context_mode: str | None = None,
) -> dict[str, object]:
    """Run one fresh isolated tool-free OpenCode1 generation and export; apply bytes only after all proof checks pass."""
    attempt_dir = Path(attempt_dir).resolve()
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    effective_model_arg = requested_model if requested_model is not None else model
    raw_requested = os.environ.get(OPENCODE_MODEL_ENV) if effective_model_arg is None else effective_model_arg
    session_title = f'{DEFAULT_RUN_TITLE}-attempt-{int(attempt)}' if title is None else title
    base: dict[str, object] = {
        'ok': False,
        'blocked': True,
        'reason': None,
        'provider': 'opencode',
        'requested_model': raw_requested if isinstance(raw_requested, str) and raw_requested.strip() else None,
        'model': None,
        'provider_id': None,
        'model_id': None,
        'effort': None,
        'effort_verification': EFFORT_NOTE,
        'permission_mode': None,
        'session_id': None,
        'session_title': session_title if isinstance(session_title, str) else None,
        'conversation_id': None,
        'exit_code': None,
        'export_exit_code': None,
        'proof_status': 'UNVERIFIED',
        'tool_calls': None,
        'tool_denied': False,
        'stream_matches_export': False,
        'finish_reason': None,
        'generation_steps': None,
        'response': '',
        'files_written': [],
        'written_hashes': {},
        'usage': dict(UNKNOWN_USAGE),
        'usage_basis': USAGE_BASIS,
        'usage_updates_observed': 0,
        'duration_seconds': 0.0,
        'partial_mutation': False,
        'context_mode': None,
        'prompt_chars': None,
        'config_verified': False,
        'isolated_xdg': False,
        'pure_mode': False,
        'prelaunch_config_sha256': None,
        'postrun_config_sha256': None,
        'protected_config_equal': False,
        'config_proof_status': 'UNVERIFIED',
        'config_proof': _build_config_proof(
            status='UNVERIFIED',
            prelaunch_sha256=None,
            postrun_sha256=None,
            protected_equal=False,
        ),
    }

    def blocked(reason: str, **extra: object) -> dict[str, object]:
        record = {**base, **extra, 'reason': reason}
        gate.save(attempt_dir / 'opencode-result.json', record)
        return record

    try:
        if model is not None and requested_model is not None and model != requested_model:
            return blocked('conflicting model and requested_model arguments supplied to OpenCode worker')
        full_model, provider_id, model_id = resolve_opencode_model(effective_model_arg)
        base['requested_model'] = full_model
        mode = resolve_context_mode(context_mode)
        base['context_mode'] = mode
        workspace = Path(workspace).resolve()
        session_dir = attempt_dir / 'opencode-session'
        xdg_root = attempt_dir / 'opencode-xdg'
        if (
            devin._is_within(session_dir, workspace)
            or devin._is_within(workspace, session_dir)
            or devin._is_within(xdg_root, workspace)
            or devin._is_within(workspace, xdg_root)
        ):
            return blocked('OpenCode session cwd and XDG directories must be outside the target workspace')
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
        config = build_opencode_config(full_model)
        problems = validate_opencode_config(config, expected_model=full_model)
        if problems:
            return blocked('; '.join(problems))
        session_dir.mkdir(mode=0o700, exist_ok=False)
        xdg_root.mkdir(mode=0o700, exist_ok=False)
        config_path = xdg_root / 'config/opencode/opencode.json'
        isolated_env, isolation_summary = build_isolated_env(
            os.environ,
            attempt_dir=attempt_dir,
            xdg_root=xdg_root,
            config_path=config_path,
            config_obj=config,
        )
        prelaunch_config_path = attempt_dir / PRELAUNCH_CONFIG_FILENAME
        postrun_config_path = attempt_dir / POSTRUN_CONFIG_FILENAME
        if os.path.lexists(prelaunch_config_path) or os.path.lexists(postrun_config_path):
            return blocked('OpenCode config evidence artifact already exists')
        config_bytes = (json.dumps(config, indent=2) + '\n').encode('utf-8')
        config_path.write_bytes(config_bytes)
        prelaunch_config_path.write_bytes(config_bytes)
        os.chmod(prelaunch_config_path, 0o400)
        prelaunch_bytes, prelaunch_disk_cfg = read_opencode_config_file(
            prelaunch_config_path, label='OpenCode prelaunch config'
        )
        disk_bytes, disk_cfg = read_opencode_config_file(config_path, label='OpenCode prelaunch config')
        if (
            prelaunch_bytes != config_bytes
            or disk_bytes != config_bytes
            or not devin._strict_equal(prelaunch_disk_cfg, config)
            or not devin._strict_equal(disk_cfg, config)
        ):
            return blocked('OpenCode prelaunch config mismatch on disk')
        disk_problems = validate_opencode_config(disk_cfg, expected_model=full_model)
        if disk_problems:
            return blocked('; '.join(disk_problems))
        prelaunch_sha256 = sha256_bytes(prelaunch_bytes)
        base['prelaunch_config_sha256'] = prelaunch_sha256
        base['config_proof'] = _build_config_proof(
            status='UNVERIFIED',
            prelaunch_sha256=prelaunch_sha256,
            postrun_sha256=None,
            protected_equal=False,
        )
    except (OpenCodeBlocked, devin.DevinBlocked, OSError) as exc:
        return blocked(
            str(exc) if isinstance(exc, (OpenCodeBlocked, devin.DevinBlocked)) else 'cannot prepare isolated OpenCode session'
        )

    (attempt_dir / 'opencode-prompt.txt').write_text(prompt, encoding='utf-8')
    try:
        executable = resolve_opencode_executable(executable_override)
    except OSError:
        gate.save(
            attempt_dir / 'opencode-invocation.json',
            {'requested_executable': executable_override or 'opencode', 'state': 'UNAVAILABLE', 'model': full_model},
        )
        return blocked('opencode executable unavailable')

    try:
        run_argv = build_run_argv(executable, full_model, prompt, title=session_title)
    except OpenCodeBlocked as exc:
        return blocked(str(exc))

    invocation: dict[str, object] = {
        'run_argv': [*run_argv[:-1], f'<prompt opencode-prompt.txt sha256={sha256_bytes(prompt.encode())}>'],
        'export_argv': None,
        'cwd': str(session_dir),
        'isolation': isolation_summary,
        'state': 'STARTING',
        'model': full_model,
        'title': session_title,
        'fresh_session': True,
        'context_mode': mode,
        'prompt_chars': len(prompt),
    }
    gate.save(attempt_dir / 'opencode-invocation.json', invocation)

    t0 = time.monotonic()
    try:
        run_res = subprocess.run(
            run_argv,
            cwd=session_dir,
            env=isolated_env,
            text=True,
            capture_output=True,
            timeout=int(timeout_seconds) + 20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - t0, 2)
        out_txt = devin._text(exc.stdout)
        err_txt = devin._text(exc.stderr)
        (attempt_dir / 'opencode.stdout.jsonl').write_text(gate.redact(out_txt), encoding='utf-8')
        (attempt_dir / 'opencode.stderr.txt').write_text(gate.redact(err_txt), encoding='utf-8')
        gate.save(attempt_dir / 'opencode-invocation.json', {**invocation, 'state': 'TIMEOUT', 'exit_code': None})
        return blocked(
            'OpenCode worker timed out',
            duration_seconds=duration,
            usage=extract_partial_usage(out_txt, ''),
        )
    except OSError:
        return blocked('OpenCode CLI failed to start')

    run_stdout = run_res.stdout or ''
    run_stderr = run_res.stderr or ''
    (attempt_dir / 'opencode.stdout.jsonl').write_text(gate.redact(run_stdout), encoding='utf-8')
    (attempt_dir / 'opencode.stderr.txt').write_text(gate.redact(run_stderr), encoding='utf-8')

    reasons: list[str] = []
    if run_res.returncode != 0:
        reasons.append(f'OpenCode run exited with code {run_res.returncode}')
    if has_provider_error(run_stderr):
        reasons.append('OpenCode run stderr reports denial/quota/auth/timeout/unsupported model')
    if gate.contains_secret(run_stdout) or gate.contains_secret(run_stderr):
        reasons.append('possible credential in OpenCode run stdout/stderr')

    stream_info: dict[str, object] | None = None
    stream_text = ''
    try:
        stream_info, stream_text = validate_run_events(run_stdout, expected_model=full_model)
    except OpenCodeBlocked as exc:
        reasons.append(str(exc))

    export_stdout = ''
    export_stderr = ''
    export_exit_code: int | None = None
    if stream_info is not None and not reasons:
        session_id = str(stream_info['session_id'])
        try:
            export_argv = build_export_argv(executable, session_id)
            invocation['export_argv'] = export_argv
            export_res = subprocess.run(
                export_argv,
                cwd=session_dir,
                env=isolated_env,
                text=True,
                capture_output=True,
                timeout=min(int(timeout_seconds) + 20, 120),
                check=False,
            )
            export_exit_code = export_res.returncode
            export_stdout = export_res.stdout or ''
            export_stderr = export_res.stderr or ''
            (attempt_dir / 'opencode-export.json').write_text(gate.redact(export_stdout), encoding='utf-8')
            (attempt_dir / 'opencode-export.stderr.txt').write_text(gate.redact(export_stderr), encoding='utf-8')
            if export_exit_code != 0:
                reasons.append(f'OpenCode export exited with code {export_exit_code}')
            if has_provider_error(export_stderr):
                reasons.append('OpenCode export stderr reports denial/quota/auth/timeout/unsupported model')
            if gate.contains_secret(export_stdout) or gate.contains_secret(export_stderr):
                reasons.append('possible credential in OpenCode export output')
        except subprocess.TimeoutExpired as exc:
            export_stdout = devin._text(exc.stdout)
            export_stderr = devin._text(exc.stderr)
            (attempt_dir / 'opencode-export.json').write_text(gate.redact(export_stdout), encoding='utf-8')
            (attempt_dir / 'opencode-export.stderr.txt').write_text(gate.redact(export_stderr), encoding='utf-8')
            reasons.append('OpenCode export timed out')
        except (OpenCodeBlocked, OSError) as exc:
            reasons.append(str(exc) if isinstance(exc, OpenCodeBlocked) else 'OpenCode export failed to start')

    duration = round(time.monotonic() - t0, 2)
    gate.save(
        attempt_dir / 'opencode-invocation.json',
        {
            **invocation,
            'state': 'FINISHED',
            'exit_code': run_res.returncode,
            'export_exit_code': export_exit_code,
            'duration_seconds': duration,
        },
    )

    postrun_sha256: str | None = None
    protected_equal = False
    config_proof_status = 'BLOCKED'
    try:
        postrun_bytes, postrun_cfg = read_opencode_config_file(
            config_path, label='OpenCode post-run config', save_raw_to=postrun_config_path
        )
        postrun_sha256 = sha256_bytes(postrun_bytes)
        pre_verify_bytes, pre_verify_cfg = read_opencode_config_file(
            prelaunch_config_path, label='OpenCode prelaunch config evidence'
        )
        if sha256_bytes(pre_verify_bytes) != prelaunch_sha256 or not devin._strict_equal(pre_verify_cfg, prelaunch_disk_cfg):
            raise OpenCodeBlocked('OpenCode prelaunch config evidence was modified')
        protected_equal = devin._strict_equal(postrun_cfg, prelaunch_disk_cfg) and not validate_opencode_config(
            postrun_cfg, expected_model=full_model
        )
        post_problems = validate_postrun_opencode_config(
            postrun_cfg, expected_model=full_model, prelaunch_config=prelaunch_disk_cfg
        )
        if post_problems:
            raise OpenCodeBlocked('; '.join(post_problems))
        config_proof_status = 'VERIFIED'
    except (OpenCodeBlocked, devin.DevinBlocked, OSError) as exc:
        if (
            postrun_sha256 is None
            and os.path.lexists(postrun_config_path)
            and not os.path.islink(postrun_config_path)
            and postrun_config_path.is_file()
        ):
            try:
                postrun_sha256 = sha256_bytes(postrun_config_path.read_bytes())
            except OSError:
                postrun_sha256 = None
        reasons.append(
            str(exc)
            if isinstance(exc, (OpenCodeBlocked, devin.DevinBlocked))
            else 'OpenCode post-run config unreadable'
        )

    config_proof = _build_config_proof(
        status=config_proof_status,
        prelaunch_sha256=prelaunch_sha256,
        postrun_sha256=postrun_sha256,
        protected_equal=protected_equal,
    )

    export_proof: dict[str, object] | None = None
    export_text = ''
    if stream_info is not None and export_exit_code == 0 and not reasons:
        try:
            export_proof, export_text = validate_export(
                export_stdout,
                expected_session_id=str(stream_info['session_id']),
                expected_model=full_model,
                expected_title=session_title,
            )
        except OpenCodeBlocked as exc:
            reasons.append(str(exc))

    unaccounted_call = any(
        'auxiliary' in r or 'omits an actual call' in r or 'omitted from' in r for r in reasons
    )
    try:
        validate_native_logs(
            xdg_root,
            expected_provider_id=provider_id,
            expected_model_id=model_id,
            expected_steps=int(stream_info['generation_steps']) if stream_info is not None else None,
            save_log_to=attempt_dir / NATIVE_LOG_FILENAME,
        )
    except (OpenCodeBlocked, devin.DevinBlocked, OSError) as exc:
        unaccounted_call = True
        reasons.append(
            str(exc)
            if isinstance(exc, (OpenCodeBlocked, devin.DevinBlocked))
            else 'OpenCode native log unreadable'
        )

    common: dict[str, object] = {
        'exit_code': run_res.returncode,
        'export_exit_code': export_exit_code,
        'duration_seconds': duration,
        'config_verified': config_proof_status == 'VERIFIED',
        'isolated_xdg': True,
        'pure_mode': True,
        'prelaunch_config_sha256': prelaunch_sha256,
        'postrun_config_sha256': postrun_sha256,
        'protected_config_equal': protected_equal,
        'config_proof_status': config_proof_status,
        'config_proof': config_proof,
        'raw_stdout_sha256': sha256_bytes((attempt_dir / 'opencode.stdout.jsonl').read_bytes())
        if (attempt_dir / 'opencode.stdout.jsonl').is_file()
        else None,
        'export_sha256': sha256_bytes((attempt_dir / 'opencode-export.json').read_bytes())
        if (attempt_dir / 'opencode-export.json').is_file()
        else None,
        'native_log_sha256': sha256_bytes((attempt_dir / NATIVE_LOG_FILENAME).read_bytes())
        if (attempt_dir / NATIVE_LOG_FILENAME).is_file()
        else None,
    }

    if stream_info is not None and export_proof is not None and not reasons:
        if stream_text != export_text:
            reasons.append('OpenCode run stream text does not match session export assistant text')
        elif stream_info['usage'] != export_proof['usage']:
            reasons.append('OpenCode run stream step_finish tokens disagree with session export tokens')
        else:
            common.update(
                session_id=export_proof['session_id'],
                conversation_id=export_proof['session_id'],
                usage=export_proof['usage'],
                usage_updates_observed=int(export_proof['generation_steps']),
                generation_steps=export_proof['generation_steps'],
                finish_reason=export_proof['finish_reason'],
                stream_matches_export=True,
            )
    else:
        common['usage'] = dict(UNKNOWN_USAGE) if unaccounted_call else extract_partial_usage(run_stdout, export_stdout)

    if reasons:
        return blocked('; '.join(reasons), **common)

    try:
        handoff, files = parse_generation(export_text, scope_files)
        preconditions = {str(s['path']): s['sha256'] for s in sources}
        written = apply_files(workspace, files, preconditions, recovery_dir=attempt_dir / 'recovery')
    except (OpenCodePartialMutation, devin.DevinPartialMutation) as exc:
        return blocked(str(exc), partial_mutation=True, **common)
    except (OpenCodeBlocked, devin.DevinBlocked) as exc:
        return blocked(str(exc), **common)
    except OSError:
        return blocked('atomic application failed; every replaced file was restored and verified', **common)

    record = {
        **base,
        **common,
        'ok': True,
        'blocked': False,
        'reason': None,
        'model': full_model,
        'provider_id': provider_id,
        'model_id': model_id,
        'proof_status': 'VERIFIED',
        'tool_calls': 0,
        'tool_denied': False,
        'response': handoff,
        'final_response_sha256': sha256_bytes(handoff.encode('utf-8')),
        'raw_response_sha256': sha256_bytes(export_text.encode('utf-8')),
        'files_written': sorted(written),
        'written_hashes': written,
    }
    gate.save(attempt_dir / 'opencode-result.json', record)
    return record
