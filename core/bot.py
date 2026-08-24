#!/usr/bin/env python3
"""
QQ 群 AI 聊天机器人 - 监听模式
- 基于 OneBot 11 反向 WebSocket 协议
- NapCat 主动连接此端口
- 对接 LLM API（OpenAI 兼容协议：远程 API 为主力，本地 LLM 为后备）
- @触发回复
- 适配 websockets 16.0 + NapCat 4.x
- 支持 ArrayMessage 格式
- 支持引用回复 + @通知
- 按用户隔离上下文 + SQLite 持久化
"""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

# 确保项目根目录在 sys.path 中（支持 python3 core/bot.py 和 python3 -m core.bot 两种启动方式）
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import websockets
import websockets.legacy.server

# 防止 entertainment 等模块 import bot 时触发重复加载
sys.modules['bot'] = sys.modules[__name__]

# 以下导入为「模块预加载」：确保各游戏/工具模块在启动时完成初始化
# （注册昵称回调、加载题库/词库、初始化 DB schema 等副作用），
# 并保持旧版单文件入口的兼容命名空间。pyflakes 报告的 unused import
# 属于有意保留，请勿删除。
import games.entertainment as entertainment
check_entertainment_command = entertainment.check_command

import games.group_roleplay as group_roleplay
import games.pun_game as pun_game
import games.guess_wife as guess_wife
import games.cosplay_search as cosplay_search
import games.image_gen as image_gen
import games.game_spy as game_spy
import games.group_vote as group_vote
import core.help_menu as help_menu

# ============================================================
#  日志（v2：stderr + data/bot.log 双写，GUI 日志面板 tail 文件）
# ============================================================
_DATA_DIR = os.path.join(_project_root, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_DATA_DIR, "bot.log")

# 重复日志限流：同一条日志 30s 内只打一次（2026-08-21 事故修复：
# NapCat 掉线时每秒 1+ 次 "opening handshake failed"（websockets 库直接打
# 到 root logger，bot 代码不经过），15 小时刷了 14.9 万条；正常路径日志
# 间隔远超 30s，无感）
class _RateLimitFilter(logging.Filter):
    _last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            now = time.time()
            last = self._last.get(msg)
            if last is not None and now - last < 30:
                return False
            self._last[msg] = now
            if len(self._last) > 2000:  # 防泄漏
                self._last.clear()
        except Exception:
            pass
        return True

_log_handlers = [logging.StreamHandler(sys.stderr)]
try:
    # 轮转（2026-08-21 事故修复：原 FileHandler 无轮转，15 小时空转刷出
    # 3.5GB bot.log）。10MB × 5 份 ≈ 50MB 上限；GUI tab_logs._tick 已支持
    # 轮转（size 变小从头读），无需改 GUI。
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024,
                              backupCount=5, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _fh.addFilter(_RateLimitFilter())
    _log_handlers.append(_fh)
except Exception as _e:
    sys.stderr.write(f"[warn] 日志文件不可写，仅输出到 stderr: {_e}\n")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
    handlers=_log_handlers,
)
logger = logging.getLogger("qq-bot")

# ============================================================
#  NapCat 连接状态文件（掉线监测/提示，2026-08-10；v2：移到程序 data/ 下，跨平台）
# ============================================================
NAPCAT_STATUS_FILE = os.path.join(_DATA_DIR, "napcat_status.txt")


