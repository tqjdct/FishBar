"""FishBar - a tiny, unobtrusive bottom-edge reading overlay.

The prototype deliberately uses only the Python standard library so it can be
run directly on a Windows machine with Python 3.11+ installed.  It keeps a
virtual mouse sensor at the panel's last position and opens the reading panel
when the pointer returns to that position.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from bisect import bisect_right
import json
import os
import re
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import font as tkfont
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk
except ImportError:  # Keep a native Tk fallback on minimal Python installs.
    Image = ImageDraw = ImageFont = ImageGrab = ImageTk = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Windows helpers

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    user32 = ctypes.windll.user32

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def enable_dpi_awareness() -> None:
        """Make Tk coordinates match physical pixels on modern Windows."""
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                shcore = ctypes.windll.shcore
                shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

    def cursor_position() -> tuple[int, int]:
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def taskbar_rectangle(default_width: int, default_height: int) -> tuple[int, int, int, int]:
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if hwnd:
            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                if rect.right > rect.left and rect.bottom > rect.top:
                    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        return 0, max(0, default_height - 48), default_width, default_height

    def taskbar_top(default_width: int, default_height: int) -> int:
        left, top, right, bottom = taskbar_rectangle(default_width, default_height)
        # The app is intentionally a bottom-taskbar prototype. If Windows has
        # placed the taskbar on another edge, use the conventional fallback.
        if top > default_height // 2 and right > left and bottom > top:
            return top
        return max(0, default_height - 48)

    def apply_toolwindow(hwnd: int) -> None:
        """Keep the overlay out of Alt+Tab and prevent focus stealing."""
        try:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
        except AttributeError:  # pragma: no cover - old 32-bit Windows
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_LAYERED = 0x00080000
        current = get_long(hwnd, GWL_EXSTYLE)
        set_long(hwnd, GWL_EXSTYLE, current | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED)

    def top_level_window(hwnd: int) -> int:
        """Resolve Tk's internal child HWND to its popup/top-level HWND."""
        parent = user32.GetParent(hwnd)
        return int(parent) if parent else int(hwnd)

    def set_window_topmost(hwnd: int, enabled: bool) -> None:
        HWND_TOPMOST = ctypes.c_void_p(-1)
        HWND_NOTOPMOST = ctypes.c_void_p(-2)
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, HWND_TOPMOST if enabled else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)

    def register_hotkey(hwnd: int | None, hotkey_id: int, modifiers: int, key: int) -> bool:
        return bool(user32.RegisterHotKey(hwnd, hotkey_id, modifiers, key))

    def unregister_hotkey(hwnd: int | None, hotkey_id: int) -> None:
        user32.UnregisterHotKey(hwnd, hotkey_id)

    def add_private_font(path: str) -> bool:
        gdi32 = ctypes.windll.gdi32
        gdi32.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        gdi32.AddFontResourceExW.restype = ctypes.c_int
        return gdi32.AddFontResourceExW(path, 0x10, None) > 0  # FR_PRIVATE

    def remove_private_font(path: str) -> None:
        if not path:
            return
        gdi32 = ctypes.windll.gdi32
        gdi32.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        gdi32.RemoveFontResourceExW.restype = wintypes.BOOL
        gdi32.RemoveFontResourceExW(path, 0x10, None)

else:  # pragma: no cover - a small fallback keeps the prototype testable elsewhere
    user32 = None

    def enable_dpi_awareness() -> None:
        pass

    def cursor_position() -> tuple[int, int]:
        return 0, 0

    def taskbar_rectangle(default_width: int, default_height: int) -> tuple[int, int, int, int]:
        return 0, max(0, default_height - 48), default_width, default_height

    def taskbar_top(default_width: int, default_height: int) -> int:
        return max(0, default_height - 48)

    def apply_toolwindow(hwnd: int) -> None:
        pass

    def top_level_window(hwnd: int) -> int:
        return hwnd

    def set_window_topmost(hwnd: int, enabled: bool) -> None:
        pass

    def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
        pass

    def register_hotkey(hwnd: int | None, hotkey_id: int, modifiers: int, key: int) -> bool:
        return False

    def unregister_hotkey(hwnd: int | None, hotkey_id: int) -> None:
        pass

    def add_private_font(path: str) -> bool:
        return False

    def remove_private_font(path: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Text and persistence


CHAPTER_RE = re.compile(
    r"^\s*(第[零〇一二两三四五六七八九十百千万亿\d]+\s*[章节回卷集部篇].*|chapter\s+\d+.*)$",
    re.IGNORECASE,
)


def font_family_from_file(path: str) -> str:
    """Read a TTF/OTF name table and return its preferred family name."""
    try:
        data = Path(path).read_bytes()
        font_offset = 0
        if data[:4] == b"ttcf":
            if len(data) < 16:
                return ""
            font_offset = struct.unpack_from(">I", data, 12)[0]
        if font_offset + 12 > len(data):
            return ""
        table_count = struct.unpack_from(">H", data, font_offset + 4)[0]
        name_offset = name_length = 0
        directory = font_offset + 12
        for index in range(table_count):
            entry = directory + index * 16
            if entry + 16 > len(data):
                return ""
            tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, entry)
            if tag == b"name":
                name_offset, name_length = offset, length
                break
        if not name_offset or name_offset + name_length > len(data) or name_offset + 6 > len(data):
            return ""
        _format, record_count, string_offset = struct.unpack_from(">HHH", data, name_offset)
        storage = name_offset + string_offset
        candidates: list[tuple[int, int, str]] = []
        for index in range(record_count):
            record = name_offset + 6 + index * 12
            if record + 12 > len(data):
                break
            platform, encoding, language, name_id, length, offset = struct.unpack_from(">HHHHHH", data, record)
            if name_id not in (1, 16) or storage + offset + length > len(data):
                continue
            raw = data[storage + offset : storage + offset + length]
            try:
                if platform in (0, 3):
                    value = raw.decode("utf-16-be")
                elif platform == 1:
                    value = raw.decode("mac_roman")
                else:
                    continue
            except UnicodeError:
                continue
            value = value.replace("\x00", "").strip()
            if value:
                # Prefer typographic family (16), then Chinese/English
                # Windows records, then any valid family record.
                name_priority = 0 if name_id == 16 else 1
                language_priority = 0 if language in (0x0804, 0x0409) else 1
                candidates.append((name_priority, language_priority, value))
        return min(candidates)[2] if candidates else ""
    except (OSError, struct.error, ValueError):
        return ""


