#!/usr/bin/python3
"""Install/uninstall in user directories. Never requires root."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from core import APP_ID, Paths, atomic_write, prepare_gui_environment, remove_managed_entry, write_managed_entry

prepare_gui_environment()

ROOT = Path(__file__).resolve().parent
MARKER = ".eye-rest-install.json"
FILES = ("app.py", "core.py", "manage.py", "start.sh", "install.sh", "uninstall.sh",
         "README.md", "assets/icon.svg", "assets/default.svg",
         "docs/images/float-preview.png", "docs/images/rest-preview.png")


def check_dependencies():
    result = subprocess.run([
        "/usr/bin/python3", "-c",
        "import gi; gi.require_version('Gtk', '3.0'); "
        "from gi.repository import Gtk, GdkPixbuf; from PIL import Image, ImageOps"],
        capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("缺少桌面组件。Ubuntu 可安装：\n"
                           "sudo apt install python3-gi gir1.2-gtk-3.0 python3-pil\n"
                           + result.stderr)


def verify_owned(directory):
    if directory.exists():
        if directory.is_symlink() or not (directory / MARKER).is_file():
            raise RuntimeError(f"安装目录已有其他内容，未覆盖或删除：{directory}")
        if json.loads((directory / MARKER).read_text()).get("application_id") != APP_ID:
            raise RuntimeError(f"安装标记不匹配，未修改：{directory}")


def stop_running():
    """Talk only to an existing owner; don't start an application just to stop it."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    import gi
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def has_owner():
        result = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (APP_ID,)),
                              GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 2000, None)
        return result.unpack()[0]
    if has_owner():
        result = subprocess.run(["/usr/bin/python3", str(ROOT / "app.py"), "--quit"],
                                capture_output=True, text=True, timeout=8)
        if result.returncode:
            raise RuntimeError("现有程序未能退出：" + result.stderr)
        deadline = time.monotonic() + 4
        while has_owner() and time.monotonic() < deadline:
            time.sleep(0.1)
        if has_owner():
            raise RuntimeError("现有程序仍在运行，请先通过浮窗退出。")


def install(args):
    check_dependencies()
    paths = Paths.discover()
    target = paths.installed_app
    verify_owned(target)
    fresh = not target.exists()
    # Detect conflicts before changing any installation files.
    for entry in (paths.applications, paths.autostart):
        if entry.exists() and "X-EyeRest-Managed=true" not in entry.read_text(encoding="utf-8"):
            raise RuntimeError(f"同名启动项已存在，未覆盖：{entry}")
    paths.data.mkdir(parents=True, exist_ok=True)
    if ROOT != target.resolve():
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=paths.data))
        backup = None
        try:
            for relative in FILES:
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            atomic_write(staging / MARKER, json.dumps({"application_id": APP_ID, "version": 1}) + "\n")
            if not args.no_start:
                stop_running()
            if target.exists():
                backup = Path(tempfile.mkdtemp(prefix=".previous-", dir=paths.data))
                backup.rmdir()
                target.rename(backup)
            try:
                staging.rename(target)
            except OSError:
                if backup:
                    backup.rename(target)
                    backup = None
                raise
            if backup:
                shutil.rmtree(backup)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    write_managed_entry(paths.applications, target / "app.py")
    if args.no_autostart:
        remove_managed_entry(paths.autostart)
    elif fresh or paths.autostart.exists():
        write_managed_entry(paths.autostart, target / "app.py", True)
    print(f"已安装：{target}\n应用菜单：护眼休息")
    print("登录自启动：" + ("开启" if paths.autostart.exists() else "关闭"))
    if not args.no_start:
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["/usr/bin/python3", str(target / "app.py")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            print("程序已启动，桌面右下角会显示小浮窗。")
        else:
            print("当前没有图形会话；登录桌面后可从应用菜单启动。")


def uninstall(args):
    paths = Paths.discover()
    verify_owned(paths.installed_app)
    stop_running()
    for entry in (paths.autostart, paths.applications):
        remove_managed_entry(entry)
    if paths.installed_app.exists():
        shutil.rmtree(paths.installed_app)
    if args.purge:
        # Remove only directories owned by this application.
        for directory in (paths.config, paths.data):
            if directory.is_symlink():
                raise RuntimeError(f"未清理符号链接：{directory}")
            if directory.exists():
                shutil.rmtree(directory)
        print("已卸载，并清理图片、设置和日志。")
    else:
        print("已卸载并关闭自启动。图片和设置已保留；使用 --purge 可同时清理。")


def main():
    parser = argparse.ArgumentParser(description="护眼休息：用户级安装与卸载")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    installer = subparsers.add_parser("install", help="安装并启动")
    installer.add_argument("--no-start", action="store_true", help="安装后不启动")
    installer.add_argument("--no-autostart", action="store_true", help="不启用登录自启动")
    uninstaller = subparsers.add_parser("uninstall", help="卸载")
    uninstaller.add_argument("--purge", action="store_true", help="同时删除图片、设置与日志")
    args = parser.parse_args()
    try:
        install(args) if args.operation == "install" else uninstall(args)
    except Exception as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
