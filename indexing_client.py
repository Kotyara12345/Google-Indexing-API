import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import BatchHttpRequest

SCOPES = ["https://www.googleapis.com/auth/indexing"]
BATCH_URI = "https://indexing.googleapis.com/batch"
BATCH_CHUNK = 50
USE_BATCH = True
REQUIRED_KEY_FIELDS = ("client_email", "private_key", "token_uri", "project_id")


class KeyValidationError(Exception):
    pass


class BatchUnavailable(Exception):
    pass


def validate_key_file(path: str) -> None:
    if not path or not os.path.exists(path):
        raise KeyValidationError("Файл ключа не найден. Выберите JSON-ключ.")
    try:
        with open(path, "r", encoding="utf-8") as f:
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


class IndexingClient:
    def __init__(self, key_path: str, use_batch: bool = USE_BATCH):
        validate_key_file(key_path)
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=SCOPES
        )
        self._service = build("indexing", "v3", credentials=creds,
                              cache_discovery=False)
        self._use_batch = use_batch

    def send(self, urls, on_result=None, cancel=None):
        ok = fail = 0
        for i in range(0, len(urls), BATCH_CHUNK):
            if cancel is not None and cancel.is_set():
                break
            chunk = urls[i:i + BATCH_CHUNK]
            for url, success, info in self._send_chunk(chunk):
                if success:
                    ok += 1
                else:
                    fail += 1
                if on_result is not None:
                    on_result(url, success, info)
        return ok, fail

    # ---------- внутреннее ----------

    def _send_chunk(self, urls):
        if self._use_batch:
            try:
                return self._send_batch_chunk(urls)
            except BatchUnavailable:
                self._use_batch = False
        return self._send_sequential_chunk(urls)

    def _send_batch_chunk(self, urls):
        results: dict[str, tuple[bool, str]] = {}

        def callback(request_id, response, exception):
            if exception is not None:
                results[request_id] = (False, str(exception))
            else:
                results[request_id] = (True, "OK")

        batch = BatchHttpRequest(callback=callback, batch_uri=BATCH_URI)
        for url in urls:
            req = self._service.urlNotifications().publish(
                body={"url": url, "type": "URL_UPDATED"}
            )
            batch.add(req, request_id=url)

        try:
            batch.execute()
        except HttpError as e:
            msg = str(e).lower()
            if (e.resp.status in (400, 404, 405, 501)
                    and "batch" in msg
                    and not results):
                raise BatchUnavailable(str(e))
            for u in urls:
                results.setdefault(u, (False, f"Batch HTTP error: {e}"))
        except Exception as e:
            if not results:
                raise BatchUnavailable(f"Batch transport error: {e}")
            for u in urls:
                results.setdefault(u, (False, f"Batch error: {e}"))

        return [(u, *results.get(u, (False, "no response"))) for u in urls]

    def _send_sequential_chunk(self, urls):
        results = []
        for url in urls:
            try:
                self._service.urlNotifications().publish(
                    body={"url": url, "type": "URL_UPDATED"}
                ).execute()
                results.append((url, True, "OK"))
            except HttpError as e:
                results.append((url, False, str(e)))
            except Exception as e:
                results.append((url, False, f"Network error: {e}"))
        return results