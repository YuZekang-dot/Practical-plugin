import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import (BREAK_SECONDS, BreakTimer, HourScheduler, Paths, Settings, autostart_enabled,
                  clamp_position, desktop_entry, import_image, next_hour, write_managed_entry)


def wall(hour, minute=0, second=0, day=16):
    return datetime(2026, 11, day, hour, minute, second).timestamp()


class SchedulingTests(unittest.TestCase):
    def test_wait_until_next_hour_even_when_started_on_hour(self):
        self.assertEqual(next_hour(wall(14)), wall(15))
        self.assertEqual(next_hour(wall(23, 59)), wall(0, day=17))

    def test_hour_fires_once(self):
        scheduler = HourScheduler(wall(13, 59, 59), 100)
        self.assertFalse(scheduler.poll(wall(13, 59, 59) + .5, 100.5))
        self.assertTrue(scheduler.poll(wall(14) + .1, 101.1))
        self.assertFalse(scheduler.poll(wall(14) + .5, 101.5))
        self.assertEqual(scheduler.next_due, wall(15))

    def test_midnight(self):
        scheduler = HourScheduler(wall(23, 59, 59), 100)
        self.assertTrue(scheduler.poll(wall(0, day=17), 101))

    def test_suspend_does_not_replay_missed_hour(self):
        scheduler = HourScheduler(wall(13, 59, 59), 100)
        self.assertFalse(scheduler.poll(wall(15, 2), 101))
        self.assertEqual(scheduler.next_due, wall(16))

    def test_long_stall_does_not_trigger_late(self):
        scheduler = HourScheduler(wall(13, 59, 40), 100)
        self.assertFalse(scheduler.poll(wall(14, 0, 1), 121))

    def test_clock_jump_does_not_trigger(self):
        scheduler = HourScheduler(wall(13, 59, 50), 100)
        self.assertFalse(scheduler.poll(wall(14), 101))
        self.assertEqual(scheduler.next_due, wall(15))

    def test_clock_rewind_does_not_repeat_seen_hour(self):
        scheduler = HourScheduler(wall(13, 59, 59), 100)
        self.assertTrue(scheduler.poll(wall(14), 101))
        self.assertFalse(scheduler.poll(wall(13, 59, 59), 102))
        self.assertFalse(scheduler.poll(wall(14), 103))

    def test_locked_or_already_resting_skips_current_hour(self):
        scheduler = HourScheduler(wall(13, 59, 59), 100)
        self.assertFalse(scheduler.poll(wall(14), 101, blocked=True))
        self.assertFalse(scheduler.poll(wall(14) + .2, 101.2))
        self.assertEqual(scheduler.next_due, wall(15))

    def test_break_is_sixty_seconds_on_monotonic_clock(self):
        self.assertEqual(BREAK_SECONDS, 60)
        timer = BreakTimer.start(500)
        self.assertEqual(timer.remaining(500), 60)
        self.assertEqual(timer.remaining(500.9), 60)
        self.assertEqual(timer.remaining(501), 59)
        self.assertEqual(timer.remaining(559.99), 1)
        self.assertEqual(timer.remaining(560), 0)
        self.assertEqual(timer.remaining(1000), 0)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_settings_round_trip(self):
        settings = Settings(self.root)
        settings.custom_image = True
        settings.position = (-1600, 40)
        settings.save()
        restored = Settings(self.root)
        self.assertTrue(restored.custom_image)
        self.assertEqual(restored.position, (-1600, 40))

    def test_bad_settings_fall_back(self):
        (self.root / "settings.json").write_text("{broken")
        settings = Settings(self.root)
        self.assertFalse(settings.custom_image)
        self.assertIsNone(settings.position)
        self.assertIsNotNone(settings.warning)
        (self.root / "settings.json").write_text('{"custom_image":"yes","position":[true,2]}')
        settings = Settings(self.root)
        self.assertFalse(settings.custom_image)
        self.assertIsNone(settings.position)

    def test_image_copy_survives_source_removal(self):
        from PIL import Image
        original = self.root / "original.jpg"
        destination = self.root / "images/background.png"
        Image.new("RGB", (100, 50), "green").save(original)
        import_image(original, destination)
        original.unlink()
        with Image.open(destination) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (100, 50))

    def test_broken_image_keeps_previous_copy(self):
        from PIL import Image
        original = self.root / "original.png"
        destination = self.root / "images/background.png"
        Image.new("RGB", (10, 10), "green").save(original)
        import_image(original, destination)
        previous = destination.read_bytes()
        original.write_bytes(b"broken image")
        with self.assertRaises(Exception):
            import_image(original, destination)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(list(destination.parent.glob(".background-*")), [])

    def test_image_exif_orientation_is_applied(self):
        from PIL import Image
        original = self.root / "portrait.jpg"
        destination = self.root / "copy.png"
        image = Image.new("RGB", (30, 10), "green")
        exif = image.getexif()
        exif[274] = 6
        image.save(original, exif=exif)
        import_image(original, destination)
        with Image.open(destination) as normalized:
            self.assertEqual(normalized.size, (10, 30))

    def test_unsupported_image_is_rejected(self):
        from PIL import Image
        original = self.root / "image.gif"
        Image.new("RGB", (10, 10)).save(original)
        with self.assertRaises(ValueError):
            import_image(original, self.root / "copy.png")

    def test_offscreen_position_is_clamped_after_monitor_removal(self):
        self.assertEqual(clamp_position((3000, 800), 200, 60, [(0, 24, 1704, 1000)]), (1504, 800))
        self.assertEqual(clamp_position((-1800, 0), 200, 60,
                                        [(-1920, 24, 1920, 1056), (0, 24, 1704, 1000)]), (-1800, 24))
        self.assertEqual(clamp_position((3000, 3000), 200, 60, [(0, 24, 1704, 1000)]), (1504, 964))

    def test_autostart_and_conflict_detection(self):
        entry = self.root / "autostart/eye-rest.desktop"
        self.assertFalse(autostart_enabled(entry))
        write_managed_entry(entry, ROOT / "app.py", True)
        self.assertTrue(autostart_enabled(entry))
        entry.write_text(entry.read_text() + "Hidden=true\n")
        self.assertFalse(autostart_enabled(entry))
        entry.write_text("[Desktop Entry]\nName=Someone else's app\n")
        with self.assertRaises(ValueError):
            write_managed_entry(entry, ROOT / "app.py", True)

    def test_desktop_exec_handles_literal_special_characters(self):
        # Launch only a harmless temporary Python script, using the actual GIO parser.
        from gi.repository import Gio
        directory = self.root / 'space 空 % $ ` " \\'
        directory.mkdir()
        marker = directory / "launched.txt"
        script = directory / "app.py"
        script.write_text("from pathlib import Path\nPath(__file__).with_name('launched.txt').write_text('ok')\n")
        entry = self.root / "test.desktop"
        entry.write_text(desktop_entry(script))
        info = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertIsNotNone(info)
        self.assertTrue(info.launch([], None))
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(marker.read_text(), "ok")

    def test_relative_xdg_paths_are_ignored(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative", "XDG_DATA_HOME": "relative"}):
            self.assertTrue(Paths.discover().config.is_absolute())


class InstallTests(unittest.TestCase):
    def test_install_update_and_uninstall_in_isolated_directories(self):
        with tempfile.TemporaryDirectory(prefix="eye rest install ") as temporary:
            root = Path(temporary)
            env = {**os.environ, "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")}
            # Prevent uninstall tests from contacting the user's real running application.
            env.pop("DBUS_SESSION_BUS_ADDRESS", None)
            def run(*arguments):
                result = subprocess.run(["/usr/bin/python3", str(ROOT / "manage.py"), *arguments],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result
            run("install", "--no-start")
            entry = root / "config/autostart/eye-rest.desktop"
            self.assertTrue(autostart_enabled(entry))
            self.assertTrue((root / "data/eye-rest/app/app.py").is_file())
            entry.unlink()  # User deliberately disabled autostart; updates must preserve it.
            run("install", "--no-start")
            self.assertFalse(entry.exists())
            settings = Settings(root / "config/eye-rest")
            settings.save()
            run("uninstall")
            self.assertTrue(settings.file.exists())
            self.assertFalse((root / "data/eye-rest/app").exists())
            self.assertFalse((root / "data/applications/eye-rest.desktop").exists())
            run("uninstall", "--purge")
            self.assertFalse((root / "config/eye-rest").exists())
            self.assertFalse((root / "data/eye-rest").exists())


if __name__ == "__main__":
    unittest.main()
