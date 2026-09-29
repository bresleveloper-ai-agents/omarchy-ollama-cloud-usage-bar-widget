"""Read Ollama Cloud limit reset times from ollama.com/settings.

/api/usage carries no reset times, but the signed-in settings page does: each
"Session usage" / "Weekly usage" block ends with
  <div class="... local-time" data-time="2026-09-29T15:00:00Z">Resets in 2 hours.</div>
That page needs the ollama.com session cookie, which Chromium keeps encrypted
with the desktop keyring, so a headless Chromium renders it from a throwaway
profile holding only the ollama.com cookie rows.

Adapted from GePi0/omarchy-ai-usage (MIT, (c) 2026 Gerard Piella): browser and
profile discovery, cookie filtering, and the sandboxed, capped render.

Everything here is best-effort. collect.py calls scrape() inside a try block
and ignores any failure.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

SETTINGS_URL = "https://ollama.com/settings"
MAX_DOM_BYTES = 10 * 1024 * 1024
# Total budget for every render attempt together; the panel kills the whole
# collector after 60s.
DEADLINE_SEC = 25

# (binary candidates in preference order, config dir under ~/.config)
BROWSERS = [
  (["google-chrome-stable", "google-chrome", "chrome"], "google-chrome"),
  (["chromium", "chromium-browser"], "chromium"),
  (["brave-browser", "brave"], "BraveSoftware/Brave-Browser"),
]

# Page section heading -> window id used by /api/usage.
SECTIONS = {"Session usage": "session", "Weekly usage": "weekly", "Monthly usage": "monthly"}
UNIT_SEC = {"minute": 60, "hour": 3600, "day": 86400}


def browsers() -> list[tuple[str, Path]]:
  out = []
  for names, dirname in BROWSERS:
    for name in names:
      path = shutil.which(name)
      if path:
        out.append((path, Path.home() / ".config" / dirname))
        break
  return out


def has_ollama_cookies(db: Path) -> bool:
  try:
    with sqlite3.connect(f"file:{db}?immutable=1", uri=True) as conn:
      row = conn.execute("SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%ollama.com'").fetchone()
      return bool(row and row[0])
  except sqlite3.Error:
    return False


def profiles(config_dir: Path) -> list[Path]:
  """Profiles holding an ollama.com session, most recently used first."""
  if not config_dir.is_dir():
    return []
  dbs = [config_dir / "Default" / "Cookies", *sorted(config_dir.glob("Profile */Cookies"))]
  found = [db for db in dbs if db.is_file() and has_ollama_cookies(db)]
  return [db.parent for db in sorted(found, key=lambda db: db.stat().st_mtime, reverse=True)]


def copy_ollama_cookies(src: Path, dst: Path) -> int:
  """Copy the cookie DB keeping only ollama.com rows, so the headless
  browser never sees the user's other sessions."""
  with sqlite3.connect(f"file:{src}?immutable=1", uri=True) as conn:
    rows = conn.execute("SELECT * FROM cookies WHERE host_key LIKE '%ollama.com'").fetchall()
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='cookies'").fetchone()
  if not rows or not schema or not schema[0]:
    return 0
  with sqlite3.connect(dst) as out:
    out.execute(schema[0])
    out.executemany(f"INSERT INTO cookies VALUES ({','.join('?' * len(rows[0]))})", rows)
  return len(rows)


def copy_crypt_key(src: Path, dst: Path) -> None:
  """Keep only the cookie decryption key material from Local State."""
  try:
    data = json.loads(src.read_text("utf-8"))
  except (OSError, ValueError):
    return
  if isinstance(data, dict) and isinstance(data.get("os_crypt"), dict):
    dst.write_text(json.dumps({"os_crypt": data["os_crypt"]}), "utf-8")


def kill_group(proc: subprocess.Popen) -> None:
  """Chromium forks helpers (zygote, crashpad) that would outlive it and keep
  the pipe open; the browser leads its own session, so kill the group."""
  try:
    os.killpg(proc.pid, signal.SIGKILL)
  except (ProcessLookupError, PermissionError):
    pass
  try:
    proc.wait(timeout=5)
  except subprocess.TimeoutExpired:
    pass


