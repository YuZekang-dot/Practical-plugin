#!/usr/bin/python3
"""A local, single-instance hourly eye-rest application for GTK 3 desktops."""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core import (APP_ID, APP_NAME, BreakTimer, HourScheduler, Paths, Settings,
                  autostart_enabled, clamp_position, import_image, prepare_gui_environment,
                  remove_managed_entry, write_managed_entry)

prepare_gui_environment()

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("eye-rest")
CSS = b"""
window.floating { background-color: transparent; }
.float-body { background: #173a30; border: 1px solid #4d7260; border-radius: 12px; padding: 8px; }
.float-body label { color: #e1eee5; }
.float-title { font-size: 12px; font-weight: bold; }
.float-time { font-size: 11px; }
.float-body button { background: transparent; border: none; box-shadow: none;
  color: #dcece1; min-width: 20px; min-height: 24px; padding: 3px; }
.float-body button:hover { background: #345448; }
window.break-window { background: #102923; }
.rest-panel { background: rgba(9, 28, 23, 0.87); border: 1px solid rgba(163, 198, 169, 0.2);
  border-radius: 24px; padding: 30px 42px; }
.rest-heading { color: #d4e2d3; font-size: 24px; }
.rest-countdown { color: #e2ecdb; font-size: 68px; font-weight: 300; font-family: monospace; }
.rest-hint { color: #b3c8b6; font-size: 16px; }
.rest-detail { color: #94ac9d; font-size: 12px; }
.rest-exit { background: #284c3c; color: #d4e2d3; border: 1px solid #55725c;
  border-radius: 8px; box-shadow: none; padding: 9px 18px; margin-top: 14px; }
.rest-exit:hover { background: #365c48; }
"""


