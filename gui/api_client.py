"""
api_client.py — GUI 侧控制 API 客户端
=====================================
- 同步 HTTP（在 QThread worker 中调用，不阻塞 UI）
- 附带直读 SQLite 的辅助查询（消息/人设/群组数据面板用）
"""

import json
import os
import sqlite3
import urllib.request
import urllib.error
from typing import Optional

# 项目根 = gui/ 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ApiError(Exception):
    pass


def _ctrl_base(cfg: dict) -> str:
    return f"http://{cfg.get('CONTROL_API_HOST', '127.0.0.1')}:{cfg.get('CONTROL_API_PORT', 8697)}"


def _request(cfg: dict, method: str, path: str, payload=None, timeout: float = 30.0) -> dict:
    url = _ctrl_base(cfg) + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise ApiError(f"HTTP {e.code}: {body}") from None
    except Exception as e:
        raise ApiError(f"{type(e).__name__}: {e}") from None


# ------------------------------------------------------------
#  控制 API 端点
# ------------------------------------------------------------
def get_status(cfg: dict) -> dict:
    return _request(cfg, "GET", "/status")


def get_config_redacted(cfg: dict) -> dict:
    return _request(cfg, "GET", "/config")


def reload_config(cfg: dict) -> dict:
    """通知 bot 重读 config.yaml/.env，返回 {applied, restart_required, errors}"""
    return _request(cfg, "POST", "/config", {})


def reload_resources(cfg: dict, what: str = "all") -> dict:
    return _request(cfg, "POST", "/reload", {"what": what}, timeout=60)


def test_llm(cfg: dict) -> dict:
    return _request(cfg, "POST", "/test/llm", {}, timeout=90)


def get_usage(cfg: dict) -> dict:
    """LLM 用量统计（token 消耗，按日/模型分桶）。"""
    return _request(cfg, "GET", "/llm/usage", timeout=10)


def get_recent_request(cfg: dict) -> dict:
    """最近一次 LLM 请求摘要（{time, model, source, preview}，无记录为 {}）。"""
    return _request(cfg, "GET", "/llm/recent", timeout=10)


def test_comfyui(cfg: dict, url: Optional[str] = None) -> dict:
    """url 可选：传了=用该地址探活（GUI 配置面板表单地址），不传=bot 内存配置。"""
    payload = {}
    if url:
        payload["url"] = url
    return _request(cfg, "POST", "/test/comfyui", payload, timeout=30)


def get_napcat(cfg: dict) -> dict:
    """NapCat 登录态 + 二维码（base64 PNG）。"""
    return _request(cfg, "GET", "/napcat", timeout=20)


def napcat_restart(cfg: dict) -> dict:
    """重启 NapCat 容器刷新二维码（仅未登录时允许）。"""
    return _request(cfg, "POST", "/napcat/restart", {}, timeout=90)


def napcat_logout(cfg: dict) -> dict:
    """注销 NapCat 登录（2026-08-23 全清：清 QQ 数据 volume + passkey +
    bot 侧状态，之后扫码全新登录）。超时 180s：含 2.7G 数据备份 + 容器重启。"""
    return _request(cfg, "POST", "/napcat/logout", {}, timeout=180)


def forward_refetch(cfg: dict, payload: dict) -> dict:
    """重新拉取一条转发存档（GUI 转发查看器「重试拉取」，08-23）。

    HTTP 挂死/未启用时 failed 的记录，通道恢复（或 WS 反向兜底）后
    可救回——forward_id 在 QQ 服务器长期有效。
    """
    return _request(cfg, "POST", "/forward/refetch", payload, timeout=90)


def request_restart(cfg: dict) -> dict:
    return _request(cfg, "POST", "/restart", {})


# ------------------------------------------------------------
#  任务列表 暂停/继续（2026-08-22：总览页任务面板按钮）
# ------------------------------------------------------------
def tasks_pause(cfg: dict) -> dict:
    """暂停任务序列（范围=全部、无限等待；幂等）。"""
    return _request(cfg, "POST", "/tasks/pause", {})


def tasks_resume(cfg: dict) -> dict:
    """继续任务序列（放行全部等待任务；幂等）。"""
    return _request(cfg, "POST", "/tasks/resume", {})


# ------------------------------------------------------------
#  SQLite 直读（GUI 数据面板）
# ------------------------------------------------------------
def db_path(cfg: dict, kind: str) -> str:
    """kind: chat / personas / settings / reports / truth_dare / spy / turtle_soup / roleplay"""
    base = {
        "chat": cfg["DB_PATH"],
        "personas": cfg["PERSONAS_DB_PATH"],
        "settings": cfg["BOT_SETTINGS_DB_PATH"],
        "reports": cfg["DAILY_REPORTS_DB_PATH"],
        "truth_dare": cfg["TRUTH_DARE_DB_PATH"],
    }
    if kind in ("spy", "turtle_soup", "roleplay"):
        # spy_history / turtle_soup_history / group_roleplay 无专用配置键，
        # 按 data_dir 约定派生
        data_dir = os.path.dirname(cfg["DB_PATH"])
        if kind == "roleplay":
            base[kind] = os.path.join(data_dir, "group_roleplay.db")
        else:
            base[kind] = os.path.join(
                data_dir, "spy_history.db" if kind == "spy" else "turtle_soup_history.db")
    return base[kind]


def query(cfg: dict, kind: str, sql: str, params: tuple = (), write: bool = False) -> list:
    """
    执行 SQL 返回行列表（dict 形式）。
    write=True 时允许写操作（提交事务）——GUI 数据面板的增删改用。
    """
    path = db_path(cfg, kind)
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        if write:
            conn.commit()
            return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------
#  本地配置读取（bot 未运行时 GUI 仍需展示配置）
# ------------------------------------------------------------
def load_yaml() -> dict:
    import yaml
    with open(os.path.join(PROJECT_ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(data: dict) -> None:
    import yaml
    with open(os.path.join(PROJECT_ROOT, "config.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=120)


def load_env() -> dict:
    env = {}
    path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(env: dict) -> None:
    path = os.path.join(PROJECT_ROOT, ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# QQ Bot v3 密钥文件（勿提交 git / 勿外传）\n")
        f.write("# 远程 API 密钥（backend=remote 时用，任意 OpenAI 兼容服务）\n")
        f.write(f"REMOTE_API_KEY={env.get('REMOTE_API_KEY', '')}\n")
        f.write("# 本地 LLM（backend=local 时用，多数本地服务无需 key 可留空）\n")
        f.write(f"LLM_API_KEY={env.get('LLM_API_KEY', '')}\n")
