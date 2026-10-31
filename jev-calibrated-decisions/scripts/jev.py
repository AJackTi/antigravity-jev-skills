#!/usr/bin/env python3
"""jev: calibrated, typed decisions from TypeSafe's Jev model, for agents.

Jev is a "System One" model. It does not generate text. You hand it a STATE
(text or JSON) and typed QUESTIONS, and it returns typed answers with
calibrated probabilities in ~250 ms for $0.042 per million input tokens.

    noul    yes/no        -> probability that the answer is yes (0..1)
    choice  pick one      -> chosen option + probability per option + confidence
    score   rate on rubric-> position on your ordered levels + confidence

Progressive disclosure:
    jev                 short help (this)
    jev guide           the agent playbook: when to use it, how to ask, how to read
    jev guide <topic>   depth on one topic (jev guide --list)
    jev examples        copy-paste examples for the common jobs
    jev docs            live index of the vendor docs

Zero dependencies. Importable as a library:
    sys.path.insert(0, "/path/to/dir/containing/jev.py")
    from jev import JevClient, noul, choice, score

Auth: --api-key, else $TYPESAFE_API_KEY, else ~/.config/typesafe/api_key (0600).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "1.1.0"
API_BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
DEFAULT_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
CONFIG_DIR = Path.home() / ".config" / "typesafe"
KEY_FILE = CONFIG_DIR / "api_key"
USAGE_FILE = CONFIG_DIR / "usage.jsonl"
CONFIG_FILE = CONFIG_DIR / "config.json"
PRICE_PER_MTOK_IN = 0.042  # USD per million input tokens (jev-1.13 list price); output tokens are free
RATE_LIMIT_RPM = 1200  # vendor-published, adjusts without notice
RATE_LIMIT_TPS = 250_000
DOCS = "https://docs.typesafe.ai"
MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10
STATE_CHAR_BUDGET = 120_000  # ~30k tokens; the state+longest-question budget is 32k tokens

# --------------------------------------------------------------------------- errors


class JevError(Exception):
    exit_code = 4  # API error after retries


class UsageError(JevError):
    exit_code = 2


class AuthError(JevError):
    exit_code = 3


class NetworkError(JevError):
    exit_code = 5


# --------------------------------------------------------------------------- auth


def resolve_key(explicit: str | None = None) -> tuple[str, str]:
    """Return (key, source). Order: explicit, $TYPESAFE_API_KEY, key file."""
    if explicit:
        return explicit.strip(), "--api-key"
    env = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env:
        return env, "$TYPESAFE_API_KEY"
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if key:
            return key, str(KEY_FILE)
    raise AuthError(
        "no API key. Run `jev auth set <key>` (stored 0600 in "
        f"{KEY_FILE}) or export TYPESAFE_API_KEY. Keys: https://console.typesafe.ai/keys"
    )


def mask(key: str) -> str:
    return key[:10] + "…" + key[-4:] if len(key) > 16 else "…"


# --------------------------------------------------------------------------- question builders


def noul(instructions, true=None, false=None) -> dict:
    """Yes/no question. Returns P(yes). Optional criteria say what yes and no mean."""
    q = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        crit = {}
        if true is not None:
            crit["true"] = true
        if false is not None:
            crit["false"] = false
        q["criteria"] = crit
    return q


def choice(instructions, options) -> dict:
    """Pick one option. `options` is a dict {name: description|None} or a list of names."""
    if isinstance(options, (list, tuple)):
        options = {str(o): None for o in options}
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions, levels) -> dict:
    """Rate on an ordered rubric. `levels` is an ordered list, low to high, 2..10 entries."""
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def box(instructions) -> dict:
    """One normalized image bounding box; pair with a presence Noul in code."""
    return {"type": "box", "instructions": instructions}


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise UsageError("questions must be a non-empty map of id -> question")
    for qid, q in questions.items():
        if not isinstance(q, dict) or "type" not in q:
            raise UsageError(f"question {qid!r}: must be an object with a `type`")
        t = q["type"]
        if t not in ("noul", "choice", "score", "box"):
            raise UsageError(f"question {qid!r}: type must be noul, choice, score or box, not {t!r}")
        if "instructions" not in q:
            raise UsageError(f"question {qid!r}: missing `instructions`")
        crit = q.get("criteria")
        if t == "box" and "criteria" in q:
            raise UsageError("Box questions do not accept criteria")
        if t == "choice":
            if not isinstance(crit, dict) or len(crit) < 2:
                raise UsageError(f"choice {qid!r}: criteria must map at least 2 options to descriptions (null allowed)")
            if len(crit) > MAX_CHOICE_OPTIONS:
                raise UsageError(f"choice {qid!r}: at most {MAX_CHOICE_OPTIONS} options")
        elif t == "score":
            if not isinstance(crit, list) or not 2 <= len(crit) <= MAX_SCORE_LEVELS:
                raise UsageError(f"score {qid!r}: criteria must be an ordered list of 2..{MAX_SCORE_LEVELS} level descriptions")
        elif t == "noul" and crit is not None:
            if not isinstance(crit, dict) or not set(crit) <= {"true", "false"}:
                raise UsageError(f"noul {qid!r}: criteria may only have `true` and `false` keys")


# --------------------------------------------------------------------------- client


def _string_values(value):
    """Asset references substitute string values, never object keys."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _string_values(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _string_values(child)


def validate_assets(assets, state, questions):
    """Validate the JSON/base64 envelope; the API validates decoded image limits."""
    if not isinstance(assets, dict):
        raise UsageError("assets must be an object")
    strings = list(_string_values([state, [q.get('instructions') for q in questions.values()],
                                    [q.get('criteria') for q in questions.values()]]))
    reference_bytes = 0
    for reference, asset in assets.items():
        if not isinstance(reference, str) or not reference.strip() or len(reference.encode('utf-8')) > 1024:
            raise UsageError("asset references must be nonempty strings of at most 1024 UTF-8 bytes")
        reference_bytes += len(reference.encode('utf-8'))
        if not any(reference in value for value in strings):
            raise UsageError("each asset must be referenced in state, instructions or criteria values")
        if not isinstance(asset, dict) or set(asset) - {'type', 'format', 'data_base64'}:
            raise UsageError("this client supports base64 image assets; use the official SDK for multipart")
        if asset.get('type', 'image') != 'image' or asset.get('format') not in ('png', 'jpg', 'jpeg'):
            raise UsageError("assets require image type and png/jpg/jpeg format")
        encoded = asset.get('data_base64')
        if not isinstance(encoded, str) or len(encoded) > ((16 * 1024 * 1024 + 2) // 3) * 4:
            raise UsageError("each image must have valid base64 and decode to at most 16 MiB")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise UsageError("invalid image base64; use standard alphabet/padding and no data: prefix") from None
        if not decoded or len(decoded) > 16 * 1024 * 1024:
            raise UsageError("decoded image is empty or exceeds 16 MiB")
        magic = b'\x89PNG\r\n\x1a\n' if asset['format'] == 'png' else b'\xff\xd8\xff'
        if not decoded.startswith(magic):
            raise UsageError("image bytes do not match the declared PNG/JPEG format")
    if reference_bytes > 65536:
        raise UsageError("asset references exceed the total 65536 UTF-8 byte limit")


def validate_box_answers(response, questions):
    wanted = [name for name, q in questions.items() if q['type'] == 'box']
    if not wanted:
        return
    model = response.get('model') if isinstance(response, dict) else None
    version = re.fullmatch(r'jev-(\d+)\.(\d+)\.(\d+)', model or '')
    if not version or tuple(map(int, version.groups())) < (1, 14, 0):
        raise JevError("Box response lacks a supported versioned model ID")
    answers = response.get('answers')
    if not isinstance(answers, dict):
        raise JevError("Box response is missing answers")
    for name in wanted:
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get('type') != 'box' or not isinstance(answer.get('box'), dict):
            raise JevError("invalid Box answer schema")
        edges = answer['box']
        if set(edges) != {'left', 'right', 'top', 'bottom'}:
            raise JevError("Box requires exactly four normalized edges")
        values = [*edges.values(), answer.get('confidence')]
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise JevError("Box values must be finite JSON numbers in [0,1]")
        if not (edges['left'] < edges['right'] and edges['top'] < edges['bottom']):
            raise JevError("Box edges have invalid ordering")


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer-authenticated API request through a redirect."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JevClient:
    """Thin HTTP client with retries. `ask()` returns the raw API response dict."""

    def __init__(self, api_key=None, model=None, timeout=30.0, retries=5, base_url=None, record=True, label="lib"):
        self.api_key, self.key_source = resolve_key(api_key)
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.retries = retries
        self.base_url = (base_url or API_BASE).rstrip("/")
        self.record = record
        self.label = label
        self.last_ms = 0.0
        self.last_request_id = ""
        self.last_attempts = 0
        self._opener = urllib.request.build_opener(_NoCredentialRedirect())

    def _request(self, method: str, path: str, body=None, extra_headers=None) -> dict:
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"jev-cli/{VERSION}",
        }
        if extra_headers is not None:
            allowed = {'TypeSafe-Danger-Zone-Images', 'TypeSafe-Danger-Zone-Long-Context'}
            if not isinstance(extra_headers, dict) or set(extra_headers) - allowed or any(v != 'true' for v in extra_headers.values()):
                raise UsageError("only explicit TypeSafe feature headers with value true are supported")
            headers.update(extra_headers)
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            t0 = time.time()
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    self.last_ms = (time.time() - t0) * 1000
                    self.last_request_id = r.headers.get("x-typesafe-request-id", "")
                    self.last_attempts = attempt
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                self.last_ms = (time.time() - t0) * 1000
                try:
                    text = e.read().decode(errors="replace").replace(self.api_key, '<redacted>')
                finally:
                    e.close()
                if e.code in (401, 403):
                    raise AuthError(f"HTTP {e.code}: API key rejected ({self.key_source}). {text[:300]}")
                if e.code in (429, 529) or e.code >= 500:
                    if attempt <= self.retries:
                        ra = e.headers.get("retry-after")
                        delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(8.0, 0.5 * 2 ** (attempt - 1))
                        time.sleep(delay + random.uniform(0, 0.25))
                        continue
                    raise JevError(f"HTTP {e.code} after {attempt} attempts: {text[:300]}")
                raise JevError(f"HTTP {e.code}: {text[:600]}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt <= self.retries:
                    time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)))
                    continue
                raise NetworkError(f"network error after {attempt} attempts: {e}")

    def models(self) -> dict:
        return self._request("GET", "/v1/models")

    def ask(self, state, questions: dict, model: str | None = None, *, assets=None,
            enable_images=False, enable_long_context=False) -> dict:
        """POST /v1/systemone. `state` is str|dict|list. Returns the raw response."""
        if state is None or state == "" or state == {} or state == []:
            raise UsageError("state is empty")
        validate_questions(questions)
        body = {"state": state, "model": model or self.model, "questions": questions}
        if not isinstance(body['model'], str) or not body['model'].strip():
            raise UsageError("model must be a nonempty string")
        if type(enable_images) is not bool or type(enable_long_context) is not bool:
            raise UsageError("experimental feature flags must be booleans")
        has_box = any(q['type'] == 'box' for q in questions.values())
        if assets is not None:
            validate_assets(assets, state, questions)
        if (assets or has_box) and not enable_images:
            raise UsageError("images/Box require explicit enable_images=True or --enable-images")
        if has_box:
            if not assets:
                raise UsageError("Box requires an image asset in the request")
            version = re.fullmatch(r'jev-(\d+)\.(\d+)\.(\d+)', body['model'])
            if version and tuple(map(int, version.groups())) < (1, 14, 0):
                raise UsageError("Box requires jev-1.14.0 or a newer supported model")
        if assets:
            body['assets'] = assets
        try:
            size = len(json.dumps(body, allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError):
            raise UsageError("request must contain finite JSON values") from None
        if size > 32 * 1024 * 1024:
            raise UsageError("JSON request exceeds the 32 MiB body limit")
        feature_headers = {}
        if enable_images:
            feature_headers['TypeSafe-Danger-Zone-Images'] = 'true'
        if enable_long_context:
            feature_headers['TypeSafe-Danger-Zone-Long-Context'] = 'true'
        try:
            resp = self._request("POST", "/v1/systemone", body,
                                 **({'extra_headers': feature_headers} if feature_headers else {}))
            validate_box_answers(resp, questions)
        except JevError as e:
            if self.record:
                record_usage(None, self, len(questions), err=e)
            raise
        if self.record:
            record_usage(resp, self, len(questions))
        return resp


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def price_per_mtok() -> float:
    v = load_config().get("price_per_mtok_in")
    return float(v) if v else PRICE_PER_MTOK_IN


def record_usage(resp: dict | None, client: "JevClient", n_questions: int, err: Exception | None = None) -> None:
    """Append one row per request (success or failure) to the local ledger."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "cmd": client.label,
            "agent": os.environ.get("JEV_AGENT") or os.environ.get("AGENT_ID") or "",
            "q": n_questions,
            "ms": round(client.last_ms),
        }
        if resp is not None:
            u = resp.get("usage") or {}
            row.update({"model": resp.get("model"), "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)})
            if client.last_request_id:
                row["rid"] = client.last_request_id
            if client.last_attempts > 1:
                row["attempts"] = client.last_attempts
        else:
            msg = str(err)
            code = msg.split(":", 1)[0].replace("HTTP ", "").strip() if msg.startswith("HTTP") else type(err).__name__
            row.update({"in": 0, "out": 0, "err": code})
        with USAGE_FILE.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def cost_usd(input_tokens: int) -> float:
    return input_tokens / 1_000_000 * price_per_mtok()


# --------------------------------------------------------------------------- input parsing


def parse_value(v: str):
    """`@path` reads a file (.json parsed). JSON-looking text is parsed. Else string."""
    if v.startswith("@"):
        p = Path(v[1:]).expanduser()
        if not p.exists():
            raise UsageError(f"file not found: {p}")
        txt = p.read_text()
        if p.suffix.lower() in (".json", ".jsonl"):
            try:
                return json.loads(txt) if p.suffix.lower() == ".json" else [json.loads(line) for line in txt.splitlines() if line.strip()]
            except json.JSONDecodeError as e:
                raise UsageError(f"{p}: invalid JSON: {e}")
        return txt
    s = v.strip()
    if s and s[0] in "{[" or s in ("true", "false", "null"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return v


def stable_sha256(value) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise UsageError(f"value cannot be canonically hashed: {e}") from e
    return hashlib.sha256(payload).hexdigest()


def build_state(args) -> object:
    srcs = [n for n in ("state", "state_file", "state_json") if getattr(args, n, None)]
    if len(srcs) > 1:
        raise UsageError("give the state one way: --state, --state-file, or --state-json (--field may be added to any of them, and becomes `text`)")
    state = None
    if getattr(args, "state", None):
        state = sys.stdin.read() if args.state == "-" else (parse_value(args.state) if args.state.startswith("@") else args.state)
    elif getattr(args, "state_file", None):
        state = parse_value("@" + args.state_file)
    elif getattr(args, "state_json", None):
        try:
            state = json.loads(args.state_json)
        except json.JSONDecodeError as e:
            raise UsageError(f"--state-json: invalid JSON: {e}")
    if getattr(args, "field", None):
        obj = {}
        if state is not None:
            obj["text"] = state
        for kv in args.field:
            if "=" not in kv:
                raise UsageError(f"--field expects key=value, got {kv!r}")
            k, v = kv.split("=", 1)
            obj[k.strip()] = parse_value(v)
        state = obj
    if state is None:
        state = implicit_stdin()
        if not state:
            raise UsageError("no state. Use --state TEXT, --state-file PATH, --state-json JSON, --field k=v, or pipe text on stdin")
    n = len(state) if isinstance(state, str) else len(json.dumps(state))
    if n > STATE_CHAR_BUDGET and not getattr(args, 'enable_long_context', False):
        eprint(f"jev: warning: state is {n:,} chars (~{n // 4:,} tokens); the per-request budget is ~32k tokens for state + longest question. Filter first, or use `jev batch`/`jev rank` which chunk.")
    return state


def implicit_stdin(timeout: float = 1.5) -> str | None:
    """Read stdin only when it is not a terminal AND has data ready. A harness that leaves
    stdin open with no writer would otherwise block forever; `--state -` forces a blocking read."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
    except (OSError, ValueError):
        pass
    return sys.stdin.read()


def parse_kv_options(items: list[str]) -> dict:
    """['billing=Payments', 'sales'] -> {'billing': 'Payments', 'sales': None}"""
    out = {}
    for it in items:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k.strip()] = parse_value(v) if v != "" else None
        else:
            out[it.strip()] = None
    return out


