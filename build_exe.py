#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包脚本：把宿舍电费监控打包成 Windows exe。

用法：
    python build_exe.py            # 默认 onedir 模式（推荐：启动快、体积小）
    python build_exe.py --onefile  # 单文件 exe（首启稍慢，体积更大）

前置：
    pip install pyinstaller requests matplotlib numpy pystray

产物：
    dist/宿舍电费监控/宿舍电费监控.exe   （onedir，目录内含依赖）
        或
    dist/宿舍电费监控.exe               （onefile，单文件）

说明：
- --windowed：无控制台黑窗，后台常驻（首次启动会弹 tkinter 配置向导）。
- --collect-all matplotlib：matplotlib 的字体/样式数据必须显式收集，否则绘图崩溃。
- 数据文件（.env/存档/日志/deploy）运行时落在 exe 同目录，不打包进 exe。
"""
import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "宿舍电费监控"
ENTRY = os.path.join(HERE, "dorm_elec_auto.py")


def ensure_pyinstaller():
    """确认 PyInstaller 可用，缺失则提示安装。"""
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("!! 未检测到 PyInstaller。请先执行：")
        print("    pip install pyinstaller")
        return False


def clean():
    """清理上一次的构建产物，避免残留干扰。"""
    for d in ("build", "dist"):
        p = os.path.join(HERE, d)
        if os.path.isdir(p):
            print(f"清理 {p} ...")
            shutil.rmtree(p, ignore_errors=True)
    spec = os.path.join(HERE, APP_NAME + ".spec")
    if os.path.exists(spec):
        os.remove(spec)


def build(onefile=False):
    if not ensure_pyinstaller():
        sys.exit(1)
    clean()

    exclude = [
        "torch", "torchvision", "tensorflow", "tensorboard",
        "scipy", "pandas", "notebook", "jupyter",
        "sympy", "numba", "llvmlite", "botocore",
        "dns", "rich", "anyio", "pydantic",
        "pygments", "lxml", "jinja2",
        "PIL.ImageFilter",  # 保留主体，排除特定子模块
    ]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                 # 无控制台窗口（GUI 程序）
        "--name", APP_NAME,
        # matplotlib 的数据文件（字体/样式）必须显式收集，否则运行时崩溃
        "--collect-all", "matplotlib",
        # 排除与本项目无关的沉重包，加速构建
    ] + [m for e in exclude for m in ("--exclude-module", e)] + [
        # matplotlib 绘图需要 PIL；其余隐式导入按需补充
        "--collect-all", "PIL",
        "--hidden-import", "PIL._tkinter_finder",  # tkinter 在 PyInstaller 下的桥接
        "--hidden-import", "pystray._win32",         # Windows 系统托盘后端
        # 运行时所需资源（若有）；当前脚本无外部静态资源，预留接口
        # "--add-data", os.path.join(HERE, "assets") + os.pathsep + "assets",
    ]
    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")        # 默认：启动快、对 matplotlib 友好
    cmd.append(ENTRY)

    print("=" * 60)
    print("执行打包：")
    print(" ".join(cmd))
    print("=" * 60)
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("!! 打包失败。")
        sys.exit(rc)

    # 提示产物位置
    if onefile:
        out = os.path.join(HERE, "dist", APP_NAME + ".exe")
    else:
        out = os.path.join(HERE, "dist", APP_NAME, APP_NAME + ".exe")
    print("=" * 60)
    print(f"打包完成！exe 位于：\n  {out}")
    print("\n下一步：把整个产物目录拷到任意位置，双击 exe 即可。")
    print("首次运行会弹出配置向导，按提示填房间号/邮箱/SMTP 即可。")
    if not onefile:
        print("（onedir 模式：请连同 exe 所在的整个文件夹一起分发）")


if __name__ == "__main__":
    onefile = "--onefile" in sys.argv
    build(onefile=onefile)