def _write_napcat_status(status: str, detail: str = "", account: str = ""):
    """写 NapCat 连接状态到状态文件（用户/监控脚本可随时读取）。

    account: 已登录时的账号标识（"昵称 (QQ号)"），供 GUI 总览页显示。

    2026-08-24：新增 qq_online 字段（QQ 真实在线状态，与 status 解耦）。
    status 只反映 WS 连接（原语义不动，外部监控脚本兼容）；qq_online
    反映 QQ 登录态（bot_offline 事件 / get_status 探测维护，见
    napcat_watchdog.qq_online_state）。假 connected 修复：WS 连着但
    QQ 被踢时，status=connected 但 qq_online=false，GUI 据此告警。
    """
    try:
        from core import napcat_watchdog as _nw
        qq_online = _nw.qq_online_state() if status == "connected" else False
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        if status == "connected":
            lines = [
                "status: connected",
                f"qq_online: {'true' if qq_online else 'false'}",
                f"time: {now_str}",
                f"detail: {detail or 'NapCat 已连接，bot 正常运行'}",
            ]
            if account:
                lines.append(f"account: {account}")
        else:
            lines = [
                "status: disconnected",
                f"time: {now_str}",
                f"detail: {detail or 'NapCat 未连接'}",
                # 2026-08-23：建议文案纠偏（08-23 复盘）——原"docker restart 多数
                # 自动恢复 token"对「登录态失效待扫码」无效且误导。真实分两种：
                #   a) QQ 登录态失效（最常见，NapCat 挂二维码）→ 只能 GUI 扫码
                #   b) HTTP 服务半死（容器活但 OneBot 服务挂）→ docker restart
                # 判断：docker logs napcat 若有「请扫描下面的二维码」= a)
                f"suggestion: 先 docker logs napcat 看根因——"
                f"有「请扫描下面的二维码/登录态已失效」= QQ 登录态失效，"
                f"打开 GUI 总览页 NapCat 卡片扫码（重启无效）；"
                f"无扫码提示 = OneBot HTTP 服务半死，docker restart napcat",
            ]
        with open(NAPCAT_STATUS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass  # 状态文件写失败不影响主流程

# ============================================================
#  从拆分模块导入
# ============================================================
from core.config import CONFIG, CONFIG_YAML_PATH, ENV_PATH, load_config as _reload_full_config, sync_bot_qq_to_yaml
from core.database import (
    get_db, get_persona_db, get_settings_db, ensure_all_dbs,
    _session_key, _format_timestamp,
    get_history, save_message, is_session_expired, reset_session,
    _get_user_nickname, cache_group_message, get_today_chat_log,
    is_blocked, block_user, unblock_user, list_blocked,
    is_on_cooldown, set_cooldown, cooldown_until,
)
from core.archive import (
    archive_message, archive_recall, archive_image, archive_voice,
    _archive_recall_images, _download_image, _download_image_sync,
    _download_voice, extract_image_urls_from_raw,
    _ARCHIVE_IMAGES_DIR, _RECALL_IMAGES_DIR, _ARCHIVE_VOICES_DIR,
)
from core.sender import (
    send_reply, send_reply_global, register_websocket, unregister_websocket,
    _consume_ws_event, get_login_info_for, _ws_alive,
    update_ws_identity, kick_non_primary_websockets, set_bot_uin,
)
from core.persona import (
    PERSONAS_SCHEMA,
    save_personality, get_personality,
    get_user_profile, save_user_profile, get_all_profiles, find_user_by_nickname,
    get_active_persona, get_persona_display, save_persona,
    persona_to_text, PERSONA_SECTIONS,
)
from core.llm import call_llm, _rp_llm_call
from core.scheduler import _start_schedulers, handle_summary, handle_evaluation
from core.router import handle_message


# ============================================================
#  配置文件带外变更检测（2026-08-22，修内存/磁盘状态分叉）
# ============================================================
def _config_file_sig() -> tuple:
    """config.yaml + .env 的 mtime 签名（带外变更检测用）。"""
    try:
        return (os.stat(CONFIG_YAML_PATH).st_mtime_ns,
                os.stat(ENV_PATH).st_mtime_ns)
    except (FileNotFoundError, OSError):
        return (0, 0)


def _maybe_reload_config_files(prev_sig: tuple) -> tuple:
    """config 文件带外变更检测（2026-08-22，修内存/磁盘状态分叉根因）。

    GUI 保存走 POST /config → load_config，只覆盖"GUI 发起"的变更；
    用户/其他进程直接写 config.yaml/.env 时 bot 无感知 → LLM_ENABLED 等
    内存值永久停在旧值（08-22 实例：20:32 直改 llm.enabled，内存分叉数小时，
    总开关"关了"却还在烧 LLM）。主循环 0.5s 轮询 mtime 签名，
    变化时自动 load_config()（内部 _refresh_env 一并刷新 .env 缓存）。

    解析失败（写盘中途读到半成品/格式错误）不更新签名 → 下一轮自动重试，
    内存配置保持旧值（load_config 自身语义：失败不动 CONFIG）。

    Returns: (new_sig, applied_count)——签名只在重载成功后推进。
    """
    now_sig = _config_file_sig()
    if now_sig == prev_sig:
        return prev_sig, 0
    try:
        report = _reload_full_config()
    except Exception as e:
        logger.warning(f"⚠️ 配置文件变更检测异常（保持内存配置，下轮重试）: {e}")
        return prev_sig, 0
    if report.get("errors"):
        # 写盘中途/解析失败：不更新签名，下一轮（文件写完后）重试
        logger.warning(f"⚠️ 配置文件重载失败（保持内存配置，下轮重试）: {report['errors']}")
        return prev_sig, 0
    applied = report.get("applied") or {}
    if applied:
        logger.info(
            f"📝 配置文件带外变更（config.yaml/.env 直接修改），自动热加载 "
            f"{len(applied)} 项: {list(applied)[:6]}"
        )
    return now_sig, len(applied)


# ============================================================
#  主账号判定（2026-08-21：多账号抢连修复）
# ============================================================
async def _resolve_primary(websocket):
    """连接级主账号判定。返回 (uin, nickname, action)：

    - (uin, nick, "register")  本连接是主账号（或未启用收敛，旧行为）→ 正常注册
    - (uin, nick, "promote")   本连接扫码升主（最后扫码登录者胜出）→ 注册 + 踢旧 + 收敛
    - ("" ,   "",  "reject")   非主账号且配置已收敛 → 拒绝（关连接，防抢连/防翻转）

    判定依据（状态驱动，无时间窗口，杜绝 3 秒重连翻转）：
      该账号 own 的 onebot11_<uin>.json：
        enable=false → 重连残留（实例本不该在跑）→ reject
        enable=true  → 显式扫码/手动开启        → promote
        无配置文件   → 新账号回落默认桥          → promote
    """
    from core import napcat_primary
    config_dir = CONFIG.get("NAPCAT_CONFIG_DIR", "")
    info = await get_login_info_for(websocket)
    if not info:
        # 识别不了账号：不抢槽、不踢人，按旧行为注册（收敛未启用时的兜底）
        logger.warning("⚠️ 主账号判定: get_login_info 失败，按旧行为注册（收敛未生效）")
        return ("", "", "register")
    uin, nick = str(info["user_id"]), str(info.get("nickname", ""))
    if not config_dir:
        # 未启用收敛：保持旧行为（后连者占槽），仅记录身份
        update_ws_identity(websocket, uin, nick)
        return (uin, nick, "register")

    primary = napcat_primary.get_primary()
    if not primary:
        # 首次连接 → 初始化为当前主账号
        napcat_primary.set_primary(uin, nick, reason="首次连接初始化")
        logger.info(f"👑 NapCat 主账号初始化: {nick} ({uin})")
        return (uin, nick, "promote")
    if uin == str(primary["uin"]):
        update_ws_identity(websocket, uin, nick)
        return (uin, nick, "register")

    # 非主账号：按配置状态判"真扫码"还是"重连残留"
    #   enable=true  → 显式扫码/手动开启        → promote
    #   None(无配置) → 新账号回落默认桥          → promote
    #   enable=false → 已被收敛关死（实例不该在跑）→ reject（重连残留）
    enabled = await asyncio.to_thread(
        napcat_primary.account_bridge_enabled, config_dir, uin
    )
    if enabled is not False:
        # 显式扫码或新账号回落 → 升主（后登录者胜出）
        napcat_primary.set_primary(uin, nick, reason="扫码升主（后登录者胜出）")
        logger.info(f"👑 NapCat 主账号切换: {nick} ({uin}) 升为主账号")
        return (uin, nick, "promote")
    # enable=false（已收敛关死）→ 自动重连残留 → 拒绝（防翻转）
    logger.warning(
        f"🚫 拒绝非主账号 WS 连接: {nick} ({uin})，"
        f"当前主账号 {primary.get('nickname', '?')} ({primary.get('uin', '?')})。"
        f"如需切主: 在 NapCat 控制台重新扫码该账号（扫码会自动开启其 onebot 桥 → 升主）"
    )
    return ("", "", "reject")


def _converge_async():
    """后台收敛配置（切主/初始化后调用）。失败只告警不影响连接。"""
    from core import napcat_primary

    def _run():
        try:
            primary = napcat_primary.get_primary()
            if not primary:
                return
            r = napcat_primary.converge(
                CONFIG["NAPCAT_CONFIG_DIR"], primary["uin"],
                CONFIG.get("NAPCAT_CONTAINER", "napcat"),
            )
            if r.get("errors"):
                logger.warning(f"⚠️ NapCat 配置收敛部分失败: {r['errors']}")
            else:
                logger.info(f"🧹 NapCat 配置收敛完成: {r}")
        except Exception as e:
            logger.warning(f"⚠️ NapCat 配置收敛异常: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ============================================================
#  登录账号异步确认（2026-08-22 加强版，替代一次性线程查询）
# ============================================================
async def _confirm_account(websocket, remote_addr: str, max_attempts: int = 5,
                           interval: float = 2.0):
    """连接级异步重试确认登录账号（模块级函数，便于单测）。

    背景：_resolve_primary 在连接建立时立即查 get_login_info，NapCat 未就绪时
    会失败 → set_bot_uin 不执行 → get_bot_uin() 永久回落 config 兜底号。
    多人部署时 config 里的号是别人的/过期的 → @ 判定错位、@bot 静默无回复，
    且旧实现无任何日志提示（排查全靠猜）。

    本函数：
      - 用连接级 get_login_info_for(ws)（不走全局活跃槽，防连接池查错账号）
      - 最多 max_attempts 次、间隔 interval 秒
      - 成功：set_bot_uin + 状态文件落盘 + config 对账告警
      - 全失败：打醒目 WARNING（@判定回落 config 兜底值，可能失效）
      - 连接中途断开：提前退出（handler finally 已写 disconnected 状态）
    """
    for attempt in range(1, max_attempts + 1):
        # 2026-08-22：_ws_alive 兼容 websockets 新旧 API（16.x 无 .open 属性，
        # 旧 getattr(ws,"open",False) 在新版恒为 False → 确认任务静默退出）
        if not _ws_alive(websocket):
            return  # 连接已断开，不再确认
        try:
            info = await get_login_info_for(websocket)
        except Exception as e:
            info = None
            logger.debug(f"登录账号确认 第{attempt}次 异常: {e}")
        if info and info.get("user_id"):
            uin = str(info["user_id"])
            nick = str(info.get("nickname", "") or "")
            account = f"{nick} ({uin})" if nick else uin
            logger.info(f"👤 NapCat 已登录账号: {account}（第{attempt}次确认成功）")
            set_bot_uin(uin)  # 运行时身份确认 → @判定/身份派生以此为真相源
            _cfg_qq = str(CONFIG.get("BOT_QQ", "") or "")
            if _cfg_qq and _cfg_qq != uin:
                # 2026-08-23：启动对账从「告警等人工改」升级为「自动回写」。
                # 确认真实登录号后把 config.yaml 的 bot.qq 兜底值同步为实际号
                # （sync_bot_qq_to_yaml 值相同不动文件），并立即热重载内存——
                # 兜底值永远跟随实际登录号，切号后不再吃过期错号。
                # 0.5s 带外变更检测随后看到值一致（mtime 变了但 applied 为空）→ 静默。
                # 失败只告警不阻断（下轮连接确认会再试）。
                try:
                    if sync_bot_qq_to_yaml(uin):
                        _reload_full_config()
                        logger.info(
                            f"📝 config bot.qq 已自动同步: {_cfg_qq} → {uin}"
                            f"（来源: NapCat get_login_info）")
                    else:
                        logger.warning(
                            f"⚠️ config bot.qq({_cfg_qq}) 与实际登录号({uin}) 不一致，"
                            f"自动回写未执行；运行时以 {uin} 为准")
                except Exception as e:
                    logger.warning(
                        f"⚠️ config bot.qq 自动同步失败（运行时仍以 {uin} 为准）: {e}")
            # 08-24：QQ 在线状态恢复（WS 重连/新连接账号确认成功 =
            # QQ 真实在线的权威信号，覆盖此前 bot_offline 置的离线）
            from core import napcat_watchdog as _nw
            _nw.mark_qq_online()
            _write_napcat_status("connected", f"remote={remote_addr}", account=account)
            return
        await asyncio.sleep(interval)
    logger.warning(
        f"⚠️ 无法确认 NapCat 登录账号（{max_attempts} 次尝试均失败），@ 判定回落到 "
        f"config bot.qq 兜底值（{CONFIG.get('BOT_QQ', '') or '空'}），可能失效——"
        f"请检查 NapCat 登录状态与 OneBot API")


# ============================================================
#  WebSocket 处理器
# ============================================================
async def websocket_handler(websocket):

    # 远端地址：aiohttp remote_address 是 namedtuple('AddrInfo', 'host port')，
    # 直接 f-string 会打出 ("('ip', port)") 带括号引号 → 取字段拼 ip:port
    try:
        _rh, _rp = websocket.remote_address
        remote_addr = f"{_rh}:{_rp}"
    except Exception:
        remote_addr = str(websocket.remote_address)
    logger.info(f"🔗 NapCat 已连接: {remote_addr}")

    # ---- 主账号判定（2026-08-21）：非主账号重连残留直接拒绝 ----
    uin, nick, action = await _resolve_primary(websocket)
    if action == "reject":
        try:
            await websocket.close(code=4002, reason="非主账号连接（主账号已固定）")
        except Exception:
            pass
        return

    _write_napcat_status("connected", f"remote={remote_addr}",
                         account=f"{nick} ({uin})" if nick else "")
    if action == "promote":
        # 踢掉池中其他连接（旧主账号/旧连接），并收敛 NapCat 配置
        kicked = kick_non_primary_websockets(websocket)
        if kicked:
            logger.info(f"🧹 已关闭 {kicked} 条旧 NapCat 连接（主账号切换）")
        _converge_async()

    # 拉取已登录账号信息（昵称+QQ号），写入状态文件供 GUI 显示。
    # 2026-08-22 加强：连接级异步重试确认（替代旧的一次性线程查询）——
    #   旧实现睡 2 秒查一次，查失败就永久回落 config 兜底号（多人部署时
    #   config 里的号必然是错的 → @ 判定错位、@bot 静默无回复且无日志）。
    #   现改为：连接级 get_login_info_for(ws)（不走全局槽，防连接池查错账号），
    #   最多 5 次、间隔 2s、单次超时 8s；成功即 set_bot_uin + 状态文件落盘；
    #   全失败打醒目 WARNING 提示运维。连接中途断开则提前退出（不误报警告）。
    asyncio.create_task(_confirm_account(websocket, remote_addr))

    # 注册 websocket，供 entertainment 模块后台发送消息
    register_websocket(websocket)
    if uin:
        update_ws_identity(websocket, uin, nick)
        # 08-22：运行时身份从连接派生（get_bot_uin 读它；CONFIG[bot.qq] 仅兜底）
        set_bot_uin(uin)

    # 注册昵称获取回调（pun_game / guess_wife / turtle_soup 需要）
    pun_game.register_get_nickname(_get_user_nickname)
    guess_wife.register_get_nickname(_get_user_nickname)
    import games.turtle_soup as turtle_soup
    turtle_soup.register_get_nickname(_get_user_nickname)

    # 启动所有后台定时任务
    await _start_schedulers()

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
                # 拦截 action 响应（request_action 的 echo 匹配），不进入消息路由
                if _consume_ws_event(data):
                    continue
                await handle_message(websocket, data)
            except json.JSONDecodeError:
                logger.warning(f"非 JSON 消息: {str(raw)[:80]}")
            except Exception as e:
                logger.error(f"消息处理错误: {e}", exc_info=True)
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"NapCat 连接断开: code={e.code}, reason={e.reason}")
        _write_napcat_status("disconnected", f"连接断开 code={e.code} reason={e.reason}")
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}", exc_info=True)
        _write_napcat_status("disconnected", f"异常: {e}")
    finally:
        # 条件清槽（2026-08-21 竞态修复）：槽里当前是这条连接才清——
        # 切主后旧连接的延迟清理不再抹掉已注册的新主连接
        unregister_websocket(websocket)
        # 仅当连接池清空才写"断开"状态（还有其他活跃连接时不误报）
        from core import sender as _sender
        if _sender.count_ws_connections() == 0:
            logger.info("🔌 NapCat 连接已清理")
            _write_napcat_status("disconnected", "连接已清理")


