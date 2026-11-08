#!/usr/bin/env python3
"""Fetch Ollama Cloud usage and write it where the Ollama Cloud usage bar widget reads it.

Source (undocumented endpoints, Bearer API key):
  - Current plans: GET /api/balance gives the monthly included credits
    (allowance, balance left, billing period, so the reset time too) and any
    purchased credits; GET /api/usage?range=30d&group_by=model gives daily
    per-model request counts, summed over the billing period.
  - Legacy plans: GET /api/usage reported each limit window (session, weekly)
    as a 0-1 `usage` fraction plus per-model request counts, with no reset
    timestamps. Used when /api/balance shows no included allowance.

For legacy windows, reset times come from a separate, best-effort step (resets.py): a headless
Chromium renders ollama.com/settings with the browser's ollama.com cookie. It
runs after the usage record is written, at most every couple of hours, sooner
only when a known reset has passed or a used window has no reset yet (knowing
when this session resets says nothing about when the next one will). Any
failure there is logged and ignored; it never touches the usage numbers.

Output: ~/.local/state/omarchy/ollama-usage/usage.json, written atomically;
reset times are cached next to it in resets.json.
The script always exits 0: failures are described inside the record so the
panel can show them. A transient failure keeps the last good windows and marks
them stale; an auth failure clears them.

Several bar instances (one per monitor) may call this at once, so runs are
serialized with a lock and skipped when the last check is under a minute old,
unless --force is given.

Environment overrides:
  OLLAMA_API_KEY          API key (default: read from the key file)
  OLLAMA_USAGE_KEY_FILE   key file (default ~/.config/omarchy/ollama-usage/api.key)
  OLLAMA_USAGE_STATE      output file
  OLLAMA_USAGE_ENDPOINT   API base URL (e.g. http://127.0.0.1:8000/api; /balance
                          and /usage are appended), honored only with
                          OLLAMA_USAGE_TEST=1 so a stray inherited variable
                          can't send the key elsewhere
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import http.client
import json
import os
import signal
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
# Current plans: /api/balance holds the monthly included credits (allowance,
# balance left, billing period) and any purchased credits; /api/usage, grouped
# by model, holds daily request counts and spend. Legacy plans had session and
# weekly `limits` in a bare /api/usage answer, which stays as the fallback.
API_BASE = "https://ollama.com/api"
if os.environ.get("OLLAMA_USAGE_TEST") == "1" and os.environ.get("OLLAMA_USAGE_ENDPOINT"):
  API_BASE = os.environ["OLLAMA_USAGE_ENDPOINT"].rstrip("/")
BALANCE_URL = f"{API_BASE}/balance"
USAGE_URL = f"{API_BASE}/usage"
MODEL_USAGE_URL = f"{USAGE_URL}?range=30d&group_by=model"
KEY_FILE = Path(os.environ.get("OLLAMA_USAGE_KEY_FILE") or Path.home() / ".config/omarchy/ollama-usage/api.key")
STATE_FILE = Path(
  os.environ.get("OLLAMA_USAGE_STATE")
  or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omarchy/ollama-usage/usage.json"
)
RESETS_FILE = STATE_FILE.parent / "resets.json"
HTTP_TIMEOUT_SEC = 15
MAX_RESPONSE_BYTES = 1024 * 1024
MIN_INTERVAL_SEC = 60
# Reset scraping schedule (see module docstring).
RESETS_REFRESH_SEC = 2 * 3600
RESETS_RETRY_SEC = 10 * 60
RESETS_FAILURE_BACKOFF_SEC = 30 * 60
AUTH_HELP = f"Create an API key at ollama.com/settings/keys and save it to {KEY_FILE} (chmod 600)."

# The API lists some tools next to models (e.g. "web search"); the panel shows
# them apart so they don't skew the model share bars.
TOOLS = {"web search", "web fetch"}

# Display order and titles; windows the API doesn't return are skipped.
WINDOWS = (("session", "Session"), ("weekly", "Weekly"), ("monthly", "Monthly"))


def now_iso() -> str:
  return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def open_regular(path: Path, flags: int, follow: bool = False) -> int:
  """Open path without truncating it, refusing FIFOs, devices and (unless
  follow) symlinks, so a planted file can't block or redirect the collector."""
  flags |= os.O_NONBLOCK | os.O_CLOEXEC
  if not follow:
    flags |= os.O_NOFOLLOW
  fd = os.open(path, flags, 0o600)
  try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
      raise OSError(f"{path} is not a regular file")
    os.set_blocking(fd, True)
    return fd
  except BaseException:
    os.close(fd)
    raise


