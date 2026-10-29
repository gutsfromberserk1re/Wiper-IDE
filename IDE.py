import os
import sys
import ctypes
import tempfile
import ast
import re
import keyword
import builtins
import math
import time
import codecs
import warnings
from pathlib import Path

from PySide6.QtCore import (
    Qt, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QDir, QProcess, QProcessEnvironment,
    QTimer, Signal, QVariantAnimation, QEasingCurve, QAbstractAnimation
)
from PySide6.QtGui import (
    QPainter, QPainterPath, QColor, QPen, QFont, QFontDatabase, QKeySequence, QAction,
    QIcon, QCursor, QTextCursor, QTextFormat, QPolygonF, QSyntaxHighlighter, QTextCharFormat, QShortcut
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPlainTextEdit, QTextEdit, QLabel, QPushButton, QAbstractButton, QMenu,
    QFileDialog, QTabBar, QStackedWidget, QDialog, QTreeView, QFileSystemModel,
    QTabWidget, QFrame, QLineEdit, QListWidget, QListWidgetItem
)

IS_WIN = sys.platform == "win32"

# Works from source AND from a Nuitka build (as long as data files are included)
APP_DIR = Path(__file__).resolve().parent


def find_icon():
    """Look for the app icon next to the script / exe / working dir.
    Tries snake.ico first, then any .ico file."""
    dirs = []
    for d in (APP_DIR, Path(sys.argv[0]).resolve().parent, Path.cwd()):
        if d not in dirs:
            dirs.append(d)
    for d in dirs:
        for name in ("snake.ico", "snake.ico.ico", "icon.ico"):
            if (d / name).is_file():
                return d / name
    for d in dirs:
        found = sorted(d.glob("*.ico"))
        if found:
            return found[0]
    return None


ICON_PATH = find_icon()
FONT_DIR = APP_DIR / "fonts"

APP_NAME = "Wiper IDE"
TITLEBAR_HEIGHT = 38
WINDOW_RADIUS = 8
RESIZE_BORDER = 6

# ---------------------------------------------------------------------------
# LOOK SETTINGS - tweak these
# ---------------------------------------------------------------------------
GLASS_MODE = "acrylic"            # "acrylic" (blur + tint)  or  "blur" (plain blur-behind)
TINT_ALPHA = 45                  # 0-255, dark tint over the blur. Lower = more see-through
PLATINUM_SILVER = QColor("#E5E9F0")
PLATINUM_BORDER = QColor("#CBD5E1")
WORKSPACE_TINT = QColor(15, 23, 42, TINT_ALPHA)
CURSOR_COLOR = "#E5E9F0"      # cursor core (silver / platinum)
GLOW_COLOR = "#B8C2D0"        # soft silver halo around it
CURSOR_WIDTH = 4                  # thickness in px (legacy-CLI chunky)
SIDEBAR_WIDTH = 270               # default Explorer width
SIDEBAR_MIN_WIDTH = 180           # narrowest the open sidebar can be dragged
SIDEBAR_MAX_WIDTH = 480
SIDEBAR_ANIM_MS = 220             # open/close duration
CODE_FONT = "Consolas"            # replaced at startup by JetBrains Mono if found

# ---------------------------------------------------------------------------
# EDITOR BEHAVIOUR
# ---------------------------------------------------------------------------
PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
CLOSERS = set(")]}\"'")
STRING_PREFIX_RE = re.compile(r"(?<!\w)[rRbBfFuU]{1,2}$")   # f"..", rb'..' etc.
GHOST_MIN_CHARS = 2               # faded suggestion appears after this many typed characters
GHOST_COLOR = QColor(171, 178, 191, 85)   # faded suggestion text (r, g, b, alpha)
GHOST_TAIL_CHARS = set(")]}'\",: ")      # text after the cursor that may be shifted aside

# Used for "Run" on unsaved buffers: makes __file__, sys.path, argv and tracebacks
# behave as if the real file was run.
BOOTSTRAP = '''import sys, os, linecache
src_file, real = sys.argv[1], sys.argv[2]
with open(src_file, encoding="utf-8") as f:
    source = f.read()
sys.argv = [real] + sys.argv[3:]
sys.path[0] = os.path.dirname(real)
linecache.cache[real] = (len(source), None, source.splitlines(True), real)
g = {"__name__": "__main__", "__file__": real, "__builtins__": __builtins__}
exec(compile(source, real, "exec"), g)
'''


def load_fonts() -> str:
    """Load fonts/*.ttf next to the script and pick JetBrains Mono if available."""
    if FONT_DIR.exists():
        for f in list(FONT_DIR.glob("*.ttf")) + list(FONT_DIR.glob("*.otf")):
            QFontDatabase.addApplicationFont(str(f))
    families = set(QFontDatabase.families())
    for name in ("JetBrains Mono", "JetBrainsMono NF", "JetBrainsMono Nerd Font",
                 "Cascadia Code", "Consolas"):
        if name in families:
            return name
    return "Consolas"


def find_python():
    """Python used to run scripts. In a Nuitka build sys.executable is the IDE
    itself, so look for a real Python on PATH instead."""
    if "__compiled__" not in globals():
        return sys.executable
    import shutil
    return shutil.which("python") or shutil.which("py")


# ---------------------------------------------------------------------------
# Windows glass + native hit-testing
# ---------------------------------------------------------------------------
if IS_WIN:
    from ctypes import wintypes

    WM_NCHITTEST = 0x0084
    HTCLIENT = 1
    HTCAPTION = 2
    HTLEFT = 10
    HTRIGHT = 11
    HTTOP = 12
    HTTOPLEFT = 13
    HTTOPRIGHT = 14
    HTBOTTOM = 15
    HTBOTTOMLEFT = 16
    HTBOTTOMRIGHT = 17

    class ACCENT_POLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class WINCOMPATTRDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                    ("SizeOfData", ctypes.c_size_t)]

    def enable_glass(hwnd: int) -> bool:
        # Rounded corners on Win11 (ignored elsewhere)
        try:
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            corner = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        except Exception:
            pass

        try:
            accent = ACCENT_POLICY()
            if GLASS_MODE == "blur":
                accent.AccentState = 3          # ENABLE_BLURBEHIND
                accent.AccentFlags = 0
                accent.GradientColor = 0
            else:
                accent.AccentState = 4          # ENABLE_ACRYLICBLURBEHIND
                accent.AccentFlags = 2
                accent.GradientColor = 0x182A170F   # AABBGGRR  (dark navy, ~10% alpha)
            data = WINCOMPATTRDATA()
            data.Attribute = 19                 # WCA_ACCENT_POLICY
            data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
            data.SizeOfData = ctypes.sizeof(accent)
            fn = ctypes.windll.user32.SetWindowCompositionAttribute
            fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
            fn.restype = wintypes.BOOL
            return bool(fn(hwnd, ctypes.byref(data)))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Syntax highlighter
# ---------------------------------------------------------------------------
def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    f.setFontItalic(italic)
    return f


class PythonHighlighter(QSyntaxHighlighter):
    BUILTINS = (
        "print len range int str float list dict set tuple bool bytes open input type "
        "isinstance enumerate zip map filter sorted sum min max abs any all super "
        "reversed round repr id iter next object Exception ValueError TypeError"
    ).split()

    def __init__(self, document):
        super().__init__(document)
        self.f_string = _fmt("#98C379")
        self.f_comment = _fmt("#5C6370", italic=True)
        kw = "|".join(k for k in keyword.kwlist if k not in ("True", "False", "None"))

        # Code rules only. Strings and comments are found by the scanner below,
        # which is applied last so a '#' inside a string stays a string.
        self.rules = [
            (r"\b[A-Za-z_]\w*(?=\()",                   _fmt("#61AFEF"), 0),
            (rf"\b(?:{kw})\b",                          _fmt("#C678DD", bold=True), 0),
            (r"\b(?:True|False|None)\b",                _fmt("#D19A66", bold=True), 0),
            (r"\b(?:" + "|".join(self.BUILTINS) + r")\b", _fmt("#56B6C2"), 0),
            (r"\b(?:self|cls)\b",                       _fmt("#E06C75", italic=True), 0),
            (r"\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?)\b",
                                                        _fmt("#D19A66"), 0),
            (r"@\w+(?:\.\w+)*",                         _fmt("#E5C07B"), 0),
            (r"\bdef\s+(\w+)",                          _fmt("#61AFEF", bold=True), 1),
            (r"\bclass\s+(\w+)",                        _fmt("#E5C07B", bold=True), 1),
            (r"(?:==|!=|<=|>=|:=|->|\*\*|//|<<|>>|[+\-*/%@&|^~<>]=?)",
                                                        _fmt("#56B6C2"), 0),
            (r"[()\[\]{}]",                              _fmt("#ABB2BF"), 0),
            (r"\b__[A-Za-z_]\w*__\b",                   _fmt("#E5C07B"), 0),
        ]
        self.rules = [(re.compile(p), f, g) for p, f, g in self.rules]

    def highlightBlock(self, text: str):
        for pattern, fmt, group in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(group), m.end(group) - m.start(group), fmt)

        # Small scanner: strings, triple-quoted strings (state 1 = ''' , 2 = """), comments.
        # State is carried across lines through the block state.
        delims = {1: "'''", 2: '"""'}
        state = self.previousBlockState()
        if state not in (1, 2):
            state = 0
        n, i = len(text), 0
        while i < n:
            if state:
                end = text.find(delims[state], i)
                if end == -1:
                    self.setFormat(i, n - i, self.f_string)
                    self.setCurrentBlockState(state)
                    return
                self.setFormat(i, end + 3 - i, self.f_string)
                i, state = end + 3, 0
                continue
            ch = text[i]
            if ch == "#":
                self.setFormat(i, n - i, self.f_comment)
                break
            if ch in "\"'":
                if text.startswith(ch * 3, i):
                    state = 1 if ch == "'" else 2
                    end = text.find(ch * 3, i + 3)
                    if end == -1:
                        self.setFormat(i, n - i, self.f_string)
                        self.setCurrentBlockState(state)
                        return
                    self.setFormat(i, end + 3 - i, self.f_string)
                    i, state = end + 3, 0
                    continue
                j = i + 1
                while j < n and text[j] != ch:
                    if text[j] == "\\":
                        j += 1
                    j += 1
                j = min(j + 1, n)
                self.setFormat(i, j - i, self.f_string)
                i = j
                continue
            i += 1
        self.setCurrentBlockState(0)


