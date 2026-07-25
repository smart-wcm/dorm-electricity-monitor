#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["requests", "matplotlib", "qrcode[pil]"]
# ///
# -*- coding: utf-8 -*-
"""
宿舍电费自动监控 · 增强版（已接入西交「cems」接口）
========================================================
每 6h 取一次余额 -> 存档(保留最近100天) -> 反推用电量 -> 低于阈值发邮件预警；
每 7 天发送「近一周·手机版」趋势图周报(柱状+余额折线) 到你的邮箱；
每 30 天发送「近30天·桌面横版」趋势图月报 到你的邮箱；
生成自包含网页报告(report.html, 手机版近30天) + 二维码，手机扫码/链接随时看。
通知渠道：Agent Mail CLI（agently-cli），以智能体邮箱身份发信；
          须先 `agently-cli auth login` 完成 OAuth 授权。

接口（已抓包确认）：
  GET https://ssn.xjtu.edu.cn/cems/mobile/meterAccount/electricity?roomId=2899
  响应: {"code":0,"data":36.58,"msg":"操作成功"}   # data 即余额(元)

部署重要说明：
  ssn.xjtu.edu.cn 是校内系统，本脚本必须运行在【校园网环境】
  （你电脑连校园网时）。推荐 Windows「任务计划程序」每 6h 跑一次。

数据存档：dorm_balance.json 仅保留最近 RETENTION_DAYS(默认100) 天，更早自动丢弃
（文件在电脑本地，建议定期备份/导出）。
========================================================
"""

import requests
import json
import time
import os
import base64
import io