def read_regular(path: Path, follow: bool = False, limit: int = 1 << 20) -> str:
  with os.fdopen(open_regular(path, os.O_RDONLY, follow), "rb") as handle:
    return handle.read(limit).decode("utf-8")


def api_key() -> str:
  key = os.environ.get("OLLAMA_API_KEY", "").strip()
  if key:
    return key
  try:
    if KEY_FILE.stat().st_mode & 0o077:
      print(f"ollama-usage: warning: {KEY_FILE} is readable by other users; chmod 600 it", file=sys.stderr)
    return read_regular(KEY_FILE, follow=True).strip()
  except (OSError, UnicodeDecodeError):
    return ""


def fetch(key: str, url: str) -> dict:
  request = urllib.request.Request(
    url,
    headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "omarchy-ollama-cloud-usage-bar-widget"},
  )
  try:
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
      body = response.read(MAX_RESPONSE_BYTES + 1)
  except http.client.HTTPException as error:  # e.g. a body cut off mid-read
    raise OSError(f"broken response: {type(error).__name__}") from error
  if len(body) > MAX_RESPONSE_BYTES:
    raise ValueError("response too large")
  try:
    payload = json.loads(body)
  except RecursionError as error:
    raise ValueError("response nested too deeply") from error
  if not isinstance(payload, dict):
    raise ValueError("unexpected response shape")
  return payload


def fraction(value) -> float | None:
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  if number != number or number < 0 or number == float("inf"):  # NaN, negative, infinite
    return None
  # Defensive: accept a percent-scaled value if the API ever switches.
  return number / 100 if number > 1.5 else number


def model_rows(raw) -> list[dict]:
  counts: dict[str, int] = {}
  for entry in raw if isinstance(raw, list) else []:
    if not isinstance(entry, dict):
      continue
    # Legacy limits name a model `name`; usage groups name it `model`.
    name = entry.get("name") or entry.get("model")
    if not isinstance(name, str) or not name:
      continue
    try:
      requests = max(0, int(entry.get("request_count") or 0))
    except (TypeError, ValueError, OverflowError):
      continue
    name = name[:200]
    counts[name] = counts.get(name, 0) + requests
  rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:50]
  return [{"name": name, "requests": n, "tool": name.lower() in TOOLS} for name, n in rows]


def usd(value, signed: bool = False) -> float | None:
  """A finite dollar amount, or None. Only `signed` amounts may be negative
  (a balance overdrawn past the allowance)."""
  if isinstance(value, bool):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  low = -1e9 if signed else 0
  return number if number == number and low <= number < 1e9 else None


def period_models(usage: dict, since: dt.datetime) -> list[dict]:
  """Per-model request counts from the daily buckets that overlap the billing
  period (the day it started counts whole: buckets are UTC days)."""
  groups = []
  for bucket in usage.get("buckets") if isinstance(usage.get("buckets"), list) else []:
    if not isinstance(bucket, dict):
      continue
    # A bucket ends at `until`; with none, its start is the best bound there is.
    end = parse_time(bucket.get("until")) or parse_time(bucket.get("from"))
    if not end or end <= since:
      continue
    groups.extend(bucket.get("groups") if isinstance(bucket.get("groups"), list) else [])
  return model_rows(groups)


