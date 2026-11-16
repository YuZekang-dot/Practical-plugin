# APP · 本地桌面工具

用于存放可以在本地运行的桌面应用。每个应用拥有独立目录、使用文档和启动入口。

## 应用列表

| 应用 | 简介 | 适配环境 |
| --- | --- | --- |
| [Eye Rest · 护眼休息](eye-rest/README.md) | 每到整点显示一分钟全屏休息图片，支持桌面浮窗、自定义背景和登录自启动 | Ubuntu 24.04 / GNOME / X11 |

## 快速开始：Eye Rest

在本目录打开终端：

```sh
cd eye-rest
./install.sh
```

安装后，从桌面浮窗使用程序，也可以在系统应用菜单中搜索“护眼休息”。默认开启登录自启动。

如果只想直接运行：

```sh
cd eye-rest
./start.sh
```

依赖安装、图片更换、命令行参数和卸载方法请参阅 [Eye Rest 使用说明](eye-rest/README.md)。

## 目录结构

```text
APP/
├── README.md
├── .gitignore
└── eye-rest/
    ├── README.md
    ├── app.py
    ├── core.py
    ├── manage.py
    ├── start.sh
    ├── install.sh
    ├── uninstall.sh
    ├── assets/
    ├── docs/
    └── tests/
```

应用运行产生的个人设置、图片副本和日志存放在用户配置及数据目录中，不属于此源码目录。
