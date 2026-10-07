import configparser
import os
import tempfile
import time
from pathlib import Path


def _app_data_dir() -> Path:
    appdata = os.getenv("LOCALAPPDATA")
    base = Path(appdata) if appdata else Path.home()
    return base / "GoogleIndexingTool"


APP_DIR = _app_data_dir()
CONFIG_FILE = APP_DIR / "config.ini"

REPLACE_RETRIES = 5
REPLACE_RETRY_DELAY = 0.1


class ConfigError(Exception):
    pass


def load_key_path() -> str:
    if not CONFIG_FILE.exists():
        return ""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(CONFIG_FILE, encoding="utf-8")
    except configparser.Error as e:
        raise ConfigError(f"Файл {CONFIG_FILE} повреждён: {e}")
    try:
        return cfg.get("paths", "key_file", fallback="")
    except (configparser.NoSectionError, configparser.NoOptionError):
        return ""


def _atomic_replace(src: str, dst: Path) -> None:
    """
    os.replace с ретраями. Антивирус на Windows иногда держит файл
    открытым на доли секунды после закрытия — получаем PermissionError
    (WinError 32). Пять попыток с паузой 100 мс обычно хватает.
    """
    last_exc = None
    for _ in range(REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(REPLACE_RETRY_DELAY)
    if last_exc:
        raise last_exc
    raise OSError("os.replace failed")


def save_key_path(path: str) -> None:
    """
    Атомарная запись: NamedTemporaryFile сам управляет дескриптором
    и закрывает его при выходе из with. После этого os.replace
    срабатывает без WinError 32.
    """
    cfg = configparser.ConfigParser()
    cfg["paths"] = {"key_file": path}
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False,
            dir=str(CONFIG_FILE.parent), prefix=".config-", suffix=".tmp",
        )
        tmp_name = tmp_file.name
        try:
            with tmp_file as f:
                cfg.write(f)
            _atomic_replace(tmp_name, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as e:
        raise ConfigError(f"Не удалось сохранить {CONFIG_FILE}: {e}")
