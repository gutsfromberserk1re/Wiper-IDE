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
from pathlib import Path

from PySide6.QtCore import (
    Qt, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QProcess, QProcessEnvironment,
    QTimer, Signal
)
from PySide6.QtGui import (
    QPainter, QPainterPath, QColor, QPen, QFont, QFontDatabase, QKeySequence, QAction,
    QIcon, QCursor, QTextCursor, QTextFormat, QPolygonF, QSyntaxHighlighter, QTextCharFormat
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPlainTextEdit, QTextEdit, QLabel, QPushButton, QAbstractButton, QMenu,
    QFileDialog, QTabBar, QStackedWidget, QDialog
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

APP_NAME = "MyPythonIDE"
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
            (r"\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?)\b",
                                                        _fmt("#D19A66"), 0),
            (r"@\w+(?:\.\w+)*",                         _fmt("#E5C07B"), 0),
            (r"\bdef\s+(\w+)",                          _fmt("#61AFEF", bold=True), 1),
            (r"\bclass\s+(\w+)",                        _fmt("#E5C07B", bold=True), 1),
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
        x, y, h = r.left(), r.top(), r.height()
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
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self._gutter_w, cr.height()))
        vg = self.viewport().geometry()
        if self.cursor_fx.geometry() != vg:      # only snap if the viewport really moved
            self.cursor_fx.setGeometry(vg)
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

    def _validate_syntax(self):
        try:
            ast.parse(self.toPlainText())
            self.error_line, self.error_col = -1, 0
            self.last_status = (True, "No syntax errors")
        except SyntaxError as e:
            self.error_line = (e.lineno or 1) - 1
            self.error_col = max((e.offset or 1) - 1, 0)
            self.last_status = (False, f"Line {e.lineno}: {e.msg}")
        except Exception as e:
            self.error_line, self.error_col = -1, 0
            self.last_status = (False, str(e))
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
        stripped = before.rstrip()
        last = stripped[-1:] if stripped else ""

        if last in "([{" and last and after[:1] == PAIRS.get(last):
            cur.insertText("\n" + indent + "    " + "\n" + indent)
            cur.setPosition(cur.position() - len(indent) - 1)    # back onto the middle line
        else:
            extra = "    " if last in (":", "(", "[", "{") and last else ""
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
        else:
            return False                                    # a lone closer: plain insert

        cur.beginEditBlock()
        cur.insertText(ch + PAIRS[ch])
        cur.movePosition(QTextCursor.MoveOperation.Left)
        cur.endEditBlock()
        self.setTextCursor(cur)
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
# Titlebar: [icon menu] [tabs]  ........  [o min] [o max] [o close]
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


class TitleBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedHeight(TITLEBAR_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 12, 0)
        layout.setSpacing(8)

        # App icon = menu button (red circle until icon.ico exists)
        self.btn_app_icon = QPushButton(self)
        self.btn_app_icon.setFixedSize(22, 22)
        self.btn_app_icon.setToolTip("Menu")
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
            self.btn_app_icon.setToolTip(f"Menu  ({why}; looked in {APP_DIR})")

        # Tabs live in the titlebar
        self.tabbar = QTabBar(self)
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

        layout.addWidget(self.btn_app_icon)
        layout.addSpacing(4)
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

        self._setup_ui()
        self._build_menu()
        self._wire_signals()

        # Linux/macOS: no WM_NCHITTEST, so edge-resize is handled via an event filter
        if not IS_WIN:
            QApplication.instance().installEventFilter(self)

        if initial_path and initial_path.exists():
            self.open_file_path(initial_path)
        else:
            self.new_tab()

    def showEvent(self, event):
        super().showEvent(event)
        if IS_WIN and not self._glass_done:
            self._glass_done = True
            self.native_frame = enable_glass(int(self.winId()))

    def _setup_ui(self):
        self.resize(1000, 650)
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

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: rgba(255,255,255,0.08); }")
        self.stack = QStackedWidget()
        self.output = OutputConsole()
        self.output.hide()
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self.output)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        main_layout.addWidget(self.splitter, 1)

        self.status = QLabel("")
        main_layout.addWidget(self.status)
        self._style_status("#94A3B8")

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
        t.btn_app_icon.clicked.connect(self._show_menu)
        t.btn_min.clicked.connect(self.showMinimized)
        t.btn_max.clicked.connect(self._toggle_maximize)
        t.btn_close.clicked.connect(self.close)
        self.tabbar.tabCloseRequested.connect(self.close_tab)
        self.tabbar.currentChanged.connect(self._on_tab_changed)
        self.output.submitted.connect(self._send_input)

    def _show_menu(self):
        btn = self.titlebar.btn_app_icon
        self.menu.exec(btn.mapToGlobal(QPoint(0, btn.height() + 4)))

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---- status bar
    def _on_syntax(self, editor, ok, msg):
        if editor is self.current_editor():
            self._refresh_status()

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

    def new_tab(self, path: Path = None):
        editor = CodeEditor()
        editor.path = path
        self._uid += 1
        editor.uid = self._uid
        self.editors[editor.uid] = editor
        self.stack.addWidget(editor)

        if path and path.exists():
            editor.setPlainText(path.read_text(encoding="utf-8", errors="replace"))
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
        fname, _ = QFileDialog.getSaveFileName(self, "Save File As", "", "Python Files (*.py);;All Files (*)")
        if not fname:
            return False
        return self._write(editor, Path(fname))

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
        self.output.show()
        self.splitter.setSizes([450, 200])
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
        proc.readyReadStandardOutput.connect(
            lambda: self.output.write(bytes(proc.readAllStandardOutput()).decode(errors="replace")))
        proc.readyReadStandardError.connect(
            lambda: self.output.write(bytes(proc.readAllStandardError()).decode(errors="replace"), "err"))
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
            self.output.hide()

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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        radius = 0 if self.isMaximized() else WINDOW_RADIUS

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        painter.setClipPath(path)

        # translucent tint over the blurred backdrop (alpha must stay > 0,
        # fully transparent pixels become click-through on Windows)
        painter.fillRect(self.rect(), WORKSPACE_TINT)

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
    app = QApplication(sys.argv)
    CODE_FONT = load_fonts()
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    window = MainWindow(initial)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
