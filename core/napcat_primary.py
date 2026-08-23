#!/usr/bin/env python3
"""NapCat 主账号管理（2026-08-21：多账号抢连漏洞修复）

背景
----
NapCat 默认 onebot11.json 是"任意账号扫码登录自动连 bot"的回落桥，
多个账号的 onebot 实例都连 bot 的 WS 端口，抢 _active_websocket 单槽位：
  - 后连者顶掉先连者，发消息撞上换槽就丢（"WebSocket 未就绪"）
  - 掉线账号每 3s 重连，重连风暴刷日志（websockets handshake failed）

策略：单一主账号
  - 主账号 = 最后一次成功连入 bot 的账号（首次连接初始化 / 非主账号
    显式扫码升主——"最后一个扫码登录的视为主账号"）
  - 非主账号的 onebot11_<uin>.json websocketClients 全部 enable=false
  - 默认 onebot11.json 保持 enable=true（新账号扫码的入口，不能关）
  - 收敛后 docker restart napcat（配置只在启动时读取；登录态持久化
    在协议数据 volume，重启不掉线）

防再发
  - 启动收敛：bot 启动后拉齐配置到单主状态
  - 巡检自愈：每 30 分钟检查配置漂移（手改/回落生成），违规即收敛
  - 状态驱动判定：非主账号 WS 连入时读它自己的配置文件——
      enable=false → 自动重连残留 → 拒绝（不翻转主账号）
      enable=true / 无配置文件 → 显式扫码 → 升主
"""
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("napcat-primary")

PRIMARY_FILE = Path(__file__).resolve().parent.parent / "data" / "napcat_primary.txt"

_ACCOUNT_FILE_RE = re.compile(r"^onebot11_(\d+)\.json$")


# ============================================================
#  主账号状态
# ============================================================
def get_primary() -> dict | None:
    """读取主账号记录 {uin, nickname, switched_at, reason}；无记录返回 None"""
    try:
        d = json.loads(PRIMARY_FILE.read_text(encoding="utf-8"))
        if str(d.get("uin", "")):
            return d
    except Exception:
        pass
    return None


def set_primary(uin, nickname: str = "", reason: str = "") -> dict:
    """设置主账号（记录切换时间与原因，供事后审计）"""
    rec = {
        "uin": str(uin),
        "nickname": nickname,
        "switched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }
    PRIMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRIMARY_FILE.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def clear_primary():
    """清除主账号记录（测试/重置用）"""
    try:
        PRIMARY_FILE.unlink()
    except FileNotFoundError:
        pass


# ============================================================
#  配置扫描与判定
# ============================================================
def account_files(config_dir: str) -> dict[str, str]:
    """扫描账号级 onebot 配置：{uin: 文件路径}（不含默认 onebot11.json）"""
    out: dict[str, str] = {}
    d = Path(config_dir)
    if not d.is_dir():
        return out
    for p in d.glob("onebot11_*.json"):
        m = _ACCOUNT_FILE_RE.match(p.name)
        if m:
            out[m.group(1)] = str(p)
    return out


def account_bridge_enabled(config_dir: str, uin) -> bool | None:
    """该账号指向 bot:8696 的 WS 桥是否启用。

    - True  = 配置文件存在且 enable=true（显式扫码 / 手动开启）
    - False = 配置文件存在且 enable=false（已被系统收敛关死）
    - None  = 无该账号配置文件（新账号，回落默认桥——视同启用）
    """
    path = Path(config_dir) / f"onebot11_{uin}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        clients = data.get("network", {}).get("websocketClients") or []
        for c in clients:
            if str(c.get("url", "")).rstrip("/").endswith(":8696"):
                return bool(c.get("enable"))
        return False  # 有配置但没有指向 bot 的桥
    except Exception:
        return None


# ============================================================
#  收敛（配置侧关死非主账号）
# ============================================================
def ensure_primary_own_config(config_dir: str, primary_uin) -> bool:
    """固化主账号的 own 配置（onebot11_<uin>.json enable=true）。

    新账号靠默认 onebot11.json 回落连入 bot 时没有 own 配置——
    若固化成 own 配置，它升主/降主后都能被 converge 精确关死，
    重连也不会再被误判成"新账号"反复升主（翻转漏洞）。
    已存在 own 配置则只确保 enable=true。返回是否有变更。
    """
    d = Path(config_dir)
    if not d.is_dir():
        return False
    path = d / f"onebot11_{primary_uin}.json"
    changed = False
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if not isinstance(data, dict) or not data.get("network"):
            data = None
    else:
        data = None
    if data is None:
        # 无 own 配置 → 从默认桥复制结构（token/URL/重连参数保持一致）
        clients = [{"url": "ws://172.17.0.1:8696/", "token": "", "enable": True,
                    "reconnectInterval": 3000, "onebotVersion": "V11"}]
        default_file = d / "onebot11.json"
        try:
            dd = json.loads(default_file.read_text(encoding="utf-8"))
            for dc in dd.get("network", {}).get("websocketClients") or []:
                if str(dc.get("url", "")).rstrip("/").endswith(":8696"):
                    clients[0] = dict(dc)
                    clients[0]["enable"] = True
                    break
        except Exception:
            pass
        data = {"network": {"websocketClients": clients}}
        changed = True
    else:
        for c in data.get("network", {}).get("websocketClients") or []:
            if str(c.get("url", "")).rstrip("/").endswith(":8696") and not c.get("enable"):
                c["enable"] = True
                changed = True
    if changed:
        if path.exists():
            try:
                shutil.copy2(str(path), str(path) + ".pre_primary")
            except OSError:
                pass
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"📌 NapCat 主账号配置固化: onebot11_{primary_uin}.json (enable=true)")
    return changed


