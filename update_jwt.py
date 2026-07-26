#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅更新 .env 中的 XJTU_CEMS_JWT（凭证续期用），其他配置原样保留。

用法：
    python update_jwt.py            # 交互粘贴新 JWT
    python update_jwt.py "eyJ..."   # 直接把新 JWT 作为命令行参数传入

说明：
- 这是「凭证每 30 天到期」场景的专用快捷工具，只动 XJTU_CEMS_JWT 行，
  不动房间号 / 收件邮箱 / 网页链接等其他配置。
- 更新后会校验 JWT 格式与过期时间，告诉你新凭证何时到期。
"""
import os
import sys
import re
import base64
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def parse_exp(tok):
    """解析 JWT 的 exp（过期时间戳）；非标准 JWT 返回 None。"""
    try:
        parts = tok.split(".")
        if len(parts) < 2:
            return None
        p = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p))
        return payload.get("exp")
    except Exception:
        return None


def main():
    # 1) 取新 JWT：优先命令行参数，否则交互输入
    if len(sys.argv) > 1:
        new = sys.argv[1].strip().strip('"').strip("'")
    else:
        try:
            new = input("粘贴新的 JWT（ProxyPin 抓包得到的 Cookie）：\n  > ").strip()
        except EOFError:
            new = ""
    if not new:
        print("⚠️ 未提供 JWT，已取消。")
        sys.exit(1)

    # 2) 格式初检
    if new.count(".") != 2:
        print("⚠️ 这串不像标准 JWT（应为 3 段、用点分隔）。请确认从抓包记录里"
              "复制的是完整的 Request Cookies 值。")
        sys.exit(1)

    # 3) 过期时间校验
    exp = parse_exp(new)
    if isinstance(exp, int):
        left = (exp - time.time()) / 86400
        if left <= 0:
            print(f"⚠️ 该 JWT 已过期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}），"
                  f"请重新抓包后再更新。")
            sys.exit(1)
        print(f"✅ JWT 格式有效，将于 "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))} 过期，"
              f"剩余 {left:.1f} 天")

    # 4) 读现有 .env，仅替换 XJTU_CEMS_JWT 行，其他行原样保留
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    replaced = False
    out = []
    for line in lines:
        if re.match(r"\s*XJTU_CEMS_JWT\s*=", line):
            out.append(f"XJTU_CEMS_JWT={new}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"XJTU_CEMS_JWT={new}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)

    print(f"✅ 已更新 {ENV_PATH} 的 XJTU_CEMS_JWT，其他配置保持不变。")


if __name__ == "__main__":
    main()
