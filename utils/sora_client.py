import asyncio
import re
import json
import mimetypes
 
import time
import threading
from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp
from yarl import URL

# Global debug switch (as requested)
debug = False


def _dbg(msg: str, *args: Any) -> None:
    if not debug:
        return
    try:
        ts = time.strftime("%H:%M:%S")
        line = msg % args if args else msg
        print(f"[SORA DEBUG {ts}] {line}")
    except Exception:
        # Avoid breaking runtime if formatting fails
        try:
            print(f"[SORA DEBUG] {msg}")
        except Exception:
            pass


SORA_BASE = "https://sora.chatgpt.com"

# Единый HTTPS-прокси для всех запросов
DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    "referer": f"{SORA_BASE}/drafts",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-gpc": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "origin": SORA_BASE,
}

# Module load log
_dbg("module_loaded: debug=%s base=%s", str(debug), SORA_BASE)



def _load_cookies_from_json(cookies_json: str) -> Dict[str, Dict[str, str]]:
    _dbg("_load_cookies_from_json: size=%d", len(cookies_json) if isinstance(cookies_json, str) else -1)
    try:
        raw = json.loads(cookies_json)
    except Exception as e:
        _dbg("_load_cookies_from_json: parse failed: %s", repr(e))
        return {}

    jar_like: Dict[str, Dict[str, str]] = {}
    def _valid_cookie_name(n: str) -> bool:
        return bool(re.match(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$", n))

    for c in raw:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain") or "sora.chatgpt.com"
        path = c.get("path") or "/"
        if not name or value is None or not _valid_cookie_name(str(name)):
            continue
        if "chatgpt.com" not in domain:
            continue
        key = f"{domain}|{path}"
        jar_like.setdefault(key, {})[name] = value
    _dbg("_load_cookies_from_json: domains/paths loaded -> %s", list(jar_like.keys()))
    return jar_like


class _AsyncBrowserSession:
    def __init__(self, proxy: Optional[str] = None, cookies_json: Optional[str] = None) -> None:
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self._token_exp_ts: Optional[float] = None  # JWT exp (unix seconds)
        self._refresh_lock: Optional[asyncio.Lock] = asyncio.Lock()
        if not cookies_json:
            raise ValueError("cookies_json is required; local cookies.json is no longer supported")
        self.cookies_map = _load_cookies_from_json(cookies_json)
        self.proxy: Optional[str] = proxy
        _dbg("_AsyncBrowserSession.__init__: cookies_map keys: %d", len(self.cookies_map))

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session and not self.session.closed:
            _dbg("_ensure_session: reuse existing session")
            return self.session
        jar = aiohttp.CookieJar(unsafe=True)
        hdrs = dict(DEFAULT_HEADERS)
        _dbg("_ensure_session: creating session with headers: %s", list(hdrs.keys()))
        # Создаём сессию. Если указан socks-прокси, используем специальный коннектор.
        connector = None
        if self.proxy and self.proxy.lower().startswith("socks"):
            try:
                # Normalize non-standard scheme like "socks://" to "socks5://"
                proxy_url = self.proxy
                if proxy_url.lower().startswith("socks://"):
                    proxy_url = "socks5://" + proxy_url.split("://", 1)[1]
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url)
                _dbg("_ensure_session: using socks proxy connector")
            except Exception as e:
                _dbg("_ensure_session: failed to init socks proxy: %s", repr(e))
        self.session = aiohttp.ClientSession(cookie_jar=jar, headers=hdrs, connector=connector)
        # Populate cookies
        for key, cookies in self.cookies_map.items():
            domain, path = key.split("|", 1)
            # Cookies exported with a leading dot (e.g. .chatgpt.com) are domain cookies
            # which should be visible to subdomains like sora.chatgpt.com.
            # aiohttp.CookieJar.update_cookies cannot set a Domain attribute directly,
            # so we seed cookies for relevant hosts explicitly to ensure they are sent.
            base_host = domain.lstrip(".")
            targets = {base_host}
            # Always ensure cookies are also available for the Sora host
            if base_host.endswith("chatgpt.com") and base_host != "sora.chatgpt.com":
                targets.add("sora.chatgpt.com")
            # For safety, if the domain itself is sora.chatgpt.com, targets already includes it
            for host in targets:
                resp_url = URL.build(scheme="https", host=host, path=path or "/")
                for name, value in cookies.items():
                    jar.update_cookies({name: value}, response_url=resp_url)
        _dbg("_ensure_session: cookies set -> %d domains/paths", len(self.cookies_map))
        return self.session

    async def _ensure_access_token(self) -> str:
        # If we have a token and it's not near expiry, reuse
        if self.access_token and self._token_exp_ts:
            now = time.time()
            if now < (self._token_exp_ts - 60):  # refresh 60s before expiry
                _dbg("_ensure_access_token: token cached & valid (len=%d)", len(self.access_token))
                return self.access_token
            _dbg("_ensure_access_token: token expiring soon -> refresh")
        elif self.access_token and not self._token_exp_ts:
            # Unknown expiry, try to decode and otherwise continue
            try:
                self._token_exp_ts = _decode_jwt_exp(self.access_token)
                now = time.time()
                if self._token_exp_ts and now < (self._token_exp_ts - 60):
                    _dbg("_ensure_access_token: token cached & valid (no exp cached before)")
                    return self.access_token
            except Exception:
                pass

        # Refresh or acquire a token
        await self._refresh_access_token(force=True)
        return self.access_token  # type: ignore[return-value]

    async def _refresh_access_token(self, force: bool = False) -> None:
        # Ensure session exists
        sess = await self._ensure_session()
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            # Double-check inside the lock to avoid redundant refreshes
            if not force and self.access_token and self._token_exp_ts:
                now = time.time()
                if now < (self._token_exp_ts - 60):
                    return

            url = f"{SORA_BASE}/api/auth/session"
            _dbg("_refresh_access_token: GET %s", url)
            kwargs: Dict[str, Any] = {}
            if self.proxy and not (self.proxy.lower().startswith("socks")):
                kwargs["proxy"] = self.proxy
            async with sess.get(url, **kwargs) as r:
                _dbg("_refresh_access_token: response status=%d", r.status)
                if r.status != 200:
                    try:
                        body = await r.json()
                    except Exception:
                        body = await r.text()
                    raise RuntimeError(f"auth_session_failed: status={r.status}, response={str(body)[:200]}")
                try:
                    data = await r.json()
                except Exception:
                    body = await r.text()
                    raise RuntimeError(f"auth_session_invalid_json: status={r.status}, response={str(body)[:200]}")

            if not isinstance(data, dict):
                raise RuntimeError(f"auth_session_unexpected_payload: {str(data)[:200]}")

            token = data.get("accessToken")
            if not token:
                raise RuntimeError("auth_session_missing_access_token")

            self.access_token = token
            # Decode exp for proactive refresh if possible
            try:
                self._token_exp_ts = _decode_jwt_exp(self.access_token)
                if self._token_exp_ts:
                    _dbg("_refresh_access_token: token exp at %d", int(self._token_exp_ts))
            except Exception as e:
                _dbg("_refresh_access_token: failed to decode exp: %s", repr(e))
                self._token_exp_ts = None

            sess.headers["authorization"] = f"Bearer {self.access_token}"
            _dbg("_refresh_access_token: token set (len=%d)", len(self.access_token))
            try:
                device_id = None
                # Prefer current cookies from jar (which may have rotated values)
                # but fall back to initial map
                for _k, _cookies in self.cookies_map.items():
                    if "oai-did" in _cookies:
                        device_id = _cookies.get("oai-did")
                        break
                if device_id:
                    sess.headers.setdefault("oai-device-id", device_id)
                    _dbg("_refresh_access_token: oai-device-id header set")
            except Exception:
                pass
            return

    async def get_json(self, path: str) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{SORA_BASE}{path}"
        _dbg("GET %s", url)
        kwargs: Dict[str, Any] = {}
        if self.proxy and not (self.proxy.lower().startswith("socks")):
            kwargs["proxy"] = self.proxy
        resp = await sess.get(url, **kwargs)
        if resp.status == 401:
            _dbg("GET %s -> 401, attempting token refresh and retry", url)
            try:
                await self._refresh_access_token(force=True)
                resp.release()
                resp = await sess.get(url, **kwargs)
            except Exception as e:
                _dbg("GET retry failed: %s", repr(e))
        _dbg("GET done: status=%d", resp.status)
        return resp

    async def post_json(self, path: str, payload: Dict[str, Any]) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{SORA_BASE}{path}"
        _dbg("POST JSON %s payload_keys=%s", url, list(payload.keys()))
        kwargs: Dict[str, Any] = {}
        if self.proxy and not (self.proxy.lower().startswith("socks")):
            kwargs["proxy"] = self.proxy
        resp = await sess.post(url, json=payload, headers={"content-type": "application/json"}, **kwargs)
        if resp.status == 401:
            _dbg("POST %s -> 401, attempting token refresh and retry", url)
            try:
                await self._refresh_access_token(force=True)
                resp.release()
                resp = await sess.post(url, json=payload, headers={"content-type": "application/json"}, **kwargs)
            except Exception as e:
                _dbg("POST retry failed: %s", repr(e))
        _dbg("POST JSON done: status=%d", resp.status)
        return resp

    async def post_multipart(self, path: str, file_field: str, filename: str, data_bytes: bytes, content_type: str) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{SORA_BASE}{path}"
        form = aiohttp.FormData()
        form.add_field(file_field, data_bytes, filename=filename, content_type=content_type)
        form.add_field("file_name", filename)
        _dbg("POST MULTIPART %s filename=%s content_type=%s size=%d", url, filename, content_type, len(data_bytes))
        kwargs: Dict[str, Any] = {}
        if self.proxy and not (self.proxy.lower().startswith("socks")):
            kwargs["proxy"] = self.proxy
        resp = await sess.post(url, data=form, **kwargs)
        if resp.status == 401:
            _dbg("POST MULTIPART %s -> 401, attempting token refresh and retry", url)
            try:
                await self._refresh_access_token(force=True)
                resp.release()
                resp = await sess.post(url, data=form, **kwargs)
            except Exception as e:
                _dbg("POST MULTIPART retry failed: %s", repr(e))
        _dbg("POST MULTIPART done: status=%d", resp.status)
        return resp


# Minimal cookie validation helpers
async def _validate_cookies_async(cookies_json: str, proxy: Optional[str] = None) -> str:
    """Try to obtain an access token using provided cookies.

    Returns the token string on success, raises on failure.
    """
    client = _AsyncBrowserSession(proxy=proxy, cookies_json=cookies_json)
    try:
        await client._ensure_access_token()
        token = client.access_token or ""
        if not token:
            raise RuntimeError("auth_session_missing_access_token")
        return token
    finally:
        if client.session and not client.session.closed:
            await client.session.close()


def validate_cookies(cookies_json: str, proxy: Optional[str] = None, timeout: float = 20.0) -> str:
    """Blocking helper to validate Sora cookies by fetching an auth token.

    - Returns token string if cookies are valid.
    - Raises an exception with details if validation fails or times out.
    Works safely even when called from within a running asyncio loop.
    """
    if not isinstance(cookies_json, str) or not cookies_json.strip():
        raise ValueError("cookies_json must be a non-empty JSON string")

    result: Dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            tok = asyncio.run(_validate_cookies_async(cookies_json, proxy))
            result["token"] = tok
        except Exception as e:  # propagate real reason
            result["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("auth_session_timeout: validation exceeded timeout")
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return str(result.get("token") or "")


async def _parse_error_resp(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
    try:
        data = await resp.json()
        err = data.get("error") or {}
        _dbg("_parse_error_resp: got JSON body with keys=%s", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
    except Exception as e:
        _dbg("_parse_error_resp: json() failed: %s", repr(e))
        err = {}
        try:
            data = await resp.text()
            _dbg("_parse_error_resp: text() ok, size=%d", len(data) if isinstance(data, str) else -1)
        except Exception as e2:
            _dbg("_parse_error_resp: text() failed: %s", repr(e2))
            data = ""
    out = {
        "http_status": resp.status,
        "type": (err or {}).get("type"),
        "code": (err or {}).get("code"),
        "message": (err or {}).get("message") or (data if isinstance(data, str) else ""),
        "raw": data,
    }
    _dbg("_parse_error_resp: status=%d code=%s message=%s", resp.status, out.get("code"), str(out.get("message"))[:200])
    return out


def _detect_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    _dbg("_detect_mime: %s => %s", path, mime or "application/octet-stream")
    return mime or "application/octet-stream"


def _decode_jwt_exp(token: str) -> Optional[float]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        import base64
        def _b64fix(s: str) -> bytes:
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8"))
        payload_raw = _b64fix(parts[1])
        payload = json.loads(payload_raw.decode("utf-8"))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
        return None
    except Exception:
        return None


 


async def generate_video(
    prompt: str,
    orientation: Optional[str] = None,
    image: Optional[bytes] = None,
    *,
    frames: int,
    size: Optional[str] = None,
    poll_interval_sec: float = 3.0,
    timeout_sec: float = 600.0,
    proxy: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Async generator that:
      1) Ensures authentication via account cookies (from DB) and access token
      2) Optionally uploads an initial photo
      3) Starts generation
      4) Polls drafts until the result is available
      5) Streams status updates via yield

    Args:
      prompt: Text prompt for the video
      orientation: "portrait" or "landscape" (omit if image is provided)
      image: Optional in-memory bytes of initial image
      size: 'small' or 'large' affecting server payload
      poll_interval_sec: Poll interval for status
      timeout_sec: Overall timeout for completion
      proxy: Optional http(s)/socks proxy URL for all requests

    Yields dict events with keys like: {event, status, error, task_id, gen_id, url, ...}
    """

    _dbg(
        "generate_video: start prompt_len=%d orientation=%s image=%s frames=%s poll=%.2fs timeout=%.2fs",
        len(prompt) if isinstance(prompt, str) else -1,
        orientation,
        "<bytes>" if image is not None else None,
        str(frames),
        size,
        poll_interval_sec,
        timeout_sec,
    )
    # Validate inputs
    if not prompt or not isinstance(prompt, str):
        raise ValueError("prompt is required and must be a string")
    if image is None and (orientation not in (None, "portrait", "landscape")):
        raise ValueError("orientation must be 'portrait' or 'landscape' if provided")
    if not isinstance(frames, int) or frames <= 0:
        raise ValueError("frames is required and must be a positive integer")
    if size is not None and str(size).lower() not in ("small", "large"):
        raise ValueError("size must be 'small' or 'large'")

    # Account selection from DB-backed pool
    selected_acc_id: Optional[int] = None
    selected_cookies_json: Optional[str] = None
    # Lazy import to avoid cycles
    try:
        from .accounts import pick_account_for_generation, mark_generation_created, mark_generation_finished, mark_account_daily_exhausted  # type: ignore
        acc, err = pick_account_for_generation()
        if acc is None:
            if err == "daily_limit_all":
                yield {
                    "event": "error",
                    "code": "no_accounts_daily_limit",
                    "message": "Нет свободных аккаунтов. Попробуйте позже",
                }
                return
            else:
                # no_accounts or no_active_slots
                msg = (
                    "Нет свободных аккаунтов. Подождите пару минут и попробуйте снова"
                    if err == "no_active_slots"
                    else "Нет свободных аккаунтов. Попробуйте позже"
                )
                yield {"event": "error", "code": str(err), "message": msg}
                return
        selected_acc_id = int(acc["id"])  # type: ignore[index]
        selected_cookies_json = str(acc["cookies_json"])  # type: ignore[index]
        yield {"event": "account", "account_id": selected_acc_id}
    except Exception as e:
        _dbg("generate_video: account pool unavailable: %s", repr(e))
        yield {"event": "error", "code": "accounts_not_configured", "message": "Пул аккаунтов не настроен"}
        return

    # Ensure auth with selected account cookies
    client = _AsyncBrowserSession(proxy=proxy, cookies_json=selected_cookies_json)
    try:
        _dbg("generate_video: ensuring access token...")
        await client._ensure_access_token()
        _dbg("generate_video: auth ok")
        yield {"event": "auth", "status": "ok"}
    except Exception as e:
        _dbg("generate_video: auth failed: %s", str(e))
        # Free active slot immediately on auth error
        try:
            if selected_acc_id is not None:
                from .accounts import mark_generation_finished  # type: ignore
                mark_generation_finished(selected_acc_id)
        except Exception as _e:
            _dbg("generate_video: failed to mark generation finished after auth error: %s", repr(_e))
        # Close session if it was created to avoid leaks
        if isinstance(client, _AsyncBrowserSession) and client.session and not client.session.closed:
            await client.session.close()
        yield {"event": "error", "code": "auth_failed", "message": str(e)}
        return

    # Step 1: Upload photo if provided
    upload_id: Optional[str] = None
    if image is not None:
        _dbg("generate_video: uploading in-memory image bytes (%d bytes)", len(image))
        try:
            filename = "photo.jpg"
            mime = "image/jpeg"
            data_bytes = image
            resp = await client.post_multipart("/backend/uploads", "file", data_bytes=data_bytes, filename=filename, content_type=mime)
            if resp.status != 200:
                err = await _parse_error_resp(resp)
                # Heuristic error mapping for image problems
                code = err.get("code") or "upload_failed"
                if resp.status == 400 and any(k in (err.get("message") or "").lower() for k in ("face", "person", "people", "invalid image")):
                    code = "invalid_start_image"
                _dbg("generate_video: upload failed status=%d code=%s msg=%s", resp.status, code, (err.get("message") or "")[:200])
                try:
                    if selected_acc_id is not None:
                        from .accounts import mark_generation_finished  # type: ignore
                        mark_generation_finished(selected_acc_id)
                except Exception as _e:
                    _dbg("generate_video: failed to mark generation finished after upload error: %s", repr(_e))
                yield {"event": "error", "code": code, "message": err.get("message"), "details": err}
                return
            media = await resp.json()
            upload_id = media.get("id")
            if not upload_id:
                _dbg("generate_video: upload ok but missing id. media=%s", str(media)[:200])
                try:
                    if selected_acc_id is not None:
                        from .accounts import mark_generation_finished  # type: ignore
                        mark_generation_finished(selected_acc_id)
                except Exception as _e:
                    _dbg("generate_video: failed to mark generation finished after upload missing id: %s", repr(_e))
                yield {"event": "error", "code": "upload_missing_id", "message": "Upload succeeded but no media id returned"}
                return
            _dbg("generate_video: uploaded media_id=%s", upload_id)
            yield {"event": "uploaded", "media_id": upload_id}
        except Exception as e:
            _dbg("generate_video: upload exception: %s", repr(e))
            try:
                if selected_acc_id is not None:
                    from .accounts import mark_generation_finished  # type: ignore
                    mark_generation_finished(selected_acc_id)
            except Exception as _e:
                _dbg("generate_video: failed to mark generation finished after upload exception: %s", repr(_e))
            yield {"event": "error", "code": "upload_exception", "message": str(e)}
            return

    # Step 2: Create generation (POST /backend/nf/create)
    payload: Dict[str, Any] = {
        "kind": "video",
        "prompt": prompt,
        "title": None,
        "size": (str(size).lower() if size else "large"),
        "n_frames": int(frames),
        "inpaint_items": [],
        "remix_target_id": None,
        "cameo_ids": None,
        "cameo_replacements": None,
        "model": "sy_8",
        "style_id": None,
        "audio_caption": None,
        "audio_transcript": None,
        "video_caption": None,
        "storyboard_id": None,
    }
    if upload_id:
        payload["inpaint_items"] = [{"kind": "upload", "upload_id": upload_id}]
    else:
        if orientation:
            payload["orientation"] = orientation
        else:
            # default to portrait if neither image nor orientation provided
            payload["orientation"] = "portrait"

    _dbg("generate_video: creating generation payload_keys=%s", list(payload.keys()))
    resp = await client.post_json("/backend/nf/create", payload)
    if resp.status != 200:
        err = await _parse_error_resp(resp)
        code = err.get("code") or "create_failed"
        if resp.status == 429:
            # Likely rate limit / concurrency limit
            code = code or "rate_limit"
        # Map well-known messages for UX clarity
        msg = str(err.get("message") or "")
        if "You already have 5 generations in progress" in msg:
            err["message"] = "Нет свободных аккаунтов. Подождите пару минут и попробуйте снова"
            err["code"] = "concurrency_limit"
        if "You've already generated 100 videos in the last day" in msg:
            err["message"] = "Нет свободных аккаунтов. Попробуйте позже"
            err["code"] = "daily_limit"
        # Prefer possibly remapped err[code]
        code_out = err.get("code") or code
        _dbg("generate_video: create failed status=%d code=%s msg=%s", resp.status, code_out, (err.get("message") or "")[:200])
        # Detect daily limit variant messages, sync counters, and free active slot
        try:
            msg_lc = str(err.get("message") or "").lower()
            if (
                "daily_limit" in (err.get("code") or "").lower()
                or (("submitted" in msg_lc or "generated" in msg_lc) and "100" in msg_lc and ("24 hours" in msg_lc or "last day" in msg_lc or "last 24 hours" in msg_lc))
            ):
                err["code"] = "daily_limit"
                code_out = "daily_limit"
        except Exception:
            pass
        # Sync counters if daily limit was hit and free active slot on any create failure
        try:
            if code_out == "daily_limit" and selected_acc_id is not None:
                from .accounts import mark_account_daily_exhausted  # type: ignore
                mark_account_daily_exhausted(selected_acc_id)
        except Exception as _e:
            _dbg("generate_video: failed to mark daily exhausted: %s", repr(_e))
        try:
            if selected_acc_id is not None:
                from .accounts import mark_generation_finished  # type: ignore
                mark_generation_finished(selected_acc_id)
        except Exception as _e:
            _dbg("generate_video: failed to mark generation finished after create error: %s", repr(_e))
        yield {"event": "error", "code": code_out, "message": err.get("message"), "details": err}
        return

    create_info = await resp.json()
    # Server may return an object or an array with a single task
    if isinstance(create_info, list):
        ci = create_info[0] if create_info else {}
    elif isinstance(create_info, dict):
        ci = create_info
    else:
        ci = {}
    task_id = ci.get("id") or ci.get("task_id")
    if not task_id:
        _dbg("generate_video: missing task_id in create_info=%s", str(create_info)[:200])
        try:
            if selected_acc_id is not None:
                from .accounts import mark_generation_finished  # type: ignore
                mark_generation_finished(selected_acc_id)
        except Exception as _e:
            _dbg("generate_video: failed to mark generation finished after missing task_id: %s", repr(_e))
        yield {"event": "error", "code": "missing_task_id", "message": f"Unexpected create response: {create_info}"}
        return
    # Mark daily counter only after task accepted
    try:
        if selected_acc_id is not None:
            from .accounts import mark_generation_created  # type: ignore
            mark_generation_created(selected_acc_id)
    except Exception as _e:
        _dbg("generate_video: failed to mark daily increment: %s", repr(_e))
    _dbg("generate_video: queued task_id=%s priority=%s", task_id, str(ci.get("priority")))
    yield {"event": "queued", "task_id": task_id, "priority": ci.get("priority")}

    try:
        async for poll_event in _poll_generation(
            client,
            str(task_id),
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
        ):
            yield poll_event
        return
    finally:
        # Close the HTTP session
        if isinstance(client, _AsyncBrowserSession) and client.session and not client.session.closed:
            _dbg("generate_video: closing session")
            await client.session.close()
        # Decrement active counter in the pool (any outcome)
        try:
            if selected_acc_id is not None:
                from .accounts import mark_generation_finished  # type: ignore
                mark_generation_finished(selected_acc_id)
        except Exception as _e:
            _dbg("generate_video: failed to mark generation finished: %s", repr(_e))


async def _poll_generation(
    client: _AsyncBrowserSession,
    task_id: str,
    *,
    poll_interval_sec: float,
    timeout_sec: float,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Shared polling logic used by both fresh and resumed generations."""

    start_time = time.time()
    found_gen_id: Optional[str] = None
    last_progress_key: Optional[str] = None

    while True:
        if timeout_sec > 0 and time.time() - start_time > timeout_sec:
            yield {
                "event": "error",
                "code": "timeout",
                "message": "Generation timed out",
                "task_id": task_id,
            }
            return

        pending_item: Optional[Dict[str, Any]] = None
        try:
            _dbg("poll_generation: polling pending...")
            pr = await client.get_json("/backend/nf/pending")
            if pr.status == 200:
                try:
                    arr = await pr.json()
                    if isinstance(arr, list):
                        for it in arr:
                            if it.get("id") == task_id:
                                pending_item = it
                                break
                except Exception as e:
                    _dbg("poll_generation: pending json parse error: %s", repr(e))
            else:
                _dbg("poll_generation: pending status=%d, ignore", pr.status)
        except Exception as e:
            _dbg("poll_generation: pending request failed: %s", repr(e))

        if pending_item:
            status = (pending_item.get("status") or "").lower()
            pct = pending_item.get("progress_pct")
            pos = pending_item.get("progress_pos_in_queue")
            eta = pending_item.get("estimated_queue_wait_time")
            msg = pending_item.get("queue_status_message")

            fail_reason = pending_item.get("failure_reason")
            if fail_reason or status in ("failed", "error", "canceled"):
                reason = str(fail_reason or status or "processing_error")
                _dbg("poll_generation: pending reported failure: %s", reason)
                yield {
                    "event": "error",
                    "code": reason,
                    "message": f"Generation failed: {reason}",
                    "details": pending_item,
                    "task_id": task_id,
                    "gen_id": found_gen_id,
                }
                return

            is_rendering = (status not in ("queued", "preprocessing")) or (
                isinstance(pct, (int, float))
                and pct is not None
                and float(pct) > 0.0
            )
            if not is_rendering:
                progress_event = {
                    "event": "progress",
                    "status": "queued",
                    "task_id": task_id,
                    "queue_position": pos,
                    "eta_sec": eta,
                    "message": msg,
                }
            else:
                progress_event = {
                    "event": "progress",
                    "status": "rendering",
                    "task_id": task_id,
                    "progress_pct": pct,
                    "message": msg,
                }

            fingerprint = json.dumps(progress_event, sort_keys=True)
            if fingerprint != last_progress_key:
                last_progress_key = fingerprint
                yield progress_event
        else:
            if last_progress_key is None:
                yield {"event": "progress", "status": "queued", "task_id": task_id}

        _dbg("poll_generation: polling drafts list ...")
        r = await client.get_json("/backend/project_y/profile/drafts?limit=15")
        if r.status == 401:
            _dbg("poll_generation: poll unauthorized (auth expired)")
            yield {
                "event": "error",
                "code": "auth_expired",
                "message": "Authentication expired while polling",
            }
            return
        if r.status >= 400:
            err = await _parse_error_resp(r)
            _dbg(
                "poll_generation: poll failed status=%d code=%s msg=%s",
                r.status,
                err.get("code"),
                (err.get("message") or "")[:200],
            )
            yield {
                "event": "error",
                "code": err.get("code") or "poll_failed",
                "message": err.get("message"),
                "details": err,
            }
            return

        try:
            payload = await r.json()
            items = payload.get("items", []) if isinstance(payload, dict) else []
        except Exception:
            items = []

        my = None
        for it in items:
            if it.get("task_id") == task_id:
                my = it
                break

        if my:
            _dbg("poll_generation: draft entry keys=%s", list(my.keys()))
            gen_id = my.get("id")
            if not found_gen_id and gen_id:
                found_gen_id = gen_id
                _dbg("poll_generation: draft found gen_id=%s", found_gen_id)
                yield {"event": "draft_found", "gen_id": found_gen_id}

            draft_has_error = False
            code_val = None
            msg_val = None
            if my.get("kind") == "sora_error":
                draft_has_error = True
                code_val = my.get("error_reason") or my.get("reason")
                msg_val = my.get("reason_str") or my.get("message")
            if (
                my.get("error_reason")
                or my.get("failure_reason")
                or my.get("reason")
                or my.get("reason_str")
            ) and not (my.get("url") and my.get("encodings")):
                draft_has_error = True
                code_val = code_val or my.get("error_reason") or my.get("failure_reason") or my.get("reason")
                msg_val = msg_val or my.get("reason_str") or my.get("message")

            if draft_has_error:
                reason = str(code_val or "processing_error")
                _dbg("poll_generation: detected error draft, reason=%s", reason)
                yield {
                    "event": "error",
                    "code": reason,
                    "message": (
                        f"Generation failed: {msg_val}" if msg_val else f"Generation failed: {reason}"
                    ),
                    "details": my,
                    "task_id": task_id,
                    "gen_id": found_gen_id,
                }
                return

            if my.get("url") and my.get("encodings"):
                if found_gen_id:
                    _dbg("poll_generation: fetching draft v2 details for %s", found_gen_id)
                    v2 = await client.get_json(f"/backend/project_y/profile/drafts/v2/{found_gen_id}")
                    if v2.status == 200:
                        d = (await v2.json()).get("draft", {})
                        _dbg("poll_generation: finished (v2) url=%s", d.get("url"))
                        yield {
                            "event": "finished",
                            "gen_id": found_gen_id,
                            "task_id": task_id,
                            "url": d.get("url"),
                            "downloadable_url": d.get("downloadable_url"),
                            "encodings": d.get("encodings"),
                            "width": d.get("width"),
                            "height": d.get("height"),
                            "prompt": d.get("prompt"),
                        }
                        return
                _dbg("poll_generation: finished (fallback) url=%s", my.get("url"))
                yield {
                    "event": "finished",
                    "gen_id": found_gen_id,
                    "task_id": task_id,
                    "url": my.get("url"),
                    "downloadable_url": my.get("downloadable_url"),
                    "encodings": my.get("encodings"),
                    "width": my.get("width"),
                    "height": my.get("height"),
                    "prompt": my.get("prompt"),
                }
                return

        await asyncio.sleep(poll_interval_sec)


