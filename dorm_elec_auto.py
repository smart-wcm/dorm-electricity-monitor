#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["requests", "matplotlib", "numpy", "pystray"]
# ///
# -*- coding: utf-8 -*-
"""
宿舍电费自动监控 · 增强版（已接入西交「cems」接口）
========================================================
每 6h 取一次余额 -> 存档(保留最近100天) -> 反推用电量 -> 低于阈值发邮件预警；
每 7 天发送「近一周·手机版」趋势图周报(柱状+余额折线) 到你的邮箱；
每 30 天发送「近30天·桌面横版」趋势图月报 到你的邮箱；
生成自包含网页报告(report.html, 近30天)，本机浏览器打开实时查看。
通知渠道：Python 内置 smtplib 直连邮箱 SMTP 服务器发信（无需 Node.js / agently-cli），
          配置发件邮箱地址 + SMTP 授权码即可；可打包为 exe 双击即用。

接口（校园网内可直接访问，无需登录凭证）：
  GET https://ssn.xjtu.edu.cn/cems/mobile/meterAccount/electricity?roomId=<你的房间号>
  响应: {"code":0,"data":36.58,"msg":"操作成功"}   # data 即余额(元)

部署重要说明：
  ssn.xjtu.edu.cn 是校内系统，本脚本必须运行在【校园网环境】
  （你电脑连校园网时）。推荐打包为 exe 双击常驻（或源码模式双击 run_dorm.vbs），
  无窗口后台每 6h 一轮。

数据存档：dorm_balance.json 仅保留最近 RETENTION_DAYS(默认100) 天，更早自动丢弃
（文件在电脑本地，建议定期备份/导出）。
========================================================
"""

