from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import urlparse

from config_store import ConfigError, load_key_path, save_key_path
from indexing_client import (
    DEFAULT_NOTIFICATION_TYPE,
    DEFAULT_SEND_MODE,
    IndexingClient,
    KeyValidationError,
    NOTIFICATION_TYPES,
    QuotaExhausted,
    SEND_MODES,
)
from url_utils import (
    build_full_urls,
    clean_url_line,
    normalize_domain,
    split_by_domain,
)

MAX_URLS = 100
QUEUE_POLL_MS = 100
COUNTER_DEBOUNCE_MS = 250
WORKER_JOIN_TIMEOUT = 0.5
TERMINAL_KINDS = ("quota", "error", "done")


def _iter_lines(text):
    """Итератор по строкам без создания полного списка через splitlines()."""
    start = 0
    n = len(text)
    while start < n:
        idx = text.find("\n", start)
        if idx == -1:
            yield text[start:]
            start = n
        else:
            yield text[start:idx]
            start = idx + 1


def _is_probably_url(line):
    """Дешёвая проверка: непустая строка, не начинается с '#'."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Google Indexing API - переиндексация")
        root.geometry("740x780")
        root.resizable(False, False)

        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._busy = False
        self._timer_id: str | None = None
        self._counter_timer_id: str | None = None
        self._closing = False

        self._build_ui()
        self._load_saved_config()

        self._timer_id = self.root.after(QUEUE_POLL_MS, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------

    def _build_ui(self):
        tk.Label(self.root, text="Файл ключа сервисного аккаунта (.json):").pack(
            anchor="w", padx=10, pady=(10, 2))
        key_frame = tk.Frame(self.root)
        key_frame.pack(padx=10, fill="x")
        self.key_entry = tk.Entry(key_frame)
        self.key_entry.pack(side="left", fill="x", expand=True)
        tk.Button(key_frame, text="Выбрать...", width=12,
                  command=self.choose_key).pack(side="left", padx=(6, 0))

        tk.Label(self.root, text="Домен (например example.com или "
                                 "https://example.com):").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.domain_entry = tk.Entry(self.root)
        self.domain_entry.pack(padx=10, fill="x")

        tk.Label(self.root, text="Тип запроса:").pack(
            anchor="w", padx=10, pady=(12, 2))
        type_frame = tk.Frame(self.root)
        type_frame.pack(padx=10, fill="x", anchor="w")
        self.notification_type = tk.StringVar(value=DEFAULT_NOTIFICATION_TYPE)
        tk.Radiobutton(
            type_frame, text="Обновление (URL_UPDATED)",
            variable=self.notification_type, value="URL_UPDATED",
        ).pack(side="left", padx=(0, 12))
        tk.Radiobutton(
            type_frame, text="Удаление (URL_DELETED)",
            variable=self.notification_type, value="URL_DELETED",
        ).pack(side="left")

        tk.Label(self.root, text="Режим отправки:").pack(
            anchor="w", padx=10, pady=(12, 2))
        mode_frame = tk.Frame(self.root)
        mode_frame.pack(padx=10, fill="x", anchor="w")
        self.send_mode = tk.StringVar(value=DEFAULT_SEND_MODE)
        tk.Radiobutton(
            mode_frame, text="Пакетный (batch, рекомендуется)",
            variable=self.send_mode, value="batch",
        ).pack(side="left", padx=(0, 12))
        tk.Radiobutton(
            mode_frame, text="Параллельный (5 потоков)",
            variable=self.send_mode, value="parallel",
        ).pack(side="left")

        tk.Label(self.root, text="URL для отправки (по одному в строке):").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.urls_text = scrolledtext.ScrolledText(self.root, height=14)
        self.urls_text.pack(padx=10, fill="both", expand=True)
        self.urls_text.bind("<KeyRelease>", self._on_text_change)
        # <<Paste>> срабатывает ДО фактической вставки текста в виджет.
        # Откладываем пересчёт на 10 мс, чтобы Tkinter успел вставить.
        self.urls_text.bind(
            "<<Paste>>",
            lambda e: self.root.after(10, self._on_text_change))

        self.counter_label = tk.Label(self.root, text=f"URL: 0 / {MAX_URLS}",
                                      anchor="e")
        self.counter_label.pack(padx=10, fill="x")

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=8)
        tk.Button(btn_frame, text="Загрузить URL из файла",
                  command=self.load_urls_file).pack(side="left", padx=4)
        self.send_btn = tk.Button(btn_frame, text="Отправить",
                                  command=self.submit)
        self.send_btn.pack(side="left", padx=4)
        self.cancel_btn = tk.Button(btn_frame, text="Отмена", state="disabled",
                                    command=self.cancel)
        self.cancel_btn.pack(side="left", padx=4)
        tk.Button(btn_frame, text="Очистить",
                  command=self.clear).pack(side="left", padx=4)

        self.log = tk.Text(self.root, height=12, state="disabled")
        self.log.pack(padx=10, pady=(0, 10), fill="both")

    def _load_saved_config(self):
        try:
            saved = load_key_path()
        except ConfigError as e:
            messagebox.showwarning("Конфигурация", str(e))
            return
        if saved:
            self.key_entry.insert(0, saved)

    # ---------- helpers ----------

    def log_msg(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ---------- debounce счётчика ----------

    def _on_text_change(self, event=None):
        # Откладываем пересчёт, чтобы не блокировать ввод при
        # вставке больших списков.
        if self._counter_timer_id is not None:
            try:
                self.root.after_cancel(self._counter_timer_id)
            except tk.TclError:
                pass
        self._counter_timer_id = self.root.after(
            COUNTER_DEBOUNCE_MS, self.update_counter)

    def update_counter(self):
        self._counter_timer_id = None
        raw = self.urls_text.get("1.0", "end")

        # Итератор по строкам без создания полного списка через
        # splitlines(). На 100 000 строк это экономит десятки МБ
        # и не фризит главный поток.
        count = 0
        stopped_early = False
        for line in _iter_lines(raw):
            if clean_url_line(line):
                count += 1
                if count > MAX_URLS:
                    stopped_early = True
                    break

        if stopped_early:
            text = f"URL: > {MAX_URLS} / {MAX_URLS}"
            self.counter_label.config(text=text + "  превышен лимит", fg="red")
        else:
            text = f"URL: {count} / {MAX_URLS}"
            self.counter_label.config(text=text, fg="black")

    def choose_key(self):
        path = filedialog.askopenfilename(
            title="Выберите JSON-ключ сервисного аккаунта",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self.key_entry.delete(0, "end")
        self.key_entry.insert(0, path)
        try:
            save_key_path(path)
        except ConfigError as e:
            messagebox.showwarning("Конфигурация", str(e))

    def load_urls_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл со списком URL",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror("Файл", f"Не удалось прочитать файл: {e}")
            return
        self.urls_text.delete("1.0", "end")
        self.urls_text.insert("1.0", content)
        if self._counter_timer_id is not None:
            try:
                self.root.after_cancel(self._counter_timer_id)
            except tk.TclError:
                pass
            self._counter_timer_id = None
        self.update_counter()

    def clear(self):
        if self._busy:
            return
        self.urls_text.delete("1.0", "end")
        self.update_counter()
        self.clear_log()

    # ---------- основное действие ----------

    def submit(self):
        if self._busy:
            return

        key_path = self.key_entry.get().strip()
        domain_raw = self.domain_entry.get().strip()
        notification_type = self.notification_type.get()
        send_mode = self.send_mode.get()

        if notification_type not in NOTIFICATION_TYPES:
            messagebox.showwarning("Тип запроса", "Выберите тип запроса.")
            return
        if send_mode not in SEND_MODES:
            messagebox.showwarning("Режим отправки", "Выберите режим отправки.")
            return

        if not domain_raw:
            messagebox.showwarning("Домен", "Укажите домен сайта.")
            return

        domain = normalize_domain(domain_raw)
        pd = urlparse(domain)
        if pd.scheme not in ("http", "https") or not pd.netloc:
            messagebox.showwarning(
                "Домен",
                "Не удалось распознать домен. Примеры:\n"
                "example.com\n"
                "https://example.com\n"
                "http://sub.example.com")
            return

        # Если пользователь ввёл домен с путём, путь игнорируется.
        # Предупреждаем, чтобы не было сюрпризов.
        raw_parsed = urlparse(domain_raw)
        if raw_parsed.path and raw_parsed.path not in ("", "/"):
            if not messagebox.askyesno(
                "Домен содержит путь",
                f"Из введённого домена будет использован только хост:\n"
                f"  введено:   {domain_raw}\n"
                f"  будет:     {domain}\n\n"
                f"Путь «{raw_parsed.path}» будет проигнорирован, "
                "относительные URL будут склеиваться с корнем сайта.\n\n"
                "Продолжить?"):
                return

        # Если домен был в Unicode, а стал Punycode — сообщим пользователю.
        # Google Search Console должен содержать тот же формат.
        if domain_raw and domain:
            rp_raw = urlparse(domain_raw)
            rp_norm = urlparse(domain)
            if rp_raw.netloc and rp_raw.netloc.lower() != rp_norm.netloc.lower():
                self.log_msg(
                    f"Домен нормализован: {rp_raw.netloc} -> {rp_norm.netloc}")
                self.log_msg(
                    "Убедитесь, что этот формат добавлен в Search Console, "
                    "иначе Google вернёт 403 PERMISSION_DENIED.")

        raw = self.urls_text.get("1.0", "end")

        # Один проход: собираем строки и считаем непустые.
        # На 100 000 строк splitlines() создал бы список на десятки МБ,
        # поэтому используем итератор _iter_lines. Точную очистку
        # (BOM, кавычки, \r) делает build_full_urls ниже.
        lines = []
        total_non_empty = 0
        too_many = False
        for line in _iter_lines(raw):
            lines.append(line)
            if _is_probably_url(line):
                total_non_empty += 1
                if total_non_empty > MAX_URLS:
                    too_many = True
                    break

        if total_non_empty == 0:
            messagebox.showwarning("Нет данных", "Введите хотя бы один URL.")
            return
        if too_many:
            messagebox.showwarning(
                "Превышен лимит",
                f"За одну отправку можно отправить не более {MAX_URLS} URL.\n"
                "В списке больше допустимого.")
            return

        if notification_type == "URL_DELETED":
            if not messagebox.askyesno(
                "Подтвердите удаление",
                f"Будет отправлен запрос на УДАЛЕНИЕ {total_non_empty} URL "
                "из выдачи Google.\n\n"
                "Страницы исчезнут из результатов поиска после обработки "
                "запроса (обычно от нескольких дней до нескольких недель).\n\n"
                "Продолжить?"):
                return

        full_urls, invalid_lines = build_full_urls(domain, lines)
        own_urls, foreign_urls = split_by_domain(full_urls, domain)

        self.clear_log()
        for u in foreign_urls:
            self.log_msg(f"ПРОПУЩЕН (чужой домен): {u}")
        for u in invalid_lines:
            self.log_msg(f"ПРОПУЩЕН (невалидная строка): {u}")

        if not own_urls:
            messagebox.showwarning(
                "Нет подходящих URL",
                f"Ни один URL не принадлежит домену {domain}.")
            return

        self._set_busy(True)
        self.cancel_event.clear()

        ctx = {
            "key_path": key_path,
            "own_urls": own_urls,
            "foreign_count": len(foreign_urls),
            "invalid_count": len(invalid_lines),
            "total_lines": total_non_empty,
            "notification_type": notification_type,
            "send_mode": send_mode,
        }
        self.worker = threading.Thread(target=self._worker, args=(ctx,),
                                       daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_event.set()
        self.log_msg("Запрошена отмена...")

    # ---------- worker ----------

    def _worker(self, ctx):
        def safe_put(item):
            # queue.Queue.put безопасен сам по себе, но если окно уже
            # закрывается, класть сообщения бессмысленно — drain-цикл
            # больше не запустится. Пропускаем.
            if self._closing:
                return
            self.msg_queue.put(item)

        try:
            client = IndexingClient(ctx["key_path"],
                                    send_mode=ctx["send_mode"])
        except KeyValidationError as e:
            safe_put(("error", str(e)))
            return
        except Exception as e:
            safe_put(("error", f"Не удалось инициализировать API: {e}"))
            return

        def on_result(url, ok, info):
            safe_put(("result", (url, ok, info)))

        try:
            ok, fail = client.send(
                ctx["own_urls"],
                notification_type=ctx["notification_type"],
                on_result=on_result,
                cancel=self.cancel_event,
            )
        except QuotaExhausted:
            safe_put(("quota", None))
            return
        except Exception as e:
            safe_put(("error", f"Сбой при отправке: {e}"))
            return

        ctx["ok"] = ok
        ctx["fail"] = fail
        safe_put(("done", ctx))

    # ---------- очередь ----------

    def _drain_queue(self):
        try:
            if self._closing:
                return

            messages = []
            try:
                while True:
                    messages.append(self.msg_queue.get_nowait())
            except queue.Empty:
                pass

            if messages:
                self._render_messages(messages)

            got_terminal = any(k in TERMINAL_KINDS for k, _ in messages)
            if (self._busy
                    and self.worker is not None
                    and not self.worker.is_alive()
                    and not got_terminal):
                self._set_busy(False)
                self.log_msg("")
                self.log_msg("Фоновый поток завершился без отчёта.")
                messagebox.showerror(
                    "Фоновый поток остановлен",
                    "Отправка завершилась неожиданно, отчёт не получен.\n\n"
                    "Проверьте логи, повторите запуск.")
        finally:
            if not self._closing:
                self._timer_id = self.root.after(
                    QUEUE_POLL_MS, self._drain_queue)

    def _render_messages(self, messages):
        self.log.config(state="normal")
        try:
            for kind, payload in messages:
                if kind == "result":
                    url, ok, info = payload
                    if ok:
                        self.log.insert("end", f"OK: {url}\n")
                    else:
                        self.log.insert(
                            "end", f"ОТКЛОНЁН API: {url} - {info}\n")
                elif kind == "quota":
                    self.log.insert("end", "\nДНЕВНАЯ КВОТА ИСЧЕРПАНА.\n")
                    self.log.insert(
                        "end",
                        "Отправка остановлена. Оставшиеся URL "
                        "отправьте после сброса квоты "
                        "(00:00 PST, ~10:00 Минск).\n")
                elif kind == "error":
                    self.log.insert("end", f"ОШИБКА: {payload}\n")
                elif kind == "done":
                    self._render_report(payload)
            self.log.see("end")
        finally:
            self.log.config(state="disabled")

        for kind, payload in messages:
            if kind == "quota":
                self._set_busy(False)
                messagebox.showwarning(
                    "Квота исчерпана",
                    "Дневной лимит Google Indexing API (200 URL) исчерпан.\n\n"
                    "Отправка остановлена. Попробуйте после сброса квоты "
                    "(00:00 PST / ~10:00 Минск).")
            elif kind == "error":
                self._set_busy(False)
                messagebox.showerror("Ошибка", payload)
            elif kind == "done":
                self._set_busy(False)
                ctx = payload
                ntype = ctx.get("notification_type",
                                DEFAULT_NOTIFICATION_TYPE)
                type_label = ("URL_UPDATED (обновление)"
                              if ntype == "URL_UPDATED"
                              else "URL_DELETED (удаление)")
                mode_label = ("batch"
                              if ctx.get("send_mode") == "batch"
                              else "parallel")
                messagebox.showinfo(
                    "Отчёт",
                    f"Тип запроса: {type_label}\n"
                    f"Режим: {mode_label}\n\n"
                    f"Всего строк: {ctx['total_lines']}\n"
                    f"Успешно отправлено: {ctx['ok']}\n"
                    f"Чужой домен: {ctx['foreign_count']}\n"
                    f"Невалидные строки: {ctx['invalid_count']}\n"
                    f"Отклонено API: {ctx['fail']}")

    def _render_report(self, ctx):
        ntype = ctx.get("notification_type", DEFAULT_NOTIFICATION_TYPE)
        type_label = ("URL_UPDATED (обновление)"
                      if ntype == "URL_UPDATED"
                      else "URL_DELETED (удаление)")
        mode_label = "batch" if ctx.get("send_mode") == "batch" else "parallel"
        self.log.insert("end", "\n---------- ОТЧЁТ ----------\n")
        self.log.insert("end", f"Тип запроса:                 {type_label}\n")
        self.log.insert("end", f"Режим отправки:              {mode_label}\n")
        self.log.insert("end", f"Всего строк:                 {ctx['total_lines']}\n")
        self.log.insert("end", f"Успешно отправлено:          {ctx['ok']}\n")
        self.log.insert("end",
                        f"Чужой домен (не отправлены): {ctx['foreign_count']}\n")
        self.log.insert("end",
                        f"Невалидные строки:           {ctx['invalid_count']}\n")
        self.log.insert("end", f"Отклонено API:               {ctx['fail']}\n")
        self.log.insert("end", "---------------------------\n")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.send_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def _on_close(self):
        self._closing = True
        if self._timer_id is not None:
            try:
                self.root.after_cancel(self._timer_id)
            except tk.TclError:
                pass
            self._timer_id = None
        if self._counter_timer_id is not None:
            try:
                self.root.after_cancel(self._counter_timer_id)
            except tk.TclError:
                pass
            self._counter_timer_id = None
        self.cancel_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=WORKER_JOIN_TIMEOUT)
        self.root.destroy()
