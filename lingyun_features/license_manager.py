"""机器绑定离线授权。只含公钥；使用 Windows CNG 验签、DPAPI 保存授权。"""

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
import winreg

PRODUCT = "lingyun_practical_toolkit"
PUBLIC_KEY_FILE = Path(__file__).with_name("license_public.json")
_THREAD_LOCK = threading.RLock()


class LicenseError(ValueError):
    pass


def machine_code():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            identity = winreg.QueryValueEx(key, "MachineGuid")[0]
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("empty MachineGuid")
    except OSError as exc:
        raise LicenseError("无法读取本机机器码，请检查 Windows 注册表读取权限。") from exc
    digest = hashlib.sha256((PRODUCT + ":" + identity.strip().lower()).encode()).hexdigest()[:32].upper()
    return "LY-" + "-".join(digest[i:i + 4] for i in range(0, 32, 4))


def _decode(text):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise LicenseError("激活码格式错误，请完整复制。")
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


def _verify_signature(data, signature, public):
    """RSA-PSS / SHA256 / 32-byte salt, implemented by Windows, not custom crypto."""
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    handle = wintypes.HANDLE
    ptr = ctypes.c_void_p
    ulong = wintypes.ULONG
    signatures = {
        "BCryptOpenAlgorithmProvider": [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
        "BCryptImportKeyPair": [handle, handle, wintypes.LPCWSTR, ctypes.POINTER(handle), ptr, ulong, ulong],
        "BCryptVerifySignature": [handle, ptr, ptr, ulong, ptr, ulong, ulong],
        "BCryptDestroyKey": [handle],
        "BCryptCloseAlgorithmProvider": [handle, ulong],
    }
    for name, args in signatures.items():
        function = getattr(bcrypt, name)
        function.argtypes = args
        function.restype = ctypes.c_long
    n, e = int(public["n"], 16), int(public["e"])
    modulus = n.to_bytes((n.bit_length() + 7) // 8, "big")
    exponent = e.to_bytes((e.bit_length() + 7) // 8, "big")
    if n.bit_length() != 2048 or e != 65537 or len(signature) != len(modulus):
        raise LicenseError("激活码签名长度或公钥不正确。")
    blob = struct.pack("<6I", 0x31415352, n.bit_length(), len(exponent), len(modulus), 0, 0) + exponent + modulus
    algorithm, key = handle(), handle()

    class Padding(ctypes.Structure):
        _fields_ = [("pszAlgId", wintypes.LPCWSTR), ("cbSalt", ulong)]

    def checked(status):
        if status != 0:
            raise LicenseError("激活码签名无效，或不属于此版本插件。")

    try:
        checked(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "RSA", None, 0))
        checked(bcrypt.BCryptImportKeyPair(algorithm, None, "RSAPUBLICBLOB", ctypes.byref(key), blob, len(blob), 0))
        digest = hashlib.sha256(data).digest()
        padding = Padding("SHA256", 32)
        checked(bcrypt.BCryptVerifySignature(key, ctypes.byref(padding), digest, len(digest), signature, len(signature), 8))
    finally:
        if key:
            bcrypt.BCryptDestroyKey(key)
        if algorithm:
            bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def verify_code(code, machine=None, public=None):
    try:
        compact = re.sub(r"\s+", "", str(code or ""))
        if not compact or len(compact) > 8192:
            raise LicenseError("请填写完整激活码。")
        prefix, payload_text, signature_text = compact.split(".")
        if prefix != "LY1":
            raise LicenseError("激活码版本不正确。")
        payload_bytes, signature = _decode(payload_text), _decode(signature_text)
        public = public or json.loads(PUBLIC_KEY_FILE.read_text(encoding="utf-8"))
        _verify_signature(payload_bytes, signature, public)
        payload = json.loads(payload_bytes)
        if payload.get("product") != PRODUCT or payload.get("v") != 1:
            raise LicenseError("激活码不属于字字实用插件。")
        if payload.get("machine") != (machine or machine_code()):
            raise LicenseError("激活码与本机机器码不匹配。")
        if not re.fullmatch(r"[a-f0-9]{32}", str(payload.get("id", ""))):
            raise LicenseError("激活码编号无效。")
        days = payload.get("days")
        plan = payload.get("plan")
        valid = (plan == "month" and days == 30 or plan == "year" and days == 365 or
                 plan == "permanent" and days == 0 or
                 plan == "custom" and type(days) is int and 1 <= days <= 36500)
        if not valid or type(days) is not int:
            raise LicenseError("激活码档位或天数无效。")
        issued = payload.get("issued_at")
        if type(issued) is not int or issued < 1:
            raise LicenseError("激活码签发时间无效。")
        return payload
    except LicenseError:
        raise
    except Exception as exc:
        raise LicenseError("激活码格式或授权公钥异常，请检查完整激活码。") from exc


def _crypt(data, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise LicenseError("授权记录无法读取或保存，请使用激活时的 Windows 账户。")
    kernel32 = ctypes.WinDLL("kernel32.dll")
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _state_path():
    directory = os.environ.get("LOCALAPPDATA")
    if not directory:
        raise LicenseError("无法定位当前 Windows 账户的授权目录。")
    return Path(directory) / "Lingyun" / PRODUCT / "license.dat"


@contextmanager
def _locked(path):
    import msvcrt
    with _THREAD_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 10
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LicenseError("另一个窗口正在更新授权，请稍后重试。")
                    time.sleep(.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _load(path, machine):
    if not path.exists():
        return {"v": 1, "machine": machine, "expires_at": 0, "permanent": False, "used": [], "last_seen": 0}
    try:
        state = json.loads(_crypt(path.read_bytes(), decrypt=True))
        if state["v"] != 1 or state["machine"] != machine or type(state["permanent"]) is not bool:
            raise ValueError("bad state")
        for key in ("expires_at", "last_seen"):
            if type(state[key]) not in (int, float) or not math.isfinite(state[key]) or state[key] < 0:
                raise ValueError("bad time")
        if not isinstance(state["used"], list) or any(not isinstance(item, str) for item in state["used"]):
            raise ValueError("bad receipts")
        return state
    except Exception as exc:
        raise LicenseError("本地授权记录损坏或属于其他账户，请联系授权方处理。") from exc


def _save(path, state):
    data = _crypt(json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode())
    fd, temp_name = tempfile.mkstemp(prefix="license-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _describe(state, now):
    permanent = state["permanent"]
    expiry = state["expires_at"]
    active = permanent or expiry > now
    date = datetime.fromtimestamp(expiry) if expiry else None
    label = "已激活·永久" if permanent else ("已激活·" + f"{date.year}/{date.month}/{date.day}" if active else "未激活")
    return {"machine_code": state["machine"], "active": active, "permanent": permanent,
            "expires_at": expiry, "expires_text": date.strftime("%Y/%m/%d %H:%M:%S") if date else "",
            "label": label, "expired": bool(expiry and not active)}


def get_status():
    machine = machine_code()
    path = _state_path()
    with _locked(path):
        now = time.time()
        state = _load(path, machine)
        if not state["permanent"] and now + 300 < state["last_seen"]:
            return {**_describe(state, now), "active": False, "label": "未激活", "error": "系统时间早于最近验证时间，请校准系统时间。"}
        if state["used"] and now > state["last_seen"] + 60:
            state["last_seen"] = now
            _save(path, state)
        return _describe(state, now)


def activate(code):
    machine = machine_code()
    payload = verify_code(code, machine)
    path = _state_path()
    with _locked(path):
        now = time.time()
        state = _load(path, machine)
        if now + 300 < max(state["last_seen"], payload["issued_at"]):
            raise LicenseError("系统时间早于签发或最近验证时间，请校准系统时间。")
        if payload["id"] in state["used"]:
            raise LicenseError("此激活码已经使用过，不能重复增加天数。")
        if state["permanent"]:
            raise LicenseError("本机已永久激活，无需再使用激活码。")
        if payload["plan"] == "permanent":
            state["permanent"] = True
        else:
            state["expires_at"] = max(now, state["expires_at"]) + payload["days"] * 86400
            if state["expires_at"] > 253402214400:
                raise LicenseError("累计到期日超出支持范围。")
        state["used"].append(payload["id"])
        state["last_seen"] = max(now, state["last_seen"])
        _save(path, state)
        return {**_describe(state, now), "added_days": payload["days"]}


def require_active():
    status = get_status()
    if not status["active"]:
        raise LicenseError(status.get("error") or ("授权已到期，请输入新激活码续期。" if status["expired"] else "请先使用本机机器码获取激活码并激活插件。"))
    return status
