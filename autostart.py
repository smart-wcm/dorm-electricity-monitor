#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理 Windows 开机自启。

优先注册任务计划（支持崩溃重启），普通用户无法注册时退回当前用户的
HKCU Run 键。两个通道都使用同一个启动目标，关闭时会分别清理。
"""

import ctypes
from ctypes import wintypes
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

TASK_NAME = "DormElectricityMonitor"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "DormElectricityMonitor"
_TASK_QUERY_TIMEOUT = 8
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_SCHTASKS = os.path.join(_SYSTEM32, "schtasks.exe")
_WHOAMI = os.path.join(_SYSTEM32, "whoami.exe")

_NOT_FOUND_MARKERS = (
    "cannot find", "does not exist", "not found", "找不到", "不存在", "未找到"
)
_ACCESS_MARKERS = ("access is denied", "拒绝访问", "需要提升", "权限不足")


def _decode_output(data):
    data = data or b""
    for encoding in ("mbcs", "utf-8", "gbk"):
        try:
            return data.decode(encoding).strip()
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace").strip()


def _run(args, timeout=_TASK_QUERY_TIMEOUT):
    """执行隐藏的系统命令，返回 (退出码, 输出)。"""
    try:
        process = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "系统命令不存在"
    except subprocess.TimeoutExpired:
        return 124, "命令执行超时"
    except OSError as exc:
        return 1, f"命令执行失败：{exc}"
    output = (process.stdout or b"") + (process.stderr or b"")
    return process.returncode, _decode_output(output)


def _schtasks(args):
    return _run([_SCHTASKS] + list(args))


def _looks_like(output, markers):
    text = (output or "").lower()
    return any(marker.lower() in text for marker in markers)


def _current_user_sid():
    """读取当前交互用户 SID，用于避免 UAC 使用其他管理员账户时归属错误。"""
    if os.name != "nt":
        return ""
    rc, output = _run([_WHOAMI, "/user", "/fo", "csv", "/nh"], timeout=5)
    if rc != 0:
        return ""
    match = re.search(r"S-\d-\d+(?:-\d+)+", output)
    return match.group(0) if match else ""


def _launch_target():
    """返回 (可执行文件, 参数, 工作目录)。"""
    if getattr(sys, "frozen", False):
        executable = sys.executable
        return executable, "", os.path.dirname(executable)

    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "dorm_elec_auto.py")
    pythonw = os.path.join(here, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(pythonw):
        current = sys.executable
        if os.path.basename(current).lower() == "python.exe":
            candidate = os.path.join(os.path.dirname(current), "pythonw.exe")
            pythonw = candidate if os.path.isfile(candidate) else current
        else:
            pythonw = current
    return pythonw, f'"{script}"', here


def _startup_command():
    executable, arguments, _ = _launch_target()
    return f'"{executable}"' + (f" {arguments}" if arguments else "")


def _task_xml():
    sid = _current_user_sid()
    user_id = f"\n      <UserId>{escape(sid)}</UserId>" if sid else ""
    executable, arguments, workdir = _launch_target()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>宿舍电费监控：登录后自动运行。</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{user_id}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(executable)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _write_xml(xml):
    fd, path = tempfile.mkstemp(prefix="dorm_task_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as file:
            file.write(xml)
        return path
    except Exception:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        raise


def _remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _run_elevated_schtasks(args):
    """用 UAC 执行 schtasks，并等待真实退出码。"""
    if os.name != "nt":
        return 1, "任务计划仅支持 Windows"

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    INFINITE = 0xFFFFFFFF
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = _SCHTASKS
    info.lpParameters = subprocess.list2cmdline(list(args))
    info.nShow = 0

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            return 1223, "用户取消了管理员授权"
        if error == 5:
            return 5, "拒绝管理员授权"
        return error or 1, f"UAC 启动失败(code={error})"

    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return 1, f"无法读取 schtasks 退出码(code={ctypes.get_last_error()})"
        return int(code.value), ""
    finally:
        kernel32.CloseHandle(info.hProcess)


def _task_state():
    """返回 present / missing / error。"""
    rc, output = _schtasks(["/Query", "/TN", TASK_NAME])
    if rc == 0:
        return "present", ""
    if rc != 127 and _looks_like(output, _NOT_FOUND_MARKERS):
        return "missing", output
    return "error", output or f"查询失败(code={rc})"


def is_task_enabled():
    return _task_state()[0] == "present"


def is_registry_enabled():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
        return bool(value)
    except (FileNotFoundError, OSError, ImportError):
        return False


def _registry_enable():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ,
                              _startup_command())
        return True, "已通过注册表开启开机自启"
    except (OSError, ImportError) as exc:
        return False, f"注册表写入失败：{exc}"


def _registry_disable():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _RUN_VALUE)
        return True, ""
    except FileNotFoundError:
        return True, ""
    except (OSError, ImportError) as exc:
        return False, f"注册表启动项清理失败：{exc}"


def is_enabled():
    return is_task_enabled() or is_registry_enabled()


def current_method():
    if is_task_enabled():
        return "task"
    if is_registry_enabled():
        return "registry"
    return None


def _create_task(xml_path):
    args = ["/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"]
    rc, output = _schtasks(args)
    if rc == 0:
        return rc, output
    if _looks_like(output, _ACCESS_MARKERS):
        return _run_elevated_schtasks(args)
    return rc, output


def enable():
    """开启自启，返回 (成功?, 提示)。"""
    if os.name != "nt":
        return False, "开机自启仅支持 Windows"

    try:
        xml_path = _write_xml(_task_xml())
    except OSError as exc:
        xml_path = None
        task_result = (1, f"任务配置文件创建失败：{exc}")
    else:
        try:
            task_result = _create_task(xml_path)
        finally:
            _remove_file(xml_path)

    rc, output = task_result
    if rc == 0:
        registry_ok, registry_message = _registry_disable()
        if registry_ok:
            return True, "已开启开机自启（任务计划：崩溃自动重启）"
        return True, f"已开启任务计划，但旧注册表启动项未清理：{registry_message}"

    registry_ok, registry_message = _registry_enable()
    if registry_ok:
        detail = output or f"任务计划失败(code={rc})"
        return True, f"{registry_message}（任务计划不可用：{detail}）"
    return False, f"开启失败：{output or rc}；{registry_message}"


def _delete_task():
    args = ["/Delete", "/TN", TASK_NAME, "/F"]
    rc, output = _schtasks(args)
    if rc == 0:
        return True, ""
    if rc != 127 and _looks_like(output, _NOT_FOUND_MARKERS):
        return True, ""
    if _looks_like(output, _ACCESS_MARKERS):
        rc, output = _run_elevated_schtasks(args)
        if rc == 0 or (rc != 127 and _looks_like(output, _NOT_FOUND_MARKERS)):
            return True, ""
    return False, output or f"任务计划删除失败(code={rc})"


def disable():
    """关闭自启并尽量清理两个通道，返回 (成功?, 提示)。"""
    if os.name != "nt":
        return False, "开机自启仅支持 Windows"

    task_ok, task_message = _delete_task()
    registry_ok, registry_message = _registry_disable()
    errors = []
    if not task_ok:
        errors.append(task_message)
    if not registry_ok:
        errors.append(registry_message)
    if errors:
        return False, "；".join(errors)
    return True, "已关闭开机自启"


def toggle():
    if is_enabled():
        ok, message = disable()
    else:
        ok, message = enable()
    return ok, message, is_enabled()


def status_text():
    if os.name != "nt":
        return "开机自启：仅支持 Windows"
    method = current_method()
    if method == "task":
        return "开机自启：已开启（任务计划）"
    if method == "registry":
        return "开机自启：已开启（注册表）"
    return "开机自启：已关闭"


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "on":
        print(enable())
    elif action == "off":
        print(disable())
    elif action == "toggle":
        print(toggle())
    else:
        print(status_text())
        print("通道:", current_method())
        print("启动目标:", _launch_target())