def included_window(key: str, previous: dict) -> dict | None:
  """The monthly included-credits window from /api/balance, or None when the
  account has no included allowance (a legacy plan) or the endpoint isn't
  there (HTTP 400/404/405), so the caller falls back to the legacy limits."""
  try:
    balance = fetch(key, BALANCE_URL)
  except urllib.error.HTTPError as error:
    if error.code in (400, 404, 405):
      return None
    raise
  included = balance.get("included") if isinstance(balance.get("included"), dict) else {}
  allowance, left = usd(included.get("allowance_usd")), usd(included.get("balance_usd"), signed=True)
  if not allowance or left is None:
    return None
  period = included.get("period") if isinstance(included.get("period"), dict) else {}
  start, end = parse_time(period.get("from")), parse_time(period.get("until"))
  used = max(0.0, allowance - left)
  purchased = balance.get("purchased") if isinstance(balance.get("purchased"), dict) else {}
  window = {
    "id": "included",
    "title": "Included credits",
    "percent": min(used / allowance, 1.5),
    "detail": f"${used:,.2f} of ${allowance:,.2f} used",
    "purchasedUsd": usd(purchased.get("balance_usd")) or 0,
    "models": [],
    "requests": 0,
    # The settings-page scrape can't learn this window's reset, so it never
    # runs for it, even when the API leaves the period out.
    "resetSource": "api",
  }
  if end:
    window["resetsAt"] = end.isoformat(timespec="seconds")
  # Without a period start there's no telling which requests belong to it, so
  # no model list rather than a mislabeled one.
  if start:
    try:
      window["models"] = period_models(fetch(key, MODEL_USAGE_URL), start)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
      # The meter stands on its own; keep the last model list rather than
      # blanking it until the next refresh.
      print(f"ollama-usage: model counts unavailable: {getattr(error, 'reason', error)}", file=sys.stderr)
      for old in previous.get("windows") or []:
        if isinstance(old, dict) and old.get("id") == "included" and isinstance(old.get("models"), list):
          window["models"] = old["models"]
  window["requests"] = sum(int(m.get("requests") or 0) for m in window["models"] if isinstance(m, dict))
  return window


def windows_from(payload: dict) -> list[dict]:
  limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
  out = []
  for window_id, title in WINDOWS:
    window = limits.get(window_id)
    if not isinstance(window, dict):
      continue
    percent = fraction(window.get("usage"))
    if percent is None:
      continue
    models = model_rows(window.get("models"))
    out.append({
      "id": window_id,
      "title": title,
      "percent": percent,
      "requests": sum(m["requests"] for m in models),
      "models": models,
    })
  return out


def read_json(path: Path) -> dict:
  try:
    record = json.loads(read_regular(path))
    return record if isinstance(record, dict) else {}
  except (OSError, ValueError):
    return {}


def previous_record() -> dict:
  return read_json(STATE_FILE)


def parse_time(value) -> dt.datetime | None:
  try:
    parsed = dt.datetime.fromisoformat(str(value))
  except ValueError:
    return None
  return parsed if parsed.tzinfo else None


def age_sec(value) -> float:
  parsed = parse_time(value)
  return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() if parsed else float("inf")


def upcoming_reset(resets: dict, window_id: str) -> str:
  """The cached reset time for a window, or "" once it has passed: the next
  window's reset is unknown until the page is read again."""
  value = (resets.get("resets") or {}).get(window_id) if isinstance(resets.get("resets"), dict) else None
  return str(value) if value and age_sec(value) < 0 else ""


def with_resets(windows: list[dict], resets: dict) -> list[dict]:
  out = []
  for window in windows:
    if window.get("resetSource") == "api":
      out.append(window)  # ollama.com said when; nothing to scrape
      continue
    window = {key: value for key, value in window.items() if key != "resetsAt"}
    reset = upcoming_reset(resets, str(window.get("id")))
    if reset:
      window["resetsAt"] = reset
    out.append(window)
  return out


def resets_due(windows: list[dict], resets: dict) -> bool:
  if not resets.get("checkedAt"):
    return True
  since_try = age_sec(resets.get("checkedAt"))
  if not resets.get("ok"):
    return since_try >= RESETS_FAILURE_BACKOFF_SEC
  if since_try < RESETS_RETRY_SEC:
    return False
  if since_try >= RESETS_REFRESH_SEC:
    return True
  known = resets.get("resets") if isinstance(resets.get("resets"), dict) else {}
  for window in windows:
    window_id = str(window.get("id"))
    # Windows the page doesn't show can't be learned; don't keep trying.
    if window_id not in known:
      continue
    if known[window_id] and not upcoming_reset(resets, window_id):
      return True  # the window rolled over
    if not known[window_id] and (fraction(window.get("percent")) or 0) > 0:
      return True  # a window started since the page was last read
  return False