class QAction(argparse.Action):
    """Collects --noul/--choice/--score in declaration order."""

    def __call__(self, parser, ns, values, option_string=None):
        lst = getattr(ns, "qlist", None) or []
        lst.append((self.dest, list(values)))
        ns.qlist = lst


def questions_from_args(args) -> dict:
    qs: dict = {}
    if getattr(args, "questions_file", None):
        v = parse_value("@" + args.questions_file)
        if not isinstance(v, dict):
            raise UsageError("--questions-file must contain a JSON object of id -> question")
        qs.update(v)
    if getattr(args, "questions_json", None):
        try:
            qs.update(json.loads(args.questions_json))
        except json.JSONDecodeError as e:
            raise UsageError(f"--questions-json: invalid JSON: {e}")
    for kind, vals in getattr(args, "qlist", None) or []:
        if len(vals) < 2:
            raise UsageError(f"--{kind} needs ID INSTRUCTIONS [...]")
        qid, instr, rest = vals[0], parse_value(vals[1]), vals[2:]
        if kind == "noul":
            kv = parse_kv_options(rest)
            extra = set(kv) - {"true", "false"}
            if extra:
                raise UsageError(f"--noul {qid}: after the instructions only true=... and false=... are allowed, got {sorted(extra)}")
            qs[qid] = noul(instr, kv.get("true"), kv.get("false"))
        elif kind == "choice":
            if len(rest) < 2:
                raise UsageError(f"--choice {qid}: give at least 2 options as NAME or NAME=description")
            qs[qid] = choice(instr, parse_kv_options(rest))
        elif kind == "score":
            if len(rest) < 2:
                raise UsageError(f"--score {qid}: give 2..{MAX_SCORE_LEVELS} level descriptions, low to high")
            qs[qid] = score(instr, [parse_value(r) for r in rest])
        elif kind == "box":
            if rest:
                raise UsageError("--box requires exactly ID INSTRUCTIONS, with no criteria")
            qs[qid] = box(instr)
    if not qs:
        raise UsageError("no questions. Use --noul/--choice/--score, --questions-file, or --questions-json (see `jev ask --help`)")
    return qs


# --------------------------------------------------------------------------- rendering


def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)


def describe_level(desc) -> str:
    if isinstance(desc, dict):
        for k in ("what", "level", "description", "name"):
            if k in desc:
                return str(desc[k])
        return json.dumps(desc)[:80]
    return str(desc)


def render_answer(qid: str, a: dict) -> str:
    t = a.get("type")
    if t == "noul":
        p = a["noul"]
        return f"{qid:<24} noul    {p:.2f}   {'yes' if p >= 0.5 else 'no'}"
    if t == "choice":
        probs = a.get("probabilities", {})
        best = a["choice"]
        others = ", ".join(f"{k} {v:.2f}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]) if k != best)
        return f"{qid:<24} choice  {best}   p={probs.get(best, 0):.2f} conf={a.get('confidence', 0):.2f}   [{others}]"
    if t == "score":
        legend = a.get("legend", {})
        top = max(len(legend) - 1, 1)
        s = a["score"]
        nearest = describe_level(legend.get(str(int(round(s))), ""))
        return f"{qid:<24} score   {s:.2f}/{top}   conf={a.get('confidence', 0):.2f}   ≈ {nearest}"
    return f"{qid:<24} {t}     {json.dumps(a)}"


def render_response(resp: dict, ms: float, verbose: bool) -> str:
    lines = [render_answer(qid, a) for qid, a in resp.get("answers", {}).items()]
    u = resp.get("usage", {})
    lines.append(f"· {resp.get('model')} · {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out tokens · {ms:.0f} ms · ${cost_usd(u.get('input_tokens', 0)):.6f}")
    return "\n".join(lines)


def emit(args, resp: dict, ms: float, extra: dict | None = None) -> None:
    if args.json:
        out = dict(resp)
        if extra:
            out.update(extra)
        print(json.dumps(out, indent=None if args.compact else 2))
    else:
        print(render_response(resp, ms, args.verbose))


# --------------------------------------------------------------------------- commands


def request_options(args, body=None):
    body = body or {}
    if set(body) - {'state', 'questions', 'model', 'assets'}:
        raise UsageError("request supports state, questions, model and assets; feature headers require CLI flags")
    assets = body.get('assets')
    path = getattr(args, 'assets_file', None)
    if path:
        if assets is not None:
            raise UsageError("give assets in the request or --assets-file, not both")
        file = Path(path).expanduser()
        try:
            if file.stat().st_size > 32 * 1024 * 1024:
                raise UsageError("assets JSON file exceeds 32 MiB")
            assets = json.loads(file.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise UsageError("cannot read assets file as JSON") from None
    return {'assets': assets, 'enable_images': getattr(args, 'enable_images', False),
            'enable_long_context': getattr(args, 'enable_long_context', False)}


def cmd_ask(args) -> int:
    state = build_state(args)
    qs = questions_from_args(args)
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="ask")
    resp = c.ask(state, qs, **request_options(args))
    emit(args, resp, c.last_ms)
    return 0


def cmd_yes(args) -> int:
    state = build_state(args)
    q = noul(parse_value(args.instructions), args.true, args.false)
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="yes")
    resp = c.ask(state, {"q": q})
    p = resp["answers"]["q"]["noul"]
    lo, hi = (args.band if args.band else (args.threshold, args.threshold))
    if p >= hi:
        verdict, code = "yes", 0
    elif p < lo:
        verdict, code = "no", 1
    else:
        verdict, code = "uncertain", 1
    if args.json:
        print(json.dumps({"noul": p, "verdict": verdict, "threshold": args.threshold, "band": args.band, "model": resp["model"], "usage": resp["usage"]}))
    else:
        print(f"{p:.3f} {verdict}")
    return code


def cmd_pick(args) -> int:
    state = build_state(args)
    opts = parse_kv_options(args.options)
    if len(opts) < 2:
        raise UsageError("pick needs at least 2 options (NAME or NAME=description)")
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="pick")
    resp = c.ask(state, {"q": choice(parse_value(args.instructions), opts)})
    a = resp["answers"]["q"]
    conf = a.get("confidence", 0.0)
    ok = conf >= args.min_confidence
    if args.json:
        print(json.dumps({"choice": a["choice"], "confidence": conf, "probabilities": a["probabilities"], "verdict": "ok" if ok else "uncertain", "min_confidence": args.min_confidence, "model": resp["model"], "usage": resp["usage"]}))
    else:
        ranked = ", ".join(f"{k} {v:.2f}" for k, v in sorted(a["probabilities"].items(), key=lambda kv: -kv[1]))
        if ok:
            print(f"{a['choice']}  conf={conf:.2f}  [{ranked}]")
        else:
            print(f"uncertain  best={a['choice']} conf={conf:.2f} < {args.min_confidence}  [{ranked}]")
    return 0 if ok else 1


def cmd_rate(args) -> int:
    state = build_state(args)
    levels = [parse_value(lv) for lv in args.levels]
    if not 2 <= len(levels) <= MAX_SCORE_LEVELS:
        raise UsageError(f"rate needs 2..{MAX_SCORE_LEVELS} level descriptions, low to high")
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="rate")
    resp = c.ask(state, {"q": score(parse_value(args.instructions), levels)})
    a = resp["answers"]["q"]
    conf = a.get("confidence", 0.0)
    top = len(levels) - 1
    ok = conf >= args.min_confidence
    nearest = int(round(a["score"]))
    if args.json:
        print(json.dumps({"score": a["score"], "top": top, "normalized": a["score"] / top, "nearest_level": nearest, "nearest": describe_level(levels[nearest]), "confidence": conf, "probabilities": a["probabilities"], "verdict": "ok" if ok else "uncertain", "model": resp["model"], "usage": resp["usage"]}))
    else:
        flag = "" if ok else f"  uncertain (conf < {args.min_confidence})"
        print(f"{a['score']:.2f}/{top}  conf={conf:.2f}  ≈ {describe_level(levels[nearest])}{flag}")
    return 0 if ok else 1


def read_candidates(args) -> list:
    if args.candidates_file:
        v = parse_value("@" + args.candidates_file)
        if isinstance(v, list):
            return v
        return [line for line in str(v).splitlines() if line.strip()]
    if args.candidates:
        return list(args.candidates)
    txt = implicit_stdin()
    if txt:
        return [line.rstrip("\n") for line in txt.splitlines() if line.strip()]
    raise UsageError("no candidates: give them as arguments, --candidates-file (lines, or a JSON array), or on stdin")