# ===================== 1. 配置区 =====================
def _load_dotenv(path=None):
    """极简 .env 解析（避免额外依赖 python-dotenv）。

    默认读取『与脚本同目录』的 .env，因此无论任务计划的工作目录是什么都能找到，
    无需再额外配置系统环境变量。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


_LOCAL_ENV = _load_dotenv()


ROOM_ID = _LOCAL_ENV.get("XJTU_ROOM_ID") or os.environ.get("XJTU_ROOM_ID") or "2899"
API_URL = f"https://ssn.xjtu.edu.cn/cems/mobile/meterAccount/electricity?roomId={ROOM_ID}"

# 登录凭证改为环境变量 / .env 读取，代码中不再硬编码（安全开源，防泄露学号姓名）
# 优先级：本地 .env（与脚本同目录） > 系统环境变量 > 空。改 .env 即可生效，无需动系统变量。
JWT_COOKIE = (_LOCAL_ENV.get("XJTU_CEMS_JWT")
              or os.environ.get("XJTU_CEMS_JWT")
              or "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile",
    "Accept": "application/json, text/plain, */*",
    "Cookie": JWT_COOKIE,
}

# ===================== 2. 通知配置（Agent Mail） =====================
# 通道：通过 agently-cli（Agent Mail CLI）以你的「智能体邮箱」身份发信。
# 发件人 = 你已 OAuth 授权的智能体邮箱（运行 `agently-cli auth login` 授权后自动确定）。
# 收件人 = 你接收通知的邮箱地址；默认填你的智能体邮箱（自收发，落进同一收件箱）。
#   想发到别的邮箱，把下面改成你的常用邮箱即可（如 123456@qq.com）。
RECIPIENT = (_LOCAL_ENV.get("XJTU_RECIPIENT")
             or os.environ.get("XJTU_RECIPIENT")
             or "")
# agently-cli 可执行文件完整路径（各机器不同，建议设为环境变量 AGENTLY_BIN 或写进 .env）。
# 若任务计划报“找不到命令”，填绝对路径，例如：
#   AGENTLY_BIN = r"C:\Users\你的用户名\AppData\Roaming\npm\agently-cli.cmd"
AGENTLY_BIN = (_LOCAL_ENV.get("AGENTLY_BIN")
               or os.environ.get("AGENTLY_BIN")
               or r"D:\AI\node\global\agently-cli.cmd")
THRESHOLD = 10.0          # 低余额预警线（元）；想改阈值就改这一行
PRICE_PER_KWH = 0.0       # 电价(元/度)，填了会把金额折算成“度”；不知道就留 0
REPORT_CYCLE_DAYS = 7     # 每隔几天生成并推送一次「近一周·手机版」周报
MONTHLY_CYCLE_DAYS = 30    # 每隔几天生成并推送一次「近30天·桌面横版」月报
RETENTION_DAYS = 100      # 历史存档保留天数（仅保留最近 N 天，更早自动丢弃）；想改存档时长改这一行

# 异常用电检测（需先积累足够数据）
ANOMALY_MIN_SAMPLES = 20  # 至少积累多少条记录才启用异常检测
ANOMALY_MULTIPLE = 3.0    # 最新段日耗电 >= 基线(中位数)的 N 倍 视为异常
ANOMALY_MIN_USAGE = 5.0   # 单次消耗低于此值(元)不报异常，避免小波动误报
ANOMALY_COOLDOWN_DAYS = 1 # 异常邮件最短间隔(天)，避免连续轰炸

# ===================== 3. 文件/产物 =====================
STORE = "dorm_balance.json"        # 历史永久存档
CHART_PNG = "chart.png"            # 趋势图
DEPLOY_DIR = "deploy"              # 部署目录
REPORT_HTML = os.path.join(DEPLOY_DIR, "index.html")  # 网页报告(部署用)
QR_PNG = "qr.png"                  # 二维码
# 网页报告公网链接（二维码指向它）；部署后填入，建议用环境变量 XJTU_SHARE_URL 或写进 .env。
# 留空则二维码留白（可后续补填后重跑生成）。
SHARE_URL = (_LOCAL_ENV.get("XJTU_SHARE_URL")
             or os.environ.get("XJTU_SHARE_URL")
             or "")

# ===================== 4. 取余额 =====================
def fetch_balance():
    r = requests.get(API_URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise ValueError(f"接口返回异常: {data}")
    return float(data["data"])

# ===================== 5. 记录 & 计算（永久存储） =====================
def load_history():
    if os.path.exists(STORE):
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    return {"history": [], "last_report": 0}

def save_history(h):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)

def compute(history, balance):
    # 存档：仅保留最近 RETENTION_DAYS 天
    history.append({"t": int(time.time()), "b": balance})
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    history[:] = [x for x in history if x["t"] >= cutoff]
    now = int(time.time())
    last24 = cons = hours = 0.0
    window_start = now - 30 * 86400   # 统计窗口(最近30天)用于日均，避免早期数据稀释
    for i in range(1, len(history)):
        if history[i-1]["t"] < window_start:
            continue
        dt = (history[i]["t"] - history[i-1]["t"]) / 3600.0
        db = history[i-1]["b"] - history[i]["b"]   # 正=用电
        if db < 0:
            continue                               # 充值上涨不计入
        if history[i]["t"] >= now - 86400:
            last24 += db
        cons += db
        hours += dt
    avg_day = cons / hours * 24 if hours > 0 else 0.0
    return history, last24, avg_day

def detect_anomaly(history):
    """异常用电检测：积累足够数据后，若最新一段日耗电 >= 基线的 N 倍则报警。
    返回 (is_anomaly, info)。基线 = 除最新段外各耗电段日耗电率(元/天)的中位数。"""
    if len(history) < ANOMALY_MIN_SAMPLES:
        return False, {}
    pairs = []   # 仅计入“耗电段”(余额下降)
    for i in range(1, len(history)):
        dt = (history[i]["t"] - history[i - 1]["t"]) / 3600.0   # 时间正流逝
        usage = history[i - 1]["b"] - history[i]["b"]   # 早的余额高、晚的低 => 正=用电
        if dt > 0 and usage > 0:
            pairs.append((dt, usage))
    if len(pairs) < 5:
        return False, {}
    rates = [u / dt * 24 for dt, u in pairs]
    baseline = sorted(rates[:-1])[len(rates[:-1]) // 2]   # 除最新段的基线(中位数)
    last_dt, last_usage = pairs[-1]
    last_rate = last_usage / last_dt * 24
    if baseline < 1e-6:
        # 平时几乎不耗电，突然出现较大消耗即为异常
        is_anom = last_usage >= ANOMALY_MIN_USAGE
    else:
        is_anom = (last_usage >= ANOMALY_MIN_USAGE) and (last_rate >= ANOMALY_MULTIPLE * baseline)
    return is_anom, {"baseline": baseline, "last_rate": last_rate, "last_usage": last_usage}

# ===================== 6. 趋势图 =====================
def gen_chart(history, mobile=True, ndays=30):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    import numpy as np
    from matplotlib.patches import Patch, FancyBboxPatch
    from matplotlib.lines import Line2D
    from matplotlib.gridspec import GridSpec
    try:
        fp = FontProperties(fname=r"C:\Windows\Fonts\msyh.ttc")
    except Exception:
        fp = FontProperties()

    # ---- 调色板（柔和、低饱和、统一冷色调） ----
    C_BAL_LINE = "#2563EB"   # 余额线 深蓝
    C_BAL_FILL = "#DBEAFE"   # 余额填充 极浅蓝
    C_RECHARGE = "#10B981"   # 充值 清新绿
    C_USAGE    = "#5B8FD6"   # 正常用电 偏蓝（与余额线同色系）
    C_ALERT    = "#EF4444"   # 异常 柔和红
    C_WARN     = "#F59E0B"   # 预警线 柔和橙
    BG         = "#FFFFFF"   # 白底
    GRID       = "#E2E8F0"   # 网格极浅
    AXIS       = "#CBD5E0"   # 边框浅灰
    TXT        = "#334155"   # 文字深灰
    MUTE       = "#64748B"   # 次要文字

    now = int(time.time())
    start = now - ndays * 86400
    pts = [x for x in history if x["t"] >= start]
    if len(pts) < 2:
        pts = history
    days = {}
    for i in range(1, len(pts)):
        d = time.strftime("%m-%d", time.localtime(pts[i]["t"]))
        db = pts[i-1]["b"] - pts[i]["b"]
        if db != 0:
            # 用电为正(余额下降=消耗)，充值为负(余额回升)，都需记下以便标记
            days[d] = days.get(d, 0) + db
    bal_by_day = {}
    for x in pts:
        d = time.strftime("%m-%d", time.localtime(x["t"]))
        bal_by_day[d] = x["b"]
    labels = sorted(bal_by_day.keys())
    use = [days.get(d, 0) for d in labels]
    bal = [bal_by_day[d] for d in labels]
    x = np.arange(len(labels))

    # ---- KPI 指标 ----
    current_balance = bal[-1] if bal else 0
    recharge_total = sum(-u for u in use if u < 0)
    span_days = max(1.0, (pts[-1]["t"] - pts[0]["t"]) / 86400.0)
    net_used = (bal[0] - bal[-1]) + recharge_total if len(bal) > 1 else 0
    avg_usage = net_used / span_days
    total_usage = sum(u for u in use if u > 0)   # 近30天总计用电(不含充值)

    # 异常基线：窗口内“正用电日”耗电的中位数
    pos = [u for u in use if u > 0]
    baseline = float(np.median(pos)) if pos else 0.0

    # ---- 布局参数（手机竖向 vs 桌面横版） ----
    if mobile:
        figsize = (8.0, 12.0)
        DPI = 200
        FS_TITLE, FS_SUB = 19, 11
        FS_KPI_V, FS_KPI_L = 20, 12
        FS_AX, FS_ANNO, FS_LEG = 12, 12, 10
        LAY = dict(left=0.11, right=0.93, top=0.72, bottom=0.12,
                   hspace=0.22, hr=(1.9, 1.0))
        TITLE_Y, SUB_Y = 0.990, 0.958
    else:
        figsize = (11, 6.4)
        DPI = 140
        FS_TITLE, FS_SUB = 16, 9.5
        FS_KPI_V, FS_KPI_L = 16, 10
        FS_AX, FS_ANNO, FS_LEG = 10, 10, 9
        LAY = dict(left=0.075, right=0.965, top=0.78, bottom=0.12,
                   hspace=0.18, hr=(1.95, 1.05))
        TITLE_Y, SUB_Y = 0.975, 0.928

    fig = plt.figure(figsize=figsize, facecolor=BG, dpi=DPI)
    gs = GridSpec(2, 1, figure=fig, height_ratios=list(LAY["hr"]), hspace=LAY["hspace"],
                  left=LAY["left"], right=LAY["right"], top=LAY["top"], bottom=LAY["bottom"])
    ax_top = fig.add_subplot(gs[0])
    ax_bot = fig.add_subplot(gs[1], sharex=ax_top)
    for ax in (ax_top, ax_bot):
        ax.set_facecolor(BG)

    # ---- 标题 + 副标题（拉开间距避免重叠） ----
    fig.text(LAY["left"], TITLE_Y, f"宿舍用电趋势（近{ndays}天）", fontproperties=fp,
             fontsize=FS_TITLE, fontweight="bold", color="#1E293B",
             va="top", ha="left", transform=fig.transFigure)
    fig.text(LAY["left"], SUB_Y, f"数据更新至 {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
             fontproperties=fp, fontsize=FS_SUB, color=MUTE, va="top", ha="left",
             transform=fig.transFigure)

    # ---- KPI 卡片（手机：2×2；桌面：1×4） ----
    wlabel = f"近{ndays}天"
    kpis = [
        ("当前余额", f"¥{current_balance:.2f}", C_BAL_LINE),
        (f"{wlabel}总计用电", f"¥{total_usage:.2f}", "#475569"),
        (f"{wlabel}充值", f"¥{recharge_total:.2f}", C_RECHARGE),
        ("日均用电", f"¥{avg_usage:.2f}", C_USAGE),
    ]
    if mobile:
        cw, ch, gx, gy = 0.395, 0.082, 0.03, 0.022
        x0 = LAY["left"]
        y_top = 0.85
        y_bot = y_top - ch - gy
        positions = [(x0, y_top), (x0 + cw + gx, y_top),
                     (x0, y_bot), (x0 + cw + gx, y_bot)]
    else:
        cw, ch, gap = 0.211, 0.060, 0.015
        x0 = LAY["left"]
        y0 = 0.84
        positions = [(x0 + i * (cw + gap), y0) for i in range(4)]
    for (cx, cy), (name, val, col) in zip(positions, kpis):
        # 卡片柔和阴影
        fig.add_artist(FancyBboxPatch((cx + 0.004, cy - 0.004), cw, ch,
                          boxstyle="round,pad=0,rounding_size=0.012",
                          linewidth=0, facecolor="#CBD5E1", alpha=0.45,
                          transform=fig.transFigure, zorder=1))
        # 卡片主体（白底 + 极淡边框）
        fig.add_artist(FancyBboxPatch((cx, cy), cw, ch,
                          boxstyle="round,pad=0,rounding_size=0.012",
                          linewidth=0.8, edgecolor="#E8EDF3", facecolor="#FFFFFF",
                          transform=fig.transFigure, zorder=2))
        # 左侧强调色条
        fig.add_artist(FancyBboxPatch((cx, cy), 0.011, ch,
                          boxstyle="round,pad=0,rounding_size=0.005",
                          linewidth=0, facecolor=col,
                          transform=fig.transFigure, zorder=3))
        fig.text(cx + 0.030, cy + ch * 0.70, name, fontproperties=fp,
                 fontsize=FS_KPI_L, color=MUTE, va="center", ha="left",
                 transform=fig.transFigure)
        fig.text(cx + 0.030, cy + ch * 0.26, val, fontproperties=fp,
                 fontsize=FS_KPI_V, color=col, fontweight="bold", va="center",
                 ha="left", transform=fig.transFigure)

    # ---- 上图：余额折线 + 浅填充 + 预警线 ----
    ytop = (max(bal) * 1.15 if bal else 1)
    ytop = int(np.ceil(ytop / 20.0) * 20)
    ax_top.fill_between(x, bal, 0, color=C_BAL_FILL, alpha=0.18, zorder=1)
    ax_top.plot(x, bal, color=C_BAL_LINE, marker="o", markersize=6,
                linewidth=3.0, zorder=4)
    ax_top.set_ylim(0, ytop)
    ax_top.set_yticks(list(range(0, ytop + 1, 20)))
    ax_top.axhline(THRESHOLD, color=C_WARN, linestyle="--", linewidth=1.5, zorder=3)
    ax_top.text(len(labels) - 1, THRESHOLD + ytop * 0.012, f"预警线 ¥{THRESHOLD:.0f}",
                fontproperties=fp, color="#B45309", fontsize=FS_AX - 1, ha="right", va="bottom",
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#FEF3C7",
                          edgecolor="none", alpha=0.95))
    ax_top.set_ylabel("余额 (元)", fontproperties=fp, fontsize=FS_AX, color=TXT)
    ax_top.tick_params(axis="y", labelsize=FS_AX, colors=TXT)
    ax_top.tick_params(axis="x", which="both", bottom=False, top=False, length=0, labelsize=0)
    ax_top.set_xticks([])
    ax_top.grid(axis="y", color=GRID, linestyle=":", linewidth=0.8, alpha=0.22, zorder=0)
    ax_top.set_axisbelow(True)
    ax_top.spines["top"].set_visible(False)
    ax_top.spines["right"].set_visible(False)
    ax_top.spines["left"].set_color(AXIS)
    ax_top.spines["bottom"].set_color(AXIS)

    # ---- 下图：每日变化圆角柱 ----
    # 问题：充值金额常达 100~200 元，而日均用电仅约 1 元。若按真实坐标绘制，
    # 充值绿柱会把纵轴拉到 200+，日用电蓝柱被压成"一根线"几乎看不见。
    # 解决：充值柱高度不按真实金额，而是按"日用电标尺"封顶(RECHARGE_CAP_MULT 倍)，
    # 既能一眼看出充值日，又不破坏日用电的可读性；真实金额用 ▲+金额 标注。
    RECHARGE_CAP_MULT = 2.0
    usage_vals = [u for u in use if u > 0]
    usage_max = max(usage_vals) if usage_vals else 0.0
    recharge_cap_h = (usage_max if usage_max > 0 else 1.0) * RECHARGE_CAP_MULT
    bar_w = 0.7
    colors = []
    for i, u in enumerate(use):
        if abs(u) <= 0:
            colors.append(None)
            continue
        if u < 0:
            color = C_RECHARGE
            h = min(abs(u), recharge_cap_h)   # 充值柱封顶，不按真实坐标
        # 异常：相对基线倍数判定，不设绝对下限——真实单日耗电费常仅几分钱，
        # 若按邮件的 5 元下限，红柱(异常)将永不出现
        elif baseline > 0 and u >= ANOMALY_MULTIPLE * baseline:
            color = C_ALERT
            h = abs(u)
        else:
            color = C_USAGE
            h = abs(u)
        colors.append(color)
        radius = min(bar_w / 2, h / 2, 0.18)
        ax_bot.add_patch(FancyBboxPatch((i - bar_w / 2, 0), bar_w, h,
                            boxstyle=f"round,pad=0,rounding_size={radius}",
                            linewidth=0, facecolor=color, alpha=0.92, zorder=3))
    top_use = max(usage_max, recharge_cap_h, 1.0)
    ax_bot.set_xlim(-0.6, len(labels) - 0.4)
    ax_bot.set_ylim(0, top_use * 1.25)
    for i, u in enumerate(use):
        if abs(u) <= 0:
            continue
        if u < 0:
            h = min(abs(u), recharge_cap_h)
            ax_bot.text(i, h + top_use * 0.03, f"▲ +{-u:.1f}",
                        ha="center", va="bottom", fontproperties=fp,
                        color="#065F46", fontsize=FS_ANNO + 1, fontweight="bold",
                        bbox=dict(boxstyle="round,pad=0.3", facecolor="#D1FAE5",
                                  edgecolor="none", alpha=0.95))
        elif colors[i] == C_ALERT:
            ax_bot.text(i, abs(u) + top_use * 0.02, "异常", ha="center", va="bottom",
                        fontproperties=fp, color=C_ALERT, fontsize=FS_ANNO, fontweight="bold")

    ax_bot.set_ylabel("每日金额变化 (元)", fontproperties=fp, fontsize=FS_AX, color=TXT)
    ax_bot.tick_params(axis="y", labelsize=FS_AX, colors=TXT)
    ax_bot.tick_params(axis="x", rotation=0, labelsize=FS_AX, colors=TXT)
    # x 轴仅保留约 7 个日期标签，避免手机端拥挤
    n = len(labels)
    step = max(1, n // 7)
    show_idx = list(range(0, n, step))
    if (n - 1) not in show_idx:
        show_idx.append(n - 1)
    ax_bot.set_xticks(x[show_idx])
    ax_bot.set_xticklabels([labels[i] for i in show_idx])
    for lbl in ax_bot.get_xticklabels():
        lbl.set_fontproperties(fp)
    ax_bot.grid(axis="y", color=GRID, linestyle=":", linewidth=0.8, alpha=0.22, zorder=0)
    ax_bot.set_axisbelow(True)
    ax_bot.spines["top"].set_visible(False)
    ax_bot.spines["right"].set_visible(False)
    ax_bot.spines["left"].set_color(AXIS)
    ax_bot.spines["bottom"].set_color(AXIS)

    # ---- 统一图例（移到底部，横向紧凑，无边框） ----
    handles = [
        Line2D([0], [0], color=C_BAL_LINE, marker="o", label="余额(元)"),
        Patch(facecolor=C_USAGE, label="日用电(正常)"),
        Patch(facecolor=C_ALERT, label="日用电(异常)"),
        Patch(facecolor=C_RECHARGE, label="充值(高度封顶)"),
    ]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.028),
               prop=fp, fontsize=FS_LEG, frameon=False, ncol=4,
               handlelength=1.4, handleheight=1.4, columnspacing=1.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    png = buf.getvalue()
    with open(CHART_PNG, "wb") as f:
        f.write(png)
    return png

# ===================== 7. 网页报告 =====================
def gen_report(history, chart_png):
    b64 = base64.b64encode(chart_png).decode()
    now = int(time.time())
    last = history[-1]["b"] if history else 0
    recent = [x for x in history if x["t"] >= now - 30*86400]
    qr_tag = ""
    if os.path.exists(QR_PNG):
        with open(QR_PNG, "rb") as f:
            qr_b64 = base64.b64encode(f.read()).decode()
        qr_tag = f'<img src="data:image/png;base64,{qr_b64}" alt="二维码" style="max-width:200px;margin-top:10px"><div class="muted">手机扫码随时看 / 或访问网页链接</div>'
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>宿舍用电报告</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fb;color:#222}}
.card{{max-width:680px;margin:16px auto;background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
h1{{font-size:20px}} .big{{font-size:30px;color:#2b6cff;font-weight:700}}
img{{width:100%;border-radius:10px;margin-top:10px}}
.muted{{color:#888;font-size:13px}}
</style></head><body>
<div class="card"><h1>宿舍用电报告</h1>
<div class="big">当前余额 ¥{last:.2f}</div>
<div class="muted">最近30天记录 {len(recent)} 条 · 更新于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}</div>
<img src="data:image/png;base64,{b64}" alt="趋势图">
{qr_tag}
<div class="muted">数据存档于本地 dorm_balance.json（保留最近100天）。低余额会自动邮件预警。</div>
</div></body></html>"""
    os.makedirs(DEPLOY_DIR, exist_ok=True)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

