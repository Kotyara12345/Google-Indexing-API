from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import BatchHttpRequest

SCOPES = ["https://www.googleapis.com/auth/indexing"]
BATCH_URI = "https://indexing.googleapis.com/batch"
BATCH_CHUNK = 50
PARALLEL_WORKERS = 5
REQUIRED_KEY_FIELDS = ("client_email", "private_key", "token_uri", "project_id")

NOTIFICATION_TYPES = ("URL_UPDATED", "URL_DELETED")
DEFAULT_NOTIFICATION_TYPE = "URL_UPDATED"

SEND_MODES = ("batch", "parallel")
DEFAULT_SEND_MODE = "batch"

HTTP_TIMEOUT = 30
BACKOFF_DELAYS = (0, 2, 5, 10)

QUOTA_STATUSES = ("RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED")

_NETWORK_ERRORS = (
    socket.timeout,
    socket.error,
    ConnectionError,
    OSError,
)


class KeyValidationError(Exception):
    pass


class BatchUnavailable(Exception):
    pass


class QuotaExhausted(Exception):
    pass


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def validate_key_file(path: str) -> None:
    path = _strip_quotes(path or "")
    if not path:
        raise KeyValidationError("Путь к ключу пуст.")
    p = Path(path)
    if not p.exists():
        raise KeyValidationError("Файл ключа не найден. Выберите JSON-ключ.")
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise KeyValidationError(f"Не удалось прочитать JSON-ключ: {e}")

    if data.get("type") != "service_account":
        raise KeyValidationError(
            "Файл не является ключом сервисного аккаунта (type != service_account)."
        )
    missing = [f for f in REQUIRED_KEY_FIELDS if not data.get(f)]
    if missing:
        raise KeyValidationError(f"В ключе отсутствуют поля: {', '.join(missing)}")


def _status_from_error_details(exception: HttpError) -> str:
    details = getattr(exception, "error_details", None)
    if not details:
        return ""
    for item in details:
        if isinstance(item, dict):
            status = item.get("reason") or item.get("status")
            if status:
                return str(status)
    return ""


def _status_from_content(content) -> str:
    if not content:
        return ""
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = json.loads(text)
        return data.get("error", {}).get("status", "") or ""
    except (AttributeError, ValueError, KeyError, UnicodeDecodeError):
        return ""


def _is_quota_error(exception: Exception) -> bool:
    """
    True, если ошибка указывает на исчерпание квоты.

    Приоритет:
      1. HttpError.resp.status == 429,
      2. error_details / error.status == RESOURCE_EXHAUSTED
         или RATE_LIMIT_EXCEEDED,
      3. текстовые маркеры (нужны для BatchError).

    403 сознательно НЕ считается квотой: он обычно означает
    PERMISSION_DENIED (нет прав в Search Console), а не лимит.
    """
    if isinstance(exception, HttpError):
        if exception.resp is not None and exception.resp.status == 429:
            return True
        status = _status_from_error_details(exception)
        if not status:
            status = _status_from_content(getattr(exception, "content", None))
        if status in QUOTA_STATUSES:
            return True

    text = str(exception)
    return ("Quota exceeded" in text
            or "RATE_LIMIT_EXCEEDED" in text
            or "RESOURCE_EXHAUSTED" in text)


def _is_quota_error_message(text: str) -> bool:
    return ("Quota exceeded" in text
            or "RATE_LIMIT_EXCEEDED" in text
            or "RESOURCE_EXHAUSTED" in text)


def _is_network_error(exception: Exception) -> bool:
    if isinstance(exception, HttpError):
        return False
    return isinstance(exception, _NETWORK_ERRORS)


class _ThreadContext:
    """Service + http для одного потока."""
    __slots__ = ("service", "http")

    def __init__(self, service, http):
        self.service = service
        self.http = http


