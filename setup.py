#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键配置工具（命令行版）。

首次配置：
    python setup.py

说明：
- setup.py 重跑时会以现有 .env 的值为默认，直接回车即保留，
  不必重新填写所有信息。
- 该接口在校园网内仅凭 roomId 即可取数，无需任何登录凭证（JWT）。
- 生成的 .env 与脚本同目录。
- 更推荐用图形向导：python config_wizard.py（带 SMTP 测试连接）。
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")

# 常用邮箱服务商 SMTP 预设（服务商: 主机, 端口, TLS 模式）
SMTP_PRESETS = {
    "QQ 邮箱":     ("smtp.qq.com",   465, "ssl"),
    "163 邮箱":    ("smtp.163.com",  465, "ssl"),
    "126 邮箱":    ("smtp.126.com",  465, "ssl"),
    "Gmail":       ("smtp.gmail.com", 587, "starttls"),
    "Outlook":     ("smtp.office365.com", 587, "starttls"),
}


def load_existing():
    """读出现有 .env 的全部键值，用于预设默认值 + 保留自定义键。"""
    d = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


# setup.py 管理的标准键（写入时按固定顺序排列在文件顶部）
MANAGED_KEYS = ("XJTU_ROOM_ID", "XJTU_RECIPIENT",
                "SMTP_HOST", "SMTP_PORT", "SMTP_TLS", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")


def ask(prompt, default=""):
    hint = " [保留现有]" if default else ""
    try:
        val = input(f"{prompt}{hint}\n  > ").strip()
    except EOFError:
        val = ""
    return val or default


def main():
    old = load_existing()
    print("=" * 50)
    print("   宿舍电费监控 · 配置（重跑会保留已有值）")
    print("=" * 50)
    print("直接回车 = 保留当前 .env 里的对应值；想改哪条就填哪条。\n")

    room = ask("1) 宿舍房间号 roomId（ProxyPin 抓包请求 URL 里的 roomId 参数）",
               old.get("XJTU_ROOM_ID", ""))

    recipient = ask("2) 收件邮箱（接收预警/周报/月报）", old.get("XJTU_RECIPIENT", ""))
    if not recipient:
        print("\n⚠️ 收件邮箱不能为空。")
        sys.exit(1)

    print("\n3) SMTP 发件邮箱（用于发预警/周报/月报邮件）")
    preset_names = list(SMTP_PRESETS)
    print("   常用服务商预设：")
    for i, name in enumerate(preset_names, start=1):
        host, port, tls = SMTP_PRESETS[name]
        print(f"     {i}) {name}  ({host}:{port}, {tls})")
    try:
        choice = input("   选择编号（0 = 手动填写，直接回车 = 0）\n  > ").strip() or "0"
    except EOFError:
        choice = "0"
    if choice.isdigit() and 1 <= int(choice) <= len(preset_names):
        host, port, tls = SMTP_PRESETS[preset_names[int(choice) - 1]]
        print(f"   ✅ 已选：{preset_names[int(choice) - 1]}")
    else:
        host = ask("   SMTP 主机（如 smtp.qq.com）", old.get("SMTP_HOST", ""))
        port = ask("   SMTP 端口（SSL 通常 465，STARTTLS 587）", old.get("SMTP_PORT", "465"))
        tls = ask("   加密方式（ssl / starttls / none）", old.get("SMTP_TLS", "ssl"))
        if not (host and port and tls):
            print("\n⚠️ SMTP 主机 / 端口 / 加密方式不能为空。")
            sys.exit(1)

    smtp_user = ask("   SMTP 账号（发件邮箱地址）", old.get("SMTP_USER", ""))
    smtp_pass = ask("   SMTP 授权码（不是登录密码！邮箱网页开启 SMTP 服务后获取）",
                    old.get("SMTP_PASS", ""))
    if not (smtp_user and smtp_pass):
        print("\n⚠️ SMTP 账号与授权码不能为空。")
        sys.exit(1)
    smtp_from = ask("   发件人地址（留空则用 SMTP 账号）", old.get("SMTP_FROM", ""))

    # 可选参数（直接回车 = 使用内置默认值；.env 里已有则回车保留现有值）
    OPTIONAL = [
        ("THRESHOLD",             "低余额预警线（元）",   "10.0"),
        ("PRICE_PER_KWH",         "电价（元/度）",         "0.0"),
        ("REPORT_CYCLE_DAYS",     "周报周期（天）",        "7"),
        ("MONTHLY_CYCLE_DAYS",    "月报周期（天）",        "30"),
        ("RETENTION_DAYS",        "历史存档保留（天）",    "100"),
        ("ANOMALY_MIN_SAMPLES",   "异常检测最少记录数",    "20"),
        ("ANOMALY_MULTIPLE",      "异常倍数阈值",          "3.0"),
        ("ANOMALY_MIN_USAGE",     "异常最小消耗（元）",    "5.0"),
        ("ANOMALY_COOLDOWN_DAYS", "异常邮件冷却（天）",    "1"),
        ("RECHARGE_CAP_MULT",     "充值柱封顶倍数",        "2.0"),
    ]
    print("\n---- 可选参数（不填 = 用程序内置默认值）----")
    opts = {}
    for i, (key, name, default) in enumerate(OPTIONAL, start=4):
        val = ask(f"{i}) {name} [内置默认 {default}]", old.get(key, ""))
        if val:
            opts[key] = val

    managed = {
        "XJTU_ROOM_ID": room,
        "XJTU_RECIPIENT": recipient,
        "SMTP_HOST": host,
        "SMTP_PORT": str(port),
        "SMTP_TLS": tls,
        "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass,
        "SMTP_FROM": smtp_from,
    }
    # 收集旧 .env 中非标准键（setup.py 不管理的自定义配置，如 XJTU_SERVE_PORT 等）
    opt_keys = set(opts)
    extra_lines = [f"{k}={v}" for k, v in old.items()
                   if k not in MANAGED_KEYS and k not in opt_keys]
    extra = "\n".join(extra_lines)
    opt = "\n".join(f"{k}={v}" for k, v in opts.items())
    content = (
        "# 本地配置，已被 .gitignore 排除，严禁提交\n"
        f"XJTU_ROOM_ID={room}\n"
        f"XJTU_RECIPIENT={recipient}\n"
        f"SMTP_HOST={host}\n"
        f"SMTP_PORT={port}\n"
        f"SMTP_TLS={tls}\n"
        f"SMTP_USER={smtp_user}\n"
        f"SMTP_PASS={smtp_pass}\n"
        f"SMTP_FROM={smtp_from}\n"
    )
    if opt:
        content += ("\n# ---- 以下为可选参数（不填则用程序内置默认值） ----\n"
                    + opt + "\n")
    if extra:
        content += f"\n# ---- 以下为自定义配置（由 setup.py 自动保留） ----\n" + extra + "\n"
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ 配置已写入：{ENV_PATH}")
    print("下一步：运行 `python dorm_elec_auto.py` 测试一次。")


if __name__ == "__main__":
    main()