# ===================== 8. 二维码 =====================
def gen_qr(url):
    if not url:
        print("SHARE_URL 为空，跳过二维码（部署后填入再跑一次即可生成）。")
        return
    import qrcode
    img = qrcode.make(url)
    img.save(QR_PNG)
    print(f"二维码已生成: {QR_PNG} -> {url}")

# ===================== 9. 推送（Agent Mail CLI） =====================
import subprocess

def _agently(args):
    """调用 agently-cli，返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run([AGENTLY_BIN] + args, capture_output=True, text=True,
                           timeout=60, cwd=os.path.dirname(os.path.abspath(__file__)))
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"找不到命令 {AGENTLY_BIN}（请在配置里填完整路径）"
    except Exception as e:
        return 1, "", str(e)

def _extract_token(out):
    """从 CLI 输出的 JSON envelope 中提取 confirmation_token。"""
    try:
        env = json.loads(out)
        return env.get("data", {}).get("confirmation_token") or ""
    except Exception:
        return ""

def _send_mail(subject, body_file, attachments):
    """两段式发送：先取 confirmation_token，再带 token 发一次（自动确认，用于定时脚本）。"""
    if not RECIPIENT:
        print("未配置 RECIPIENT，跳过邮件发送。")
        return False
    # CLI 要求 body-file / attachment 为相对 cwd 的路径
    body_rel = os.path.basename(body_file)
    atts_rel = [os.path.basename(a) for a in (attachments or [])]
    base = ["message", "+send", "--to", RECIPIENT,
            "--subject", subject, "--body-file", body_rel]
    for a in atts_rel:
        base += ["--attachment", a]
    rc, out, err = _agently(base)
    if rc != 0:
        print("邮件[阶段1]失败:", (out or err).strip()[:300])
        return False
    token = _extract_token(out)
    if not token:
        # 部分情况下首调即发出（无需确认）
        print("邮件已发送（无需确认）。")
        return True
    rc2, out2, err2 = _agently(base + ["--confirmation-token", token])
    if rc2 != 0:
        print("邮件[阶段2]失败:", (out2 or err2).strip()[:300])
        return False
    print("邮件已发送（含趋势图附件）。")
    return True

def push_email_text(subject, body):
    """纯文本预警邮件（无附件）。"""
    fd, path = _body_tmp(body)
    try:
        _send_mail(subject, path, [])
    finally:
        _rm_tmp(path)

def push_email_img(subject, html, chart_bytes, qr_bytes=None):
    """带趋势图附件的邮件；chart/qr 会先落盘（CLI 要求相对路径附件），发完清理。"""
    import tempfile
    d = os.path.dirname(os.path.abspath(__file__))
    chart_path = os.path.join(d, "chart_send.png")
    with open(chart_path, "wb") as f:
        f.write(chart_bytes)
    atts = [chart_path]
    if qr_bytes:
        qr_path = os.path.join(d, "qr_send.png")
        with open(qr_path, "wb") as f:
            f.write(qr_bytes)
        atts.append(qr_path)
    fd, html_path = _body_tmp(html)
    try:
        _send_mail(subject, html_path, atts)
    finally:
        _rm_tmp(html_path)
        for p in atts:
            try:
                os.remove(p)
            except Exception:
                pass

def _body_tmp(content):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".html", dir=os.path.dirname(os.path.abspath(__file__)))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return fd, path

def _rm_tmp(path):
    try:
        os.remove(path)
    except Exception:
        pass

def _read_qr():
    """读取二维码文件为字节（不存在返回 None）。"""
    if os.path.exists(QR_PNG):
        with open(QR_PNG, "rb") as f:
            return f.read()
    return None

# ===================== 10. 主流程 =====================
def main():
    if not JWT_COOKIE:
        raise SystemExit(
            "❌ 未配置 XJTU_CEMS_JWT：请设置环境变量，或将 .env.example 复制为 .env 后填写真实 JWT。")
    if not RECIPIENT:
        raise SystemExit(
            "❌ 未配置 XJTU_RECIPIENT：请在 .env 中填写接收通知的邮箱（XJTU_RECIPIENT=you@example.com）。")
    bal = fetch_balance()
    h = load_history()
    now = int(time.time())
    h["history"], last24, avg_day = compute(h["history"], bal)
    save_history(h)
    kwh = f" | 约 {last24/PRICE_PER_KWH:.2f} 度" if PRICE_PER_KWH > 0 else ""
    print(f"当前余额 ¥{bal:.2f} | 近24h ¥{last24:.2f}{kwh} | 日均 ¥{avg_day:.2f}")

    # 低余额预警（每次）
    if bal <= THRESHOLD:
        title = f"⚠️ 宿舍电费仅剩 ¥{bal:.2f}"
        body = (f"低于预警线 {THRESHOLD:.0f} 元，该充值了！\n"
                f"近24h用电 ¥{last24:.2f}，日均约 ¥{avg_day:.2f}。")
        push_email_text(title, body)

    # 异常用电检测（积累足够数据后）
    is_anom, info = detect_anomaly(h["history"])
    if is_anom and (now - h.get("last_anomaly", 0) >= ANOMALY_COOLDOWN_DAYS * 86400):
        title = "⚠️ 宿舍用电异常提醒"
        body = (f"检测到近期用电量明显偏高：\n"
                f"本次消耗 ¥{info['last_usage']:.2f}（约 ¥{info['last_rate']:.2f}/天），\n"
                f"平时约 ¥{info['baseline']:.2f}/天，已达基线 {info['last_rate']/info['baseline']:.1f} 倍。\n"
                f"可能是忘记关电器 / 电器故障 / 漏电，建议留意。")
        push_email_text(title, body)
        h["last_anomaly"] = now
        save_history(h)
        print("检测到异常用电，已发送提醒邮件")

    # 周报 / 月报：按周期生成对应版本的趋势图作为邮件附件
    history = h["history"]
    data_span = (now - history[0]["t"]) if len(history) > 1 else 0

    # 周报：每 7 天，发送「近一周·手机版」趋势图（须先积累满 7 天数据，首周不发）
    weekly_due = (data_span >= REPORT_CYCLE_DAYS * 86400
                  and now - h.get("last_report", 0) >= REPORT_CYCLE_DAYS * 86400)
    if weekly_due:
        wk_chart = gen_chart(history, mobile=True, ndays=REPORT_CYCLE_DAYS)
        text = (f"当前余额 ¥{bal:.2f}，近24h用电 ¥{last24:.2f}，日均约 ¥{avg_day:.2f}。"
                + (f" 约 {last24/PRICE_PER_KWH:.2f} 度。" if PRICE_PER_KWH > 0 else ""))
        push_email_img(f"宿舍用电周报（近{REPORT_CYCLE_DAYS}天）", f"<p>{text}</p>",
                       wk_chart, _read_qr())
        h["last_report"] = now
        save_history(h)
        print("已推送周报（近一周·手机版趋势图）")
    else:
        print(f"数据积累中（已满 {data_span/86400:.1f} 天 / 周报需 {REPORT_CYCLE_DAYS} 天），暂不发送周报")

    # 月报：每 30 天，发送「近30天·桌面横版」趋势图（须先积累满 30 天数据）
    monthly_due = (data_span >= MONTHLY_CYCLE_DAYS * 86400
                   and now - h.get("last_monthly", 0) >= MONTHLY_CYCLE_DAYS * 86400)
    if monthly_due:
        mo_chart = gen_chart(history, mobile=False, ndays=MONTHLY_CYCLE_DAYS)
        text = (f"当前余额 ¥{bal:.2f}，近30天日均约 ¥{avg_day:.2f}。"
                + (f" 近24h约 {last24/PRICE_PER_KWH:.2f} 度。" if PRICE_PER_KWH > 0 else ""))
        push_email_img(f"宿舍用电月报（近{MONTHLY_CYCLE_DAYS}天）", f"<p>{text}</p>",
                       mo_chart, _read_qr())
        h["last_monthly"] = now
        save_history(h)
        print("已推送月报（近30天·桌面横版趋势图）")

    # 本地默认图表（手机版·近30天）+ 网页报告 + 二维码，放最后刷新，确保 chart.png 始终是默认版
    chart_png = gen_chart(h["history"])
    gen_report(h["history"], chart_png)
    gen_qr(SHARE_URL)

if __name__ == "__main__":
    main()
