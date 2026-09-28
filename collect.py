#!/usr/bin/env python3
"""Fetch Ollama Cloud usage and write it where the Ollama Cloud usage bar widget reads it.

Source: GET https://ollama.com/api/usage with a Bearer API key. The endpoint
is undocumented; it reports each limit window (session, weekly, sometimes
monthly) as a 0-1 `usage` fraction plus per-model request counts. It carries
no reset timestamps.

Output: ~/.local/state/omarchy/ollama-usage/usage.json, written atomically.
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
  OLLAMA_USAGE_ENDPOINT   endpoint URL, honored only with OLLAMA_USAGE_TEST=1 so
                          a stray inherited variable can't send the key elsewhere
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
ENDPOINT = "https://ollama.com/api/usage"
if os.environ.get("OLLAMA_USAGE_TEST") == "1" and os.environ.get("OLLAMA_USAGE_ENDPOINT"):
  ENDPOINT = os.environ["OLLAMA_USAGE_ENDPOINT"]
KEY_FILE = Path(os.environ.get("OLLAMA_USAGE_KEY_FILE") or Path.home() / ".config/omarchy/ollama-usage/api.key")
STATE_FILE = Path(
  os.environ.get("OLLAMA_USAGE_STATE")
  or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omarchy/ollama-usage/usage.json"
)
HTTP_TIMEOUT_SEC = 15
MAX_RESPONSE_BYTES = 1024 * 1024
MIN_INTERVAL_SEC = 60
AUTH_HELP = f"Create an API key at ollama.com/settings/keys and save it to {KEY_FILE} (chmod 600)."

# The API lists some tools next to models (e.g. "web search"); the panel shows
# them apart so they don't skew the model share bars.
TOOLS = {"web search", "web fetch"}

# Display order and titles; windows the API doesn't return are skipped.
WINDOWS = (("session", "Session"), ("weekly", "Weekly"), ("monthly", "Monthly"))


def now_iso() -> str:
  return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def api_key() -> str:
  key = os.environ.get("OLLAMA_API_KEY", "").strip()
  if key:
    return key
  try:
    if KEY_FILE.stat().st_mode & 0o077:
      print(f"ollama-usage: warning: {KEY_FILE} is readable by other users; chmod 600 it", file=sys.stderr)
    return KEY_FILE.read_text().strip()
  except OSError:
    return ""


def fetch(key: str) -> dict:
  request = urllib.request.Request(
    ENDPOINT,
    headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "omarchy-ollama-cloud-usage-bar-widget"},
  )
  with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
    body = response.read(MAX_RESPONSE_BYTES + 1)
  if len(body) > MAX_RESPONSE_BYTES:
    raise ValueError("response too large")
  payload = json.loads(body)
  if not isinstance(payload, dict):
    raise ValueError("unexpected response shape")
  return payload


def fraction(value) -> float | None:
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  if number != number or number < 0:  # NaN or negative
    return None
  # Defensive: accept a percent-scaled value if the API ever switches.
  return number / 100 if number > 1.5 else number


def model_rows(raw) -> list[dict]:
  counts: dict[str, int] = {}
  for entry in raw if isinstance(raw, list) else []:
    if not isinstance(entry, dict) or not entry.get("name"):
      continue
    try:
      requests = max(0, int(entry.get("request_count") or 0))
    except (TypeError, ValueError):
      continue
    name = str(entry["name"])
    counts[name] = counts.get(name, 0) + requests
  rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
  return [{"name": name, "requests": n, "tool": name.lower() in TOOLS} for name, n in rows]


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


def previous_record() -> dict:
  try:
    record = json.loads(STATE_FILE.read_text())
    return record if isinstance(record, dict) else {}
  except (OSError, ValueError):
    return {}


def build_record(previous: dict) -> dict:
  checked = now_iso()
  base = {"schemaVersion": SCHEMA_VERSION, "checkedAt": checked, "error": "", "authHelp": ""}

  key = api_key()
  if not key:
    return {**base, "ok": False, "stale": False, "updatedAt": "", "windows": [], "authHelp": AUTH_HELP}

  try:
    windows = windows_from(fetch(key))
    if not windows:
      raise ValueError("no usage windows in response; the API may have changed")
    return {**base, "ok": True, "stale": False, "updatedAt": checked, "windows": windows}
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
    "windows": previous.get("windows") or [],
    "error": message,
  }


def write_atomic(record: dict) -> None:
  STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".usage.", suffix=".tmp")
  try:
    with os.fdopen(fd, "w") as handle:
      json.dump(record, handle, indent=2)
      handle.write("\n")
    os.replace(tmp, STATE_FILE)
  except BaseException:
    Path(tmp).unlink(missing_ok=True)
    raise


def recently_checked(previous: dict) -> bool:
  try:
    checked = dt.datetime.fromisoformat(str(previous.get("checkedAt")))
  except ValueError:
    return False
  return (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() < MIN_INTERVAL_SEC


def main() -> int:
  parser = argparse.ArgumentParser(description="Write the Ollama Cloud usage record for the bar widget")
  parser.add_argument("--force", action="store_true", help=f"check even if the last check is under {MIN_INTERVAL_SEC}s old")
  args = parser.parse_args()

  STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
  with open(STATE_FILE.parent / ".lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    previous = previous_record()
    if not args.force and recently_checked(previous):
      return 0
    record = build_record(previous)
    write_atomic(record)
  if record["error"] or record["authHelp"]:
    print(f"ollama-usage: {record['error'] or record['authHelp']}", file=sys.stderr)
  return 0


if __name__ == "__main__":
  sys.exit(main())