class IndexingClient:
    def __init__(self, key_path: str,
                 send_mode: str = DEFAULT_SEND_MODE,
                 http_timeout: int = HTTP_TIMEOUT):
        key_path = _strip_quotes(key_path)
        validate_key_file(key_path)
        if send_mode not in SEND_MODES:
            raise ValueError(f"send_mode must be one of {SEND_MODES}")

        self._key_path = key_path
        self._http_timeout = http_timeout
        self._send_mode = send_mode
        self._local = threading.local()
        self._batch_broken = False

    def _build_thread_ctx(self) -> _ThreadContext:
        creds = service_account.Credentials.from_service_account_file(
            self._key_path, scopes=SCOPES
        )
        http = creds.authorize(httplib2.Http(timeout=self._http_timeout))
        service = build("indexing", "v3", http=http, cache_discovery=False)
        return _ThreadContext(service, http)

    def _get_thread_ctx(self) -> _ThreadContext:
        ctx = getattr(self._local, "ctx", None)
        if ctx is None:
            ctx = self._build_thread_ctx()
            self._local.ctx = ctx
        return ctx

    def _invalidate_thread_ctx(self):
        self._local.ctx = None

    def send(self, urls, notification_type=DEFAULT_NOTIFICATION_TYPE,
             on_result=None, cancel=None):
        if notification_type not in NOTIFICATION_TYPES:
            raise ValueError(
                f"notification_type должен быть одним из {NOTIFICATION_TYPES}, "
                f"получено: {notification_type!r}"
            )

        use_batch = (self._send_mode == "batch" and not self._batch_broken)
        ok = fail = 0

        for i in range(0, len(urls), BATCH_CHUNK):
            if cancel is not None and cancel.is_set():
                break
            chunk = urls[i:i + BATCH_CHUNK]

            if use_batch:
                try:
                    chunk_results = self._send_batch_chunk(
                        chunk, notification_type, cancel)
                except BatchUnavailable:
                    self._batch_broken = True
                    use_batch = False
                    chunk_results = self._send_parallel_chunk(
                        chunk, notification_type, cancel)
            else:
                chunk_results = self._send_parallel_chunk(
                    chunk, notification_type, cancel)

            for url, success, info in chunk_results:
                if success:
                    ok += 1
                else:
                    fail += 1
                if on_result is not None:
                    on_result(url, success, info)
            if cancel is not None and cancel.is_set():
                break
        return ok, fail

    # ---------- batch ----------

    def _send_batch_chunk(self, urls, notification_type, cancel):
        ctx = self._get_thread_ctx()
        service = ctx.service
        http = ctx.http

        results: dict[str, tuple[bool, str]] = {}
        last = len(BACKOFF_DELAYS) - 1

        def make_callback(quota_state):
            def callback(request_id, response, exception):
                if exception is not None:
                    if _is_quota_error(exception):
                        quota_state["hit"] = True
                    results[request_id] = (False, str(exception))
                else:
                    results[request_id] = (True, "OK")
            return callback

        for attempt, delay in enumerate(BACKOFF_DELAYS):
            if delay > 0:
                self._sleep_with_cancel(delay, cancel)
            if cancel is not None and cancel.is_set():
                break

            results.clear()
            quota_state = {"hit": False}

            # Явно передаём http в BatchHttpRequest, чтобы он не
            # использовал глобальный транспорт по умолчанию.
            batch = BatchHttpRequest(
                callback=make_callback(quota_state),
                batch_uri=BATCH_URI,
                http=http,
            )
            for url in urls:
                req = service.urlNotifications().publish(
                    body={"url": url, "type": notification_type}
                )
                batch.add(req, request_id=url)

            transport_error = False
            try:
                batch.execute()
            except socket.timeout as e:
                for u in urls:
                    results.setdefault(u, (False, f"Socket timeout: {e}"))
                transport_error = True
            except HttpError as e:
                msg = str(e).lower()
                if (e.resp is not None
                        and e.resp.status in (400, 404, 405, 501)
                        and "batch" in msg
                        and not results):
                    raise BatchUnavailable(str(e))
                if _is_quota_error(e):
                    quota_state["hit"] = True
                if not results:
                    transport_error = True
                    if not quota_state["hit"]:
                        for u in urls:
                            results.setdefault(
                                u, (False, f"Batch HTTP error: {e}"))
            except Exception as e:
                if _is_network_error(e):
                    self._invalidate_thread_ctx()
                    raise BatchUnavailable(f"Batch network error: {e}")
                if not results:
                    raise BatchUnavailable(f"Batch transport error: {e}")
                for u in urls:
                    results.setdefault(u, (False, f"Batch error: {e}"))

            if quota_state["hit"]:
                if attempt < last:
                    continue
                for u in urls:
                    results.setdefault(u, (False, "Quota exceeded (retry)"))
                raise QuotaExhausted("Daily quota exhausted")

            if transport_error:
                pass
            break

        return [(u, *results.get(u, (False, "no response"))) for u in urls]

    # ---------- parallel ----------

    def _send_parallel_chunk(self, urls, notification_type, cancel):
        results: dict[str, tuple[bool, str]] = {}

        def task(url):
            if cancel is not None and cancel.is_set():
                return False, "Cancelled"
            return self._send_one_with_retry(url, notification_type, cancel)

        executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
        try:
            future_to_url = {executor.submit(task, url): url for url in urls}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    success, info = future.result()
                    results[url] = (success, info)
                except Exception as e:
                    results[url] = (False, f"Worker error: {e}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if any((not ok) and _is_quota_error_message(info)
               for ok, info in results.values()):
            raise QuotaExhausted("Daily quota exhausted")

        return [(u, *results.get(u, (False, "no response"))) for u in urls]

    # ---------- одиночный запрос с backoff ----------

    def _send_one_with_retry(self, url, notification_type, cancel):
        ctx = self._get_thread_ctx()
        service = ctx.service
        last = len(BACKOFF_DELAYS) - 1
        for attempt, delay in enumerate(BACKOFF_DELAYS):
            if delay > 0:
                self._sleep_with_cancel(delay, cancel)
            if cancel is not None and cancel.is_set():
                return False, "Cancelled"
            try:
                service.urlNotifications().publish(
                    body={"url": url, "type": notification_type}
                ).execute()
                return True, "OK"
            except socket.timeout as e:
                self._invalidate_thread_ctx()
                return False, f"Socket timeout: {e}"
            except HttpError as e:
                if _is_quota_error(e):
                    # Ставим явный маркер, чтобы внешний чек
                    # _is_quota_error_message(info) сработал даже
                    # если тело ошибки Google нестандартное.
                    if attempt == last:
                        return False, f"RATE_LIMIT_EXCEEDED: {e}"
                    continue
                return False, str(e)
            except Exception as e:
                if _is_network_error(e):
                    self._invalidate_thread_ctx()
                    return False, f"Network error: {e}"
                return False, f"Unexpected error: {e}"
        return False, "Retries exhausted"

    @staticmethod
    def _sleep_with_cancel(seconds: float, cancel):
        """
        Event.wait(timeout) просыпается сразу при cancel.set(),
        в отличие от цикла time.sleep(0.2).
        """
        if cancel is None:
            time.sleep(seconds)
            return
        cancel.wait(timeout=seconds)