def build_record(previous: dict) -> dict:
  checked = now_iso()
  base = {"schemaVersion": SCHEMA_VERSION, "checkedAt": checked, "error": "", "authHelp": ""}

  key = api_key()
  if not key:
    return {**base, "ok": False, "stale": False, "updatedAt": "", "windows": [], "authHelp": AUTH_HELP}

  try:
    included = included_window(key, previous)
    windows = [included] if included else windows_from(fetch(key, USAGE_URL))
    if not windows:
      raise ValueError("no included credits or usage limits in response; the API may have changed")
    return {**base, "ok": True, "stale": False, "updatedAt": checked, "windows": with_resets(windows, read_json(RESETS_FILE))}
  except urllib.error.HTTPError as error:
    if error.code in (401, 403):
      return {**base, "ok": False, "stale": False, "updatedAt": "", "windows": [], "error": f"ollama.com rejected the API key (HTTP {error.code})", "authHelp": AUTH_HELP}
    message = f"ollama.com returned HTTP {error.code}"
  except (urllib.error.URLError, TimeoutError, OSError) as error:
    reason = getattr(error, "reason", error)
    message = f"Could not reach ollama.com ({reason})"
  except ValueError as error:
    message = f"Unreadable response from ollama.com ({error})"

  # Transient failure (network, 429, 5xx, bad body): keep the last good
  # numbers, flagged as stale.
  return {
    **base,
    "ok": bool(previous.get("windows")),
    "stale": True,
    "updatedAt": str(previous.get("updatedAt") or ""),
    "windows": with_resets(previous.get("windows") or [], read_json(RESETS_FILE)),
    "error": message,
  }


def write_atomic(record: dict, path: Path = STATE_FILE) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
  try:
    with os.fdopen(fd, "w") as handle:
      json.dump(record, handle, indent=2)
      handle.write("\n")
    os.replace(tmp, path)
  except BaseException:
    Path(tmp).unlink(missing_ok=True)
    raise


def recently_checked(previous: dict) -> bool:
  try:
    checked = dt.datetime.fromisoformat(str(previous.get("checkedAt")))
  except ValueError:
    return False
  return (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() < MIN_INTERVAL_SEC


def open_lock(name: str):
  lock_path = STATE_FILE.parent / name
  return os.fdopen(open_regular(lock_path, os.O_RDWR | os.O_CREAT), "r+b")


def update_resets(force: bool) -> None:
  """Best-effort reset-time refresh; never raises."""
  try:
    with open_lock(".resets.lock") as lock:
      try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        return  # another bar instance is already on it
      cached = read_json(RESETS_FILE)
      if not force and not resets_due(previous_record().get("windows") or [], cached):
        return
      entry = {"checkedAt": now_iso(), "ok": False, "error": "", "resets": cached.get("resets") or {}}
      try:
        sys.dont_write_bytecode = True  # a __pycache__ write would hot-reload the plugin
        import resets
        entry["resets"] = resets.scrape()
        entry["ok"] = True
      except Exception as error:
        entry["error"] = str(error) or type(error).__name__
        print(f"ollama-usage: reset times unavailable: {entry['error']}", file=sys.stderr)
      write_atomic(entry, RESETS_FILE)
    with open_lock(".lock") as lock:
      fcntl.flock(lock, fcntl.LOCK_EX)
      record = previous_record()
      if record.get("windows"):
        record["windows"] = with_resets(record["windows"], entry)
        write_atomic(record)
  except Exception as error:
    print(f"ollama-usage: reset times skipped: {error}", file=sys.stderr)


def main() -> int:
  parser = argparse.ArgumentParser(description="Write the Ollama Cloud usage record for the bar widget")
  parser.add_argument("--force", action="store_true", help=f"check even if the last check is under {MIN_INTERVAL_SEC}s old")
  parser.add_argument("--no-resets", action="store_true", help="skip reading reset times from ollama.com/settings")
  parser.add_argument("--resets-now", action="store_true", help="read reset times now, ignoring their schedule")
  args = parser.parse_args()

  # Turn the panel's timeout (SIGTERM) into a normal exit so cleanup runs and
  # a headless browser never outlives us.
  signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

  STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
  try:
    lock_file = open_lock(".lock")
  except OSError as error:
    print(f"ollama-usage: refusing lock file: {error}", file=sys.stderr)
    return 1
  with lock_file as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    previous = previous_record()
    if not args.force and not args.resets_now and recently_checked(previous):
      return 0
    record = build_record(previous)
    write_atomic(record)
  if record["error"] or record["authHelp"]:
    print(f"ollama-usage: {record['error'] or record['authHelp']}", file=sys.stderr)
  scrape_needed = any(w.get("resetSource") != "api" for w in record.get("windows") or [])
  if not args.no_resets and scrape_needed:
    update_resets(args.resets_now)
  return 0


if __name__ == "__main__":
  sys.exit(main())