# ---------------------------------------------------------------------------
# Smooth "smear / trail" cursor (Neovide / rice style)
# ---------------------------------------------------------------------------
class SmearCursor(QWidget):
    SLOW = 0.16     # trailing corners follow slowly...
    FAST = 0.60     # ...leading corners snap ahead -> visible trail
    PAD = 16        # extra repaint margin for the glow
    BREATH_PERIOD = 3.6   # seconds for one full breath (bigger = slower)
    BREATH_MIN = 0.15     # dimmest point (0 = fully vanishes)

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.cur = [QPointF() for _ in range(4)]
        self.tgt = [QPointF() for _ in range(4)]
        self.speed = [self.FAST] * 4
        self.ready = False
        self._t0 = time.perf_counter()      # breathing restarts from full brightness
        self._last = time.perf_counter()
        self._last_rect = QRect()

        self.anim = QTimer(self)
        self.anim.setTimerType(Qt.TimerType.PreciseTimer)
        self.anim.setInterval(8)
        self.anim.timeout.connect(self._tick)

        # only runs while the editor has focus (started/stopped from the editor)
        self.breathe = QTimer(self)
        self.breathe.setInterval(33)
        self.breathe.timeout.connect(self._breathe_tick)

    def start_breathing(self):
        self._t0 = time.perf_counter()
        if not self.breathe.isActive():
            self.breathe.start()

    def stop_breathing(self):
        self.breathe.stop()

    def _breathe_tick(self):
        if not self.anim.isActive() and self.editor.hasFocus():
            self.update(self._last_rect)

    def _opacity(self):
        """Slow sine 'breathing': full -> dim -> full, no hard blink."""
        if self.anim.isActive():
            return 1.0
        t = time.perf_counter() - self._t0
        wave = 0.5 + 0.5 * math.cos(2 * math.pi * t / self.BREATH_PERIOD)
        return self.BREATH_MIN + (1.0 - self.BREATH_MIN) * wave

    @staticmethod
    def _center(pts):
        return QPointF(sum(p.x() for p in pts) / 4, sum(p.y() for p in pts) / 4)

    @staticmethod
    def _hull(pts):
        """Convex hull of the 4 corners - stops the trail twisting into a bow-tie."""
        P = sorted((p.x(), p.y()) for p in pts)

        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        lower, upper = [], []
        for pt in P:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], pt) <= 0:
                lower.pop()
            lower.append(pt)
        for pt in reversed(P):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], pt) <= 0:
                upper.pop()
            upper.append(pt)
        hull = lower[:-1] + upper[:-1]
        if len(hull) < 3:
            hull = P
        return QPolygonF([QPointF(x, y) for x, y in hull])

    def _repaint(self):
        r = QPolygonF(self.cur).boundingRect().toAlignedRect().adjusted(
            -self.PAD, -self.PAD, self.PAD, self.PAD)
        self.update(r.united(self._last_rect))
        self._last_rect = r

    def retarget(self, animate=True):
        r = self.editor.cursorRect()
        # centre the bar on the caret boundary so it sits BETWEEN characters
        # (e.g. between the two quotes of "") instead of covering the next one
        x, y, h = max(r.left() - CURSOR_WIDTH // 2, 0), r.top(), r.height()
        new = [QPointF(x, y), QPointF(x + CURSOR_WIDTH, y),
               QPointF(x + CURSOR_WIDTH, y + h), QPointF(x, y + h)]
        self._t0 = time.perf_counter()

        if not animate or not self.ready:
            self.cur = [QPointF(p) for p in new]
            self.tgt = [QPointF(p) for p in new]
            self.ready = True
            self.anim.stop()
            self._repaint()
            return

        old_c, new_c = self._center(self.tgt), self._center(new)
        dx, dy = new_c.x() - old_c.x(), new_c.y() - old_c.y()
        dist = math.hypot(dx, dy)
        if dist > 0.5:
            dots = [((p.x() - new_c.x()) * dx + (p.y() - new_c.y()) * dy) / dist for p in new]
            lo, hi = min(dots), max(dots)
            for i, d in enumerate(dots):
                t = 0.5 if hi - lo < 1e-6 else (d - lo) / (hi - lo)
                self.speed[i] = self.SLOW + (self.FAST - self.SLOW) * t
        self.tgt = new
        if not self.anim.isActive():
            self._last = time.perf_counter()
            self.anim.start()

    def _tick(self):
        now = time.perf_counter()
        dt = min(max((now - self._last) * 1000.0, 2.0), 50.0)
        self._last = now
        done = True
        for i in range(4):
            k = 1.0 - (1.0 - self.speed[i]) ** (dt / 16.0)
            c, t = self.cur[i], self.tgt[i]
            c.setX(c.x() + (t.x() - c.x()) * k)
            c.setY(c.y() + (t.y() - c.y()) * k)
            if abs(t.x() - c.x()) > 0.3 or abs(t.y() - c.y()) > 0.3:
                done = False
        if done:
            self.cur = [QPointF(p) for p in self.tgt]
            self.anim.stop()
            self._t0 = time.perf_counter()
        self._repaint()

    def paintEvent(self, event):
        if not self.editor.hasFocus():
            return
        poly = self._hull(self.cur)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(self._opacity())

        # soft glow: a few wide, faint strokes under the solid core
        p.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((14, 16), (9, 30), (5, 58)):
            c = QColor(GLOW_COLOR)
            c.setAlpha(alpha)
            pen = QPen(c, width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolygon(poly)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(CURSOR_COLOR))
        p.drawPolygon(poly)


# ---------------------------------------------------------------------------
# Code editor
# ---------------------------------------------------------------------------
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    syntax_changed = Signal(bool, str)

    # words offered by autocomplete besides the ones found in the document
    STATIC_WORDS = (set(keyword.kwlist)
                    | {n for n in dir(builtins) if not n.startswith("_")}
                    | {"self", "cls", "__init__", "__name__", "__main__", "__file__"})

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = None
        self.uid = None
        self._gutter_w = 0
        self.error_line = -1
        self.error_col = 0
        self.last_status = (True, "")
        self._tree = None               # cached ast, shared by syntax check + outline
        self._tree_err = None
        self._tree_rev = -1

        font = QFont(CODE_FONT, 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setCursorWidth(0)               # hide native cursor, SmearCursor draws it
        self.setStyleSheet("""
            QPlainTextEdit {
                background: transparent;
                color: #ABB2BF;
                border: none;
                selection-background-color: rgba(125, 211, 252, 0.28);
            }
        """)

        self.highlighter = PythonHighlighter(self.document())
        self.gutter = LineNumberArea(self)
        self.cursor_fx = SmearCursor(self)
        self._retarget_timer = QTimer(self)
        self._retarget_timer.setSingleShot(True)
        self._retarget_timer.timeout.connect(self.cursor_fx.retarget)

        # ---- ghost suggestion (faded inline text, Tab / Right accepts)
        self._doc_words = set()
        self._words_rev = -1
        self._ghost_text = ""
        self._ghost_pos = -1
        self._ghost_prefix = None
        self._ghost_cands = []
        self._ghost_idx = 0

        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.cursor_fx.retarget(False))
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.cursor_fx.retarget(False))
        self._update_margin()
        self._refresh_selections()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._validate_syntax)
        self.textChanged.connect(self._timer.start)

    # ---- layout
    def line_number_width(self):
        digits = max(2, len(str(self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _layout_overlays(self):
        cr = self.contentsRect()
        gg = QRect(cr.left(), cr.top(), self._gutter_w, cr.height())
        if self.gutter.geometry() != gg:
            self.gutter.setGeometry(gg)
        vg = self.viewport().geometry()
        old = self.cursor_fx.geometry()
        if old != vg:
            self.cursor_fx.setGeometry(vg)
            # The caret is stored in viewport coordinates, so a pure size change
            # (e.g. the sidebar sliding open) needs no re-snap - only a moved origin does.
            if vg.topLeft() != old.topLeft() or not self.cursor_fx.ready:
                self.cursor_fx.raise_()
                self.cursor_fx.retarget(False)

    def _update_margin(self):
        w = self.line_number_width()
        if w != self._gutter_w:
            self._gutter_w = w
            self.setViewportMargins(w, 0, 0, 0)
        self._layout_overlays()

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
            self.cursor_fx.retarget(False)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlays()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.cursor_fx.start_breathing()
        self.cursor_fx.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.cursor_fx.stop_breathing()
        self._clear_ghost()
        self.cursor_fx.update()

    def _on_cursor_moved(self):
        if self._ghost_text and self.textCursor().position() != self._ghost_pos:
            self._ghost_text, self._ghost_prefix = "", None
        self._refresh_selections()
        self.viewport().update()
        self._retarget_timer.start(0)

    def paint_line_numbers(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), QColor(0, 0, 0, 30))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if n == self.error_line:
                    p.setPen(QColor("#E06C75"))
                elif n == current:
                    p.setPen(QColor("#E2E8F0"))
                else:
                    p.setPen(QColor("#64748B"))
                p.drawText(0, top, self.gutter.width() - 8, self.fontMetrics().height(),
                           Qt.AlignmentFlag.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1

    # ---- current line + error underline
    def _refresh_selections(self):
        sels = []
        cur_sel = QTextEdit.ExtraSelection()
        cur_sel.format.setBackground(QColor(255, 255, 255, 16))
        cur_sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cur_sel.cursor = self.textCursor()
        cur_sel.cursor.clearSelection()
        sels.append(cur_sel)

        if self._ghost_active():
            tail = self.textCursor()
            tail.clearSelection()
            tail.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            if tail.hasSelection():          # hidden here, redrawn after the ghost in paintEvent
                hide = QTextEdit.ExtraSelection()
                hide.format.setForeground(QColor(0, 0, 0, 0))
                hide.cursor = tail
                sels.append(hide)

        if self.error_line >= 0:
            block = self.document().findBlockByNumber(self.error_line)
            if block.isValid():
                err = QTextEdit.ExtraSelection()
                err.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
                err.format.setUnderlineColor(QColor("#E06C75"))
                err.format.setBackground(QColor(224, 108, 117, 35))
                c = QTextCursor(block)
                col = min(self.error_col, max(block.length() - 1, 0))
                c.setPosition(block.position() + col)
                c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                if not c.hasSelection():
                    c.setPosition(block.position())
                    c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                err.cursor = c
                sels.append(err)
        self.setExtraSelections(sels)
        self.gutter.update()

    def parse_tree(self):
        """(tree, error) for the current text. Parsed once per document revision, so
        the syntax check and the outline never parse the same text twice."""
        rev = self.document().revision()
        if rev != self._tree_rev:
            self._tree_rev = rev
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")      # e.g. invalid escape sequence warnings
                    self._tree, self._tree_err = ast.parse(self.toPlainText()), None
            except Exception as e:                       # SyntaxError, ValueError (NUL), RecursionError...
                self._tree, self._tree_err = None, e
        return self._tree, self._tree_err

    def _validate_syntax(self):
        _, err = self.parse_tree()
        if err is None:
            self.error_line, self.error_col = -1, 0
            self.last_status = (True, "No syntax errors")
        elif isinstance(err, SyntaxError):
            self.error_line = (err.lineno or 1) - 1
            self.error_col = max((err.offset or 1) - 1, 0)
            self.last_status = (False, f"Line {err.lineno or 1}: {err.msg}")
        else:
            self.error_line, self.error_col = -1, 0
            self.last_status = (False, str(err))
        self._refresh_selections()
        self.syntax_changed.emit(*self.last_status)

    # -----------------------------------------------------------------------
    # Typing helpers
    # -----------------------------------------------------------------------
    def keyPressEvent(self, e):
        key, mods = e.key(), e.modifiers()

        # Ctrl+Space = show a suggestion / cycle to the next one
        if key == Qt.Key.Key_Space and mods & Qt.KeyboardModifier.ControlModifier:
            self._update_ghost(cycle=True, min_chars=1)
            return

        if self._ghost_active():
            if key == Qt.Key.Key_Tab or (key == Qt.Key.Key_Right and mods == Qt.KeyboardModifier.NoModifier):
                self._accept_ghost()
                return
            if key == Qt.Key.Key_Escape:
                self._clear_ghost()
                return

        if key == Qt.Key.Key_Slash and mods & Qt.KeyboardModifier.ControlModifier:
            self._clear_ghost()
            self._toggle_comment()
            return

        if key == Qt.Key.Key_Home and not mods & Qt.KeyboardModifier.ControlModifier:
            self._clear_ghost()
            self._smart_home(bool(mods & Qt.KeyboardModifier.ShiftModifier))
            return

        if key == Qt.Key.Key_Backtab:
            self._indent_selection(unindent=True)
            return

        if key == Qt.Key.Key_Tab:
            cur = self.textCursor()
            if cur.hasSelection():
                self._indent_selection(unindent=False)
            else:
                cur.insertText(" " * (4 - cur.positionInBlock() % 4))   # align to next 4-col stop
            return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._smart_newline()
            return

        if key == Qt.Key.Key_Backspace and mods == Qt.KeyboardModifier.NoModifier:
            if self._smart_backspace():
                self._update_ghost()
                return

        if self._handle_pairing(e):
            self._clear_ghost()
            return

        super().keyPressEvent(e)
        text = e.text()
        if (text and (text.isalnum() or text == "_")) or key == Qt.Key.Key_Backspace:
            self._update_ghost()
        else:
            self._clear_ghost()

    # ---- Enter: keep indent, add one level after ':' or an opening bracket,
    #      and split "(|)" onto three lines. One undo step.
    def _smart_newline(self):
        cur = self.textCursor()
        cur.beginEditBlock()
        cur.removeSelectedText()
        line = cur.block().text()
        pos = cur.positionInBlock()
        before, after = line[:pos], line[pos:]
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        indent = indent[:pos]            # Enter inside the indentation must not duplicate it
        stripped = before.rstrip()
        last = stripped[-1:] if stripped else ""

        if last in "([{" and last and after[:1] == PAIRS.get(last):
            cur.insertText("\n" + indent + "    " + "\n" + indent)
            cur.setPosition(cur.position() - len(indent) - 1)    # back onto the middle line
        else:
            in_comment = stripped.lstrip().startswith("#")      # "# note:" is not a block opener
            extra = "    " if last and last in ":([{" and not in_comment else ""
            cur.insertText("\n" + indent + extra)
        cur.endEditBlock()
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    # ---- Backspace: remove up to 4 spaces of indentation, or an empty pair like ()
    def _smart_backspace(self) -> bool:
        cur = self.textCursor()
        if cur.hasSelection():
            return False
        line = cur.block().text()
        pos = cur.positionInBlock()
        before = line[:pos]

        if before.endswith(" ") and before.strip() == "":
            n = min(pos % 4 or 4, len(before) - len(before.rstrip(" ")))
            cur.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, n)
            cur.removeSelectedText()
            self.setTextCursor(cur)
            return True

        if 0 < pos < len(line) and PAIRS.get(line[pos - 1]) == line[pos]:
            cur.beginEditBlock()
            cur.deleteChar()
            cur.deletePreviousChar()
            cur.endEditBlock()
            self.setTextCursor(cur)
            return True
        return False

    # ---- Tab / Shift+Tab on a selection (or Shift+Tab on the current line)
    def _indent_selection(self, unindent: bool):
        cur = self.textCursor()
        doc = self.document()
        start, end = cur.selectionStart(), cur.selectionEnd()
        first, last = doc.findBlock(start), doc.findBlock(end)
        if end > start and last.position() == end and last.blockNumber() > first.blockNumber():
            last = last.previous()          # selection ended at the very start of a line

        cur.beginEditBlock()
        block = first
        while block.isValid():
            c = QTextCursor(block)
            text = block.text()
            if unindent:
                if text.startswith("\t"):
                    n = 1
                else:
                    n = min(4, len(text) - len(text.lstrip(" ")))
                if n:
                    c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, n)
                    c.removeSelectedText()
            elif text.strip():
                c.insertText("    ")
            if block.blockNumber() >= last.blockNumber():
                break
            block = block.next()
        cur.endEditBlock()

    # ---- Home: first non-blank character, then column 0 (press again)
    def _smart_home(self, select: bool):
        cur = self.textCursor()
        block = cur.block()
        line = block.text()
        first = block.position() + len(line) - len(line.lstrip(" \t"))
        target = first if cur.position() != first else block.position()
        mode = QTextCursor.MoveMode.KeepAnchor if select else QTextCursor.MoveMode.MoveAnchor
        cur.setPosition(target, mode)
        self.setTextCursor(cur)

    # ---- Ctrl+/ : comment / uncomment the selected lines (one undo step)
    def _toggle_comment(self):
        cur = self.textCursor()
        doc = self.document()
        start, end = cur.selectionStart(), cur.selectionEnd()
        first, last = doc.findBlock(start), doc.findBlock(end)
        if end > start and last.position() == end and last.blockNumber() > first.blockNumber():
            last = last.previous()
        blocks, block = [], first
        while block.isValid():
            if block.text().strip():
                blocks.append(block)
            if block.blockNumber() >= last.blockNumber():
                break
            block = block.next()
        if not blocks:
            return
        uncomment = all(b.text().lstrip().startswith("#") for b in blocks)
        col = min(len(b.text()) - len(b.text().lstrip(" \t")) for b in blocks)
        cur.beginEditBlock()
        for b in blocks:
            text = b.text()
            c = QTextCursor(b)
            if uncomment:
                i = len(text) - len(text.lstrip(" \t"))
                n = 2 if text[i:i + 2] == "# " else 1
                c.setPosition(b.position() + i)
                c.setPosition(b.position() + i + n, QTextCursor.MoveMode.KeepAnchor)
                c.removeSelectedText()
            else:
                c.setPosition(b.position() + col)
                c.insertText("# ")
        cur.endEditBlock()

    # ---- auto-close brackets and quotes, with type-over and selection wrapping
    def _handle_pairing(self, e) -> bool:
        ch = e.text()
        if len(ch) != 1 or ch not in PAIRS and ch not in CLOSERS:
            return False
        mods = e.modifiers()
        if (mods & Qt.KeyboardModifier.ControlModifier) and not (mods & Qt.KeyboardModifier.AltModifier):
            return False

        cur = self.textCursor()

        # wrap a selection:  abc  +  "   ->   "abc"
        if cur.hasSelection():
            if ch not in PAIRS:
                return False
            start, end = cur.selectionStart(), cur.selectionEnd()
            cur.beginEditBlock()
            cur.setPosition(end)
            cur.insertText(PAIRS[ch])
            cur.setPosition(start)
            cur.insertText(ch)
            cur.setPosition(start + 1)
            cur.setPosition(end + 1, QTextCursor.MoveMode.KeepAnchor)
            cur.endEditBlock()
            self.setTextCursor(cur)
            return True

        line = cur.block().text()
        pos = cur.positionInBlock()
        prv = line[pos - 1] if pos > 0 else ""
        nxt = line[pos] if pos < len(line) else ""

        # typing the closing char right before an identical one just steps over it
        if ch in CLOSERS and nxt == ch:
            cur.movePosition(QTextCursor.MoveOperation.Right)
            self.setTextCursor(cur)
            self.cursor_fx.retarget(False)          # one-char hop: no smear across the quotes
            return True

        if ch in "([{":
            if nxt and not (nxt.isspace() or nxt in ")]}"):
                return False
        elif ch in "\"'":
            if prv == ch:                                   # third quote of """ / '''
                return False
            if (prv.isalnum() or prv == "_") and not STRING_PREFIX_RE.search(line[:pos]):
                return False                                # apostrophe in don't, it's ...
            if nxt and not (nxt.isspace() or nxt in ")]},:;"):
                return False
            if self._in_string_or_comment(cur):             # x = "abc|  ->  closes it, no stray ""
                return False
        else:
            return False                                    # a lone closer: plain insert

        start = cur.position()
        cur.beginEditBlock()
        cur.insertText(ch + PAIRS[ch])
        cur.setPosition(start + 1)                          # caret goes BETWEEN the pair
        cur.endEditBlock()
        self.setTextCursor(cur)
        self.cursor_fx.retarget(False)                      # draw it there at once, no smear over the pair
        return True

    # -----------------------------------------------------------------------
    # Ghost suggestion (keywords + builtins + words already in the file)
    # -----------------------------------------------------------------------
    def _word_prefix(self) -> str:
        cur = self.textCursor()
        before = cur.block().text()[:cur.positionInBlock()]
        m = re.search(r"[A-Za-z_]\w*$", before)
        return m.group(0) if m else ""

    def _ghost_active(self) -> bool:
        if not self._ghost_text or not self.hasFocus():
            return False
        cur = self.textCursor()
        return not cur.hasSelection() and cur.position() == self._ghost_pos

    def _clear_ghost(self):
        had = bool(self._ghost_text)
        self._ghost_text, self._ghost_prefix = "", None
        if had:
            self._refresh_selections()
            self.viewport().update()

    def _in_string_or_comment(self, cur) -> bool:
        col = cur.positionInBlock()
        if col == 0:
            return False
        skip = {self.highlighter.f_string.foreground().color().name(),
                self.highlighter.f_comment.foreground().color().name()}
        try:
            for fr in cur.block().layout().formats():
                if fr.start <= col - 1 < fr.start + fr.length \
                        and fr.format.foreground().color().name() in skip:
                    return True
        except Exception:
            pass
        return False

    def _candidates(self, prefix: str):
        rev = self.document().revision()
        if rev != self._words_rev:
            self._words_rev = rev
            self._doc_words = set(re.findall(r"[A-Za-z_]\w{2,}", self.toPlainText()))
        doc = {w for w in self._doc_words if w.startswith(prefix) and w != prefix}
        static = {w for w in self.STATIC_WORDS if w.startswith(prefix) and w != prefix} - doc
        key = lambda w: (len(w), w.lower())
        return sorted(doc, key=key) + sorted(static, key=key)    # your own names first

    def _update_ghost(self, cycle: bool = False, min_chars: int = GHOST_MIN_CHARS):
        cur = self.textCursor()
        prefix = self._word_prefix()
        line, pos = cur.block().text(), cur.positionInBlock()
        tail = line[pos:]
        if (cur.hasSelection() or len(prefix) < min_chars or len(tail) > 24
                or not set(tail) <= GHOST_TAIL_CHARS or self._in_string_or_comment(cur)):
            self._clear_ghost()
            return

        if prefix != self._ghost_prefix:
            self._ghost_cands = self._candidates(prefix)
            self._ghost_idx, self._ghost_prefix = 0, prefix
        elif cycle and self._ghost_cands:
            self._ghost_idx = (self._ghost_idx + 1) % len(self._ghost_cands)
        if not self._ghost_cands:
            self._clear_ghost()
            return

        self._ghost_text = self._ghost_cands[self._ghost_idx][len(prefix):]
        self._ghost_pos = cur.position()
        self._refresh_selections()
        self.viewport().update()

    def _accept_ghost(self):
        text = self._ghost_text
        self._clear_ghost()
        cur = self.textCursor()
        cur.insertText(text)
        self.setTextCursor(cur)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._ghost_active():
            return
        r = self.cursorRect()
        fm = self.fontMetrics()
        cur = self.textCursor()
        tail = cur.block().text()[cur.positionInBlock():]
        x = r.left() + 2
        y = r.top() + fm.ascent()
        p = QPainter(self.viewport())
        p.setFont(self.font())
        p.setPen(GHOST_COLOR)
        p.drawText(x, y, self._ghost_text)
        if tail:                                   # real text pushed aside, like a normal insert
            p.setPen(QColor("#ABB2BF"))
            p.drawText(x + fm.horizontalAdvance(self._ghost_text), y, tail)


# ---------------------------------------------------------------------------
# Output console
# ---------------------------------------------------------------------------
class OutputConsole(QPlainTextEdit):
    """Program output + a live input line: type after the last output and press
    Enter to send the text to the running script's stdin (input() works)."""
    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._input_start = 0           # everything before this is program output
        self.setUndoRedoEnabled(False)
        self.setCursorWidth(2)
        self.setFont(QFont(CODE_FONT, 10))
        self.setStyleSheet("""
            QPlainTextEdit {
                background-color: rgba(0, 0, 0, 0.18);
                color: #E2E8F0;
                border: none;
                border-top: 1px solid rgba(255,255,255,0.10);
                selection-background-color: rgba(125, 211, 252, 0.28);
            }
        """)

    def clear(self):
        super().clear()
        self._input_start = 0

    def write(self, text: str, tag: str = "out"):
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#E06C75" if tag == "err" else "#A0AEC0"))
        c = QTextCursor(self.document())
        c.setPosition(self._input_start)       # output goes above whatever is being typed
        c.insertText(text, fmt)
        self._input_start = c.position()
        if at_bottom:
            sb.setValue(sb.maximum())

    def _submit(self):
        c = QTextCursor(self.document())
        c.setPosition(self._input_start)
        c.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        line = c.selectedText().replace("\u2029", "\n")
        c.clearSelection()
        c.movePosition(QTextCursor.MoveOperation.End)
        c.insertText("\n")
        self._input_start = c.position()
        self.setTextCursor(c)
        self.ensureCursorVisible()
        self.submitted.emit(line + "\n")

    def keyPressEvent(self, e):
        key, mods = e.key(), e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        cur = self.textCursor()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._submit()
            return
        if key == Qt.Key.Key_Tab or key == Qt.Key.Key_Backtab:
            return
        if key == Qt.Key.Key_Home and not ctrl:
            keep = (QTextCursor.MoveMode.KeepAnchor if mods & Qt.KeyboardModifier.ShiftModifier
                    else QTextCursor.MoveMode.MoveAnchor)
            if cur.position() >= self._input_start:
                cur.setPosition(self._input_start, keep)
                self.setTextCursor(cur)
                return

        text = e.text()
        typing = bool(text) and text.isprintable() and not (ctrl and not alt)
        paste = (ctrl and key == Qt.Key.Key_V) or (
            key == Qt.Key.Key_Insert and mods & Qt.KeyboardModifier.ShiftModifier)
        deleting = key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) or (ctrl and key == Qt.Key.Key_X)

        if deleting:
            if cur.selectionStart() < self._input_start:
                return                                  # old output is read-only
            if key == Qt.Key.Key_Backspace and not cur.hasSelection() \
                    and cur.position() <= self._input_start:
                return
        elif typing or paste:
            if cur.selectionStart() < self._input_start or cur.position() < self._input_start:
                cur.clearSelection()
                cur.movePosition(QTextCursor.MoveOperation.End)
                self.setTextCursor(cur)
        super().keyPressEvent(e)


