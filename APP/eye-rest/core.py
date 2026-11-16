"""Non-GUI scheduling, settings and installation helpers for Eye Rest."""
from __future__ import annotations

import json
import math
import os
import tempfile
import warnings
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

APP_ID = "local.eyerest.App"
APP_NAME = "护眼休息"
BREAK_SECONDS = 60


def prepare_gui_environment() -> None:
    """Use host GTK modules when launched from a Snap-packaged editor/terminal."""
    for key in ("GTK_PATH", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX", "GTK_IM_MODULE_FILE",
                "GIO_MODULE_DIR", "GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE",
                "GDK_PIXBUF_MODULEDIR", "GSETTINGS_SCHEMA_DIR"):
        if "/snap/" in os.environ.get(key, ""):
            os.environ.pop(key, None)


def next_hour(wall: float) -> float:
    local = datetime.fromtimestamp(wall)
    return (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()


class HourScheduler:
    """Poll local wall time, skipping missed hours and clock discontinuities."""

    def __init__(self, wall: float, steady: float):
        self.seen = deque(maxlen=48)
        self.reset(wall, steady)

    def reset(self, wall: float, steady: float) -> None:
        self.next_due = next_hour(wall)
        self.last_wall, self.last_steady = wall, steady

    def poll(self, wall: float, steady: float, blocked: bool = False) -> bool:
        wall_gap, steady_gap = wall - self.last_wall, steady - self.last_steady
        self.last_wall, self.last_steady = wall, steady
        if steady_gap < 0 or wall_gap < 0 or wall_gap > 10 or abs(wall_gap - steady_gap) > 2:
            self.reset(wall, steady)
            return False
        if wall < self.next_due:
            return False
        due = self.next_due
        self.next_due = next_hour(wall)
        slot = datetime.fromtimestamp(due).strftime("%Y-%m-%d %H")
        if slot in self.seen:
            return False
        self.seen.append(slot)
        return not blocked and 0 <= wall - due <= 5


@dataclass(frozen=True)
class BreakTimer:
    deadline: float

    @classmethod
    def start(cls, steady: float, duration: int = BREAK_SECONDS) -> "BreakTimer":
        return cls(steady + duration)

    def remaining(self, steady: float) -> int:
        return max(0, math.ceil(self.deadline - steady))


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    autostart: Path
    applications: Path

    @classmethod
    def discover(cls) -> "Paths":
        home = Path.home()
        # The XDG specification requires absolute paths; ignore invalid values.
        def base(key: str, default: Path) -> Path:
            value = os.environ.get(key, "")
            return Path(value) if value and Path(value).is_absolute() else default
        config = base("XDG_CONFIG_HOME", home / ".config")
        data = base("XDG_DATA_HOME", home / ".local/share")
        return cls(config / "eye-rest", data / "eye-rest",
                   config / "autostart/eye-rest.desktop", data / "applications/eye-rest.desktop")

    @property
    def image(self) -> Path:
        return self.data / "images/background.png"

    @property
    def installed_app(self) -> Path:
        return self.data / "app"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Settings:
    def __init__(self, directory: Path):
        self.file = directory / "settings.json"
        self.custom_image = False
        self.position = None
        self.warning = None
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings must be an object")
            self.custom_image = raw.get("custom_image") is True
            position = raw.get("position")
            if (isinstance(position, list) and len(position) == 2
                    and all(type(v) is int and abs(v) < 1_000_000 for v in position)):
                self.position = tuple(position)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.warning = f"设置读取失败，已使用默认设置：{error}"

    def save(self) -> None:
        atomic_write(self.file, json.dumps({"version": 1, "custom_image": self.custom_image,
                                           "position": self.position}, ensure_ascii=False, indent=2) + "\n")


def import_image(source: Path, destination: Path) -> None:
    """Validate and normalize a user image; replace the old copy only on success."""
    from PIL import Image, ImageOps

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                if original.format not in {"PNG", "JPEG"}:
                    raise ValueError("请选择 PNG 或 JPEG 图片。")
                if original.width * original.height > 32_000_000:
                    raise ValueError("图片过大，请选择不超过 3200 万像素的图片。")
                original.load()
                normalized = ImageOps.exif_transpose(original)
                normalized.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                normalized = normalized.convert("RGBA" if "A" in normalized.getbands()
                                                or "transparency" in normalized.info else "RGB")
                fd, temporary = tempfile.mkstemp(prefix=".background-", suffix=".png", dir=destination.parent)
                os.close(fd)
                normalized.save(temporary, format="PNG")
                os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def clamp_position(position, width: int, height: int, areas) -> tuple[int, int]:
    """Keep the complete floating control on the nearest monitor's work area."""
    if not areas:
        return (0, 0)
    if position is None:
        x, y, w, h = areas[0]
        return max(x, x + w - width - 24), max(y, y + h - height - 24)
    px, py = position
    def distance(area):
        x, y, w, h = area
        return max(x - px, 0, px - (x + w - 1)) ** 2 + max(y - py, 0, py - (y + h - 1)) ** 2
    x, y, w, h = min(areas, key=distance)
    return min(max(px, x), max(x, x + w - width)), min(max(py, y), max(y, y + h - height))


def desktop_entry(entrypoint: Path, autostart: bool = False) -> str:
    # Desktop Exec has its own escaping rules, independent of shell quoting.
    argument = str(entrypoint.resolve()).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        argument = argument.replace(character, "\\" + character)
    # Key-file decoding happens before Exec parsing, so escape backslashes again.
    argument = argument.replace("\\", "\\\\")
    icon = str(entrypoint.parent / "assets/icon.svg").replace("\\", "\\\\")
    result = ("[Desktop Entry]\nType=Application\nVersion=1.0\n"
              "Name=护眼休息\nName[en]=Eye Rest\nComment=每到整点，休息一分钟\n"
              f'Exec=/usr/bin/python3 "{argument}"\nIcon={icon}\n'
              "Terminal=false\nCategories=Utility;\nStartupNotify=false\n"
              "StartupWMClass=eye-rest\nX-EyeRest-Managed=true\n")
    if autostart:
        result += "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\n"
    return result


def autostart_enabled(path: Path) -> bool:
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
        section = parser["Desktop Entry"]
        return not section.getboolean("Hidden", False) and section.getboolean("X-GNOME-Autostart-enabled", True)
    except (OSError, KeyError, ValueError, configparser.Error):
        return False


def remove_managed_entry(path: Path) -> None:
    if path.exists():
        if "X-EyeRest-Managed=true" not in path.read_text(encoding="utf-8"):
            raise ValueError(f"未删除非本程序创建的文件：{path}")
        path.unlink()


def write_managed_entry(path: Path, entrypoint: Path, autostart: bool = False) -> None:
    if path.exists() and "X-EyeRest-Managed=true" not in path.read_text(encoding="utf-8"):
        raise ValueError(f"同名文件已存在，为避免覆盖请先检查：{path}")
    atomic_write(path, desktop_entry(entrypoint, autostart))