def render(binary: str, config_dir: Path, profile_dir: Path, timeout: float) -> str:
  with tempfile.TemporaryDirectory(prefix="ollama-usage-") as tmp:
    profile = Path(tmp) / "Default"
    profile.mkdir()
    if not copy_ollama_cookies(profile_dir / "Cookies", profile / "Cookies"):
      raise RuntimeError("no ollama.com cookies")
    copy_crypt_key(config_dir / "Local State", Path(tmp) / "Local State")
    command = [
      binary, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-sync",
      "--disable-extensions", f"--user-data-dir={tmp}", "--virtual-time-budget=10000",
      "--dump-dom", f"{SETTINGS_URL}?nonce={uuid.uuid4().hex[:8]}",
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
      out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      raise RuntimeError("headless browser timed out")
    finally:
      kill_group(proc)
  if len(out) > MAX_DOM_BYTES:
    raise RuntimeError("page too large")
  dom = out.decode("utf-8", "replace")
  if proc.returncode != 0 or len(dom) < 2000:
    raise RuntimeError(f"headless browser failed (exit {proc.returncode})")
  if "signin.ollama.com" in dom or not any(heading in dom for heading in SECTIONS):
    raise RuntimeError("signed out of ollama.com in this browser profile")
  return dom


def iso_utc(value: dt.datetime) -> str:
  return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def relative_reset(text: str, now: dt.datetime) -> str:
  """Fallback: 'Resets in 1 day, 2 hours.' -> ISO time (coarse)."""
  seconds = sum(float(n) * UNIT_SEC[u.lower()] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s+(minute|hour|day)s?\b", text, re.I))
  if not seconds and re.search(r"less than a minute", text, re.I):
    seconds = 30
  elif not seconds:
    m = re.search(r"\ban?\s+(minute|hour|day)\b", text, re.I)
    seconds = UNIT_SEC[m.group(1).lower()] if m else 0
  return iso_utc(now + dt.timedelta(seconds=seconds)) if seconds else ""


def parse(dom: str, now: dt.datetime) -> dict[str, str]:
  """Window id -> reset time (ISO, UTC) for every section on the page; ""
  when the section is there but shows no reset (e.g. nothing used yet)."""
  marks = sorted((m.start(), SECTIONS[m.group(0)]) for m in re.finditer("|".join(SECTIONS), dom))
  out: dict[str, str] = {}
  for i, (start, window_id) in enumerate(marks):
    if window_id in out:
      continue
    # A heading repeats inside its own block (aria-label="Session usage 13%
    # used"), so a block runs to the next *different* heading.
    end = next((pos for pos, other in marks[i + 1:] if other != window_id), len(dom))
    block = dom[start:end]
    reset = ""
    m = re.search(r'data-time="([^"]+)"', block)
    if m:
      try:
        reset = iso_utc(dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")))
      except ValueError:
        pass
    if not reset:
      m = re.search(r"Resets in[^<]*", block)
      reset = relative_reset(m.group(0), now) if m else ""
    out[window_id] = reset
  return out


def scrape() -> dict[str, str]:
  """Reset times per window, or RuntimeError describing why not."""
  deadline = time.monotonic() + DEADLINE_SEC
  errors = []
  found = browsers()
  if not found:
    raise RuntimeError("no Chromium-based browser found")
  for binary, config_dir in found:
    candidates = profiles(config_dir)
    if not candidates:
      errors.append(f"{Path(binary).name}: not signed in to ollama.com")
    for profile in candidates:
      remaining = deadline - time.monotonic()
      if remaining < 3:
        raise RuntimeError("; ".join(errors + ["out of time"]))
      try:
        return parse(render(binary, config_dir, profile, remaining), dt.datetime.now(dt.timezone.utc))
      except (RuntimeError, OSError, sqlite3.Error) as error:
        errors.append(f"{Path(binary).name}/{profile.name}: {error}")
  raise RuntimeError("; ".join(errors))
