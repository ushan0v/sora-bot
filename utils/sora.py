from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
import uuid as _uuid
from aiohttp_socks import ProxyConnector  
from aiohttp_socks import ProxyConnector
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Mapping, Optional, Union

import aiohttp
from yarl import URL
from playwright.async_api import async_playwright

SORA_BASE = "https://sora.chatgpt.com"
DEBUG = False


def _dbg(msg: str, *args: Any) -> None:
    if not DEBUG:
        return
    try:
        ts = time.strftime("%H:%M:%S")
        line = msg % args if args else msg
        print(f"[SORA DEBUG {ts}] {line}")
    except Exception:
        try:
            print(f"[SORA DEBUG] {msg}")
        except Exception:
            pass


def _redact(value: Optional[Union[str, bytes]], *, keep: int = 6) -> str:
    try:
        if value is None:
            return "None"
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        s = str(value)
        if len(s) <= keep:
            return s
        return f"{s[:keep]}…(len={len(s)})"
    except Exception:
        return "<redact_error>"


def _shorten(obj: Any, *, maxlen: int = 200) -> str:
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        try:
            s = str(obj)
        except Exception:
            s = "<unrepr>"
    if len(s) > maxlen:
        return s[:maxlen] + "…"
    return s


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


class SoraClient:

    def __init__(
        self,
        *,
        cookies: Union[str, Mapping[str, str], Iterable[Mapping[str, Any]]],
        proxy: Optional[str] = None,
        base_url: str = SORA_BASE,
    ) -> None:
        _dbg("SoraClient.__init__: base_url=%s, proxy=%s", base_url, proxy or "-")
        self._base = base_url.rstrip("/")
        self._proxy = proxy
        self._cookies_map = self._normalize_cookies(cookies)
        _dbg(
            "SoraClient.__init__: cookies_map_keys=%s",
            list(self._cookies_map.keys())
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._access_token: Optional[str] = None
        self._token_exp_ts: Optional[float] = None
        self._refresh_lock: Optional[asyncio.Lock] = asyncio.Lock()
        self._sentinel_token: Optional[str] = None
        self._cookies_seed_json: Optional[str] = None
        try:
            if isinstance(cookies, str):
                json.loads(cookies)
                self._cookies_seed_json = cookies
            else:
                self._cookies_seed_json = json.dumps(cookies)
        except Exception:
            try:
                self._cookies_seed_json = json.dumps(self._reconstruct_cookies_list())
            except Exception:
                self._cookies_seed_json = None
        _dbg(
            "SoraClient.__init__: cookies_seed_json=%s",
            _shorten(self._cookies_seed_json)
        )
    @staticmethod
    def _normalize_cookies(
        cookies: Union[str, Mapping[str, str], Iterable[Mapping[str, Any]]]
    ) -> Dict[str, Dict[str, str]]:
        _dbg("_normalize_cookies: type=%s", type(cookies).__name__)
        if isinstance(cookies, str):
            cookies_obj = json.loads(cookies)
        else:
            cookies_obj = cookies

        jar_like: Dict[str, Dict[str, str]] = {}

        def _valid_cookie_name(n: str) -> bool:
            return bool(re.match(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$", n))
        if isinstance(cookies_obj, Iterable) and not isinstance(cookies_obj, Mapping):
            cnt = 0
            for c in cookies_obj: 
                if not isinstance(c, Mapping):
                    continue
                name = c.get("name")
                value = c.get("value")
                domain = c.get("domain") or "sora.chatgpt.com"
                path = c.get("path") or "/"
                if not name or value is None or not _valid_cookie_name(str(name)):
                    continue
                key = f"{str(domain).lstrip('.') }|{path}"
                jar_like.setdefault(key, {})[str(name)] = str(value)
                cnt += 1
            _dbg("_normalize_cookies: list processed=%d keys=%s", cnt, list(jar_like.keys()))
            return jar_like
        if isinstance(cookies_obj, Mapping):
            key = "sora.chatgpt.com|/"
            jar_like[key] = {}
            for name, value in cookies_obj.items():
                if not _valid_cookie_name(str(name)):
                    continue
                jar_like[key][str(name)] = str(value)
            _dbg("_normalize_cookies: dict processed=%d", len(jar_like[key]))
            return jar_like

        raise ValueError("Unsupported cookies format. Provide list-of-objects, dict, or JSON string")
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            _dbg("_ensure_session: reuse existing")
            return self._session

        jar = aiohttp.CookieJar(unsafe=True)
        headers = dict(DEFAULT_HEADERS)

        connector = None
        if self._proxy and self._proxy.lower().startswith("socks"):
            try:
                proxy_url = self._proxy
                if proxy_url.lower().startswith("socks://"):
                    proxy_url = "socks5://" + proxy_url.split("://", 1)[1]

                connector = ProxyConnector.from_url(proxy_url)
                _dbg("using socks proxy connector")
            except Exception as e:
                _dbg("failed to init socks proxy: %r", e)

        self._session = aiohttp.ClientSession(cookie_jar=jar, headers=headers, connector=connector)
        _dbg("_ensure_session: created session; proxy=%s", self._proxy or "-")
        seeded = 0
        for key, cookies in self._cookies_map.items():
            domain, path = key.split("|", 1)
            base_host = domain.lstrip(".")
            targets = {base_host}
            if base_host.endswith("chatgpt.com") and base_host != "sora.chatgpt.com":
                targets.add("sora.chatgpt.com")
            for host in targets:
                resp_url = URL.build(scheme="https", host=host, path=path or "/")
                for name, value in cookies.items():
                    jar.update_cookies({name: value}, response_url=resp_url)
                    seeded += 1
        _dbg("_ensure_session: cookies seeded=%d", seeded)

        return self._session

    async def _ensure_access_token(self) -> str:
        if self._access_token and self._token_exp_ts:
            _dbg(
                "_ensure_access_token: have token exp=%s",
                time.strftime("%H:%M:%S", time.localtime(self._token_exp_ts)) if self._token_exp_ts else "-",
            )
            now = time.time()
            if now < (self._token_exp_ts - 60):
                _dbg("_ensure_access_token: token still valid")
                return self._access_token
        elif self._access_token and not self._token_exp_ts:
            try:
                self._token_exp_ts = _decode_jwt_exp(self._access_token)
                now = time.time()
                if self._token_exp_ts and now < (self._token_exp_ts - 60):
                    _dbg("_ensure_access_token: decoded exp, token valid")
                    return self._access_token
            except Exception:
                pass

        _dbg("_ensure_access_token: refreshing access token")
        await self._refresh_access_token(force=True)
        assert self._access_token, "missing access token after refresh"
        _dbg("_ensure_access_token: got token=%s", _redact(self._access_token))
        return self._access_token

    async def _refresh_access_token(self, *, force: bool = False) -> None:
        sess = await self._ensure_session()
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            _dbg("_refresh_access_token: force=%s", force)
            if not force and self._access_token and self._token_exp_ts:
                now = time.time()
                if now < (self._token_exp_ts - 60):
                    _dbg("_refresh_access_token: skip, still valid")
                    return

            url = f"{self._base}/api/auth/session"
            kwargs: Dict[str, Any] = {}
            if self._proxy and not self._proxy.lower().startswith("socks"):
                kwargs["proxy"] = self._proxy

            _dbg("GET %s", url)
            async with sess.get(url, **kwargs) as r:
                _dbg("auth session status=%d", r.status)
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

            if not isinstance(data, Mapping):
                raise RuntimeError(f"auth_session_unexpected_payload: {str(data)[:200]}")

            token = data.get("accessToken")
            if not token:
                raise RuntimeError("auth_session_missing_access_token")

            self._access_token = str(token)
            self._token_exp_ts = _decode_jwt_exp(self._access_token)
            sess.headers["authorization"] = f"Bearer {self._access_token}"
            _dbg(
                "_refresh_access_token: new token=%s exp=%s",
                _redact(self._access_token),
                self._token_exp_ts,
            )
            try:
                device_id = None
                for _k, _cookies in self._cookies_map.items():
                    if "oai-did" in _cookies:
                        device_id = _cookies.get("oai-did")
                        break
                if device_id:
                    sess.headers.setdefault("OAI-Device-Id", str(device_id))
                    _dbg("_refresh_access_token: set OAI-Device-Id from cookies=%s", _redact(device_id))
            except Exception:
                pass
    async def _get(self, path: str) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{self._base}{path}"
        _is_noise = "/backend/project_y/profile/drafts" in url
        if not _is_noise:
            _dbg("_get: url=%s", url)
        kwargs: Dict[str, Any] = {}
        if self._proxy and not self._proxy.lower().startswith("socks"):
            kwargs["proxy"] = self._proxy
        resp = await sess.get(url, **kwargs)
        if not _is_noise:
            _dbg("_get: status=%d", resp.status)
        if resp.status == 401:
            _dbg("_get: 401 -> refresh token and retry")
            await self._refresh_access_token(force=True)
            resp.release()
            resp = await sess.get(url, **kwargs)
            if not _is_noise:
                _dbg("_get: retry status=%d", resp.status)
        return resp

    async def _post_json(self, path: str, payload: Mapping[str, Any], extra_headers: Optional[Mapping[str, str]] = None) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{self._base}{path}"
        _dbg("_post_json: url=%s payload=%s headers=%s", url, _shorten(payload), list((extra_headers or {}).keys()))
        kwargs: Dict[str, Any] = {}
        if self._proxy and not self._proxy.lower().startswith("socks"):
            kwargs["proxy"] = self._proxy
        headers = {"content-type": "application/json"}
        if isinstance(extra_headers, Mapping):
            headers.update({k: str(v) for k, v in extra_headers.items()})
        resp = await sess.post(url, json=dict(payload), headers=headers, **kwargs)
        _dbg("_post_json: status=%d", resp.status)
        if resp.status == 401:
            _dbg("_post_json: 401 -> refresh token and retry")
            await self._refresh_access_token(force=True)
            resp.release()
            resp = await sess.post(url, json=dict(payload), headers=headers, **kwargs)
            _dbg("_post_json: retry status=%d", resp.status)
        return resp

    async def _post_multipart(
        self,
        path: str,
        file_field: str,
        filename: str,
        data_bytes: bytes,
        content_type: str,
    ) -> aiohttp.ClientResponse:
        sess = await self._ensure_session()
        await self._ensure_access_token()
        url = path if path.startswith("http") else f"{self._base}{path}"
        _dbg("_post_multipart: url=%s filename=%s ctype=%s size=%d", url, filename, content_type, len(data_bytes))
        form = aiohttp.FormData()
        form.add_field(file_field, data_bytes, filename=filename, content_type=content_type)
        form.add_field("file_name", filename)
        kwargs: Dict[str, Any] = {}
        if self._proxy and not self._proxy.lower().startswith("socks"):
            kwargs["proxy"] = self._proxy
        resp = await sess.post(url, data=form, **kwargs)
        _dbg("_post_multipart: status=%d", resp.status)
        if resp.status == 401:
            _dbg("_post_multipart: 401 -> refresh token and retry")
            await self._refresh_access_token(force=True)
            resp.release()
            resp = await sess.post(url, data=form, **kwargs)
            _dbg("_post_multipart: retry status=%d", resp.status)
        return resp
    async def validate_cookies(self) -> str:
        await self._ensure_access_token()
        assert self._access_token
        return self._access_token
    async def aclose(self) -> None:
        try:
            if self._session and not self._session.closed:
                _dbg("aclose: closing session")
                await self._session.close()
        except Exception:
            pass

    async def __aenter__(self) -> "SoraClient":
        _dbg("__aenter__: ensure session")
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Optional[bool]:
        _dbg("__aexit__: closing")
        await self.aclose()
        return None

    async def _maybe_authenticate(self) -> None:
        try:
            _dbg("_maybe_authenticate: GET /backend/authenticate")
            r = await self._get("/backend/authenticate")
            _dbg("_maybe_authenticate: status=%d", r.status)
            r.release()
        except Exception:
            pass

    def _build_sentinel_header(self, flow: str) -> Optional[Dict[str, str]]:
        token_str = self._sentinel_token or None

        if not token_str:
            _dbg("_build_sentinel_header: no sentinel token yet for flow=%s", flow)
            return None
        try:
            obj = json.loads(token_str)
            if isinstance(obj, Mapping):
                if str(obj.get("flow") or "") != flow:
                    obj = dict(obj)
                    obj["flow"] = flow
                token_str = json.dumps(obj, separators=(",", ":"))
            _dbg("_build_sentinel_header: prepared for flow=%s", flow)
        except Exception:
            pass
        return {"OpenAI-Sentinel-Token": token_str}

    def _reconstruct_cookies_list(self) -> List[Dict[str, Any]]:
        lst: List[Dict[str, Any]] = []
        for key, cookies in self._cookies_map.items():
            domain, path = key.split("|", 1)
            base_host = domain.lstrip(".")
            dom_val = ("." + base_host) if base_host.endswith("chatgpt.com") else base_host
            for name, value in cookies.items():
                lst.append({
                    "name": name,
                    "value": value,
                    "domain": dom_val,
                    "path": path or "/",
                })
        _dbg("_reconstruct_cookies_list: total=%d", len(lst))
        return lst

    async def _ensure_sentinel_token(self, flow: str) -> None:
        if self._sentinel_token:
            _dbg("_ensure_sentinel_token: already present for flow=%s", flow)
            return
        try:
            cookies_obj: Union[str, List[Dict[str, Any]], Dict[str, Any]]
            if self._cookies_seed_json:
                cookies_obj = json.loads(self._cookies_seed_json)
            else:
                cookies_obj = self._reconstruct_cookies_list()
        except Exception:
            cookies_obj = self._reconstruct_cookies_list()

        sess = await self._ensure_session()
        ua = sess.headers.get("user-agent")
        device_id = sess.headers.get("OAI-Device-Id")
        if not device_id:
            for _k, _cookies in self._cookies_map.items():
                if "oai-did" in _cookies:
                    device_id = _cookies.get("oai-did")
                    break
        if not device_id:
            device_id = str(_uuid.uuid4())
            sess.headers["OAI-Device-Id"] = device_id

        try:
            token_str = await get_sentinel_token_via_playwright(
                cookies=cookies_obj,
                device_id=str(device_id),
                user_agent=ua,
                flow=flow,
                proxy=self._proxy,
            )
            self._sentinel_token = token_str
            _dbg("_ensure_sentinel_token: fetched token=%s", _redact(token_str))
        except Exception as e:
            _dbg("sentinel auto fetch failed: %r", e)
            return

    async def generate_video(
        self,
        *,
        prompt: str,
        frames: int,
        orientation: Optional[str] = None, 
        size: Optional[str] = None,
        start_image: Optional[Union[str, Path, bytes]] = None, 
        poll_interval_sec: float = 3.0,
        timeout_sec: float = 600.0,
        sentinel_flow: str = "sora_2_create_task",
    ) -> AsyncGenerator[Dict[str, Any], None]:

        _dbg(
            "generate_video: prompt=%s frames=%s orientation=%s size=%s start_image=%s flow=%s",
            _shorten(prompt), frames, orientation, size,
            (str(start_image) if isinstance(start_image, (str, Path)) else ("bytes" if isinstance(start_image, (bytes, bytearray)) else None)),
            sentinel_flow,
        )
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt is required and must be a string")
        if not isinstance(frames, int) or frames <= 0:
            raise ValueError("frames must be a positive integer")
        if size is not None and str(size).lower() not in ("small", "large"):
            raise ValueError("size must be 'small' or 'large'")
        if orientation not in (None, "portrait", "landscape"):
            raise ValueError("orientation must be 'portrait' or 'landscape'")
        try:
            await self._ensure_access_token()
            _dbg("auth ok")
            yield {"event": "auth", "status": "ok"}
        except Exception as e:
            _dbg("generate_video: auth_failed: %r", e)
            yield {"event": "error", "code": "auth_failed", "message": str(e)}
            return
        _dbg("generate_video: maybe authenticate")
        await self._maybe_authenticate()
        upload_id: Optional[str] = None
        if start_image is not None:
            _dbg("generate_video: uploading start image")
            try:
                if isinstance(start_image, (str, Path)):
                    path = Path(start_image)
                    data_bytes = path.read_bytes()
                    filename = path.name
                    content_type = _detect_mime(filename)
                elif isinstance(start_image, (bytes, bytearray)):
                    data_bytes = bytes(start_image)
                    filename = "photo.jpg"
                    content_type = "image/jpeg"
                else:
                    raise TypeError("start_image must be bytes or file path")

                r = await self._post_multipart(
                    "/backend/uploads",
                    file_field="file",
                    filename=filename,
                    data_bytes=data_bytes,
                    content_type=content_type,
                )
                _dbg("generate_video: upload status=%d", r.status)
                if r.status != 200:
                    err = await _parse_error_resp(r)
                    code = err.get("code") or "upload_failed"
                    msg = err.get("message")
                    if r.status == 400 and any(k in (str(msg or "").lower()) for k in ("face", "person", "people", "invalid image")):
                        code = "invalid_start_image"
                    yield {"event": "error", "code": code, "message": msg, "details": err}
                    return
                media = await r.json()
                upload_id = media.get("id")
                if not upload_id:
                    yield {"event": "error", "code": "upload_missing_id", "message": "Upload succeeded but no media id returned"}
                    return
                yield {"event": "uploaded", "media_id": upload_id}
            except Exception as e:
                _dbg("generate_video: upload_exception: %r", e)
                yield {"event": "error", "code": "upload_exception", "message": str(e)}
                return
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
            payload["orientation"] = orientation or "portrait"
        sentinel_hdr = self._build_sentinel_header(sentinel_flow)
        if not sentinel_hdr:
            await self._ensure_sentinel_token(sentinel_flow)
            sentinel_hdr = self._build_sentinel_header(sentinel_flow)
        _dbg("generate_video: create payload=%s", _shorten(payload))
        r = await self._post_json("/backend/nf/create", payload, extra_headers=sentinel_hdr)
        if r.status != 200:
            err = await _parse_error_resp(r)
            if (err.get("code") or "").lower() == "sentinel_block":
                msg = (
                    "Запрос отклонён защитой Sentinel. Модуль не смог автоматически получить "
                    "корректный OpenAI-Sentinel-Token. Убедитесь, что cookies валидны и вы авторизованы, "
                    "затем попробуйте снова."
                )
                yield {"event": "error", "code": "sentinel_block", "message": msg, "details": err}
                return
            yield {"event": "error", "code": err.get("code") or "create_failed", "message": err.get("message"), "details": err}
            return

        create_info = await r.json()
        ci = create_info[0] if isinstance(create_info, list) and create_info else (create_info if isinstance(create_info, Mapping) else {})
        task_id = ci.get("id") or ci.get("task_id")
        if not task_id:
            yield {"event": "error", "code": "missing_task_id", "message": f"Unexpected create response: {create_info}"}
            return

        _dbg("generate_video: queued task_id=%s priority=%s", task_id, ci.get("priority"))
        yield {"event": "queued", "task_id": task_id, "priority": ci.get("priority")}
        start_time = time.time()
        found_gen_id: Optional[str] = None
        last_progress_fingerprint: Optional[str] = None

        while True:
            if time.time() - start_time > timeout_sec:
                yield {"event": "error", "code": "timeout", "message": "Generation timed out"}
                return
            pending_item: Optional[Dict[str, Any]] = None
            try:
                pr = await self._get("/backend/nf/pending")
                if pr.status == 200:
                    try:
                        arr = await pr.json()
                        if isinstance(arr, list):
                            for it in arr:
                                if it.get("id") == task_id:
                                    pending_item = it
                                    break
                    except Exception:
                        pass
            except Exception as e:
                _dbg("pending request failed: %r", e)

            if pending_item:
                status = (pending_item.get("status") or "").lower()
                pct = pending_item.get("progress_pct")
                pos = pending_item.get("progress_pos_in_queue")
                eta = pending_item.get("estimated_queue_wait_time")
                msg = pending_item.get("queue_status_message")
                _dbg("generate_video: pending status=%s pct=%s pos=%s eta=%s", status, pct, pos, eta)

                fail_reason = pending_item.get("failure_reason")
                if fail_reason or status in ("failed", "error", "canceled"):
                    reason = str(fail_reason or status or "processing_error")
                    yield {
                        "event": "error",
                        "code": reason,
                        "message": f"Generation failed: {reason}",
                        "details": pending_item,
                        "task_id": task_id,
                        "gen_id": found_gen_id,
                    }
                    return

                is_rendering = (status not in ("queued", "preprocessing")) or (isinstance(pct, (int, float)) and float(pct or 0) > 0.0)
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
                fp = json.dumps(progress_event, sort_keys=True)
                if fp != last_progress_fingerprint:
                    last_progress_fingerprint = fp
                    yield progress_event
            else:
                if last_progress_fingerprint is None:
                    yield {"event": "progress", "status": "queued", "task_id": task_id}
            r = await self._get("/backend/project_y/profile/drafts?limit=15")
            if r.status == 401:
                yield {"event": "error", "code": "auth_expired", "message": "Authentication expired while polling"}
                return
            if r.status >= 400:
                err = await _parse_error_resp(r)
                yield {"event": "error", "code": err.get("code") or "poll_failed", "message": err.get("message"), "details": err}
                return

            try:
                items = (await r.json()).get("items", [])
            except Exception:
                items = []

            my = None
            for it in items:
                if it.get("task_id") == task_id:
                    my = it
                    break

            if my:
                gen_id = my.get("id")
                if not found_gen_id and gen_id:
                    found_gen_id = gen_id
                    _dbg("generate_video: draft_found gen_id=%s", found_gen_id)
                    yield {"event": "draft_found", "gen_id": found_gen_id}

                draft_has_error = False
                code_val = None
                msg_val = None
                if my.get("kind") == "sora_error":
                    draft_has_error = True
                    code_val = my.get("error_reason") or my.get("reason")
                    msg_val = my.get("reason_str") or my.get("message")
                if (my.get("error_reason") or my.get("failure_reason") or my.get("reason") or my.get("reason_str")) and not (my.get("url") and my.get("encodings")):
                    draft_has_error = True
                    code_val = code_val or my.get("error_reason") or my.get("failure_reason") or my.get("reason")
                    msg_val = msg_val or my.get("reason_str") or my.get("message")

                if draft_has_error:
                    reason = str(code_val or "processing_error")
                    _dbg("generate_video: draft_error code=%s msg=%s", reason, _shorten(msg_val))
                    yield {
                        "event": "error",
                        "code": reason,
                        "message": (f"Generation failed: {msg_val}" if msg_val else f"Generation failed: {reason}"),
                        "details": my,
                        "task_id": task_id,
                        "gen_id": found_gen_id,
                    }
                    return

                if my.get("url") and my.get("encodings"):
                    if found_gen_id:
                        v2 = await self._get(f"/backend/project_y/profile/drafts/v2/{found_gen_id}")
                        if v2.status == 200:
                            d = (await v2.json()).get("draft", {})
                            _dbg("generate_video: finished v2 url=%s", d.get("url"))
                            yield {
                                "event": "finished",
                                "gen_id": found_gen_id,
                                "task_id": task_id,
                                "url": d.get("url"),
                                "width": d.get("width"),
                                "height": d.get("height"),
                                "prompt": d.get("prompt"),
                            }
                            return
                    _dbg("generate_video: finished fallback url=%s", my.get("url"))
                    yield {
                        "event": "finished",
                        "gen_id": found_gen_id,
                        "task_id": task_id,
                        "url": my.get("url"),
                        "width": my.get("width"),
                        "height": my.get("height"),
                        "prompt": my.get("prompt"),
                    }
                    return

            await asyncio.sleep(poll_interval_sec)

def _parse_error_resp(resp: aiohttp.ClientResponse) -> "asyncio.Future[Dict[str, Any]]":
    async def _inner() -> Dict[str, Any]:
        _dbg("_parse_error_resp: status=%d", resp.status)
        try:
            data = await resp.json()
            err = data.get("error") or {}
        except Exception:
            err = {}
            try:
                data = await resp.text()
            except Exception:
                data = ""
        _dbg(
            "_parse_error_resp: type=%s code=%s msg=%s",
            (err or {}).get("type"), (err or {}).get("code"), _shorten((err or {}).get("message") or data)
        )
        return {
            "http_status": resp.status,
            "type": (err or {}).get("type"),
            "code": (err or {}).get("code"),
            "message": (err or {}).get("message") or (data if isinstance(data, str) else ""),
            "raw": data,
        }

    return asyncio.ensure_future(_inner())


def _detect_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    out = mime or "application/octet-stream"
    _dbg("_detect_mime: %s -> %s", path, out)
    return out


def _decode_jwt_exp(token: str) -> Optional[float]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            _dbg("_decode_jwt_exp: invalid parts count=%d", len(parts))
            return None
        def _b64fix(s: str) -> bytes:
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8"))
        payload_raw = _b64fix(parts[1])
        payload = json.loads(payload_raw.decode("utf-8"))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            _dbg("_decode_jwt_exp: exp=%s", exp)
            return float(exp)
        return None
    except Exception:
        _dbg("_decode_jwt_exp: failed to decode token=%s", _redact(token))
        return None


__all__ = ["SoraClient", "DEBUG"]

DEFAULT_UA_FIREFOX = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) "
    "Gecko/20100101 Firefox/144.0"
)


async def get_sentinel_token_via_playwright(
    cookies: Union[str, Mapping[str, str], Iterable[Mapping[str, Any]]],
    *,
    device_id: str,
    user_agent: Optional[str] = None,
    flow: str = "sora_2_create_task",
    timeout_ms: int = 7000,
    proxy: Optional[str] = None,
) -> str:
    _dbg(
        "get_sentinel_token_via_playwright: flow=%s timeout_ms=%s proxy=%s",
        flow, timeout_ms, proxy or "-",
    )
    if isinstance(cookies, str):
        try:
            cookies_obj: Any = json.loads(cookies)
        except Exception:
            cookies_obj = cookies
    else:
        cookies_obj = cookies

    pw_cookies: List[Dict[str, Any]] = []
    have_device_cookie = False

    def _push_cookie(name: str, value: str, domain: str, path: str) -> None:
        nonlocal have_device_cookie
        if name == "oai-did" and value:
            have_device_cookie = True
        host = domain.lstrip(".") if isinstance(domain, str) else "sora.chatgpt.com"
        pw_cookies.append({
            "name": str(name),
            "value": str(value),
            "domain": host if not host.startswith(".") else host[1:],
            "path": path or "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "None",
        })

    if isinstance(cookies_obj, Iterable) and not isinstance(cookies_obj, Mapping):
        for c in cookies_obj:
            try:
                if not isinstance(c, Mapping):
                    continue
                name = c.get("name")
                value = c.get("value")
                domain = c.get("domain") or "sora.chatgpt.com"
                path = c.get("path") or "/"
                if name and value is not None:
                    _push_cookie(str(name), str(value), str(domain), str(path))
            except Exception:
                continue
    elif isinstance(cookies_obj, Mapping):
        for name, value in cookies_obj.items():
            _push_cookie(str(name), str(value), "sora.chatgpt.com", "/")
    else:
        raise ValueError("Unsupported cookies format for Playwright: list-of-objects, dict, or JSON string")

    _dbg("get_sentinel_token_via_playwright: cookies_count=%d have_device=%s", len(pw_cookies), have_device_cookie)
    if not have_device_cookie:
        pw_cookies.append({
            "name": "oai-did",
            "value": device_id,
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "None",
        })

    ua = user_agent or DEFAULT_HEADERS.get("user-agent") or DEFAULT_UA_FIREFOX

    async with async_playwright() as p:
        launch_kwargs: Dict[str, Any] = {"headless": True}
        if proxy and isinstance(proxy, str) and proxy.strip():
            launch_kwargs["proxy"] = {"server": proxy}
        _dbg("get_sentinel_token_via_playwright: launching chromium headless proxy=%s", bool(launch_kwargs.get("proxy")))
        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(user_agent=ua)
        await ctx.add_cookies(pw_cookies)
        page = await ctx.new_page()

        _dbg("get_sentinel_token_via_playwright: goto %s/profile", SORA_BASE)
        await page.goto(f"{SORA_BASE}/profile", wait_until="domcontentloaded")
        try:
            await page.wait_for_function(
                "() => (typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function')",
                timeout=timeout_ms,
            )
        except Exception:
            try:
                _dbg("get_sentinel_token_via_playwright: inject sdk.js and wait")
                await page.add_script_tag(url="https://chatgpt.com/sentinel/97790f37/sdk.js")
                await page.wait_for_function(
                    "() => (typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function')",
                    timeout=timeout_ms,
                )
            except Exception as e:
                _dbg("get_sentinel_token_via_playwright: sdk unavailable: %r", e)
                await ctx.close()
                await browser.close()
                raise RuntimeError(f"Sentinel SDK not available: {e}")

        token_obj = await page.evaluate(
            "(flow) => window.SentinelSDK.token(flow)",
            flow,
        )

        if isinstance(token_obj, Mapping):
            out = dict(token_obj)
            out["flow"] = flow
            out["id"] = device_id
            token_str = json.dumps(out, separators=(",", ":"))
        else:
            try:
                base_obj = json.loads(token_obj) if isinstance(token_obj, str) else {}
            except Exception:
                base_obj = {}
            base_obj.update({"flow": flow, "id": device_id})
            token_str = json.dumps(base_obj, separators=(",", ":"))

        await ctx.close()
        await browser.close()
        _dbg("get_sentinel_token_via_playwright: token=%s", _redact(token_str))
        return token_str