async def resume_generation(
    task_id: str,
    *,
    account_id: int,
    poll_interval_sec: float = 3.0,
    timeout_sec: float = 900.0,
    proxy: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Resume polling of an existing generation task."""

    if not task_id:
        raise ValueError("task_id is required for resume_generation")
    if int(account_id) <= 0:
        raise ValueError("account_id must be a positive integer for resume_generation")

    try:
        from .db import get_account_credentials  # type: ignore

        creds = get_account_credentials(int(account_id))
    except Exception as e:
        _dbg("resume_generation: failed to load account credentials: %s", repr(e))
        creds = None

    if not creds or not creds.get("cookies_json"):
        yield {
            "event": "error",
            "code": "account_missing",
            "message": "Аккаунт недоступен для продолжения генерации",
        }
        return

    client = _AsyncBrowserSession(proxy=proxy, cookies_json=str(creds["cookies_json"]))
    try:
        yield {"event": "account", "account_id": int(account_id)}
        await client._ensure_access_token()
        yield {"event": "auth", "status": "ok"}

        async for poll_event in _poll_generation(
            client,
            str(task_id),
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
        ):
            yield poll_event
    except Exception as e:
        _dbg("resume_generation: exception: %s", repr(e))
        yield {"event": "error", "code": "resume_failed", "message": str(e)}
    finally:
        if isinstance(client, _AsyncBrowserSession) and client.session and not client.session.closed:
            _dbg("resume_generation: closing session")
            await client.session.close()
        try:
            from .accounts import mark_generation_finished  # type: ignore

            mark_generation_finished(int(account_id))
        except Exception as _e:
            _dbg("resume_generation: failed to mark generation finished: %s", repr(_e))


__all__ = [
    "generate_video",
    "resume_generation",
    "validate_cookies",
]