def chunk_candidates(cands: list, max_chars: int, max_n: int) -> list[list[tuple[int, object]]]:
    chunks, cur, size = [], [], 0
    for i, c in enumerate(cands):
        n = len(c) if isinstance(c, str) else len(json.dumps(c))
        if cur and (size + n > max_chars or len(cur) >= max_n):
            chunks.append(cur)
            cur, size = [], 0
        cur.append((i, c))
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def cmd_rank(args) -> int:
    cands = read_candidates(args)
    if not cands:
        raise UsageError("no candidates")
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="rank")
    levels = [parse_value(lv) for lv in args.levels] if args.levels else None
    question_text = args.instructions or ("How well does `candidate` match `query`?" if levels else "Is `candidate` relevant to `query`?")
    if "{i}" in question_text or "candidates[" in question_text:
        eprint("jev: warning: refer to the item as `candidate`; each question carries its own item. Positional `candidates[i]` references mis-locate past ~20 items (measured 27% wrong at 150).")
    results: list[dict] = []
    usage_in = usage_out = 0
    t0 = time.time()
    chunks = chunk_candidates(cands, args.chunk_chars, args.chunk_size)

    def run(chunk):
        # The item lives INSIDE its question, not at an index in the state: Jev mis-locates
        # `candidates[i]` in long arrays (measured 2026-09-18: 86/320 wrong at 150 per request,
        # 29/320 at 25) while an embedded or keyed item scored 0/320 wrong at 320 per request.
        state = {"query": parse_value(args.query)}
        if args.context:
            state["context"] = parse_value(args.context)
        qs = {}
        for j, (i, cand) in enumerate(chunk):
            instr = {"question": question_text, "candidate": cand}
            qs[f"c{j}"] = score(instr, levels) if levels else noul(instr)
        resp = c.ask(state, qs)
        out = []
        for j, (i, cand) in enumerate(chunk):
            a = resp["answers"][f"c{j}"]
            if levels:
                top = len(levels) - 1
                out.append({"i": i, "p": a["score"] / top, "score": a["score"], "confidence": a.get("confidence"), "candidate": cand})
            else:
                out.append({"i": i, "p": a["noul"], "candidate": cand})
        return out, resp.get("usage", {})

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for out, u in ex.map(run, chunks):
            results.extend(out)
            usage_in += u.get("input_tokens", 0)
            usage_out += u.get("output_tokens", 0)
    results.sort(key=lambda r: -r["p"])
    if args.min is not None:
        results = [r for r in results if r["p"] >= args.min]
    if args.top:
        results = results[: args.top]
    ms = (time.time() - t0) * 1000
    if args.json:
        print(json.dumps({"results": results, "n": len(cands), "requests": len(chunks), "usage": {"input_tokens": usage_in, "output_tokens": usage_out}, "ms": round(ms)}, indent=None if args.compact else 2))
    else:
        for r in results:
            cand = r["candidate"] if isinstance(r["candidate"], str) else json.dumps(r["candidate"])
            cand = cand.replace("\n", " ")
            if len(cand) > args.width:
                cand = cand[: args.width - 1] + "…"
            print(f"{r['p']:.3f}  #{r['i']:<4} {cand}")
        eprint(f"· {len(cands)} candidates · {len(chunks)} request(s) · {usage_in} in tokens · {ms:.0f} ms · ${cost_usd(usage_in):.5f}")
    return 0


def cmd_batch(args) -> int:
    qs = questions_from_args(args)
    question_sha256 = stable_sha256(qs)
    if args.input == "-":
        raw = sys.stdin.read()
    else:
        p = Path(args.input).expanduser()
        if not p.exists():
            raise UsageError(f"input not found: {p}")
        raw = p.read_text()
    items = []
    for n, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        if args.text_lines:
            items.append((n, None, line))
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            items.append((n, None, line))
            continue
        if isinstance(obj, dict) and args.state_key:
            if args.state_key not in obj:
                raise UsageError(f"line {n}: no key {args.state_key!r}")
            items.append((n, obj.get(args.id_key), obj[args.state_key]))
        else:
            items.append((n, obj.get(args.id_key) if isinstance(obj, dict) else None, obj))
    if not items:
        raise UsageError("no input rows")
    c = JevClient(args.api_key, args.model, timeout=args.timeout, label="batch")
    out_f = open(args.out, "w" if args.overwrite else "x") if args.out else sys.stdout
    usage_in = 0
    t0 = time.time()
    errors = 0

    def run(item):
        n, ident, state = item
        metadata = {
            "requested_model": c.model,
            "question_sha256": question_sha256,
            "state_sha256": stable_sha256(state),
        }
        try:
            resp = c.ask(state, qs)
            row = {
                "line": n,
                "answers": resp["answers"],
                "usage": resp["usage"],
                "model": resp.get("model"),
                **metadata,
            }
        except JevError as e:
            row = {"line": n, "error": str(e), **metadata}
        if ident is not None:
            row["id"] = ident
        if args.echo:
            row["state"] = state
        return row

    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            for row in ex.map(run, items):
                if "error" in row:
                    errors += 1
                else:
                    usage_in += row["usage"].get("input_tokens", 0)
                out_f.write(json.dumps(row) + "\n")
                out_f.flush()
    finally:
        if args.out:
            out_f.close()
    ms = (time.time() - t0) * 1000
    eprint(f"· {len(items)} rows · {errors} errors · {usage_in} in tokens · {ms:.0f} ms · ${cost_usd(usage_in):.5f}" + (f" · wrote {args.out}" if args.out else ""))
    return 0 if errors == 0 else 4


def cmd_models(args) -> int:
    c = JevClient(args.api_key, args.model, timeout=args.timeout, record=False)
    resp = c.models()
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        for m in resp.get("models", []):
            print(f"{m.get('name'):<14} {str(m.get('release_date', ''))[:10]}  {m.get('description', '')}")
    return 0


def cmd_auth(args) -> int:
    if args.action == "set":
        key = args.key or (sys.stdin.readline().strip() if not sys.stdin.isatty() else "")
        if not key:
            raise UsageError("jev auth set <key>  (or pipe the key on stdin)")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            KEY_FILE.write_text(key.strip() + "\n")
        finally:
            os.umask(old)
        os.chmod(KEY_FILE, 0o600)
        print(f"stored {mask(key)} in {KEY_FILE} (0600)")
        return 0
    if args.action == "clear":
        if KEY_FILE.exists():
            KEY_FILE.unlink()
            print(f"removed {KEY_FILE}")
        else:
            print("no key file")
        return 0
    try:
        key, src = resolve_key(args.api_key)
    except AuthError as e:
        print(f"no key: {e}")
        return 3
    print(f"key {mask(key)} from {src}")
    if src == str(KEY_FILE):
        mode = oct(KEY_FILE.stat().st_mode & 0o777)
        print(f"file mode {mode}" + ("" if mode == "0o600" else "  <- should be 0600"))
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            msg = fn()
            print(f"ok    {name}: {msg}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  {name}: {e}")

    step("python", lambda: sys.version.split()[0])
    def key():
        k, src = resolve_key(args.api_key)
        return f"{mask(k)} from {src}"

    step("key", key)
    c = None

    def models():
        nonlocal c
        c = JevClient(args.api_key, args.model, timeout=args.timeout, record=False)
        r = c.models()
        return f"{[m['name'] for m in r.get('models', [])]} in {c.last_ms:.0f} ms"

    step("GET /v1/models", models)

    def roundtrip():
        r = c.ask("The export button crashes the settings page in Safari but works in Chrome.",
                  {"is_bug": noul("Does this describe a software bug?"),
                   "severity": score("How severe?", ["Cosmetic", "Degraded, workaround exists", "Blocking"])})
        a = r["answers"]
        return f"model={r['model']} is_bug={a['is_bug']['noul']:.2f} severity={a['severity']['score']:.2f} in {c.last_ms:.0f} ms, {r['usage']['input_tokens']} tokens"

    if c is not None:
        step("POST /v1/systemone", roundtrip)
    step("usage ledger", lambda: f"{USAGE_FILE} ({USAGE_FILE.stat().st_size if USAGE_FILE.exists() else 0} bytes)")
    step("skill", lambda: ", ".join(str(p) for p in (Path.home() / ".claude/skills/jev", Path.home() / ".codex/skills/jev", Path.home() / ".config/opencode/skills/jev") if p.exists()) or "not linked (see `jev guide install`)")
    print("healthy" if ok else "problems found")
    return 0 if ok else 4


def load_ledger() -> list[dict]:
    if not USAGE_FILE.exists():
        return []
    rows = []
    for line in USAGE_FILE.read_text().splitlines():
        try:
            r = json.loads(line)
            r["_t"] = datetime.fromisoformat(r["ts"])
            rows.append(r)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return rows


def agent_names(ids: set[str]) -> dict[str, str]:
    """Agents are attributed by $JEV_AGENT (or $AGENT_ID). Names are whatever you export."""
    return {i: (i if i else "(no agent)") for i in ids}


def _agg(sel: list[dict]) -> dict:
    ok = [r for r in sel if "err" not in r]
    tin = sum(r.get("in", 0) for r in ok)
    ms = sorted(r["ms"] for r in ok if r.get("ms"))
    pct = lambda p: ms[min(len(ms) - 1, int(len(ms) * p))] if ms else 0  # noqa: E731
    return {
        "requests": len(sel), "errors": len(sel) - len(ok), "questions": sum(r.get("q", 0) for r in ok),
        "input_tokens": tin, "output_tokens": sum(r.get("out", 0) for r in ok), "usd": round(cost_usd(tin), 6),
        "avg_ms": round(sum(ms) / len(ms)) if ms else 0, "p50_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": ms[-1] if ms else 0,
    }


def _peaks(sel: list[dict]) -> dict:
    per_min: dict[str, int] = {}
    per_sec: dict[str, int] = {}
    for r in sel:
        per_min[r["ts"][:16]] = per_min.get(r["ts"][:16], 0) + 1
        per_sec[r["ts"]] = per_sec.get(r["ts"], 0) + r.get("in", 0)
    rpm = max(per_min.values()) if per_min else 0
    tps = max(per_sec.values()) if per_sec else 0
    return {"peak_rpm": rpm, "peak_rpm_at": max(per_min, key=per_min.get) if per_min else None, "rpm_limit": RATE_LIMIT_RPM,
            "peak_tps": tps, "peak_tps_at": max(per_sec, key=per_sec.get) if per_sec else None, "tps_limit": RATE_LIMIT_TPS}


