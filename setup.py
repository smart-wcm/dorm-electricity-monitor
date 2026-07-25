#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键配置：回答几个问题，自动把配置写入 .env（与脚本同目录）。

用法：
    python setup.py
然后：
    python dorm_elec_auto.py        # 测试跑一次
"""
import os
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def ask(prompt, default=""):
    try:
        val = input(f"{prompt}\n  > ").strip()
    except EOFError:
        val = ""
    return val or default


def detect_agently():
    """探测 agently-cli 的常见安装位置。"""
    candidates = [
        r"D:\AI\node\global\agently-cli.cmd",
        os.path.expanduser(r"~\AppData\Roaming\npm\agently-cli.cmd"),
        os.path.expanduser(r"~\AppData\Local\npm\agently-cli.cmd"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # 在 PATH 里找
    try:
        out = subprocess.run(["where", "agently-cli.cmd"],
                             capture_output=True, text=True, shell=True)
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if lines:
            return lines[0]
    except Exception:
        pass
    return ""


def main():
    print("=" * 50)
    print("   宿舍电费监控 · 一键配置")
    print("=" * 50)
    print("按提示输入以下信息（带 [默认值] 的直接回车即可）：\n")

    jwt = ask("1) 登录 JWT（ProxyPin 抓包得到的 Cookie，含学号姓名，切勿外泄）")
    if not jwt:
        print("\n⚠️ JWT 不能为空。请先抓包获取后重新运行本脚本。")
        sys.exit(1)

    room = ask("2) 宿舍房间号 roomId", "2899")

    recipient = ask("3) 收件邮箱（接收预警/周报/月报，可用智能体邮箱自收发）")
    if not recipient:
        print("\n⚠️ 收件邮箱不能为空。")
        sys.exit(1)

    share = ask("4) 网页报告公网链接（CloudStudio 部署后获得，暂时可留空）", "")

    agently = ask("5) agently-cli 绝对路径（直接回车自动探测）", "")
    if not agently:
        agently = detect_agently()
        if agently:
            print(f"    ✅ 自动探测到: {agently}")
        else:
            print("    ⚠️ 未探测到 agently-cli，请先 `npm i -g agently-cli` 并 "
                  "`agently-cli auth login` 授权，稍后在 .env 手动补 AGENTLY_BIN。")

    content = (
        "# 本地配置（含凭证），已被 .gitignore 排除，严禁提交\n"
        f"XJTU_CEMS_JWT={jwt}\n"
        f"XJTU_ROOM_ID={room}\n"
        f"XJTU_RECIPIENT={recipient}\n"
        f"XJTU_SHARE_URL={share}\n"
        f"AGENTLY_BIN={agently}\n"
    )
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n✅ 配置已写入：{ENV_PATH}")
    print("下一步：运行 `python dorm_elec_auto.py` 测试一次；")
    print("       定时任务配置见 README 第七章。")


if __name__ == "__main__":
    main()
