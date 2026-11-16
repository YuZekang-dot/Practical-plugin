"""Short visible GUI check with a unique application ID and temporary settings."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="eye-rest-gui-") as temporary:
        os.environ["XDG_CONFIG_HOME"] = str(Path(temporary) / "config")
        os.environ["XDG_DATA_HOME"] = str(Path(temporary) / "data")
        from app import EyeRestApp
        from core import BreakTimer, autostart_enabled, import_image
        from gi.repository import Gdk, GLib, Gtk
        from PIL import Image

        test_id = "local.eyerest.Test" + str(os.getpid())
        app = EyeRestApp(application_id=test_id)
        remote_command = ["/usr/bin/python3", "-c",
                          "import sys; from app import EyeRestApp; "
                          f"sys.exit(EyeRestApp(application_id={test_id!r}).run(sys.argv))"]
        failures = []
        remote = None
        float_window = None
        deadline = 0

        def checked(function):
            def wrapper():
                try:
                    return function()
                except Exception:
                    failures.append(traceback.format_exc())
                    app.exit_application()
                    return GLib.SOURCE_REMOVE
            return wrapper

        def later(delay, function):
            GLib.timeout_add(delay, checked(function))

        def controls():
            nonlocal remote, float_window, deadline
            float_window = app.floating
            assert float_window.get_visible()
            assert float_window.get_size().width > 100
            artifacts = ROOT / "tests/artifacts"
            artifacts.mkdir(exist_ok=True)
            screenshot = Gdk.pixbuf_get_from_window(float_window.get_window(), 0, 0, *float_window.get_size())
            screenshot.savev(str(artifacts / "float-preview.png"), "png", [], [])
            float_window.autostart_item.set_active(True)
            assert autostart_enabled(app.paths.autostart)
            float_window.autostart_item.set_active(False)
            assert not app.paths.autostart.exists()
            source = Path(temporary) / "sample.jpg"
            Image.new("RGB", (640, 400), (30, 65, 45)).save(source)
            import_image(source, app.paths.image)
            app.image_imported(None)
            source.unlink()
            assert app.settings.custom_image and app.background.get_width() == 640
            app.restore_image()
            assert not app.settings.custom_image and app.background.get_width() == 1600
            remote = subprocess.Popen(remote_command, cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 5
            later(100, check_duplicate)
            return GLib.SOURCE_REMOVE

        def check_duplicate():
            if remote.poll() is None:
                assert time.monotonic() < deadline, "Second launch did not reach existing instance"
                return GLib.SOURCE_CONTINUE
            output, error = remote.communicate()
            assert remote.returncode == 0, output + error
            assert app.floating is float_window and len(app.get_windows()) == 1
            app.start_break()
            assert app.timer.remaining(time.monotonic()) == 60
            later(700, check_fullscreen)
            return GLib.SOURCE_REMOVE

        def check_fullscreen():
            display = Gdk.Display.get_default()
            assert len(app.overlays) == display.get_n_monitors()
            assert not app.floating.get_visible()
            for i, window in enumerate(app.overlays):
                geometry = display.get_monitor(i).get_geometry()
                assert tuple(window.get_size()) == (geometry.width, geometry.height), (window.get_size(), geometry)
                assert window.get_window().get_state() & Gdk.WindowState.FULLSCREEN
                assert window.background_widget.get_pixbuf() is not None
            screenshot = Gdk.pixbuf_get_from_window(app.overlays[0].get_window(), 0, 0,
                                                    *app.overlays[0].get_size())
            artifacts = ROOT / "tests/artifacts"
            artifacts.mkdir(exist_ok=True)
            screenshot.savev(str(artifacts / "rest-preview.png"), "png", [], [])
            expected_deadline = app.timer.deadline
            app.apply_layout()
            assert app.timer.deadline == expected_deadline
            later(300, end_with_button)
            return GLib.SOURCE_REMOVE

        def descendants(widget):
            yield widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    yield from descendants(child)

        def end_with_button():
            button = next(widget for widget in descendants(app.overlays[0])
                          if isinstance(widget, Gtk.Button) and widget.get_label() == "结束本次休息")
            button.clicked()
            assert app.timer is None and not app.overlays and app.floating.get_visible()
            app.start_break()
            # Exercise the real GUI tick and automatic completion without waiting a minute.
            app.timer = BreakTimer.start(time.monotonic(), 1)
            later(1300, check_automatic_end)
            return GLib.SOURCE_REMOVE

        def check_automatic_end():
            assert app.timer is None and not app.overlays and app.floating.get_visible()
            app.start_break()
            app.on_lock_changed(None, None, None, None, None, GLib.Variant("(b)", (True,)))
            assert app.locked and app.timer is None
            app.start_break()
            assert app.timer is None
            app.on_lock_changed(None, None, None, None, None, GLib.Variant("(b)", (False,)))
            app.start_break()
            app.on_sleep_changed(None, None, None, None, None, GLib.Variant("(b)", (True,)))
            assert app.sleeping and app.timer is None
            app.on_sleep_changed(None, None, None, None, None, GLib.Variant("(b)", (False,)))
            app.save_position()
            later(200, remote_quit)
            return GLib.SOURCE_REMOVE

        def remote_quit():
            nonlocal remote
            remote = subprocess.Popen([*remote_command, "--quit"], cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return GLib.SOURCE_REMOVE

        def timeout():
            nonlocal watchdog
            watchdog = 0
            failures.append("GUI test timed out")
            app.exit_application()
            return GLib.SOURCE_REMOVE

        later(400, controls)
        watchdog = GLib.timeout_add_seconds(15, timeout)
        result = app.run([str(ROOT / "app.py")])
        if watchdog:
            GLib.source_remove(watchdog)
        if remote:
            try:
                stdout, stderr = remote.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                remote.terminate()
                stdout, stderr = remote.communicate(timeout=3)
                failures.append("Remote launch timed out")
            if remote.returncode:
                failures.append(stdout + stderr)
        if failures:
            print("\n".join(failures), file=sys.stderr)
            return 1
        assert result == 0
        print("GUI PASS: float, image, autostart toggle, single instance, fullscreen, countdown, exit, lock/sleep, layout rebuild")
        return 0


if __name__ == "__main__":
    sys.exit(main())
