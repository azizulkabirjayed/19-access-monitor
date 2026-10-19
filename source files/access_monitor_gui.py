
import ctypes
import json
import math
import ntpath
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QEasingCurve,
    QEvent,
    QFileInfo,
    QIODevice,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFileIconProvider,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Access Monitor"
DLL_NAME = "access_monitor_core.dll"
TITLEBAR_HEIGHT = 32
RESIZE_MARGIN = 5
REFRESH_MS = 5000
SAMPLE_MS = 60000
LIVE_MS = 1000
HISTORY_KEEP_DAYS = 30
HISTORY_MAX_EVENTS = 60000

C_TITLEBAR = "#3C3C3C"
C_BODY = "#1E1E1E"
C_TEXT = "#CCCCCC"
C_MUTED = "#8A8A8A"
C_HOVER = "#505050"
C_ACCENT = "#007ACC"
C_CLOSE_HOVER = "#E81123"
C_OK = "#4EC9B0"
C_ERR = "#F48771"
C_WARN = "#E5C07B"

CAP_CAMERA, CAP_MIC, CAP_LOCATION, CAP_FILES, CAP_SCREEN, CAP_INPUT = 0, 1, 2, 3, 4, 5
CAP_COUNT = 6
CAP_CLIPBOARD = 6
CAP_AUDIO_USE = 7
CAP_NET_USE = 8
CAP_LABELS = ["Camera", "Microphone", "Location", "Files", "Screen", "Input"]
CAP_TIPS = [
    "Webcam access (ConsentStore\\webcam).",
    "Microphone access (ConsentStore\\microphone).",
    "Location access (ConsentStore\\location).",
    "Broad file-system access — the whole drive, not just the app's own folders\n"
    "(ConsentStore\\broadFileSystemAccess).",
    "Programmatic screen / window capture: screenshots and screen recording\n"
    "(ConsentStore\\graphicsCaptureProgrammatic).",
    "Human Interface Device access — raw keyboard, mouse and similar devices\n"
    "(ConsentStore\\humanInterfaceDevice).\n"
    "Note: Windows has no consent gate for global keyboard/mouse hooks used by\n"
    "classic desktop apps, so this column cannot prove an app is not logging input.",
]
EXTRA_CLIP, EXTRA_AUDIO, EXTRA_NET = 0, 1, 2
EXTRA_LABELS = ["Clipboard", "Audio", "Network"]
EXTRA_TIPS = [
    "MASTER switch: Windows clipboard history (Win+V).\n"
    "OFF stops Windows from keeping a list of everything you copy.\n"
    "There is no per-app clipboard permission, so this is the only real control.",
    "MASTER switch: mute the default audio output device.\n"
    "OFF mutes everything at once; the per-app switches below mute one app.",
    "MASTER switch: Windows Firewall default outbound action.\n"
    "OFF blocks every outbound connection that is not explicitly allowed \u2014\n"
    "this affects the whole system, not just one app. Needs Administrator.",
]
CLIP_TIP = (
    "Clipboard: who last put data on the clipboard (GetClipboardOwner).\n"
    "Windows has no per-app clipboard permission, so this is detection only —\n"
    "there is no switch, and apps that only READ the clipboard cannot be detected."
)

PERM_DEFAULT, PERM_DENY, PERM_ALLOW = -1, 0, 1
PERM_TEXT = {
    PERM_DEFAULT: "no per-app rule — Windows' default (allowed)",
    PERM_DENY: "Deny",
    PERM_ALLOW: "Allow",
}
CAP_COLORS = ["#4FA3E3", "#C586C0", "#4EC9B0", "#E5C07B",
              "#E06C75", "#9A86FD", "#56B6C2", "#98C379", "#7F8C9B"]
HIST_LABELS = CAP_LABELS + ["Clipboard", "Audio", "Network"]
HIST_RECORDED = CAP_NET_USE
NET_LEGEND_TIP = (
    "No per-app network history. Windows does not expose which app sent traffic "
    "without an ETW trace, so this series stays empty by design."
)

MATRIX_LABELS = [
    "Camera", "Microphone", "Location", "Files / Storage",
    "Screen Capture", "Input Devices", "Clipboard", "Audio Output", "Network",
]
RANGE_TODAY, RANGE_7D, RANGE_30D = "today", "7d", "30d"
RANGE_OPTIONS = [
    ("Today", RANGE_TODAY),
    ("Past 7 Days", RANGE_7D),
    ("Past 30 Days", RANGE_30D),
]


def _hour_label(hour: int) -> str:
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{hour12} {period}"


def build_time_rows(mode: str):
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if mode == RANGE_TODAY:
        rows = []
        for h in range(24):
            start = today + timedelta(hours=h)
            rows.append((_hour_label(h), start, start + timedelta(hours=1)))
        return rows
    if mode == RANGE_7D:
        rows = []
        for back in range(6, -1, -1):
            start = today - timedelta(days=back)
            rows.append((start.strftime("%a, %b %d"), start, start + timedelta(days=1)))
        return rows
    span_start = today - timedelta(days=29)
    rows = []
    for i in range(6):
        start = span_start + timedelta(days=i * 5)
        end = start + timedelta(days=5)
        label = f"{start.strftime('%b %d')}–{(end - timedelta(days=1)).strftime('%b %d')}"
        rows.append((label, start, end))
    return rows


