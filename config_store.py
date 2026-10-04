import configparser
import os

CONFIG_FILE = "config.ini"


class ConfigError(Exception):
    pass


def load_key_path() -> str:
    if not os.path.exists(CONFIG_FILE):
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


def save_key_path(path: str) -> None:
    cfg = configparser.ConfigParser()
    cfg["paths"] = {"key_file": path}
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)
    except OSError as e:
        raise ConfigError(f"Не удалось сохранить {CONFIG_FILE}: {e}")