import requests
import json
import time
import os
import io
import logging
import logging.handlers
import argparse
import atexit
import sys
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ===================== 1. 配置区 =====================
def app_dir():
    """返回程序『数据根目录』——所有可写文件（.env/存档/日志/deploy）的落脚点。

    - PyInstaller 打包后（sys.frozen）：返回 exe 所在目录，保证数据与 exe 同目录、
      便携可迁移；此时 __file__ 指向临时解压目录 _MEIxxxxx，绝不能用作数据落脚点。
    - 源码运行：返回脚本所在目录（与原行为一致）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# APP_DIR 在模块加载时一次性求值，供本文件内所有路径定位使用。
APP_DIR = app_dir()


def _load_dotenv(path=None):
    """极简 .env 解析（避免额外依赖 python-dotenv）。

    默认读取『与 exe/脚本同目录』的 .env，因此无论以何种方式启动（双击/命令行/自启）都能找到，
    无需再额外配置系统环境变量。
    """
    if path is None:
        path = os.path.join(APP_DIR, ".env")
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                # 正确处理引号包裹的值：仅剥离首尾成对的同一种引号
                if (v.startswith('"') and v.endswith('"') and len(v) >= 2) or \
                   (v.startswith("'") and v.endswith("'") and len(v) >= 2):
                    v = v[1:-1]
                env[k.strip()] = v
    except FileNotFoundError:
        pass
    return env


_LOCAL_ENV = _load_dotenv()


def _env_num(key, default, cast):
    """从 .env/环境变量读取数值型配置；缺失或非法时回退默认值。

    用于所有数值配置的解析：.env 手改坏了（如 SMTP_PORT=abc）也只会回退默认值，
    不会在导入期崩溃（--windowed 模式下无控制台，导入期崩溃毫无提示）。
    """
    raw = _LOCAL_ENV.get(key) or os.environ.get(key) or ""
    try:
        return cast(raw) if raw not in ("", None) else default
    except (ValueError, TypeError):
        return default


ROOM_ID = _LOCAL_ENV.get("XJTU_ROOM_ID") or os.environ.get("XJTU_ROOM_ID") or ""
API_URL = f"https://ssn.xjtu.edu.cn/cems/mobile/meterAccount/electricity?roomId={ROOM_ID}"

# 鉴权说明：该 cems 接口在校园网环境下仅凭 roomId 即可返回余额，无需任何登录凭证（JWT）。
# 只要本机处于西安交大校园网（或能访问 ssn.xjtu.edu.cn 的内网）即可，无需抓包获取 Cookie。
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile",
    "Accept": "application/json, text/plain, */*",
}

# ===================== 2. 通知配置（SMTP 邮件） =====================
# 通道：Python 内置 smtplib 直连邮箱 SMTP 服务器发信（无需 Node.js / agently-cli），
# 因此可被打包进 exe，双击即用。发件账号与授权码在 .env 中配置。
# 收件人 = 你接收通知的邮箱地址。
RECIPIENT = (_LOCAL_ENV.get("XJTU_RECIPIENT")
             or os.environ.get("XJTU_RECIPIENT")
             or "")
# SMTP 配置（必填四件套：主机/端口/账号/授权码）。
# 常见服务商预设：
#   QQ 邮箱:     SMTP_HOST=smtp.qq.com   SMTP_PORT=465  SMTP_TLS=ssl
#   163 邮箱:    SMTP_HOST=smtp.163.com  SMTP_PORT=465  SMTP_TLS=ssl
#   Gmail:       SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_TLS=starttls
#   Outlook:     SMTP_HOST=smtp.office365.com SMTP_PORT=587 SMTP_TLS=starttls
# 注意：授权码（SMTP_PASS）不是登录密码，需到对应邮箱网页开启 SMTP 服务后获取。
SMTP_HOST = (_LOCAL_ENV.get("SMTP_HOST")
             or os.environ.get("SMTP_HOST")
             or "")
SMTP_PORT = _env_num("SMTP_PORT", 465, int)
SMTP_USER = (_LOCAL_ENV.get("SMTP_USER")
             or os.environ.get("SMTP_USER")
             or "")
SMTP_PASS = (_LOCAL_ENV.get("SMTP_PASS")
             or os.environ.get("SMTP_PASS")
             or "")
# 发件人地址；多数服务商要求等于 SMTP_USER，留空则回退到 SMTP_USER。
SMTP_FROM = (_LOCAL_ENV.get("SMTP_FROM")
             or os.environ.get("SMTP_FROM")
             or SMTP_USER)
# 加密方式：ssl（隐式 SSL，端口 465）/ starttls（STARTTLS，端口 587）/ none（不加密）
SMTP_TLS = (_LOCAL_ENV.get("SMTP_TLS")
            or os.environ.get("SMTP_TLS")
            or "ssl").lower()


# 以下参数均可通过 .env 覆盖（key=值，如 THRESHOLD=5.0）；不填则用内置默认。
# 配置向导「高级参数」页可图形化设置，也可直接改 .env。
THRESHOLD = _env_num("THRESHOLD", 10.0, float)          # 低余额预警线（元）
PRICE_PER_KWH = _env_num("PRICE_PER_KWH", 0.0, float)   # 电价(元/度)，填了会把金额折算成"度"；不知道就留 0
REPORT_CYCLE_DAYS = _env_num("REPORT_CYCLE_DAYS", 7, int)     # 每隔几天生成并推送一次「近一周」周报
MONTHLY_CYCLE_DAYS = _env_num("MONTHLY_CYCLE_DAYS", 30, int)  # 每隔几天生成并推送一次「近30天」月报
RETENTION_DAYS = _env_num("RETENTION_DAYS", 100, int)   # 历史存档保留天数（仅保留最近 N 天，更早自动丢弃）

# 异常用电检测（需先积累足够数据）
ANOMALY_MIN_SAMPLES = _env_num("ANOMALY_MIN_SAMPLES", 20, int)   # 至少积累多少条记录才启用异常检测
ANOMALY_MULTIPLE = _env_num("ANOMALY_MULTIPLE", 3.0, float)      # 最新段日耗电 >= 基线(中位数)的 N 倍 视为异常
ANOMALY_MIN_USAGE = _env_num("ANOMALY_MIN_USAGE", 5.0, float)    # 单次消耗低于此值(元)不报异常，避免小波动误报
ANOMALY_COOLDOWN_DAYS = _env_num("ANOMALY_COOLDOWN_DAYS", 1, int)  # 异常邮件最短间隔(天)，避免连续轰炸
RECHARGE_CAP_MULT = _env_num("RECHARGE_CAP_MULT", 2.0, float)    # 图表中充值柱高度封顶倍数

FAIL_ALERT_HOURS = 24      # 抓取连续失败超此时长(小时)才发「持续异常」邮件；瞬时失败仅记日志，避免误报

# ===================== 3. 文件/产物 =====================
STORE = os.path.join(APP_DIR, "dorm_balance.json")  # 历史永久存档（绝对路径，避免依赖启动 cwd 导致数据写到别处）
DEPLOY_DIR = os.path.join(APP_DIR, "deploy")  # 部署目录（绝对路径，供网页服务读取）
CHART_PNG = os.path.join(DEPLOY_DIR, "chart.png")   # 趋势图（同时供网页实时读取）
REPORT_HTML = os.path.join(DEPLOY_DIR, "index.html")  # 网页报告(部署用)
LOG_FILE = os.path.join(APP_DIR, "dorm_monitor.log")  # 运行日志（绝对路径，避免依赖启动 cwd）
# 日志器（handler 在 main() 里按运行环境挂载，避免导入即写文件）
log = logging.getLogger("dorm_monitor")

# 托盘读取的运行状态；只存内存，不写入存档。
_STATUS_LOCK = threading.Lock()
_STATUS = {
    "state": "starting",       # starting / running / success / error
    "balance": None,
    "last_run": 0,
    "last_success": 0,
    "last24": None,
    "avg_day": None,
    "history_count": 0,
    "last_error": "",
    "next_run": 0,
}


def _update_status(**changes):
    with _STATUS_LOCK:
        _STATUS.update(changes)


def get_status():
    """返回托盘使用的状态快照。"""
    with _STATUS_LOCK:
        return dict(_STATUS)


# ===================== 3b. 实时网页服务（本机） =====================
# 网页报告默认指向【本机实时服务】地址：本机浏览器打开即看最新图，
# 无需公网、无需每次重新部署（服务由本进程常驻提供，见 README「实时网页」）。
# 端口可用 XJTU_SERVE_PORT 覆盖（默认 8765）。
SERVE_PORT = _env_num("XJTU_SERVE_PORT", 8765, int)
# 绑定地址：默认仅本机可访问（127.0.0.1，最安全）。如需局域网内其它设备访问，
# 可设 XJTU_SERVE_HOST=0.0.0.0（注意：同网段设备均可看到你的余额/用电页面，无鉴权）。
SERVE_HOST = (_LOCAL_ENV.get("XJTU_SERVE_HOST")
              or os.environ.get("XJTU_SERVE_HOST")
              or "127.0.0.1")
# 抓取循环间隔(小时)；可用 XJTU_INTERVAL_HOURS 覆盖。服务模式每间隔抓一次，启动即先抓一次。
# 下限 0.5 小时：防止误配成 0/极小值导致死循环狂刷校内接口。
INTERVAL_HOURS = max(0.5, _env_num("XJTU_INTERVAL_HOURS", 6.0, float))

def _local_lan_ip():
    """获取本机局域网 IPv4（本机浏览器访问服务用）。失败回退 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

