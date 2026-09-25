#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 系统托盘界面。

托盘只负责展示状态和发出控制请求，抓取工作仍由主模块的后台线程完成，
因此右键菜单不会阻塞 6 小时的服务循环。
"""

import os
import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem

import autostart


APP_TITLE = "宿舍电费监控"
_REFRESH_SECONDS = 2


class TrayController:
    def __init__(self, get_status, request_run, request_stop, local_url, log_file):
        self.get_status = get_status
        self.request_run = request_run
        self.request_stop = request_stop
        self.local_url = local_url
        self.log_file = log_file
        self.icon = pystray.Icon(
            "dorm-electricity-monitor",
            self._make_image("starting"),
            APP_TITLE,
            menu=self._make_menu(),
        )
        self._stop_refresh = threading.Event()
        self._refresh_thread = None
        self._autostart_busy = False
        self._autostart_lock = threading.Lock()

    @staticmethod
    def _make_image(state):
        colors = {
            "starting": (100, 116, 139, 255),
            "running": (37, 99, 235, 255),
            "success": (16, 185, 129, 255),
            "error": (239, 68, 68, 255),
        }
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 3, 61, 61), radius=15,
                               fill=colors.get(state, colors["starting"]))
        # 简单的闪电图形，不依赖外部图标文件，方便 onefile/onedir 打包。
        draw.polygon(
            [(36, 8), (18, 35), (30, 35), (25, 56),
             (47, 27), (35, 27)],
            fill=(255, 255, 255, 255),
        )
        return image

    @staticmethod
    def _format_time(timestamp):
        if not timestamp:
            return "暂无"
        return time.strftime("%m-%d %H:%M", time.localtime(timestamp))

    def _status_line(self):
        status = self.get_status()
        state = status.get("state", "starting")
        balance = status.get("balance")
        last_success = status.get("last_success")
        if state == "running":
            return "正在抓取电费..."
        if state == "error":
            error = status.get("last_error") or "未知错误"
            return f"运行异常：{error[:35]}"
        if balance is None:
            return "等待首次抓取"
        return f"余额 ¥{balance:.2f} · 更新于 {self._format_time(last_success)}"

    def _tooltip(self):
        status = self.get_status()
        state = status.get("state", "starting")
        balance = status.get("balance")
        if state == "error":
            return f"{APP_TITLE}：运行异常"
        if balance is None:
            return f"{APP_TITLE}：等待抓取"
        return f"{APP_TITLE}：¥{balance:.2f}"

    def _make_menu(self):
        return pystray.Menu(
            MenuItem(lambda item: self._status_line(), None, enabled=False),
            MenuItem(lambda item: self._details_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            MenuItem("立即抓取", self._request_run),
            MenuItem("打开网页报告", self._open_report),
            MenuItem(
                lambda item: self._autostart_label(),
                self._toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            MenuItem("查看运行日志", self._open_log),
            pystray.Menu.SEPARATOR,
            MenuItem("退出监控", self._exit),
        )

    def _details_line(self):
        status = self.get_status()
        last_run = self._format_time(status.get("last_run"))
        history_count = status.get("history_count", 0)
        return f"上次运行：{last_run} · 存档 {history_count} 条"

    @staticmethod
    def _autostart_label():
        return autostart.status_text()

    def _request_run(self, icon, item):
        status = self.get_status()
        if status.get("state") == "running":
            self._notify("当前正在抓取，请稍候。")
            return
        self.request_run()
        self._notify("已请求立即抓取。")

    def _open_report(self, icon, item):
        try:
            webbrowser.open(self.local_url)
        except Exception as exc:
            self._notify(f"打开报告失败：{exc}")

    def _open_log(self, icon, item):
        try:
            os.startfile(self.log_file)
        except OSError as exc:
            self._notify(f"打开日志失败：{exc}")

    def _toggle_autostart(self, icon, item):
        with self._autostart_lock:
            if self._autostart_busy:
                self._notify("正在处理开机自启设置，请稍候。")
                return
            self._autostart_busy = True

        def work():
            try:
                ok, message, _ = autostart.toggle()
                self._notify(message)
            except Exception as exc:
                ok = False
                self._notify(f"开机自启设置失败：{exc}")
            finally:
                with self._autostart_lock:
                    self._autostart_busy = False
                try:
                    self.icon.update_menu()
                except Exception:
                    pass

        threading.Thread(target=work, name="autostart-toggle", daemon=True).start()

    def _notify(self, message):
        try:
            self.icon.notify(message, APP_TITLE)
        except Exception:
            pass

    def _exit(self, icon, item):
        self.request_stop()
        self._stop_refresh.set()
        icon.stop()

    def _refresh_once(self):
        status = self.get_status()
        state = status.get("state", "starting")
        self.icon.icon = self._make_image(state)
        self.icon.title = self._tooltip()

    def _setup(self, icon):
        icon.visible = True
        self._refresh_once()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="tray-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self):
        while not self._stop_refresh.wait(_REFRESH_SECONDS):
            try:
                self._refresh_once()
            except Exception:
                pass

    def run(self):
        self.icon.run(setup=self._setup)
        self._stop_refresh.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=2)


def run(get_status, request_run, request_stop, local_url, log_file):
    """在当前线程运行托盘消息循环。"""
    TrayController(get_status, request_run, request_stop, local_url, log_file).run()