# ---------------------------------------------------------------------------
# Titlebar: [app icon: left-click Explorer / right-click commands] [tabs] ... [window controls]
# ---------------------------------------------------------------------------
class CircleButton(QAbstractButton):
    def __init__(self, color, glyph, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.glyph = glyph
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self.underMouse()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color.lighter(118) if hover else self.color)
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        if hover:
            dark = self.color.lightness() > 150
            p.setPen(QColor(0, 0, 0, 170) if dark else QColor(255, 255, 255, 230))
            f = self.font()
            f.setPixelSize(11)
            f.setBold(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph)


class TitleTabBar(QTabBar):
    """Titlebar tabs have no close buttons, so middle-click closes a tab."""

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            i = self.tabAt(e.position().toPoint())
            if i >= 0:
                self.tabCloseRequested.emit(i)
                e.accept()
                return
        super().mouseReleaseEvent(e)


class SlideHost(QWidget):
    """Clipping container for the sidebar. While the panel slides, its content keeps
    its full width and is simply revealed - so nothing inside re-lays out on every
    frame (that per-frame relayout was the main source of stutter)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inner = None
        self.full_width = SIDEBAR_WIDTH
        self.animating = False

    def set_animating(self, on: bool, full_width: int = None):
        self.animating = on
        if full_width:
            self.full_width = full_width
        self._fit()

    def _fit(self):
        if self.inner is not None:
            w = self.full_width if self.animating else self.width()
            self.inner.setGeometry(0, 0, max(w, 1), self.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class TitleBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedHeight(TITLEBAR_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 12, 0)
        layout.setSpacing(8)

        # App icon: left-click toggles Explorer; right-click opens the command menu.
        self.btn_app_icon = QPushButton(self)
        self.btn_app_icon.setFixedSize(22, 22)
        self.btn_app_icon.setToolTip("Left-click: toggle Explorer · Ctrl+Tab: toggle sidebar · Right-click: commands")
        self.btn_app_icon.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_app_icon.setCursor(Qt.CursorShape.PointingHandCursor)
        icon = QIcon(str(ICON_PATH)) if ICON_PATH else QIcon()
        icon_ok = not icon.pixmap(QSize(20, 20)).isNull()
        if icon_ok:
            self.btn_app_icon.setIcon(icon)
            self.btn_app_icon.setIconSize(QSize(20, 20))
            self.btn_app_icon.setStyleSheet("""
                QPushButton { background: transparent; border: none; border-radius: 4px; }
                QPushButton:hover { background-color: rgba(0,0,0,0.08); }
            """)
        else:
            self.btn_app_icon.setStyleSheet("""
                QPushButton { background-color: #FF5F56; border: 1px solid #E0443E; border-radius: 11px; }
                QPushButton:hover { background-color: #FF7B73; }
            """)
            why = ("no .ico file found" if not ICON_PATH
                   else f"Qt could not read {ICON_PATH.name}")
            self.btn_app_icon.setToolTip(f"Left-click: toggle Explorer · Ctrl+Tab: toggle sidebar · Right-click: commands ({why}; looked in {APP_DIR})")

        # Tabs live in the titlebar
        self.tabbar = TitleTabBar(self)
        self.tabbar.setDrawBase(False)
        self.tabbar.setExpanding(False)
        self.tabbar.setTabsClosable(False)
        self.tabbar.setMovable(True)
        self.tabbar.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabbar.setStyleSheet("""
            QTabBar { background: transparent; }
            QTabBar::tab {
                background: transparent; border: none; color: #64748B;
                padding: 5px 10px; margin: 0px 2px; font-size: 12px;
            }
            QTabBar::tab:selected { color: #0F172A; font-weight: bold; }
            QTabBar::tab:hover:!selected { color: #1E293B; }
        """)

        self.btn_min = CircleButton("#D4D4D8", "–", self)
        self.btn_max = CircleButton("#9ACD32", "+", self)
        self.btn_close = CircleButton("#1E3A8A", "×", self)
        self.brand_label = QLabel("Wiper IDE", self)
        self.brand_label.setStyleSheet("color: #334155; font-size: 12px; font-weight: 700; padding-right: 5px;")

        layout.addWidget(self.btn_app_icon)
        layout.addSpacing(4)
        layout.addWidget(self.brand_label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(6)
        layout.addWidget(self.tabbar, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch()
        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_max)
        layout.addWidget(self.btn_close)


# ---------------------------------------------------------------------------
# Dropdown menu with the same frosted glass as the window
# ---------------------------------------------------------------------------
class GlassMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def showEvent(self, event):
        super().showEvent(event)
        if IS_WIN:
            try:
                enable_glass(int(self.winId()))
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Themed message box (replaces QMessageBox): frosted glass + platinum titlebar
# ---------------------------------------------------------------------------
class GlassDialog(QDialog):
    BAR_H = 32
    BUTTON_STYLES = {
        "normal": """
            QPushButton { background: rgba(255,255,255,0.08); color: #F8FAFC;
                border: 1px solid rgba(255,255,255,0.16); border-radius: 6px; padding: 6px 18px; }
            QPushButton:hover { background: rgba(255,255,255,0.16); }
        """,
        "danger": """
            QPushButton { background: transparent; color: #E06C75;
                border: 1px solid rgba(224,108,117,0.45); border-radius: 6px; padding: 6px 18px; }
            QPushButton:hover { background: rgba(224,108,117,0.16); }
        """,
        "primary": """
            QPushButton { background: #E5E9F0; color: #0F172A; font-weight: bold;
                border: 1px solid #CBD5E1; border-radius: 6px; padding: 6px 20px; }
            QPushButton:hover { background: #FFFFFF; }
        """,
    }

    def __init__(self, parent, title, text, hint, buttons):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(400)
        self.choice = None
        self._glass_done = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QWidget(self)
        bar.setFixedHeight(self.BAR_H)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 0, 12, 0)
        lbl = QLabel(title)
        lbl.setStyleSheet("color: #0F172A; font-size: 12px; font-weight: bold; background: transparent;")
        close = CircleButton("#1E3A8A", "×", bar)
        close.clicked.connect(self.reject)
        bl.addWidget(lbl)
        bl.addStretch()
        bl.addWidget(close)
        root.addWidget(bar)

        body = QVBoxLayout()
        body.setContentsMargins(22, 18, 22, 18)
        body.setSpacing(6)
        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #F1F5F9; font-size: 14px; background: transparent;")
        body.addWidget(msg)
        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            h.setStyleSheet("color: #94A3B8; font-size: 12px; background: transparent;")
            body.addWidget(h)
        body.addSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        for label, key, kind in buttons:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(self.BUTTON_STYLES[kind])
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            if kind == "primary":
                b.setDefault(True)
            row.addWidget(b)
        body.addLayout(row)
        root.addLayout(body)

    def _pick(self, key):
        self.choice = key
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if IS_WIN and not self._glass_done:
            self._glass_done = True
            enable_glass(int(self.winId()))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(0, 0, w, h), WINDOW_RADIUS, WINDOW_RADIUS)
        p.setClipPath(shape)
        p.fillRect(self.rect(), QColor(15, 23, 42, 150))          # tint (kept > 0 alpha)
        p.fillRect(0, 0, w, self.BAR_H, PLATINUM_SILVER)
        p.setPen(QPen(PLATINUM_BORDER, 1))
        p.drawLine(0, self.BAR_H, w, self.BAR_H)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), WINDOW_RADIUS, WINDOW_RADIUS)

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.BAR_H
                and self.windowHandle()):
            self.windowHandle().startSystemMove()
            return
        super().mousePressEvent(event)


def glass_message(parent, title, text, buttons, hint="") -> str:
    """buttons: [(label, key, 'normal' | 'danger' | 'primary')]. Returns the key, or 'cancel'."""
    dlg = GlassDialog(parent, title, text, hint, buttons)
    dlg.exec()
    return dlg.choice or "cancel"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path = None):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        if ICON_PATH:
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        self.proc = None
        self._tmp = None
        self.native_frame = False
        self._glass_done = False
        self.editors = {}
        self._uid = 0
        self.project_root = None
        self._git_proc = None
        self._last_search = None
        self._outline_key = None

        self._setup_ui()
        self._build_menu()
        self._wire_signals()
        if initial_path and initial_path.exists():
            self.set_project_root(initial_path if initial_path.is_dir() else initial_path.parent)

        # Linux/macOS: no WM_NCHITTEST, so edge-resize is handled via an event filter
        if not IS_WIN:
            QApplication.instance().installEventFilter(self)

        if initial_path and initial_path.exists():
            self.open_file_path(initial_path)
        else:
            self.new_tab()

        # polish + lay out the sidebar once in the background so the first open is smooth
        QTimer.singleShot(250, self._prewarm_sidebar)

    def showEvent(self, event):
        super().showEvent(event)
        if IS_WIN and not self._glass_done:
            self._glass_done = True
            self.native_frame = enable_glass(int(self.winId()))

    def _setup_ui(self):
        self.resize(1100, 700)
        self.setStyleSheet("QMainWindow { background: transparent; }")

        self.central_widget = QWidget(self)
        self.central_widget.setObjectName("central")
        self.central_widget.setStyleSheet("#central { background: transparent; }")
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.titlebar = TitleBar(self)
        self.tabbar = self.titlebar.tabbar
        main_layout.addWidget(self.titlebar)

        # Workspace: the titlebar app icon toggles a compact, animated Explorer.
        # No permanent activity rail: keep every pixel available to the editor.
        self.workspace = QSplitter(Qt.Orientation.Horizontal)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.setHandleWidth(1)
        self.workspace.setStyleSheet("QSplitter::handle { background: rgba(255,255,255,0.10); }")

        # Sidebar: compact view switcher (Explorer / Search / Git) with
        # persistent lower sections for Outline and Python Environment.
        self.sidebar = SlideHost()
        self.sidebar_inner = QFrame(self.sidebar)
        self.sidebar.inner = self.sidebar_inner
        self.sidebar.setMinimumWidth(0)
        self.sidebar.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        self.sidebar_inner.setStyleSheet("""
            QFrame { background: rgba(13, 20, 33, 0.30); border-right: 1px solid rgba(255,255,255,0.09); }
            QLabel { color: #CBD5E1; background: transparent; border: none; }
            QPushButton { color: #DCE4EE; background: rgba(255,255,255,0.045);
                border: 1px solid rgba(203,213,225,0.15); border-radius: 6px;
                padding: 6px 8px; text-align: left; }
            QPushButton:hover { background: rgba(255,255,255,0.10); }
            QPushButton:checked { background: rgba(148,163,184,0.18); border-color: rgba(203,213,225,0.34); }
            QLineEdit { color: #E2E8F0; background: rgba(0,0,0,0.18); border: 1px solid rgba(203,213,225,0.18);
                border-radius: 6px; padding: 7px; selection-background-color: rgba(125,211,252,0.25); }
            QTreeView, QListWidget { background: transparent; color: #D8E1ED; border: none;
                outline: none; padding: 3px; selection-background-color: rgba(148,163,184,0.22);
                selection-color: #FFFFFF; }
            QTreeView::item, QListWidget::item { min-height: 23px; padding: 3px 4px; border-radius: 4px; }
            QTreeView::item:hover, QListWidget::item:hover { background: rgba(255,255,255,0.07); }
            QTabWidget::pane { border: none; background: transparent; }
            QTabBar { background: transparent; }
            QTabBar::tab {
                color: #94A3B8;
                background: transparent;
                border: none;
                padding: 6px 14px;
                min-width: 42px;
                font-size: 13px;
                font-weight: 500;
            }
            QTabBar::tab:selected {
                color: #E2E8F0;
                border-bottom: 2px solid #B8C2D0;
                font-weight: 600;
            }
        """)
        side_layout = QVBoxLayout(self.sidebar_inner)
        side_layout.setContentsMargins(8, 9, 8, 8)
        side_layout.setSpacing(7)

        self.sidebar_views = QTabWidget()
        self.sidebar_views.setDocumentMode(True)
        self.sidebar_views.setMinimumHeight(150)

        # Explorer view
        explorer_page = QWidget()
        explorer_layout = QVBoxLayout(explorer_page)
        explorer_layout.setContentsMargins(0, 0, 0, 0)
        explorer_layout.setSpacing(7)
        self.btn_open_folder = QPushButton("＋  Open Folder")
        self.btn_open_folder.setToolTip("Choose a project folder")
        explorer_layout.addWidget(self.btn_open_folder)
        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.file_model.setReadOnly(False)
        self.file_tree = QTreeView()
        self.file_tree.setModel(self.file_model)
        self.file_tree.setHeaderHidden(True)
        for column in range(1, 4):
            self.file_tree.hideColumn(column)
        self.file_tree.setAnimated(True)
        self.file_tree.setIndentation(14)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setEditTriggers(QTreeView.EditTrigger.EditKeyPressed | QTreeView.EditTrigger.SelectedClicked)
        self.file_tree.setToolTip("Open a project folder to browse its files")
        self.explorer_empty = QLabel("Open a folder to explore your project\n\nNo project folder selected")
        self.explorer_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.explorer_empty.setWordWrap(True)
        self.explorer_empty.setStyleSheet("color: rgba(203,213,225,0.58); font-size: 11px; padding: 18px;")
        explorer_layout.addWidget(self.explorer_empty, 1)
        explorer_layout.addWidget(self.file_tree, 1)
        self.file_tree.hide()
        self.sidebar_views.addTab(explorer_page, "Explorer")

        # Project-wide text search
        search_page = QWidget()
        search_layout = QVBoxLayout(search_page)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(7)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search in project…")
        self.search_results = QListWidget()
        self.search_results.setToolTip("Double-click a result to open the matching file")
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_results, 1)
        self.sidebar_views.addTab(search_page, "Search")

        # Git is intentionally a thin status view using the local Git CLI.
        git_page = QWidget()
        git_layout = QVBoxLayout(git_page)
        git_layout.setContentsMargins(0, 0, 0, 0)
        git_layout.setSpacing(7)
        self.git_refresh = QPushButton("↻  Refresh Git status")
        self.git_status = QListWidget()
        self.git_status.setToolTip("Git status for the selected project")
        git_layout.addWidget(self.git_refresh)
        git_layout.addWidget(self.git_status, 1)
        self.sidebar_views.addTab(git_page, "Git")
        side_layout.addWidget(self.sidebar_views, 3)

        # Lower utility area remains visible whichever primary view is selected.
        self.utility_tabs = QTabWidget()
        self.utility_tabs.setDocumentMode(True)
        self.utility_tabs.setMinimumHeight(115)
        self.outline_list = QListWidget()
        self.outline_list.setToolTip("Functions and classes in the active Python file")
        self.utility_tabs.addTab(self.outline_list, "Outline")
        self.environment_list = QListWidget()
        self.utility_tabs.addTab(self.environment_list, "Python Env")
        side_layout.addWidget(self.utility_tabs, 2)
        self.workspace.addWidget(self.sidebar)

        # One width animation drives the splitter. No opacity effect: re-rendering the
        # whole sidebar offscreen every frame is what made the old animation stutter.
        self._sidebar_width_animation = QVariantAnimation(self)
        self._sidebar_width_animation.valueChanged.connect(self._set_sidebar_animated_width)
        self._sidebar_width_animation.finished.connect(self._finish_sidebar_animation)
        self._sidebar_is_open = False
        self._sidebar_target_width = SIDEBAR_WIDTH
        self.sidebar.hide()

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: rgba(255,255,255,0.10); }")
        self.stack = QStackedWidget()
        self.output = OutputConsole()
        self.problems = QPlainTextEdit()
        self.problems.setReadOnly(True)
        self.problems.setUndoRedoEnabled(False)
        self.problems.setFont(QFont(CODE_FONT, 10))
        self.problems.setPlaceholderText("No problems detected.")
        self.problems.setStyleSheet("""
            QPlainTextEdit { background: rgba(0,0,0,0.14); color: #CBD5E1; border: none;
                padding: 8px; selection-background-color: rgba(125,211,252,0.25); }
        """)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.addTab(self.output, "Output")
        self.bottom_tabs.addTab(self.problems, "Problems")
        self.bottom_tabs.setStyleSheet("""
            QTabWidget::pane { background: rgba(0,0,0,0.15); border: none;
                border-top: 1px solid rgba(255,255,255,0.10); }
            QTabBar { background: rgba(5,10,20,0.20); }
            QTabBar::tab { background: transparent; color: #94A3B8; border: none;
                padding: 7px 14px; margin-right: 2px; font-size: 11px; }
            QTabBar::tab:selected { color: #E2E8F0; background: rgba(203,213,225,0.10);
                border-bottom: 2px solid #B8C2D0; }
            QTabBar::tab:hover:!selected { color: #E2E8F0; }
        """)
        self.bottom_tabs.setMinimumHeight(110)
        self.bottom_tabs.hide()
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self.bottom_tabs)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.workspace.addWidget(self.splitter)
        self.workspace.setStretchFactor(0, 0)
        self.workspace.setStretchFactor(1, 1)
        self.workspace.setSizes([0, 1100])
        main_layout.addWidget(self.workspace, 1)

        self.status = QLabel("")
        main_layout.addWidget(self.status)
        self._style_status("#94A3B8")
        # Start with an empty Explorer instead of listing the process working
        # directory (which may be a Python install or Windows system folder).

    def _style_status(self, color):
        self.status.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 3px 10px; background: rgba(0,0,0,0.12);")

    def _build_menu(self):
        self.menu = GlassMenu(self)
        self.menu.setStyleSheet("""
            QMenu {
                background-color: rgba(15, 23, 42, 0.40);
                color: #F8FAFC;
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item { padding: 6px 28px 6px 16px; border-radius: 4px; margin: 0px 2px; }
            QMenu::item:selected { background-color: rgba(255, 255, 255, 0.14); }
            QMenu::separator { height: 1px; background: rgba(255, 255, 255, 0.14); margin: 4px 8px; }
        """)

        def act(text, shortcut, slot):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(lambda checked=False: slot())
            self.addAction(a)
            return a

        self.menu.addAction(act("New", "Ctrl+N", self.new_tab))
        self.menu.addAction(act("Open", "Ctrl+O", self.open_file_dialog))
        self.menu.addSeparator()
        self.menu.addAction(act("Save", "Ctrl+S", self.save))
        self.menu.addAction(act("Save As", "Ctrl+Shift+S", self.save_as))
        self.menu.addSeparator()
        self.menu.addAction(act("Run", "Ctrl+R", self.run_script))
        self.menu.addAction(act("Stop", "Ctrl+Shift+R", self.stop_script))
        self.menu.addSeparator()
        self.menu.addAction(act("Close Tab", "Ctrl+W", lambda: self.close_tab(self.tabbar.currentIndex())))
        self.menu.addAction(act("Exit", None, self.close))

    def _wire_signals(self):
        t = self.titlebar
        t.btn_app_icon.clicked.connect(self._toggle_explorer)
        t.btn_app_icon.customContextMenuRequested.connect(self._show_menu_at_icon)
        t.btn_min.clicked.connect(self.showMinimized)
        t.btn_max.clicked.connect(self._toggle_maximize)
        t.btn_close.clicked.connect(self.close)
        self.tabbar.tabCloseRequested.connect(self.close_tab)
        self.tabbar.currentChanged.connect(self._on_tab_changed)
        self.output.submitted.connect(self._send_input)
        self.btn_open_folder.clicked.connect(self._choose_project_folder)
        self.file_tree.doubleClicked.connect(self._on_file_tree_double_clicked)
        self.bottom_tabs.currentChanged.connect(lambda _: self.bottom_tabs.update())
        self.search_input.returnPressed.connect(self._search_project)
        self.search_results.itemDoubleClicked.connect(self._open_search_result)
        self.git_refresh.clicked.connect(self._refresh_git_status)
        self.outline_list.itemDoubleClicked.connect(self._jump_to_outline_item)
        self.sidebar_views.currentChanged.connect(self._sidebar_view_changed)
        self.workspace.splitterMoved.connect(self._on_workspace_moved)
        self.utility_tabs.currentChanged.connect(lambda _: self.utility_tabs.update())
        self._refresh_environment()
        self.shortcut_sidebar_previous = QShortcut(QKeySequence("Shift+A"), self)
        self.shortcut_sidebar_previous.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_sidebar_previous.activated.connect(lambda: self._cycle_sidebar_view(-1))
        self.shortcut_sidebar_next = QShortcut(QKeySequence("Shift+D"), self)
        self.shortcut_sidebar_next.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_sidebar_next.activated.connect(lambda: self._cycle_sidebar_view(1))

        # Ctrl+Tab toggles the sidebar without stealing plain Tab from the editor,
        # where it is used for indentation and accepting autocomplete suggestions.
        self.shortcut_toggle_sidebar = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.shortcut_toggle_sidebar.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_toggle_sidebar.activated.connect(self._toggle_explorer)

    def _sidebar_view_changed(self, index):
        """Refresh view-specific data on demand without slowing the editor."""
        if index == 2:
            self._refresh_git_status()
        elif index == 1:
            query = self.search_input.text().strip()
            if query and self._last_search != (str(self.project_root), query):
                self._search_project()

    def _cycle_sidebar_view(self, delta):
        count = self.sidebar_views.count()
        self.sidebar_views.setCurrentIndex((self.sidebar_views.currentIndex() + delta) % count)
        if not self._sidebar_is_open:
            self._toggle_explorer()
        # Keep keyboard navigation useful even when the panel was closed.

    def _refresh_environment(self):
        py = find_python()
        self.environment_list.clear()
        self.environment_list.addItem("Python " + sys.version.split()[0])
        self.environment_list.addItem(Path(py).name if py else "python (not found on PATH)")
        self.environment_list.addItem("Interpreter:")
        self.environment_list.addItem(py or "Install Python and tick 'Add to PATH'")
        self.environment_list.addItem("Packages: use pip in the terminal")
        self.environment_list.setToolTip(py or "")

    def _refresh_outline(self):
        editor = self.current_editor()
        if not editor:
            rows = (("No active file", None),)
        else:
            tree, err = editor.parse_tree()          # cached: no second parse
            if tree is None:
                line = err.lineno if isinstance(err, SyntaxError) else None
                rows = ((f"Syntax error near line {line or '?'}", None),)
            else:
                entries = []
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        kind = ("class" if isinstance(node, ast.ClassDef)
                                else "async def" if isinstance(node, ast.AsyncFunctionDef) else "def")
                        entries.append((getattr(node, "lineno", 1), kind, node.name))
                rows = tuple((f"{kind}  {name}  ·  L{line}", line) for line, kind, name in sorted(entries))
                rows = rows or (("No classes or functions", None),)

        key = (editor.uid if editor else None, rows)
        if key == self._outline_key:                 # unchanged -> don't rebuild (no flicker / lost scroll)
            return
        self._outline_key = key
        self.outline_list.clear()
        for text, line in rows:
            item = QListWidgetItem(text)
            if line:
                item.setData(Qt.ItemDataRole.UserRole, line)
            self.outline_list.addItem(item)

    def _goto_line(self, editor, line):
        block = editor.document().findBlockByNumber(max(0, int(line) - 1))
        if block.isValid():
            editor.setTextCursor(QTextCursor(block))
            editor.centerCursor()
        editor.setFocus()

    def _jump_to_outline_item(self, item):
        line = item.data(Qt.ItemDataRole.UserRole)
        editor = self.current_editor()
        if editor and line:
            self._goto_line(editor, line)

    def _search_project(self):
        self.search_results.clear()
        if not self.project_root:
            self.search_results.addItem("Open a project folder first.")
            return
        needle = self.search_input.text().strip()
        if not needle:
            return
        self._last_search = (str(self.project_root), needle)
        needle_cf = needle.casefold()
        ignored = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build"}
        exts = {".py", ".pyw", ".txt", ".md", ".json", ".toml", ".ini", ".yml", ".yaml",
                ".qss", ".css", ".html", ".js", ".ts", ".rs", ".c", ".h"}
        max_results = 500
        count = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.search_results.setUpdatesEnabled(False)
        try:
            for root, dirs, files in os.walk(self.project_root):
                dirs[:] = [d for d in dirs if d not in ignored]
                for name in files:
                    path = Path(root) / name
                    if path.suffix.lower() not in exts:
                        continue
                    try:
                        if path.stat().st_size > 1_000_000:
                            continue
                        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    except (OSError, UnicodeError):
                        continue
                    for number, line in enumerate(lines, 1):
                        if needle_cf in line.casefold():
                            item = QListWidgetItem(f"{path.relative_to(self.project_root)}:{number}  {line.strip()[:140]}")
                            item.setData(Qt.ItemDataRole.UserRole, (str(path), number))
                            self.search_results.addItem(item)
                            count += 1
                            if count >= max_results:
                                self.search_results.addItem("Result limit reached (500). Refine your search.")
                                return
            if not count:
                self.search_results.addItem("No matches found.")
        except OSError as exc:
            self.search_results.addItem(f"Search failed: {exc}")
        finally:
            self.search_results.setUpdatesEnabled(True)
            QApplication.restoreOverrideCursor()

    def _open_search_result(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        path, line = data
        self.open_file_path(Path(path))
        editor = self.current_editor()
        if editor:
            self._goto_line(editor, line)

    # Git runs through QProcess so a slow repo can never freeze the UI.
    def _refresh_git_status(self):
        self.git_status.clear()
        old, self._git_proc = self._git_proc, None
        if old is not None:
            try:
                old.kill()
            except RuntimeError:
                pass
        if not self.project_root:
            self.git_status.addItem("Open a project folder first.")
            return
        self.git_status.addItem("Checking Git status…")
        proc = QProcess(self)
        self._git_proc = proc
        proc.setProgram("git")
        proc.setArguments(["-C", str(self.project_root), "status", "--short", "--branch"])
        proc.finished.connect(lambda code, _status, p=proc: self._git_finished(p, code))
        proc.errorOccurred.connect(lambda err, p=proc: self._git_error(p, err))
        proc.start()

    def _git_finished(self, proc, code):
        if proc is not self._git_proc:               # superseded by a newer refresh
            proc.deleteLater()
            return
        self._git_proc = None
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        err = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace")
        proc.deleteLater()
        self.git_status.clear()
        if code != 0:
            if "not a git repository" in err.lower():
                self.git_status.addItem("This folder is not a Git repository.")
                self.git_status.addItem("Run `git init` to start tracking it.")
            else:
                self.git_status.addItem((err or "Unable to read Git status.").strip())
            return
        lines = out.splitlines()
        if not lines or (len(lines) == 1 and lines[0].startswith("##")):
            lines.append("Working tree clean")
        for line in lines:
            self.git_status.addItem(line)

    def _git_error(self, proc, error):
        if proc is not self._git_proc or error != QProcess.ProcessError.FailedToStart:
            return
        self._git_proc = None
        proc.deleteLater()
        self.git_status.clear()
        self.git_status.addItem("Git CLI not found.")
        self.git_status.addItem("Install Git for Windows to enable this view.")

    def _show_menu_at_icon(self, pos):
        # Right-click the app icon for New/Open/Save/Run/Close commands.
        btn = self.titlebar.btn_app_icon
        self.menu.exec(btn.mapToGlobal(QPoint(0, btn.height() + 4)))

    def _set_sidebar_animated_width(self, value):
        """Resize the sidebar through the splitter so the editor moves with it."""
        width = max(0, int(value))
        total = max(1, self.workspace.width())
        width = min(width, max(0, total - 200))
        self.workspace.setSizes([width, max(1, total - width)])

    def _finish_sidebar_animation(self):
        host = self.sidebar
        if self._sidebar_is_open:
            host.set_animating(False)                # content follows the real width again
            host.setMinimumWidth(SIDEBAR_MIN_WIDTH)
            self._set_sidebar_animated_width(self._sidebar_target_width)
        else:
            fw = QApplication.focusWidget()
            editor = self.current_editor()
            if editor and fw is not None and host.isAncestorOf(fw):
                editor.setFocus()                    # don't leave focus in a hidden panel
            host.hide()
            host.set_animating(False)
            host.setMinimumWidth(0)
            self.workspace.setSizes([0, max(1, self.workspace.width())])

    def _on_workspace_moved(self, pos, index):
        """Remember the width the user dragged the sidebar to."""
        if (self._sidebar_is_open
                and self._sidebar_width_animation.state() != QAbstractAnimation.State.Running
                and self.sidebar.width() >= SIDEBAR_MIN_WIDTH):
            self._sidebar_target_width = self.sidebar.width()

    def _prewarm_sidebar(self):
        """Polish and lay out the sidebar once while idle, so its first open
        doesn't have to do all of that in the middle of the animation."""
        try:
            inner = self.sidebar_inner
            if not self.sidebar.isVisible():
                inner.resize(self._sidebar_target_width, max(300, self.workspace.height()))
            for w in [inner] + inner.findChildren(QWidget):
                w.ensurePolished()
            if inner.layout() is not None:
                inner.layout().activate()
        except Exception:
            pass

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---- status bar
    def _on_syntax(self, editor, ok, msg):
        if editor is self.current_editor():
            self._refresh_status()
            self._refresh_problems()
            self._refresh_outline()

    def _refresh_problems(self):
        editor = self.current_editor()
        if not editor:
            text = "No editor is open."
        else:
            ok, message = editor.last_status
            if ok:
                text = "✓  No syntax errors detected in the active file."
            else:
                name = editor.path.name if editor.path else "Untitled.py"
                text = f"●  {name}: {message}"
        if self.problems.toPlainText() != text:      # avoid resetting the pane on every keystroke
            self.problems.setPlainText(text)

    def _animate_sidebar(self, start: int, end: int):
        anim = self._sidebar_width_animation
        anim.stop()
        # a short remaining distance (reversing mid-flight) gets a proportionally short run
        fraction = min(1.0, abs(end - start) / max(1, self._sidebar_target_width))
        anim.setDuration(int(max(90, SIDEBAR_ANIM_MS * fraction)))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(int(start))
        anim.setEndValue(int(end))
        self._set_sidebar_animated_width(start)
        anim.start()

    def _toggle_explorer(self, checked=False):
        """Toggle the Explorer from the titlebar app icon with a smooth slide."""
        host = self.sidebar
        self._sidebar_width_animation.stop()
        # Continue from the current width so reversing mid-flight never snaps.
        start = host.width() if host.isVisible() else 0

        if self._sidebar_is_open:
            if start <= 0:
                start = self._sidebar_target_width
            self._sidebar_is_open = False
            host.setMinimumWidth(0)
            host.set_animating(True, self._sidebar_target_width)
            self._animate_sidebar(start, 0)
            return

        self._sidebar_is_open = True
        host.setMinimumWidth(0)
        host.setMaximumWidth(SIDEBAR_MAX_WIDTH)
        host.set_animating(True, self._sidebar_target_width)
        host.show()
        self._animate_sidebar(start, self._sidebar_target_width)

    def _choose_project_folder(self):
        current = str(self.project_root) if getattr(self, "project_root", None) else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Open Project Folder", current)
        if folder:
            self.set_project_root(Path(folder))

    def set_project_root(self, folder: Path):
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            return
        self.project_root = folder
        self._last_search = None
        self.search_results.clear()
        root_index = self.file_model.setRootPath(str(folder))
        self.file_tree.setRootIndex(root_index)
        self.file_tree.setToolTip(str(folder))
        self.explorer_empty.hide()
        self.file_tree.show()
        if self.sidebar_views.currentIndex() == 2:
            self._refresh_git_status()

    def _on_file_tree_double_clicked(self, index):
        path = Path(self.file_model.filePath(index))
        if path.is_file():
            self.open_file_path(path)

    def _refresh_status(self):
        ed = self.current_editor()
        if not ed or not ed.last_status[1]:
            self.status.setText("")
            return
        ok, msg = ed.last_status
        self._style_status("#98C379" if ok else "#E06C75")
        self.status.setText(("✓ " if ok else "✗ ") + msg)

    # ---- tabs (QTabBar in titlebar + QStackedWidget)
    def _editor_at(self, index):
        if index < 0:
            return None
        return self.editors.get(self.tabbar.tabData(index))

    def _index_of(self, editor):
        for i in range(self.tabbar.count()):
            if self.tabbar.tabData(i) == editor.uid:
                return i
        return -1

    def current_editor(self) -> CodeEditor:
        return self._editor_at(self.tabbar.currentIndex())

    def _on_tab_changed(self, index):
        ed = self._editor_at(index)
        if ed:
            self.stack.setCurrentWidget(ed)
            ed.setFocus()
        self._refresh_status()
        self._refresh_problems()
        self._refresh_outline()

    def new_tab(self, path: Path = None):
        editor = CodeEditor()
        editor.path = path
        self._uid += 1
        editor.uid = self._uid
        self.editors[editor.uid] = editor
        self.stack.addWidget(editor)

        if path and path.exists():
            try:
                editor.setPlainText(path.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                glass_message(self, "Open failed", f"Could not read {path.name}.",
                              [("OK", "ok", "primary")], hint=str(exc))
        editor.document().setModified(False)
        editor.document().modificationChanged.connect(lambda _, e=editor: self._update_tab_title(e))
        editor.syntax_changed.connect(lambda ok, msg, e=editor: self._on_syntax(e, ok, msg))

        idx = self.tabbar.addTab(path.name if path else "Untitled.py")
        self.tabbar.setTabData(idx, editor.uid)
        self.tabbar.setCurrentIndex(idx)
        editor.setFocus()

    def _update_tab_title(self, editor):
        idx = self._index_of(editor)
        if idx < 0:
            return
        name = editor.path.name if editor.path else "Untitled.py"
        self.tabbar.setTabText(idx, name + (" *" if editor.document().isModified() else ""))

    def _confirm_discard(self, editor) -> bool:
        """Ask about unsaved changes. True = ok to go on (saved or discarded)."""
        if not editor.document().isModified():
            return True
        name = editor.path.name if editor.path else "Untitled.py"
        r = glass_message(
            self, "Unsaved changes", f"Save changes to {name}?",
            [("Discard", "discard", "danger"), ("Cancel", "cancel", "normal"), ("Save", "save", "primary")],
            hint="Your changes will be lost if you don't save them.")
        if r == "save":
            return self.save(editor)
        return r == "discard"

    def close_tab(self, index: int):
        editor = self._editor_at(index)
        if editor is None:
            return
        if not self._confirm_discard(editor):
            return
        if self.tabbar.count() == 1:
            self.new_tab()
        idx = self._index_of(editor)
        self.tabbar.removeTab(idx)
        self.stack.removeWidget(editor)
        self.editors.pop(editor.uid, None)
        editor.deleteLater()

    # ---- file IO
    def _write(self, editor, path: Path) -> bool:
        try:
            path.write_text(editor.toPlainText(), encoding="utf-8")
        except OSError as e:
            glass_message(self, "Save failed", "Could not save the file.",
                          [("OK", "ok", "primary")], hint=str(e))
            return False
        editor.path = path
        editor.document().setModified(False)
        self._update_tab_title(editor)
        return True

    def save(self, editor=None) -> bool:
        editor = editor or self.current_editor()
        if not editor:
            return False
        if not editor.path:
            return self.save_as(editor)
        return self._write(editor, editor.path)

    def save_as(self, editor=None) -> bool:
        editor = editor or self.current_editor()
        if not editor:
            return False
        start_dir = editor.path or ((self.project_root or Path.home()) / "Untitled.py")
        fname, chosen = QFileDialog.getSaveFileName(self, "Save File As", str(start_dir),
                                                    "Python Files (*.py);;All Files (*)")
        if not fname:
            return False
        path = Path(fname)
        if not path.suffix and chosen.startswith("Python"):
            path = path.with_suffix(".py")
        return self._write(editor, path)

    def open_file_dialog(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open File", "", "Python Files (*.py);;All Files (*)")
        if fname:
            self.open_file_path(Path(fname))

    def open_file_path(self, path: Path):
        for ed in self.editors.values():
            if ed.path == path:
                self.tabbar.setCurrentIndex(self._index_of(ed))
                return
        self.new_tab(path)

    # ---- run (works on unsaved text - nothing is written to your file)
    def run_script(self):
        editor = self.current_editor()
        if not editor:
            return
        self.stop_script(hide=False)

        python = find_python()
        self.bottom_tabs.show()
        self.bottom_tabs.setCurrentWidget(self.output)
        available = max(300, self.splitter.height())
        panel_height = min(190, max(120, available // 3))
        self.splitter.setSizes([max(120, available - panel_height), panel_height])
        self.output.clear()
        if not python:
            self.output.write("Python was not found on PATH. Install Python and tick 'Add to PATH'.\n", "err")
            return

        name = editor.path.name if editor.path else "Untitled.py"
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")      # same encoding in both directions
        workdir = editor.path.parent if editor.path else Path.home()

        if editor.path and not editor.document().isModified():
            # saved and unchanged: run the real file
            args, label = ["-u", str(editor.path)], name
        else:
            # untitled or edited: run the buffer from a temp copy, through a tiny bootstrap
            # that sets __file__, sys.path, argv and tracebacks as if it were the real file
            self._tmp = tempfile.TemporaryDirectory(prefix="ide_run_")
            tmp = Path(self._tmp.name)
            src = tmp / "buffer.py"
            boot = tmp / "_ide_boot.py"
            src.write_text(editor.toPlainText(), encoding="utf-8")
            boot.write_text(BOOTSTRAP, encoding="utf-8")
            real = editor.path if editor.path else workdir / name
            args = ["-u", str(boot), str(src), str(real)]
            label = f"{name} (unsaved)"

        self.output.write(f"> Running {label}...\n")

        proc = QProcess(self)
        self.proc = proc
        proc.setProgram(python)
        proc.setArguments(args)
        proc.setWorkingDirectory(str(workdir))
        proc.setProcessEnvironment(env)
        # incremental decoders: a multi-byte character split across two reads stays intact
        dec_out = codecs.getincrementaldecoder("utf-8")("replace")
        dec_err = codecs.getincrementaldecoder("utf-8")("replace")
        proc.readyReadStandardOutput.connect(
            lambda: self.output.write(dec_out.decode(bytes(proc.readAllStandardOutput()))))
        proc.readyReadStandardError.connect(
            lambda: self.output.write(dec_err.decode(bytes(proc.readAllStandardError())), "err"))
        proc.finished.connect(lambda code, _: self._on_finished(proc, code))
        proc.start()

    def _send_input(self, text: str):
        if self.proc is not None and self.proc.state() == QProcess.ProcessState.Running:
            self.proc.write(text.encode("utf-8"))

    def _on_finished(self, proc, code):
        if proc is self.proc:
            self.output.write(f"\n> Finished (exit code {code})\n")
            self._cleanup_tmp()

    def _cleanup_tmp(self):
        tmp, self._tmp = self._tmp, None
        if tmp:
            try:
                tmp.cleanup()
            except Exception:
                pass

    def stop_script(self, hide=True):
        proc, self.proc = self.proc, None
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()
            proc.waitForFinished(500)
        self._cleanup_tmp()
        if hide:
            self.bottom_tabs.hide()

    def keyPressEvent(self, event):
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier and not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)):
            if event.key() == Qt.Key.Key_A:
                self._cycle_sidebar_view(-1)
                return
            if event.key() == Qt.Key.Key_D:
                self._cycle_sidebar_view(1)
                return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # ask about every modified tab before the window goes away
        for ed in list(self.editors.values()):
            if ed.document().isModified():
                self.tabbar.setCurrentIndex(self._index_of(ed))
                if not self._confirm_discard(ed):
                    event.ignore()
                    return
        self.stop_script(hide=False)
        super().closeEvent(event)

    # ---- painting
    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        radius = 0 if self.isMaximized() else WINDOW_RADIUS
        r = event.rect()

        # The anti-aliased rounded clip only matters where the dirty area touches a
        # corner; skipping it elsewhere makes the frequent interior repaints cheap.
        if radius and (r.left() < radius or r.top() < radius
                       or r.right() >= w - radius or r.bottom() >= h - radius):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
            painter.setClipPath(path)

        # translucent tint over the blurred backdrop (alpha must stay > 0,
        # fully transparent pixels become click-through on Windows)
        painter.fillRect(r, WORKSPACE_TINT)

        # solid platinum titlebar
        painter.fillRect(0, 0, w, TITLEBAR_HEIGHT, PLATINUM_SILVER)
        painter.setPen(QPen(PLATINUM_BORDER, 1))
        painter.drawLine(0, TITLEBAR_HEIGHT, w, TITLEBAR_HEIGHT)

    # ---- Linux / macOS: drag + resize through the window manager
    def _edges_at(self, pos):
        edges = Qt.Edge(0)
        if self.isMaximized() or self.isFullScreen():
            return edges
        b = RESIZE_BORDER
        if pos.x() < b:
            edges |= Qt.Edge.LeftEdge
        if pos.x() > self.width() - b:
            edges |= Qt.Edge.RightEdge
        if pos.y() < b:
            edges |= Qt.Edge.TopEdge
        if pos.y() > self.height() - b:
            edges |= Qt.Edge.BottomEdge
        return edges

    def eventFilter(self, obj, ev):
        if (not IS_WIN and ev.type() == QEvent.Type.MouseButtonPress
                and ev.button() == Qt.MouseButton.LeftButton
                and isinstance(obj, QWidget) and obj.window() is self):
            pos = self.mapFromGlobal(ev.globalPosition().toPoint())
            edges = self._edges_at(pos)
            handle = self.windowHandle()
            if edges and handle:
                handle.startSystemResize(edges)
                return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, event):
        # only reached for clicks on empty titlebar space (buttons/tabs accept theirs)
        if (not IS_WIN and event.button() == Qt.MouseButton.LeftButton
                and event.position().y() < TITLEBAR_HEIGHT and self.windowHandle()):
            self.windowHandle().startSystemMove()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (not IS_WIN and event.button() == Qt.MouseButton.LeftButton
                and event.position().y() < TITLEBAR_HEIGHT):
            self._toggle_maximize()
            return
        super().mouseDoubleClickEvent(event)

    # ---- Windows native drag / resize
    def nativeEvent(self, event_type, message):
        if IS_WIN:
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:
                return super().nativeEvent(event_type, message)
            if msg.message == WM_NCHITTEST:
                pos = self.mapFromGlobal(QCursor.pos())
                x, y, w, h = pos.x(), pos.y(), self.width(), self.height()

                if not self.isMaximized():
                    b = RESIZE_BORDER
                    if y < b and x < b: return True, HTTOPLEFT
                    if y < b and x > w - b: return True, HTTOPRIGHT
                    if y > h - b and x < b: return True, HTBOTTOMLEFT
                    if y > h - b and x > w - b: return True, HTBOTTOMRIGHT
                    if y < b: return True, HTTOP
                    if y > h - b: return True, HTBOTTOM
                    if x < b: return True, HTLEFT
                    if x > w - b: return True, HTRIGHT

                if 0 <= y < TITLEBAR_HEIGHT:
                    child = self.childAt(pos)
                    if isinstance(child, QAbstractButton):
                        return True, HTCLIENT
                    if isinstance(child, QTabBar):
                        if child.tabAt(child.mapFrom(self, pos)) >= 0:
                            return True, HTCLIENT
                    return True, HTCAPTION

        return super().nativeEvent(event_type, message)


def main():
    global CODE_FONT
    # Prefer crisp UI text and high-DPI pixmaps without changing the app layout.
    try:                                   # always on in Qt 6; the attribute may be gone entirely
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    app = QApplication(sys.argv)
    ui_font = QFont("Segoe UI", 10)
    ui_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(ui_font)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Wiper")
    CODE_FONT = load_fonts()
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    window = MainWindow(initial)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