def decode_text(raw: bytes) -> str:
    """Decode common mainland-Chinese novel encodings without dependencies."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    # Strict validation is important here.  Interpreting UTF-8 bytes as a
    # legacy Chinese code page can produce more mojibake code points than the
    # correct decode, so decoded string length is not a useful signal.
    for encoding in ("utf-8", "gb18030", "gbk", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class Reader:
    def __init__(self, page_chars: int = 520) -> None:
        self.page_chars = page_chars
        self.layout_columns = 0
        self.layout_lines = 0
        self.layout_width = 0
        self.measure_text: Callable[[str], int] | None = None
        self._character_widths: dict[str, int] = {}
        self.path: str | None = None
        self.title = "还没有导入小说"
        self.pages: list[str] = []
        self.page_starts: list[int] = []
        self.page_ends: list[int] = []
        self.full_text = ""
        self.page_index = 0
        self.chapter_for_page: dict[int, str] = {}
        self._source_lines: list[tuple[str, int]] = []

    def set_layout(
        self,
        columns: int,
        lines: int,
        width: int = 0,
        measure_text: Callable[[str], int] | None = None,
    ) -> None:
        self.layout_columns = max(0, int(columns))
        self.layout_lines = max(0, int(lines))
        self.layout_width = max(0, int(width))
        self.measure_text = measure_text
        self._character_widths.clear()

    def _wrap_line(self, line: str, source_start: int) -> list[tuple[str, int, int]]:
        """Split one source line using the active font's measured pixel width."""
        if not line:
            return [("", source_start, source_start)]
        if not self.layout_width or self.measure_text is None:
            columns = max(1, self.layout_columns)
            return [
                (line[index : index + columns], source_start + index, source_start + min(len(line), index + columns))
                for index in range(0, len(line), columns)
            ]

        wrapped: list[tuple[str, int, int]] = []
        start = 0
        current_width = 0
        for index, character in enumerate(line):
            character_width = self._character_widths.get(character)
            if character_width is None:
                # Text tabs use a position-dependent tab stop.  Four spaces
                # are a conservative and deterministic approximation for the
                # rare tab found in a plain-text novel.
                sample = "    " if character == "\t" else character
                character_width = max(0, int(self.measure_text(sample)))
                self._character_widths[character] = character_width
            if index > start and current_width + character_width > self.layout_width:
                wrapped.append((line[start:index], source_start + start, source_start + index))
                start = index
                current_width = 0
            current_width += character_width
        wrapped.append((line[start:], source_start + start, source_start + len(line)))
        return wrapped

    def _paginate_by_lines(self) -> None:
        """Build pages from measured display lines without dropping source text."""
        lines_per_page = max(1, self.layout_lines)
        page_lines: list[tuple[str, int, int]] = []
        chapter_pages: dict[int, str] = {}
        display_line_count = 0
        self.pages = []
        self.page_starts = []
        self.page_ends = []

        def finish_page() -> None:
            if not page_lines:
                return
            self.pages.append("\n".join(item[0] for item in page_lines))
            self.page_starts.append(page_lines[0][1])
            self.page_ends.append(page_lines[-1][2])
            page_lines.clear()

        for line, source_start in self._source_lines:
            heading = line.strip()
            if CHAPTER_RE.match(heading):
                chapter_pages[display_line_count // lines_per_page] = heading
            for display_line in self._wrap_line(line, source_start):
                page_lines.append(display_line)
                display_line_count += 1
                if len(page_lines) == lines_per_page:
                    finish_page()
        finish_page()
        if not self.pages:
            self.pages = ["（这本书是空的。）"]
            self.page_starts = [0]
            self.page_ends = [0]
        self.chapter_for_page = {
            min(page_index, len(self.pages) - 1): heading
            for page_index, heading in chapter_pages.items()
        }

    def _paginate_by_characters(self) -> None:
        """Fallback used only before a visual layout has been supplied."""
        if not self.full_text:
            self.pages = ["（这本书是空的。）"]
            self.page_starts = [0]
            self.page_ends = [0]
            self.chapter_for_page = {}
            return
        size = max(1, int(self.page_chars))
        self.pages = [self.full_text[index : index + size] for index in range(0, len(self.full_text), size)]
        self.page_starts = list(range(0, len(self.full_text), size))
        self.page_ends = [min(len(self.full_text), start + size) for start in self.page_starts]
        self.chapter_for_page = {}
        for match in re.finditer(r"(?m)^.*$", self.full_text):
            heading = match.group(0).strip()
            if CHAPTER_RE.match(heading):
                self.chapter_for_page[match.start() // size] = heading

    def page_for_offset(self, offset: int) -> int:
        if not self.pages or not self.page_starts:
            return 0
        target = max(0, min(len(self.full_text), int(offset)))
        return min(len(self.pages) - 1, max(0, bisect_right(self.page_starts, target) - 1))

    def current_offset(self) -> int:
        if not self.page_starts:
            return 0
        return self.page_starts[min(self.page_index, len(self.page_starts) - 1)]

    def _ensure_source_lines(self) -> None:
        """Build source-line offsets lazily when page mode actually needs them."""
        if self._source_lines or not self.full_text:
            return
        source_start = 0
        while source_start < len(self.full_text):
            newline = self.full_text.find("\n", source_start)
            if newline < 0:
                self._source_lines.append((self.full_text[source_start:], source_start))
                break
            self._source_lines.append((self.full_text[source_start:newline], source_start))
            source_start = newline + 1

    def repaginate(self, offset: int | None = None, saved_page: int | None = None) -> None:
        if self.layout_columns and self.layout_lines:
            self._ensure_source_lines()
            self._paginate_by_lines()
        else:
            self._paginate_by_characters()
        if offset is not None:
            self.page_index = self.page_for_offset(offset)
        else:
            requested = self.page_index if saved_page is None else int(saved_page)
            self.page_index = min(max(0, requested), max(0, len(self.pages) - 1))

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    def load(self, path: str, saved_page: int = 0, saved_offset: int | None = None, paginate: bool = True) -> None:
        raw = Path(path).read_bytes()
        text = decode_text(raw).replace("\r\n", "\n").replace("\r", "\n")
        # Keep every non-newline character from the source.  Only terminal
        # line separators are removed to avoid manufacturing a final blank
        # page for the conventional newline at end of file.
        self.full_text = text.rstrip("\n")
        self._source_lines = []
        self.path = str(Path(path).resolve())
        self.title = Path(path).stem
        if paginate:
            self.repaginate(offset=saved_offset, saved_page=saved_page)
        else:
            self.pages = []
            self.page_starts = []
            self.page_ends = []
            self.chapter_for_page = {}
            self.page_index = max(0, int(saved_page))

    def page_text(self) -> str:
        return self.pages[self.page_index] if self.pages else ""

    def chapter(self) -> str:
        if not self.pages:
            return ""
        heading = ""
        for index, value in sorted(self.chapter_for_page.items()):
            if index > self.page_index:
                break
            heading = value
        return heading

    def move(self, delta: int) -> None:
        if self.pages:
            self.page_index = min(max(0, self.page_index + delta), len(self.pages) - 1)


class Store:
    def __init__(self) -> None:
        base = Path(os.environ.get("APPDATA", Path.home())) / "FishBar"
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.settings_path = base / "settings.json"
        self.progress_path = base / "progress.json"
        self.settings: dict[str, Any] = {
            "opacity": 0.78,
            "display_mode": "transparent",
            "reading_mode": "page",
            "auto_scroll_enabled": False,
            "auto_scroll_speed": 20,
            "always_on_top": True,
            "taskbar_pinned": False,
            "hide_delay": 0,
            "page_chars": 520,
            "page_lines": 0,
            "font_size": 18,
            "font_family": "Noto Serif SC",
            "font_path": "",
            "font_weight": 400,
            "text_color": "#000000",
            "panel_width": 780,
            "panel_height": 214,
            "panel_x": None,
            "panel_y": None,
            "last_path": "",
            "library": [],
        }
        self.progress: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        for path, target in ((self.settings_path, self.settings), (self.progress_path, self.progress)):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    target.update(value)
            except (OSError, ValueError, TypeError):
                pass
        library = self.settings.get("library", [])
        if not isinstance(library, list):
            library = []
        self.settings["library"] = [str(path) for path in library if isinstance(path, str) and path]
        enabled = self.settings.get("auto_scroll_enabled", False)
        self.settings["auto_scroll_enabled"] = enabled if isinstance(enabled, bool) else False
        try:
            speed = int(self.settings.get("auto_scroll_speed", 20))
        except (TypeError, ValueError):
            speed = 20
        self.settings["auto_scroll_speed"] = max(1, min(120, speed))

    def save(self) -> None:
        for path, value in ((self.settings_path, self.settings), (self.progress_path, self.progress)):
            temporary = path.with_suffix(path.suffix + ".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# App


class FishBarApp:
    SENSOR_HEIGHT = 6
    HOVER_POLL_MS = 30
    FREE_SCROLL_PIXELS = 16
    AUTO_SCROLL_INTERVAL_MS = 16
    AUTO_SCROLL_IDLE_MS = 250
    TRANSPARENT_COLOR = "#010101"
    TRAY_CALLBACK_MESSAGE = 0x8000 + 37  # WM_APP + private offset
    TRAY_ICON_ID = 1
    HOTKEY_IDS = {1: "open", 2: "toggle", 3: "next", 4: "previous", 5: "settings", 6: "reset_position"}
    # MOD_CONTROL | MOD_ALT
    HOTKEY_MODIFIERS = 0x0002 | 0x0001
    NOTO_SERIF_SC_WEIGHTS = {
        200: "Noto Serif SC ExtraLight",
        300: "Noto Serif SC Light",
        400: "Noto Serif SC",
        500: "Noto Serif SC Medium",
        600: "Noto Serif SC SemiBold",
        900: "Noto Serif SC Black",
    }

    def __init__(self) -> None:
        enable_dpi_awareness()
        self.store = Store()
        self.reader = Reader(int(self.store.settings["page_chars"]))
        self.display_mode = str(self.store.settings.get("display_mode", "transparent"))
        if self.display_mode not in ("transparent", "tinted"):
            self.display_mode = "transparent"
        self.reading_mode = str(self.store.settings.get("reading_mode", "page"))
        if self.reading_mode not in ("page", "scroll"):
            self.reading_mode = "page"
        self.always_on_top = bool(self.store.settings.get("always_on_top", True))
        self.scroll_position = 0.0
        self.scroll_offset = 0
        self.scroll_restore_by_offset = False
        self.reader_layout_dirty = False
        self.auto_scroll_last_tick = time.monotonic()
        self.auto_scroll_remainder = 0.0
        self.root = tk.Tk()
        self.font_family = str(self.store.settings.get("font_family", "Microsoft YaHei UI")) or "Microsoft YaHei UI"
        self.loaded_font_path = ""
        self._restore_custom_font()
        self.root.title("FishBar")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#010101")
        try:
            # The root only owns timers and the Windows hotkey message hook.
            # Hover detection uses GetCursorPos, so no transparent window has
            # to receive mouse events.
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.sensor_width = self.root.winfo_screenwidth()
        self.panel = tk.Toplevel(self.root)
        self.panel.withdraw()
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", self.always_on_top)
        self.panel.configure(bg="#141b23")
        try:
            self.panel.attributes("-alpha", float(self.store.settings["opacity"]))
        except tk.TclError:
            pass
        # A color-key transparent Tk window does not receive mouse input on
        # its transparent pixels.  This almost-invisible window sits directly
        # underneath the visual panel and receives drag/wheel events that
        # would otherwise fall through to the Windows taskbar.
        self.hitbox = tk.Toplevel(self.root)
        self.hitbox.withdraw()
        self.hitbox.overrideredirect(True)
        self.hitbox.attributes("-topmost", True)
        self.hitbox.configure(bg="#020202")
        try:
            self.hitbox.attributes("-alpha", 1 / 255)
        except tk.TclError:
            pass
        self.panel_visible = False
        self.manual_hidden = False
        self.hide_after_id: str | None = None
        self.hover_inside = False
        self.startup_grace_until = 0.0
        self.settings_window: tk.Toplevel | None = None
        self.hwnd = 0
        self.hotkey_queue: list[int] = []
        self.tray_event_queue: list[int] = []
        self.registered_hotkeys: set[int] = set()
        self._wndproc_callback: Any = None
        self._old_wndproc: int | None = None
        self.tray_data: Any = None
        self.tray_icon_added = False
        self.closing = False
        self.panel_screen_rect = (0, 0, 0, 0)
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.settings_save_after: str | None = None
        self._pagination_font: tkfont.Font | None = None
        self._overlay_image: Any = None
        self._overlay_source: Any = None
        self._overlay_background: Any = None
        self._overlay_capture_after: str | None = None
        self._pil_font_cache: dict[tuple[str, int, int], Any] = {}
        self._text_content_signature: tuple[str | None, str, int, int] | None = None
        self._hidden_scroll_anchor: tuple[str, int] | None = None
        self._build_panel()
        self._sync_reader_layout()
        self._build_hitbox()
        self._place_sensor()
        self.root.update_idletasks()
        if IS_WINDOWS:
            self.hwnd = int(self.root.winfo_id())
            apply_toolwindow(self.hwnd)
            apply_toolwindow(top_level_window(int(self.hitbox.winfo_id())))
            apply_toolwindow(top_level_window(int(self.panel.winfo_id())))
            self._set_hitbox_opacity()
            self.set_panel_opacity(float(self.store.settings["opacity"]))
            self.set_panel_topmost(self.always_on_top)
            self._install_window_proc()
            self._register_hotkeys()
            self._add_tray_icon()
        self._try_restore_last_book()
        self.root.after(self.HOVER_POLL_MS, self._poll_hover)
        self.root.after(50, self._poll_hotkeys)
        self.root.after(self.AUTO_SCROLL_INTERVAL_MS, self._auto_scroll_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.hitbox.protocol("WM_DELETE_WINDOW", self.close)
        self.panel.protocol("WM_DELETE_WINDOW", self.close)

    def _restore_custom_font(self) -> None:
        path = str(self.store.settings.get("font_path", ""))
        if (not path or not Path(path).is_file()) and self.font_family == "Noto Serif SC":
            path = self._bundled_font_path()
        if not path or not Path(path).is_file() or not IS_WINDOWS:
            return
        if add_private_font(path):
            self.loaded_font_path = path
            self.font_family = font_family_from_file(path) or self.font_family

    def _import_font_file(self, path: str) -> str:
        resolved = str(Path(path).resolve())
        family = font_family_from_file(resolved)
        if not family:
            raise ValueError("无法读取该字体的字体名称")
        if resolved == self.loaded_font_path:
            self.font_family = family
            self.store.settings["font_path"] = resolved
            self.store.settings["font_family"] = family
            return family
        if not add_private_font(resolved):
            raise ValueError("Windows 无法载入该字体文件")
        previous = self.loaded_font_path
        self.loaded_font_path = resolved
        self.font_family = family
        self.store.settings["font_path"] = resolved
        self.store.settings["font_family"] = family
        if previous and previous != resolved:
            remove_private_font(previous)
        return family

    def _font_weight(self) -> int:
        try:
            requested = int(self.store.settings.get("font_weight", 400))
        except (TypeError, ValueError):
            requested = 400
        return max(200, min(900, requested))

    def _text_font(self, size: int | None = None) -> tuple[Any, ...]:
        value = int(size if size is not None else self.store.settings["font_size"])
        weight = self._font_weight()
        if self.font_family == "Noto Serif SC":
            # Tk/GDI exposes only named instances, so use the nearest one for
            # line metrics.  Transparent rendering uses the exact variable
            # font axis value and is not snapped to these instances.
            nearest = min(self.NOTO_SERIF_SC_WEIGHTS, key=lambda item: abs(item - weight))
            return self.NOTO_SERIF_SC_WEIGHTS[nearest], value
        return self.font_family, value, "bold" if weight >= 600 else "normal"

    def _text_color(self) -> str:
        value = str(self.store.settings.get("text_color", "#000000"))
        return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else "#000000"

    def _minimum_panel_size(self, font_size: int | None = None) -> tuple[int, int]:
        """Return the smallest panel that can paint one complete glyph line."""
        value = int(font_size if font_size is not None else self.store.settings.get("font_size", 18))
        try:
            text_font = tkfont.Font(root=self.root, font=self._text_font(value))
            glyph_width = max(int(text_font.measure("中")), int(text_font.measure("W")), 1)
            line_height = (
                int(text_font.metrics("linespace"))
                + int(self.text.cget("spacing1"))
                + int(self.text.cget("spacing3"))
            )
            padx = int(self.text.cget("padx"))
            pady = int(self.text.cget("pady"))
            del text_font
            return glyph_width + padx * 2 + 2, line_height + pady * 2
        except (tk.TclError, TypeError, ValueError):
            return max(20, value * 2), max(20, value * 2)

    def _text_layout(self, width: int, height: int, font_size: int) -> tuple[int, int, int]:
        """Return visible columns, display lines and a safe page capacity."""
        try:
            text_font = tkfont.Font(root=self.root, font=self._text_font(font_size))
            char_width = max(1, int(text_font.measure("中")))
            line_height = max(
                1,
                int(text_font.metrics("linespace"))
                + int(self.text.cget("spacing1"))
                + int(self.text.cget("spacing3")),
            )
            del text_font
            padx = int(self.text.cget("padx"))
            pady = int(self.text.cget("pady"))
        except (tk.TclError, TypeError, ValueError):
            char_width, line_height, padx, pady = max(1, font_size), max(1, font_size + 3), 2, 1
        # Two pixels of horizontal reserve protect the final glyph from
        # rounding differences between Tk font measurement and text painting.
        usable_width = max(1, max(1, width) - padx * 2 - 2)
        columns = max(1, usable_width // char_width)
        lines = max(1, (max(1, height) - pady * 2) // line_height)
        capacity = max(40, min(1200, (columns * lines // 20) * 20))
        return columns, lines, capacity

    def _sync_reader_layout(self) -> None:
        width, height = self._effective_panel_size()
        font_size = max(12, min(30, int(self.store.settings.get("font_size", 18))))
        columns, lines, capacity = self._text_layout(width, height, font_size)
        padx = int(self.text.cget("padx"))
        usable_width = max(1, width - padx * 2 - 2)
        self._pagination_font = tkfont.Font(root=self.root, font=self._text_font(font_size))
        self.reader.set_layout(columns, lines, usable_width, self._pagination_font.measure)
        self.reader.page_chars = capacity
        self.store.settings["page_chars"] = capacity
        self.store.settings["page_lines"] = lines

    def _build_panel(self) -> None:
        self.panel_frame = tk.Frame(self.panel, bg="#141b23", highlightthickness=0)
        self.panel_frame.pack(fill="both", expand=True)
        self.panel_frame.pack_propagate(False)
        body = tk.Frame(self.panel_frame, bg="#141b23")
        body.pack(fill="both", expand=True)
        self.text_body = body
        # Small requested dimensions let the containing panel geometry win;
        # Tk's default Text size (80x24 characters) otherwise makes the
        # supposedly compact overlay grow to nearly the whole screen.
        self.text = tk.Text(body, width=1, height=1, wrap="none" if self.reading_mode == "page" else "word", state="disabled", relief="flat", bd=0, padx=2, pady=1, bg="#141b23", fg=self._text_color(), insertbackground=self._text_color(), font=self._text_font(), spacing1=1, spacing2=0, spacing3=1, highlightthickness=0)
        self.text.pack(side="left", fill="both", expand=True)
        self.text_canvas = tk.Canvas(body, bg=self.TRANSPARENT_COLOR, bd=0, highlightthickness=0, takefocus=False)
        self.text.bind("<Button-1>", lambda _event: self._cancel_hide())

        for widget in (self.panel, self.panel_frame, body, self.text):
            widget.bind("<Enter>", lambda _event: self._cancel_hide())
            widget.bind("<Leave>", lambda _event: self._schedule_hide())
        # Dragging any part of the panel repositions it; a click without
        # movement still reaches the underlying button command as usual.
        self._bind_drag_tree(self.panel)
        self.root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self._apply_display_mode(self.display_mode)

    def _build_hitbox(self) -> None:
        self.hitbox.bind("<ButtonPress-1>", self._start_drag)
        self.hitbox.bind("<B1-Motion>", self._drag_motion)
        self.hitbox.bind("<ButtonRelease-1>", self._end_drag)
        self.hitbox.bind("<Enter>", lambda _event: self._cancel_hide())
        self.hitbox.bind("<Leave>", lambda _event: self._schedule_hide())

    def _set_hitbox_opacity(self) -> None:
        """Make the input surface imperceptible but still mouse-active."""
        try:
            self.hitbox.attributes("-alpha", 1 / 255)
        except tk.TclError:
            pass
        if IS_WINDOWS and self.hitbox.winfo_exists():
            try:
                hwnd = top_level_window(int(self.hitbox.winfo_id()))
                user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
                user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
                user32.SetLayeredWindowAttributes(hwnd, 0, 1, 0x00000002)  # LWA_ALPHA
            except (AttributeError, ctypes.ArgumentError, tk.TclError):
                pass

    def _bind_drag_tree(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._start_drag, add="+")
        widget.bind("<B1-Motion>", self._drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", self._end_drag, add="+")
        for child in widget.winfo_children():
            self._bind_drag_tree(child)

    def _start_drag(self, event: tk.Event) -> None:
        if not self.panel_visible:
            return
        self._cancel_hide()
        self.drag_origin = (int(event.x_root), int(event.y_root), self.panel.winfo_x(), self.panel.winfo_y())

    def _drag_motion(self, event: tk.Event) -> None:
        if not self.drag_origin or not self.panel_visible:
            return
        start_x, start_y, panel_x, panel_y = self.drag_origin
        new_x = panel_x + int(event.x_root) - start_x
        new_y = panel_y + int(event.y_root) - start_y
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width, height = self.panel.winfo_width(), self.panel.winfo_height()
        new_x = max(0, min(screen_w - width, new_x))
        new_y = max(0, min(screen_h - height, new_y))
        self._apply_panel_geometry(width, height, new_x, new_y)
        self._keep_panel_above_taskbar()

    def _end_drag(self, _event: tk.Event) -> None:
        if self.drag_origin:
            self.store.settings["panel_x"] = self.panel_screen_rect[0]
            self.store.settings["panel_y"] = self.panel_screen_rect[1]
            self.store.save()
            self._keep_panel_above_taskbar()
        self.drag_origin = None

    def reset_panel_position(self) -> None:
        self.store.settings["panel_x"] = None
        self.store.settings["panel_y"] = None
        self.store.save()
        self.show_panel()

    @staticmethod
    def _bundled_font_path() -> str:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        bundled = base / "NotoSerifSC-VF.ttf"
        return str(bundled) if bundled.is_file() else ""

    def _font_render_path(self) -> str:
        custom = str(self.store.settings.get("font_path", ""))
        if custom and Path(custom).is_file():
            return custom
        if self.font_family == "Noto Serif SC":
            bundled = self._bundled_font_path()
            if bundled:
                return bundled
            noto = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "NotoSerifSC-VF.ttf"
            if noto.is_file():
                return str(noto)
        return ""

    def _overlay_font(self) -> Any:
        if ImageFont is None:
            return None
        path = self._font_render_path()
        if not path:
            return None
        points = int(self.store.settings.get("font_size", 18))
        pixels = max(1, round(points * float(self.root.winfo_fpixels("1p"))))
        weight = self._font_weight()
        key = (path, pixels, weight)
        if key in self._pil_font_cache:
            return self._pil_font_cache[key]
        try:
            value = ImageFont.truetype(path, pixels)
            try:
                axes = value.get_variation_axes()
                if len(axes) == 1:
                    value.set_variation_by_axes([weight])
            except (AttributeError, OSError, ValueError):
                pass
            # Slider drags can visit hundreds of weight values.  Keep a small
            # rolling cache so variable font objects do not accumulate.
            if len(self._pil_font_cache) >= 12:
                self._pil_font_cache.pop(next(iter(self._pil_font_cache)))
            self._pil_font_cache[key] = value
            return value
        except (OSError, ValueError):
            return None

    def _capture_overlay_background(self) -> None:
        """Capture what is behind the hidden panel for halo-free AA edges."""
        self._overlay_background = None
        if ImageGrab is None or self.display_mode != "transparent":
            return
        left, top, right, bottom = self.panel_screen_rect
        if right <= left or bottom <= top:
            return
        try:
            captured = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
            self._overlay_background = captured
        except (OSError, ValueError):
            self._overlay_background = None

    def _schedule_overlay_background_capture(self) -> None:
        if self.display_mode != "transparent":
            return
        if self._overlay_capture_after:
            try:
                self.root.after_cancel(self._overlay_capture_after)
            except tk.TclError:
                pass
        self._overlay_capture_after = self.root.after(220, self._recapture_visible_background)

    def _recapture_visible_background(self) -> None:
        self._overlay_capture_after = None
        if not self.panel_visible or self.display_mode != "transparent":
            return
        try:
            if self.reading_mode == "scroll":
                self.text.update_idletasks()
                self._capture_hidden_scroll_anchor()
            self.panel.withdraw()
            self.root.update_idletasks()
            self._capture_overlay_background()
            self.panel.deiconify()
            self.set_panel_topmost(self.always_on_top)
            if self.reading_mode == "scroll":
                self._restore_hidden_scroll_anchor()
            self._render_text_overlay()
        except tk.TclError:
            pass

    def _render_text_overlay(self) -> None:
        """Paint transparent-mode glyphs without ClearType fringe pixels."""
        if self.display_mode != "transparent" or Image is None or ImageDraw is None or ImageTk is None:
            self.text_canvas.place_forget()
            self._overlay_image = None
            self._overlay_source = None
            return
        font = self._overlay_font()
        if font is None:
            # Native Tk rendering remains available for imported fonts whose
            # backing file cannot be located.
            self.text_canvas.place_forget()
            self._overlay_image = None
            self._overlay_source = None
            return
        self.text.update_idletasks()
        width = max(1, self.text.winfo_width())
        height = max(1, self.text.winfo_height())
        mask = Image.new("L", (width, height), 0)
        painter = ImageDraw.Draw(mask)
        try:
            start = self.text.index(f"{self.text.index('@0,0')} display linestart")
            end_of_text = self.text.index("end-1c")
            visited: set[str] = set()
            while start not in visited and self.text.compare(start, "<=", end_of_text):
                visited.add(start)
                info = self.text.dlineinfo(start)
                if info is None:
                    break
                x, y, _line_width, line_height, baseline = (int(value) for value in info)
                if y >= height:
                    break
                # ``display lineend`` and ``+ 1 display lines`` disagree by
                # one character on Tk/Windows for soft-wrapped lines.  Using
                # the next display-line start as the exclusive boundary makes
                # adjacent ranges exact and non-overlapping.
                next_start = self.text.index(f"{start} + 1 display lines")
                content = self.text.get(start, next_start)
                if content.endswith("\n"):
                    content = content[:-1]
                if content:
                    painter.text((x, y + baseline), content, font=font, fill=255, anchor="ls", stroke_width=0)
                if next_start == start:
                    break
                start = next_start
                if y + line_height >= height and self.text.dlineinfo(start) is None:
                    break
        except (tk.TclError, TypeError, ValueError):
            self.text_canvas.place_forget()
            self._overlay_image = None
            self._overlay_source = None
            return

        image = Image.new("RGB", (width, height), self.TRANSPARENT_COLOR)
        background = self._overlay_background
        if background is not None:
            if background.size != (width, height):
                background = background.resize((width, height))
            foreground = Image.new("RGB", (width, height), self._text_color())
            # Pre-composite only antialiased glyph pixels against the real
            # screen content captured behind the panel.  Zero-coverage pixels
            # stay equal to TRANSPARENT_COLOR, so the rest of the panel remains
            # genuinely transparent instead of becoming a frozen screenshot.
            blended = Image.composite(foreground, background, mask)
            nonzero_mask = mask.point(lambda value: 255 if value else 0, mode="1")
            image.paste(blended, (0, 0), nonzero_mask)
        else:
            # If screen capture is unavailable, coverage dithering keeps edge
            # detail while still emitting only key/text colors (no halo).
            dithered = mask.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            image.paste(self._text_color(), (0, 0, width, height), dithered)
        self._overlay_source = image
        self._overlay_image = ImageTk.PhotoImage(image, master=self.text_canvas)
        self.text_canvas.configure(bg=self.TRANSPARENT_COLOR, width=width, height=height)
        self.text_canvas.delete("all")
        self.text_canvas.create_image(0, 0, anchor="nw", image=self._overlay_image)
        self.text_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.text_canvas.tk.call("raise", self.text_canvas._w)

    def _apply_display_mode(self, mode: str) -> None:
        """Switch between true color-key transparency and a tinted panel."""
        self.display_mode = "transparent" if mode == "transparent" else "tinted"
        color = self.TRANSPARENT_COLOR if self.display_mode == "transparent" else "#141b23"
        foreground = self._text_color()
        try:
            self.panel.configure(bg=color)
            self.panel_frame.configure(bg=color, highlightthickness=0 if self.display_mode == "transparent" else 1)
        except tk.TclError:
            return

        def recolor(widget: tk.Misc) -> None:
            try:
                widget.configure(bg=color)
                widget.configure(fg=foreground)
                if widget.winfo_class() == "Text":
                    widget.configure(insertbackground=foreground)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                recolor(child)

        recolor(self.panel_frame)
        self._render_text_overlay()

    def set_panel_opacity(self, value: float) -> None:
        """Apply opacity through both Tk and SetLayeredWindowAttributes.

        Tk's ``-alpha`` can be lost when WS_EX_LAYERED is added later for the
        no-activate tool window. Calling the Win32 API explicitly keeps the
        result reliable on Windows 10/11.
        """
        alpha = min(1.0, max(0.35, float(value)))
        try:
            self.panel.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        if IS_WINDOWS and self.panel.winfo_exists():
            try:
                hwnd = top_level_window(int(self.panel.winfo_id()))
                user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
                user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
                if self.display_mode == "transparent":
                    # Every pixel painted with TRANSPARENT_COLOR is removed;
                    # glyphs and other non-key pixels remain fully opaque.
                    user32.SetLayeredWindowAttributes(hwnd, 0x010101, 0, 0x00000001)  # LWA_COLORKEY
                else:
                    user32.SetLayeredWindowAttributes(hwnd, 0, int(round(alpha * 255)), 0x00000002)  # LWA_ALPHA
            except (AttributeError, ctypes.ArgumentError, tk.TclError):
                pass

    def set_panel_topmost(self, enabled: bool) -> None:
        self.always_on_top = bool(enabled)
        try:
            self.panel.attributes("-topmost", self.always_on_top)
            self.hitbox.attributes("-topmost", self.always_on_top)
        except tk.TclError:
            pass
        if IS_WINDOWS and self.panel.winfo_exists() and self.hitbox.winfo_exists():
            set_window_topmost(top_level_window(int(self.hitbox.winfo_id())), self.always_on_top)
            set_window_topmost(top_level_window(int(self.panel.winfo_id())), self.always_on_top)

    def _place_sensor(self) -> None:
        # Keep a real HWND for global hotkeys, but move the invisible owner
        # window off-screen. The taskbar hot zone is calculated virtually.
        self.root.geometry("1x1+-100+-100")

    def _hot_zone(self) -> tuple[int, int, int, int]:
        # When hidden, only the panel's own last position is sensitive.  The
        # whole taskbar must not become a trigger merely because the panel was
        # parked on it.
        return self.panel_screen_rect

    def _effective_panel_size(self, width: int | None = None, height: int | None = None) -> tuple[int, int]:
        """Return the actual on-screen size after applying screen limits."""
        requested_width = int(self.store.settings.get("panel_width", 780) if width is None else width)
        requested_height = int(self.store.settings.get("panel_height", 214) if height is None else height)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        minimum_width, minimum_height = self._minimum_panel_size()
        return (
            min(max(minimum_width, requested_width), max(1, screen_w - 2)),
            min(max(minimum_height, requested_height), max(1, screen_h - 2)),
        )

    def _place_panel(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        base = taskbar_top(screen_w, screen_h)
        width, height = self._effective_panel_size()
        saved_x = self.store.settings.get("panel_x")
        saved_y = self.store.settings.get("panel_y")
        if isinstance(saved_x, (int, float)) and isinstance(saved_y, (int, float)):
            x = max(0, min(screen_w - width, int(saved_x)))
            y = max(0, min(screen_h - height, int(saved_y)))
        else:
            x = max(12, (screen_w - width) // 2)
            y = max(8, base - height)
        self._apply_panel_geometry(width, height, x, y)

    def _apply_panel_geometry(self, width: int, height: int, x: int, y: int) -> None:
        previous_rect = self.panel_screen_rect
        geometry = f"{width}x{height}+{x}+{y}"
        self.hitbox.geometry(geometry)
        self.panel.geometry(geometry)
        if IS_WINDOWS and self.hitbox.winfo_exists() and self.panel.winfo_exists():
            move_window(top_level_window(int(self.hitbox.winfo_id())), x, y, width, height)
            move_window(top_level_window(int(self.panel.winfo_id())), x, y, width, height)
        self.panel_screen_rect = (x, y, x + width, y + height)
        if self.panel_visible and previous_rect != self.panel_screen_rect:
            self._overlay_background = None
            self._schedule_overlay_background_capture()

    def _panel_overlaps_taskbar(self) -> bool:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        taskbar = taskbar_rectangle(screen_w, screen_h)
        left, top, right, bottom = self.panel_screen_rect
        return left < taskbar[2] and right > taskbar[0] and top < taskbar[3] and bottom > taskbar[1]

    def _keep_panel_above_taskbar(self) -> None:
        """Keep a parked panel usable even after the taskbar is clicked."""
        if not self.panel_visible or not (self.always_on_top or self._panel_overlaps_taskbar()):
            return
        try:
            self.hitbox.attributes("-topmost", True)
            self.panel.attributes("-topmost", True)
            self.hitbox.lift()
            self.panel.lift()
        except tk.TclError:
            pass
        if IS_WINDOWS and self.panel.winfo_exists() and self.hitbox.winfo_exists():
            set_window_topmost(top_level_window(int(self.hitbox.winfo_id())), True)
            set_window_topmost(top_level_window(int(self.panel.winfo_id())), True)

    def _inside(self, point: tuple[int, int], rect: tuple[int, int, int, int]) -> bool:
        return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]

    def _poll_hover(self) -> None:
        try:
            if self.drag_origin:
                return
            point = cursor_position()
            # Both the hidden trigger and visible hover test use the panel's
            # recorded position; the rest of the taskbar is unrelated.
            inside = self._inside(point, self.panel_screen_rect) if self.panel_visible else self._inside(point, self._hot_zone())
            if inside:
                self.hover_inside = True
                self._cancel_hide()
                if self.panel_visible:
                    self._keep_panel_above_taskbar()
                if not self.panel_visible and not self.manual_hidden:
                    self.show_panel()
            elif self.panel_visible:
                self.hover_inside = False
                if time.monotonic() >= self.startup_grace_until:
                    self._schedule_hide()
            else:
                # A manual hide only lasts until the pointer leaves the
                # bottom sensor, which makes the boss key reliable without
                # disabling hover behavior forever.
                self.manual_hidden = False
        finally:
            self.root.after(self.HOVER_POLL_MS, self._poll_hover)

    def _schedule_hide(self) -> None:
        if self.drag_origin:
            return
        if not self.panel_visible or time.monotonic() < self.startup_grace_until:
            return
        # <Leave> may fire while moving between child widgets.  Verify the
        # global cursor position against the whole panel so a genuine leave
        # hides immediately without a delay timer or a false child-boundary
        # hide.
        if self._inside(cursor_position(), self.panel_screen_rect):
            self.hover_inside = True
            return
        self.hover_inside = False
        self.hide_panel()

    def _cancel_hide(self) -> None:
        if self.hide_after_id:
            try:
                self.root.after_cancel(self.hide_after_id)
            except tk.TclError:
                pass
            self.hide_after_id = None

    def show_panel(self) -> None:
        self._cancel_hide()
        self.manual_hidden = False
        was_visible = self.panel_visible
        self._place_panel()
        if not was_visible:
            self.root.update_idletasks()
            self._capture_overlay_background()
        self.hitbox.deiconify()
        self._set_hitbox_opacity()
        self.panel.deiconify()
        self.set_panel_topmost(self.always_on_top)
        self.panel_visible = True
        self._keep_panel_above_taskbar()
        signature = self._current_text_signature()
        content_is_current = self._text_content_signature == signature
        # Restore a source display line plus its within-line pixel offset. Tk
        # can change that partial-line offset while a Toplevel is withdrawn,
        # even though the underlying text index has not changed.
        use_hidden_anchor = (
            content_is_current
            and not self.scroll_restore_by_offset
            and self._hidden_scroll_anchor is not None
        )
        self._refresh_text(reinsert=not content_is_current, restore_position=not use_hidden_anchor)

    def show_startup_hint(self) -> None:
        # The panel may be shown at launch for the demo, but it must obey the
        # same hover rule immediately: once the pointer leaves, hide_after_id
        # is scheduled using the configured delay.
        self.startup_grace_until = 0.0
        self.show_panel()

    def hide_panel(self, manual: bool = False) -> None:
        self._cancel_hide()
        if self.panel_visible and self.reading_mode == "scroll":
            self.text.update_idletasks()
            self._capture_hidden_scroll_anchor()
            self._update_scroll_location()
            self._save_scroll_position()
        self.panel.withdraw()
        self.hitbox.withdraw()
        self.panel_visible = False
        self.manual_hidden = manual

    def _current_text_signature(self) -> tuple[str | None, str, int, int]:
        page = self.reader.page_index if self.reading_mode == "page" else -1
        return self.reader.path, self.reading_mode, page, len(self.reader.full_text)

    def _capture_hidden_scroll_anchor(self) -> None:
        """Remember the exact top display line and partial pixel displacement."""
        try:
            start = self.text.index(f"{self.text.index('@0,0')} display linestart")
            info = self.text.dlineinfo(start)
            target_y = int(info[1]) if info is not None else 0
            self._hidden_scroll_anchor = start, target_y
        except (tk.TclError, TypeError, ValueError):
            self._hidden_scroll_anchor = None

    def _restore_hidden_scroll_anchor(self) -> None:
        anchor = self._hidden_scroll_anchor
        self._hidden_scroll_anchor = None
        if anchor is None:
            return
        try:
            index, target_y = anchor
            self.text.yview(index)
            self.text.update_idletasks()
            # The Text widget's top content inset is theme/DPI dependent, so
            # measure it rather than assuming yview(index) places the line at
            # pixel zero. One correction normally suffices; a short bounded
            # loop also covers rounding at fractional DPI scales.
            for _attempt in range(3):
                info = self.text.dlineinfo(index)
                if info is None:
                    break
                correction = int(info[1]) - target_y
                if not correction:
                    break
                self.text.tk.call(self.text._w, "yview", "scroll", correction, "pixels")
                self.text.update_idletasks()
            self._update_scroll_location()
        except (tk.TclError, TypeError, ValueError):
            pass

    def _refresh_text(self, reinsert: bool = True, restore_position: bool = True) -> None:
        if not self.panel_visible:
            if reinsert:
                self._text_content_signature = None
            return
        self.text.configure(state="normal", wrap="word" if self.reading_mode == "scroll" else "none")
        if reinsert:
            self.text.delete("1.0", "end")
            content = self.reader.full_text if self.reading_mode == "scroll" else self.reader.page_text()
            self.text.insert("1.0", content)
            self._text_content_signature = self._current_text_signature()
            self._hidden_scroll_anchor = None
        self.text.configure(state="disabled")
        self.text.update_idletasks()
        if self.reading_mode == "scroll":
            if restore_position:
                try:
                    if self.scroll_restore_by_offset:
                        target = max(0, min(len(self.reader.full_text), int(self.scroll_offset)))
                        self.text.yview(self.text.index(f"1.0 + {target} chars"))
                        self.scroll_restore_by_offset = False
                    else:
                        self.text.yview_moveto(max(0.0, min(1.0, self.scroll_position)))
                    self.text.update_idletasks()
                    self._update_scroll_location()
                except (tk.TclError, TypeError, ValueError):
                    pass
            else:
                self._restore_hidden_scroll_anchor()
        elif self.reading_mode == "page" and restore_position:
            self.text.yview_moveto(0)
        self._render_text_overlay()
        self.root.after_idle(self._render_text_overlay)

    def _update_scroll_location(self) -> None:
        """Record both the exact source offset and legacy fractional position."""
        try:
            top_index = self.text.index("@0,0")
            count = self.text.count("1.0", top_index, "chars")
            self.scroll_offset = int(count[0]) if count else 0
            self.scroll_position = float(self.text.yview()[0])
        except (tk.TclError, TypeError, ValueError):
            pass

    def _on_wheel(self, event: tk.Event) -> str | None:
        # Preserve high-resolution wheel deltas instead of reducing every
        # gesture to one line/page command.
        if not self.panel_visible or not self._inside(cursor_position(), self.panel_screen_rect):
            return None
        delta = int(getattr(event, "delta", 0))
        self.handle_wheel(delta if delta else -120)
        return "break"

    def _scroll_pixels(self, pixels: int) -> None:
        """Move the continuous reader by physical pixels, including partial lines."""
        if not pixels:
            return
        try:
            self.text.tk.call(self.text._w, "yview", "scroll", int(pixels), "pixels")
            self.text.update_idletasks()
            self._update_scroll_location()
            self._render_text_overlay()
            self._save_scroll_position()
        except tk.TclError:
            pass

    def _auto_scroll_tick(self) -> None:
        """Advance continuous reading at a stable pixels-per-second rate."""
        now = time.monotonic()
        elapsed = max(0.0, min(0.25, now - self.auto_scroll_last_tick))
        self.auto_scroll_last_tick = now
        enabled = bool(self.store.settings.get("auto_scroll_enabled", False))
        active = enabled and self.reading_mode == "scroll" and self.panel_visible and bool(self.reader.full_text)
        next_delay = self.AUTO_SCROLL_IDLE_MS
        if active:
            try:
                speed = max(1, min(120, int(self.store.settings.get("auto_scroll_speed", 20))))
                can_advance = self.text.yview()[1] < 1.0
            except (tk.TclError, TypeError, ValueError):
                speed = 20
                can_advance = False
            if can_advance:
                next_delay = self.AUTO_SCROLL_INTERVAL_MS
                self.auto_scroll_remainder += speed * elapsed
                pixels = int(self.auto_scroll_remainder)
                if pixels:
                    self.auto_scroll_remainder -= pixels
                    self._scroll_pixels(pixels)
            else:
                self.auto_scroll_remainder = 0.0
        else:
            self.auto_scroll_remainder = 0.0
        if not self.closing:
            self.root.after(next_delay, self._auto_scroll_tick)

    def handle_wheel(self, delta: int) -> None:
        if self.reading_mode == "scroll":
            # One standard wheel notch moves less than one text line.  Smaller
            # precision-touchpad deltas scale proportionally and remain smooth.
            magnitude = 1.0 if abs(delta) <= 1 else max(0.125, min(6.0, abs(delta) / 120.0))
            pixels = max(1, round(self.FREE_SCROLL_PIXELS * magnitude))
            self._scroll_pixels(-pixels if delta > 0 else pixels)
        else:
            self.navigate(-1 if delta > 0 else 1)

    def _save_scroll_position(self) -> None:
        if self.reader.path:
            if not self.reader_layout_dirty and self.reader.pages:
                self.reader.page_index = self.reader.page_for_offset(self.scroll_offset)
                self.store.progress[self.reader.path] = self.reader.page_index
            self.store.progress[f"offset::{self.reader.path}"] = self.scroll_offset
            self.store.progress[f"scroll::{self.reader.path}"] = self.scroll_position
            self.store.progress[f"scroll_offset::{self.reader.path}"] = self.scroll_offset
            self._schedule_progress_save()

    def navigate(self, delta: int) -> None:
        if self.reading_mode == "scroll":
            self._scroll_pixels(int(delta) * self.FREE_SCROLL_PIXELS)
            return
        if not self.reader.pages:
            self.show_panel()
            return
        self.reader.move(delta)
        if self.reader.path:
            self.store.progress[self.reader.path] = self.reader.page_index
            self.store.progress[f"offset::{self.reader.path}"] = self.reader.current_offset()
            self.store.progress[f"scroll::{self.reader.path}"] = self.scroll_position
            self.store.progress[f"scroll_offset::{self.reader.path}"] = self.scroll_offset
            self.store.settings["last_path"] = self.reader.path
            self.store.save()
        self.show_panel()

    @staticmethod
    def _path_key(path: str) -> str:
        """Return a case-insensitive key suitable for a Windows path library."""
        return os.path.normcase(os.path.normpath(path)).casefold()

    def _library_paths(self) -> list[str]:
        """Return the de-duplicated library, including legacy/current books."""
        candidates = list(self.store.settings.get("library", []))
        last = self.store.settings.get("last_path", "")
        if isinstance(last, str) and last:
            candidates.append(last)
        if self.reader.path:
            candidates.append(self.reader.path)

        result: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                resolved = str(Path(candidate).expanduser().resolve())
            except (OSError, ValueError):
                continue
            key = self._path_key(resolved)
            if key in seen:
                continue
            seen.add(key)
            result.append(resolved)
        self.store.settings["library"] = result
        return result

    def _remember_book(self, path: str) -> str:
        resolved = str(Path(path).expanduser().resolve())
        paths = self._library_paths()
        key = self._path_key(resolved)
        if all(self._path_key(item) != key for item in paths):
            paths.append(resolved)
        self.store.settings["library"] = paths
        return resolved

    def _save_current_book_state(self) -> None:
        """Persist the active book before changing books or closing FishBar."""
        path = self.reader.path
        if not path:
            return
        if self.reading_mode == "scroll":
            if self.panel_visible:
                self.text.update_idletasks()
                self._update_scroll_location()
            if not self.reader_layout_dirty and self.reader.pages:
                self.reader.page_index = self.reader.page_for_offset(self.scroll_offset)
            offset = self.scroll_offset
        else:
            offset = self.reader.current_offset() if self.reader.pages else 0

        self.store.progress[path] = self.reader.page_index
        self.store.progress[f"offset::{path}"] = offset
        self.store.progress[f"scroll::{path}"] = self.scroll_position
        self.store.progress[f"scroll_offset::{path}"] = self.scroll_offset
        self.store.settings["last_path"] = path
        self._remember_book(path)

    def _load_book(self, path: str, *, save_current: bool = True, show_panel: bool = True) -> str:
        """Load one library book and restore its own page/scroll position."""
        resolved = str(Path(path).expanduser().resolve())
        if not Path(resolved).is_file():
            raise FileNotFoundError(f"文件不存在：{resolved}")
        if save_current:
            self._save_current_book_state()

        try:
            saved_page = int(self.store.progress.get(resolved, 0))
        except (TypeError, ValueError):
            saved_page = 0
        saved_offset_value = self.store.progress.get(f"offset::{resolved}")
        try:
            saved_offset = int(saved_offset_value) if saved_offset_value is not None else None
        except (TypeError, ValueError):
            saved_offset = None

        paginate = self.reading_mode == "page"
        self.reader.load(resolved, saved_page, saved_offset, paginate=paginate)
        self.reader_layout_dirty = not paginate
        try:
            self.scroll_position = float(self.store.progress.get(f"scroll::{resolved}", 0.0))
        except (TypeError, ValueError):
            self.scroll_position = 0.0
        self.scroll_position = max(0.0, min(1.0, self.scroll_position))

        scroll_offset_value = self.store.progress.get(f"scroll_offset::{resolved}")
        try:
            if scroll_offset_value is not None:
                self.scroll_offset = int(scroll_offset_value)
            elif saved_offset is not None:
                self.scroll_offset = saved_offset
            else:
                self.scroll_offset = round(self.scroll_position * len(self.reader.full_text))
        except (TypeError, ValueError):
            self.scroll_offset = 0
        self.scroll_offset = max(0, min(len(self.reader.full_text), self.scroll_offset))
        # The yview fraction retains partial-line pixel position when it is
        # available. Older progress files fall back to an exact text offset.
        self.scroll_restore_by_offset = f"scroll::{resolved}" not in self.store.progress

        self.store.settings["last_path"] = resolved
        self._remember_book(resolved)
        self.store.save()
        if show_panel:
            self.show_panel()
        return resolved

    def open_book(self, parent: tk.Misc | None = None) -> str | None:
        dialog_parent = parent or self.panel
        path = filedialog.askopenfilename(
            title="导入小说",
            filetypes=[("文本小说", "*.txt"), ("所有文件", "*.*")],
            parent=dialog_parent,
        )
        if not path:
            return None
        try:
            return self._load_book(path)
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("导入失败", f"无法读取这本小说：\n{exc}", parent=dialog_parent)
            return None

    def _try_restore_last_book(self) -> None:
        last = str(self.store.settings.get("last_path", ""))
        if last:
            try:
                self._remember_book(last)
            except (OSError, ValueError):
                pass
        if last and Path(last).is_file():
            try:
                self._load_book(last, save_current=False, show_panel=False)
            except (OSError, UnicodeError, ValueError):
                pass
        self.store.save()

    def _register_hotkeys(self) -> None:
        # Ctrl+Alt+O open, H toggle, Right/Left next/previous, S settings, R reset position.
        for hotkey_id, key in ((1, ord("O")), (2, ord("H")), (3, 0x27), (4, 0x25), (5, ord("S")), (6, ord("R"))):
            if register_hotkey(self.hwnd, hotkey_id, self.HOTKEY_MODIFIERS, key):
                self.registered_hotkeys.add(hotkey_id)

    def _install_window_proc(self) -> None:
        """Subclass the Tk owner window so WM_HOTKEY cannot be swallowed."""
        if not IS_WINDOWS or not self.hwnd:
            return
        GWL_WNDPROC = -4
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_void_p
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        set_long.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallWindowProcW.restype = LRESULT
        self._old_wndproc = get_long(self.hwnd, GWL_WNDPROC)

        @WNDPROC
        def window_proc(hwnd: int, message: int, w_param: int, l_param: int) -> int:
            if message == 0x0312:  # WM_HOTKEY
                self.hotkey_queue.append(int(w_param))
                return 0
            if message == self.TRAY_CALLBACK_MESSAGE:
                # Shell_NotifyIcon legacy callbacks store the mouse message in
                # lParam. Keep the WndProc tiny and handle UI from Tk's timer.
                self.tray_event_queue.append(int(l_param) & 0xFFFF)
                return 0
            if self._old_wndproc:
                return int(user32.CallWindowProcW(self._old_wndproc, hwnd, message, w_param, l_param))
            return 0

        self._wndproc_callback = window_proc
        set_long(self.hwnd, GWL_WNDPROC, ctypes.cast(self._wndproc_callback, ctypes.c_void_p))

    def _add_tray_icon(self) -> None:
        if not IS_WINDOWS or not self.hwnd:
            return
        shell32 = ctypes.windll.shell32
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wintypes.HICON
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = self.TRAY_ICON_ID
        data.uFlags = 0x00000001 | 0x00000002 | 0x00000004  # MESSAGE | ICON | TIP
        data.uCallbackMessage = self.TRAY_CALLBACK_MESSAGE
        data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        data.szTip = "FishBar 摸鱼条"
        self.tray_data = data
        self.tray_icon_added = bool(shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(data)))  # NIM_ADD

    def _remove_tray_icon(self) -> None:
        if IS_WINDOWS and self.tray_icon_added and self.tray_data is not None:
            ctypes.windll.shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(self.tray_data))  # NIM_DELETE
            self.tray_icon_added = False

    def _show_tray_menu(self) -> None:
        if not IS_WINDOWS or self.closing:
            return
        MF_STRING = 0x00000000
        MF_CHECKED = 0x00000008
        MF_SEPARATOR = 0x00000800
        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, MF_STRING, 1001, "显示阅读面板")
        user32.AppendMenuW(menu, MF_STRING, 1002, "导入 TXT 小说…")
        user32.AppendMenuW(menu, MF_STRING, 1003, "设置…")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.always_on_top else 0), 1004, "窗口置顶")
        user32.AppendMenuW(menu, MF_STRING, 1005, "重置窗口位置")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 1099, "退出 FishBar")
        x, y = cursor_position()
        owner = user32.GetParent(self.hwnd) or self.hwnd
        user32.SetForegroundWindow(owner)
        command = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, x, y, 0, owner, None)
        user32.DestroyMenu(menu)
        user32.PostMessageW(owner, 0, 0, 0)
        if command == 1001:
            self.show_panel()
        elif command == 1002:
            self.show_panel()
            self.open_book()
        elif command == 1003:
            self.open_settings()
        elif command == 1004:
            self.set_panel_topmost(not self.always_on_top)
            self.store.settings["always_on_top"] = self.always_on_top
            self.store.save()
        elif command == 1005:
            self.reset_panel_position()
        elif command == 1099:
            self.close()

    def _handle_tray_event(self, message: int) -> None:
        if message in (0x0202, 0x0203):  # WM_LBUTTONUP / WM_LBUTTONDBLCLK
            self.show_panel()
        elif message in (0x0205, 0x007B):  # WM_RBUTTONUP / WM_CONTEXTMENU
            self._show_tray_menu()

    def _poll_hotkeys(self) -> None:
        while self.hotkey_queue:
            action = self.HOTKEY_IDS.get(self.hotkey_queue.pop(0))
            if action == "open":
                self.open_book()
            elif action == "toggle":
                self.hide_panel(manual=True) if self.panel_visible else self.show_panel()
            elif action == "next":
                self.navigate(1)
            elif action == "previous":
                self.navigate(-1)
            elif action == "settings":
                self.open_settings()
            elif action == "reset_position":
                self.reset_panel_position()
        while self.tray_event_queue and not self.closing:
            self._handle_tray_event(self.tray_event_queue.pop(0))
        if not self.closing:
            self.root.after(50, self._poll_hotkeys)

    def open_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            return
        win = self.settings_window = tk.Toplevel(self.panel)
        win.title("FishBar 设置")
        win.geometry("720x875")
        win.minsize(680, 830)
        win.resizable(False, False)
        bg = "#17212b"
        surface = "#202d3a"
        border = "#2d4051"
        control_bg = "#2a3b4b"
        text = "#e7eff7"
        secondary = "#cbd8e5"
        muted = "#91a7ba"
        win.configure(bg=bg)
        win.attributes("-topmost", True)

        header = tk.Frame(win, bg=bg)
        header.pack(fill="x", padx=24, pady=(20, 14))
        tk.Label(header, text="摸鱼条设置", bg=bg, fg=text, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        tk.Label(header, text="管理小说书库，并调整阅读面板的外观和行为", bg=bg, fg=muted, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        content = tk.Frame(win, bg=bg)
        content.pack(fill="both", expand=True, padx=24)

        def make_section(title: str, description: str, parent: tk.Frame = content, side: str | None = None) -> tk.Frame:
            card = tk.Frame(parent, bg=surface, highlightthickness=1, highlightbackground=border)
            if side:
                card.pack(side=side, fill="both", expand=True, padx=(0, 6) if side == "left" else (6, 0))
            else:
                card.pack(fill="x", pady=(0, 12))
            inner = tk.Frame(card, bg=surface)
            inner.pack(fill="x", padx=16, pady=(12, 12))
            inner.grid_columnconfigure(1, weight=1)
            tk.Label(inner, text=title, bg=surface, fg=text, font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
            tk.Label(inner, text=description, bg=surface, fg=muted, font=("Microsoft YaHei UI", 8)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
            return inner

        def add_label(parent: tk.Frame, row: int, label: str) -> None:
            tk.Label(parent, text=label, width=11, anchor="w", bg=surface, fg=secondary, font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=5)

        opacity = tk.DoubleVar(value=float(self.store.settings["opacity"]))
        font_size = tk.IntVar(value=int(self.store.settings["font_size"]))
        font_weight = tk.IntVar(value=self._font_weight())
        text_color = tk.StringVar(value=self._text_color().upper())
        page_lines = tk.IntVar(value=max(1, int(self.store.settings.get("page_lines", self.reader.layout_lines or 1))))
        panel_width = tk.IntVar(value=int(self.store.settings["panel_width"]))
        panel_height = tk.IntVar(value=int(self.store.settings["panel_height"]))
        auto_scroll_enabled = tk.BooleanVar(value=bool(self.store.settings.get("auto_scroll_enabled", False)))
        auto_scroll_speed = tk.IntVar(value=int(self.store.settings.get("auto_scroll_speed", 20)))
        opacity_text = tk.StringVar(value=f"{round(float(opacity.get()) * 100)}%")
        auto_scroll_speed_text = tk.StringVar(value=f"{auto_scroll_speed.get()} 像素/秒")
        layout_after_id: str | None = None
        pending_layout_offset: int | None = None

        def on_opacity(value: str) -> None:
            try:
                current = max(0.35, min(1.0, float(value)))
            except (TypeError, ValueError):
                return
            self.store.settings["opacity"] = round(current, 2)
            opacity_text.set(f"{round(current * 100)}%")
            self.set_panel_opacity(current)
            self._schedule_settings_save()

        def on_mode() -> None:
            self.store.settings["display_mode"] = mode_var.get()
            self._apply_display_mode(mode_var.get())
            self.set_panel_opacity(float(opacity.get()))
            self._schedule_settings_save()

        def on_reading_mode() -> None:
            flush_page_layout()
            offset = current_content_offset()
            previous_mode = self.reading_mode
            if previous_mode == "scroll":
                self.scroll_offset = offset
                self._save_scroll_position()
            self.reading_mode = reading_var.get()
            self.store.settings["reading_mode"] = self.reading_mode
            if self.reading_mode == "scroll":
                # Keep the page currently being read instead of restoring an
                # older saved scroll position when switching modes.
                self.scroll_offset = offset
                self.scroll_restore_by_offset = True
                if self.reader.path:
                    self.store.progress[f"scroll_offset::{self.reader.path}"] = self.scroll_offset
            else:
                # Convert the exact source offset to its containing page so
                # switching back to pagination never jumps to page one.
                if self.reader_layout_dirty:
                    self.reader.repaginate(offset=offset)
                    self.reader_layout_dirty = False
                else:
                    self.reader.page_index = self.reader.page_for_offset(offset)
                if self.reader.path:
                    self.store.progress[self.reader.path] = self.reader.page_index
                    self.store.progress[f"offset::{self.reader.path}"] = self.reader.current_offset()
            self._refresh_text()
            self._schedule_settings_save()

        def on_topmost() -> None:
            self.set_panel_topmost(bool(topmost_var.get()))
            self.store.settings["always_on_top"] = self.always_on_top
            self._schedule_settings_save()

        def on_auto_scroll_toggle() -> None:
            self.store.settings["auto_scroll_enabled"] = bool(auto_scroll_enabled.get())
            self.auto_scroll_remainder = 0.0
            self.auto_scroll_last_tick = time.monotonic()
            self._schedule_settings_save()

        def on_auto_scroll_speed(value: str) -> None:
            try:
                current = max(1, min(120, int(float(value))))
            except (TypeError, ValueError, tk.TclError):
                return
            self.store.settings["auto_scroll_speed"] = current
            auto_scroll_speed_text.set(f"{current} 像素/秒")
            self.auto_scroll_remainder = 0.0
            self._schedule_settings_save()

        def int_from(var: tk.Variable, low: int, high: int) -> int | None:
            try:
                return max(low, min(high, int(var.get())))
            except (TypeError, ValueError, tk.TclError):
                return None

        def current_content_offset() -> int:
            """Return the exact source character at the top of the panel."""
            if not self.reader.full_text:
                return 0
            if self.reading_mode == "scroll":
                if self.panel_visible:
                    self.text.update_idletasks()
                    self._update_scroll_location()
                return max(0, min(len(self.reader.full_text), int(self.scroll_offset)))
            return self.reader.current_offset() if self.reader.pages else 0

        def sync_page_layout(preserved_offset: int | None = None) -> None:
            width = int_from(panel_width, 1, 4000)
            height = int_from(panel_height, 1, 4000)
            font_value = int_from(font_size, 12, 30)
            if width is None or height is None or font_value is None:
                return
            offset = current_content_offset() if preserved_offset is None else preserved_offset
            effective_width, effective_height = self._effective_panel_size(width, height)
            self.text.configure(font=self._text_font(font_value))
            columns, lines, capacity = self._text_layout(effective_width, effective_height, font_value)
            padx = int(self.text.cget("padx"))
            usable_width = max(1, effective_width - padx * 2 - 2)
            self._pagination_font = tkfont.Font(root=self.root, font=self._text_font(font_value))
            self.reader.set_layout(columns, lines, usable_width, self._pagination_font.measure)
            self.reader.page_chars = capacity
            self.store.settings["page_chars"] = capacity
            self.store.settings["page_lines"] = lines
            page_lines.set(lines)
            if self.reader.path:
                if self.reading_mode == "scroll":
                    # Continuous reading uses the Text widget's own wrapping;
                    # rebuilding every page of a multi-million-character book
                    # here only freezes the settings UI.  Defer that work until
                    # the user actually switches back to page mode.
                    self.reader_layout_dirty = True
                    self.scroll_offset = offset
                    self.scroll_restore_by_offset = True
                    self.store.progress[f"offset::{self.reader.path}"] = offset
                    self._refresh_text(reinsert=False)
                else:
                    self.reader.repaginate(offset=offset)
                    self.reader_layout_dirty = False
                    self.store.progress[self.reader.path] = self.reader.page_index
                    self.store.progress[f"offset::{self.reader.path}"] = self.reader.current_offset()
                    self._refresh_text()

        def flush_page_layout() -> None:
            nonlocal layout_after_id, pending_layout_offset
            if layout_after_id:
                try:
                    self.root.after_cancel(layout_after_id)
                except tk.TclError:
                    pass
            layout_after_id = None
            offset = pending_layout_offset
            pending_layout_offset = None
            if offset is not None:
                sync_page_layout(offset)

        def queue_page_layout(offset: int) -> None:
            nonlocal layout_after_id, pending_layout_offset
            # Keep the location captured before the first change in a burst;
            # later spinbox/slider events are folded into one layout update.
            if pending_layout_offset is None:
                pending_layout_offset = offset
            if layout_after_id:
                try:
                    self.root.after_cancel(layout_after_id)
                except tk.TclError:
                    pass
            layout_after_id = self.root.after(180, flush_page_layout)

        def on_font(*_args: object) -> None:
            value = int_from(font_size, 12, 30)
            if value is not None:
                offset = current_content_offset()
                self.store.settings["font_size"] = value
                queue_page_layout(offset)
                self._schedule_settings_save()

        def on_weight(*_args: object) -> None:
            try:
                value = max(200, min(900, int(font_weight.get())))
            except (TypeError, ValueError, tk.TclError):
                return
            offset = current_content_offset()
            self.store.settings["font_weight"] = value
            # Variable-font rendering can update the visible glyphs directly;
            # expensive Text reflow is debounced below.
            self._render_text_overlay()
            queue_page_layout(offset)
            self._schedule_settings_save()

        def color_foreground(value: str) -> str:
            red, green, blue = (int(value[index : index + 2], 16) for index in (1, 3, 5))
            return "#101820" if red * 299 + green * 587 + blue * 114 > 150000 else "#ffffff"

        def choose_text_color() -> None:
            _rgb, selected = colorchooser.askcolor(color=text_color.get(), title="选择正文颜色", parent=win)
            if not selected:
                return
            selected = selected.upper()
            if selected == self.TRANSPARENT_COLOR.upper() and self.display_mode == "transparent":
                messagebox.showwarning("颜色不可用", "该颜色是透明模式的透明键，请选择其他颜色。", parent=win)
                return
            text_color.set(selected)
            self.store.settings["text_color"] = selected
            self.text.configure(fg=selected, insertbackground=selected)
            color_button.configure(bg=selected, activebackground=selected, fg=color_foreground(selected), activeforeground=color_foreground(selected))
            self._render_text_overlay()
            self._schedule_settings_save()

        def import_font() -> None:
            path = filedialog.askopenfilename(
                title="导入正文字体",
                filetypes=[("字体文件", "*.ttf *.otf *.ttc"), ("TrueType 字体", "*.ttf *.ttc"), ("OpenType 字体", "*.otf")],
                parent=win,
            )
            if not path:
                return
            try:
                offset = current_content_offset()
                family = self._import_font_file(path)
                font_name.set(family)
                queue_page_layout(offset)
                self._schedule_settings_save()
            except (OSError, ValueError) as exc:
                messagebox.showerror("字体导入失败", str(exc), parent=win)

        def on_panel_size(*_args: object) -> None:
            width = int_from(panel_width, 1, 4000)
            height = int_from(panel_height, 1, 4000)
            if width is None or height is None:
                return
            offset = current_content_offset()
            self.store.settings["panel_width"] = width
            self.store.settings["panel_height"] = height
            if self.panel_visible:
                self._place_panel()
            queue_page_layout(offset)
            self._schedule_settings_save()

        library_label_to_path: dict[str, str] = {}

        def refresh_library_controls(selected_path: str | None = None) -> None:
            paths = self._library_paths()
            base_labels = [
                f"{Path(path).stem} — {Path(path).parent.name or Path(path).parent}"
                for path in paths
            ]
            label_counts: dict[str, int] = {}
            for label in base_labels:
                label_counts[label] = label_counts.get(label, 0) + 1
            library_label_to_path.clear()
            labels: list[str] = []
            for path, base_label in zip(paths, base_labels):
                label = base_label if label_counts[base_label] == 1 else f"{Path(path).stem} — {Path(path).parent}"
                if not Path(path).is_file():
                    label += "（文件不存在）"
                library_label_to_path[label] = path
                labels.append(label)

            book_combo.configure(values=labels)
            target = selected_path or self.reader.path or str(self.store.settings.get("last_path", ""))
            selected_label = next(
                (
                    label
                    for label, path in library_label_to_path.items()
                    if target and self._path_key(path) == self._path_key(target)
                ),
                labels[0] if labels else "",
            )
            library_choice.set(selected_label or "尚未导入小说")
            switch_book_button.configure(state="normal" if labels else "disabled")
            if self.reader.path:
                current_book_text.set(f"当前阅读：{self.reader.title}")
            else:
                current_book_text.set("当前未打开小说")

        def switch_selected_book() -> None:
            flush_page_layout()
            path = library_label_to_path.get(library_choice.get())
            if not path:
                return
            if not Path(path).is_file():
                messagebox.showerror("无法切换小说", f"小说文件已经移动或删除：\n{path}", parent=win)
                return
            try:
                loaded = self._load_book(path)
            except (OSError, UnicodeError, ValueError) as exc:
                messagebox.showerror("无法切换小说", f"无法读取这本小说：\n{exc}", parent=win)
                return
            refresh_library_controls(loaded)
            win.lift()

        def import_library_book() -> None:
            flush_page_layout()
            loaded = self.open_book(win)
            if loaded:
                refresh_library_controls(loaded)
                win.lift()

        mode_var = tk.StringVar(value=self.display_mode)
        reading_var = tk.StringVar(value=self.reading_mode)
        topmost_var = tk.BooleanVar(value=self.always_on_top)
        font_name = tk.StringVar(value=self.font_family)
        library_choice = tk.StringVar()
        current_book_text = tk.StringVar()

        combo_style = ttk.Style(win)
        try:
            # Windows' native ttk theme ignores readonly field colours. Clam
            # consistently honours the dark library palette in source/EXE.
            combo_style.theme_use("clam")
        except tk.TclError:
            pass
        win.option_add("*TCombobox*Listbox.background", control_bg)
        win.option_add("*TCombobox*Listbox.foreground", text)
        win.option_add("*TCombobox*Listbox.selectBackground", "#2b79c2")
        win.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        combo_style.configure(
            "FishBar.TCombobox",
            fieldbackground=control_bg,
            background=control_bg,
            foreground=text,
            arrowcolor=text,
            bordercolor=border,
            lightcolor=control_bg,
            darkcolor=control_bg,
        )
        combo_style.map(
            "FishBar.TCombobox",
            fieldbackground=[("readonly", control_bg)],
            foreground=[("readonly", text)],
            selectbackground=[("readonly", control_bg)],
            selectforeground=[("readonly", text)],
        )

        library_section = make_section("小说书库", "导入多本 TXT 后可随时切换，每本书会单独保存阅读位置")
        book_row = tk.Frame(library_section, bg=surface)
        book_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        book_row.grid_columnconfigure(0, weight=1)
        book_combo = ttk.Combobox(book_row, textvariable=library_choice, state="readonly", style="FishBar.TCombobox")
        book_combo.grid(row=0, column=0, sticky="ew")
        switch_book_button = tk.Button(book_row, text="切换", command=switch_selected_book, bg="#344f67", fg="white", activebackground="#416786", activeforeground="white", relief="flat", padx=14, pady=4, cursor="hand2")
        switch_book_button.grid(row=0, column=1, padx=(8, 0))
        tk.Button(book_row, text="导入新小说…", command=import_library_book, bg="#2b79c2", fg="white", activebackground="#3b8bd1", activeforeground="white", relief="flat", padx=14, pady=4, cursor="hand2").grid(row=0, column=2, padx=(8, 0))
        tk.Label(library_section, textvariable=current_book_text, bg=surface, fg=muted, font=("Microsoft YaHei UI", 8), anchor="w").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        refresh_library_controls()

        appearance = make_section("外观", "调整面板的显示效果和正文样式")
        add_label(appearance, 2, "显示模式")
        mode_row = tk.Frame(appearance, bg=surface)
        mode_row.grid(row=2, column=1, sticky="w", pady=5)
        tk.Radiobutton(mode_row, text="纯透明文字", value="transparent", variable=mode_var, command=on_mode, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).pack(side="left")
        tk.Radiobutton(mode_row, text="半透明背景", value="tinted", variable=mode_var, command=on_mode, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).pack(side="left", padx=(18, 0))
        add_label(appearance, 3, "背景透明度")
        opacity_row = tk.Frame(appearance, bg=surface)
        opacity_row.grid(row=3, column=1, sticky="ew", pady=5)
        opacity_row.grid_columnconfigure(0, weight=1)
        opacity_scale = tk.Scale(opacity_row, from_=0.35, to=1.0, resolution=0.01, orient="horizontal", variable=opacity, showvalue=False, bg=surface, fg=text, troughcolor=control_bg, activebackground="#4d9de0", highlightthickness=0, bd=0, length=300, command=on_opacity)
        opacity_scale.grid(row=0, column=0, sticky="ew")
        tk.Label(opacity_row, textvariable=opacity_text, width=5, anchor="e", bg=surface, fg=secondary, font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, padx=(10, 0))
        add_label(appearance, 4, "正文大小")
        tk.Spinbox(appearance, from_=12, to=30, textvariable=font_size, width=8, bg=control_bg, fg=text, insertbackground=text, buttonbackground=control_bg, relief="flat", highlightthickness=0).grid(row=4, column=1, sticky="w", pady=5)
        add_label(appearance, 5, "正文字体")
        font_row = tk.Frame(appearance, bg=surface)
        font_row.grid(row=5, column=1, sticky="ew", pady=5)
        font_row.grid_columnconfigure(0, weight=1)
        tk.Label(font_row, textvariable=font_name, bg=control_bg, fg=text, anchor="w", padx=10, pady=4).grid(row=0, column=0, sticky="ew")
        tk.Button(font_row, text="导入字体…", command=import_font, bg="#344f67", fg="white", activebackground="#416786", activeforeground="white", relief="flat", padx=12, pady=4, cursor="hand2").grid(row=0, column=1, padx=(8, 0))
        add_label(appearance, 6, "字体粗细")
        weight_row = tk.Frame(appearance, bg=surface)
        weight_row.grid(row=6, column=1, sticky="ew", pady=5)
        weight_row.grid_columnconfigure(1, weight=1)
        tk.Label(weight_row, text="细", bg=surface, fg=muted, font=("Microsoft YaHei UI", 8)).grid(row=0, column=0, padx=(0, 5))
        weight_scale = tk.Scale(weight_row, from_=200, to=900, resolution=1, orient="horizontal", variable=font_weight, showvalue=False, bg=surface, troughcolor=control_bg, activebackground="#4d9de0", highlightthickness=0, bd=0, length=260)
        weight_scale.grid(row=0, column=1, sticky="ew")
        tk.Label(weight_row, text="粗", bg=surface, fg=muted, font=("Microsoft YaHei UI", 8)).grid(row=0, column=2, padx=(5, 8))
        tk.Spinbox(weight_row, from_=200, to=900, increment=1, textvariable=font_weight, width=5, bg=control_bg, fg=text, insertbackground=text, buttonbackground=control_bg, relief="flat", highlightthickness=0).grid(row=0, column=3)
        add_label(appearance, 7, "正文颜色")
        color_button = tk.Button(appearance, textvariable=text_color, command=choose_text_color, width=11, anchor="center", bg=text_color.get(), fg=color_foreground(text_color.get()), activebackground=text_color.get(), activeforeground=color_foreground(text_color.get()), relief="flat", cursor="hand2")
        color_button.grid(row=7, column=1, sticky="w", pady=5)

        lower_sections = tk.Frame(content, bg=bg)
        lower_sections.pack(fill="x")
        reading = make_section("阅读", "自动滚动仅在无级滚动且面板显示时生效", lower_sections, "left")
        add_label(reading, 2, "阅读方式")
        reading_row = tk.Frame(reading, bg=surface)
        reading_row.grid(row=2, column=1, sticky="w", pady=5)
        tk.Radiobutton(reading_row, text="翻页", value="page", variable=reading_var, command=on_reading_mode, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).pack(side="left")
        tk.Radiobutton(reading_row, text="无级滚动", value="scroll", variable=reading_var, command=on_reading_mode, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).pack(side="left", padx=(18, 0))
        add_label(reading, 3, "每页行数（自动）")
        tk.Spinbox(reading, from_=1, to=200, textvariable=page_lines, width=8, state="readonly", readonlybackground=control_bg, bg=control_bg, fg=text, insertbackground=text, buttonbackground=control_bg, relief="flat", highlightthickness=0).grid(row=3, column=1, sticky="w", pady=5)
        add_label(reading, 4, "自动滚动")
        tk.Checkbutton(reading, text="启用（隐藏时暂停）", variable=auto_scroll_enabled, command=on_auto_scroll_toggle, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).grid(row=4, column=1, sticky="w", pady=5)
        add_label(reading, 5, "滚动速度")
        auto_speed_row = tk.Frame(reading, bg=surface)
        auto_speed_row.grid(row=5, column=1, sticky="ew", pady=5)
        auto_speed_row.grid_columnconfigure(0, weight=1)
        tk.Scale(auto_speed_row, from_=1, to=120, resolution=1, orient="horizontal", variable=auto_scroll_speed, showvalue=False, bg=surface, troughcolor=control_bg, activebackground="#4d9de0", highlightthickness=0, bd=0, length=90, command=on_auto_scroll_speed).grid(row=0, column=0, sticky="ew")
        tk.Label(auto_speed_row, textvariable=auto_scroll_speed_text, width=9, anchor="e", bg=surface, fg=secondary, font=("Microsoft YaHei UI", 8)).grid(row=0, column=1, padx=(6, 0))

        window_section = make_section("窗口", "设置面板位置和显示行为", lower_sections, "right")
        add_label(window_section, 2, "窗口行为")
        tk.Checkbutton(window_section, text="窗口置顶", variable=topmost_var, command=on_topmost, bg=surface, fg=text, selectcolor=control_bg, activebackground=surface, activeforeground="white", highlightthickness=0).grid(row=2, column=1, sticky="w", pady=5)
        add_label(window_section, 3, "窗口尺寸")
        size_row = tk.Frame(window_section, bg=surface)
        size_row.grid(row=3, column=1, sticky="w", pady=5)
        tk.Label(size_row, text="宽", bg=surface, fg=muted, font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Spinbox(size_row, from_=1, to=4000, increment=20, textvariable=panel_width, width=6, bg=control_bg, fg=text, insertbackground=text, buttonbackground=control_bg, relief="flat", highlightthickness=0).pack(side="left", padx=(5, 10))
        tk.Label(size_row, text="高", bg=surface, fg=muted, font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Spinbox(size_row, from_=1, to=4000, increment=10, textvariable=panel_height, width=6, bg=control_bg, fg=text, insertbackground=text, buttonbackground=control_bg, relief="flat", highlightthickness=0).pack(side="left", padx=(5, 0))

        font_size.trace_add("write", on_font)
        font_weight.trace_add("write", on_weight)
        panel_width.trace_add("write", on_panel_size)
        panel_height.trace_add("write", on_panel_size)
        # Existing settings may predate automatic page sizing. Recalculate
        # once when opening this window so the displayed value and layout are
        # immediately consistent, even before the user changes a dimension.
        sync_page_layout()

        def save_settings() -> None:
            flush_page_layout()
            self.store.settings["opacity"] = round(float(opacity.get()), 2)
            self.store.settings["display_mode"] = mode_var.get()
            self.store.settings["reading_mode"] = reading_var.get()
            self.store.settings["auto_scroll_enabled"] = bool(auto_scroll_enabled.get())
            self.store.settings["auto_scroll_speed"] = max(1, min(120, int(auto_scroll_speed.get())))
            self.store.settings["always_on_top"] = bool(topmost_var.get())
            self.reader.page_chars = int(self.store.settings.get("page_chars", 520))
            self.text.configure(font=self._text_font(int(self.store.settings.get("font_size", 18))))
            self.set_panel_opacity(float(self.store.settings["opacity"]))
            if self.panel_visible:
                self._place_panel()
            self.store.save()
            win.destroy()
            self.settings_window = None

        def close_settings() -> None:
            flush_page_layout()
            self.store.save()
            win.destroy()
            self.settings_window = None

        footer = tk.Frame(win, bg=bg)
        footer.pack(fill="x", padx=24, pady=(0, 18))
        tk.Label(footer, text="鼠标离开面板后会自动隐藏\n快捷键：Ctrl+Alt+O 导入到书库 · H 显示/隐藏 · ←/→ 翻页或滚屏 · S 设置", bg=bg, fg=muted, justify="left", anchor="w", font=("Microsoft YaHei UI", 8)).pack(side="left", fill="x", expand=True)
        tk.Button(footer, text="保存并关闭", command=save_settings, bg="#2b79c2", fg="white", activebackground="#3b8bd1", activeforeground="white", relief="flat", padx=16, pady=7, cursor="hand2").pack(side="right")
        win.bind("<Return>", lambda _event: save_settings())
        win.protocol("WM_DELETE_WINDOW", close_settings)

    def _schedule_settings_save(self) -> None:
        if self.settings_save_after:
            try:
                self.root.after_cancel(self.settings_save_after)
            except tk.TclError:
                pass
        self.settings_save_after = self.root.after(300, self._persist_live_settings)

    def _schedule_progress_save(self) -> None:
        """Checkpoint active reading without re-arming a timer every pixel."""
        if self.settings_save_after is None:
            self.settings_save_after = self.root.after(1000, self._persist_live_settings)

    def _persist_live_settings(self) -> None:
        self.settings_save_after = None
        self.store.save()

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.reader.path:
            self._save_current_book_state()
        self.store.save()
        if IS_WINDOWS:
            self._remove_tray_icon()
            for hotkey_id in self.registered_hotkeys:
                unregister_hotkey(self.hwnd, hotkey_id)
            if self._old_wndproc and self.hwnd:
                try:
                    set_long = user32.SetWindowLongPtrW
                    set_long(self.hwnd, -4, ctypes.c_void_p(self._old_wndproc))
                except Exception:
                    pass
            remove_private_font(self.loaded_font_path)
            self.loaded_font_path = ""
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if sys.platform != "win32":
        print("FishBar 是 Windows 桌面原型；当前环境会使用兼容模式运行。")
    app = FishBarApp()
    if "--demo" in sys.argv:
        demo_book = Path(__file__).with_name("sample_novel.txt")
        if demo_book.is_file():
            app.reader.load(str(demo_book), 0)
    app.show_startup_hint()
    if "--settings" in sys.argv:
        app.open_settings()
    app.run()


if __name__ == "__main__":
    main()
