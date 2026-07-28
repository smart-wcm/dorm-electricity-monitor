#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键配置工具。

首次配置：
    python setup.py

说明：
- setup.py 重跑时会以现有 .env 的值为默认，直接回车即保留，
  不必重新填写所有信息。
- 该接口在校园网内仅凭 roomId 即可取数，无需任何登录凭证（JWT）。
- 更新的 .env 与脚本同目录。
"""
import os
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def load_existing():
    """读出现有 .env 的键值，仅用于预设默认值。"""
    d = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def ask(prompt, default=""):
    hint = " [保留现有]" if default else ""
    try:
        val = input(f"{prompt}{hint}\n  > ").strip()
    except EOFError:
        val = ""
    return val or default


def detect_agently():
    """探测 agently-cli 的常见安装位置。"""
    candidates = [
        os.path.expanduser(r"~\AppData\Roaming\npm\agently-cli.cmd"),
        os.path.expanduser(r"~\AppData\Local\npm\agently-cli.cmd"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    try:
        out = subprocess.run(["where", "agently-cli.cmd"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", shell=True)
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if lines:
            return lines[0]
    except Exception:
        pass
    return ""


def main():
    old = load_existing()
    print("=" * 50)
    print("   宿舍电费监控 · 配置（重跑会保留已有值）")
    print("=" * 50)
    print("直接回车 = 保留当前 .env 里的对应值；想改哪条就填哪条。\n")

    room = ask("1) 宿舍房间号 roomId（ProxyPin 抓包请求 URL 里的 roomId 参数）",
               old.get("XJTU_ROOM_ID", "2899"))

    recipient = ask("2) 收件邮箱（接收预警/周报/月报）", old.get("XJTU_RECIPIENT", ""))
    if not recipient:
        print("\n⚠️ 收件邮箱不能为空。")
        sys.exit(1)

    share = ask("3) 网页报告公网链接（CloudStudio 部署后获得，可留空）",
                old.get("XJTU_SHARE_URL", ""))

    agently = ask("4) agently-cli 绝对路径（回车自动探测）", old.get("AGENTLY_BIN", ""))
    if not agently:
        agently = detect_agently()
        if agently:
            print(f"    ✅ 自动探测到: {agently}")
        else:
            print("    ⚠️ 未探测到 agently-cli，请先 `npm i -g agently-cli` 并 "
                  "`agently-cli auth login` 授权，稍后在 .env 手动补 AGENTLY_BIN。")

    content = (
        "# 本地配置，已被 .gitignore 排除，严禁提交\n"
        f"XJTU_ROOM_ID={room}\n"
        f"XJTU_RECIPIENT={recipient}\n"
        f"XJTU_SHARE_URL={share}\n"
        f"AGENTLY_BIN={agently}\n"
    )
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ 配置已写入：{ENV_PATH}")
    print("下一步：运行 `python dorm_elec_auto.py` 测试一次。")


if __name__ == "__main__":
    main()