def _row_index_for(mode: str, dt: datetime, today_date) -> "int | None":
    delta = (today_date - dt.date()).days
    if mode == RANGE_TODAY:
        return dt.hour if delta == 0 else None
    if delta < 0:
        return None
    if mode == RANGE_7D:
        return 6 - delta if delta <= 6 else None
    if delta > 29:
        return None
    return 5 - (delta // 5)


AM_PATH_MAX = 520


class AmApp(ctypes.Structure):

    _fields_ = [
        ("path", ctypes.c_wchar * AM_PATH_MAX),
        ("perm", ctypes.c_int * CAP_COUNT),
        ("in_use", ctypes.c_int * CAP_COUNT),
        ("audio", ctypes.c_int),
        ("net", ctypes.c_int),
        ("clip", ctypes.c_int),
        ("last_start", ctypes.c_longlong * CAP_COUNT),
        ("last_stop", ctypes.c_longlong * CAP_COUNT),
    ]


ZEROS = (0,) * CAP_COUNT


@dataclass
class AppInfo:
    path: str
    perm: tuple
    in_use: tuple
    audio: int
    net: int
    clip: int = 0
    last_start: tuple = field(default=ZEROS)
    last_stop: tuple = field(default=ZEROS)

    @property
    def name(self) -> str:
        return ntpath.basename(self.path)

    def signature(self):
        return (self.perm, self.in_use, self.audio, self.net, self.clip)


def _base_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


class CoreBackend:

    def __init__(self):
        path = os.path.join(_base_dir(), DLL_NAME)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"{DLL_NAME} was not found.\nRun build.bat first to build the DLL "
                f"(it must sit in the same folder as access_monitor_gui.py)."
            )
        d = self._dll = ctypes.CDLL(path)

        d.am_struct_size.restype = ctypes.c_int
        if d.am_struct_size() != ctypes.sizeof(AmApp):
            raise RuntimeError("The DLL and Python disagree on the struct size. Rebuild the DLL with build.bat.")

        d.am_is_admin.restype = ctypes.c_int
        d.am_scan.argtypes = [ctypes.c_int]
        d.am_scan.restype = ctypes.c_int
        d.am_count.restype = ctypes.c_int
        d.am_get.argtypes = [ctypes.c_int, ctypes.POINTER(AmApp)]
        d.am_get.restype = ctypes.c_int
        d.am_query.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(AmApp)]
        d.am_query.restype = ctypes.c_int
        d.am_get_global.argtypes = [ctypes.c_int]
        d.am_get_global.restype = ctypes.c_int
        d.am_set_global.argtypes = [ctypes.c_int, ctypes.c_int]
        d.am_set_global.restype = ctypes.c_int
        d.am_set_permission.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int]
        d.am_set_permission.restype = ctypes.c_int
        d.am_clear_permission.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        d.am_clear_permission.restype = ctypes.c_int
        d.am_clear_global.argtypes = [ctypes.c_int]
        d.am_clear_global.restype = ctypes.c_int
        d.am_set_audio_mute.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        d.am_set_audio_mute.restype = ctypes.c_int
        d.am_set_network_block.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
        d.am_set_network_block.restype = ctypes.c_int
        d.am_error_text.argtypes = [ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int]
        d.am_error_text.restype = ctypes.c_int
        for name in ("am_get_clipboard_history", "am_get_audio_master", "am_get_net_master"):
            getattr(d, name).argtypes = []
            getattr(d, name).restype = ctypes.c_int
        for name in ("am_set_clipboard_history", "am_set_audio_master", "am_set_net_master"):
            getattr(d, name).argtypes = [ctypes.c_int]
            getattr(d, name).restype = ctypes.c_int

    @staticmethod
    def _to_info(a: AmApp) -> AppInfo:
        return AppInfo(
            a.path, tuple(a.perm), tuple(a.in_use), a.audio, a.net, a.clip,
            tuple(a.last_start), tuple(a.last_stop),
        )

    def scan(self, include_running: bool):
        n = self._dll.am_scan(1 if include_running else 0)
        apps, buf = [], AmApp()
        for i in range(n):
            if self._dll.am_get(i, ctypes.byref(buf)) == 0:
                apps.append(self._to_info(buf))
        return apps

    def query(self, path: str):
        buf = AmApp()
        if self._dll.am_query(path, ctypes.byref(buf)) == 0:
            return self._to_info(buf)
        return None

    def is_admin(self) -> bool:
        return bool(self._dll.am_is_admin())

    def get_global(self, cap: int) -> bool:
        return self._dll.am_get_global(cap) != 0

    def get_extra(self, which: int) -> bool:
        fn = (self._dll.am_get_clipboard_history,
              self._dll.am_get_audio_master,
              self._dll.am_get_net_master)[which]
        return fn() != 0

    def set_extra(self, which: int, on: bool) -> int:
        fn = (self._dll.am_set_clipboard_history,
              self._dll.am_set_audio_master,
              self._dll.am_set_net_master)[which]
        return fn(1 if on else 0)

    def set_global(self, cap: int, allow: bool) -> int:
        return self._dll.am_set_global(cap, 1 if allow else 0)

    def set_permission(self, path: str, cap: int, allow: bool) -> int:
        return self._dll.am_set_permission(path, cap, 1 if allow else 0)

    def clear_permission(self, path: str, cap: int) -> int:
        return self._dll.am_clear_permission(path, cap)

    def clear_global(self, cap: int) -> int:
        return self._dll.am_clear_global(cap)

    def set_audio_mute(self, path: str, mute: bool) -> int:
        return self._dll.am_set_audio_mute(path, 1 if mute else 0)

    def set_network_block(self, path: str, block: bool) -> int:
        return self._dll.am_set_network_block(path, 1 if block else 0)

    def error_text(self, code: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        self._dll.am_error_text(code, buf, 512)
        return buf.value or f"Error {code}"


FT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
FT_MIN_VALID = 125000000000000000


def ft_to_local(ft: int) -> datetime:
    return (FT_EPOCH + timedelta(microseconds=ft // 10)).astimezone()


def history_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AccessMonitor")


class UsageHistory:

    def __init__(self):
        self.dir = history_dir()
        self.file = os.path.join(self.dir, "usage_history.json")
        self.events: list = []
        self._keys: set = set()
        self._clip_owner = ""
        self._audio_live: set = set()
        self.error = ""
        self._load()

    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                data = json.load(f)
            cutoff = self._cutoff_ft()
            for ev in data.get("events", []):
                if len(ev) != 3:
                    continue
                ft, cap, path = int(ev[0]), int(ev[1]), str(ev[2])
                if ft < cutoff or not (0 <= cap < HIST_RECORDED):
                    continue
                key = (ft, cap, path.lower())
                if key in self._keys:
                    continue
                self._keys.add(key)
                self.events.append([ft, cap, path])
        except FileNotFoundError:
            pass
        except Exception as e:
            self.error = f"History file could not be read ({e}). A new one will be started."
            self.events, self._keys = [], set()

    @staticmethod
    def _cutoff_ft() -> int:
        cut = datetime.now(timezone.utc) - timedelta(days=HISTORY_KEEP_DAYS)
        return int((cut - FT_EPOCH).total_seconds() * 10_000_000)

    def save(self) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "events": self.events}, f, separators=(",", ":"))
            os.replace(tmp, self.file)
            return True
        except Exception as e:
            self.error = f"History could not be saved ({e})."
            return False

    def record(self, apps) -> int:
        added = 0
        for a in apps:
            for cap in range(CAP_COUNT):
                ft = int(a.last_start[cap])
                if ft < FT_MIN_VALID:
                    continue
                key = (ft, cap, a.path.lower())
                if key in self._keys:
                    continue
                self._keys.add(key)
                self.events.append([ft, cap, a.path])
                added += 1
            if a.clip and a.path.lower() != self._clip_owner:
                self._clip_owner = a.path.lower()
                added += self._log_now(CAP_CLIPBOARD, a.path)

        live = {a.path.lower() for a in apps if a.audio != -2}
        for a in apps:
            if a.audio != -2 and a.path.lower() not in self._audio_live:
                added += self._log_now(CAP_AUDIO_USE, a.path)
        self._audio_live = live
        if added:
            cutoff = self._cutoff_ft()
            if any(ft < cutoff for ft, _cap, _path in self.events):
                kept = []
                for ft, cap, path in self.events:
                    if ft < cutoff:
                        self._keys.discard((ft, cap, path.lower()))
                    else:
                        kept.append([ft, cap, path])
                self.events = kept
            if len(self.events) > HISTORY_MAX_EVENTS:
                self.events.sort(key=lambda e: e[0])
                drop = self.events[: len(self.events) - HISTORY_MAX_EVENTS]
                self.events = self.events[len(drop):]
                for ft, cap, path in drop:
                    self._keys.discard((ft, cap, path.lower()))
            self.save()
        return added

    def _log_now(self, cap: int, path: str) -> int:
        ft = int((datetime.now(timezone.utc) - FT_EPOCH).total_seconds() * 10_000_000)
        key = (ft, cap, path.lower())
        if key in self._keys:
            return 0
        self._keys.add(key)
        self.events.append([ft, cap, path])
        return 1

    def clear(self):
        self.events, self._keys = [], set()
        self._clip_owner = ""
        self._audio_live = set()
        try:
            os.remove(self.file)
        except OSError:
            pass

    def daily(self, cap: int, days: int):
        today = date.today()
        first = today - timedelta(days=days - 1)
        buckets = {first + timedelta(days=i): {} for i in range(days)}
        for ft, c, path in self.events:
            if c != cap:
                continue
            try:
                d = ft_to_local(ft).date()
            except (OverflowError, OSError, ValueError):
                continue
            bucket = buckets.get(d)
            if bucket is None:
                continue
            name = ntpath.basename(path) or path
            bucket[name] = bucket.get(name, 0) + 1
        return [(d, len(buckets[d]), buckets[d]) for d in sorted(buckets)]

    def top_apps(self, cap: int, days: int, limit: int = 5):
        totals = {}
        for _d, _n, apps in self.daily(cap, days):
            for name, n in apps.items():
                totals[name] = totals.get(name, 0) + n
        return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def span_text(self) -> str:
        if not self.events:
            return "no data yet"
        oldest = ft_to_local(min(e[0] for e in self.events)).date()
        return f"recording since {oldest.isoformat()} · {len(self.events)} events"

def create_backend():
    if sys.platform != "win32":
        return None, "Access Monitor only runs on Windows."
    try:
        return CoreBackend(), None
    except Exception as e:
        return None, str(e)


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C_ACCENT))
        p.drawRoundedRect(0, 0, size, size, size * 0.2, size * 0.2)
        p.setPen(QColor("white"))
        font = QFont("Segoe UI")
        font.setBold(True)
        font.setPixelSize(int(size * 0.62))
        p.setFont(font)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "A")
        p.end()
        icon.addPixmap(pm)
    return icon