# 本机实时服务地址；可用 XJTU_LOCAL_URL 显式覆盖（如做了端口映射/固定 IP）。
# 默认绑定 127.0.0.1（仅本机可访问）时指向回环地址；绑 0.0.0.0 时指向局域网 IP。
LOCAL_URL = (_LOCAL_ENV.get("XJTU_LOCAL_URL")
             or os.environ.get("XJTU_LOCAL_URL")
             or f"http://{'127.0.0.1' if SERVE_HOST in ('127.0.0.1', 'localhost') else _local_lan_ip()}:{SERVE_PORT}/")

# ===================== 3c. 实时网页服务（内置于本进程） =====================
class _LiveHandler(SimpleHTTPRequestHandler):
    # Python 3.13 起 directory 不再是类属性，改为 __init__ 参数；
    # 若不显式传 directory=，父类默认用 os.getcwd()，导致 HTTP 服务
    # 找不到 deploy/ 目录而返回 404。因此必须覆盖 __init__。
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DEPLOY_DIR, **kwargs)

    def end_headers(self):
        # 禁缓存：确保手机每次刷新都拉到最新 chart.png
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()
    def log_message(self, *args):
        pass  # 静默

def start_server_thread():
    """在守护线程里起网页服务，实时把 deploy/ 提供给本机浏览器。

    默认只绑定 127.0.0.1（仅本机可访问，不对外暴露）；
    如确有局域网访问需求，可在 .env 设 XJTU_SERVE_HOST=0.0.0.0（无鉴权，请自行权衡）。
    """
    try:
        httpd = ThreadingHTTPServer((SERVE_HOST, SERVE_PORT), _LiveHandler)
    except OSError as e:
        log.warning("网页服务启动失败(端口 %s 被占?): %s；抓取照常。", SERVE_PORT, e)
        return
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    log.info("网页服务已启动: %s（浏览器打开即看，每60秒自动刷新）", LOCAL_URL)
    try:
        os.makedirs(DEPLOY_DIR, exist_ok=True)
        with open(os.path.join(DEPLOY_DIR, "serve_url.txt"), "w", encoding="utf-8") as f:
            f.write(LOCAL_URL + "\n")
    except Exception:
        pass

# ===================== 4. 取余额 =====================
class AuthExpired(Exception):
    """接口拒绝访问：可能不在校园网 / 接口地址或参数已变更。"""
    pass


def _fmt_ts(ts):
    """时间戳 -> 本地可读时间字符串。"""
    if not ts:
        return "未知时间"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def setup_logging():
    """挂载日志 handler：文件(dorm_monitor.log, 滚动) + 控制台。重复调用安全。"""
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                             "%Y-%m-%d %H:%M:%S")
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=200000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    # pythonw 运行时 sys.stderr 为 None，挂控制台 handler 会在首条日志即崩溃，
    # 故仅在确实存在控制台时才挂 StreamHandler。
    if sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        log.addHandler(ch)


