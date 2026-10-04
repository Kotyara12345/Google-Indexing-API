import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import urlparse

from config_store import ConfigError, load_key_path, save_key_path
from indexing_client import IndexingClient, KeyValidationError
from url_utils import build_full_urls, normalize_domain, split_by_domain

MAX_URLS = 100
QUEUE_POLL_MS = 100


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Google Indexing API — переиндексация")
        root.geometry("720x700")
        root.resizable(False, False)

        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._busy = False
        self._timer_id: str | None = None
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
        tk.Button(key_frame, text="Выбрать…", width=12,
                  command=self.choose_key).pack(side="left", padx=(6, 0))

        tk.Label(self.root, text="Домен (например example.com или "
                                 "https://example.com):").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.domain_entry = tk.Entry(self.root)
        self.domain_entry.pack(padx=10, fill="x")

        tk.Label(self.root, text="URL для отправки (по одному в строке):").pack(
            anchor="w", padx=10, pady=(12, 2))
        self.urls_text = scrolledtext.ScrolledText(self.root, height=14)
        self.urls_text.pack(padx=10, fill="both", expand=True)
        self.urls_text.bind("<KeyRelease>", self.update_counter)
        self.urls_text.bind(
            "<<Paste>>", lambda e: self.root.after(50, self.update_counter))

        self.counter_label = tk.Label(self.root, text=f"URL: 0 / {MAX_URLS}",
                                      anchor="e")
        self.counter_label.pack(padx=10, fill="x")

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=8)
        tk.Button(btn_frame, text="Загрузить URL из файла",
                  command=self.load_urls_file).pack(side="left", padx=4)
        self.send_btn = tk.Button(btn_frame, text="Отправить на переиндексацию",
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

    def update_counter(self, event=None):
        raw = self.urls_text.get("1.0", "end").strip()
        count = len([l for l in raw.splitlines() if l.strip()])
        text = f"URL: {count} / {MAX_URLS}"
        if count > MAX_URLS:
            self.counter_label.config(text=text + "  ⚠ превышен лимит", fg="red")
        else:
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
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror("Файл", f"Не удалось прочитать файл: {e}")
            return
        self.urls_text.delete("1.0", "end")
        self.urls_text.insert("1.0", content)
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

        raw = self.urls_text.get("1.0", "end")
        lines = [l for l in raw.splitlines() if l.strip()]
        if not lines:
            messagebox.showwarning("Нет данных", "Введите хотя бы один URL.")
            return
        if len(lines) > MAX_URLS:
            messagebox.showwarning(
                "Превышен лимит",
                f"За одну отправку можно отправить не более {MAX_URLS} URL.\n"
                f"Сейчас в списке: {len(lines)}.")
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
            "total_lines": len(lines),
        }
        self.worker = threading.Thread(target=self._worker, args=(ctx,),
                                       daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_event.set()
        self.log_msg("Запрошена отмена…")

    # ---------- worker ----------

    def _worker(self, ctx):
        try:
            client = IndexingClient(ctx["key_path"])
        except KeyValidationError as e:
            self.msg_queue.put(("error", str(e)))
            return
        except Exception as e:
            self.msg_queue.put(("error", f"Не удалось инициализировать API: {e}"))
            return

        def on_result(url, ok, info):
            self.msg_queue.put(("result", (url, ok, info)))

        try:
            ok, fail = client.send(ctx["own_urls"], on_result=on_result,
                                   cancel=self.cancel_event)
        except Exception as e:
            self.msg_queue.put(("error", f"Сбой при отправке: {e}"))
            return

        ctx["ok"] = ok
        ctx["fail"] = fail
        self.msg_queue.put(("done", ctx))

    # ---------- очередь и завершение ----------

    def _drain_queue(self):
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "result":
                    url, ok, info = payload
                    if ok:
                        self.log_msg(f"OK: {url}")
                    else:
                        self.log_msg(f"ОТКЛОНЁН API: {url} — {info}")
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("Ошибка", payload)
                elif kind == "done":
                    self._set_busy(False)
                    self._show_report(payload)
        except queue.Empty:
            pass
        self._timer_id = self.root.after(QUEUE_POLL_MS, self._drain_queue)

    def _show_report(self, ctx):
        report = (
            "────────── ОТЧЁТ ──────────\n"
            f"Всего строк:                 {ctx['total_lines']}\n"
            f"Успешно отправлено:          {ctx['ok']}\n"
            f"Чужой домен (не отправлены): {ctx['foreign_count']}\n"
            f"Невалидные строки:           {ctx['invalid_count']}\n"
            f"Отклонено API:               {ctx['fail']}\n"
            "───────────────────────────"
        )
        self.log_msg("\n" + report)
        messagebox.showinfo(
            "Отчёт о переиндексации",
            f"Всего строк: {ctx['total_lines']}\n\n"
            f"✅ Успешно отправлено: {ctx['ok']}\n"
            f"⚠ Чужой домен: {ctx['foreign_count']}\n"
            f"⚠ Невалидные строки: {ctx['invalid_count']}\n"
            f"❌ Отклонено API: {ctx['fail']}")

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
        self.cancel_event.set()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=0.2)
        self.root.destroy()