def cmd_usage(args) -> int:
    rows = load_ledger()
    if args.label:
        rows = [r for r in rows if r.get("cmd") == args.label]
    if args.agent:
        rows = [r for r in rows if args.agent in (r.get("agent") or "")]
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            raise UsageError("--since expects YYYY-MM-DD or an ISO timestamp")
        rows = [r for r in rows if r["_t"] >= since]
    if not rows:
        print("no usage recorded" + (" for that filter" if (args.label or args.agent or args.since) else " yet"))
        return 0
    now = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = load_config()

    if args.tail:
        sel = rows[-args.tail:]
        if args.json:
            print(json.dumps([{k: v for k, v in r.items() if k != "_t"} for r in sel], indent=2))
            return 0
        names = agent_names({r.get("agent", "") for r in sel})
        print(f"{'ts':<19} {'cmd':<12} {'agent':<22} {'q':>4} {'in':>7} {'ms':>5}  rid/err")
        for r in sel:
            tail = r.get("err") and f"ERR {r['err']}" or r.get("rid", "")
            print(f"{r['ts']:<19} {r.get('cmd', '?')[:12]:<12} {names[r.get('agent', '')][:22]:<22} {r.get('q', 0):>4} {r.get('in', 0):>7} {r.get('ms', 0):>5}  {tail}")
        return 0

    if args.by:
        key = args.by
        if key == "day":
            groups = {}
            for r in rows:
                if r["_t"] >= day0 - timedelta(days=args.days - 1):
                    groups.setdefault(r["ts"][:10], []).append(r)
        elif key == "hour":
            groups = {}
            for r in rows:
                if r["_t"] >= now - timedelta(hours=24):
                    groups.setdefault(r["ts"][:13] + ":00", []).append(r)
        elif key in ("cmd", "model"):
            groups = {}
            for r in rows:
                groups.setdefault(str(r.get(key) or "?"), []).append(r)
        elif key == "agent":
            names = agent_names({r.get("agent", "") for r in rows})
            groups = {}
            for r in rows:
                groups.setdefault(names[r.get("agent", "")], []).append(r)
        else:
            raise UsageError("--by expects day, hour, cmd, agent or model")
        table = {k: _agg(v) for k, v in groups.items()}
        order = sorted(table) if key in ("day", "hour") else sorted(table, key=lambda k: -table[k]["input_tokens"])
        if args.json:
            print(json.dumps({k: table[k] for k in order}, indent=2))
            return 0
        print(f"{key:<24} {'requests':>8} {'errors':>6} {'questions':>9} {'in tokens':>10} {'usd':>9} {'avg ms':>6}")
        for k in order:
            a = table[k]
            print(f"{k[:24]:<24} {a['requests']:>8} {a['errors']:>6} {a['questions']:>9} {a['input_tokens']:>10,} {a['usd']:>9.4f} {a['avg_ms']:>6}")
        return 0

    windows = {
        "today": day0, "yesterday": (day0 - timedelta(days=1), day0), "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30), "month": day0.replace(day=1), "all": datetime.min,
    }
    summary = {}
    for name, w in windows.items():
        lo, hi = (w if isinstance(w, tuple) else (w, None))
        sel = [r for r in rows if r["_t"] >= lo and (hi is None or r["_t"] < hi)]
        summary[name] = _agg(sel)
    last7 = [r for r in rows if r["_t"] >= now - timedelta(days=7)]
    last30 = [r for r in rows if r["_t"] >= now - timedelta(days=30)]
    first_ts = rows[0]["_t"]
    days_observed = max(1e-9, (now - first_ts).total_seconds() / 86400)
    rate7 = summary["7d"]["usd"] / min(7, days_observed)
    rate30 = summary["30d"]["usd"] / min(30, days_observed)
    forecast = {"usd_per_day_7d": round(rate7, 6), "usd_per_day_30d": round(rate30, 6),
                "usd_per_month_at_7d_rate": round(rate7 * 30, 4), "usd_per_month_at_30d_rate": round(rate30 * 30, 4),
                "days_observed": round(days_observed, 2)}
    credits = None
    if cfg.get("credits_usd") is not None:
        as_of = datetime.fromisoformat(cfg.get("credits_as_of") or first_ts.isoformat())
        spent = cost_usd(sum(r.get("in", 0) for r in rows if r["_t"] >= as_of))
        remaining = float(cfg["credits_usd"]) - spent
        credits = {"credits_usd": float(cfg["credits_usd"]), "as_of": as_of.isoformat(timespec="seconds"), "spent_since": round(spent, 6),
                   "remaining_usd": round(remaining, 6), "remaining_mtok": round(remaining / price_per_mtok(), 3) if remaining > 0 else 0.0,
                   "days_left_at_7d_rate": round(remaining / rate7, 1) if rate7 > 0 and remaining > 0 else None}
    budget = None
    if cfg.get("budget_usd_month"):
        b = float(cfg["budget_usd_month"])
        mtd = summary["month"]["usd"]
        dim = ((day0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        budget = {"budget_usd_month": b, "mtd_usd": mtd, "pct_used": round(100 * mtd / b, 1) if b else None,
                  "projected_month_end_usd": round(mtd + rate7 * (dim - now.day), 4), "days_left_in_month": dim - now.day}
    errors = {}
    for r in last7:
        if "err" in r:
            errors[r["err"]] = errors.get(r["err"], 0) + 1
    by_cmd = {}
    for r in rows:
        by_cmd.setdefault(r.get("cmd") or "?", []).append(r)
    names = agent_names({r.get("agent", "") for r in rows})
    by_agent = {}
    for r in rows:
        by_agent.setdefault(names[r.get("agent", "")], []).append(r)
    by_model = {}
    for r in rows:
        if r.get("model"):
            by_model.setdefault(r["model"], []).append(r)
    peaks = _peaks(last30)
    out = {
        "price_per_mtok_in": price_per_mtok(), "ledger": str(USAGE_FILE), "rows": len(rows), "first": rows[0]["ts"], "last": rows[-1]["ts"],
        "windows": summary, "by_cmd": {k: _agg(v) for k, v in by_cmd.items()}, "by_agent": {k: _agg(v) for k, v in by_agent.items()},
        "by_model": {k: _agg(v) for k, v in by_model.items()}, "errors_7d": errors, "throughput_30d": peaks,
        "forecast": forecast, "credits": credits, "budget": budget,
    }
    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"jev usage · ${price_per_mtok()}/Mtok in, output free · {len(rows)} requests since {rows[0]['ts'][:16]} · {USAGE_FILE}")
    print(f"\n{'window':<10} {'requests':>8} {'errors':>6} {'questions':>9} {'in tokens':>10} {'out':>7} {'usd':>9} {'avg ms':>6}")
    for name, a in summary.items():
        print(f"{name:<10} {a['requests']:>8} {a['errors']:>6} {a['questions']:>9} {a['input_tokens']:>10,} {a['output_tokens']:>7,} {a['usd']:>9.4f} {a['avg_ms']:>6}")
    a7 = summary["7d"]
    print(f"\nlatency 7d   p50 {a7['p50_ms']} ms · p95 {a7['p95_ms']} ms · max {a7['max_ms']} ms · avg questions/request {a7['questions'] / max(1, a7['requests'] - a7['errors']):.1f} · avg tokens/request {a7['input_tokens'] / max(1, a7['requests'] - a7['errors']):,.0f}")
    print(f"throughput   peak {peaks['peak_rpm']} req/min ({100 * peaks['peak_rpm'] / RATE_LIMIT_RPM:.1f}% of {RATE_LIMIT_RPM}) at {peaks['peak_rpm_at']} · peak {peaks['peak_tps']:,} tok/s ({100 * peaks['peak_tps'] / RATE_LIMIT_TPS:.2f}% of {RATE_LIMIT_TPS:,}) · 30d window")
    print("errors 7d    " + (", ".join(f"{k} x{v}" for k, v in sorted(errors.items(), key=lambda kv: -kv[1])) if errors else "none"))
    print(f"forecast     ${forecast['usd_per_day_7d']:.4f}/day at the 7d rate -> ${forecast['usd_per_month_at_7d_rate']:.2f}/month · ${forecast['usd_per_month_at_30d_rate']:.2f}/month at the 30d rate")
    if credits:
        dl = f" · ~{credits['days_left_at_7d_rate']} days left at the 7d rate" if credits["days_left_at_7d_rate"] else ""
        print(f"credits      ${credits['remaining_usd']:.4f} remaining of ${credits['credits_usd']:.2f} set {credits['as_of'][:16]} ({credits['remaining_mtok']:.1f} Mtok){dl}")
    else:
        print("credits      not configured. The API exposes no balance; read it at https://console.typesafe.ai and run `jev config set credits <usd>`")
    if budget:
        print(f"budget       ${budget['mtd_usd']:.4f} of ${budget['budget_usd_month']:.2f} this month ({budget['pct_used']}%) · projected month end ${budget['projected_month_end_usd']:.2f}")
    print(f"\n{'by command':<24} {'requests':>8} {'questions':>9} {'in tokens':>10} {'usd':>9}")
    for k, v in sorted(by_cmd.items(), key=lambda kv: -sum(r.get('in', 0) for r in kv[1]))[:12]:
        a = _agg(v)
        print(f"{k[:24]:<24} {a['requests']:>8} {a['questions']:>9} {a['input_tokens']:>10,} {a['usd']:>9.4f}")
    print(f"\n{'by agent':<24} {'requests':>8} {'questions':>9} {'in tokens':>10} {'usd':>9}")
    for k, v in sorted(by_agent.items(), key=lambda kv: -sum(r.get('in', 0) for r in kv[1]))[:12]:
        a = _agg(v)
        print(f"{k[:24]:<24} {a['requests']:>8} {a['questions']:>9} {a['input_tokens']:>10,} {a['usd']:>9.4f}")
    if len(by_model) > 1:
        print("\nby model: " + ", ".join(f"{k} {len(v)}" for k, v in by_model.items()))
    print("\nmore: --by day|hour|cmd|agent|model · --tail N · --since DATE · --label X · --agent X · --json · jev config · jev cost")
    return 0


CONFIG_KEYS = {
    "credits": ("credits_usd", float, "prepaid balance in USD, read off https://console.typesafe.ai; `jev usage` counts down from it"),
    "credits_as_of": ("credits_as_of", str, "ISO timestamp the balance was read (default: when you set it)"),
    "budget": ("budget_usd_month", float, "monthly spend budget in USD; `jev usage` shows % used and a month-end projection"),
    "price": ("price_per_mtok_in", float, f"USD per million input tokens if the vendor changes it (default {PRICE_PER_MTOK_IN})"),
}


def cmd_config(args) -> int:
    cfg = load_config()
    if args.action == "show" or not args.action:
        if args.json:
            print(json.dumps(cfg, indent=2))
            return 0
        print(f"{CONFIG_FILE}" + ("" if CONFIG_FILE.exists() else " (not created yet)"))
        for k, (real, _, desc) in CONFIG_KEYS.items():
            print(f"  {k:<14} {str(cfg.get(real, '-')):<24} {desc}")
        return 0
    if args.action == "unset":
        if not args.key or args.key not in CONFIG_KEYS:
            raise UsageError(f"jev config unset <{'|'.join(CONFIG_KEYS)}>")
        cfg.pop(CONFIG_KEYS[args.key][0], None)
        if args.key == "credits":
            cfg.pop("credits_as_of", None)
        save_config(cfg)
        print(f"unset {args.key}")
        return 0
    if args.action == "set":
        if not args.key or args.key not in CONFIG_KEYS or args.value is None:
            raise UsageError(f"jev config set <{'|'.join(CONFIG_KEYS)}> <value>")
        real, typ, _ = CONFIG_KEYS[args.key]
        try:
            val = typ(args.value)
        except ValueError:
            raise UsageError(f"{args.key} expects a {typ.__name__}")
        if args.key == "credits_as_of":
            try:
                datetime.fromisoformat(val)
            except ValueError:
                raise UsageError("credits_as_of expects an ISO timestamp, e.g. 2026-09-19T14:00")
        cfg[real] = val
        if args.key == "credits":
            cfg["credits_as_of"] = args.as_of or datetime.now().isoformat(timespec="seconds")
        save_config(cfg)
        print(f"set {args.key} = {val}" + (f" (as of {cfg['credits_as_of']})" if args.key == "credits" else ""))
        return 0
    raise UsageError("jev config [show|set|unset]")


CHARS_PER_TOKEN = 4.0  # rough English heuristic; --exact measures the real count with one tiny call


def cmd_cost(args) -> int:
    """Estimate tokens and cost for a prospective job before running it."""
    state = build_state(args)
    text = state if isinstance(state, str) else json.dumps(state)
    est_state = int(len(text) / CHARS_PER_TOKEN)
    q_tokens = int(args.question_chars / CHARS_PER_TOKEN)
    exact = None
    if args.exact:
        c = JevClient(args.api_key, args.model, timeout=args.timeout, label="cost")
        r = c.ask(state, {"probe": noul("Is this text written in English?")})
        exact = r["usage"]["input_tokens"]
        est_state = max(0, exact - q_tokens - 40)  # subtract the probe question and request overhead
    per_request = est_state + args.questions * q_tokens
    fits = per_request <= 64_000 and (est_state + q_tokens) <= 32_000
    n_items = args.items or 1
    if args.items:
        # rank/batch shape: each item is its own request if --per-item, else items are packed with the state repeated per request
        if args.per_item:
            total = per_request * n_items
            requests = n_items
        else:
            per_req_items = max(1, min(250, (64_000 - est_state) // max(1, q_tokens)))
            requests = -(-n_items // per_req_items)
            total = requests * est_state + n_items * q_tokens
    else:
        total, requests = per_request, 1
    out = {"state_tokens": est_state, "exact": exact is not None, "question_tokens_each": q_tokens, "questions": args.questions,
           "tokens_per_request": per_request, "fits_request_budget": fits, "items": n_items, "requests": requests,
           "total_input_tokens": total, "usd": round(cost_usd(total), 6), "price_per_mtok_in": price_per_mtok()}
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    how = "measured" if exact is not None else f"estimated at {CHARS_PER_TOKEN:g} chars/token (add --exact to measure)"
    print(f"state        {est_state:,} tokens ({how})")
    print(f"per request  {per_request:,} tokens = state + {args.questions} question(s) x ~{q_tokens} · {'fits' if fits else 'EXCEEDS'} the 64k/32k request budget")
    if args.items:
        print(f"job          {n_items:,} items -> {requests:,} request(s), {total:,} input tokens")
    print(f"cost         ${cost_usd(total):.6f} at ${price_per_mtok()}/Mtok" + (f" (${cost_usd(total) / n_items:.7f} per item)" if args.items else ""))
    return 0


def cmd_schema(args) -> int:
    print(json.dumps({
        "state": "string | object | array  (the content to judge; use an object with named fields; reference them in instructions with backticks like `ticket.message`)",
        "model": "jev-latest",
        "questions": {
            "is_urgent": {"type": "noul", "instructions": "Does `ticket.message` convey urgency?", "criteria": {"true": "optional: what yes means", "false": "optional: what no means"}},
            "department": {"type": "choice", "instructions": "Which team should handle `ticket.message`?", "criteria": {"billing": "Charges, invoices, refunds", "technical": "Bugs, outages, integrations", "other": None}},
            "frustration": {"type": "score", "instructions": "How frustrated does the customer appear in `ticket.message`?", "criteria": ["Calm and matter-of-fact", "Frustrated but civil", "Very angry or threatening to leave"]},
        },
    }, indent=2))
    print("\nAnswers come back under the same ids: noul -> {noul}; choice -> {choice, probabilities, confidence}; score -> {score, legend, probabilities, confidence}.\ninstructions and every criteria entry may be a string OR a JSON object/array (see `jev guide structure`).")
    return 0


def cmd_docs(args) -> int:
    url = f"{DOCS}/llms.txt"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"jev-cli/{VERSION}"}), timeout=20) as r:
            txt = r.read().decode()
    except Exception as e:  # noqa: BLE001
        raise NetworkError(f"could not fetch {url}: {e}")
    if args.grep:
        needle = args.grep.lower()
        txt = "\n".join(line for line in txt.splitlines() if needle in line.lower())
    print(txt)
    return 0


def cmd_guide(args) -> int:
    if args.list:
        for k, (title, _, live) in GUIDE.items():
            print(f"{k:<12} {title}" + (f"   (live: {live if live.startswith('http') else DOCS + live + '.md'})" if live else ""))
        return 0
    topic = args.topic or "when"
    if topic not in GUIDE:
        raise UsageError(f"unknown topic {topic!r}. Topics: {', '.join(GUIDE)}")
    title, body, live = GUIDE[topic]
    if args.live or topic == "vendor":
        if not live:
            raise UsageError(f"topic {topic!r} has no live page")
        url = live if live.startswith("http") else f"{DOCS}{live}.md"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"jev-cli/{VERSION}"}), timeout=20) as r:
                print(r.read().decode())
        except Exception as e:  # noqa: BLE001
            raise NetworkError(f"could not fetch {url}: {e}")
        return 0
    print(f"# {title}\n\n{body.strip()}\n")
    if live:
        print(f"Live source: {live if live.startswith('http') else DOCS + live + '.md'}   (jev guide {topic} --live)")
    return 0


