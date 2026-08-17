import ctypes
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from PyQt6.QtCore import QPoint, QProcess, QStandardPaths, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget
)

# constants
MODS = {"ctrl":"^", "alt":"!", "shift":"+", "win":"#"}
MOD_ORDER = ("ctrl", "alt", "shift", "win")
CLICK_TRIGGERS = {
    "click":"LButton","left click":"LButton","click left":"LButton",
    "click right":"RButton","right click":"RButton"
}
KEY_ALIASES = {
    "control": "ctrl",
    "return": "enter",
    "escape": "esc",
    "bs": "backspace",
    "del": "delete",
    "pagedown": "pgdn",
    "pageup": "pgup",
}
AHK_KEY_NAMES = {
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Esc",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pgup": "PgUp",
    "pgdn": "PgDn",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "insert": "Insert",
    "capslock": "CapsLock",
    "printscreen": "PrintScreen",
    "appskey": "AppsKey",
    "pause": "Pause",
    "wheelup": "WheelUp",
    "wheeldown": "WheelDown",
    "wheelleft": "WheelLeft",
    "wheelright": "WheelRight",
    "numpad0": "Numpad0",
    "numpad1": "Numpad1",
    "numpad2": "Numpad2",
    "numpad3": "Numpad3",
    "numpad4": "Numpad4",
    "numpad5": "Numpad5",
    "numpad6": "Numpad6",
    "numpad7": "Numpad7",
    "numpad8": "Numpad8",
    "numpad9": "Numpad9",
    "numpaddot": "NumpadDot",
    "numpadadd": "NumpadAdd",
    "numpadsub": "NumpadSub",
    "numpadmult": "NumpadMult",
    "numpaddiv": "NumpadDiv",
    "numpadenter": "NumpadEnter",
}
HOTKEY_SYMBOL_KEYS = {
    "+": "vkBB",
    "-": "vkBD",
}
MOD_RE = re.compile(r"(?i)^(ctrl|alt|shift|win)\s*(?:\+|-|\s)\s*(.*)$")
COORD_CLICK_RE = re.compile(
    r"(?i)^(click|rclick|mclick|x1click|x2click)\s*@\s*"
    r"(-?\d+)\s*,\s*(-?\d+)$"
)
WINDOW_CLICK_RE = re.compile(
    r"(?i)^(click|rclick|mclick|x1click|x2click)\s+in\s+\[([^\]]+)\]\s*@\s*"
    r"(-?\d+)\s*,\s*(-?\d+)$"
)
PROJECT_VERSION = 1
ITEM_KIND_ROLE = int(Qt.ItemDataRole.UserRole)
ITEM_ID_ROLE = ITEM_KIND_ROLE + 1


@dataclass
class Mapping:
    trigger: str
    steps: list[str]
    name: str = ""
    enabled: bool = True
    repeat: int = 1
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_project_data(self) -> dict:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "steps": list(self.steps),
            "enabled": self.enabled,
            "repeat": self.repeat,
        }

# helpers
def escape_ahk_string(text: str) -> str:
    return text.replace("`", "``").replace('"', '`"')

def _normalize_key_name(raw_key: str) -> str | None:
    key = raw_key.strip()
    if not key:
        return None
    if len(key) == 1:
        return key.lower() if key.isalnum() else key

    key = KEY_ALIASES.get(key.lower(), key.lower())
    if key in AHK_KEY_NAMES:
        return key
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        return key
    return None

def parse_combo(raw: str) -> tuple[list[str], str] | None:
    text = raw.strip()
    if not text:
        return None

    mods: list[str] = []
    while True:
        m = MOD_RE.match(text)
        if not m:
            break
        mods.append(m.group(1).lower())
        text = m.group(2)

    key = _normalize_key_name(text)
    if key is None:
        return None
    ordered_mods = [m for m in MOD_ORDER if m in mods]
    return ordered_mods, key

def _key_to_hotkey_ahk(key: str) -> str:
    if len(key) == 1:
        if key.isalnum():
            return key.lower()
        return HOTKEY_SYMBOL_KEYS.get(key, key)
    if key in AHK_KEY_NAMES:
        return AHK_KEY_NAMES[key]
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        return f"F{key[1:]}"
    return key

def _key_to_send_ahk(key: str) -> str:
    if len(key) == 1 and key.isalnum():
        return key.lower()
    if key in AHK_KEY_NAMES:
        return f'{{{AHK_KEY_NAMES[key]}}}'
    if re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        return f'{{F{key[1:]}}}'
    return f"{{{key}}}"

def normalize_hotkey(raw: str) -> str:
    return hotkey_to_ahk(raw).lower()

def _click_button(action: str) -> str:
    return {
        "rclick": "Right",
        "mclick": "Middle",
        "x1click": "X1",
        "x2click": "X2",
    }.get(action.lower(), "Left")

def to_ahk_step(token: str) -> str:
    t = token.strip()
    if m := WINDOW_CLICK_RE.fullmatch(t):
        action, executable, x, y = m.groups()
        target = escape_ahk_string(f"ahk_exe {executable.strip()}")
        return (
            f'ClickInWindow("{target}", {int(x)}, {int(y)}, '
            f'"{_click_button(action)}")'
        )
    if m := COORD_CLICK_RE.fullmatch(t):
        action, x, y = m.groups()
        return (
            f'ClickOnScreen({int(x)}, {int(y)}, '
            f'"{_click_button(action)}")'
        )
    if m := re.fullmatch(r"(?i)click\s+x(\d+)", t):
        return f"Click {int(m.group(1))}"
    if t.lower() in {"click right", "right click"}:
        return 'Click "Right"'
    if t.lower().startswith("click"):
        return t
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)\s*s", t, re.I):
        return f"Sleep {int(float(m.group(1))*1000)}"
    if t.startswith('"') and t.endswith('"'):
        return f'SendText "{escape_ahk_string(t[1:-1])}"'
    parsed = parse_combo(t)
    if not parsed:
        return f'Send "{escape_ahk_string(t)}"'
    mods, key = parsed
    mod_part = "".join(MODS[m] for m in mods)
    return f'Send "{mod_part}{_key_to_send_ahk(key)}"'