def fetch_balance(retries=3, backoff=2.0):
    """抓取余额，带重试与指数退避。

    接口拒绝（401/403）立即抛 AuthExpired（重试无意义）；
    网络/HTTP/JSON 等瞬时错误最多重试 retries 次（退避 2/4/8...秒）。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # 显式声明不使用任何代理：proxies={"http": None, "https": None}。
            # 注意 requests 中 proxies={} 并不等于禁用代理——空 dict 不会为
            # http/https 指定值，requests 仍会回退到 Windows 系统代理/环境变量，
            # 于是被 ProxyPin 等抓包/代理软件设置的本地代理拦住（WinError 10061）。
            r = requests.get(API_URL, headers=HEADERS, timeout=15,
                             proxies={"http": None, "https": None})
        except requests.RequestException as e:
            last_err = RuntimeError(f"网络请求异常: {e}")
            log.warning("抓取第%d/%d 次失败(网络): %s", attempt, retries, e)
        else:
            if r.status_code in (401, 403):
                raise AuthExpired(f"HTTP {r.status_code}")
            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                last_err = e
                log.warning("抓取第%d/%d 次失败(HTTP %d)", attempt, retries, r.status_code)
            else:
                try:
                    data = r.json()
                except ValueError:
                    last_err = ValueError(f"接口返回非 JSON: {r.text[:200]}")
                    log.warning("抓取第%d/%d 次失败(非 JSON)", attempt, retries)
                else:
                    code = data.get("code")
                    if code != 0:
                        # 接口拒绝访问（未登录/无权限/参数错误等）判定为访问被拒
                        if code in (401, 403, -1):
                            raise AuthExpired(f"接口拒绝访问(code={code}, msg={data.get('msg')})")
                        last_err = ValueError(f"接口返回异常: {data}")
                        log.warning("抓取第%d/%d 次失败(code=%s)", attempt, retries, code)
                    else:
                        return float(data["data"])
        if attempt < retries:
            time.sleep(backoff * (2 ** (attempt - 1)))
    raise last_err if last_err else RuntimeError("未知抓取错误")

# ===================== 5. 记录 & 计算（永久存储） =====================
def load_history():
    if os.path.exists(STORE):
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    return {"history": [], "last_report": 0}

def save_history(h):
    """原子写：先落临时文件，再 os.replace 覆盖。

    os.replace 在同一目录内是原子操作，可避免计划任务重叠或写入中途被
    中断时生成半截 JSON 导致 dorm_balance.json 损坏（旧实现直接覆盖写，
    多实例并发时有此风险）。临时文件与 STORE 同目录，确保跨文件系统 rename 也成立。
    """
    import tempfile
    d = os.path.dirname(os.path.abspath(STORE))  # = APP_DIR
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".balance_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE)  # 原子替换
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid):
    """判断 PID 是否仍存活（不依赖锁文件修改时间）。

    Windows 用 kernel32.OpenProcess 探测；其它平台退化为 os.kill(pid, 0)。
    """
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_INFORMATION(0x0400) | PROCESS_VM_READ(0x0010)
            handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_run_lock():
    """进程互斥锁：避免多实例并发读写 / 重复发信。

    与 STORE 同目录原子创建 `.dorm_elec.lock`（O_CREAT|O_EXCL 跨平台原子）。
    - 锁已存在且其中记录的 PID 仍存活 -> 视为有其它实例在跑，本次跳过；
    - 锁存在但 PID 已死（含被任务管理器/任务计划强杀、未来得及清锁）-> 残留，删掉强占；
    - 锁内写入当前 PID 便于排查。
    返回 (fd, path)；进程退出时由 atexit 调用 _release_lock 释放（本脚本用 atexit 兜底）。
    """
    lock = os.path.join(APP_DIR, ".dorm_elec.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        old_pid = None
        try:
            with open(lock, "r") as f:
                old_pid = int(f.read().strip())
        except (OSError, ValueError):
            old_pid = None
        if old_pid is not None and _pid_alive(old_pid):
            raise SystemExit(
                f"⚠️ 已有实例运行中（PID {old_pid}，锁文件 {lock}），本次跳过以避免并发读写。")
        # PID 已死或无法解析 -> 残留锁，强占
        try:
            os.remove(lock)
        except OSError:
            pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            # 极小竞态：删除后、重建前，另一实例抢先重建了锁
            raise SystemExit(
                f"⚠️ 锁文件 {lock} 被其它实例抢先重建，本次跳过以避免并发读写。")
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd, lock


def _release_lock(fd, path):
    """释放互斥锁（忽略已关闭 / 不存在等异常）。"""
    if not fd:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


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
    r"""异常用电检测：积累足够数据后，若最新一段日耗电 >= 基线的 N 倍则报警。
    返回 (is_anomaly, info)。基线 = 除最新段外各耗电段日耗电率(元/天)的中位数。
    仅取近 30 天数据参与基线计算，避免旧数据（如假期零耗电）污染基线。"""
    if len(history) < ANOMALY_MIN_SAMPLES:
        return False, {}
    # 仅取近 30 天的记录，避免旧数据污染基线
    window_start = int(time.time()) - 30 * 86400
    recent = [x for x in history if x["t"] >= window_start]
    if len(recent) < 5:
        return False, {}
    pairs = []   # 仅计入"耗电段"(余额下降)
    for i in range(1, len(recent)):
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
    os.makedirs(os.path.dirname(CHART_PNG), exist_ok=True)
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

    # 异常基线：窗口内"正用电日"耗电的中位数
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
    now = int(time.time())
    last = history[-1]["b"] if history else 0
    recent = [x for x in history if x["t"] >= now - 30*86400]
    os.makedirs(DEPLOY_DIR, exist_ok=True)
    # 图表作为独立文件 chart.png 落盘（serve.py / 静态托管实时读取），
    # 页面用 ?v=时间戳 做缓存破坏，确保刷新即最新。
    with open(CHART_PNG, "wb") as f:
        f.write(chart_png)
    cache_bust = now
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>宿舍用电报告</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fb;color:#222}}
.card{{max-width:680px;margin:16px auto;background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
h1{{font-size:20px}} .big{{font-size:30px;color:#2b6cff;font-weight:700}}
img{{width:100%;border-radius:10px;margin-top:10px}}
.muted{{color:#888;font-size:13px}} a{{color:#2b6cff}}
</style></head><body>
<div class="card"><h1>宿舍用电报告</h1>
<div class="big">当前余额 ¥{last:.2f}</div>
<div class="muted">最近30天记录 {len(recent)} 条 · 更新于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}（每60秒自动刷新）</div>
<img src="chart.png?v={cache_bust}" alt="趋势图">
<div class="muted">数据存档于本地 dorm_balance.json（保留最近100天）。低余额会自动邮件预警。</div>
</div></body></html>"""
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