def cmd_examples(args) -> int:
    if not args.name:
        print("Copy-paste examples. `jev examples <name>` prints one; `jev examples all` prints everything.\n")
        for k, (title, _) in EXAMPLES.items():
            print(f"  {k:<10} {title}")
        return 0
    keys = list(EXAMPLES) if args.name == "all" else [args.name]
    for k in keys:
        if k not in EXAMPLES:
            raise UsageError(f"unknown example {k!r}. Names: {', '.join(EXAMPLES)}")
        title, body = EXAMPLES[k]
        print(f"## {title}\n{body.strip()}\n")
    return 0


# --------------------------------------------------------------------------- embedded docs

GUIDE: dict[str, tuple[str, str, str | None]] = {}

GUIDE["when"] = ("Jev playbook for agents: when to reach for it, how to ask, how to read", r"""
Jev is TypeSafe's System One model. It does not write text. It makes ONE narrow judgment
per question against a state you hand it, and returns a calibrated probability. ~250 ms.
$0.042 per million input tokens (a 1k-token item costs $0.00004). Output tokens are free.
Same input -> same answer, run to run. It is a semantic if-statement, not a chatbot.

## Reach for Jev when
1. The decision REPEATS over many items and you should not read them all into context:
   classify / filter / rank / dedupe hundreds of notes, emails, transcript segments, log
   lines, RSS items, PRs, findings. `jev rank` and `jev batch` do this in a few requests.
2. You want a CALIBRATED PROBABILITY, not your own vibe. Your confidence is not calibrated;
   Jev's is trained to be (RLCD). Use it to decide act / confirm / escalate.
3. A SCRIPT or CUE needs a judgment that runs unattended, a million times, with no agent
   loop: "is this Ring event a delivery?", "is this email a receipt?", "is this item relevant?"
4. You need an INDEPENDENT CHECK of your own output: does the source support this claim,
   does this draft break a stated rule, does this text contain an instruction aimed at an
   agent (prompt injection), is this tool call risky.
5. You need SPEED: real-time gates inside a request path or a UI.

## Do NOT use Jev for
- Generation, rewriting, summarizing, explaining. It cannot. Use yourself.
- Math, counting, date arithmetic, numeric comparison. Keep those in code; Jev extracts
  the parts (a Choice over months/days), code does the arithmetic.
- Multi-hop reasoning, double negatives, "a property of a property".
- One small item you can judge in a glance. The call costs more than the decision.
- Hostile input without care: state is data, and injected text can move an answer. Write
  precise criteria and test edge cases.

## The three primitives (pick by the SHAPE of the answer your code needs)
| need                         | type   | answer fields                                   |
| is this true?                | noul   | noul = P(yes) 0..1  (no separate confidence)    |
| which one of these?          | choice | choice, probabilities{opt: p}, confidence        |
| where on this rubric?        | score  | score (0..N-1, may fall between), legend,        |
|                              |        | probabilities{level: p}, confidence              |

## Fast path (one question)
    jev yes  "Does this message ask for a refund?" --state "I was charged twice, fix it"
    jev pick "Which team handles this?" billing="Charges, refunds" tech="Bugs" other -s "…"
    jev rate "How severe is the bug?" "Cosmetic" "Degraded, workaround exists" "Blocking" -s @report.txt
  `yes` exits 0/1 on --threshold (default 0.5). `pick`/`rate` exit 1 below --min-confidence.
  Pipe text on stdin instead of --state:  cat email.txt | jev yes "Is this a receipt?"

## Real path (many questions, one call: SPECULATIVE FAN-OUT)
Ask EVERY question you might need in ONE request. They run in parallel and independently;
adding questions barely changes latency and costs only the question tokens. Let code ignore
the answers it does not need.
    jev ask --field message=@ticket.txt --field policy=@refund_policy.md \
      --noul   refund   'Does `message` request a refund?' \
      --noul   covered  'Does `policy` cover the refund requested in `message`?' \
      --choice team     'Which team should handle `message`?' billing=Charges tech=Bugs other \
      --score  anger    'How angry is `message`?' "Calm" "Frustrated but civil" "Hostile" --json
  Shell rule: SINGLE-QUOTE any instruction containing backticks, or zsh runs them as commands.

## Writing a question that works (Jev reads LITERALLY)
- One judgment per question. "Analyze this and decide what to do" is not a question; split
  it. "Does `message` ask for money back?" is.
- Say the exact condition. If you find yourself explaining what you "really meant", that
  explanation belongs in the instructions or criteria.
- Point at the field with a backticked path: `ticket.messages[0].text`. Question IDs are
  NOT sent to the model; the instructions must carry the whole meaning.
- NEVER index into a long array (`items[137]`). Measured 2026-09-18: 27% wrong at 150
  items per request, still 9% at 25. Key the object instead (`items.k137`) or put the
  item inside the question itself; both scored 0 wrong out of 320. `jev rank` does the
  latter for you.
- Choice: describe each option so the boundary with its neighbour is clear; add `other` /
  `none` when the list may not cover the input. Up to 255 options.
- Score: levels describe SITUATIONS, low to high, 2..10 of them. "Broken but a workaround
  exists" works; "moderately bad" and bare numbers do not. One dimension per score.
- Noul: phrase it so high = yes. Optional true=/false= criteria pin a subtle boundary.
- Send only the state the questions need. Irrelevant context lowers accuracy (context rot).
  Filter in code first. Budget: ~32k tokens for state + longest question.

## Reading the answer
- noul: p >= 0.5 is yes, but pick the threshold by stakes. Do not read 0.5 as "medium".
- choice: `choice` is the argmax; `confidence` says how peaked the distribution is. A second
  option with real mass is signal, not noise.
- score: a position between levels (0.7*1 + 0.3*2 = 1.3). Round for one outcome; compare or
  sort for ranking; divide by N-1 to normalize before weighting several scores together.
- THREE BANDS: high confidence -> act; medium -> confirm or gather more; low -> do not act,
  escalate or ask. Thresholds scale with risk: read-only at 0.6, destructive at 0.9.
- Do not carry a threshold tuned on a noul over to a choice, and do not expect P(x) and
  1 - P(not x) from two questions to agree. Ask the question you want, one way.
- Combine independent answers in CODE with weights you own (composite scoring). A weight
  change is a code change, not a prompt rewrite, and needs no re-inference.

## Build it into a tool, not just a chat turn
    sys.path.insert(0, "/path/to/dir/containing/jev.py")
    from jev import JevClient, noul, choice, score
    r = JevClient(label="my_tool").ask(state, {"ok": noul("Is `text` a receipt?")})
    p = r["answers"]["ok"]["noul"]
Keep the QUESTIONS and THRESHOLDS in one constant block at the top of the tool so a human
can review them. That is the part that needs eyes; the rest is plumbing.

## Go deeper
    jev guide --list          topics: questions choice score noul state confidence patterns
                              design pitfalls structure cookbooks api library usage
                              exit-codes install vendor
    jev guide design          BUILDING a tool or cron job on Jev, not just calling it once
    jev guide vendor          TypeSafe's own agent skill, fetched live
    jev guide <topic> --live  the vendor's page for that topic, current
    jev examples              copy-paste recipes: triage rank filter guardrail verify batch python
    jev docs                  live index of every vendor page and cookbook
""", "/concepts/how-to-build-with-system-one")

GUIDE["questions"] = ("Writing instructions and criteria", r"""
A question is: an ID you choose (not sent to the model), a `type`, `instructions`, and
`criteria` (options for choice, ordered levels for score, optional true/false for noul).

## Rules that come from measured failure modes (jev-1.13)
1. LITERAL READING. Jev answers the words, not the intent. Scoping words, negations and
   implied conditions are taken at face value. Write the exact condition. Put boundary
   cases in the criteria. Split any interpretation into two literal questions.
2. ATOMIC. Broad questions hide several judgments behind one number. "Is this spam?"
   becomes: requests a credential? sender identity conflicts with domain? announces an
   unexpected reward? Then weight them in code.
3. NAME THE FIELD. With an object state, reference `path.to[0].field` in backticks.
   Structured state + named references removes ambiguity about what to judge. But do
   not rely on POSITIONAL indexes in long arrays: `items[137]` mis-locates (27% wrong
   at 150 items, 9% at 25, measured 2026-09-18). Use keyed objects (`items.k137`) or
   embed the item in the question; both 0% wrong at 320.
4. ALIGN INSTRUCTIONS AND CRITERIA. A noul where true means "no" confuses it. The
   criteria are an extension of the instruction, in the same direction.
5. CONTRASTIVE OPTIONS. For a choice, say what each option covers, what belongs to the
   neighbour instead, and 1-3 examples. Same field names on every option.
6. SITUATIONS, NOT DEGREES. Score levels are matched one by one against the state; the
   model does not see the level numbers or neighbours. "Cosmetic; no impact" beats "1".
7. COVER THE SPACE. Add `other` / `none_of_the_above` to a choice when inputs may not fit.
   For selection-from-candidates, the model cannot pick a value you did not offer.
8. KEEP MATH OUT. Counting, dates, magnitudes, hex colours, assembly: convert or compute in
   code, hand Jev the semantic part ("does this colour read as a warning?").
9. LESS STATE. Send only what the question needs. Retrieve and filter first.

## Shapes
    noul   {"type":"noul","instructions":"Does `msg` request a refund?",
            "criteria":{"true":"asks for money back or a credit","false":"complains without asking for a remedy"}}
    choice {"type":"choice","instructions":"Which team should handle `msg`?",
            "criteria":{"billing":"charges, invoices, refunds","orders":"status, delivery, returns","other":null}}
    score  {"type":"score","instructions":"How severe is the bug in `report`?",
            "criteria":["Cosmetic; no functional impact","Broken, workaround exists","Blocking; no workaround"]}

instructions and every criteria entry may be a JSON object or array instead of a string
(`jev guide structure`). Start with strings; add structure when two options keep getting
confused or a level needs examples.
""", "/primitives")

GUIDE["choice"] = ("Choice: pick one option from a defined set", r"""
Use when the answer is one of a known set with NO order between them: routing, category,
language, entity type, which candidate span is the value. Up to 255 options; adding an
option costs a few tokens, so give the FULL list rather than a shortlist.

Answer: `choice` (argmax), `probabilities` (every option, sums to 1), `confidence`
(how peaked). Read the runner-up: 0.60 vs 0.38 means two teams want this ticket.

    jev pick "Which team should handle this?" \
      returns="Exchanges, refunds, wrong or damaged items" \
      shipping="Delivery status, delays, lost packages" \
      billing="Charges, invoices, payment problems" other \
      -s "My shoes came in the wrong size and I was charged twice" --min-confidence 0.4

- Confusable neighbours: give each option an object {what, not_for, examples}.
- Deep taxonomy: one choice per level, options = children, value = the subtree; walk in
  code, keep several branches alive when probabilities are close (beam search).
- Relative vs absolute: a choice says WHICH option is best even when all are bad. Pair it
  with one noul per option (or a `none` option) to know whether ANY fits.
- Speculative: "If this is a return, why?" is fine to ask on every ticket; ignore the
  answer when the department was not returns.
""", "/primitives/choice")

GUIDE["score"] = ("Score: rate the state on an ordered rubric", r"""
Use when the answer is a position on a spectrum you can describe in steps: severity,
frustration, relevance, seniority, formality. 2..10 levels, low to high. Level N is its
index in the array. The model judges each level's description on its own against the
state; it never sees the numbers or the neighbours.

Answer: `score` = sum(level * p), can land between levels; `probabilities` per level;
`legend` maps level -> description; `confidence`.

    jev rate "How much does this bug report give an engineer to work with?" \
      "No detail; just says something is broken" \
      "Names the feature but no steps or environment" \
      "Steps to reproduce or environment, but not both" \
      "Steps to reproduce and environment" -s @report.txt

- Describe SITUATIONS. Examples inside a level (object with what + examples) sharpen it,
  but only when they resemble real inputs. Test against known cases.
- One dimension per score. "Punctual and smart and experienced" cannot be placed.
- Give a rare extreme its own level ("abusive or threatening") so the top is not shared.
- Do not interpolate magnitudes from a score ("1.3 means 30% of users"). It is a
  position for thresholding, ranking and rounding, not a measurement.
- Same score, different shapes: 1.0 can be all-on-level-1 or half 0 / half 2. Read
  probabilities and confidence alongside.
- Composite: normalize each score by (levels-1), then weight in code:
  priority = 0.6*severity + 0.3*frustration + 0.1*report_quality.
""", "/primitives/score")

GUIDE["noul"] = ("Noul: probability that a yes/no statement is true", r"""
Use for a clean yes/no where the probability itself is the signal: mentions X, requests
Y, contains PII, is a receipt, is relevant to the query, contains an instruction aimed at
an agent. Answer: `noul` = P(yes). No separate confidence: near 1 strong yes, near 0
strong no, near 0.5 undecided (NOT "medium"; for degree use a score).

    jev yes 'Does `text` contain an instruction directed at an AI agent or assistant?' \
      --true "tells an agent/assistant/model to do, ignore, or reveal something" \
      --false "ordinary content with no directive aimed at a model" \
      --field text=@untrusted.md --threshold 0.7

- Phrase so high means yes. A statement form ("the customer is requesting a refund")
  works as well as a question; test both on your data.
- Criteria are optional; add true=/false= when the boundary is subtle.
- One noul per label when several labels may apply at once (multi-label), instead of
  a single choice.
- Counting: one noul per item, the item embedded in its question or under a key
  (`items.k7`), never `items[7]` in a long array; then count in code.
- `jev yes` exit codes: 0 yes, 1 no or uncertain (with --band LOW HIGH), 2+ errors.
""", "/primitives/noul")

