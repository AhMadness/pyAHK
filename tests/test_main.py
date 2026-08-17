import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

import main


class MainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.windows = []
        self.warning_messages = []
        self._orig_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(self._capture_warning)

    def tearDown(self):
        for window in self.windows:
            window.autosave_timer.stop()
            window.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()
        QMessageBox.warning = self._orig_warning

    def make_window(self):
        window = main.KeyMapper()
        window.autosave_timer.stop()
        window.recovery_path = (
            Path(self.temp_dir.name) / f"recovery-{len(self.windows)}.json"
        )
        self.windows.append(window)
        return window

    def _capture_warning(self, *args, **kwargs):
        if len(args) >= 3:
            self.warning_messages.append(args[2])
        return QMessageBox.StandardButton.Ok

    def test_hotkey_plus_and_minus(self):
        self.assertEqual(main.hotkey_to_ahk("+"), "vkBB")
        self.assertEqual(main.hotkey_to_ahk("Ctrl++"), "^vkBB")
        self.assertEqual(main.hotkey_to_ahk("-"), "vkBD")
        self.assertEqual(main.hotkey_to_ahk("Ctrl+-"), "^vkBD")

    def test_step_plus_and_quoted_text_escaping(self):
        self.assertEqual(main.to_ahk_step("+"), 'Send "{+}"')
        self.assertEqual(main.to_ahk_step("Ctrl++"), 'Send "^{+}"')
        self.assertEqual(main.to_ahk_step('"a"b"'), 'SendText "a`"b"')

    def test_literal_text_and_right_click_generate_valid_actions(self):
        self.assertEqual(main.to_ahk_step('"a+b!{c}#^"'),
                         'SendText "a+b!{c}#^"')
        self.assertEqual(main.to_ahk_step("Click right"), 'Click "Right"')

    def test_multi_click_is_not_accepted_as_a_hotkey(self):
        self.assertEqual(main.hotkey_to_ahk("Click x2"), "")
        self.assertEqual(main.hotkey_to_ahk("Click x3"), "")

    def test_duplicate_trigger_detected_after_normalization(self):
        w = self.make_window()
        w.trigger.setText("Ctrl+A")
        w.seq.addItem("B")
        w.add_mapping()

        w.trigger.setText("ctrl-a")
        w.seq.addItem("C")
        w.add_mapping()

        self.assertEqual(len(w.maps), 1)
        self.assertTrue(self.warning_messages)
        self.assertIn("already mapped", self.warning_messages[-1])

    def test_control_conflict_detected_after_normalization(self):
        w = self.make_window()
        w.trigger.setText("Ctrl+A")
        w.seq.addItem("B")
        w.add_mapping()

        w.toggle.setText("ctrl-a")
        w.add_mapping()

        self.assertNotIn("toggle", w.control_items)
        self.assertTrue(self.warning_messages)
        self.assertIn("already assigned", self.warning_messages[-1])

    def test_info_only_preview_is_generated(self):
        w = self.make_window()
        w.info.setText("Ctrl+I")
        w.add_mapping()
        self.assertTrue(w.preview.toPlainText().strip())
        self.assertIn("^i:: {", w.preview.toPlainText())
        self.assertIn('ToolTip("Info:`n(no mappings)")',
                      w.preview.toPlainText())
        self.assertIn("SetTimer(HideInfo, -5000)",
                      w.preview.toPlainText())

    def test_toggle_preview_uses_colored_status_gui(self):
        w = self.make_window()
        w.toggle.setText("Ctrl+T")
        w.add_mapping()
        script = w.preview.toPlainText()
        self.assertIn('Gui("+AlwaysOnTop -Caption +ToolWindow +E0x20")',
                      script)
        self.assertIn('scriptEnabled ? "2E7D32" : "C62828"', script)
        self.assertIn("SetTimer(HideToggleStatus, -1000)", script)
        self.assertNotIn("ToolTip(scriptEnabled", script)

    def test_project_round_trip_preserves_controls_and_mapping_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.pyahk.json"
            w = self.make_window()
            w.controls = {"toggle": "F10", "exit": "F11", "info": "F12"}
            w.maps = [main.Mapping(
                trigger="Ctrl+A",
                steps=['"hello"', "0.25 s", "Click right"],
                name="Greeting",
                enabled=False,
                repeat=4,
            )]
            w.project_path = path

            self.assertTrue(w.save_project())
            data = json.loads(path.read_text(encoding="utf-8"))
            controls, mappings = w._decode_project(data)

            self.assertEqual(controls, w.controls)
            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0].trigger, "Ctrl+A")
            self.assertEqual(mappings[0].steps,
                             ['"hello"', "0.25 s", "Click right"])
            self.assertEqual(mappings[0].name, "Greeting")
            self.assertFalse(mappings[0].enabled)
            self.assertEqual(mappings[0].repeat, 4)

    def test_project_loader_rejects_duplicate_hotkeys(self):
        w = self.make_window()
        data = {
            "version": main.PROJECT_VERSION,
            "controls": {"toggle": "Ctrl+A", "exit": "", "info": ""},
            "mappings": [{
                "name": "",
                "trigger": "ctrl-a",
                "steps": ["B"],
                "enabled": True,
            }],
        }
        with self.assertRaisesRegex(ValueError, "Duplicate hotkey"):
            w._decode_project(data)

    def test_editing_updates_the_existing_mapping(self):
        w = self.make_window()
        mapping = main.Mapping("Ctrl+A", ["B"], name="Existing", repeat=2)
        w.maps = [mapping]
        w._rebuild_maplist()

        w._edit_mapping_item(w.maplist.item(0))
        w.seq.item(0).setText("C")
        w.repeat_spin.setValue(3)
        w.add_mapping()

        self.assertEqual(len(w.maps), 1)
        self.assertEqual(w.maps[0].uid, mapping.uid)
        self.assertEqual(w.maps[0].name, "Existing")
        self.assertEqual(w.maps[0].steps, ["C"])
        self.assertEqual(w.maps[0].repeat, 3)
        self.assertIsNone(w.editing_mapping_id)

    def test_disabled_mapping_is_saved_but_not_generated(self):
        w = self.make_window()
        w.controls["info"] = "F12"
        w.maps = [
            main.Mapping("F1", ["A"], name="Active"),
            main.Mapping("F2", ["B"], name="Paused", enabled=False),
        ]
        w._refresh()
        script = w.preview.toPlainText()

        self.assertIn("F1::", script)
        self.assertNotIn("F2::", script)
        self.assertIn("Active: F1", script)
        self.assertNotIn("Paused: F2", script)
        self.assertEqual(len(w._project_data()["mappings"]), 2)

    def test_recorded_clicks_use_screen_coordinates(self):
        w = self.make_window()
        w.maps = [main.Mapping("F1", ["Click @ 120, -30", "RClick @ 5, 9"])]
        w._refresh()
        script = w.preview.toPlainText()

        self.assertIn('CoordMode "Mouse", "Screen"', script)
        self.assertIn('ClickOnScreen(120, -30, "Left")', script)
        self.assertIn('ClickOnScreen(5, 9, "Right")', script)

    def test_recorder_converts_keys_delays_clicks_and_wheel(self):
        events = [
            (10.0, "key", ("ctrl",), "C"),
            (10.5, "click", "RClick", 20, 30),
            (10.5, "key", (), "WheelDown"),
        ]
        self.assertEqual(main.KeyMapper._recording_tokens(events), [
            "Ctrl+C", "0.5 s", "RClick @ 20, 30", "WheelDown"
        ])
        self.assertEqual(main.to_ahk_step("WheelDown"), 'Send "{WheelDown}"')

    def test_recorder_key_translation_handles_ctrl_letters_and_special_keys(self):
        self.assertEqual(
            main.KeyMapper._recording_key_name(SimpleNamespace(vk=67, char="\\x03")),
            "C",
        )
        self.assertEqual(
            main.KeyMapper._recording_key_name(SimpleNamespace(name="page_down")),
            "PgDn",
        )
        self.assertEqual(
            main.KeyMapper._recording_modifier(SimpleNamespace(name="cmd_l")),
            "win",
        )

    def test_sequence_list_is_configured_for_drag_reordering(self):
        from PyQt6.QtWidgets import QAbstractItemView

        w = self.make_window()
        self.assertEqual(
            w.seq.dragDropMode(),
            QAbstractItemView.DragDropMode.InternalMove,
        )

    def test_window_relative_click_activates_target_application(self):
        w = self.make_window()
        token = "RClick in [notepad.exe] @ 40, 70"
        w.maps = [main.Mapping("F3", [token])]
        w._refresh()
        script = w.preview.toPlainText()

        self.assertIn("ClickInWindow(target", script)
        self.assertIn('WinActivate target', script)
        self.assertIn(
            'ClickInWindow("ahk_exe notepad.exe", 40, 70, "Right")',
            script,
        )

    def test_recorder_emits_window_relative_click_token(self):
        events = [
            (10.0, "click", "Click", 25, 35, "notepad.exe", 500, 600)
        ]
        self.assertEqual(
            main.KeyMapper._recording_tokens(events),
            ["Click in [notepad.exe] @ 25, 35"],
        )

    def test_repeat_wraps_the_complete_mapping(self):
        w = self.make_window()
        w.maps = [main.Mapping("F4", ["A", "0.1 s"], repeat=3)]
        w._refresh()
        script = w.preview.toPlainText()

        self.assertIn("    Loop 3 {", script)
        self.assertIn('        Send "a"', script)
        self.assertIn("        Sleep 100", script)
        self.assertIn("[x3]", w._mapping_label(w.maps[0]))

    def test_old_project_without_repeat_defaults_to_one(self):
        w = self.make_window()
        data = {
            "version": main.PROJECT_VERSION,
            "controls": {"toggle": "", "exit": "", "info": ""},
            "mappings": [{
                "name": "Old",
                "trigger": "F5",
                "steps": ["A"],
                "enabled": True,
            }],
        }
        _, mappings = w._decode_project(data)
        self.assertEqual(mappings[0].repeat, 1)

    def test_recovery_autosave_restores_project_and_path(self):
        recovery = Path(self.temp_dir.name) / "shared-recovery.json"
        original = self.make_window()
        original.recovery_path = recovery
        original.project_path = Path(self.temp_dir.name) / "saved.pyahk.json"
        original.controls["exit"] = "F12"
        original.maps = [main.Mapping("F6", ["B"], repeat=2)]
        original.project_dirty = True
        original._write_recovery()
        self.assertTrue(recovery.exists())

        restored = self.make_window()
        restored.recovery_path = recovery
        original_question = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        try:
            restored._offer_recovery()
        finally:
            QMessageBox.question = original_question

        self.assertTrue(restored.project_dirty)
        self.assertEqual(restored.controls["exit"], "F12")
        self.assertEqual(restored.maps[0].repeat, 2)
        self.assertEqual(restored.project_path, original.project_path)

    def test_build_button_switches_to_cancel_without_starting_a_process(self):
        w = self.make_window()
        self.assertFalse(w._build_is_busy())
        w._set_build_busy(True)
        self.assertEqual(w.build_button.text(), "Cancel build")
        w._set_build_busy(False)
        self.assertEqual(w.build_button.text(), "Build .exe")

    def test_unknown_named_hotkey_is_rejected(self):
        self.assertEqual(main.hotkey_to_ahk("definitely-not-a-key"), "")


if __name__ == "__main__":
    unittest.main()