# ===================== 9. 推送（SMTP 邮件） =====================
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def _smtp_connect():
    """按 SMTP_TLS 配置建立已认证连接并返回 SMTP 对象。

    - ssl:      隐式 SSL（smtplib.SMTP_SSL，端口 465）
    - starttls: 先明文连接再升级（smtplib.SMTP + starttls，端口 587）
    - none:     不加密（仅用于本地测试服务器）
    """
    if SMTP_TLS == "starttls":
        srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        srv.ehlo()
        srv.starttls()
        srv.ehlo()
    elif SMTP_TLS == "none":
        srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    else:  # 默认 ssl
        srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    srv.login(SMTP_USER, SMTP_PASS)
    return srv


def _new_msg(subject):
    """构造邮件骨架（发件人/收件人/主题/日期/Message-ID）。"""
    msg = EmailMessage()
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(SMTP_HOST or "localhost"))
    return msg


def _send_msg(msg):
    """实际发送：建立连接→send_message→退出。返回是否成功。"""
    if not RECIPIENT:
        log.warning("未配置 RECIPIENT，跳过邮件发送。")
        return False
    try:
        srv = _smtp_connect()
    except Exception as e:
        log.warning("SMTP 连接失败: %s", e)
        return False
    try:
        srv.send_message(msg)
    except Exception as e:
        log.warning("邮件发送失败: %s", e)
        return False
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    log.info("邮件已发送: %s", msg.get("Subject", "?"))
    return True


def push_email_text(subject, body):
    """纯文本预警邮件（无附件）。返回是否发送成功。"""
    msg = _new_msg(subject)
    msg.set_content(body, charset="utf-8")
    return _send_msg(msg)


def push_email_img(subject, html, chart_bytes):
    """带趋势图内联图片的邮件。

    chart 以 cid: 引用内嵌进 HTML 正文（而非外部附件），客户端直接显示，
    无需用户点开附件。
    """
    msg = _new_msg(subject)
    # 纯文本 fallback + HTML 正文
    msg.set_content("本邮件为 HTML 格式，请用支持 HTML 的客户端查看。", charset="utf-8")
    # 在 HTML 正文末尾追加对内嵌图片的 cid 引用，确保图片真正显示
    html_full = html
    if chart_bytes:
        html_full += '<br><img src="cid:chart" alt="趋势图" style="max-width:100%;">'
    msg.add_alternative(html_full, subtype="html", charset="utf-8")
    # 关联内嵌图片（add_related 须在 add_alternative 之后，挂到 HTML 部分）
    html_part = msg.get_payload()[1]
    if chart_bytes:
        html_part.add_related(chart_bytes, "image", "png",
                              cid="<chart>", filename="chart.png")
    return _send_msg(msg)


# ===================== 10. 主流程 =====================
def _config_complete():
    """判断 .env 中关键字段是否齐全（用于决定是否弹首次配置向导）。"""
    return bool(ROOM_ID and RECIPIENT and SMTP_HOST and SMTP_USER and SMTP_PASS)