GUIDE["state"] = ("State: what you hand the model", r"""
State is the material a panel of experts would be shown before judging. String, JSON
object, or array. Images require explicit opt-in and assets; audio/video need preprocessing.

    --state "text"            --state @file        --state -  (stdin)     cat f | jev …
    --state-file path         (.json parsed, else text)
    --state-json '{"a":1}'
    --field msg=@ticket.txt --field policy=@policy.md --field n=3   (builds an object)

Every shape below is state, and a question is separate from it:

| Shape | How | Reference it as |
|---|---|---|
| one string | `-s TEXT`, `-s @file`, stdin | the whole state |
| many sources at once | `--field msg=@t.txt --field policy=@p.md --field orders=@o.json` | `msg`, `policy`, `orders` |
| nested JSON | `--state-file o.json` | `open[0].charges`, `sla.tier` |
| a sequence | `--state-json '["turn 1","turn 2"]'` | the array, in order |
| a whole document | `--state-file bigdoc.md` | measured OK to 107k chars |
| full control | `jev ask -f request.json` (state AND questions) | anything you wrote |

A `--field` value that is `@file.json` or inline JSON is PARSED, so an object stays an
object and the model sees real structure. Mixing types in one request is fine: a text file,
a JSON file, a JSONL file and a bare scalar side by side.

Prefer an OBJECT with named fields, then reference fields in instructions with backticked
paths: `ticket.messages[0].text`, `policy`. Small arrays index fine; long ones do not
(`items[137]` is wrong 27% of the time at 150 items). Key them: `{"k137": ...}`.

- One request = one state, any number of questions, all independent, all parallel.
- Budget, MEASURED 2026-09-20 by binary search rather than taken from the docs: the largest
  state accepted was **107,500 chars = 32,388 input tokens** (765 ms); ~110k chars returns
  `HTTP 400 max_tokens_exceeded`. English measured at **3.32 chars/token**, not 4, so the
  real ceiling is nearer 107k chars than the 150k a 4-char rule of thumb implies. `jev`
  warns above 120k chars; `rank` and `batch` chunk under it.
- Irrelevant material costs accuracy. Filter in code. If you cannot, ask a noul for
  relevance first and drop what fails.
- State is data, not instructions, but adversarial text inside it can still steer an
  answer. Be explicit in criteria; test edge cases before trusting a guardrail.
- When a later question depends on an earlier ANSWER (you need it to fetch data or pick
  the next options), make a second request. Otherwise ask everything at once.
""", "/concepts/state")

GUIDE["confidence"] = ("Probabilities vs confidence, and how to act on them", r"""
Every choice/score answer carries `probabilities` (the distribution) and `confidence`
(one number, 0..1, derived from how peaked that distribution is). Nouls carry only P(yes).

Calibration: across many predictions, outcomes given 0.8 happen ~80% of the time. It
describes groups, not any single answer; confidence 1.0 is not a guarantee.

## Three bands (draw the lines by stakes, then test on your data)
  high    act automatically
  medium  act with a confirmation, or gather more, or flag
  low     do not act: escalate to a person or a reasoning model, or ask

## Thresholds scale with risk
    if a.confidence < 0.5:        escalate()          # genuinely unsure
    elif a.choice == "read_only":  do_it()             # cheap to be wrong
    elif a.choice == "destroy":
        if a.confidence > 0.9:     confirm_then_do()
        else:                      ask_user()

- Low choice confidence: no clear winner, OR several acceptable answers (harmless).
- Low score confidence: levels overlap, question measures >1 thing, or the state does
  not say enough.
- If all you need is the best option, take the argmax; thresholds are for deciding
  whether to ACT. If you have a statistical procedure in mind, use probabilities, not
  the confidence summary.
- Aliases move (`jev-latest` -> new version). Thresholds tuned on one version: pin the
  versioned id (`jev-1.13.0`) with --model and migrate on your schedule.
""", "/confidence")

GUIDE["patterns"] = ("Patterns that compose Jev into systems", r"""
1. SPECULATIVE FAN-OUT. Put every question the workflow might need in one request,
   including ones that matter only on some branches. Parallel, independent, near-zero
   latency cost. 13 questions in one call measured 12x cheaper and 10x faster than 13
   calls, with identical answers. Code decides which answers count.
2. CONFIDENCE-GATED ROUTING. The answer says WHAT; confidence says WHETHER to act. Gate
   each action at its own threshold by consequence. (`jev guide confidence`)
3. COMPOSITE SCORING. Break a judgment into atomic scores, normalize by (levels-1),
   weight in code. Weights are reviewable and tunable without re-inference.
4. INTENT ROUTING. Cheap classifier in front of expensive handlers: deterministic code
   for one intent, a specialist LLM for another, a human for the low-confidence rest.
5. SELECT, DO NOT GENERATE. Find candidate spans/values with regex or a generative model,
   then let Jev CHOOSE the right one. Extraction becomes a choice over enumerated options
   with an explicit "not stated".
6. RERANK / FILTER. Cheap retrieval (BM25, embeddings, grep) makes a shortlist; one Jev
   question per query-candidate pair grades it. `jev rank` does this in one request per
   chunk. Measured on a legal corpus: top-1 5% -> 18%, top-10 38% -> 62%.
7. VERIFY AND ESCALATE. Check a claim against its source (supports / contradicts / says
   nothing), an extraction against the document, a tool-call trace against its intent.
   Low confidence goes to a human or a reasoning model. Cascade: small model extracts,
   Jev verifies, big model only on the uncertain slice.
8. GUARDRAIL. One request per message, in and out of an LLM: a battery of nouls (jailbreak?
   harm? self-harm? instruction aimed at the model?) plus a severity score. Threshold in
   code: pass / review / block / route.
9. MAP-REDUCE. Thousands of items x same questions -> `jev batch`, then aggregate in code.
   Probabilities become features for a classical model when you have labels.
10. TWO-STAGE SELECTION. Rank everything cheaply, then re-judge the top K with fuller
    evidence and let the second stage reject all of them. (skill suggestion cookbook:
    wrong loads down by more than half.)
""", "/patterns")

GUIDE["design"] = ("Designing a NEW integration (tool, cron job, pipeline)", r"""
Runtime use is `jev yes|pick|rate|ask|rank|batch`. This topic is for the other job: you
are about to BUILD something that calls Jev on every run. Much of it is distilled from
TypeSafe's own agent skill (github.com/typesafe-ai/skills, MIT), which is a docs-oriented
skill for exactly this job; `jev guide vendor` fetches it live.

## Start from the behaviour, work back to the judgments
What will the tool show, select, change, or hand off? List the judgments that decision
needs. Keep known rules, calculations, exact lookups and execution in code. Preserve the
stack and scope already in place; add Jev only where semantic understanding helps.

## Shapes beyond "classify"
- Route AND fill arguments: one request selects the handler and its closed-set params.
- Select instead of generate: code finds candidate spans/values, Jev picks the intended
  one. Check candidate COVERAGE: the model cannot choose a value you did not offer.
- Find and judge evidence: cheap retrieval, then per-candidate relevance, then select.
- Judgments as reusable data: score dimensions once; weights, thresholds, rankings and
  views change in code with no re-inference while the evidence and questions stand.
- Verify and escalate: check a claim/field/tool-call against its evidence; send the
  uncertain slice to a person or a reasoning model.
- Respond to changing state: keep INFERRED state distinct from OBSERVED facts, and check
  freshness before applying an old answer to a changed situation.

## Atomic is not "one fact per question"
A bounded action selection or a contextual interpretation is a valid single judgment.
Split independently useful dimensions; do not split a relationship you need judged whole.

## Policy stays explicit in code
Weighted sums suit compensating preferences. An "any serious violation blocks" rule needs
separate conditions, not a weight. Ignore uncertainty on branches you do not take. Several
acceptable alternatives can spread probability; low confidence on a harmless preference
choice is not a reason to stop. Typed output guarantees the INTERFACE, not truth:
validate on your own data before trusting it unattended.

## Ship checklist
1. Questions + thresholds in ONE constant block at the top of the file. Agents write
   mediocre questions; expect a human to edit them, so make them easy to find.
2. `--model jev-1.13.0` pinned once thresholds are tuned. `jev-latest` moves.
3. A handful of known cases run before the first unattended run; when one fails, separate
   missing evidence / model error / code error / service failure before changing anything.
4. `label=` set on the client so `jev usage` attributes the spend.
5. Long lists: never `items[i]`; key the object or embed the item (see `pitfalls` row 10).
6. Never embed the key in a web page or a repo; the CLI reads ~/.config/typesafe/api_key.
""", "/concepts/how-to-build-with-system-one")

GUIDE["vendor"] = ("TypeSafe's own agent skill (fetched live from GitHub)", r"""
TypeSafe publishes a docs-oriented skill for agents BUILDING integrations:
  https://github.com/typesafe-ai/skills  (skills/typesafe-ai/SKILL.md, MIT, ~2.5k tokens)
It has no executable and no runtime commands; `jev` is the runtime. Its value is the
design guidance and the map of which vendor page to read for which task, and it is
updated by the vendor when the API changes. `jev guide vendor --live` prints the current
copy. `jev guide design` is our distillation, kept short.
""", "https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md")

GUIDE["pitfalls"] = ("Jagged edges of jev-1.13 (vendor-acknowledged)", r"""
| # | failure                              | do this instead                                   |
| 1 | literal reading                      | write the exact condition; boundary cases in criteria |
| 2 | math, counting, magnitudes           | compute in code; one noul per item then sum       |
| 3 | date/time comparison                 | extract parts as choices; compare in code         |
| 4 | indirection, double negatives        | direct wording; name the field                    |
| 5 | large state with irrelevant detail   | filter first; relevance noul if you cannot        |
| 6 | adversarial content in state         | precise criteria; test edge cases                 |
| 7 | contradictory instructions/criteria  | align them; true must mean yes                    |
| 8 | expected structural invariants       | P(x) and 1-P(not x) need not agree; noul vs choice thresholds differ; ask one way |
| 9 | generation                           | it cannot; select from candidates instead         |
| 10| positional index into a long array   | (ours, measured 2026-09-18) `items[137]` 27% wrong at 150, 9% at 25; keyed object or item-in-question 0% at 320 |

Numeric representations (hex colours, RGB, assembly) underperform names and high-level
code. Non-English works but is weaker; watch confidence. Reviewed by the vendor
2026-09-17. Live: jev guide pitfalls --live
""", "/model-jaggedness/jev-1.13")

GUIDE["structure"] = ("Structured instructions and criteria (JSON instead of strings)", r"""
`instructions`, every choice option description, every score level, and noul true/false
may be a string, an object, an array, or null. The model is trained on structure; keys
are labels it reads, so use short descriptive names (none are reserved).

    "instructions": {"question": "Does the claimed sender conflict with the domain?",
                     "compare": ["`ticket.sender.display_name`", "`ticket.sender.email`"],
                     "focus": "Compare the named organization with the email domain."}

    "criteria": {"return_policy": {"what": "rules for returns in general",
                                   "not_for": "the status of a specific return",
                                   "examples": ["Can I return sale items?"]},
                 "return_status": {"what": "where a specific return is",
                                   "not_for": "general policy questions",
                                   "examples": ["Where is my refund for order A-104?"]}}

    score level: {"what": "Broken or degraded, workaround exists",
                  "examples": ["export fails in one browser but works in another"]}

When: a question has several parts; two options keep getting confused; a level needs
examples; the supporting data is already JSON (schema, taxonomy, record). Use the same
field names across options so the model compares like with like. Examples only help when
they resemble real inputs; the wrong example changes little, the right one concentrates
probability (measured 0.54 -> 0.90 confidence on the same ticket).

Taxonomy walk: option value = the child subtree, so the model sees what lives under a
branch before committing. Trim large subtrees to direct children plus sample leaves.
""", "/primitives/advanced")