def hotkey_to_ahk(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    lower = text.lower()
    if lower in CLICK_TRIGGERS:
        return CLICK_TRIGGERS[lower]
    if re.fullmatch(r"click\s+x\d+", lower):
        return ""
    parsed = parse_combo(text)
    if not parsed:
        return ""
    mods, key = parsed
    return "".join(MODS[m] for m in mods) + _key_to_hotkey_ahk(key)

# small key-picker
class KeyPicker(QDialog):
    KEYS = (
        list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + [str(i) for i in range(10)]
        + ["+", "-", ".", "/"]
        + ["Enter","Tab","Esc","Space","Backspace","Delete",
           "Up","Down","Left","Right","Home","End","PgUp","PgDn","Pause"]
        + ["Numpad0","Numpad1","Numpad2","Numpad3","Numpad4",
           "Numpad5","Numpad6","Numpad7","Numpad8","Numpad9",
           "NumpadDot","NumpadAdd","NumpadSub","NumpadMult",
           "NumpadDiv","NumpadEnter"]
    )
    def __init__(self, parent=None, allow_multi_click=True):
        super().__init__(parent)
        self.setWindowTitle("Pick key or click")
        self.result = ""
        lay = QVBoxLayout(self)

        modrow = QHBoxLayout()
        self.c = QCheckBox("Ctrl"); self.a = QCheckBox("Alt")
        self.s = QCheckBox("Shift"); self.w = QCheckBox("Win")
        for chk in (self.c,self.a,self.s,self.w):
            modrow.addWidget(chk)
        click_choices = [("Click", "Click")]
        if allow_multi_click:
            click_choices.extend([
                ("Click x2", "Click x2"),
                ("Click x3", "Click x3"),
            ])
        click_choices.append(("RClick", "Click right"))
        for label,cmd in click_choices:
            b = QPushButton(label)
            b.setFixedWidth(60)
            b.setToolTip(f"Add {cmd} action")
            b.clicked.connect(lambda _, k=cmd: self._picked(k))
            modrow.addWidget(b)
        lay.addLayout(modrow)

        normal_keys = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + [str(i) for i in range(10)] \
            + ["+","-",".","/","Enter","Tab","Esc","Space","Backspace","Delete",
               "Up","Down","Left","Right","Home","End","PgUp","PgDn","Pause"]
        numpad_keys = ["Numpad0","Numpad1","Numpad2","Numpad3","Numpad4",
                       "Numpad5","Numpad6","Numpad7","Numpad8","Numpad9",
                       "NumpadDot","NumpadAdd","NumpadSub","NumpadMult",
                       "NumpadDiv","NumpadEnter"]

        grid = QGridLayout()
        main_label = QLabel("Main")
        main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(main_label,0,0,1,10)
        row,col = 1,0
        for k in normal_keys:
            btn=QPushButton(k); btn.setFixedWidth(44)
            btn.clicked.connect(lambda _, kk=k: self._picked(kk))
            grid.addWidget(btn,row,col)
            col+=1
            if col==10: col=0; row+=1

        row+=1
        label=QLabel("NumPad")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(label,row,0,1,10)
        row+=1; col=0
        DISPLAY={"NumpadAdd":"+","NumpadSub":"-","NumpadMult":"*",
                 "NumpadDiv":"/","NumpadDot":".","NumpadEnter":"Enter"}
        for k in numpad_keys:
            face=DISPLAY.get(k,k.replace("Numpad",""))
            btn=QPushButton(face); btn.setFixedWidth(44)
            btn.clicked.connect(lambda _, kk=k: self._picked(kk))
            grid.addWidget(btn,row,col)
            col+=1
            if col==10: col=0; row+=1

        lay.addLayout(grid)

    def _picked(self,key):
        if key.lower().startswith("click"):
            self.result = key
        else:
            mods=[m for m,chk in
                  (("Ctrl",self.c),("Alt",self.a),
                   ("Shift",self.s),("Win",self.w))
                  if chk.isChecked()]
            self.result = "+".join(mods+[key]) if mods else key
        self.accept()

# main window
class KeyMapper(QMainWindow):
    def __init__(self):
        super().__init__()
        self.maps: list[Mapping] = []
        self.controls = {"toggle": "", "exit": "", "info": ""}
        self.control_items = {}
        self.editing_mapping_id: str | None = None
        self.project_path: Path | None = None
        self.project_dirty = False
        self._ignore_control_changes = False
        recovery_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        self.recovery_path = Path(
            recovery_root or tempfile.gettempdir()
        ) / "recovery.pyahk.json"
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(750)
        self.autosave_timer.timeout.connect(self._write_recovery)

        self.runner = QProcess(self)
        self.runner.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.runner.finished.connect(self._run_finished)
        self.runner.errorOccurred.connect(self._run_error)
        self._runner_temp: Path | None = None
        self._run_stop_requested = False
        self._run_error_shown = False

        self.builder = QProcess(self)
        self.builder.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.builder.started.connect(self._build_started)
        self.builder.finished.connect(self._build_finished)
        self.builder.errorOccurred.connect(self._build_error)
        self.compiler_installer = QProcess(self)
        self.compiler_installer.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self.compiler_installer.finished.connect(self._installer_finished)
        self.compiler_installer.errorOccurred.connect(self._installer_error)
        self.compiler_poll_timer = QTimer(self)
        self.compiler_poll_timer.setInterval(500)
        self.compiler_poll_timer.timeout.connect(self._poll_compiler_install)
        self._compiler_install_deadline = 0.0
        self._build_temp: Path | None = None
        self._build_output: Path | None = None
        self._build_error_shown = False
        self._build_cancel_requested = False
        self._closing = False

        self.keyboard_listener = None
        self.mouse_listener = None
        self._recorder_keyboard = None
        self._recorder_mouse = None
        self._recording = False
        self._recording_events = []
        self._recording_modifiers = set()
        self._recording_lock = threading.Lock()
        self._record_relative_clicks = True

        self._create_project_menu()

        root=QWidget(); self.setCentralWidget(root)
        V=QVBoxLayout(root)

        # Trigger hot-key
        self.trigger=QLineEdit()
        self.trigger.setReadOnly(True)
        self.trigger.setPlaceholderText("Choose hotkey to map on")
        btn_t=QPushButton("⌨"); btn_t.setFixedWidth(28)
        btn_t.setToolTip("Choose hotkey to map on")
        btn_t.clicked.connect(lambda: self._pick_into(self.trigger))
        old_tp=self.trigger.mousePressEvent
        def triggerClicked(ev):
            self._pick_into(self.trigger); old_tp(ev)
        self.trigger.mousePressEvent=triggerClicked
        row=QHBoxLayout()
        row.addWidget(self.trigger); row.addWidget(btn_t)
        V.addLayout(row)

        # Sequence builder
        seqrow=QHBoxLayout()
        self.seq=QListWidget()
        self.seq.setMaximumHeight(120)
        self.seq.setToolTip("Mapping Functions")
        self.seq.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.seq.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.seq.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.seq.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.seq.customContextMenuRequested.connect(self._seq_context_menu)
        col=QVBoxLayout()
        for lab,fn,tip in [
            ("⌨",self._add_key,"Add keystroke"),
            ("⏱",self._add_delay,"Add delay"),
            ("🖉",self._add_text,"Add text")
        ]:
            b=QPushButton(lab); b.setFixedWidth(28)
            b.setToolTip(tip); b.clicked.connect(fn)
            col.addWidget(b)
        seqrow.addWidget(self.seq); seqrow.addLayout(col)
        V.addLayout(seqrow)

        record_row = QHBoxLayout()
        self.record_status = QLabel("")
        self.record_status.hide()
        self.record_button = QPushButton("Record", clicked=self.start_recording)
        self.record_button.setToolTip("Record keyboard and mouse actions")
        self.stop_record_button = QPushButton("Stop", clicked=self.stop_recording)
        self.stop_record_button.setToolTip("Stop recording and add captured actions")
        self.stop_record_button.setEnabled(False)
        repeat_label = QLabel("Repeat")
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 999)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setSuffix("x")
        self.repeat_spin.setToolTip("Number of times to run this mapping")
        self.relative_clicks = QCheckBox("Relative clicks")
        self.relative_clicks.setChecked(True)
        self.relative_clicks.setToolTip(
            "Record clicks relative to the target application window"
        )
        record_row.addWidget(repeat_label)
        record_row.addWidget(self.repeat_spin)
        record_row.addWidget(self.relative_clicks)
        record_row.addWidget(self.record_status)
        record_row.addStretch()
        record_row.addWidget(self.record_button)
        record_row.addWidget(self.stop_record_button)
        V.addLayout(record_row)

        # Toggle + Exit + Info row
        te_row = QHBoxLayout()
        FIELD_WIDTH = 116

        def make_control(field_name, placeholder):
            fld = QLineEdit()
            fld.setFixedWidth(FIELD_WIDTH)
            fld.setPlaceholderText(placeholder)
            fld.setClearButtonEnabled(True)
            fld.setToolTip(placeholder)
            # click -> key picker
            orig = fld.mousePressEvent

            def on_mouse(ev):
                self._pick_into(fld)
                orig(ev)

            fld.mousePressEvent = on_mouse
            # any keypress -> key picker
            fld.keyPressEvent = lambda ev: self._pick_into(fld)
            # clear "x" -> remove mapping
            fld.textChanged.connect(lambda txt, n=field_name:
                                    self._on_control_cleared(n, txt))
            return fld

        # Instantiate the three control fields and their pick-buttons
        self.toggle = make_control("toggle", "Toggle hot-key")
        btn_toggle = QPushButton("⌨")
        btn_toggle.setFixedWidth(FIELD_WIDTH // 4)
        btn_toggle.setToolTip("Pick toggle hotkey")
        btn_toggle.clicked.connect(lambda: self._pick_into(self.toggle))

        self.exit = make_control("exit", "Exit hot-key")
        btn_exit = QPushButton("⌨")
        btn_exit.setFixedWidth(FIELD_WIDTH // 4)
        btn_exit.setToolTip("Pick exit hotkey")
        btn_exit.clicked.connect(lambda: self._pick_into(self.exit))

        self.info = make_control("info", "Info hot-key")
        btn_info = QPushButton("⌨")
        btn_info.setFixedWidth(FIELD_WIDTH // 4)
        btn_info.setToolTip("Pick info hotkey")
        btn_info.clicked.connect(lambda: self._pick_into(self.info))

        # Add them to the row
        te_row.addWidget(self.toggle);
        te_row.addWidget(btn_toggle)
        te_row.addSpacing(10)
        te_row.addWidget(self.exit);
        te_row.addWidget(btn_exit)
        te_row.addSpacing(10)
        te_row.addWidget(self.info);
        te_row.addWidget(btn_info)
        V.addLayout(te_row)

        # Add / Reset
        self.add_button=QPushButton("Add mapping",clicked=self.add_mapping)
        self.add_button.setFixedWidth(110)
        self.cancel_edit_button=QPushButton("Cancel edit",clicked=self._cancel_mapping_edit)
        self.cancel_edit_button.setFixedWidth(100)
        self.cancel_edit_button.hide()
        reset=QPushButton("Reset",clicked=self._reset_all)
        reset.setFixedWidth(100)
        left_ctrl=QHBoxLayout()
        left_ctrl.addStretch(); left_ctrl.addWidget(self.add_button)
        left_ctrl.addWidget(self.cancel_edit_button); left_ctrl.addStretch()
        right_ctrl=QHBoxLayout()
        right_ctrl.addStretch(); right_ctrl.addWidget(reset); right_ctrl.addStretch()
        ctrl=QHBoxLayout()
        ctrl.addLayout(left_ctrl,1); ctrl.addLayout(right_ctrl,1)
        V.addLayout(ctrl)

        V.addSpacing(8)
        sep=QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:grey;"); sep.setFixedHeight(2)
        V.addWidget(sep)

        # Mappings & Preview
        mapping_label=QLabel("Key Mappings")
        mapping_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label=QLabel("AutoHotKey Script")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.maplist=QListWidget()
        self.maplist.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.maplist.customContextMenuRequested.connect(self._maplist_context_menu)
        self.maplist.itemDoubleClicked.connect(self._edit_mapping_item)

        self.preview=QTextEdit(); self.preview.setReadOnly(True)

        left=QVBoxLayout(); left.addWidget(mapping_label); left.addWidget(self.maplist)
        right=QVBoxLayout(); right.addWidget(preview_label); right.addWidget(self.preview)
        disp=QHBoxLayout(); disp.setStretch(0,1); disp.setStretch(1,1)
        disp.addLayout(left); disp.addLayout(right)
        V.addLayout(disp)

        V.addSpacing(6)
        sep=QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:grey;"); sep.setFixedHeight(2)
        V.addWidget(sep)
        V.addSpacing(4)

        hb=QHBoxLayout()
        self.run_button=QPushButton("Run",clicked=self.run_script)
        self.run_button.setToolTip("Run the current script for testing")
        self.run_button.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.stop_run_button=QPushButton("Stop",clicked=self.stop_script)
        self.stop_run_button.setToolTip("Stop the running test script")
        self.stop_run_button.setEnabled(False)
        self.stop_run_button.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        save=QPushButton("Save .ahk",clicked=self.save_ahk)
        save.setToolTip("Save script as .ahk file")
        save.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.build_button=QPushButton("Build .exe",clicked=self.build_exe)
        self.build_button.setToolTip("Compile script to executable")
        self.build_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed
        )
        hb.addWidget(self.run_button,1); hb.addWidget(self.stop_run_button,1)
        hb.addWidget(save,1); hb.addWidget(self.build_button,1)
        V.addLayout(hb)

        self.statusBar().showMessage("Ready")
        self._update_window_title()
        self.resize(620,600)
        QTimer.singleShot(0, self._offer_recovery)

    def _create_project_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        new_action = QAction("&New Project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        file_menu.addAction(new_action)

        open_action = QAction("&Open Project...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        file_menu.addAction(open_action)

        self.save_project_action = QAction("&Save Project", self)
        self.save_project_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_project_action.triggered.connect(self.save_project)
        file_menu.addAction(self.save_project_action)

        save_as_action = QAction("Save Project &As...", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(lambda: self.save_project(save_as=True))
        file_menu.addAction(save_as_action)

    def _update_window_title(self):
        name = self.project_path.name if self.project_path else "Untitled"
        dirty = " *" if self.project_dirty else ""
        self.setWindowTitle(f"pyAHK - {name}{dirty}")

    def _mark_dirty(self):
        if not self.project_dirty:
            self.project_dirty = True
            self._update_window_title()
        self.autosave_timer.start()

    def _has_project_content(self):
        return bool(self.maps or any(self.controls.values()))

    def _has_generated_content(self):
        return bool(any(mapping.enabled for mapping in self.maps) or
                    any(self.controls.values()))

    def _project_data(self):
        return {
            "version": PROJECT_VERSION,
            "controls": dict(self.controls),
            "mappings": [mapping.to_project_data() for mapping in self.maps],
        }

    def _maybe_save_project(self):
        if not self.project_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved Project",
            "Save changes to the current project?",
            QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self.save_project()
        return choice == QMessageBox.StandardButton.Discard

    def save_project(self, checked=False, save_as=False):
        del checked
        path = self.project_path
        if save_as or path is None:
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "Save pyAHK Project",
                str(path) if path else "keymap.pyahk.json",
                "pyAHK Project (*.pyahk.json *.json)",
            )
            if not selected:
                return False
            path = Path(selected)
            if not path.name.lower().endswith(".json"):
                path = Path(str(path) + ".pyahk.json")

        try:
            payload = json.dumps(
                self._project_data(), ensure_ascii=False, indent=2
            ) + "\n"
            path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(
                self, "Save Failed", f"Could not save project:\n{exc}"
            )
            return False

        self.project_path = path
        self.project_dirty = False
        self._clear_recovery()
        self._update_window_title()
        self.statusBar().showMessage(f"Saved {path.name}", 3000)
        return True

    def _decode_project(self, data):
        if not isinstance(data, dict):
            raise ValueError("The project root must be an object.")
        if data.get("version") != PROJECT_VERSION:
            raise ValueError("This project version is not supported.")

        raw_controls = data.get("controls", {})
        if not isinstance(raw_controls, dict):
            raise ValueError("Project controls are invalid.")
        controls = {}
        used_hotkeys = {}
        for field_name in ("toggle", "exit", "info"):
            value = raw_controls.get(field_name, "")
            if not isinstance(value, str):
                raise ValueError(f"The {field_name} hotkey must be text.")
            value = value.strip()
            if value:
                canonical = normalize_hotkey(value)
                if not canonical:
                    raise ValueError(f'Invalid {field_name} hotkey: "{value}".')
                if canonical in used_hotkeys:
                    raise ValueError(f'Duplicate hotkey: "{value}".')
                used_hotkeys[canonical] = field_name
            controls[field_name] = value

        raw_mappings = data.get("mappings", [])
        if not isinstance(raw_mappings, list):
            raise ValueError("Project mappings must be a list.")
        mappings = []
        for index, raw_mapping in enumerate(raw_mappings, start=1):
            if not isinstance(raw_mapping, dict):
                raise ValueError(f"Mapping {index} is invalid.")
            trigger = raw_mapping.get("trigger", "")
            steps = raw_mapping.get("steps", [])
            name = raw_mapping.get("name", "")
            enabled = raw_mapping.get("enabled", True)
            repeat = raw_mapping.get("repeat", 1)
            if not isinstance(trigger, str) or not normalize_hotkey(trigger):
                raise ValueError(f"Mapping {index} has an invalid trigger.")
            if (not isinstance(steps, list) or not steps or
                    not all(isinstance(step, str) and step for step in steps)):
                raise ValueError(f"Mapping {index} has invalid steps.")
            if (not isinstance(name, str) or type(enabled) is not bool or
                    type(repeat) is not int or not 1 <= repeat <= 999):
                raise ValueError(f"Mapping {index} has invalid properties.")
            canonical = normalize_hotkey(trigger)
            if canonical in used_hotkeys:
                raise ValueError(f'Duplicate hotkey: "{trigger}".')
            used_hotkeys[canonical] = f"mapping {index}"
            mappings.append(Mapping(
                trigger=trigger.strip(),
                steps=list(steps),
                name=name.strip(),
                enabled=enabled,
                repeat=repeat,
            ))
        return controls, mappings

    def open_project(self):
        if not self._maybe_save_project():
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open pyAHK Project",
            "",
            "pyAHK Project (*.pyahk.json *.json)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            controls, mappings = self._decode_project(data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            QMessageBox.critical(
                self, "Open Failed", f"Could not open project:\n{exc}"
            )
            return

        self._load_project_state(controls, mappings)
        self.project_path = path
        self.project_dirty = False
        self._clear_recovery()
        self._update_window_title()
        self.statusBar().showMessage(f"Opened {path.name}", 3000)

    def _load_project_state(self, controls, mappings):
        self._stop_runner_now()
        self._ignore_control_changes = True
        try:
            self.controls = dict(controls)
            self.toggle.setText(self.controls["toggle"])
            self.exit.setText(self.controls["exit"])
            self.info.setText(self.controls["info"])
        finally:
            self._ignore_control_changes = False
        self.maps = list(mappings)
        self._cancel_mapping_edit()
        self._rebuild_maplist()
        self._refresh()

    def new_project(self):
        if not self._maybe_save_project():
            return
        self._load_project_state(
            {"toggle": "", "exit": "", "info": ""}, []
        )
        self.project_path = None
        self.project_dirty = False
        self._clear_recovery()
        self._update_window_title()
        self.statusBar().showMessage("New project", 3000)

    def _write_recovery(self):
        if not self.project_dirty:
            return
        data = self._project_data()
        data["recovery"] = {
            "project_path": str(self.project_path) if self.project_path else ""
        }
        temporary = self.recovery_path.with_suffix(
            self.recovery_path.suffix + ".tmp"
        )
        try:
            self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.recovery_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.statusBar().showMessage("Could not update recovery autosave", 4000)

    def _offer_recovery(self):
        if not self.recovery_path.exists() or self.project_dirty:
            return
        try:
            data = json.loads(self.recovery_path.read_text(encoding="utf-8"))
            controls, mappings = self._decode_project(data)
        except (OSError, json.JSONDecodeError, ValueError):
            self._clear_recovery()
            return

        choice = QMessageBox.question(
            self,
            "Recover Project",
            "Recover the project from the last unexpected shutdown?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            self._clear_recovery()
            return

        self._load_project_state(controls, mappings)
        recovery = data.get("recovery", {})
        raw_path = recovery.get("project_path", "") if isinstance(
            recovery, dict
        ) else ""
        self.project_path = Path(raw_path) if raw_path else None
        self.project_dirty = True
        self._update_window_title()
        self.statusBar().showMessage("Recovered unsaved project", 5000)

    def _clear_recovery(self):
        self.autosave_timer.stop()
        try:
            self.recovery_path.unlink(missing_ok=True)
            self.recovery_path.with_suffix(
                self.recovery_path.suffix + ".tmp"
            ).unlink(missing_ok=True)
        except OSError:
            pass

    def _pick_into(self,lineedit):
        dlg=KeyPicker(self, allow_multi_click=False)
        if dlg.exec():
            lineedit.setText(dlg.result)

    def _on_control_cleared(self, control_name: str, text: str):
        if self._ignore_control_changes or text:
            return
        if self.controls.get(control_name):
            self.controls[control_name] = ""
            self._rebuild_maplist()
            self._refresh()
            self._mark_dirty()

    def _add_key(self):
        sel=self.seq.selectedItems()
        dlg=KeyPicker(self)
        if dlg.exec():
            if len(sel)==1:
                sel[0].setText(dlg.result)
                self.seq.clearSelection()
            else:
                self.seq.addItem(dlg.result)

    def _add_delay(self):
        sel=self.seq.selectedItems()
        val,ok=QInputDialog.getDouble(self,"Delay (seconds)","Seconds:",1.0,0.0,3600.0,2)
        if ok:
            txt=f"{val:g} s"
            if len(sel)==1:
                sel[0].setText(txt)
                self.seq.clearSelection()
            else:
                self.seq.addItem(txt)

    def _add_text(self):
        sel=self.seq.selectedItems()
        txt,ok=QInputDialog.getText(self,"Literal text","Text to send:")
        if ok and txt:
            lit=f'"{txt}"'
            if len(sel)==1:
                sel[0].setText(lit)
                self.seq.clearSelection()
            else:
                self.seq.addItem(lit)

    def _seq_context_menu(self, pos):
        sels = self.seq.selectedItems()
        if not sels:
            return

        menu = QMenu(self)
        multi = len(sels) > 1
        if not multi:
            editA = menu.addAction("Edit")
        repA = menu.addAction("Replicate")
        remA = menu.addAction("Remove")
        act = menu.exec(self.seq.mapToGlobal(pos))

        if act == remA:
            rows = sorted({self.seq.row(it) for it in sels}, reverse=True)
            for r in rows:
                # only remove if the index is still valid
                if 0 <= r < self.seq.count():
                    self.seq.takeItem(r)

        elif act == repA:
            for it in sels:
                self.seq.addItem(it.text())

        elif not multi and act == editA:
            it=sels[0]; txt=it.text()
            # detect type
            if m:=re.fullmatch(r"(\d+(?:\.\d+)?)\s*s",txt):
                val=float(m.group(1))
                new,ok=QInputDialog.getDouble(self,"Edit Delay","Seconds:",val,0.0,3600.0,2)
                if ok:
                    it.setText(f"{new:g} s")
                    self.seq.clearSelection()
            elif txt.startswith('"') and txt.endswith('"'):
                inner=txt[1:-1]
                new,ok=QInputDialog.getText(self,"Edit Text","Text to send:",text=inner)
                if ok:
                    it.setText(f'"{new}"')
                    self.seq.clearSelection()
            elif (window_match := WINDOW_CLICK_RE.fullmatch(txt)) or (
                    screen_match := COORD_CLICK_RE.fullmatch(txt)):
                if window_match:
                    action, executable, x, y = window_match.groups()
                else:
                    action, x, y = screen_match.groups()
                    executable = None
                new,ok=QInputDialog.getText(
                    self, "Edit Click Position", "X, Y:", text=f"{x}, {y}"
                )
                if ok:
                    coords = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", new)
                    if not coords:
                        QMessageBox.warning(
                            self, "Invalid Position", "Enter the position as X, Y."
                        )
                    else:
                        prefix = (
                            f"{action} in [{executable}]" if executable else action
                        )
                        it.setText(
                            f"{prefix} @ {int(coords.group(1))}, "
                            f"{int(coords.group(2))}"
                        )
                        self.seq.clearSelection()
            else:
                dlg=KeyPicker(self)
                if dlg.exec():
                    it.setText(dlg.result)
                    self.seq.clearSelection()

    def add_mapping(self):
        fields = {
            "Trigger": self.trigger.text().strip(),
            "Toggle": self.toggle.text().strip(),
            "Exit": self.exit.text().strip(),
            "Info": self.info.text().strip(),
        }
        normalized = {}
        for name, value in fields.items():
            if not value:
                continue
            canonical = normalize_hotkey(value)
            if not canonical:
                QMessageBox.warning(
                    self,
                    "Invalid Hotkey",
                    f'{name} hotkey "{value}" is not a valid key combination.'
                )
                return
            normalized[name] = canonical

        dupes = [k for k, v in Counter(normalized.values()).items() if v > 1]
        if dupes:
            dupe_canonical = dupes[0]
            dupe_raw = next(
                value for name, value in fields.items()
                if normalized.get(name) == dupe_canonical
            )
            QMessageBox.warning(
                self,
                "Hotkey Conflict",
                f'Hotkey "{dupe_raw}" is assigned more than once.'
            )
            return

        existing_trigs = {}
        for mapping in self.maps:
            if mapping.uid == self.editing_mapping_id:
                continue
            canonical = normalize_hotkey(mapping.trigger)
            if canonical:
                existing_trigs[canonical] = mapping.trigger

        for name in ("Toggle", "Exit", "Info"):
            key = fields[name]
            if key and normalized.get(name) in existing_trigs:
                QMessageBox.warning(
                    self,
                    "Hotkey Conflict",
                    f'{name} hotkey "{key}" is already assigned to another mapping.'
                )
                return

        trig = fields["Trigger"]
        trig_norm = normalized.get("Trigger")
        steps = [self.seq.item(i).text() for i in range(self.seq.count())]
        if bool(trig) != bool(steps):
            QMessageBox.warning(
                self,
                "Incomplete Mapping",
                "Choose both a trigger hotkey and at least one action."
            )
            return
        if trig and steps and trig_norm in existing_trigs:
            QMessageBox.warning(
                self,
                "Hotkey Conflict",
                f'Trigger hotkey "{trig}" is already mapped to another sequence.'
            )
            return

        changed = False
        for field_name, display_name in (
                ("toggle", "Toggle"), ("exit", "Exit"), ("info", "Info")):
            value = fields[display_name]
            if self.controls[field_name] != value:
                self.controls[field_name] = value
                changed = True

        mapping_changed = False
        if trig and steps:
            repeat = self.repeat_spin.value()
            if self.editing_mapping_id:
                mapping = self._mapping_by_uid(self.editing_mapping_id)
                if mapping:
                    mapping.trigger = trig
                    mapping.steps = steps
                    mapping.repeat = repeat
                    mapping_changed = True
            else:
                self.maps.append(Mapping(
                    trigger=trig, steps=steps, repeat=repeat
                ))
                mapping_changed = True
            self._cancel_mapping_edit()

        changed = changed or mapping_changed
        self._rebuild_maplist()
        self._refresh()
        if changed:
            self._mark_dirty()

    def _mapping_by_uid(self, uid):
        return next((mapping for mapping in self.maps if mapping.uid == uid), None)

    def _mapping_label(self, mapping):
        label = f"{mapping.trigger} -> {', '.join(mapping.steps)}"
        if mapping.repeat > 1:
            label += f" [x{mapping.repeat}]"
        if mapping.name:
            label = f"{mapping.name}: {label}"
        return label if mapping.enabled else f"[Disabled] {label}"

    def _rebuild_maplist(self):
        self.maplist.clear()
        self.control_items.clear()
        for field_name in ("toggle", "exit", "info"):
            value = self.controls[field_name]
            if not value:
                continue
            item = QListWidgetItem(f"{field_name.title()} -> {value}")
            item.setData(ITEM_KIND_ROLE, "control")
            item.setData(ITEM_ID_ROLE, field_name)
            self.maplist.addItem(item)
            self.control_items[field_name] = item
        for mapping in self.maps:
            item = QListWidgetItem(self._mapping_label(mapping))
            item.setData(ITEM_KIND_ROLE, "mapping")
            item.setData(ITEM_ID_ROLE, mapping.uid)
            if not mapping.enabled:
                item.setForeground(QColor("#777777"))
            self.maplist.addItem(item)

    def _edit_mapping_item(self, item):
        if item.data(ITEM_KIND_ROLE) != "mapping":
            return
        mapping = self._mapping_by_uid(item.data(ITEM_ID_ROLE))
        if not mapping:
            return
        self.editing_mapping_id = mapping.uid
        self.trigger.setText(mapping.trigger)
        self.seq.clear()
        self.seq.addItems(mapping.steps)
        self.repeat_spin.setValue(mapping.repeat)
        self.add_button.setText("Update mapping")
        self.cancel_edit_button.show()
        self.statusBar().showMessage("Editing mapping", 3000)

    def _cancel_mapping_edit(self):
        self.editing_mapping_id = None
        self.trigger.clear()
        self.seq.clear()
        if hasattr(self, "repeat_spin"):
            self.repeat_spin.setValue(1)
        if hasattr(self, "add_button"):
            self.add_button.setText("Add mapping")
            self.cancel_edit_button.hide()

    def _duplicate_mapping(self, mapping):
        picker = KeyPicker(self, allow_multi_click=False)
        picker.setWindowTitle("Choose hotkey for duplicate")
        if not picker.exec():
            return
        trigger = picker.result
        canonical = normalize_hotkey(trigger)
        used = {
            normalize_hotkey(existing.trigger) for existing in self.maps
        } | {
            normalize_hotkey(value) for value in self.controls.values() if value
        }
        if not canonical or canonical in used:
            QMessageBox.warning(
                self, "Hotkey Conflict", f'Hotkey "{trigger}" is already assigned.'
            )
            return
        name = f"{mapping.name} Copy".strip() if mapping.name else ""
        self.maps.append(Mapping(
            trigger=trigger,
            steps=list(mapping.steps),
            name=name,
            enabled=mapping.enabled,
            repeat=mapping.repeat,
        ))
        self._rebuild_maplist()
        self._refresh()
        self._mark_dirty()

    def _rename_mapping(self, mapping):
        name, ok = QInputDialog.getText(
            self, "Rename Mapping", "Name:", text=mapping.name
        )
        if ok and name.strip() != mapping.name:
            mapping.name = name.strip()
            self._rebuild_maplist()
            self._refresh()
            self._mark_dirty()

    def _maplist_context_menu(self, pos):
        item = self.maplist.itemAt(pos)
        if not item:
            return

        kind = item.data(ITEM_KIND_ROLE)
        identifier = item.data(ITEM_ID_ROLE)
        menu = QMenu(self)
        if kind == "control":
            remove_action = menu.addAction("Remove mapping")
            if menu.exec(self.maplist.mapToGlobal(pos)) == remove_action:
                getattr(self, identifier).clear()
            return

        mapping = self._mapping_by_uid(identifier)
        if not mapping:
            return
        edit_action = menu.addAction("Edit")
        duplicate_action = menu.addAction("Duplicate")
        rename_action = menu.addAction("Rename")
        toggle_action = menu.addAction("Disable" if mapping.enabled else "Enable")
        menu.addSeparator()
        remove_action = menu.addAction("Remove mapping")
        action = menu.exec(self.maplist.mapToGlobal(pos))

        if action == edit_action:
            self._edit_mapping_item(item)
        elif action == duplicate_action:
            self._duplicate_mapping(mapping)
        elif action == rename_action:
            self._rename_mapping(mapping)
        elif action == toggle_action:
            mapping.enabled = not mapping.enabled
            self._rebuild_maplist()
            self._refresh()
            self._mark_dirty()
        elif action == remove_action:
            if self.editing_mapping_id == mapping.uid:
                self._cancel_mapping_edit()
            self.maps = [entry for entry in self.maps if entry.uid != mapping.uid]
            self._rebuild_maplist()
            self._refresh()
            self._mark_dirty()


    def _reset_all(self):
        ans=QMessageBox.question(self,"Confirm Reset",
                                 "Clear all mappings and inputs?",
                                 QMessageBox.StandardButton.Yes|
                                 QMessageBox.StandardButton.No)
        if ans==QMessageBox.StandardButton.Yes:
            self._load_project_state(
                {"toggle": "", "exit": "", "info": ""}, []
            )
            self._mark_dirty()

    def _refresh(self):
        if not self._has_generated_content():
            self.preview.clear()
            return

        lines=[
            "; generated by KeyMapper",
            "#Requires AutoHotkey v2.0+",
            "",
            "global scriptEnabled := true",
            "global infoVisible := false",
            ""
        ]
        enabled_mappings = [mapping for mapping in self.maps if mapping.enabled]
        all_steps = [
            step for mapping in enabled_mappings for step in mapping.steps
        ]
        if any(COORD_CLICK_RE.fullmatch(step) for step in all_steps):
            lines.extend([
                'ClickOnScreen(x, y, button := "Left") {',
                '    CoordMode "Mouse", "Screen"',
                "    Click x, y, button",
                "}",
                "",
            ])
        if any(WINDOW_CLICK_RE.fullmatch(step) for step in all_steps):
            lines.extend([
                'ClickInWindow(target, x, y, button := "Left") {',
                "    if !WinExist(target)",
                "        return",
                "    if !WinActive(target) {",
                "        WinActivate target",
                "        if !WinWaitActive(target,, 2)",
                "            return",
                "    }",
                '    CoordMode "Mouse", "Client"',
                "    Click x, y, button",
                "}",
                "",
            ])

        if t := self.controls["toggle"]:
            th=hotkey_to_ahk(t)
            lines+=[
                "global toggleStatusGui := 0",
                "",
                "HideToggleStatus(*) {",
                "    global toggleStatusGui",
                "    SetTimer(HideToggleStatus, 0)",
                "    if IsObject(toggleStatusGui) {",
                "        toggleStatusGui.Destroy()",
                "        toggleStatusGui := 0",
                "    }",
                "}",
                "",
                "ShowToggleStatus(message, color) {",
                "    global toggleStatusGui",
                "    HideToggleStatus()",
                '    toggleStatusGui := Gui("+AlwaysOnTop -Caption +ToolWindow +E0x20")',
                "    toggleStatusGui.BackColor := color",
                "    toggleStatusGui.MarginX := 14",
                "    toggleStatusGui.MarginY := 8",
                '    toggleStatusGui.SetFont("s10 bold cWhite", "Segoe UI")',
                '    toggleStatusGui.AddText("Center w110 h24 +0x200", message)',
                '    toggleStatusGui.Show("NoActivate AutoSize xCenter y20")',
                "    SetTimer(HideToggleStatus, -1000)",
                "}",
                "",
                f"{th}:: {{",
                "    global scriptEnabled",
                "    scriptEnabled := !scriptEnabled",
                '    ShowToggleStatus(scriptEnabled ? "ENABLED" : "DISABLED", '
                'scriptEnabled ? "2E7D32" : "C62828")',
                "}",
                ""
            ]
        if e := self.controls["exit"]:
            lines.append(f"{hotkey_to_ahk(e)}::ExitApp")
            lines.append("")
        if i := self.controls["info"]:
            ih=hotkey_to_ahk(i)
            info_lines=[]
            for mapping in enabled_mappings:
                clean=[s[1:-1] if s.startswith('"') and s.endswith('"') else s
                       for s in mapping.steps]
                prefix = f"{mapping.name}: " if mapping.name else ""
                repeat = f" [x{mapping.repeat}]" if mapping.repeat > 1 else ""
                info_lines.append(
                    escape_ahk_string(
                        f"{prefix}{mapping.trigger} -> {', '.join(clean)}{repeat}"
                    )
                )
            tip_body = "`n".join(info_lines) if info_lines else "(no mappings)"
            tip = "Info:`n" + tip_body
            lines+=[
                "HideInfo(*) {",
                "    global infoVisible",
                "    ToolTip()",
                "    infoVisible := false",
                "}",
                "",
                f"{ih}:: {{",
                "    global infoVisible",
                "    if infoVisible {",
                "        SetTimer(HideInfo, 0)",
                "        HideInfo()",
                "    } else {",
                f'        ToolTip("{tip}")',
                "        infoVisible := true",
                "        SetTimer(HideInfo, -5000)",
                "    }",
                "}",
                ""
            ]
        lines.append("#HotIf scriptEnabled")
        for mapping in enabled_mappings:
            ah=hotkey_to_ahk(mapping.trigger)
            body=[to_ahk_step(s) for s in mapping.steps]
            if len(body)==1 and mapping.repeat == 1:
                lines.append(f"{ah}:: {body[0]}")
            else:
                lines.append(f"{ah}::")
                lines.append("{")
                if mapping.repeat > 1:
                    lines.append(f"    Loop {mapping.repeat} {{")
                    for b in body:
                        lines.append(f"        {b}")
                    lines.append("    }")
                else:
                    for b in body:
                        lines.append(f"    {b}")
                lines.append("}")
        lines.append("#HotIf")
        self.preview.setPlainText("\n".join(lines))

    def _find_ahk_runtime(self, allow_browse=True):
        install_dirs = [
            Path(r"C:\Program Files\AutoHotkey\v2"),
            Path.home() / "AppData" / "Local" / "Programs" /
            "AutoHotkey" / "v2",
        ]
        names = (
            ["AutoHotkey64.exe", "AutoHotkey32.exe"]
            if sys.maxsize > 2 ** 32 else
            ["AutoHotkey32.exe", "AutoHotkey64.exe"]
        )
        preferred = [
            directory / name for directory in install_dirs for name in names
        ]
        for candidate in preferred:
            if candidate.exists():
                return candidate
        found = shutil.which("AutoHotkey.exe")
        if found:
            return Path(found)
        if not allow_browse:
            return None
        selected, _ = QFileDialog.getOpenFileName(
            self, "Locate AutoHotkey v2", "", "Executable (*.exe)"
        )
        return Path(selected) if selected else None

    def run_script(self):
        if not self._has_generated_content():
            QMessageBox.warning(self, "Key Map Empty", "You have no mappings defined!")
            return
        if self.runner.state() != QProcess.ProcessState.NotRunning:
            self.statusBar().showMessage("The script is already running", 3000)
            return
        runtime = self._find_ahk_runtime()
        if not runtime:
            return

        self._refresh()
        self._cleanup_runner_temp()
        self._runner_temp = Path(tempfile.mkdtemp(prefix="pyahk-run-"))
        script_path = self._runner_temp / "keymap.ahk"
        try:
            script_path.write_text(self.preview.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self._cleanup_runner_temp()
            QMessageBox.critical(
                self, "Run Failed", f"Could not prepare the script:\n{exc}"
            )
            return

        self._run_stop_requested = False
        self._run_error_shown = False
        self.runner.setProgram(str(runtime))
        self.runner.setArguments([str(script_path)])
        self.runner.setWorkingDirectory(str(script_path.parent))
        self.runner.start()
        if not self.runner.waitForStarted(3000):
            if not self._run_error_shown:
                self._show_run_error(self.runner.errorString())
            return

        self.run_button.setEnabled(False)
        self.stop_run_button.setEnabled(True)
        self.statusBar().showMessage("Script running")

    def stop_script(self):
        if self.runner.state() == QProcess.ProcessState.NotRunning:
            return
        self._run_stop_requested = True
        self.statusBar().showMessage("Stopping script...")
        self.runner.terminate()
        QTimer.singleShot(1500, self._kill_runner_if_needed)

    def _kill_runner_if_needed(self):
        if self.runner.state() != QProcess.ProcessState.NotRunning:
            self.runner.kill()

    def _stop_runner_now(self):
        if self.runner.state() == QProcess.ProcessState.NotRunning:
            return
        self._run_stop_requested = True
        self.runner.kill()
        self.runner.waitForFinished(2000)
        if self.runner.state() == QProcess.ProcessState.NotRunning:
            self._set_runner_stopped()

    def _run_finished(self, exit_code, exit_status):
        output = bytes(self.runner.readAll()).decode(errors="replace").strip()
        requested = self._run_stop_requested
        self._set_runner_stopped()
        if requested:
            self.statusBar().showMessage("Script stopped", 3000)
        elif exit_code == 0:
            self.statusBar().showMessage("Script finished", 3000)
        else:
            detail = output or f"AutoHotkey exited with code {exit_code}."
            QMessageBox.critical(self, "Script Failed", detail)

    def _run_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._show_run_error(self.runner.errorString())

    def _show_run_error(self, detail):
        self._run_error_shown = True
        self._set_runner_stopped()
        QMessageBox.critical(
            self, "Run Failed", detail or "AutoHotkey could not be started."
        )

    def _set_runner_stopped(self):
        self.run_button.setEnabled(True)
        self.stop_run_button.setEnabled(False)
        self._run_stop_requested = False
        self._cleanup_runner_temp()

    def _cleanup_runner_temp(self):
        if self._runner_temp is not None:
            shutil.rmtree(self._runner_temp, ignore_errors=True)
            self._runner_temp = None

    def _load_recorder_backend(self):
        if self._recorder_keyboard is None or self._recorder_mouse is None:
            from pynput import keyboard, mouse
            self._recorder_keyboard = keyboard
            self._recorder_mouse = mouse
        return self._recorder_keyboard, self._recorder_mouse

    def start_recording(self):
        if self._recording:
            return
        try:
            keyboard, mouse = self._load_recorder_backend()
            self._recording_events = []
            self._recording_modifiers = set()
            self._record_relative_clicks = self.relative_clicks.isChecked()
            self._recording = True
            self.keyboard_listener = keyboard.Listener(
                on_press=self._record_key_press,
                on_release=self._record_key_release,
            )
            self.mouse_listener = mouse.Listener(
                on_click=self._record_mouse_click,
                on_scroll=self._record_mouse_scroll,
            )
            self.keyboard_listener.start()
            self.mouse_listener.start()
        except Exception as exc:
            self._recording = False
            self._stop_recorder_listeners()
            QMessageBox.critical(
                self, "Recorder Failed", f"Could not start the recorder:\n{exc}"
            )
            return

        self.record_button.setEnabled(False)
        self.stop_record_button.setEnabled(True)
        self.record_status.setText("Recording...")
        self.record_status.show()
        self.statusBar().showMessage("Recording keyboard and mouse actions")

    def stop_recording(self, checked=False, discard=False):
        del checked
        if not self._recording and not self.keyboard_listener and not self.mouse_listener:
            return
        self._recording = False
        self._stop_recorder_listeners()

        with self._recording_lock:
            events = list(self._recording_events)
            self._recording_events = []
            self._recording_modifiers.clear()
        if not discard:
            events = self._remove_stop_button_click(events)
            tokens = self._recording_tokens(events)
            self.seq.addItems(tokens)
        else:
            tokens = []

        self.record_button.setEnabled(True)
        self.stop_record_button.setEnabled(False)
        self.record_status.hide()
        if not discard:
            message = f"Recorded {len(tokens)} action{'s' if len(tokens) != 1 else ''}"
            self.statusBar().showMessage(message, 4000)

    def _stop_recorder_listeners(self):
        for listener_name in ("keyboard_listener", "mouse_listener"):
            listener = getattr(self, listener_name)
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
                setattr(self, listener_name, None)

    @staticmethod
    def _event_is_injected(extra):
        return bool(extra and isinstance(extra[-1], bool) and extra[-1])

    def _record_key_press(self, key, *extra):
        if not self._recording or self._event_is_injected(extra):
            return
        modifier = self._recording_modifier(key)
        with self._recording_lock:
            if modifier:
                self._recording_modifiers.add(modifier)
                return
            key_name = self._recording_key_name(key)
            if key_name:
                mods = tuple(
                    mod for mod in MOD_ORDER if mod in self._recording_modifiers
                )
                self._recording_events.append(
                    (time.monotonic(), "key", mods, key_name)
                )

    def _record_key_release(self, key, *extra):
        if self._event_is_injected(extra):
            return
        modifier = self._recording_modifier(key)
        if modifier:
            with self._recording_lock:
                self._recording_modifiers.discard(modifier)

    @staticmethod
    def _recording_modifier(key):
        name = getattr(key, "name", "") or ""
        prefix = name.lower().split("_")[0]
        return {
            "ctrl": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "cmd": "win",
        }.get(prefix)

    @staticmethod
    def _recording_key_name(key):
        vk = getattr(key, "vk", None)
        if isinstance(vk, int):
            if 96 <= vk <= 105:
                return f"Numpad{vk - 96}"
            if 65 <= vk <= 90:
                return chr(vk)
            if 48 <= vk <= 57:
                return chr(vk)

        char = getattr(key, "char", None)
        if isinstance(char, str) and len(char) == 1 and char.isprintable():
            return char

        name = (getattr(key, "name", "") or "").lower()
        special = {
            "enter": "Enter", "tab": "Tab", "esc": "Esc",
            "space": "Space", "backspace": "Backspace", "delete": "Delete",
            "home": "Home", "end": "End", "page_up": "PgUp",
            "page_down": "PgDn", "up": "Up", "down": "Down",
            "left": "Left", "right": "Right", "pause": "Pause",
            "insert": "Insert", "caps_lock": "CapsLock",
            "print_screen": "PrintScreen", "menu": "AppsKey",
        }
        if name in special:
            return special[name]
        if re.fullmatch(r"f([1-9]|1\d|2[0-4])", name):
            return name.upper()
        return None

    def _record_mouse_click(self, x, y, button, pressed, *extra):
        if (not self._recording or not pressed or
                self._event_is_injected(extra)):
            return
        button_name = (getattr(button, "name", "") or "").lower()
        action = {
            "left": "Click", "right": "RClick", "middle": "MClick",
            "x1": "X1Click", "x2": "X2Click",
        }.get(button_name)
        if action:
            target = None
            click_x, click_y = int(x), int(y)
            if self._record_relative_clicks:
                relative = self._window_relative_click(click_x, click_y)
                if relative:
                    target, click_x, click_y = relative
            with self._recording_lock:
                self._recording_events.append(
                    (
                        time.monotonic(), "click", action, click_x, click_y,
                        target, int(x), int(y)
                    )
                )

    @staticmethod
    def _window_relative_click(x, y):
        if sys.platform != "win32":
            return None
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            point = wintypes.POINT(int(x), int(y))

            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            hwnd = user32.WindowFromPoint(point)
            hwnd = user32.GetAncestor(hwnd, 2) if hwnd else None
            if not hwnd:
                return None

            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, process_id.value)
            if not handle:
                return None
            try:
                path_buffer = ctypes.create_unicode_buffer(32768)
                path_size = wintypes.DWORD(len(path_buffer))
                kernel32.QueryFullProcessImageNameW.argtypes = [
                    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                    ctypes.POINTER(wintypes.DWORD)
                ]
                if not kernel32.QueryFullProcessImageNameW(
                        handle, 0, path_buffer, ctypes.byref(path_size)):
                    return None
                executable = Path(path_buffer.value).name.replace("]", "")
            finally:
                kernel32.CloseHandle(handle)

            client_point = wintypes.POINT(int(x), int(y))
            if not user32.ScreenToClient(hwnd, ctypes.byref(client_point)):
                return None
            if not executable:
                return None
            return executable, int(client_point.x), int(client_point.y)
        except (AttributeError, OSError, ValueError):
            return None

    def _record_mouse_scroll(self, x, y, dx, dy, *extra):
        del x, y
        if not self._recording or self._event_is_injected(extra):
            return
        if dy:
            action = "WheelUp" if dy > 0 else "WheelDown"
            count = max(1, int(abs(dy)))
        elif dx:
            action = "WheelRight" if dx > 0 else "WheelLeft"
            count = max(1, int(abs(dx)))
        else:
            return
        now = time.monotonic()
        with self._recording_lock:
            for _ in range(count):
                self._recording_events.append((now, "key", (), action))

    def _remove_stop_button_click(self, events):
        if not events or events[-1][1] != "click":
            return events
        event = events[-1]
        screen_x, screen_y = (
            event[6:8] if len(event) >= 8 else event[3:5]
        )
        local = self.stop_record_button.mapFromGlobal(QPoint(screen_x, screen_y))
        if self.stop_record_button.rect().contains(local):
            return events[:-1]
        return events

    @staticmethod
    def _recording_tokens(events):
        tokens = []
        previous_time = None
        for event in events:
            timestamp, kind = event[:2]
            if previous_time is not None:
                delay = timestamp - previous_time
                if delay >= 0.08:
                    value = f"{delay:.2f}".rstrip("0").rstrip(".")
                    tokens.append(f"{value} s")
            if kind == "key":
                mods, key_name = event[2], event[3]
                pretty_mods = [mod.title() for mod in mods]
                tokens.append("+".join(pretty_mods + [key_name]))
            elif kind == "click":
                target = event[5] if len(event) >= 6 else None
                if target:
                    tokens.append(
                        f"{event[2]} in [{target}] @ {event[3]}, {event[4]}"
                    )
                else:
                    tokens.append(f"{event[2]} @ {event[3]}, {event[4]}")
            previous_time = timestamp
        return tokens

    def save_ahk(self):
        if not self._has_generated_content():
            QMessageBox.warning(self, "Key Map Empty", "You have no mappings defined!")
            return
        p,_=QFileDialog.getSaveFileName(self,"Save AHK","keymap.ahk","AHK (*.ahk)")
        if p:
            try:
                Path(p).write_text(self.preview.toPlainText(), encoding="utf-8")
            except OSError as e:
                QMessageBox.critical(self, "Save Failed", f"Could not save file:\n{e}")

    def build_exe(self):
        if self._build_is_busy():
            self._cancel_build()
            return
        if not self._has_generated_content():
            QMessageBox.warning(self, "Key Map Empty", "You have no mappings defined!")
            return

        compiler = self._find_ahk2exe()
        if compiler is None:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Ahk2Exe Not Found")
            dlg.setText("Ahk2Exe.exe is required to compile your script.")
            dlg.setInformativeText("Install it, locate it manually, or cancel:")
            btn_install = dlg.addButton("Install", QMessageBox.ButtonRole.AcceptRole)
            btn_browse = dlg.addButton("Browse", QMessageBox.ButtonRole.AcceptRole)
            dlg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            dlg.exec()
            if dlg.clickedButton() == btn_install:
                self._start_compiler_install()
                return
            elif dlg.clickedButton() == btn_browse:
                path, _ = QFileDialog.getOpenFileName(
                    self, "Locate Ahk2Exe.exe", "", "Executable (*.exe)"
                )
                if not path or Path(path).name.lower() != "ahk2exe.exe":
                    QMessageBox.warning(self, "Invalid File", "That isn't an Ahk2Exe.exe!")
                    return
                compiler = Path(path)
            else:
                return
        self._start_compile(compiler)

    def _ahk_install_roots(self):
        roots = [
            Path(r"C:\Program Files\AutoHotkey"),
            Path.home() / "AppData" / "Local" / "Programs" / "AutoHotkey",
        ]
        runtime = self._find_ahk_runtime(allow_browse=False)
        if runtime and runtime.parent.name.lower() == "v2":
            roots.insert(0, runtime.parent.parent)
        unique = []
        for root in roots:
            if root not in unique:
                unique.append(root)
        return unique

    def _find_ahk2exe(self):
        return next(
            (
                root / "Compiler" / "Ahk2Exe.exe"
                for root in self._ahk_install_roots()
                if (root / "Compiler" / "Ahk2Exe.exe").exists()
            ),
            None,
        )

    def _find_ahk2exe_installer(self):
        return next(
            (
                root / "UX" / "install-ahk2exe.ahk"
                for root in self._ahk_install_roots()
                if (root / "UX" / "install-ahk2exe.ahk").exists()
            ),
            None,
        )

    def _start_compiler_install(self):
        installer = self._find_ahk2exe_installer()
        runtime = self._find_ahk_runtime()
        if not installer:
            QMessageBox.warning(
                self,
                "Installer Missing",
                "AutoHotkey's install-ahk2exe.ahk file was not found.",
            )
            return
        if not runtime:
            return
        self._build_cancel_requested = False
        self._set_build_busy(True)
        self._compiler_install_deadline = time.monotonic() + 180
        self.compiler_poll_timer.start()
        self.compiler_installer.setProgram(str(runtime))
        self.compiler_installer.setArguments([str(installer)])
        self.compiler_installer.start()
        self.statusBar().showMessage(
            "Installing Ahk2Exe; complete any AutoHotkey prompts..."
        )

    def _poll_compiler_install(self):
        compiler = self._find_ahk2exe()
        if compiler:
            self.compiler_poll_timer.stop()
            self._start_compile(compiler)
            return
        if time.monotonic() >= self._compiler_install_deadline:
            self._cancel_build()
            QMessageBox.warning(
                self,
                "Installer Timed Out",
                "Ahk2Exe.exe did not appear. You can try again or locate it manually.",
            )

    def _installer_finished(self, exit_code, exit_status):
        del exit_status
        if (exit_code != 0 and not self._find_ahk2exe() and
                self.compiler_poll_timer.isActive()):
            output = bytes(
                self.compiler_installer.readAll()
            ).decode(errors="replace").strip()
            self.compiler_poll_timer.stop()
            self._set_build_busy(False)
            if not self._closing:
                QMessageBox.critical(
                    self,
                    "Installer Failed",
                    output or f"The installer exited with code {exit_code}.",
                )

    def _installer_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        self.compiler_poll_timer.stop()
        self._set_build_busy(False)
        if not self._closing:
            QMessageBox.critical(
                self,
                "Installer Failed",
                self.compiler_installer.errorString() or
                "The AutoHotkey installer could not be started.",
            )

    def _start_compile(self, compiler):
        self.compiler_poll_timer.stop()
        out_file, _ = QFileDialog.getSaveFileName(
            self, "Save EXE", "keymap.exe", "EXE (*.exe)"
        )
        if not out_file:
            self._set_build_busy(False)
            return
        output = Path(out_file)
        if output.suffix.lower() != ".exe":
            output = Path(str(output) + ".exe")

        base = self._find_ahk_runtime()
        if not base:
            self._set_build_busy(False)
            return

        self._refresh()
        self._cleanup_build_temp()
        self._build_temp = Path(tempfile.mkdtemp(prefix="pyahk-build-"))
        temp_ahk = self._build_temp / "keymap.ahk"
        try:
            temp_ahk.write_text(self.preview.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self._cleanup_build_temp()
            self._set_build_busy(False)
            QMessageBox.critical(
                self, "Write Failed", f"Could not prepare the script:\n{exc}"
            )
            return

        self._build_output = output
        self._build_error_shown = False
        self._build_cancel_requested = False
        self._set_build_busy(True)
        self.builder.setProgram(str(compiler))
        self.builder.setArguments([
            "/in", str(temp_ahk),
            "/out", str(output),
            "/bin", str(base),
        ])
        self.builder.setWorkingDirectory(str(self._build_temp))
        self.builder.start()
        self.statusBar().showMessage("Starting compiler...")

    def _build_started(self):
        self.statusBar().showMessage("Building executable...")

    def _build_finished(self, exit_code, exit_status):
        del exit_status
        output_text = bytes(self.builder.readAll()).decode(
            errors="replace"
        ).strip()
        output_path = self._build_output
        canceled = self._build_cancel_requested
        succeeded = bool(
            exit_code == 0 and output_path and output_path.exists()
        )
        self._finish_build_state()
        if self._closing or canceled:
            return
        if succeeded:
            self.statusBar().showMessage("Executable built", 4000)
            QMessageBox.information(
                self, "Success", f"Executable created at:\n{output_path}"
            )
        elif not self._build_error_shown:
            QMessageBox.critical(
                self,
                "Compile Failed",
                output_text or f"Ahk2Exe exited with code {exit_code}.",
            )

    def _build_error(self, error):
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._build_error_shown = True
        detail = self.builder.errorString()
        self._finish_build_state()
        if not self._closing:
            QMessageBox.critical(
                self,
                "Compile Failed",
                detail or "Ahk2Exe.exe could not be started.",
            )

    def _build_is_busy(self):
        return bool(
            self.compiler_poll_timer.isActive() or
            self.compiler_installer.state() != QProcess.ProcessState.NotRunning or
            self.builder.state() != QProcess.ProcessState.NotRunning
        )

    def _set_build_busy(self, busy):
        self.build_button.setText("Cancel build" if busy else "Build .exe")
        self.build_button.setToolTip(
            "Cancel the current build" if busy else
            "Compile script to executable"
        )

    def _cancel_build(self):
        self._build_cancel_requested = True
        self.compiler_poll_timer.stop()
        for process in (self.compiler_installer, self.builder):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(2000)
        self._finish_build_state()
        self._build_cancel_requested = False
        self.statusBar().showMessage("Build canceled", 3000)

    def _finish_build_state(self):
        self.compiler_poll_timer.stop()
        self._cleanup_build_temp()
        self._build_output = None
        self._set_build_busy(self._build_is_busy())

    def _cleanup_build_temp(self):
        if self._build_temp is not None:
            shutil.rmtree(self._build_temp, ignore_errors=True)
            self._build_temp = None

    def closeEvent(self, event):
        if self._recording or self.keyboard_listener or self.mouse_listener:
            self.stop_recording(discard=True)
        if not self._maybe_save_project():
            event.ignore()
            return
        self._closing = True
        if self._build_is_busy():
            self._cancel_build()
        self._stop_runner_now()
        self._cleanup_runner_temp()
        self._clear_recovery()
        event.accept()


if __name__=="__main__":
    app=QApplication(sys.argv)
    app.setApplicationName("pyAHK")
    app.setOrganizationName("pyAHK")
    KeyMapper().show()
    sys.exit(app.exec())