class CaptionButton(QPushButton):

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(46, TITLEBAR_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_kind(self, kind: str):
        self.kind = kind
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        hovered = self.underMouse()
        if hovered:
            bg = C_CLOSE_HOVER if self.kind == "close" else C_HOVER
        else:
            bg = C_TITLEBAR
        p.fillRect(self.rect(), QColor(bg))
        pen = QPen(QColor("#FFFFFF" if hovered else C_TEXT))
        pen.setWidth(1)
        p.setPen(pen)
        cx, cy = self.width() // 2, self.height() // 2
        if self.kind == "min":
            p.drawLine(cx - 5, cy, cx + 5, cy)
        elif self.kind == "max":
            p.drawRect(cx - 5, cy - 5, 10, 10)
        elif self.kind == "restore":
            p.drawRect(cx - 5, cy - 2, 8, 8)
            p.drawPolyline(
                QPolygon(
                    [
                        QPoint(cx - 2, cy - 2),
                        QPoint(cx - 2, cy - 5),
                        QPoint(cx + 5, cy - 5),
                        QPoint(cx + 5, cy + 2),
                        QPoint(cx + 3, cy + 2),
                    ]
                )
            )
        elif self.kind == "close":
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.drawLine(cx - 5, cy - 5, cx + 5, cy + 5)
            p.drawLine(cx - 5, cy + 5, cx + 5, cy - 5)


class TitleBar(QWidget):

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self._win = window
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLEBAR_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        icon_label = QLabel()
        icon_label.setPixmap(window.windowIcon().pixmap(16, 16))
        icon_label.setContentsMargins(10, 0, 8, 0)
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(icon_label)

        title = QLabel(APP_NAME)
        title.setObjectName("titleText")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(title)

        lay.addStretch(1)

        self.btn_min = CaptionButton("min")
        self.btn_max = CaptionButton("max")
        self.btn_close = CaptionButton("close")
        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(window.toggle_max_restore)
        self.btn_close.clicked.connect(window.close)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._win.isMaximized():
            handle = self._win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._win.toggle_max_restore()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class MenuBar(QWidget):

    PAGE_NAMES = ("Apps and their permissions", "Usage History", "Live Activity")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("menuBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLEBAR_HEIGHT)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(0)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, name in enumerate(self.PAGE_NAMES):
            btn = QPushButton(name)
            btn.setObjectName("menuBtn")
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.ArrowCursor)
            btn.setFixedHeight(TITLEBAR_HEIGHT)
            self.group.addButton(btn, i)
            lay.addWidget(btn)
        self.group.button(0).setChecked(True)
        lay.addStretch(1)


class ToggleSwitch(QAbstractButton):
    W, H = 56, 24

    def __init__(self, checked: bool = False, parent=None, compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        self.W, self.H = (34, 18) if compact else (44, 24)
        self._track = (26, 13) if compact else (36, 18)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._ghost = False

    def set_ghost(self, v: bool):
        self._ghost = v
        self.update()

    def sizeHint(self):
        return QSize(self.W, self.H)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()

        if not self.isEnabled():
            p.setOpacity(0.4)
        tw, th = self._track
        x0 = (self.width() - tw) / 2
        y0 = (h - th) / 2
        track = QRectF(x0, y0, tw, th)

        if self._ghost:
            pen = QPen(QColor("#6E6E6E"))
            pen.setWidthF(1.4)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(0.7, 0.7, -0.7, -0.7), th / 2, th / 2)
            knob = QColor("#9A9A9A")
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C_ACCENT if self.isChecked() else "#5A5A5A"))
            p.drawRoundedRect(track, th / 2, th / 2)
            knob = QColor("#FFFFFF")

        d = th - 6
        kx = x0 + (tw - d - 3 if self.isChecked() else 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob)
        p.drawEllipse(QRectF(kx, y0 + 3, d, d))