def _reload_config_from_env():
    """首次配置向导写完 .env 后，重新读取并刷新本模块的配置全局变量。

    模块级变量在 import 时已求值；向导运行后 .env 变了，必须手动重读，
    否则后续 validate_config / 邮件发送仍用旧（空）值。
    """
    global ROOM_ID, API_URL, RECIPIENT, SMTP_HOST, SMTP_PORT
    global SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_TLS
    global THRESHOLD, PRICE_PER_KWH, REPORT_CYCLE_DAYS, MONTHLY_CYCLE_DAYS, RETENTION_DAYS
    global ANOMALY_MIN_SAMPLES, ANOMALY_MULTIPLE, ANOMALY_MIN_USAGE, ANOMALY_COOLDOWN_DAYS
    global RECHARGE_CAP_MULT, SERVE_PORT, SERVE_HOST, INTERVAL_HOURS, LOCAL_URL
    env = _load_dotenv()
    ROOM_ID = env.get("XJTU_ROOM_ID") or os.environ.get("XJTU_ROOM_ID") or ROOM_ID
    API_URL = f"https://ssn.xjtu.edu.cn/cems/mobile/meterAccount/electricity?roomId={ROOM_ID}"
    RECIPIENT = env.get("XJTU_RECIPIENT") or os.environ.get("XJTU_RECIPIENT") or RECIPIENT
    SMTP_HOST = env.get("SMTP_HOST") or os.environ.get("SMTP_HOST") or SMTP_HOST
    SMTP_PORT = _env_num("SMTP_PORT", SMTP_PORT, int)
    SMTP_USER = env.get("SMTP_USER") or os.environ.get("SMTP_USER") or SMTP_USER
    SMTP_PASS = env.get("SMTP_PASS") or os.environ.get("SMTP_PASS") or SMTP_PASS
    SMTP_FROM = env.get("SMTP_FROM") or os.environ.get("SMTP_FROM") or SMTP_USER
    SMTP_TLS = (env.get("SMTP_TLS") or os.environ.get("SMTP_TLS") or SMTP_TLS).lower()
    THRESHOLD = _env_num("THRESHOLD", THRESHOLD, float)
    PRICE_PER_KWH = _env_num("PRICE_PER_KWH", PRICE_PER_KWH, float)
    REPORT_CYCLE_DAYS = _env_num("REPORT_CYCLE_DAYS", REPORT_CYCLE_DAYS, int)
    MONTHLY_CYCLE_DAYS = _env_num("MONTHLY_CYCLE_DAYS", MONTHLY_CYCLE_DAYS, int)
    RETENTION_DAYS = _env_num("RETENTION_DAYS", RETENTION_DAYS, int)
    ANOMALY_MIN_SAMPLES = _env_num("ANOMALY_MIN_SAMPLES", ANOMALY_MIN_SAMPLES, int)
    ANOMALY_MULTIPLE = _env_num("ANOMALY_MULTIPLE", ANOMALY_MULTIPLE, float)
    ANOMALY_MIN_USAGE = _env_num("ANOMALY_MIN_USAGE", ANOMALY_MIN_USAGE, float)
    ANOMALY_COOLDOWN_DAYS = _env_num("ANOMALY_COOLDOWN_DAYS", ANOMALY_COOLDOWN_DAYS, int)
    RECHARGE_CAP_MULT = _env_num("RECHARGE_CAP_MULT", RECHARGE_CAP_MULT, float)
    SERVE_PORT = _env_num("XJTU_SERVE_PORT", SERVE_PORT, int)
    SERVE_HOST = (env.get("XJTU_SERVE_HOST")
                  or os.environ.get("XJTU_SERVE_HOST") or SERVE_HOST)
    INTERVAL_HOURS = max(0.5, _env_num("XJTU_INTERVAL_HOURS", INTERVAL_HOURS, float))
    LOCAL_URL = (env.get("XJTU_LOCAL_URL")
                 or os.environ.get("XJTU_LOCAL_URL")
                 or f"http://{'127.0.0.1' if SERVE_HOST in ('127.0.0.1', 'localhost') else _local_lan_ip()}:{SERVE_PORT}/")


def validate_config():
    """启动校验：房间号须正整数，收件人/SMTP 关键字段须齐全。失败即清晰报错退出。"""
    if not RECIPIENT:
        raise SystemExit(
            "❌ 未配置 XJTU_RECIPIENT：请在 .env 中填写接收通知的邮箱"
            "（XJTU_RECIPIENT=you@example.com）。")
    try:
        rid = int(ROOM_ID)
        if rid <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise SystemExit(f"❌ XJTU_ROOM_ID 必须是正整数，当前为：{ROOM_ID!r}")
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        raise SystemExit(
            "❌ SMTP 配置不完整：请在 .env 填写 SMTP_HOST / SMTP_USER / SMTP_PASS "
            "（发件邮箱地址 + SMTP 授权码）。常用预设见 config_wizard 或 .env.example。")