GUIDE["cookbooks"] = ("Vendor cookbooks (worked, measured examples)", r"""
Each is a page under https://docs.typesafe.ai/cookbooks/<slug>.md
  parallel_questions               13 questions, one call: 12.2x cheaper, 10x faster, same answers
  skill_suggestion                 rank 182 agent skills, re-judge top 3, may reject all; wrong loads halved
  rerank_typesafe                  BM25 shortlist -> one question per pair; top-1 5% -> 18%
  semantic_find                    score 218 line ids vs a query in ONE choice; noul: is there an answer at all
  classifying_rag_passages         per passage: relevant? usable? contradicts premise? instructs the model?
  citation_check                   supports / contradicts / says nothing; confidence flags for review
  llm_guardrails                   noul battery + severity score on every LLM input and output
  function_calling                 NL request -> function name + closed-set args, confidence-aware
  pre_parsed_value_extraction      regex finds candidates, Jev selects the intended span
  date_extraction_cookbook         month/day/year as choices with "not stated"; code assembles and compares
  autoformat                       recover Markdown structure: line-join nouls, then block-type choices
  hierarchical_classification      beam search over choice probabilities through deep taxonomies
  entity_alignment                 one 3-level score decides merge / leave / send to curator
  sde_cascade                      mini extracts -> Jev verifies -> reasoning model only where needed
  consistency_noul_cookbook        14 nouls x 15 runs: std dev 0.0102, below every LLM; route 0.3-0.7 to review
  consistency_choice_cookbook      add an explicit `uncertain` outcome to moderation decisions
  classification_using_confidence  75 industry groups; low confidence -> report the parent division
  autoresearch_feature_discovery   propose questions, turn text into features, train CatBoost on them
Read one with: jev docs --grep <slug>   then fetch the .md URL.
""", "/cookbooks/parallel_questions")

GUIDE["api"] = ("HTTP API, limits, pricing", r"""
POST https://api.typesafe.ai/v1/systemone      Authorization: Bearer <key>
body   {"state": str|obj|arr, "model": "jev-latest", "questions": {id: Question}}
answer {"model": "jev-1.13.0", "answers": {id: Answer}, "usage": {"input_tokens", "output_tokens"}}
GET  /v1/models   lists aliases (jev-latest, jev-preview); versioned ids like jev-1.13.0 also accepted

Errors: 401 bad key · 422 malformed request (body names the field) · 429 rate limit ·
529 overloaded. `jev` retries 429/529/5xx with backoff and honours retry-after.
Limits (jev-1.13, adjust without notice): 250k tokens/s, 1200 req/min; 64k tokens per
request, 32k for state + longest question. MEASURED 2026-09-20: 107,500 chars of English
state = 32,388 tokens went through; ~110k chars returned `400 max_tokens_exceeded`, an error
code the docs do not list. English runs 3.32 chars/token, so budget on tokens, not on the
4-chars-per-token rule of thumb. Text only.
Price: $42 per billion input tokens ($0.042/Mtok). Output free. `jev usage` shows spend,
`jev cost` estimates a job, `jev config set credits` tracks a balance (no balance endpoint
exists; see `jev guide usage`).
Model is not fine-tuned per customer; you shape it through state, instructions, criteria.
Not trained on customer traffic. `jev schema` prints a request skeleton.
Full JSON path: jev ask -f request.json   (or `jev ask` with the JSON body on stdin)
""", "/api")

GUIDE["library"] = ("Using jev from Python (tools, Cues, pipelines)", r"""
    import sys; sys.path.insert(0, "/path/to/dir/containing/jev.py")
    from jev import JevClient, noul, choice, score, JevError

    QUESTIONS = {                                  # keep questions + thresholds together, reviewable
        "is_receipt": noul("Is `email.body` a purchase receipt or order confirmation?"),
        "vendor": choice("Which vendor sent `email`?", {"amazon": None, "apple": None, "other": None}),
        "urgency": score("How urgent is `email.body`?", ["None", "This week", "Today"]),
    }
    THRESHOLDS = {"is_receipt": 0.7, "vendor_conf": 0.6}

    client = JevClient(label="mail_triage")        # label shows up in `jev usage`
    r = client.ask({"email": {"subject": s, "body": b}}, QUESTIONS)
    a = r["answers"]
    if a["is_receipt"]["noul"] >= THRESHOLDS["is_receipt"]: ...
    if a["vendor"]["confidence"] < THRESHOLDS["vendor_conf"]: escalate()

- `ask()` raises JevError (exit_code attr: 2 usage, 3 auth, 4 api, 5 network).
- Many items: a ThreadPoolExecutor over `client.ask` (8 workers is safe), or shell out
  to `jev batch`. Every call appends one line to ~/.config/typesafe/usage.jsonl.
- Official SDKs exist (`pip install typesafe-sdk`, `npm i @typesafe-ai/sdk`) with typed
  answers and retries; this module is the zero-dependency path that works from any agent.
- Pin `--model jev-1.13.0` in a pipeline whose thresholds you tuned.
""", "/sdk/python")

GUIDE["usage"] = ("Usage, cost, credits and rate limits", r"""
Every request appends one row to ~/.config/typesafe/usage.jsonl: ts, cmd (the label), agent
($JEV_AGENT), q (questions), in/out tokens, ms, model, rid (x-typesafe-request-id),
attempts when retried, or err on failure. Nothing secret is in it. `record=False` on
JevClient skips it.

    jev usage                 windows (today/yesterday/7d/30d/month/all), latency p50/p95,
                              peak req/min and tok/s vs the published limits, errors, forecast,
                              credits countdown, budget, by command, by agent (names resolved)
    jev usage --by day        also hour, cmd, agent, model     --days N
    jev usage --tail 20       recent requests with request ids (quote a rid to the vendor)
    jev usage --since 2026-09-01 --label rank --agent 8ba5 --json
    jev cost --state-file f --questions 12 [--exact]      tokens + usd before you run it
    jev cost -s "…" --items 5000                          a rank-shaped job over 5,000 items
    jev config set credits 50     balance read off the console; usage counts down from it
    jev config set budget 5       monthly USD budget; usage shows % used and a projection
    jev config set price 0.042    only if the vendor changes list price

## What the API does NOT give you (measured 2026-09-19)
No balance, usage, or quota endpoint exists (/v1/usage, /v1/balance, /v1/account ... all
404) and responses carry no rate-limit headers, only x-typesafe-request-id. "Tokens
remaining" therefore means one of three things here:
  1. credits remaining: a balance YOU snapshot from https://console.typesafe.ai via
     `jev config set credits`, minus spend recorded since. Drift = calls made from
     another machine or key. Re-snapshot when you look at the console.
  2. request budget: 64k tokens per request, 32k for state + longest question. `jev cost`
     says whether a state fits and how many items pack per request.
  3. rate headroom: 1,200 req/min and 250k tok/s (adjust without notice). `jev usage`
     shows your measured peaks as a percentage of both.
Price: $42 per billion input tokens ($0.042/Mtok). Output is free. A 1k-token item costs
$0.00004; a thousand of them cost four cents.
""", "/models")

GUIDE["exit-codes"] = ("Exit codes", r"""
  0  success / yes / confident
  1  `yes`: no or uncertain · `pick`/`rate`: confidence below --min-confidence
  2  usage error (bad flags, empty state, malformed question)
  3  auth error (no key, key rejected)
  4  API error after retries (429/529/5xx exhausted, 422) · `batch`: some rows failed
  5  network error after retries
Errors print one line to stderr prefixed `jev:`; with --json the line is JSON.
""", None)

GUIDE["install"] = ("Where jev lives and how it is maintained", r"""
Single file      jev.py, anywhere on disk; symlink it onto PATH:  ln -s $PWD/jev.py ~/.local/bin/jev
Requires         Python 3.10+, standard library only
Key              jev auth set <key>   -> ~/.config/typesafe/api_key (0600), or export TYPESAFE_API_KEY
                 Keys: https://console.typesafe.ai/keys
Usage ledger     ~/.config/typesafe/usage.jsonl   (jev usage)
Config           ~/.config/typesafe/config.json   (jev config: credits, budget, price; no secrets)
Skill            copy the jev/ skill folder (SKILL.md) into your agent's skills dir:
                   Claude Code   ~/.claude/skills/jev/
                   Codex         ~/.agents/skills/jev/  (documented user path; ~/.codex/skills/ also read)
                   OpenCode      ~/.config/opencode/skills/jev/
                 One canonical copy, symlinked into each, is easiest to keep current.
Attribution      export JEV_AGENT=<name> so `jev usage --by agent` can tell your agents apart
Health           jev doctor
Vendor docs      https://docs.typesafe.ai   (Markdown at any page path + .md; index at /llms.txt)
Vendor skill     github.com/typesafe-ai/skills is a docs-orientation skill for BUILDING integrations,
                 with no executable. `jev guide vendor` fetches it live. Installing both puts two skills
                 on the same triggers; pick one.
""", None)

EXAMPLES: dict[str, tuple[str, str]] = {}

EXAMPLES["gutcheck"] = ("One calibrated yes/no, usable in an `if`", r"""
    if jev yes 'Does `text` describe a package delivery?' --field text="$DESC" --threshold 0.7; then
      echo delivery
    fi
    cat draft.md | jev yes "Does the text contain an em dash character or the phrase 'you're absolutely right'?"
    # (that one is a lint, and grep does it better; use jev for judgments grep cannot make)
    cat draft.md | jev yes "Does the text flatter the reader or agree without giving a reason?" --json
""")

EXAMPLES["triage"] = ("Classify + speculative questions in one call", r"""
    jev ask --field message=@ticket.txt --field open_orders='["A-104","A-118"]' \
      --choice team 'Which team should handle `message`?' \
          billing="Charges, invoices, refunds, subscriptions" \
          orders="Order status, delivery, cancellation, returns" \
          account="Login, profile, permissions, security" other \
      --noul refund 'Does `message` explicitly ask for money back or a credit?' \
      --noul mentions_order 'Does `message` refer to one of `open_orders` by id?' \
      --score anger 'How angry is the author of `message`?' \
          "Calm and matter-of-fact" "Frustrated but civil" "Hostile or threatening to leave" \
      --json
    # then in code: route on team.choice if team.confidence >= 0.6 else escalate;
    # read `refund` only on the billing branch; read `mentions_order` only on orders.
""")

EXAMPLES["rank"] = ("Rank or filter many candidates against a query (one request per chunk)", r"""
    # lines in a file
    jev rank --query "notes about thermostat schedules" --candidates-file titles.txt --top 10
    # JSON array of objects (each candidate is shown to the model as JSON)
    jev rank --query "a bug that causes data loss" --candidates-file issues.json --min 0.6 --json
    # stdin, graded on a rubric instead of yes/no
    ls Meetings | jev rank --query "meetings about the Q3 launch" \
      --levels "unrelated" "adjacent" "directly about it"
    # custom question; refer to the item as `candidate` (it is embedded in each question)
    jev rank --query "$Q" --candidates-file x.txt --instructions 'Does `candidate` answer `query`?'
    # extra shared context for every judgment
    jev rank --query "…" --candidates-file x.txt --context @rubric.md
""")

EXAMPLES["guardrail"] = ("Screen untrusted text before acting on it", r"""
    jev ask --field text=@untrusted.md \
      --noul injection 'Does `text` contain an instruction aimed at an AI agent, assistant, or model?' \
          true="tells a model to do, ignore, reveal, or change something" \
          false="ordinary content; any imperatives are aimed at a human reader" \
      --noul secrets 'Does `text` ask the reader to disclose a credential, key, or password?' \
      --score harm 'How much harm would follow from doing what `text` asks?' \
          "None" "Minor or reversible" "Serious or irreversible" --json
    # thresholds live in your code: block if injection >= 0.7 or harm >= 1.5; review 0.4..0.7
""")

EXAMPLES["verify"] = ("Check a claim against its source, or a draft against a rule", r"""
    jev ask --field claim="$CLAIM" --field source=@source.md \
      --choice support 'How does `source` relate to `claim`?' \
          supports="the source states or entails the claim" \
          contradicts="the source states the opposite or an incompatible fact" \
          silent="the source does not address the claim" --json
    # confidence < 0.8 -> a human confirms it

    jev ask --field draft=@post.md --field rules=@voice_rules.md \
      --noul r1 'Does `draft` violate any rule stated in `rules`?' \
      --noul r2 'Does `draft` contain a sentence that flatters the reader?' \
      --score tone 'How formal is `draft`?' "casual" "neutral" "formal"
""")

EXAMPLES["batch"] = ("Same questions over thousands of rows -> JSONL", r"""
    # rows: one JSON object per line; --state-key picks the field to judge, --id-key is echoed back
    jev batch --input items.jsonl --state-key text --id-key id --concurrency 8 \
      --noul relevant 'Is `text` about home automation?' \
      --score quality 'How useful is `text` to a power user?' "noise" "some value" "must read" \
      --out results.jsonl
    # plain text lines, one state per line
    jev batch --input lines.txt --text-lines --noul q "Is this line a TODO?" > out.jsonl
    # aggregate in code:  jq 'select(.answers.relevant.noul > 0.7) | .id' results.jsonl
""")

EXAMPLES["python"] = ("From a tool or a cron job", r"""
    import sys; sys.path.insert(0, "/path/to/dir/containing/jev.py")
    from jev import JevClient, noul, choice, score
    Q = {"delivery": noul("Does `event.description` describe a package being delivered?"),
         "who": choice("Who is at the door in `event.description`?",
                       {"courier": None, "family": None, "stranger": None, "nobody": None})}
    r = JevClient(label="ring_watch").ask({"event": ev}, Q)
    if r["answers"]["delivery"]["noul"] > 0.8: notify()
""")

# --------------------------------------------------------------------------- argparse