# ============================================================
#  启动入口
# ============================================================
def run_server():
    host = CONFIG.get("LISTEN_HOST", "0.0.0.0")
    port = int(CONFIG.get("LISTEN_PORT", "8696"))

    # 初始化数据库（聊天、人设、Bot设置）
    ensure_all_dbs()

    # 初始化存档目录
    os.makedirs(_ARCHIVE_IMAGES_DIR, exist_ok=True)
    os.makedirs(_RECALL_IMAGES_DIR, exist_ok=True)
    os.makedirs(_ARCHIVE_VOICES_DIR, exist_ok=True)

    # LLM 后端一致性检查（选了 remote 但没 key → 回退本地，提前告警）
    _backend = str(CONFIG.get("LLM_BACKEND", "remote")).lower()
    if _backend == "remote" and not CONFIG.get("REMOTE_API_KEY"):
        logger.warning("⚠️ llm.backend=remote 但 REMOTE_API_KEY 为空（.env 未配置），已回退本地 LLM: %s", CONFIG.get("LLM_API"))
    elif _backend == "local" and not CONFIG.get("LLM_API"):
        logger.warning("⚠️ llm.backend=local 但 local_api 未配置，LLM 调用将失败")

    logger.info(f"🚀 QQ 机器人 v3 启动中... 监听 {host}:{port}")

    async def main():
        from core.llm import _set_main_loop
        from core import control_api
        _set_main_loop()

        # 控制 API（GUI 专用通道，先于 WS 启动，GUI 可立即看到 bot 状态）
        await control_api.start_control_api()

        server = await websockets.serve(
            websocket_handler,
            host,
            port,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,  # 2MB
            # 08-24 修复：默认 max_queue=16，NapCat 登录后瞬间批量推送
            # （一次可达 20+ 条，含图片/语音/文本）在 bot 主账号判定完成
            # （get_login_info 最长 8s）前积压，超过 16 条旧消息被队列
            # 丢弃 → 启动瞬间图片/消息静默丢失不存档。调大缓冲防丢帧。
            max_queue=1024,
        )
        logger.info(f"✅ WebSocket 服务已启动: ws://{host}:{port}")

        # NapCat 集成：确保协议端在跑（Windows=内置绿色版自动拉起；
        # Linux=docker 容器；off=外部自管跳过）。WS 服务必须先于 NapCat
        # 就绪——NapCat 会反向连入本服务（auto 模式 Windows 一键部署关键）
        from core import napcat_manager
        _nc = napcat_manager.ensure_running()
        if _nc.get("ok"):
            logger.info(f"🐱 NapCat 集成: {_nc.get('message', '就绪')}")
        else:
            logger.warning(f"⚠️ NapCat 集成未就绪: {_nc.get('error', '')}（不影响 bot 运行，可稍后在 GUI 刷新）")

        # NapCat HTTP 服务守护（08-22）：探活 + 告警 + 可选自动重启。
        # 半死态（QQ 客户端活、OneBot HTTP 挂死）无容器 crash 无告警，
        # 此前转发存档静默丢数据；watchdog 每 tick 读 CONFIG 支持热生效。
        from core import napcat_watchdog
        asyncio.create_task(napcat_watchdog.watchdog_loop())

        # ---- 首次主账号收敛（2026-08-21 多账号抢连修复）----
        # 有主账号记录 → 后台把配置拉齐到单主状态（主账号桥 enable、
        # 非主账号桥 disable + docker restart 生效）。
        # 无记录（功能引入后首启）→ 不擅自收敛：主账号由第一个 WS 连接
        # 确定（最后扫码登录者胜出），升主时立即收敛。
        if CONFIG.get("NAPCAT_CONFIG_DIR"):
            def _initial_converge():
                import time as _t
                from core import napcat_primary
                _t.sleep(5)  # 等 NapCat 容器就绪（重启收敛可能发生在刚拉起的容器上）
                try:
                    primary = napcat_primary.get_primary()
                    if primary is None:
                        logger.info(
                            "👑 NapCat 无主账号记录：将由第一个 WS 连接确定"
                            "（最后扫码登录者视为主账号），升主时自动收敛配置"
                        )
                        return
                    r = napcat_primary.converge(
                        CONFIG["NAPCAT_CONFIG_DIR"], primary["uin"],
                        CONFIG.get("NAPCAT_CONTAINER", "napcat"),
                    )
                    logger.info(f"🧹 NapCat 首次配置收敛: {r}")
                except Exception as e:
                    logger.warning(f"⚠️ NapCat 首次配置收敛失败: {e}")

            threading.Thread(target=_initial_converge, daemon=True).start()

        # 优雅关闭（v2：跨平台信号——Windows ProactorEventLoop 不支持 add_signal_handler）
        stop = asyncio.Future()
        loop = asyncio.get_event_loop()

        def _signal_handler(*_a):
            if not stop.done():
                stop.set_result(True)

        if sys.platform == "win32":
            # Windows：仅 SIGINT 可靠（SIGTERM 在 Proactor 下受限），另加轮询兜底
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _signal_handler)

        # 2026-08-22：config 带外变更检测签名（主循环每轮比对 mtime）
        _cfg_sig = _config_file_sig()
        while not stop.done():
            await asyncio.sleep(0.5)
            # GUI 控制 API 的重启请求（POST /restart）
            if control_api.is_restart_requested():
                logger.warning("🔁 收到 GUI 重启请求，退出进程（由 GUI 重新拉起）")
                break
            # 2026-08-22：config.yaml/.env 带外变更检测（mtime 签名，0.5s 轮询）
            _cfg_sig, _cfg_applied = _maybe_reload_config_files(_cfg_sig)

        logger.info("🛑 正在停止服务...")
        # 2026-08-24 孤儿进程修复：bot 退出必须清掉 NapCat 子进程
        # （win 绿色版 node.exe 是 bot 的孙进程，不清会残留成孤儿；
        # docker/off 模式 stop() 内部已是 no-op）
        try:
            from core import napcat_manager
            napcat_manager.stop()
        except Exception as e:
            logger.warning(f"⚠️ 停止 NapCat 异常: {e}")
        server.close()
        await server.wait_closed()
        await control_api.stop_control_api()
        logger.info("👋 服务已停止")

    asyncio.run(main())


if __name__ == "__main__":
    run_server()