def _run_once():
    """单次抓取+计算+存档+图+报告+邮件；返回本轮是否成功。"""
    now = int(time.time())
    h = load_history()
    _update_status(state="running", last_run=now, last_error="",
                   history_count=len(h.get("history", [])), next_run=0)

    # ===== 抓取余额（含重试退避；瞬时失败仅记日志，持续失败才邮件）=====
    try:
        bal = fetch_balance()
    except AuthExpired as e:
        push_email_text(
            "⚠️ 电费接口拒绝访问（本次跳过）",
            "查询电费接口返回拒绝访问（%s）。\n"
            "请确认本机处于校园网（能访问 ssn.xjtu.edu.cn），或接口地址/参数已变更。\n"
            "本次未记录新数据；修复后下次运行即恢复正常。" % e)
        _update_status(state="error", last_error=f"接口拒绝访问：{e}",
                       history_count=len(h.get("history", [])))
        log.error("接口拒绝访问，已发邮件通知，跳过本轮。")
        return False
    except Exception as e:
        last_ok = h.get("last_success")
        if last_ok and (now - last_ok) >= FAIL_ALERT_HOURS * 3600:
            push_email_text(
                "⚠️ 电费抓取持续失败",
                "自 %s 以来持续无法查询电费接口：%s\n"
                "若为网络/服务器临时问题，通常稍后自动恢复；若持续，请检查网络或接口。" %
                (_fmt_ts(last_ok), e))
            log.error("抓取持续失败，已发邮件通知：%s", e)
        else:
            log.warning("本次抓取失败（瞬时，未发邮件，下次运行重试）：%s", e)
        _update_status(state="error", last_error=str(e),
                       history_count=len(h.get("history", [])))
        return False
    else:
        h["last_success"] = now

    h["history"], last24, avg_day = compute(h["history"], bal)
    _update_status(balance=bal, last_success=now, last24=last24,
                   avg_day=avg_day, history_count=len(h["history"]))
    save_history(h)
    kwh = f" | 约 {last24/PRICE_PER_KWH:.2f} 度" if PRICE_PER_KWH > 0 else ""
    log.info("当前余额 ¥%.2f | 近24h ¥%.2f%s | 日均 ¥%.2f", bal, last24, kwh, avg_day)

    # 低余额预警（每次）
    if bal <= THRESHOLD:
        title = f"⚠️ 宿舍电费仅剩 ¥{bal:.2f}"
        body = (f"低于预警线 {THRESHOLD:.0f} 元，该充值了！\n"
                f"近24h用电 ¥{last24:.2f}，日均约 ¥{avg_day:.2f}。")
        push_email_text(title, body)
        log.info("低余额预警已发送")

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
        log.info("检测到异常用电，已发送提醒邮件")

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
                       wk_chart)
        h["last_report"] = now
        save_history(h)
        log.info("已推送周报（近一周·手机版趋势图）")
    else:
        log.info("数据积累中（已满 %.1f 天 / 周报需 %d 天），暂不发送周报",
                 data_span / 86400, REPORT_CYCLE_DAYS)

    # 月报：每 30 天，发送「近30天·桌面横版」趋势图（须先积累满 30 天数据）
    monthly_due = (data_span >= MONTHLY_CYCLE_DAYS * 86400
                   and now - h.get("last_monthly", 0) >= MONTHLY_CYCLE_DAYS * 86400)
    if monthly_due:
        mo_chart = gen_chart(history, mobile=False, ndays=MONTHLY_CYCLE_DAYS)
        text = (f"当前余额 ¥{bal:.2f}，近30天日均约 ¥{avg_day:.2f}。"
                + (f" 近24h约 {last24/PRICE_PER_KWH:.2f} 度。" if PRICE_PER_KWH > 0 else ""))
        push_email_img(f"宿舍用电月报（近{MONTHLY_CYCLE_DAYS}天）", f"<p>{text}</p>",
                       mo_chart)
        h["last_monthly"] = now
        save_history(h)
        log.info("已推送月报（近30天·桌面横版趋势图）")

    # 本地默认图表（近30天）+ 网页报告，放最后刷新，确保 chart.png 始终是默认版
    chart_png = gen_chart(h["history"])
    gen_report(h["history"], chart_png)
    _update_status(state="success", last_error="", history_count=len(h["history"]))
    log.info("=== 本轮运行结束 ===")
    return True