SHORT_HELP = """jev: calibrated, typed decisions from TypeSafe's Jev model (~250 ms, $0.042/Mtok)

  jev yes  "question?" -s TEXT                 P(yes); exit 0 yes / 1 no
  jev pick "which?" a=desc b=desc other -s TEXT   one option + probabilities + confidence
  jev rate "how much?" "low" "mid" "high" -s TEXT position on your rubric + confidence
  jev ask  --noul/--choice/--score … -s TEXT   many questions, one call (fan-out), --json
  jev rank --query Q --candidates-file F       rank/filter many items, chunked
  jev batch --input rows.jsonl --noul …        same questions over many rows -> JSONL

  jev guide            the playbook (read once per session)   jev guide --list
  jev examples         copy-paste recipes                     jev docs   live vendor index
  jev usage            spend, tokens, latency, throughput, credits, budget   (--by, --tail, --json)
  jev cost             estimate a job before running it   jev config   credits / budget / price
  jev doctor · models · schema · auth set|status|clear · version

State: -s/--state TEXT | --state-file F | --state-json J | --field k=v (v=@file) | stdin.
Questions reference fields with backticks; SINGLE-QUOTE them in a shell: 'Does `message` ask for a refund?'
"""


def add_state_args(p):
    g = p.add_argument_group("state")
    g.add_argument("-s", "--state", help="text to judge; @path reads a file; '-' reads stdin")
    g.add_argument("--state-file", help="file to judge (.json is parsed, else text)")
    g.add_argument("--state-json", help="inline JSON state")
    g.add_argument("--field", action="append", metavar="KEY=VALUE", help="build an object state; VALUE may be @file or JSON; repeatable; combines with --state as `text`")


def add_question_args(p, *, boxes=False):
    g = p.add_argument_group("questions (repeatable, any mix)")
    g.add_argument("--noul", dest="noul", action=QAction, nargs="+", metavar="ARG", help="ID INSTRUCTIONS [true=…] [false=…]")
    g.add_argument("--choice", dest="choice", action=QAction, nargs="+", metavar="ARG", help="ID INSTRUCTIONS OPTION[=desc] OPTION[=desc] …")
    g.add_argument("--score", dest="score", action=QAction, nargs="+", metavar="ARG", help="ID INSTRUCTIONS LEVEL LEVEL … (low to high, 2..10)")
    if boxes:
        g.add_argument("--box", dest="box", action=QAction, nargs="+", metavar="ARG", help="ID INSTRUCTIONS; needs image opt-in/assets and Jev 1.14+")
    g.add_argument("--questions-file", "-q", help="JSON file: {id: question}")
    g.add_argument("--questions-json", help="inline JSON: {id: question}")


def add_common(p):
    p.add_argument("--model", default=None, help=f"model id or alias (default {DEFAULT_MODEL}; pin e.g. jev-1.13.0)")
    p.add_argument("--api-key", default=None, help="override key resolution")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true", help="machine output (raw API response where applicable)")
    p.add_argument("--compact", action="store_true", help="single-line JSON")
    p.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jev", description=SHORT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter, add_help=True)
    p.add_argument("--version", action="version", version=f"jev {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("ask", help="many questions, one call", description="Ask any mix of noul/choice/score questions about one state in ONE request (speculative fan-out). Also: `jev ask -f request.json` or a full JSON body on stdin.", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES["triage"][1])
    add_state_args(a)
    add_question_args(a, boxes=True)
    a.add_argument("-f", "--request", help="full request JSON file (state+questions); '-' for stdin")
    a.add_argument("--assets-file", help="JSON object of base64 PNG/JPEG image assets")
    a.add_argument("--enable-images", action="store_true", help="explicit experimental Images/Box header for this request only")
    a.add_argument("--enable-long-context", action="store_true", help="explicit separate experimental long-context header")
    add_common(a)
    a.set_defaults(fn=cmd_ask_entry)

    y = sub.add_parser("yes", help="one yes/no -> P(yes); exit 0/1", description="One noul question. Prints `<p> yes|no|uncertain`. Exit 0 when p >= --threshold (or >= HIGH of --band), else 1.")
    y.add_argument("instructions", help="the yes/no question; phrase so that high = yes")
    y.add_argument("--true", help="what a yes means (optional criteria)")
    y.add_argument("--false", help="what a no means (optional criteria)")
    y.add_argument("--threshold", type=float, default=0.5)
    y.add_argument("--band", type=float, nargs=2, metavar=("LOW", "HIGH"), help="p < LOW no · LOW..HIGH uncertain · >= HIGH yes")
    add_state_args(y)
    add_common(y)
    y.set_defaults(fn=cmd_yes)

    k = sub.add_parser("pick", help="one choice -> option + confidence", description="One choice question. Options as NAME or NAME=description (null description is fine when the name is clear). Exit 1 when confidence < --min-confidence.")
    k.add_argument("instructions")
    k.add_argument("options", nargs="+", metavar="OPTION[=desc]")
    k.add_argument("--min-confidence", type=float, default=0.0)
    add_state_args(k)
    add_common(k)
    k.set_defaults(fn=cmd_pick)

    r = sub.add_parser("rate", help="one score -> position on rubric", description="One score question. Levels low to high, 2..10, each describing a situation. Exit 1 when confidence < --min-confidence.")
    r.add_argument("instructions")
    r.add_argument("levels", nargs="+", metavar="LEVEL")
    r.add_argument("--min-confidence", type=float, default=0.0)
    add_state_args(r)
    add_common(r)
    r.set_defaults(fn=cmd_rate)

    rk = sub.add_parser("rank", help="rank/filter many candidates vs a query", description="Grades every candidate against --query with one question per candidate (the item embedded in its question), packed into as few requests as the token budget allows. Default: noul relevance. With --levels: a score rubric (normalized to 0..1 as p).", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES["rank"][1])
    rk.add_argument("--query", required=True, help="what you are looking for (text, JSON, or @file)")
    rk.add_argument("--candidates-file", help="one candidate per line, or a .json array")
    rk.add_argument("candidates", nargs="*", help="candidates as arguments (or stdin)")
    rk.add_argument("--instructions", help="the question, referring to `candidate` and `query`, e.g. 'Does `candidate` answer `query`?' (the item is embedded in each question)")
    rk.add_argument("--levels", nargs="+", metavar="LEVEL", help="use a score rubric instead of yes/no")
    rk.add_argument("--context", help="extra shared state (text, JSON, or @file) available as `context`")
    rk.add_argument("--top", type=int, help="keep the best N")
    rk.add_argument("--min", type=float, help="keep p >= MIN")
    rk.add_argument("--chunk-size", type=int, default=250, help="max candidates per request (default 250; 320 measured at 0.4 s)")
    rk.add_argument("--chunk-chars", type=int, default=90_000, help="max candidate chars per request (default 90k)")
    rk.add_argument("--concurrency", type=int, default=6)
    rk.add_argument("--width", type=int, default=110, help="truncate candidate display")
    add_common(rk)
    rk.set_defaults(fn=cmd_rank)

    b = sub.add_parser("batch", help="same questions over many rows -> JSONL", description="One request per input row, in parallel. Rows: JSONL (object = state, or --state-key picks a field), or plain text lines (--text-lines). Output: JSONL with answers per row.", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES["batch"][1])
    b.add_argument("--input", "-i", required=True, help="JSONL or text file; '-' for stdin")
    b.add_argument("--state-key", help="field of each JSON row to use as the state")
    b.add_argument("--id-key", default="id", help="field echoed back as `id` (default id)")
    b.add_argument("--text-lines", action="store_true", help="treat every line as a plain-text state")
    b.add_argument("--echo", action="store_true", help="include the state in each output row")
    b.add_argument("--out", "-o", help="write JSONL here instead of stdout; refuses to overwrite unless --overwrite")
    b.add_argument("--overwrite", action="store_true", help="allow replacing an existing --out file")
    b.add_argument("--concurrency", type=int, default=8)
    add_question_args(b)
    add_common(b)
    b.set_defaults(fn=cmd_batch)

    m = sub.add_parser("models", help="list models/aliases")
    add_common(m)
    m.set_defaults(fn=cmd_models)

    au = sub.add_parser("auth", help="set | status | clear the API key")
    au.add_argument("action", choices=["set", "status", "clear"], nargs="?", default="status")
    au.add_argument("key", nargs="?")
    add_common(au)
    au.set_defaults(fn=cmd_auth)

    d = sub.add_parser("doctor", help="check key, connectivity, one round trip")
    add_common(d)
    d.set_defaults(fn=cmd_doctor)

    u = sub.add_parser("usage", help="spend, tokens, latency, throughput, credits and budget from the local ledger", description="Every request appends a row to ~/.config/typesafe/usage.jsonl. Default prints the summary; --by breaks it down; --tail lists recent requests. Credits and budget come from `jev config` because the API exposes no balance.")
    u.add_argument("--by", choices=["day", "hour", "cmd", "agent", "model"], help="breakdown table")
    u.add_argument("--days", type=int, default=14, help="days for --by day (default 14)")
    u.add_argument("--tail", type=int, metavar="N", help="last N requests with request ids")
    u.add_argument("--since", help="only rows at or after YYYY-MM-DD / ISO timestamp")
    u.add_argument("--label", help="only rows with this cmd/label")
    u.add_argument("--agent", help="only rows whose agent id contains this")
    add_common(u)
    u.set_defaults(fn=cmd_usage)

    cf = sub.add_parser("config", help="show | set | unset credits, budget, price", description="Values live in ~/.config/typesafe/config.json (no secrets). `jev config set credits 50` after reading the balance off the console; `jev usage` then counts down.")
    cf.add_argument("action", nargs="?", choices=["show", "set", "unset"], default="show")
    cf.add_argument("key", nargs="?", help="credits | credits_as_of | budget | price")
    cf.add_argument("value", nargs="?")
    cf.add_argument("--as-of", help="ISO timestamp the credits balance was read (with `set credits`)")
    add_common(cf)
    cf.set_defaults(fn=cmd_config)

    co = sub.add_parser("cost", help="estimate tokens and cost for a job before running it", description="Estimates from characters (4 per token) or measures with one tiny real call (--exact). --items N and --per-item model a rank/batch job.")
    co.add_argument("--questions", type=int, default=1, help="questions per request (default 1)")
    co.add_argument("--question-chars", type=int, default=120, help="average characters per question incl. criteria (default 120)")
    co.add_argument("--items", type=int, help="number of items for a rank/batch-shaped job")
    co.add_argument("--per-item", action="store_true", help="each item is its own request with the full state (batch shape) rather than packed (rank shape)")
    co.add_argument("--exact", action="store_true", help="measure the state's real token count with one tiny call (costs a fraction of a cent)")
    add_state_args(co)
    add_common(co)
    co.set_defaults(fn=cmd_cost)

    s = sub.add_parser("schema", help="print a request skeleton")
    s.set_defaults(fn=cmd_schema)

    g = sub.add_parser("guide", help="the agent playbook; `guide <topic>`; `guide --list`")
    g.add_argument("topic", nargs="?")
    g.add_argument("--list", action="store_true")
    g.add_argument("--live", action="store_true", help="fetch the vendor's current page for this topic")
    g.set_defaults(fn=cmd_guide)

    e = sub.add_parser("examples", help="copy-paste recipes; `examples <name>` or `examples all`")
    e.add_argument("name", nargs="?")
    e.set_defaults(fn=cmd_examples)

    dc = sub.add_parser("docs", help="live vendor doc index (llms.txt); --grep to filter")
    dc.add_argument("--grep")
    dc.set_defaults(fn=cmd_docs)

    ver = sub.add_parser("version")
    ver.set_defaults(fn=lambda a: print(f"jev {VERSION}") or 0)
    return p


def cmd_ask_entry(args) -> int:
    if args.request:
        raw = sys.stdin.read() if args.request == "-" else Path(args.request).expanduser().read_text()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise UsageError(f"request JSON invalid: {e}")
        if not isinstance(body, dict) or "state" not in body or "questions" not in body:
            raise UsageError("request JSON must have `state` and `questions`")
        c = JevClient(args.api_key, args.model or body.get("model"), timeout=args.timeout, label="ask")
        resp = c.ask(body["state"], body["questions"], **request_options(args, body))
        emit(args, resp, c.last_ms)
        return 0
    # a full JSON body on stdin (no flags) is also accepted
    if not (args.state or args.state_file or args.state_json or args.field) and not getattr(args, "qlist", None) and not args.questions_file and not args.questions_json and not sys.stdin.isatty():
        raw = implicit_stdin() or ""
        try:
            body = json.loads(raw)
            if isinstance(body, dict) and "state" in body and "questions" in body:
                c = JevClient(args.api_key, args.model or body.get("model"), timeout=args.timeout, label="ask")
                resp = c.ask(body["state"], body["questions"], **request_options(args, body))
                emit(args, resp, c.last_ms)
                return 0
        except json.JSONDecodeError:
            pass
        raise UsageError("stdin was not a request JSON; pass questions with --noul/--choice/--score and the state with --state or stdin")
    return cmd_ask(args)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        print(SHORT_HELP.rstrip())
        return 0
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "compact"):
        args.compact = False
    if not hasattr(args, "verbose"):
        args.verbose = False
    try:
        return int(args.fn(args) or 0)
    except JevError as e:
        if getattr(args, "json", False):
            eprint(json.dumps({"error": str(e), "exit_code": e.exit_code}))
        else:
            eprint(f"jev: {e}")
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