def styled_label(text: str, style: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.get_style_context().add_class(style)
    return label


class FloatingWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_focus_on_map(False)
        self.stick()
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.get_style_context().add_class("floating")

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        body.get_style_context().add_class("float-body")
        self.add(body)
        drag = Gtk.EventBox()
        drag.set_visible_window(False)
        drag.connect("button-press-event", self.on_drag)
        drag.set_tooltip_text("拖动可移动位置；整点自动休息 60 秒")
        contents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(ROOT / "assets/icon.svg"), 32, 32, True))
        contents.pack_start(icon, False, False, 0)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.pack_start(styled_label(APP_NAME, "float-title"), False, False, 0)
        self.status = styled_label("", "float-time")
        labels.pack_start(self.status, False, False, 0)
        contents.pack_start(labels, False, False, 0)
        drag.add(contents)
        body.pack_start(drag, True, True, 0)

        self.menu = Gtk.Menu()
        for title, callback in [("立即休息一分钟", lambda *_: app.start_break()),
                                ("更换图片…", lambda *_: app.choose_image()),
                                ("恢复默认图片", lambda *_: app.restore_image())]:
            item = Gtk.MenuItem(label=title)
            item.connect("activate", callback)
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.autostart_item = Gtk.CheckMenuItem(label="登录桌面时自动启动")
        self.autostart_item.set_active(autostart_enabled(app.paths.autostart))
        self.autostart_handler = self.autostart_item.connect("toggled", self.on_autostart)
        self.menu.append(self.autostart_item)
        exit_item = Gtk.MenuItem(label="退出程序")
        exit_item.connect("activate", lambda *_: app.exit_application())
        self.menu.append(exit_item)
        self.menu.show_all()
        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.MENU))
        menu_button.set_tooltip_text("立即休息、更换图片、设置自启动")
        menu_button.set_popup(self.menu)
        body.pack_start(menu_button, False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_tooltip_text("退出程序；若已开启自启动，下次登录仍会启动")
        close.connect("clicked", lambda *_: app.exit_application())
        body.pack_start(close, False, False, 0)
        self.connect("delete-event", self.on_delete)
        self.connect("configure-event", self.on_configure)

    def on_drag(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def on_delete(self, *_):
        self.app.exit_application()
        return True

    def on_configure(self, *_):
        if self.app.position_source:
            GLib.source_remove(self.app.position_source)
        self.app.position_source = GLib.timeout_add(600, self.app.save_position)
        return False

    def on_autostart(self, item):
        enabled = item.get_active()
        try:
            if enabled:
                write_managed_entry(self.app.paths.autostart, Path(__file__), True)
            else:
                remove_managed_entry(self.app.paths.autostart)
        except (OSError, ValueError) as error:
            item.handler_block(self.autostart_handler)
            item.set_active(autostart_enabled(self.app.paths.autostart))
            item.handler_unblock(self.autostart_handler)
            self.app.show_error(f"无法更改自启动设置：{error}")
        else:
            LOG.info("Autostart changed: %s", enabled)

    def place(self):
        display = self.get_display()
        primary = display.get_primary_monitor()
        monitors = [display.get_monitor(i) for i in range(display.get_n_monitors())]
        if primary in monitors:
            monitors.remove(primary)
            monitors.insert(0, primary)
        areas = []
        for monitor in monitors:
            rect = monitor.get_workarea()
            areas.append((rect.x, rect.y, rect.width, rect.height))
        width, height = self.get_size()
        self.move(*clamp_position(self.app.settings.position, width, height, areas))
        return GLib.SOURCE_REMOVE


class BreakWindow(Gtk.ApplicationWindow):
    def __init__(self, app, monitor_index):
        super().__init__(application=app, title="护眼休息 · 一分钟")
        self.app = app
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.get_style_context().add_class("break-window")
        self.connect("delete-event", self.on_delete)
        self.connect("key-press-event", self.on_key)
        monitor = self.get_display().get_monitor(monitor_index)
        rect = monitor.get_geometry()
        self.move(rect.x, rect.y)
        self.set_default_size(rect.width, rect.height)
        self.fullscreen_on_monitor(self.get_screen(), monitor_index)

        overlay = Gtk.Overlay()
        self.canvas = Gtk.DrawingArea()
        self.canvas.connect("size-allocate", lambda *_: self.refresh_background())
        overlay.add(self.canvas)
        self.background_widget = Gtk.Image()
        self.background_widget.set_halign(Gtk.Align.CENTER)
        self.background_widget.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.background_widget)
        overlay.set_overlay_pass_through(self.background_widget, True)
        self.rendered_size = None
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panel.set_halign(Gtk.Align.CENTER)
        panel.set_valign(Gtk.Align.CENTER)
        panel.get_style_context().add_class("rest-panel")
        panel.pack_start(styled_label("让眼睛休息一下", "rest-heading"), False, False, 0)
        self.countdown = styled_label("01:00", "rest-countdown")
        panel.pack_start(self.countdown, False, False, 0)
        panel.pack_start(styled_label("看看远处，也可以轻轻闭上眼睛", "rest-hint"), False, False, 0)
        panel.pack_start(styled_label("一分钟后，自动回到桌面", "rest-detail"), False, False, 0)
        button = Gtk.Button(label="结束本次休息")
        button.set_halign(Gtk.Align.CENTER)
        button.set_tooltip_text("也可按 Esc；下一整点仍会提醒")
        button.get_style_context().add_class("rest-exit")
        button.connect("clicked", lambda *_: app.finish_break())
        panel.pack_start(button, False, False, 0)
        overlay.add_overlay(panel)
        self.add(overlay)

    def refresh_background(self, force=False):
        pixbuf = self.app.background
        if not pixbuf:
            self.background_widget.clear()
            return
        width, height = self.canvas.get_allocated_width(), self.canvas.get_allocated_height()
        if width > 1 and height > 1 and (force or self.rendered_size != (width, height)):
            scale = min(width / pixbuf.get_width(), height / pixbuf.get_height())
            scaled = pixbuf.scale_simple(max(1, round(pixbuf.get_width() * scale)),
                                         max(1, round(pixbuf.get_height() * scale)),
                                         GdkPixbuf.InterpType.BILINEAR)
            self.background_widget.set_from_pixbuf(scaled)
            self.rendered_size = (width, height)

    def set_remaining(self, seconds):
        text = f"{seconds // 60:02d}:{seconds % 60:02d}"
        if self.countdown.get_text() != text:
            self.countdown.set_text(text)

    def on_delete(self, *_):
        self.app.finish_break()
        return True

    def on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.app.finish_break()
            return True
        return False


class EyeRestApp(Gtk.Application):
    def __init__(self, application_id=APP_ID):
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.paths = Paths.discover()
        self.settings = Settings(self.paths.config)
        self.scheduler = HourScheduler(time.time(), time.monotonic())
        self.floating = None
        self.overlays = []
        self.timer = None
        self.background = None
        self.inhibit_cookie = 0
        self.position_source = 0
        self.layout_source = 0
        self.tick_source = 0
        self.locked = False
        self.sleeping = False
        self.exiting = False
        self.importing = False
        self.dialogs = set()
        self.bus_subscriptions = []
        self.screen_handlers = []
        self.exit_sources = []

    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_from_file(str(ROOT / "assets/icon.svg"))
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.load_background()
        self.watch_session()
        screen = Gdk.Screen.get_default()
        for signal_name in ("monitors-changed", "size-changed"):
            self.screen_handlers.append((screen, screen.connect(signal_name, self.on_layout_changed)))
        for number in (signal.SIGTERM, signal.SIGINT):
            self.exit_sources.append(GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number,
                                                         self.on_exit_signal))

    def do_command_line(self, command_line):
        arguments = command_line.get_arguments()[1:]
        if arguments == ["--help"]:
            command_line.print_literal("用法：eye-rest [--quit | --break-now]\n")
            return 0
        if any(argument not in {"--quit", "--break-now"} for argument in arguments):
            command_line.printerr_literal("用法：eye-rest [--quit | --break-now]\n")
            return 2
        if "--quit" in arguments:
            self.exit_application()
        else:
            self.activate()
            if "--break-now" in arguments:
                self.start_break()
        return 0

    def do_activate(self):
        if self.floating is None:
            self.floating = FloatingWindow(self)
            self.floating.show_all()
            self.floating.place()
            GLib.idle_add(self.floating.place)
            self.tick_source = GLib.timeout_add(200, self.tick)
            self.tick()
            LOG.info("Started; next break at %s", datetime.fromtimestamp(self.scheduler.next_due))
        elif self.timer:
            for window in self.overlays:
                window.present()
        else:
            self.floating.show_all()
            self.floating.place()
        if self.settings.warning:
            LOG.warning(self.settings.warning)
            self.floating.set_tooltip_text(self.settings.warning)
            self.settings.warning = None

    def tick(self):
        try:
            steady = time.monotonic()
            trigger = self.scheduler.poll(time.time(), steady,
                                          self.timer is not None or self.locked or self.sleeping)
            if self.timer:
                remaining = self.timer.remaining(steady)
                if remaining == 0:
                    self.finish_break()
                else:
                    for overlay in self.overlays:
                        overlay.set_remaining(remaining)
            elif trigger:
                self.start_break()
            if self.floating:
                status = "下次 " + datetime.fromtimestamp(self.scheduler.next_due).strftime("%H:%M")
                if self.floating.status.get_text() != status:
                    self.floating.status.set_text(status)
        except Exception:
            LOG.exception("Timer callback failed")
            self.finish_break()
        return GLib.SOURCE_CONTINUE

    def start_break(self):
        if self.timer or self.exiting or self.locked or self.sleeping:
            return
        for dialog in list(self.dialogs):
            dialog.response(Gtk.ResponseType.CANCEL)
        if self.floating:
            self.save_position()
            self.floating.menu.popdown()
            self.floating.hide()
        self.timer = BreakTimer.start(time.monotonic())
        try:
            self.create_overlays()
            if not self.overlays:
                raise RuntimeError("没有可用的显示器")
            self.inhibit_cookie = self.inhibit(self.overlays[0], Gtk.ApplicationInhibitFlags.IDLE,
                                               "正在进行一分钟护眼休息")
            LOG.info("Break started")
        except Exception:
            LOG.exception("Could not start break")
            self.finish_break()

    def create_overlays(self):
        display = Gdk.Display.get_default()
        for index in range(display.get_n_monitors()):
            window = BreakWindow(self, index)
            self.overlays.append(window)
            if self.timer:
                window.set_remaining(self.timer.remaining(time.monotonic()))
            window.show_all()
        primary = display.get_primary_monitor()
        for index, window in enumerate(self.overlays):
            if display.get_monitor(index) == primary:
                window.present()
                break

    def finish_break(self):
        was_active = self.timer is not None
        self.timer = None
        if self.inhibit_cookie:
            self.uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0
        for window in self.overlays:
            window.destroy()
        self.overlays.clear()
        if self.floating and not self.exiting:
            self.floating.show_all()
        if was_active:
            LOG.info("Break ended")

    def save_position(self):
        if self.position_source:
            GLib.source_remove(self.position_source)
            self.position_source = 0
        if self.floating and self.floating.get_visible():
            position = tuple(self.floating.get_position())
            if self.settings.position != position:
                self.settings.position = position
                self.save_settings()
        return GLib.SOURCE_REMOVE

    def save_settings(self):
        try:
            self.settings.save()
            return True
        except OSError as error:
            LOG.exception("Could not save settings")
            self.show_error(f"设置未能保存，下次启动可能恢复原设置：{error}")
            return False

    def on_layout_changed(self, *_):
        if self.layout_source:
            GLib.source_remove(self.layout_source)
        self.layout_source = GLib.timeout_add(300, self.apply_layout)

    def apply_layout(self):
        self.layout_source = 0
        if self.exiting:
            return GLib.SOURCE_REMOVE
        if self.floating:
            self.floating.place()
        if self.timer:
            if self.inhibit_cookie:
                self.uninhibit(self.inhibit_cookie)
                self.inhibit_cookie = 0
            for window in self.overlays:
                window.destroy()
            self.overlays.clear()
            try:
                self.create_overlays()
                if self.overlays:
                    self.inhibit_cookie = self.inhibit(self.overlays[0], Gtk.ApplicationInhibitFlags.IDLE,
                                                       "正在进行一分钟护眼休息")
                else:
                    self.finish_break()
            except Exception:
                LOG.exception("Could not update monitor layout")
                self.finish_break()
        return GLib.SOURCE_REMOVE

    def load_background(self):
        custom = self.settings.custom_image
        path = self.paths.image if custom else ROOT / "assets/default.svg"
        try:
            self.background = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except GLib.Error:
            LOG.exception("Could not load image: %s", path)
            self.settings.custom_image = False
            self.background = None
            if custom:
                self.settings.warning = "自定义图片无法读取，已恢复默认背景；可从菜单重新选择图片。"
                self.load_background()
        for window in self.overlays:
            window.refresh_background(force=True)

    def run_dialog(self, dialog):
        self.dialogs.add(dialog)
        try:
            return dialog.run()
        finally:
            self.dialogs.discard(dialog)
            dialog.destroy()

    def show_error(self, message):
        if self.exiting or not self.floating:
            LOG.error(message)
            return
        parent = self.overlays[0] if self.overlays else self.floating
        dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text="护眼休息")
        dialog.format_secondary_text(message)
        self.run_dialog(dialog)

    def choose_image(self):
        if self.importing:
            return
        dialog = Gtk.FileChooserDialog(title="选择休息图片", transient_for=self.floating,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "使用这张图片", Gtk.ResponseType.OK)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("图片（PNG、JPEG）")
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        dialog.add_filter(image_filter)
        preview = Gtk.Image()
        dialog.set_preview_widget(preview)

        def update_preview(chooser):
            try:
                filename = chooser.get_preview_filename()
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(filename or "", 320, 220, True)
                preview.set_from_pixbuf(pixbuf)
                chooser.set_preview_widget_active(True)
            except (GLib.Error, TypeError):
                chooser.set_preview_widget_active(False)
        dialog.connect("update-preview", update_preview)
        self.dialogs.add(dialog)
        response = dialog.run()
        filename = dialog.get_filename()
        self.dialogs.discard(dialog)
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not filename or self.exiting:
            return
        self.importing = True
        self.floating.set_tooltip_text("正在导入图片…")

        def worker():
            error = None
            try:
                import_image(Path(filename), self.paths.image)
            except Exception as exception:
                LOG.exception("Image import failed")
                error = str(exception)
            GLib.idle_add(self.image_imported, error)
        threading.Thread(target=worker, name="image-import", daemon=True).start()

    def image_imported(self, error):
        self.importing = False
        if self.exiting:
            return GLib.SOURCE_REMOVE
        if error:
            self.show_error("图片导入失败，原来的图片已保留。\n" + error)
        else:
            self.settings.custom_image = True
            self.save_settings()
            self.load_background()
            self.floating.set_tooltip_text("图片已更新；点击菜单中的“立即休息一分钟”可以查看效果。")
        return GLib.SOURCE_REMOVE

    def restore_image(self):
        if self.importing:
            return
        self.settings.custom_image = False
        self.save_settings()
        self.load_background()
        self.floating.set_tooltip_text("已恢复默认背景。")

    def watch_session(self):
        try:
            session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            subscription = session_bus.signal_subscribe(
                "org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
                "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE, self.on_lock_changed)
            self.bus_subscriptions.append((session_bus, subscription))
            result = session_bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                           "org.gnome.ScreenSaver", "GetActive", None,
                                           GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
            self.locked = result.unpack()[0]
        except GLib.Error:
            LOG.warning("Screen lock service is unavailable")
        try:
            system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            subscription = system_bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self.on_sleep_changed)
            self.bus_subscriptions.append((system_bus, subscription))
        except GLib.Error:
            LOG.warning("Sleep service is unavailable; missed-hour detection remains active")

    def on_lock_changed(self, _connection, _sender, _path, _interface, _signal, parameters):
        self.locked = parameters.unpack()[0]
        self.scheduler.reset(time.time(), time.monotonic())
        if self.locked:
            self.finish_break()

    def on_sleep_changed(self, _connection, _sender, _path, _interface, _signal, parameters):
        self.sleeping = parameters.unpack()[0]
        self.scheduler.reset(time.time(), time.monotonic())
        self.finish_break()

    def on_exit_signal(self):
        self.exit_application()
        return GLib.SOURCE_CONTINUE

    def exit_application(self):
        if not self.exiting:
            self.save_position()
            self.exiting = True
            self.finish_break()
            for dialog in list(self.dialogs):
                dialog.response(Gtk.ResponseType.CANCEL)
            if self.floating:
                self.floating.destroy()
                self.floating = None
            self.quit()

    def do_shutdown(self):
        self.exiting = True
        self.finish_break()
        for source in [self.tick_source, self.position_source, self.layout_source, *self.exit_sources]:
            if source:
                GLib.source_remove(source)
        for connection, subscription in self.bus_subscriptions:
            connection.signal_unsubscribe(subscription)
        for screen, handler in self.screen_handlers:
            screen.disconnect(handler)
        LOG.info("Stopped")
        Gtk.Application.do_shutdown(self)


def main():
    paths = Paths.discover()
    try:
        log_dir = paths.data / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "app.log", maxBytes=200_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
    except OSError:
        LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)
    if not Gdk.Display.get_default():
        print("请在图形桌面中启动护眼休息。", file=sys.stderr)
        return 1
    Gdk.set_program_class("eye-rest")
    return EyeRestApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