def _service_loop(stop_event, wake_event):
    """后台服务循环；wake_event.set() 可让下一轮立即开始。"""
    while not stop_event.is_set():
        try:
            _run_once()
        except Exception as e:
            _update_status(state="error", last_error=str(e))
            log.exception("单次运行异常（服务继续）: %s", e)
        if stop_event.is_set():
            break
        next_run = time.time() + INTERVAL_HOURS * 3600
        _update_status(next_run=next_run)
        log.info("休眠 %.1f 小时至下一轮...", INTERVAL_HOURS)
        wake_event.wait(INTERVAL_HOURS * 3600)
        wake_event.clear()


def main():
    # ===== 调试参数 =====
    parser = argparse.ArgumentParser(description="宿舍电费自动监控")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验配置+打印将做什么，不抓数不发包")
    parser.add_argument("--test-email", action="store_true",
                        help="发送一封测试邮件确认 SMTP 邮件通道，然后退出")
    parser.add_argument("--once", action="store_true",
                        help="单次运行一次即退出（调试用）；默认无参数为常驻服务模式")
    parser.add_argument("--no-tray", action="store_true",
                        help="不启动系统托盘，仅运行后台服务（兼容旧用法）")
    args = parser.parse_args()

    # ===== 首次配置向导：.env 不存在或关键字段缺失时弹图形窗口引导填写 =====
    # 调试参数（dry-run/test-email）跳过向导，便于排障；无图形环境也跳过（回退到校验报错）。
    if not (args.dry_run or args.test_email) and not _config_complete():
        try:
            import config_wizard
            done = config_wizard.run_first_setup(APP_DIR)
        except Exception as e:
            # 无显示/无 tkinter 等环境：回退到原有的 fail-fast 配置校验
            log.warning("配置向导不可用（%s），继续走配置校验。", e)
            done = False
        if not done:
            # 用户在向导里取消：直接退出，不进入服务
            raise SystemExit("未完成首次配置，已退出。请重新运行并完成配置向导。")
        # 向导写完 .env 后，需重新加载本模块级配置变量才能读到新值
        _reload_config_from_env()

    # ===== 进程互斥：避免计划任务重叠并发读写 / 重复发信 =====
    # 调试模式（dry-run / 测试邮件）为手动单次操作，不加锁直接放行
    if not (args.dry_run or args.test_email):
        _lock_fd, _lock_path = _acquire_run_lock()
        atexit.register(_release_lock, _lock_fd, _lock_path)

    # ===== 日志与配置校验（fail-fast）=====
    setup_logging()
    try:
        validate_config()
    except SystemExit as e:
        log.error("配置校验失败: %s", e)
        raise
    initial_history = load_history()
    latest = initial_history.get("history", [])[-1:]
    _update_status(
        balance=latest[0]["b"] if latest else None,
        last_success=initial_history.get("last_success", 0),
        history_count=len(initial_history.get("history", [])),
    )
    log.info("=== 开始运行 (PID %d) ===", os.getpid())

    # ===== 调试：测试邮件 =====
    if args.test_email:
        ok = push_email_text("✅ 电费监控测试邮件",
                             "这是一封测试邮件，说明 SMTP 邮件通道正常。\n如收到说明配置无误。")
        log.info("测试邮件发送结果: %s", ok)
        return

    # ===== 调试：dry-run =====
    if args.dry_run:
        h = load_history()
        log.info("[dry-run] 房间号=%s  收件人=%s", ROOM_ID, RECIPIENT)
        log.info("[dry-run] 接口=%s", API_URL)
        log.info("[dry-run] SMTP=%s:%d (TLS=%s) 发件=%s", SMTP_HOST, SMTP_PORT, SMTP_TLS, SMTP_USER)
        log.info("[dry-run] 历史记录 %d 条；将执行：抓取余额→存档→"
                 "(低余额/异常/周报/月报)判定→生成图表与网页报告", len(h["history"]))
        log.info("[dry-run] 未抓取、未发送任何邮件。")
        return

    # ===== 单次/服务模式分发 =====
    if args.once:
        _run_once()
        return

    start_server_thread()
    stop_event = threading.Event()
    wake_event = threading.Event()

    if args.no_tray:
        _service_loop(stop_event, wake_event)
        return

    try:
        import tray_ui
    except Exception as e:
        log.warning("系统托盘不可用（%s），继续运行后台服务。", e)
        _service_loop(stop_event, wake_event)
        return

    worker = threading.Thread(
        target=_service_loop,
        args=(stop_event, wake_event),
        name="dorm-service",
        daemon=True,
    )
    worker.start()
    try:
        tray_ui.run(
            get_status=get_status,
            request_run=wake_event.set,
            request_stop=stop_event.set,
            local_url=LOCAL_URL,
            log_file=LOG_FILE,
        )
    except Exception:
        log.exception("系统托盘运行异常，后台服务继续。")
        while worker.is_alive():
            worker.join(timeout=1)
    finally:
        stop_event.set()
        wake_event.set()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
