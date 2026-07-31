#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次配置向导（图形界面）。

用途：双击 exe 首次启动时，若检测到 .env 不存在或关键字段缺失，
dorm_elec_auto.py 会调用 run_first_setup() 弹出本向导，引导用户填写
房间号、收件邮箱、SMTP 发件信息；填写完成后保存为 .env。

向导分两个页签：「基础配置」（必填）与「高级参数」（可选，
不填则用程序内置默认值）。

也可独立运行做「重新配置」：
    python config_wizard.py
"""
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# 常用邮箱服务商 SMTP 预设（主机, 端口, TLS 模式）
# 选不同服务商时自动填入这三项；发件账号/授权码仍需用户自己填。
SMTP_PRESETS = {
    "QQ 邮箱":     ("smtp.qq.com",   465, "ssl"),
    "163 邮箱":    ("smtp.163.com",  465, "ssl"),
    "126 邮箱":    ("smtp.126.com",  465, "ssl"),
    "Gmail":       ("smtp.gmail.com", 587, "starttls"),
    "Outlook":     ("smtp.office365.com", 587, "starttls"),
    "自定义":      ("", 465, "ssl"),
}

# 高级参数（可选）：(env 键名, 中文名, 内置默认值, 说明)
# 留空 = 不写入 .env，程序使用内置默认值；填了则覆盖默认值。
OPTIONAL_FIELDS = [
    ("THRESHOLD",          "低余额预警线（元）",   10.0, "余额低于此值发预警邮件"),
    ("PRICE_PER_KWH",      "电价（元/度）",         0.0, "填了会把金额折算成「度」，不知道留 0"),
    ("REPORT_CYCLE_DAYS",  "周报周期（天）",         7,  "每隔几天发一次周报"),
    ("MONTHLY_CYCLE_DAYS", "月报周期（天）",        30,  "每隔几天发一次月报"),
    ("RETENTION_DAYS",     "历史存档保留（天）",   100,  "仅保留最近 N 天记录"),
    ("ANOMALY_MIN_SAMPLES", "异常检测最少记录数",   20,  "积累多少条记录后启用异常检测"),
    ("ANOMALY_MULTIPLE",   "异常倍数阈值",          3.0, "日耗电 ≥ 基线中位数的 N 倍视为异常"),
    ("ANOMALY_MIN_USAGE",  "异常最小消耗（元）",    5.0, "单次消耗低于此值不报异常"),
    ("ANOMALY_COOLDOWN_DAYS", "异常邮件冷却（天）",  1,  "异常邮件最短间隔"),
    ("RECHARGE_CAP_MULT",  "充值柱封顶倍数",        2.0, "图表中充值柱高度封顶"),
]


def _env_path(app_dir):
    return os.path.join(app_dir, ".env")


def _load_existing(app_dir):
    """读出现有 .env 的键值，用于回填表单默认值。"""
    d = {}
    path = _env_path(app_dir)
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def _test_smtp(host, port, tls, user, pwd, to_addr, status_label, btn):
    """后台线程里跑 SMTP 连接 + 发测试邮件，避免阻塞界面。"""
    def task():
        try:
            import smtplib
            from email.message import EmailMessage
            from email.utils import formatdate
            if tls == "starttls":
                srv = smtplib.SMTP(host, int(port), timeout=20)
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
            elif tls == "none":
                srv = smtplib.SMTP(host, int(port), timeout=20)
            else:
                srv = smtplib.SMTP_SSL(host, int(port), timeout=20)
            srv.login(user, pwd)
            msg = EmailMessage()
            msg["From"] = user
            msg["To"] = to_addr
            msg["Subject"] = "✅ 电费监控测试邮件"
            msg["Date"] = formatdate(localtime=True)
            msg.set_content("这是一封来自「宿舍电费监控」配置向导的测试邮件。\n"
                            "如收到，说明 SMTP 配置正确，可以正常发送电费通知。")
            srv.send_message(msg)
            srv.quit()
            ok, info = True, "测试邮件已发送，请到收件箱查收（含垃圾箱）。"
        except Exception as e:
            ok, info = False, f"失败：{e}"
        # 回到主线程更新 UI
        btn["state"] = "normal"
        status_label.config(text=info,
                            foreground="green" if ok else "red")

    btn["state"] = "disabled"
    status_label.config(text="正在发送测试邮件…", foreground="black")
    threading.Thread(target=task, daemon=True).start()


def _save(app_dir, vals):
    """把表单值写入 .env（覆盖 setup.py 管理的标准键，保留其它自定义键）。"""
    old = _load_existing(app_dir)
    managed = {
        "XJTU_ROOM_ID": vals["room"],
        "XJTU_RECIPIENT": vals["to"],
        "SMTP_HOST": vals["host"],
        "SMTP_PORT": vals["port"],
        "SMTP_USER": vals["user"],
        "SMTP_PASS": vals["pwd"],
        "SMTP_FROM": vals["user"],  # 默认发件人 = 登录账号
        "SMTP_TLS": vals["tls"],
    }
    # 高级参数：仅写入用户填了非空值的键
    opt_lines = [f"{k}={v}" for k, v in vals.get("opts", {}).items()]
    opt_keys = {k for k in vals.get("opts", {})}
    extra_lines = [f"{k}={v}" for k, v in old.items()
                   if k not in managed and k not in opt_keys
                   and not k.startswith("SMTP_")]
    lines = ["# 本地配置，已被 .gitignore 排除，严禁提交"]
    for k, v in managed.items():
        lines.append(f"{k}={v}")
    if opt_lines:
        lines.append("")
        lines.append("# ---- 以下为可选参数（不填则用程序内置默认值） ----")
        lines.extend(opt_lines)
    if extra_lines:
        lines.append("")
        lines.append("# ---- 以下为自定义配置（由向导自动保留） ----")
        lines.extend(extra_lines)
    with open(_env_path(app_dir), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_first_setup(app_dir=None):
    """弹出图形配置向导。

    app_dir: .env 与数据的落脚目录（打包后为 exe 所在目录）。
    返回 True 表示用户已完成并保存；False 表示用户取消。
    无图形环境（无 DISPLAY / 无 tkinter）抛 RuntimeError，由调用方回退。
    """
    if app_dir is None:
        app_dir = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                   else os.path.dirname(os.path.abspath(__file__)))

    root = tk.Tk()
    root.title("宿舍电费监控 · 首次配置")
    root.resizable(False, False)

    old = _load_existing(app_dir)

    # ---- 表单变量 ----
    v_room = tk.StringVar(value=old.get("XJTU_ROOM_ID", ""))
    v_to = tk.StringVar(value=old.get("XJTU_RECIPIENT", ""))
    v_provider = tk.StringVar(value="QQ 邮箱")
    v_host = tk.StringVar(value=old.get("SMTP_HOST", "smtp.qq.com"))
    v_port = tk.StringVar(value=old.get("SMTP_PORT", "465"))
    v_tls = tk.StringVar(value=old.get("SMTP_TLS", "ssl"))
    v_user = tk.StringVar(value=old.get("SMTP_USER", ""))
    v_pwd = tk.StringVar(value=old.get("SMTP_PASS", ""))

    pad = {"padx": 8, "pady": 4}

    # ---- 标题 ----
    ttk.Label(root, text="首次使用，请填写以下信息（只需一次）",
              font=("Microsoft YaHei", 11, "bold")).pack(pady=(12, 6))

    # ---- 页签：基础配置 / 高级参数 ----
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=(0, 4))
    basic = ttk.Frame(nb, padding=8)
    adv = ttk.Frame(nb, padding=8)
    nb.add(basic, text="基础配置")
    nb.add(adv, text="高级参数（可选）")

    def row(r, text):
        ttk.Label(basic, text=text).grid(row=r, column=0, sticky="e", **pad)

    # ===== 基础配置页 =====
    row(0, "宿舍房间号 roomId：")
    ttk.Entry(basic, textvariable=v_room, width=40).grid(row=0, column=1, **pad)

    row(1, "收件邮箱：")
    ttk.Entry(basic, textvariable=v_to, width=40).grid(row=1, column=1, **pad)
    ttk.Label(basic, foreground="gray",
              text="接收电费预警/周报/月报的邮箱").grid(row=2, column=1, sticky="w", padx=8)

    ttk.Separator(basic, orient="horizontal").grid(row=3, column=0,
              columnspan=2, sticky="ew", padx=8, pady=8)
    ttk.Label(basic, text="邮件发送设置（SMTP）",
              font=("Microsoft YaHei", 10, "bold")).grid(row=4, column=0,
              columnspan=2, pady=(0, 4))

    row(5, "发件邮箱服务商：")
    provider_box = ttk.Combobox(basic, textvariable=v_provider,
                                values=list(SMTP_PRESETS.keys()),
                                state="readonly", width=36)
    provider_box.grid(row=5, column=1, **pad)

    def on_provider_change(event=None):
        host, port, tls = SMTP_PRESETS.get(v_provider.get(), ("", 465, "ssl"))
        if host:  # 自定义时 host 为空，不动用户已填值
            v_host.set(host)
        v_port.set(str(port))
        v_tls.set(tls)
    provider_box.bind("<<ComboboxSelected>>", on_provider_change)

    row(6, "SMTP 服务器：")
    ttk.Entry(basic, textvariable=v_host, width=40).grid(row=6, column=1, **pad)
    row(7, "SMTP 端口：")
    ttk.Entry(basic, textvariable=v_port, width=40).grid(row=7, column=1, **pad)
    row(8, "加密方式：")
    ttk.Combobox(basic, textvariable=v_tls, values=["ssl", "starttls", "none"],
                 state="readonly", width=36).grid(row=8, column=1, **pad)
    row(9, "发件邮箱账号：")
    ttk.Entry(basic, textvariable=v_user, width=40).grid(row=9, column=1, **pad)
    row(10, "SMTP 授权码：")
    ttk.Entry(basic, textvariable=v_pwd, width=40, show="*").grid(row=10, column=1, **pad)
    ttk.Label(basic, foreground="gray", justify="left",
              text="授权码 ≠ 登录密码！需到邮箱网页「设置→SMTP/POP3 服务」开启并获取。\n"
                   "例如 QQ 邮箱：设置 → 账户 → 开启 SMTP 服务 → 生成授权码。"
              ).grid(row=11, column=0, columnspan=2, sticky="w", padx=12)

    # ===== 高级参数页（可选）=====
    ttk.Label(adv, foreground="gray",
              text="以下参数留空即用程序内置默认值，填了会覆盖默认值。").grid(
              row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
    v_opts = {}
    for i, (key, name, default, hint) in enumerate(OPTIONAL_FIELDS):
        r = i + 1
        ttk.Label(adv, text=f"{name}（默认 {default}）").grid(
            row=r, column=0, sticky="e", padx=8, pady=3)
        entry = ttk.Entry(adv, width=14)
        entry.grid(row=r, column=1, padx=4, pady=3)
        if old.get(key, "") != "":
            entry.insert(0, old[key])
        v_opts[key] = entry
        ttk.Label(adv, text=hint, foreground="gray").grid(
            row=r, column=2, sticky="w", padx=8, pady=3)

    # ---- 状态行 + 按钮 ----
    status = ttk.Label(root, text="", foreground="black")
    status.pack(pady=(2, 2))

    btn_frame = ttk.Frame(root)
    btn_frame.pack(pady=(0, 10))

    def collect():
        opts = {}
        for key, _name, _default, _hint in OPTIONAL_FIELDS:
            raw = v_opts[key].get().strip()
            if raw:
                opts[key] = raw
        return {
            "room": v_room.get().strip(),
            "to": v_to.get().strip(),
            "host": v_host.get().strip(),
            "port": v_port.get().strip(),
            "tls": v_tls.get().strip().lower() or "ssl",
            "user": v_user.get().strip(),
            "pwd": v_pwd.get().strip(),
            "opts": opts,
        }

    def do_test():
        vals = collect()
        missing = []
        if not vals["host"]: missing.append("SMTP 服务器")
        if not vals["port"]: missing.append("SMTP 端口")
        if not vals["user"]: missing.append("发件账号")
        if not vals["pwd"]: missing.append("授权码")
        if not vals["to"]: missing.append("收件邮箱")
        if missing:
            messagebox.showwarning("信息不全", "请先填写：" + "、".join(missing))
            return
        try:
            int(vals["port"])
        except ValueError:
            messagebox.showwarning("端口无效", "SMTP 端口必须是数字。")
            return
        _test_smtp(vals["host"], vals["port"], vals["tls"],
                   vals["user"], vals["pwd"], vals["to"], status, test_btn)

    def do_save():
        vals = collect()
        if not vals["room"]:
            messagebox.showwarning("信息不全", "请填写宿舍房间号 roomId。")
            return
        if not vals["to"]:
            messagebox.showwarning("信息不全", "请填写收件邮箱。")
            return
        if not (vals["host"] and vals["user"] and vals["pwd"]):
            messagebox.showwarning("信息不全", "请填写完整的 SMTP 信息（服务器/账号/授权码）。")
            return
        try:
            int(vals["room"])
        except ValueError:
            messagebox.showwarning("房间号无效", "房间号必须是数字。")
            return
        try:
            int(vals["port"])
        except ValueError:
            messagebox.showwarning("端口无效", "SMTP 端口必须是数字。")
            return
        for key, name, default, _hint in OPTIONAL_FIELDS:
            raw = v_opts[key].get().strip()
            if not raw:
                continue
            try:
                if isinstance(default, int):
                    int(raw)
                else:
                    float(raw)
            except ValueError:
                messagebox.showwarning(
                    "参数无效", f"高级参数「{name}」必须是数字，当前填了：{raw!r}")
                return
        _save(app_dir, vals)
        messagebox.showinfo("完成", f"配置已保存到：\n{_env_path(app_dir)}\n\n"
                                    "即将开始监控服务。")
        root.result = True
        root.destroy()

    test_btn = ttk.Button(btn_frame, text="测试连接", command=do_test)
    test_btn.pack(side="left", padx=6)
    ttk.Button(btn_frame, text="保存并开始", command=do_save).pack(side="left", padx=6)
    ttk.Button(btn_frame, text="取消", command=root.destroy).pack(side="left", padx=6)

    # ---- 居中显示 ----
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    root.result = False
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return getattr(root, "result", False)


if __name__ == "__main__":
    ok = run_first_setup()
    print("配置完成" if ok else "已取消")