def _centered(widget: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(widget)
    return box


def show_info_popup(parent: QWidget, title: str, text: str):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def _icon_to_base64_png(icon: QIcon, size: int = 16) -> str:
    try:
        pix = icon.pixmap(size, size)
        if pix.isNull():
            return ""
        buf = QByteArray()
        qbuf = QBuffer(buf)
        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(qbuf, "PNG")
        return bytes(buf.toBase64().data()).decode("ascii")
    except Exception:
        return ""


def add_action_bar(layout: QHBoxLayout, on_refresh, parent: QWidget, title: str, description: str, extra_widgets=None):
    layout.addStretch(1)
    if extra_widgets:
        for w in extra_widgets:
            layout.addWidget(w)
    btn_refresh = QPushButton("Refresh")
    btn_refresh.setObjectName("toolBtn")
    btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if on_refresh is not None:
        btn_refresh.clicked.connect(on_refresh)
    layout.addWidget(btn_refresh)

    btn_info = QPushButton("ⓘ")
    btn_info.setObjectName("infoBtn")
    btn_info.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_info.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn_info.setToolTip("Show a description of this page")
    btn_info.clicked.connect(lambda: show_info_popup(parent, title, description))
    layout.addWidget(btn_info)
    return btn_refresh, btn_info


class AppsPage(QWidget):
    COLUMNS = ["App"] + CAP_LABELS + ["Clipboard", "Audio", "Network"]
    COL_CLIP = 1 + CAP_COUNT
    COL_AUDIO = COL_CLIP + 1
    COL_NET = COL_CLIP + 2
    COL_APP_MIN = 230
    COL_W = 104
    HEADER_H = 54

    def __init__(self, backend, history: "UsageHistory | None" = None):
        super().__init__()
        self.setObjectName("appsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.b = backend
        self.h = history
        self._apps: list[AppInfo] = []
        self._row_of: dict[str, int] = {}
        self._global = [True] * CAP_COUNT
        self._admin = backend.is_admin()
        self._icons = QFileIconProvider()
        self._description = self._build_description()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.btn_refresh, self.btn_info = add_action_bar(
            top, self._on_refresh_clicked, self, "Apps and their permissions", self._description
        )
        root.addLayout(top)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setIconSize(QSize(20, 20))
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setFixedHeight(self.HEADER_H)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, self.COL_APP_MIN)
        for c in range(1, len(self.COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, self.COL_W)
            self.table.horizontalHeaderItem(c).setTextAlignment(
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
            )
        self.table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        for cap in range(CAP_COUNT):
            self.table.horizontalHeaderItem(1 + cap).setToolTip(
                CAP_TIPS[cap] + "\n\nThe small switch under the title is the master switch "
                "for ALL desktop apps."
            )
        self.table.horizontalHeaderItem(self.COL_CLIP).setToolTip(CLIP_TIP)
        self.table.horizontalHeaderItem(self.COL_AUDIO).setToolTip(
            "Audio: ON = sound on, OFF = muted. Only for apps that currently have an audio session."
        )
        self.table.horizontalHeaderItem(self.COL_NET).setToolTip(
            "Network: OFF = inbound and outbound blocked in Windows Firewall. Needs Administrator."
        )

        self.master = []
        for cap, label in enumerate(CAP_LABELS):
            sw = ToggleSwitch(True, hh, compact=True)
            sw.setToolTip(
                f"MASTER switch: {label} for ALL desktop (.exe) apps at once.\n"
                "Windows itself enforces this one, so it is far more reliable\n"
                "than the per-app switches in the column below."
            )
            sw.clicked.connect(lambda _=False, c=cap: self._on_master(c))
            self.master.append(sw)

        self.extra = []
        for which, label in enumerate(EXTRA_LABELS):
            sw = ToggleSwitch(True, hh, compact=True)
            sw.setToolTip(EXTRA_TIPS[which])
            if which == EXTRA_NET and not self._admin:
                sw.setEnabled(False)
                sw.setToolTip(EXTRA_TIPS[which] + "\n\nRead-only: restart as Administrator.")
            sw.clicked.connect(lambda _=False, w=which: self._on_extra(w))
            self.extra.append(sw)

        root.addWidget(self.table, 1)
        hh.sectionResized.connect(lambda *_: self._place_master())
        self.table.horizontalScrollBar().valueChanged.connect(lambda *_: self._place_master())
        QTimer.singleShot(0, self._fit_columns)

        self.status = QLabel("")
        self.status.setObjectName("status")
        root.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_refresh)
        self.timer.start(REFRESH_MS)
        self.refresh(force_rebuild=True)

    def _build_description(self) -> str:
        if self._admin:
            mode = "You're running this app with full (Administrator) access, so the Network switch can be turned on or off."
        else:
            mode = (
                "The Network switch is view-only right now. To change it, close this app and reopen it "
                "by right-clicking it and choosing \"Run as administrator\"."
            )
        hint = (
            "This page lists every app on your PC along with the permissions it has — things like "
            "Camera, Microphone, or Location.\n\n"
            "Each column has a small switch at the top. That switch turns a permission on or off for "
            "ALL apps at once — for example, turning off \"Camera\" there blocks every app from using "
            "your camera.\n\n"
            "Right-click a switch to choose Allow, Deny, or go back to Windows' own default setting.\n\n"
            "Clipboard doesn't have a per-app switch in Windows, only the one at the top.\n\n"
            "To see which apps are using a permission right this second, check the Live Activity page instead."
        )
        return f"{mode}\n\n{hint}"

    def _place_master(self):
        if not self.master:
            return
        hh = self.table.horizontalHeader()
        y = self.HEADER_H - self.master[0].height() - 7
        width = hh.width()
        pairs = [(1 + cap, sw) for cap, sw in enumerate(self.master)]
        pairs += [(self.COL_CLIP, self.extra[EXTRA_CLIP]),
                  (self.COL_AUDIO, self.extra[EXTRA_AUDIO]),
                  (self.COL_NET, self.extra[EXTRA_NET])]
        for col, sw in pairs:
            left = hh.sectionViewportPosition(col)
            cx = left + hh.sectionSize(col) / 2
            sw.move(int(cx - sw.width() / 2), y)
            sw.setVisible(left >= -2 and left + hh.sectionSize(col) <= width + 2)
            sw.raise_()

    def _fit_columns(self):
        fixed = self.COL_W * (len(self.COLUMNS) - 1)
        avail = self.table.viewport().width()
        self.table.setColumnWidth(0, max(self.COL_APP_MIN, avail - fixed))
        self._place_master()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_columns)

    def _say(self, text, ok=True):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {C_OK if ok else C_ERR};")

    @staticmethod
    def _set_checked_silent(sw: QAbstractButton, value: bool):
        sw.blockSignals(True)
        sw.setChecked(value)
        sw.blockSignals(False)

    def _on_refresh_clicked(self):
        self.refresh(force_rebuild=True)

    def _auto_refresh(self):
        win = self.window()
        if not self.isVisible() or (win is not None and win.isMinimized()):
            return
        self.refresh(force_rebuild=False)

    @staticmethod
    def _sort_key(a: AppInfo):
        if any(p != -1 for p in a.perm) or a.net == 1:
            rank = 0
        elif a.audio != -2:
            rank = 1
        else:
            rank = 2
        return (rank, a.name.lower())

    def refresh(self, force_rebuild: bool = False):
        new_global = [self.b.get_global(i) for i in range(CAP_COUNT)]
        apps = self.b.scan(True)
        if self.h is not None:
            self.h.record(apps)

        if force_rebuild or len(apps) != len(self._apps):
            apps.sort(key=self._sort_key)
        else:
            order = {a.path.lower(): i for i, a in enumerate(self._apps)}
            apps.sort(key=lambda a: (order.get(a.path.lower(), 1 << 30), a.name.lower()))

        for i, sw in enumerate(self.master):
            self._set_checked_silent(sw, new_global[i])
        for which, sw in enumerate(self.extra):
            try:
                self._set_checked_silent(sw, self.b.get_extra(which))
            except Exception:
                pass

        same_rows = [a.path.lower() for a in apps] == [a.path.lower() for a in self._apps]
        if force_rebuild or not same_rows or new_global != self._global:
            self._global = new_global
            self._rebuild(apps)
        else:
            for row, (old, new) in enumerate(zip(self._apps, apps)):
                if old.signature() != new.signature():
                    self._apps[row] = new
                    self._populate_row(row, new, create_name=False)

    def _rebuild(self, apps):
        scroll = self.table.verticalScrollBar().value()
        self._apps = list(apps)
        self._row_of = {a.path.lower(): i for i, a in enumerate(apps)}
        for r in range(self.table.rowCount()):
            for c in range(1, self.table.columnCount()):
                w = self.table.cellWidget(r, c)
                if w is not None:
                    w.hide()
        self.table.setRowCount(0)
        self.table.setRowCount(len(apps))
        for row, app in enumerate(apps):
            self._populate_row(row, app, create_name=True)
        self.table.verticalScrollBar().setValue(scroll)
        if not apps:
            self._say("No apps found.", ok=True)

    def _set_cell(self, row: int, col: int, widget: QWidget):
        old = self.table.cellWidget(row, col)
        if old is not None:
            old.hide()
        self.table.setCellWidget(row, col, widget)

    def _populate_row(self, row: int, app: AppInfo, create_name: bool):
        if create_name:
            item = QTableWidgetItem(self._icons.icon(QFileInfo(app.path)), app.name)
            item.setToolTip(app.path)
            self.table.setItem(row, 0, item)

        for cap in range(CAP_COUNT):
            self._set_cell(row, 1 + cap, self._perm_cell(app, cap))

        lab = QLabel("—")
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setToolTip(CLIP_TIP)
        self._set_cell(row, self.COL_CLIP, lab)

        if app.audio == -2:
            lab = QLabel("—")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setToolTip("This app has no audio session right now (it is not playing sound).")
            self._set_cell(row, self.COL_AUDIO, lab)
        else:
            sw = ToggleSwitch(app.audio == 1)
            sw.setToolTip("ON = sound on, OFF = muted")
            sw.clicked.connect(lambda _=False, p=app.path, s=sw: self._on_audio(p, s))
            self._set_cell(row, self.COL_AUDIO, _centered(sw))

        sw = ToggleSwitch(app.net == 0)
        if self._admin:
            sw.setToolTip("ON = allowed, OFF = blocked in Windows Firewall")
            sw.clicked.connect(lambda _=False, p=app.path, s=sw: self._on_net(p, s))
        else:
            sw.setEnabled(False)
            sw.setToolTip("Restart the app as Administrator to change network access.")
        self._set_cell(row, self.COL_NET, _centered(sw))

    def _perm_cell(self, app: AppInfo, cap: int) -> QWidget:
        allowed_globally = self._global[cap]
        perm = app.perm[cap]
        on = allowed_globally and perm != 0
        sw = ToggleSwitch(on)
        sw.set_ghost(perm == -1)
        if not allowed_globally:
            sw.setEnabled(False)
            sw.setToolTip(f"{CAP_LABELS[cap]} is turned off for all desktop apps (master switch above).")
        else:
            sw.setToolTip(
                f"{CAP_LABELS[cap]}: {PERM_TEXT[perm]}\n"
                "Left-click: Allow / Deny.   Right-click: Default / Allow / Deny."
            )
            sw.clicked.connect(lambda _=False, p=app.path, c=cap, s=sw: self._on_perm(p, c, s))
            sw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            sw.customContextMenuRequested.connect(
                lambda pos, p=app.path, c=cap, s=sw, v=perm: self._perm_menu(s, pos, p, c, v)
            )
        return _centered(sw)

    def _perm_menu(self, sw: QWidget, pos, path: str, cap: int, perm: int):
        menu = QMenu(self)
        menu.setTitle(CAP_LABELS[cap])
        actions = []
        for label, value in (("Windows default", -1), ("Allow", 1), ("Deny", 0)):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(perm == value)
            actions.append((act, value))
        chosen = menu.exec(sw.mapToGlobal(pos))
        for act, value in actions:
            if act is chosen:
                if value != perm:
                    self._apply_perm(path, cap, value)
                return

    def _apply_perm(self, path: str, cap: int, value: int):
        if value == -1:
            code = self.b.clear_permission(path, cap)
            what = "reset to Windows default"
        else:
            code = self.b.set_permission(path, cap, value == 1)
            what = "Allow" if value == 1 else "Deny"
        label = f"{self._name(path)} → {CAP_LABELS[cap]}"
        if code != 0:
            self._say(f"{label}: {what} failed: {self.b.error_text(code)}", ok=False)
        else:
            self._say(f"{label}: {what}.")
        self._after_change(path)

    @staticmethod
    def _name(path: str) -> str:
        return ntpath.basename(path)

    def _after_change(self, path: str):
        info = self.b.query(path)
        row = self._row_of.get(path.lower())
        if info is not None and row is not None:
            self._apps[row] = info
            self._populate_row(row, info, create_name=False)

    def _fail(self, sw: QAbstractButton, checked: bool, what: str, code: int):
        self._set_checked_silent(sw, not checked)
        self._say(f"{what} failed: {self.b.error_text(code)}", ok=False)

    def _on_perm(self, path, cap, sw):
        self._apply_perm(path, cap, PERM_ALLOW if sw.isChecked() else PERM_DENY)

    def _on_audio(self, path, sw):
        checked = sw.isChecked()
        code = self.b.set_audio_mute(path, not checked)
        what = f"{self._name(path)} → Audio"
        if code != 0:
            return self._fail(sw, checked, what, code)
        self._say(f"{what}: {'sound on' if checked else 'muted'}.")
        self._after_change(path)

    def _on_net(self, path, sw):
        checked = sw.isChecked()
        code = self.b.set_network_block(path, not checked)
        what = f"{self._name(path)} → Network"
        if code != 0:
            return self._fail(sw, checked, what, code)
        self._say(f"{what}: {'allowed' if checked else 'blocked'}.")
        self._after_change(path)

    def _on_extra(self, which: int):
        sw = self.extra[which]
        checked = sw.isChecked()
        label = EXTRA_LABELS[which]
        if which == EXTRA_NET and not checked:
            answer = QMessageBox.question(
                self,
                "Block all outbound traffic?",
                "This sets Windows Firewall's default outbound action to Block for the active "
                "profiles.\nEvery program without an explicit allow rule loses network access, "
                "not just one app.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_checked_silent(sw, True)
                return
        code = self.b.set_extra(which, checked)
        what = f"All apps → {label}"
        if code != 0:
            self._set_checked_silent(sw, not checked)
            self._say(f"{what} failed: {self.b.error_text(code)}", ok=False)
            return
        if which == EXTRA_CLIP:
            state = "history on" if checked else "history off"
        elif which == EXTRA_AUDIO:
            state = "sound on" if checked else "muted"
        else:
            state = "outbound allowed by default" if checked else "outbound blocked by default"
        self._say(f"{what}: {state}.")
        self.refresh(force_rebuild=True)

    def _on_master(self, cap):
        checked = self.master[cap].isChecked()
        code = self.b.set_global(cap, checked)
        what = f"All desktop apps → {CAP_LABELS[cap]}"
        if code != 0:
            self._set_checked_silent(self.master[cap], not checked)
            self._say(f"{what} failed: {self.b.error_text(code)}", ok=False)
            return
        self._say(f"{what}: set to {'Allow' if checked else 'Deny'}.")
        self.refresh(force_rebuild=True)


class ActivityMatrixView(QWidget):

    PAD_L, PAD_R, PAD_T, PAD_B = 150, 20, 48, 18
    MAX_DOTS = 5

    @staticmethod
    def _split_label(label: str):
        if " " not in label:
            return [label]
        head, _sep, tail = label.rpartition(" ")
        return [head, tail]

    def __init__(self, icon_provider: QFileIconProvider, parent=None):
        super().__init__(parent)
        self.setObjectName("activityMatrix")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(360)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._icons = icon_provider
        self._icon_cache: dict = {}
        self._row_labels: list = []
        self._cells: dict = {}
        self._hover_key = None
        self._hover_hits: list = []

    def set_data(self, row_labels, cells: dict):
        self._row_labels = row_labels
        self._cells = cells
        self._hover_key = None
        QToolTip.hideText()
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(
            self.PAD_L,
            self.PAD_T,
            max(10.0, self.width() - self.PAD_L - self.PAD_R),
            max(10.0, self.height() - self.PAD_T - self.PAD_B),
        )

    def _col_x(self, col: int) -> float:
        r = self._plot()
        n = len(MATRIX_LABELS)
        return r.left() + r.width() * (col + 0.5) / n

    def _row_y(self, row: int) -> float:
        r = self._plot()
        n = max(1, len(self._row_labels))
        return r.top() + r.height() * (row + 0.5) / n

    def _cell_rect(self, row: int, col: int) -> QRectF:
        r = self._plot()
        n_c = len(MATRIX_LABELS)
        n_r = max(1, len(self._row_labels))
        cw = r.width() / n_c
        rh = r.height() / n_r
        return QRectF(r.left() + col * cw, r.top() + row * rh, cw, rh)

    def _dot_icon(self, path: str) -> QIcon:
        key = path.lower()
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = self._icons.icon(QFileInfo(path))
            self._icon_cache[key] = icon
        return icon

    def _dot_positions(self, row: int, col: int, count: int):
        rect = self._cell_rect(row, col)
        cx, cy = rect.center().x(), rect.center().y()
        shown = min(count, self.MAX_DOTS)
        if shown <= 1:
            return [(cx, cy)]
        radius = min(rect.width(), rect.height()) * 0.26
        pts = []
        for i in range(shown):
            angle = 2 * math.pi * i / shown
            pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        return pts

    def _hit_test(self, pos: QPointF):
        for key, hx, hy, info in self._hover_hits:
            if (pos.x() - hx) ** 2 + (pos.y() - hy) ** 2 <= 8 ** 2:
                return key, info
        return None, None

    def mouseMoveEvent(self, event):
        key, info = self._hit_test(event.position())
        if key != self._hover_key:
            self._hover_key = key
            if info is not None:
                QToolTip.showText(event.globalPosition().toPoint(), self._tip_html(info), self)
            else:
                QToolTip.hideText()
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_key = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def _tip_html(self, info: dict) -> str:
        b64 = _icon_to_base64_png(info["icon"])
        img = f'<img src="data:image/png;base64,{b64}" width="16" height="16"> ' if b64 else ""
        when = info["dt"].strftime("%a, %b %d %Y · %I:%M:%S %p")
        freq = info["count"]
        freq_line = (
            f"{freq} access(es) in this slot"
            if freq > 1
            else "1 access in this slot"
        )
        return (
            f"<div style='white-space:nowrap'>"
            f"<b>{img}{info['name']}</b><br>"
            f"{MATRIX_LABELS[info['cap']]}<br>"
            f"{when}<br>"
            f"{freq_line}"
            f"</div>"
        )

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot()

        font = self.font()
        font.setPixelSize(11)
        p.setFont(font)

        if not self._row_labels:
            p.setPen(QColor(C_MUTED))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "No data yet.")
            return

        p.setPen(QColor("#333333"))
        for row in range(len(self._row_labels) + 1):
            y = r.top() + r.height() * row / len(self._row_labels)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        for col in range(len(MATRIX_LABELS) + 1):
            x = r.left() + r.width() * col / len(MATRIX_LABELS)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))

        p.setPen(QColor(C_MUTED))
        for row, label in enumerate(self._row_labels):
            p.drawText(
                QRectF(0, self._row_y(row) - 9, self.PAD_L - 12, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        cw = r.width() / len(MATRIX_LABELS)
        line_h = 14
        for col, label in enumerate(MATRIX_LABELS):
            lines = self._split_label(label)
            p.setPen(QColor(CAP_COLORS[col]))
            top_y = r.top() - 8 - line_h * len(lines)
            cx = self._col_x(col)
            for li, line in enumerate(lines):
                p.drawText(
                    QRectF(cx - cw / 2, top_y + li * line_h, cw, line_h),
                    Qt.AlignmentFlag.AlignCenter,
                    line,
                )

        self._hover_hits = []
        for (row, col), apps in self._cells.items():
            if not apps:
                continue
            col_color = QColor(CAP_COLORS[col])
            positions = self._dot_positions(row, col, len(apps))
            for i, (hx, hy) in enumerate(positions):
                entry = apps[i]
                key = (row, col, entry["name"])
                hovered = key == self._hover_key
                p.setPen(Qt.PenStyle.NoPen)
                glow = QColor(col_color)
                glow.setAlpha(255 if hovered else 215)
                p.setBrush(glow)
                radius = 5.0 if hovered else 4.0
                p.drawEllipse(QPointF(hx, hy), radius, radius)
                self._hover_hits.append((key, hx, hy, entry))
            if len(apps) > self.MAX_DOTS:
                rect = self._cell_rect(row, col)
                p.setPen(QColor(C_MUTED))
                p.drawText(
                    QRectF(rect.right() - 30, rect.bottom() - 16, 28, 14),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                    f"+{len(apps) - self.MAX_DOTS}",
                )


class SegmentedControl(QWidget):

    def __init__(self, options, on_change, parent=None):
        super().__init__(parent)
        self.setObjectName("segGroup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for i, (label, key) in enumerate(options):
            btn = QPushButton(label)
            btn.setObjectName("segBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if i == 0:
                btn.setChecked(True)
            self.group.addButton(btn)
            row.addWidget(btn)
            self.buttons[btn] = key
        self.group.buttonClicked.connect(lambda b: on_change(self.buttons[b]))

    def current(self) -> str:
        for btn, key in self.buttons.items():
            if btn.isChecked():
                return key
        return RANGE_TODAY


class UsagePage(QWidget):

    def __init__(self, backend, history: UsageHistory):
        super().__init__()
        self.setObjectName("usagePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.b = backend
        self.h = history
        self._mode = RANGE_TODAY
        self._icons = QFileIconProvider()
        self._description = self._build_description()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.seg = SegmentedControl(RANGE_OPTIONS, self._on_range_changed)
        self.btn_refresh, self.btn_info = add_action_bar(
            top, self._on_refresh, self, "Usage History", self._description, extra_widgets=[self.seg]
        )
        root.addLayout(top)

        self.matrix_panel = QWidget()
        self.matrix_panel.setObjectName("matrixPanel")
        self.matrix_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        mp = QVBoxLayout(self.matrix_panel)
        mp.setContentsMargins(4, 4, 4, 4)
        self.matrix = ActivityMatrixView(self._icons)
        mp.addWidget(self.matrix)
        self._fx = QGraphicsOpacityEffect(self.matrix_panel)
        self.matrix_panel.setGraphicsEffect(self._fx)
        self._fx.setOpacity(1.0)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        root.addWidget(self.matrix_panel, 1)

        self.reload()

    def _build_description(self) -> str:
        return (
            "This page shows when your apps used things like the Camera, Microphone, or Location "
            "over time.\n\n"
            "Pick Today, Past 7 Days, or Past 30 Days at the top to change the time range.\n\n"
            "Each dot on the grid means: this app used this permission at this time. Hover over a "
            "dot to see which app it was, exactly when it happened, and how many times.\n\n"
            "The Network column is always empty — Windows doesn't let us track that one.\n\n"
            "Only the last 30 days are kept; older entries are removed automatically. This history "
            "is saved only on your own PC, in a small file at:\n"
            + os.path.join(history_dir(), "usage_history.json")
        )

    def _on_range_changed(self, mode: str):
        if mode == self._mode:
            return
        self._mode = mode
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.25)
        self._anim.finished.connect(self._finish_range_change)
        self._anim.start()

    def _finish_range_change(self):
        self._anim.finished.disconnect(self._finish_range_change)
        self.reload()
        self._anim.setStartValue(0.25)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _on_refresh(self):
        try:
            self.h.record(self.b.scan(False))
        except Exception:
            pass
        self.reload()

    def _build_matrix(self, mode: str):
        rows = build_time_rows(mode)
        row_labels = [label for label, _s, _e in rows]
        today_date = datetime.now().astimezone().date()
        cells: dict = {}
        totals: dict = {}
        for ft, cap, path in self.h.events:
            if not (0 <= cap < HIST_RECORDED):
                continue
            try:
                dt = ft_to_local(ft)
            except (OverflowError, OSError, ValueError):
                continue
            ridx = _row_index_for(mode, dt, today_date)
            if ridx is None:
                continue
            name = ntpath.basename(path) or path
            key = (ridx, cap)
            bucket = cells.setdefault(key, {})
            entry = bucket.get(name)
            if entry is None or dt > entry["dt"]:
                bucket[name] = {
                    "name": name,
                    "path": path,
                    "cap": cap,
                    "dt": dt,
                    "count": 1,
                    "icon": self._icons.icon(QFileInfo(path)),
                }
            else:
                entry["count"] += 1
            totals[cap] = totals.get(cap, 0) + 1
        out_cells = {key: list(bucket.values()) for key, bucket in cells.items()}
        return row_labels, out_cells, totals

    def reload(self):
        row_labels, cells, _totals = self._build_matrix(self._mode)
        self.matrix.set_data(row_labels, cells)


class DotCell(QWidget):

    def __init__(self, active: bool = False, parent=None):
        super().__init__(parent)
        self._active = active
        self.setMinimumSize(24, 20)

    def set_active(self, active: bool):
        if active != self._active:
            self._active = active
            self.update()

    def is_active(self) -> bool:
        return self._active

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        if self._active:
            halo = QColor("#F14C4C")
            halo.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPointF(cx, cy), 9, 9)
            p.setBrush(QColor("#F14C4C"))
            p.drawEllipse(QPointF(cx, cy), 5, 5)
        else:
            p.setPen(QColor("#4A4A4A"))
            p.drawLine(QPointF(cx - 4, cy), QPointF(cx + 4, cy))


class LivePage(QWidget):

    LIVE_LABELS = CAP_LABELS + ["Clipboard", "Audio"]
    COLUMNS = ["App"] + LIVE_LABELS
    COL_W = 104
    COL_APP_MIN = 260

    def __init__(self, backend, history: "UsageHistory | None" = None):
        super().__init__()
        self.setObjectName("livePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.b = backend
        self.h = history
        self._rows: list[str] = []
        self._icons = QFileIconProvider()
        self._description = self._build_description()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.btn_refresh, self.btn_info = add_action_bar(
            top, self._on_refresh_clicked, self, "Live Activity", self._description
        )
        root.addLayout(top)

        self.status = QLabel()
        self.status.setObjectName("mode")
        self.status.setStyleSheet(f"color: {C_ERR};")
        self.status.setVisible(False)
        root.addWidget(self.status)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setIconSize(QSize(18, 18))
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, self.COL_APP_MIN)
        for c in range(1, len(self.COLUMNS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, self.COL_W)
            self.table.horizontalHeaderItem(c).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        for cap in range(CAP_COUNT):
            self.table.horizontalHeaderItem(1 + cap).setToolTip(CAP_TIPS[cap])
        self.table.horizontalHeaderItem(1 + CAP_COUNT).setToolTip(CLIP_TIP)
        self.table.horizontalHeaderItem(2 + CAP_COUNT).setToolTip(
            "Audio: the app has an active audio session right now."
        )
        root.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_tick)
        self.timer.start(LIVE_MS)
        self.tick(force=True)
        QTimer.singleShot(0, self._fit_columns)

    def _build_description(self) -> str:
        return (
            "This page shows what every running app is doing right now, live.\n\n"
            f"A red dot lights up next to a tool the moment an app starts using it, and turns off "
            f"the moment it stops. It refreshes every {LIVE_MS // 1000} second(s) on its own.\n\n"
            "Next to each tool's name in the column header you'll also see a number in brackets, "
            "like Camera[1] — that's how many apps are using that tool right now.\n\n"
            "This page only watches; it doesn't change any settings. To allow or block a permission, "
            "go to the \"Apps and their permissions\" page instead."
        )

    def _on_refresh_clicked(self):
        self.tick(force=True)

    def _fit_columns(self):
        fixed = self.COL_W * (len(self.COLUMNS) - 1)
        self.table.setColumnWidth(0, max(self.COL_APP_MIN, self.table.viewport().width() - fixed))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_columns)

    def _auto_tick(self):
        win = self.window()
        if not self.isVisible() or (win is not None and win.isMinimized()):
            return
        self.tick()

    @staticmethod
    def _flags(app: AppInfo):
        return list(app.in_use) + [1 if app.clip else 0, 1 if app.audio != -2 else 0]

    def tick(self, force: bool = False):
        try:
            apps = self.b.scan(True)
        except Exception as e:
            self.status.setText(f"Scan failed: {e}")
            self.status.setVisible(True)
            return
        self.status.setVisible(False)
        if self.h is not None:
            self.h.record(apps)

        live = [(a, self._flags(a)) for a in apps]
        live.sort(key=lambda af: (-sum(af[1]), af[0].name.lower()))

        paths = [a.path.lower() for a, _f in live]
        if force or paths != self._rows:
            self._rebuild(live)
        else:
            for row, (_a, flags) in enumerate(live):
                for col, value in enumerate(flags, start=1):
                    cell = self.table.cellWidget(row, col)
                    if isinstance(cell, DotCell):
                        cell.set_active(bool(value))
        self._update_header_counts(live)

    def _rebuild(self, live):
        scroll = self.table.verticalScrollBar().value()
        self._rows = [a.path.lower() for a, _f in live]
        self.table.setRowCount(0)
        self.table.setRowCount(len(live))
        for row, (app, flags) in enumerate(live):
            item = QTableWidgetItem(self._icons.icon(QFileInfo(app.path)), app.name)
            item.setToolTip(app.path)
            self.table.setItem(row, 0, item)
            for col, value in enumerate(flags, start=1):
                dot = DotCell(bool(value))
                label = self.LIVE_LABELS[col - 1]
                dot.setToolTip(
                    f"{app.name} is using {label} right now." if value
                    else f"{app.name} is not using {label}."
                )
                self.table.setCellWidget(row, col, dot)
        self.table.verticalScrollBar().setValue(scroll)

    def _update_header_counts(self, live):
        counts = [0] * len(self.LIVE_LABELS)
        for _a, flags in live:
            for i, value in enumerate(flags):
                counts[i] += 1 if value else 0
        for i, label in enumerate(self.LIVE_LABELS):
            item = self.table.horizontalHeaderItem(1 + i)
            item.setText(f"{label}[{counts[i]}]")


STYLE = f"""
#root {{ background: {C_TITLEBAR}; }}

#titleBar {{ background: {C_TITLEBAR}; }}
#titleText {{ color: {C_TEXT}; font-size: 12px; background: transparent; padding-left: 2px; }}

#menuBar {{ background: #2D2D2D; border-top: 1px solid #1A1A1A; border-bottom: 1px solid #1A1A1A; }}
#menuBar QPushButton#menuBtn {{
    background: transparent; color: {C_TEXT}; border: none;
    border-bottom: 2px solid transparent; padding: 0 14px; font-size: 13px;
}}
#menuBar QPushButton#menuBtn:hover {{ background: {C_HOVER}; }}
#menuBar QPushButton#menuBtn:checked {{ color: #FFFFFF; border-bottom: 2px solid {C_ACCENT}; }}

#stack {{ background: {C_BODY}; }}
#pageText {{ background: {C_BODY}; color: #FFFFFF; font-size: 30px; }}
#errorText {{ background: {C_BODY}; color: {C_ERR}; font-size: 15px; padding: 24px; }}

#appsPage, #usagePage, #livePage {{ background: {C_BODY}; }}
#appsPage QTableWidget QHeaderView ToggleSwitch {{ background: transparent; }}
#usagePage QLabel, #livePage QLabel {{ color: {C_TEXT}; font-size: 12px; }}
#usagePage QLabel#hint, #livePage QLabel#hint {{ color: {C_MUTED}; font-size: 11px; }}
#usagePage QLabel#mode, #livePage QLabel#mode {{ font-size: 11px; }}
#matrixPanel {{ background: #202020; border: 1px solid #2B2B2B; border-radius: 3px; }}
#segGroup {{ background: #252526; border: 1px solid #3C3C3C; border-radius: 3px; }}
QPushButton#segBtn {{
    background: transparent; color: {C_MUTED}; border: none; padding: 5px 12px;
    border-radius: 2px; font-size: 12px;
}}
QPushButton#segBtn:hover {{ background: {C_HOVER}; color: {C_TEXT}; }}
QPushButton#segBtn:checked {{ background: {C_ACCENT}; color: #FFFFFF; font-weight: 600; }}
#appsPage QLabel {{ color: {C_TEXT}; font-size: 12px; }}
#appsPage QLabel#status {{ color: {C_TEXT}; font-size: 12px; }}

QPushButton#toolBtn {{
    background: #0E639C; color: #FFFFFF; border: none; padding: 6px 16px; border-radius: 2px;
}}
QPushButton#toolBtn:hover {{ background: #1177BB; }}
QPushButton#infoBtn {{
    background: transparent; color: {C_TEXT}; border: 1px solid #454545;
    padding: 5px 11px; border-radius: 2px; font-weight: 600; font-size: 13px;
}}
QPushButton#infoBtn:hover {{ background: {C_HOVER}; }}

QMessageBox {{ background: #252526; }}
QMessageBox QLabel {{ color: {C_TEXT}; }}
QMessageBox QPushButton {{
    background: #0E639C; color: #FFFFFF; border: none; padding: 5px 16px; border-radius: 2px;
}}
QMessageBox QPushButton:hover {{ background: #1177BB; }}

QTableWidget {{
    background: {C_BODY}; alternate-background-color: #252526; color: {C_TEXT};
    border: 1px solid #2B2B2B; font-size: 13px;
}}
QTableWidget::item {{ padding-left: 8px; }}
QHeaderView::section {{
    background: #2A2A2B; color: #E0E0E0; padding: 9px 6px 0 6px; border: none;
    border-bottom: 1px solid #3C3C3C; font-weight: 600;
}}
QHeaderView::section:first {{ padding-left: 8px; }}
QTableCornerButton::section {{ background: #2A2A2B; border: none; }}
QHeaderView {{ background: #2A2A2B; border: none; }}
QScrollBar:vertical {{ background: {C_BODY}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #424242; min-height: 30px; border-radius: 3px; }}
QScrollBar::handle:vertical:hover {{ background: #555555; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {C_BODY}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #424242; min-width: 30px; border-radius: 3px; }}
QScrollBar::handle:horizontal:hover {{ background: #555555; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: #252526; color: {C_TEXT}; border: 1px solid #454545; padding: 4px; }}
QMenu {{
    background: #252526; color: {C_TEXT}; border: 1px solid #454545; padding: 4px;
    font-size: 12px;
}}
QMenu::item {{ padding: 5px 24px 5px 22px; }}
QMenu::item:selected {{ background: {C_ACCENT}; color: #FFFFFF; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
"""


class MainWindow(QWidget):
    def __init__(self, backend, error):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(760, 420)
        self.resize(1040, 640)

        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(0)
        self._layout.setContentsMargins(*(RESIZE_MARGIN,) * 4)

        self.titlebar = TitleBar(self)
        self.titlebar.setCursor(Qt.CursorShape.ArrowCursor)
        self._layout.addWidget(self.titlebar)

        self.menubar = MenuBar(self)
        self._layout.addWidget(self.menubar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        self.stack.setCursor(Qt.CursorShape.ArrowCursor)

        self.b = backend
        self.history = UsageHistory()

        if backend is not None:
            self.page1 = AppsPage(backend, self.history)
        else:
            self.page1 = QLabel(error or "Unknown error")
            self.page1.setObjectName("errorText")
            self.page1.setWordWrap(True)
            self.page1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page1)

        if backend is not None:
            self.page2 = UsagePage(backend, self.history)
        else:
            self.page2 = QLabel(error or "Unknown error")
            self.page2.setObjectName("errorText")
            self.page2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page2)

        if backend is not None:
            self.page3 = LivePage(backend, self.history)
        else:
            self.page3 = QLabel(error or "Unknown error")
            self.page3.setObjectName("errorText")
            self.page3.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page3)

        self._layout.addWidget(self.stack, 1)

        self.menubar.group.idClicked.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._on_page_changed)
        self.setStyleSheet(STYLE)

        if backend is not None:
            self._sampler = QTimer(self)
            self._sampler.timeout.connect(self._sample_usage)
            self._sampler.start(SAMPLE_MS)

    def _on_page_changed(self, index: int):
        if index == 1 and isinstance(self.page2, UsagePage):
            self.page2.reload()
        elif index == 2 and isinstance(self.page3, LivePage):
            self.page3.tick(force=True)

    def _sample_usage(self):
        if self.isMinimized() or self.stack.currentIndex() in (0, 2):
            return
        try:
            self.history.record(self.b.scan(False))
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self.history.save()
        except Exception:
            pass
        super().closeEvent(event)

    def toggle_max_restore(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "titlebar"):
            maximized = self.isMaximized()
            m = 0 if maximized else RESIZE_MARGIN
            self._layout.setContentsMargins(m, m, m, m)
            self.titlebar.btn_max.set_kind("restore" if maximized else "max")
        super().changeEvent(event)

    def _edges_at(self, pos):
        m = RESIZE_MARGIN
        edges = Qt.Edge(0)
        if pos.x() < m:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= self.width() - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() < m:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= self.height() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _update_cursor(self, edges):
        l = bool(edges & Qt.Edge.LeftEdge)
        r = bool(edges & Qt.Edge.RightEdge)
        t = bool(edges & Qt.Edge.TopEdge)
        b = bool(edges & Qt.Edge.BottomEdge)
        if (l and t) or (r and b):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif (r and t) or (l and b):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif l or r:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif t or b:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self.isMaximized():
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self._update_cursor(self._edges_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edges = self._edges_at(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)


def main():
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AccessMonitor.App")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI", 9))

    backend, error = create_backend()
    window = MainWindow(backend, error)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