def converge(config_dir: str, primary_uin, container: str = "napcat",
             do_restart: bool = True) -> dict:
    """把配置拉齐到单主状态：主账号桥启用、非主账号桥关闭。

    返回 {enabled, disabled, errors, restarted}。
    默认 onebot11.json 不动（新账号扫码入口）。
    """
    result = {"enabled": [], "disabled": [], "errors": [], "restarted": False}
    # 固化主账号 own 配置（新账号升主后也能被后续收敛精确关死）
    try:
        if ensure_primary_own_config(config_dir, primary_uin):
            result["enabled"].append(str(primary_uin))
    except Exception as e:
        result["errors"].append(f"固化主账号配置: {e}")
    for uin, path in account_files(config_dir).items():
        want = (uin == str(primary_uin))
        cur = account_bridge_enabled(config_dir, uin)
        if cur == want:
            continue
        try:
            p = Path(path)
            data = json.loads(p.read_text(encoding="utf-8"))
            changed = False
            for c in data.get("network", {}).get("websocketClients") or []:
                if str(c.get("url", "")).rstrip("/").endswith(":8696"):
                    if bool(c.get("enable")) != want:
                        c["enable"] = want
                        changed = True
            if changed:
                # 备份收敛前状态（固定后缀，重复收敛覆盖）
                shutil.copy2(str(p), str(p) + ".pre_primary")
                p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if uin == str(primary_uin) and uin not in result["enabled"]:
                    result["enabled"].append(uin)
                elif uin not in result["enabled"]:
                    result["disabled"].append(uin)
                logger.info(f"🧹 NapCat 配置收敛: uin={uin} enable→{want}")
        except Exception as e:
            result["errors"].append(f"{uin}: {e}")

    if do_restart and (result["enabled"] or result["disabled"]):
        try:
            r = subprocess.run(["docker", "restart", container],
                               capture_output=True, timeout=90)
            if r.returncode == 0:
                result["restarted"] = True
                logger.info(f"🧹 NapCat 已重启使配置生效: {container}")
            else:
                result["errors"].append(f"docker restart 失败: {r.stderr.decode(errors='ignore')[:100]}")
        except Exception as e:
            result["errors"].append(f"docker restart 异常: {e}")
    return result


def drift_report(config_dir: str, primary_uin) -> list[str]:
    """检查配置漂移：返回违规（非主账号桥 enable=true）的 uin 列表。空 = 合规"""
    if not primary_uin:
        return []
    bad = []
    for uin in account_files(config_dir):
        if uin == str(primary_uin):
            continue
        if account_bridge_enabled(config_dir, uin) is True:
            bad.append(uin)
    return bad


def enable_all_bridges(config_dir: str) -> list[str]:
    """把全部账号桥恢复 enable=true（注销全清后调用，2026-08-23）。

    背景：单一主账号机制会把非主账号的 onebot11_<uin>.json 收敛成
    enable=false 并在 WS 连入时 reject。注销全清后旧登录态已不存在、
    主账号记录已清，但旧收敛配置还在——此时若直接扫码，新账号的
    WS 会被按「非主账号」拒绝，登进 QQ 却连不进 bot（比协议层故障
    更隐蔽的第二层雷）。

    全开后任意账号扫码连入都走「无主账号记录 → 首个连接升主」
    的 P1 路径，升主时再自动收敛关死其余账号。默认 onebot11.json
    不动（新账号扫码入口）。返回被改动的 uin 列表（空 = 无需操作）。
    """
    changed: list[str] = []
    for uin, path in account_files(config_dir).items():
        if account_bridge_enabled(config_dir, uin) is not False:
            continue
        try:
            p = Path(path)
            data = json.loads(p.read_text(encoding="utf-8"))
            changed_uin = False
            for c in data.get("network", {}).get("websocketClients") or []:
                if str(c.get("url", "")).rstrip("/").endswith(":8696") and not c.get("enable"):
                    c["enable"] = True
                    changed_uin = True
            if changed_uin:
                shutil.copy2(str(p), str(p) + ".pre_primary")
                p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                changed.append(uin)
                logger.info(f"🔓 NapCat 注销复位: onebot11_{uin}.json 桥恢复 enable=true")
        except Exception as e:
            logger.warning(f"⚠️ 注销复位账号桥失败 {uin}: {e}")
    return changed
