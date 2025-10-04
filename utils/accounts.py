import json
import hashlib
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict

from .sora_client import validate_cookies
from config import PROXY_URL

from .db import (
    add_account_row,
    list_accounts_counts,
    acquire_account_for_generation,
    increment_daily_generation,
    decrement_active_generation,
    reset_daily_where_needed,
    get_account_id_by_key,
    list_all_accounts_minimal,
    set_daily_generations,
)


DAILY_LIMIT = 100
CONCURRENCY_LIMIT = 5


class DuplicateAccountError(ValueError):
    pass


def _canonicalize_cookies(cookies_json: str) -> str:
    """Return a canonical JSON string for cookies to compare equality.

    Canonicalization rule:
      - Expect a list of cookie objects
      - For each cookie keep only: name, value, domain, path
      - Normalize domain/path to lowercase
      - Sort items by (domain, path, name)
      - Dump with sorted keys and no spaces
    """
    data = json.loads(cookies_json)
    if not isinstance(data, list):
        raise ValueError("cookies_json must be a JSON array")
    norm = []
    for c in data:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = (c.get("domain") or "").lower()
        path = (c.get("path") or "/").lower()
        if name is None or value is None:
            continue
        norm.append({
            "domain": domain,
            "path": path,
            "name": str(name),
            "value": str(value),
        })
    norm.sort(key=lambda x: (x["domain"], x["path"], x["name"]))
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        import base64
        def _b64fix(s: str) -> bytes:
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8"))
        payload_raw = _b64fix(parts[1])
        return json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return {}


def add_account(cookies_json: str) -> int:
    """Add a new account into the pool with default counters.

    cookies_json must be a JSON array of cookie objects (exported from a browser).
    Returns the new account id.
    """
    # Validate basic JSON structure early
    try:
        data = json.loads(cookies_json)
        if not isinstance(data, list):
            raise ValueError("cookies_json must be a JSON array")
    except Exception as e:
        raise ValueError(f"Invalid cookies_json: {e}")
    # Minimal cookies validity check by attempting to fetch access token via Sora
    # Also extract a stable account key from the token if possible.
    try:
        token = validate_cookies(cookies_json, proxy=PROXY_URL)
    except Exception as e:
        raise ValueError(f"Cookie validation failed: {e}")

    payload = _decode_jwt_payload(token or "")
    # Prefer stable identifiers from JWT payload
    account_key = None
    for k in ("email", "user_id", "userId", "sub", "uid"):
        v = payload.get(k) if isinstance(payload, dict) else None
        if isinstance(v, str) and v.strip():
            account_key = v.strip().lower() if k == "email" else v.strip()
            break

    # If we cannot extract a stable key, fall back to canonical cookies hash
    if not account_key:
        try:
            canon = _canonicalize_cookies(cookies_json)
            account_key = "cookiehash:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
        except Exception:
            # Should not happen given earlier parse, but keep safe fallback
            account_key = None

    # Check duplicates by account_key when available
    if account_key:
        existed_id = get_account_id_by_key(account_key)
        if existed_id is not None:
            raise DuplicateAccountError("Account already exists in the database")

    # Extra guard: check for exact cookies duplicate (canonicalized)
    try:
        new_canon = _canonicalize_cookies(cookies_json)
    except Exception:
        new_canon = None
    if new_canon is not None:
        for _id, existing_json, _akey in list_all_accounts_minimal():
            try:
                if _canonicalize_cookies(existing_json) == new_canon:
                    raise DuplicateAccountError("Cookies already present in the database")
            except Exception:
                # ignore malformed rows
                continue

    return add_account_row(cookies_json, account_key=account_key)


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pick_account_for_generation() -> Tuple[Optional[Dict], Optional[str]]:
    """Pick an account ready for generation and increment its active counter.

    Returns (account_dict, error_key). If account_dict is None, error_key is one of:
      - 'no_accounts'          -> no accounts configured at all
      - 'daily_limit_all'      -> every account reached daily limit
      - 'no_active_slots'      -> daily available accounts exist, but no free active slots (<5)
    """
    today_str = _today_utc_str()
    now_iso = _now_iso()

    # Reset daily counters as needed (cheap global pass)
    reset_daily_where_needed(today_str)

    total, available_daily, available_slots = list_accounts_counts(DAILY_LIMIT, CONCURRENCY_LIMIT)
    if total == 0:
        return None, "no_accounts"
    if available_daily == 0:
        return None, "daily_limit_all"
    if available_slots == 0:
        return None, "no_active_slots"

    acc = acquire_account_for_generation(today_str, now_iso, DAILY_LIMIT, CONCURRENCY_LIMIT)
    if acc is None:
        # Race condition: treat as no_active_slots for caller UX
        return None, "no_active_slots"
    return acc, None


def mark_generation_created(acc_id: int) -> None:
    """Call right after the create task API succeeded (task accepted)."""
    increment_daily_generation(acc_id, _today_utc_str(), _now_iso())


def mark_generation_finished(acc_id: int) -> None:
    """Call in finally: any outcome (success or error)."""
    decrement_active_generation(acc_id)


def mark_account_daily_exhausted(acc_id: int) -> None:
    """Mark account as having reached the DAILY_LIMIT for today.

    Used when Sora API returns a daily-limit error so our selector avoids this account until tomorrow.
    """
    set_daily_generations(acc_id, DAILY_LIMIT, _today_utc_str(), _now_iso())
