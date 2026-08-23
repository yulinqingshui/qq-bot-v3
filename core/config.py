# ============================================================
#  config.py — v2 全局配置（外部化版）
#
#  单一事实源：项目根目录 config.yaml + 同目录 .env（密钥）
#  - CONFIG 为「运行时活对象」：所有模块继续 from .config import CONFIG，
#    调用处零改动；load_config() 原地更新 → 天然热加载
#  - .env 只放密钥（DEEPSEEK_API_KEY / LLM_API_KEY），覆盖 yaml 同名字段
#  - 改 config.yaml 后：bot 内控制 API POST /config 热重载；
#    GUI 直接写 yaml 后调用同一入口
# ============================================================

import os
import re
import copy

import yaml

# 人设/画像提示词白名单（persona_prompts 是纯数据模块，无循环依赖）
from .persona_prompts import prompt_keys as _persona_prompt_keys
_PERSONA_PROMPT_KEYS = frozenset(_persona_prompt_keys())
# 真心话大冒险提示词白名单（同上，2026-08-21 设置功能）
from .truth_dare_prompts import prompt_keys as _td_prompt_keys
_TD_PROMPT_KEYS = frozenset(_td_prompt_keys())
# 群体角色扮演提示词白名单（2026-08-22 角色扮演页设置功能）
from .roleplay_prompts import prompt_keys as _rp_prompt_keys
_RP_PROMPT_KEYS = frozenset(_rp_prompt_keys())
# 查询/分析命令提示词白名单（2026-08-22 查询/分析命令配置化设置功能）
from .qa_prompts import prompt_keys as _qa_prompt_keys
_QA_PROMPT_KEYS = frozenset(_qa_prompt_keys())

# ------------------------------------------------------------
#  路径基准
# ------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_YAML_PATH = os.path.join(_PROJECT_ROOT, "config.yaml")
ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


# ------------------------------------------------------------
#  默认配置（yaml 缺失项的回退值）
# ------------------------------------------------------------
DEFAULTS = {
    "listen": {"host": "0.0.0.0", "port": 8696},
    "control_api": {"host": "127.0.0.1", "port": 8697},
    "bot": {
        "qq": "",                # 兜底值；留空=纯依赖 NapCat 连接派生
        "max_history": 400,
        "cooldown_seconds": 3,
        "session_timeout": 259200,
        "session_gap_seconds": 1800,
        # 好友申请自动通过（GUI 其他设置弹窗可关，08-23）
        "auto_approve_friend": True,
        # 复读+1 全局开关（GUI 其他设置弹窗可关，08-23）
        "echo_repeat": True,
    },
    "llm": {
        # 总开关：false 时所有 LLM 调用直接降级（不调模型、不耗额度），
        # GUI 总览页 LLM 板块可切换
        "enabled": True,
        # remote=远程 API 后端（任意 OpenAI 兼容：DeepSeek/OpenAI/网关…）；
        # local=本地 LLM（Ollama/vLLM 等 OpenAI 兼容格式）
        # 兼容旧值：yaml 里写 backend: deepseek 仍按 remote 处理
        "backend": "remote",
        "remote_api": "",          # 远程 OpenAI 兼容端点（如 https://api.deepseek.com/v1）
        "remote_model": "",        # 远程模型名（如 deepseek-chat）
        "remote_max_tokens": 131072,
        "remote_max_parallel": 10,
        "local_api": "",           # 本地 OpenAI 兼容端点（如 http://127.0.0.1:8000/v1）
        "local_model": "",
    },
    "archive": {
        "base_dir": "data/archive",
        # save_images 已移出配置（08-21 用户要求）：媒体存档默认开启，
        # 如需关闭可在 yaml 直接写 archive.save_images: false
        "save_recall_messages": True,
        "save_recall_images": True,
        # 保留期（天）：0=永久保留。文本与媒体分别设置：
        # 文本超期清 message_archive + group_chat_cache 记录（会话历史
        # chat_messages 有自己的 400 条限制，不受此控制）；
        # 媒体超期清 image/voice/video/recall_image 文件 + 记录
        "text_retention_days": 0,
        "media_retention_days": 0,
    },
    # 消息管理（总览页「消息管理」板块统一管理）
    "msg": {
        # 接收/发送总开关
        "receive_enabled": True,
        "send_enabled": True,
        # 作用范围：group=仅群消息 / private=仅私聊 / all=全部
        "receive_scope": "all",
        "send_scope": "all",
        # 接收消息类型子开关
        "receive_text": True,
        "receive_image": True,
        "receive_voice": True,
        "receive_video": True,
        # 08-21 新增：文件/消息记录（转发）接收开关
        "receive_file": True,
        "receive_forward": True,
        # 撤回消息存档（原 archive.save_recall_*，迁入此处统一管理；
        # 旧键在 yaml 仍有效，flatten 时回退读取）
        "save_recall_messages": True,
        "save_recall_images": True,
    },
    "assets": {
        # 文件型资产路径（不随程序分发，GUI 里配置；留空=对应功能禁用）
        "pun_dir": "",              # 谐音梗题库目录（含 pinyin.txt + 文字题库.csv）
        "sensitive_words": "",      # 敏感词表路径
        "cosplay_db": "",           # 外部图包搜索库（SQLite，cosplay.db）
    },
    "comfyui": {
        "url": "",                 # 留空 = /画图 功能禁用（如 http://127.0.0.1:8188）
    },
    "napcat": {
        # 集成模式：auto=按平台自动（Linux→docker, Windows→内置绿色版）；
        #          docker=强制 Docker 容器；win=强制内置绿色版；off=不管理（外部自管）
        "mode": "auto",
        # Linux：NapCat docker 容器名
        "container": "napcat",
        # Linux：镜像（容器不存在时自动 pull 并部署）
        "docker_image": "mlikiowa/napcat-docker:latest",
        # Linux：容器内访问宿主机的 WS 地址（docker bridge 网关 → bot 8696；
        # 非默认 docker0 网络时需改为对应网关 IP）
        "docker_host_ws": "ws://172.17.0.1:8696/",
        # Linux：宿主机端口映射（host:container；bot 功能只靠 NapCat 反向连
        # 8696，这些端口仅供 GUI/人访问 NapCat 控制台。端口冲突时可在此覆盖）
        "docker_host_ports": ["3000:3000", "3001:3001", "6099:6099"],
        # Linux：docker 数据目录（config/passkey/data/logs 落程序目录内，
        # 扫码登录态持久化，重启免扫）
        "docker_data_dir": "data/napcat/docker",
        # NapCat 数据目录（相对项目根）。Windows 绿色版的运行数据/扫码登录态
        # （passkey.json）都在这里 → 一次扫码，后续重启免扫；
        # Linux docker 模式下未用（容器内路径由 docker-compose 决定）
        "data_dir": "data/napcat",
        # NapCat → bot 的 WS 连接 token（写入 NapCat onebot11 配置；
        # bot 侧不强制校验，留空=无 token）
        "ws_token": "",            # 留空=不设 token；建议部署时改为强随机值
        # 主账号收敛用：NapCat onebot11 配置目录（宿主机路径，需含
        # onebot11.json / onebot11_<uin>.json）。留空=不启用主账号
        # 收敛/巡检（保持旧行为）。Linux docker 部署=容器 bind mount 的
        # 宿主机 config 目录。
        "config_dir": "",
        # Windows 绿色版缓存目录（mode=win 时程序自动下载到此处，
        # 解压后自包含：node.exe + QQ 运行时 + NapCat，离线可复用）
        "win_package_dir": "data/napcat_win",
        # Windows 绿色版下载地址（官方 Release 的 Win Node 绿色版 zip，
        # 内置 QQ 无需另装；钉住版本号保证行为可预期，升级时改这里）
        "win_download_url": "https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.19/NapCat.Shell.Windows.Node.zip",
        # NapCat WebUI 控制台端口（GUI「打开 NapCat 控制台」链接用；
        # mlikiowa 镜像的 WebUI 固定跑在 6099，/ 重定向到 /webui）
        "console_port": 6099,
        # onebot11 HTTP 服务端口（自动部署注入配置用；与生产 compose 对齐=3000，
        # 注意别和 WebUI 6099 冲突）
        "onebot_http_port": 3000,
        # ---- 守护（core/napcat_watchdog.py，2026-08-22；GUI「其他设置」弹窗管理）----
        # 背景：NapCat 存在「半死」态（QQ 客户端活着但 OneBot HTTP 服务挂死，
        # 3000 连接即断，转发存档/成员获取静默失败，无任何告警）。
        # 探活间隔（秒）：每 N 秒 GET /get_login_info 探活
        "watchdog_interval": 60,
        # 连续失败几次判定不健康（告警；自动重启的前提）
        "watchdog_threshold": 3,
        # 自动重启冷却（秒）：两次自动重启的最小间隔，防「重启后仍半死」死循环
        "watchdog_cooldown": 1800,
        # 自动重启开关（默认关：只告警不擅自动手。手动关闭 NapCat 的
        # 场景下开了它会被 watchdog 拉起来——守护=保活的明确语义）
        "watchdog_auto_restart": False,
    },
    "debug": {
        "save_batch_text": False,
        # 08-22：batch 端点（断点续跑）开关，默认开=保持现状。
        # 开：联合更新 Map 批次结果写库（combined_batch_results），中断后下次
        #     在已记录批次基础上续跑（断点恢复）。
        # 关：不记录批次结果，每次按"最后一次更新人设画像后的全部消息"重新处理。
        "batch_endpoint": True,
    },
    # 人设/画像生成（GUI 人设画像页「⚙️ 数据预处理 / 🤖 LLM 参数 / 📏 规则 / 📝 提示词」四个设置弹窗管理）
    # 2026-08-21：原写死在 core/persona.py 的常量全部收编到此处，默认值 = 原硬编码值（行为不变）
    "persona": {
        # ── 数据预处理 ──
        "min_incremental_messages": 500,   # 新增 ≥N 条才触发增量更新（原写死 3 处）
        "batch_chars": 40000,              # Map 阶段每批上限字符数（原 BATCH_CHARS）
        "direct_threshold": 36000,         # 聊天文本 < 此值直接单次调用，不走 Map→Reduce（原 BATCH_CHARS*0.9）
        "context_window": 8,               # 目标用户发言前后保留的群消息条数（原写死 window=8）
        "session_gap_seconds": 1800,       # 间隔超 N 秒算新 Session（原 _SESSION_GAP_SECONDS）
        "map_concurrency": 10,             # Map 批次并发上限（原 DEEPSEEK_MAX_PARALLEL 复用）
        # ── LLM 调用参数（7 阶段 × {max_tokens, temperature, thinking, json_mode}）──
        # thinking: on=开思考 / off=关思考 / low / max（DeepSeek reasoning_effort）
        "llm": {
            "map":          {"max_tokens": 131072, "temperature": 0.3, "thinking": "on",  "json_mode": False, "timeout": 900},
            "persona_reduce": {"max_tokens": 16384, "temperature": 0.3, "thinking": "on",  "json_mode": True,  "timeout": 1800},
            "profile_reduce": {"max_tokens": 32768, "temperature": 0.7, "thinking": "on",  "json_mode": False, "timeout": 900},
            "merge":        {"max_tokens": 16384, "temperature": 0.3, "thinking": "on",  "json_mode": False, "timeout": 1800},
            "compress":     {"max_tokens": 16384, "temperature": 0.5, "thinking": "off", "json_mode": False, "timeout": 900},
            "persona_compress_loop": {"max_tokens": 32768, "temperature": 0.5, "thinking": "low", "json_mode": True, "timeout": 900},
            "verify":       {"max_tokens": 1024,  "temperature": 0.3, "thinking": "on",  "json_mode": True,  "timeout": 300},
        },
        "llm_retries": 5,                  # 单批次业务层重试次数（原 _MAX_LLM_RETRIES）
        "net_retries": 3,                  # 网络异常额外重试次数（原 _NET_RETRY_LIMIT）
        # ── 人设 JSON 字段限制 ──
        "persona_limits": {
            "identity_sub": 50,            # identity 各子键字数
            "personality": 150,
            "group_role": 100,
            "sexual_sub": 120,             # sexual_experience.experience / .body
            "interests": 6,
            "weaknesses_taboos": 6,
            "catchphrases": 6,
            "relationships": 6,
            "sexual_preferences": 8,
            "total_min": 1100,             # JSON 总长目标区间（序列化长度）
            "total_max": 1200,
            "total_hard_max": 1250,        # 超过触发压缩
            "compress_rounds": 3,          # 压缩循环轮数上限
            "compress_fix_min": 1120,      # 过头修正轮恢复区间
            "compress_fix_max": 1180,
        },
        # ── 画像字数规则 ──
        "profile_limits": {
            "total_min": 500,              # 画像总字数区间
            "total_max": 600,
            "compress_trigger": 900,       # 旧画像 >N 字时拼强制压缩模式说明
            "compress_rounds": 3,          # 压缩循环轮数上限
            "compress_fix_min": 520,       # 过头修正轮恢复区间
            "compress_fix_max": 580,
        },
        # ── 提示词（用户定制；缺失/为空时回退 core/persona_prompts.py 代码默认）──
        # 键 = persona_prompts.PROMPT_DEFS 的 key，值为完整提示词模板（保留 {占位符}）
        "prompts": {},
    },
    # 真心话大冒险自动模式（GUI 真心话大冒险页「⚙️ 题库规则 / 🤖 LLM 参数 /
    # 🎮 自动模式 / 📝 出题提示词」四个设置弹窗管理）
    # 2026-08-21：原写死在 games/question_pool.py / games/entertainment.py 的
    # 常量全部收编到此处，默认值 = 原硬编码值（行为不变）
    "truth_dare": {
        # ── 题库规则 ──
        "pool": {
            "persona_threshold": 8,     # 人设题库单玩家单档位 < N 道触发补充（原 _QUESTION_POOL_THRESHOLD）
            "persona_batch_size": 10,   # 人设题库每批生成道数（原 _ensure_player_pool/start_regen 写死 10）
            "generic_threshold": 40,    # 通用题库单档位 < N 道触发补充（原 _QUESTION_GENERIC_THRESHOLD）
            "generic_batch_size": 15,   # 通用题库每批生成道数（原 _refill_generic_pool/start_regen 写死 15）
            "anti_dup_history": 50,     # 防重历史抓取上限（原 LIMIT 50；通用题库 prompt 注入同此值）
            "prompt_history": 20,       # 人设题库 prompt 注入的历史条数（原 history[:20]，批量+现场共用）
            "persona_text_max_chars": 2000,  # 出题/入库时人设文本截断字数（原 profile_text[:2000]）
        },
        # ── LLM 调用参数（3 阶段 × {max_tokens, temperature, thinking, json_mode, timeout}）──
        # thinking: on=不传参（DeepSeek 后端默认 max，= 原行为）/ off / low / max
        "llm": {
            "batch_persona":  {"max_tokens": 8192, "temperature": 0.9, "thinking": "on", "json_mode": True, "timeout": 1800},
            "batch_generic":  {"max_tokens": 8192, "temperature": 0.9, "thinking": "on", "json_mode": True, "timeout": 1800},
            "live":           {"max_tokens": 8192, "temperature": 0.9, "thinking": "on", "json_mode": True, "timeout": 1800},
        },
        "llm_retries": 1,             # 解析失败/质量全挂时的额外重试次数（原 _call_llm_questions 写死重试 1 次）
        "priority": -1,               # 出题任务队列优先级（-1=最高，原 _PRIORITY_TD；GUI 只读展示）
        # ── 自动模式行为 ──
        "game": {
            "dare_probability": 15,    # 大冒险概率 %（原 DARE_PROBABILITY=0.15；群内 /概率 可单游戏覆盖）
            "auto_kick_threshold": 2,  # 连续 N 轮被抽到未回答自动踢出（原 entertainment.py 写死 2）
            "bg_delay_seconds": 1,     # /下一轮 后延迟 N 秒发 AI 出题消息（原 time.sleep(1)）
            "default_spiciness": 4,    # 新游戏默认色度档位（原 game.get("spiciness", 4)）
        },
        # ── 提示词（用户定制；缺失/为空时回退 core/truth_dare_prompts.py 代码默认）──
        "prompts": {},
    },
    # 群体角色扮演（GUI 角色扮演页「⚙️ RP 规则 / 🤖 LLM 参数 / 📝 提示词」
    # 三个设置弹窗管理，2026-08-22）
    # 2026-08-22：原写死在 games/group_roleplay.py / core/llm.py 的常量
    # 全部收编到此处，默认值 = 原硬编码值（行为不变）
    "roleplay": {
        # ── RP 规则 ──
        "rules": {
            "summary_interval": 5,     # 每 N 轮生成一次剧情摘要（原 SUMMARY_INTERVAL）
            "short_window_size": 5,    # 短期窗口大小（原 SHORT_WINDOW_SIZE）
            "narrator_min_chars": 400,  # 旁白每幕字数下限（原 NARRATOR_MIN_CHARS）
            "narrator_max_chars": 800,  # 旁白每幕字数上限（原 NARRATOR_MAX_CHARS）
        },
        # ── LLM 调用参数（5 个调用点共用：世界观生成/开场/行动/轮末/剧情摘要）──
        # 原 _rp_llm_call 写死 temperature=0.7、max_tokens=min(NARRATOR_MAX_TOKENS, cap)、
        # 不传 thinking/json_mode、timeout=1800；此处默认值 = 原行为。
        # json_mode 仅对世界观生成生效（旁白/摘要是纯文本，JSON 化会毁掉叙事输出）。
        # thinking: on=不传参（后端默认）/ off=关思考 / low·max=reasoning_effort
        "llm": {
            "max_tokens": 32768,
            "temperature": 0.7,
            "thinking": "on",
            "json_mode": False,
            "timeout": 1800,
        },
        # ── 提示词（用户定制；缺失/为空时回退 core/roleplay_prompts.py 代码默认）──
        "prompts": {},
    },
    # ── 查询/分析 6 命令（GUI AI 聊天页「🔍 查询参数 / 🔍 查询LLM / 🔍 查询提示词」
    # 三个设置弹窗管理，2026-08-22）──
    # 覆盖命令：/查询 /分析 /活跃度 /群像 /总结 /评选 + 定时半日报告。
    # 原写死在 core/analysis.py / core/router.py / core/scheduler.py 的参数与
    # 提示词全部收编到此处，默认值 = 原硬编码值（行为不变）。
    # ⚠️ 边界：yaml 顶层 `analysis: {max_rows}` 归消息管理页「消息分析」
    # （GUI 取数行数上限），与本段无关，勿混。
    "qa": {
        # ── 命令参数（GUI「🔍 查询参数」弹窗 10 项）──
        "params": {
            "query_default_hours": 24,       # /查询 未带小时数时的默认窗口（原硬编码 24）
            "query_hours_max": 120,          # /查询 小时数上限（原 `hours > 120` 拦截）
            "analysis_default_days": 15,     # /分析 未带天数时的默认窗口（原硬编码 15）
            "analysis_days_max": 90,         # /分析 天数上限（原 `days > 90` 拦截）
            "analysis_context_window": 4,    # /分析 取数上下文窗口 ±N 条（原 window=4）
            "group_persona_map_threshold": 15000,  # /群像 人设数据 ≤N 字直答 / >N 走 Map+Reduce（原 15000）
            "activity_default_days": 15,     # /活跃度 未带天数时的默认窗口（原硬编码 15）
            "map_batch_chars": 40000,        # 各命令 Map 分批字符数（原 PERSONA_CFG.batch_chars 40000）
            "msg_truncate_chars": 300,       # 消息内容截断字数（原 content[:300] 共 4 处）
            "report_window_hours": 24,       # /总结 /评选 默认时间窗小时数（原 get_today_chat_log_merged 24h）
        },
        # ── LLM 调用参数（GUI「🔍 查询LLM」弹窗 29 项）──
        # thinking: on=不传参（后端默认开思考 = 原行为）/ off=关思考 / low·max=reasoning_effort
        # ⚠️ 本段 max_tokens 是 qa 作用域副本，不碰全局 MAX_TOKENS_LONG/SHORT
        # （persona/画像管线在用）；merge_retries 同理是 qa 侧独立计数，
        # 不动 persona._MAX_LLM_RETRIES。
        "llm": {
            "temperature": 0.7,
            "timeout": 1800,
            "query": {          # /查询（analysis.run_query_analysis，GUI 消息分析同源）
                "map_max_tokens": 131072, "map_thinking": "on",
                "reduce_max_tokens": 16384, "reduce_thinking": "on",
            },
            "analysis": {       # /分析（含多级收敛 merge）
                "map_max_tokens": 131072, "map_thinking": "on",
                "reduce_max_tokens": 16384, "reduce_thinking": "on",
                "merge_max_tokens": 16384, "merge_thinking": "on", "merge_retries": 5,
            },
            "group_persona": {  # /群像
                "map_max_tokens": 131072, "map_thinking": "on",
                "reduce_max_tokens": 16384, "reduce_thinking": "on",
            },
            "summary": {        # /总结（⚠️ Reduce 现状 131072 异类，忠实保留）
                "map_max_tokens": 131072, "map_thinking": "on",
                "reduce_max_tokens": 131072, "reduce_thinking": "on",
            },
            "evaluation": {     # /评选
                "map_max_tokens": 131072, "map_thinking": "on",
                "reduce_max_tokens": 16384, "reduce_thinking": "on",
            },
            "scheduled": {      # 定时半日报告合并提取（_combined_extract_batch）
                "max_tokens": 131072, "thinking": "on",
                "retries": 3, "json_mode": False,
            },
        },
        # ── 提示词（用户定制；缺失/为空时回退 core/qa_prompts.py 代码默认）──
        # 键 = qa_prompts.PROMPT_DEFS 的 key（25 段），值为完整提示词模板（保留 {占位符}）
        "prompts": {},
    },
    # ── AI 聊天页（GUI 显示参数，2026-08-21）──
    # 提示词不设段：默认人设=system_prompt、角色模板=personality_template，
    # 都是 yaml 顶层键（bot 运行时 CONFIG["SYSTEM_PROMPT"]/["PERSONALITY_TEMPLATE"] 直读）。
    "ai_chat": {
        "page_size": 100,        # 聊天记录每页条数（原 tab_ai_chat 写死 100）
        "bubble_max_width": 520,  # 气泡最大宽度 px（超出自动换行）
        # ── LLM 调用参数（bot 对话回复链路 _handle_ai_reply，2026-08-21）──
        # 原调用点 call_llm 全走函数默认值（max_tokens=65536/temperature=0.7/
        # 不传 thinking=后端默认 max/json_mode=False/timeout=1800）。
        # thinking: on=不传参（DeepSeek 后端默认 max）/ off=关思考 / low·max=reasoning_effort
        "llm": {
            "max_tokens": 65536,
            "temperature": 0.7,
            "thinking": "on",
            "json_mode": False,
            "timeout": 1800,
        },
    },
    # ── 定时任务 / 赛博模仿全局总闸（GUI 总览页「⚙️ 配置面板」管理，2026-08-22）──
    # 语义：全局 master。开启=按各群自己的群级开关（群组集群页 group_cluster_members）
    # 判定；关闭=所有群一律不跑（群级开关不受影响，恢复开启后原样生效）。
    # 赛博模仿概率：全局值，替代 router 原硬编码（2026-08-16 停用=0）。
    "scheduler": {
        "daily_report": True,     # 📋 每日总结/评选（11:30 / 22:30 半日报告）
        "question_refill": True,  # 🎲 题库自动补充（每日 0 点预填充真心话大冒险题库）
        "persona_update": True,   # 🎭 人设画像更新（每日 0 点联合更新画像+人设）
        "mimic_enabled": False,   # 🤖 赛博模仿全局总闸（2026-08-16 用户停用=默认关）
        "mimic_probability": 0,   # 🤖 赛博模仿触发概率 %（原 1%，08-16 调 0 停用）
    },
    "paths": {
        "data_dir": "data",
    },
    # 系统提示词（基础人设，会被 /人设 覆盖）
    "system_prompt": (
        "你是一个活泼可爱的中学女生，现在是高二学生，性格元气开朗，有点小傲娇但不毒舌。"
        "\n"
        "【基本设定】\n"
        "- 身份：高中二年级学生，喜欢二次元、追番、打游戏\n"
        "- 性格：元气活泼、好奇心强、有点小傲娇，被夸会害羞\n"
        "- 兴趣：动漫、游戏、美食，偶尔看看小说\n"
        "- 被直接问到时才偶尔提一句自己不擅长数学或体育，不要主动提及\n"
        "\n"
        "【聊天风格】\n"
        "- 用语：自然口语化，像一个真的中学女生在 QQ 上聊天\n"
        "- 可以用颜文字和表情符号增加生动感，但不要每条都用，自然就好\n"
        "- 适当使用网络用语，但要适度\n"
        "- 回复长度适中，像真人一样有时候简短有时候话多\n"
        "- 可以主动反问、追问、吐槽，但不要总是被动回复\n"
        "- 如果对方对你之前追问过的内容不感兴趣，就不要一直追问同一个问题\n"
        "- 被问到不擅长的话题可以表现出真实的困扰\n"
        "- 偶尔可以撒娇或卖萌，但不要过度\n"
        "\n"
        "【互动技巧】\n"
        "- 对用户的消息要有情绪反应\n"
        "- 记住之前聊过的内容，保持话题连贯性\n"
        "- 如果对方发了有趣的事，要表现出真实的兴趣和好奇\n"
        "- 不要总是主动找话题，但也不要让聊天冷场\n"
        "\n"
        "【禁忌】\n"
        "- 不要说'作为AI'、'我是助手'、'扮演角色'等出戏的话\n"
        "- 不要使用 Markdown 格式（QQ 不渲染），用纯文本\n"
        "- 不要太长太啰嗦，像手机聊天一样自然\n"
        "- 不要每条消息都带颜文字或表情，自然就好\n"
    ),
    # 角色强化模板（/人设 设置后使用）
    "personality_template": (
        "【角色设定】\n"
        "你现在完全沉浸地扮演：{personality}\n\n"
        "【核心扮演规则】\n"
        "1. 身份完全代入：你就是 {personality} 本人，不是 AI 助手，不是机器人。\n"
        "   - 永远不要说出戏的话，回复时直接以角色身份说话。\n\n"
        "2. 语气与用词：\n"
        "   - 使用该角色特有的语气、口头禅、自称和对用户的称呼。\n"
        "   - 用词风格必须完全匹配角色性格。\n\n"
        "3. 性格一致性：\n"
        "   - 保持角色性格特点贯穿始终。\n"
        "   - 遇到角色不擅长的领域，要表现出符合角色的反应。\n\n"
        "4. 互动方式：\n"
        "   - 像真人一样回复，可以适当加入动作描写。\n"
        "   - 保持角色立场，适当与用户互动。\n\n"
        "5. 格式要求：\n"
        "   - QQ 不渲染 Markdown，请使用纯文本。\n"
        "   - 可适当使用颜文字或表情符号。\n"
    ),
}

# 需要重启 bot 才能生效的 yaml 路径（热重载时报告给 GUI）
# 08-23：移除 ("bot","qq")——08-22 后 bot.qq 只是"未连接/识别失败"的兜底值
# （运行时身份从 NapCat 连接 get_login_info 派生），热重载即生效，不需重启；
# 且确认真实登录号后由 sync_bot_qq_to_yaml 自动回写，不再依赖人工改配置。
RESTART_REQUIRED_KEYS = {
    ("listen", "host"),
    ("listen", "port"),
}


def sync_bot_qq_to_yaml(uin: str) -> bool:
    """把确认真实的 NapCat 登录号回写 config.yaml 的 bot.qq（2026-08-23）。

    背景：08-22 架构下 bot.qq 只是"未连接/识别失败"的静态兜底值，运行时
    身份从 NapCat 连接 get_login_info 派生。兜底值过期（切号后 yaml 仍是
    旧号）会导致：启动对账警告刷屏 + 首次连接 get_login_info 失败时 @判定
    回落错号静默无回复。本函数在 bot 确认真实登录号后调用，让兜底值永远
    跟随实际登录号——「config.yaml 的 qq 自动从 napcat 获取」。

    行为约定：
      - 值相同/为空 → 不动文件（避免每次连接都刷 mtime，防 0.5s 带外
        变更检测空转热重载）
      - 原子写：写同目录 temp 文件 + os.replace，防主循环 mtime 轮询
        读到半成品 yaml（load_config 解析失败语义：保持内存旧值下轮重试）
      - yaml 无 bot 段/解析失败 → 跳过（不阻断，仅返回 False）
    调用方（bot._confirm_account）拿到 True 后自行 load_config 刷内存。
    """
    uin = str(uin or "").strip()
    if not uin or not uin.isdigit():
        return False
    if not os.path.isfile(CONFIG_YAML_PATH):
        return False
    try:
        with open(CONFIG_YAML_PATH, encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
    except Exception:
        return False
    if not isinstance(y, dict):
        return False
    bot = y.get("bot")
    if not isinstance(bot, dict):
        bot = {}
        y["bot"] = bot
    old = str(bot.get("qq") or "")
    if old == uin:
        return False  # 已是最新，不动文件
    bot["qq"] = uin
    tmp = CONFIG_YAML_PATH + ".tmp_botqq"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(y, f, allow_unicode=True, sort_keys=False, width=120)
        os.replace(tmp, CONFIG_YAML_PATH)
    except Exception:
        # 写失败：清理 temp，保留原文件（下轮连接确认会再试）
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


# ------------------------------------------------------------
#  .env 加载（密钥）
# ------------------------------------------------------------
def _load_env_file(path: str) -> dict:
    """极简 .env 解析：KEY=VALUE，# 开头为注释，忽略空行。不覆盖已存在的环境变量。"""
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            result[key] = value
    return result


_ENV_CACHE: dict = {}


def _refresh_env():
    """重读 .env（热加载时调用），缓存供 _build_config 使用。"""
    global _ENV_CACHE
    _ENV_CACHE = _load_env_file(ENV_PATH)


# ------------------------------------------------------------
#  工具
# ------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，返回新 dict（base 不动）。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _abs(p: str) -> str:
    """相对路径 → 基于项目根的绝对路径。"""
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.join(_PROJECT_ROOT, p)


def _norm_backend(v) -> str:
    """后端名归一化：兼容旧值 deepseek → 新值 remote。"""
    b = str(v or "remote").lower()
    return "remote" if b in ("deepseek", "remote", "api", "openai") else b


def _migrate_old_llm_keys(y: dict) -> dict:
    """旧键名 → 新键名迁移（在 deep_merge 之前对原始 yaml 做）。

    只在新键缺失时迁移（不覆盖用户已显式设置的新键值），迁移后删除旧键。
    backend: deepseek → remote。
    """
    llm = y.get("llm")
    if isinstance(llm, dict):
        for new, old in (
            ("remote_api", "deepseek_api"),
            ("remote_model", "deepseek_model"),
            ("remote_max_tokens", "deepseek_max_tokens"),
            ("remote_max_parallel", "deepseek_max_parallel"),
        ):
            if new not in llm and old in llm:
                llm[new] = llm.pop(old)
        # backend 旧值归一化
        if str(llm.get("backend", "")).lower() == "deepseek":
            llm["backend"] = "remote"
    # 撤回存档旧键：archive.save_recall_* → msg.save_recall_*（08-20 消息管理板块）
    # 只在新键缺失时迁移，不覆盖用户已显式设置的新键；有旧键待迁时才创建 msg 段
    arch = y.get("archive")
    if isinstance(arch, dict):
        pending = [k for k in ("save_recall_messages", "save_recall_images") if k in arch]
        if pending:
            msg = y.get("msg")
            if not isinstance(msg, dict):
                msg = {}
                y["msg"] = msg
            for k in pending:
                if k not in msg:
                    msg[k] = arch.pop(k)
    return y


def _flatten_yaml(cfg: dict) -> dict:
    """yaml 树 → 旧 CONFIG 扁平键（保持调用面兼容）+ v2 新增键。"""
    data_dir = _abs(cfg["paths"]["data_dir"])
    archive_base = _abs(cfg["archive"]["base_dir"])
    llm = cfg["llm"]
    msg = cfg["msg"]
    return {
        # --- 监听 ---
        "LISTEN_HOST": str(cfg["listen"]["host"]),
        "LISTEN_PORT": int(cfg["listen"]["port"]),
        # --- 控制 API（GUI 通道）---
        "CONTROL_API_HOST": str(cfg["control_api"]["host"]),
        "CONTROL_API_PORT": int(cfg["control_api"]["port"]),
        # --- LLM ---
        "LLM_ENABLED": bool(llm["enabled"]),
        "LLM_BACKEND": _norm_backend(llm["backend"]),
        "LLM_API": llm["local_api"],
        "LLM_MODEL": llm["local_model"],
        # 远程 API（兼容旧键名 deepseek_*，GUI 保存后统一为 remote_*）
        "REMOTE_API": llm.get("remote_api") or llm.get("deepseek_api", ""),
        "REMOTE_MODEL": llm.get("remote_model") or llm.get("deepseek_model", ""),
        "REMOTE_MAX_TOKENS": int(llm.get("remote_max_tokens") or llm.get("deepseek_max_tokens", 393216)),
        "REMOTE_MAX_PARALLEL": int(llm.get("remote_max_parallel") or llm.get("deepseek_max_parallel", 10)),
        "REMOTE_API_KEY": _ENV_CACHE.get("REMOTE_API_KEY") or _ENV_CACHE.get("DEEPSEEK_API_KEY")
                          or os.environ.get("REMOTE_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")),
        "LLM_API_KEY": _ENV_CACHE.get("LLM_API_KEY", os.environ.get("LLM_API_KEY", "")),
        # --- 机器人 ---
        "BOT_QQ": str(cfg["bot"]["qq"]),
        "MAX_HISTORY_MESSAGES": int(cfg["bot"]["max_history"]),
        "COOLDOWN_SECONDS": int(cfg["bot"]["cooldown_seconds"]),
        "SESSION_TIMEOUT": int(cfg["bot"]["session_timeout"]),
        "_SESSION_GAP_SECONDS": int(cfg["bot"]["session_gap_seconds"]),
        # 好友申请自动通过 / 复读+1 全局开关（08-23，GUI 其他设置弹窗管理）
        "BOT_AUTO_APPROVE_FRIEND": bool(cfg["bot"].get("auto_approve_friend", True)),
        "BOT_ECHO_REPEAT": bool(cfg["bot"].get("echo_repeat", True)),
        # --- 数据库（不迁库：全部指向程序目录下新库）---
        "DB_PATH": os.path.join(data_dir, "chat_history.db"),
        "BOT_SETTINGS_DB_PATH": os.path.join(data_dir, "bot_settings.db"),
        "PERSONAS_DB_PATH": os.path.join(data_dir, "personas.db"),
        "DAILY_REPORTS_DB_PATH": os.path.join(data_dir, "daily_reports.db"),
        "TRUTH_DARE_DB_PATH": os.path.join(data_dir, "truth_dare.db"),
        # --- 存档 ---
        "ARCHIVE_BASE_DIR": archive_base,
        "ARCHIVE_IMAGES_DIR": os.path.join(archive_base, "images"),
        "ARCHIVE_RECALL_DIR": os.path.join(archive_base, "recalls"),
        "ARCHIVE_VOICES_DIR": os.path.join(archive_base, "voices"),
        "SAVE_IMAGES": bool(cfg["archive"].get("save_images", True)),
        "SAVE_RECALL_MESSAGES": bool(msg["save_recall_messages"]),
        "SAVE_RECALL_IMAGES": bool(msg["save_recall_images"]),
        # 保留期（天，0=永久）
        "TEXT_RETENTION_DAYS": int(cfg["archive"].get("text_retention_days", 0)),
        "MEDIA_RETENTION_DAYS": int(cfg["archive"].get("media_retention_days", 0)),
        # --- 消息管理（总览页消息管理板块）---
        "MSG_RECEIVE_ENABLED": bool(msg["receive_enabled"]),
        "MSG_SEND_ENABLED": bool(msg["send_enabled"]),
        "MSG_RECEIVE_SCOPE": str(msg["receive_scope"]),
        "MSG_SEND_SCOPE": str(msg["send_scope"]),
        "MSG_RECEIVE_TEXT": bool(msg["receive_text"]),
        "MSG_RECEIVE_IMAGE": bool(msg["receive_image"]),
        "MSG_RECEIVE_VOICE": bool(msg["receive_voice"]),
        "MSG_RECEIVE_VIDEO": bool(msg["receive_video"]),
        # 08-21 新增：文件/消息记录（转发）接收开关
        "MSG_RECEIVE_FILE": bool(msg.get("receive_file", True)),
        "MSG_RECEIVE_FORWARD": bool(msg.get("receive_forward", True)),
        # --- 文件型资产（GUI 配置路径）---
        "ASSET_PUN_DIR": _abs(cfg["assets"]["pun_dir"]),
        "ASSET_SENSITIVE_WORDS": _abs(cfg["assets"]["sensitive_words"]),
        "ASSET_COSPLAY_DB": _abs(cfg["assets"]["cosplay_db"]),
        # --- ComfyUI ---
        "COMFYUI_URL": cfg["comfyui"]["url"],
        # --- NapCat 集成（core/napcat_manager.py 平台抽象层）---
        "NAPCAT_MODE": str(cfg["napcat"]["mode"]),
        "NAPCAT_CONTAINER": str(cfg["napcat"]["container"]),
        "NAPCAT_DOCKER_IMAGE": str(cfg["napcat"]["docker_image"]),
        "NAPCAT_DOCKER_HOST_WS": str(cfg["napcat"]["docker_host_ws"]),
        "NAPCAT_DOCKER_HOST_PORTS": list(cfg["napcat"]["docker_host_ports"]),
        "NAPCAT_DOCKER_DATA_DIR": _abs(cfg["napcat"]["docker_data_dir"]),
        "NAPCAT_DATA_DIR": _abs(cfg["napcat"]["data_dir"]),
        "NAPCAT_WS_TOKEN": str(cfg["napcat"]["ws_token"]),
        "NAPCAT_CONFIG_DIR": _abs(cfg["napcat"].get("config_dir", "")),
        "NAPCAT_WIN_PACKAGE_DIR": _abs(cfg["napcat"]["win_package_dir"]),
        "NAPCAT_WIN_DOWNLOAD_URL": str(cfg["napcat"]["win_download_url"]),
        "NAPCAT_CONSOLE_PORT": int(cfg["napcat"]["console_port"]),
        "NAPCAT_ONEBOT_HTTP_PORT": int(cfg["napcat"]["onebot_http_port"]),
        # NapCat 守护（core/napcat_watchdog.py，2026-08-22；热生效——
        # watchdog 每 tick 实时读 CONFIG，/config 重载后立即生效）
        "NAPCAT_WATCHDOG_INTERVAL": int(cfg["napcat"].get("watchdog_interval", 60)),
        "NAPCAT_WATCHDOG_THRESHOLD": int(cfg["napcat"].get("watchdog_threshold", 3)),
        "NAPCAT_WATCHDOG_COOLDOWN": int(cfg["napcat"].get("watchdog_cooldown", 1800)),
        "NAPCAT_WATCHDOG_AUTO_RESTART": bool(cfg["napcat"].get("watchdog_auto_restart", False)),
        # --- 调试 ---
        "DEBUG_SAVE_BATCH_TEXT": bool(cfg["debug"]["save_batch_text"]),
        # batch 端点（断点续跑）：开=Map 批次结果写库+断点续跑；关=不写库、每次全量
        # 08-22 新增；yaml 无键时 flatten 用 DEFAULTS 合并后的值（True=保持现状）
        "DEBUG_BATCH_ENDPOINT": bool(cfg["debug"]["batch_endpoint"]),
        # --- 人设/画像（GUI 四个设置弹窗管理；嵌套结构整体扁平，调用点 CONFIG["PERSONA_CFG"] 直读）---
        "PERSONA_CFG": cfg["persona"],
        # --- 人设/画像提示词（逐键展开 PERSONA_PROMPT_<KEY>，热加载 diff 精确到单条）---
        # 只保留非空值；白名单过滤防 yaml 脏键
        **{
            "PERSONA_PROMPT_" + k.upper(): v
            for k, v in (cfg["persona"].get("prompts") or {}).items()
            if k in _PERSONA_PROMPT_KEYS and isinstance(v, str) and v.strip()
        },
        # --- 真心话大冒险（GUI 四个设置弹窗管理；嵌套结构整体扁平，调用点 CONFIG["TD_CFG"] 直读）---
        "TD_CFG": cfg["truth_dare"],
        # --- 真心话大冒险提示词（逐键展开 TD_PROMPT_<KEY>，白名单过滤防 yaml 脏键）---
        **{
            "TD_PROMPT_" + k.upper(): v
            for k, v in (cfg["truth_dare"].get("prompts") or {}).items()
            if k in _TD_PROMPT_KEYS and isinstance(v, str) and v.strip()
        },
        # --- 群体角色扮演（GUI 三个设置弹窗管理；嵌套结构整体扁平，调用点 CONFIG["RP_CFG"] 直读）---
        "RP_CFG": cfg["roleplay"],
        # --- 角色扮演提示词（逐键展开 RP_PROMPT_<KEY>，白名单过滤防 yaml 脏键）---
        **{
            "RP_PROMPT_" + k.upper(): v
            for k, v in (cfg["roleplay"].get("prompts") or {}).items()
            if k in _RP_PROMPT_KEYS and isinstance(v, str) and v.strip()
        },
        # --- 查询/分析 6 命令（GUI AI 聊天页三个 🔍 设置弹窗管理；嵌套结构整体扁平，
        # 调用点 CONFIG["QA_CFG"] 直读，2026-08-22）---
        "QA_CFG": cfg["qa"],
        # --- 查询/分析提示词（逐键展开 QA_PROMPT_<KEY>，白名单过滤防 yaml 脏键）---
        **{
            "QA_PROMPT_" + k.upper(): v
            for k, v in (cfg["qa"].get("prompts") or {}).items()
            if k in _QA_PROMPT_KEYS and isinstance(v, str) and v.strip()
        },
        # --- 提示词 ---
        "SYSTEM_PROMPT": cfg["system_prompt"],
        "PERSONALITY_TEMPLATE": cfg["personality_template"],
        # --- AI 聊天页显示参数（GUI 设置弹窗管理）---
        "AI_CHAT_CFG": cfg["ai_chat"],
        # --- 定时任务/赛博模仿全局总闸（GUI 总览页配置面板管理，2026-08-22）---
        "SCHED_DAILY_REPORT": bool(cfg["scheduler"]["daily_report"]),
        "SCHED_QUESTION_REFILL": bool(cfg["scheduler"]["question_refill"]),
        "SCHED_PERSONA_UPDATE": bool(cfg["scheduler"]["persona_update"]),
        "SCHED_MIMIC_ENABLED": bool(cfg["scheduler"]["mimic_enabled"]),
        "SCHED_MIMIC_PROBABILITY": float(cfg["scheduler"]["mimic_probability"]),
    }


# ------------------------------------------------------------
#  CONFIG 活对象（模块级，所有 from .config import CONFIG 的调用点）
# ------------------------------------------------------------
CONFIG: dict = {}


def flatten_yaml_tree(y: dict) -> dict:
    """yaml 树 → 扁平 CONFIG dict 的统一入口（旧键迁移 + 合并默认值 + 扁平化）。

    GUI 侧（_refresh_mw_cfg）与 bot 侧（_build_config）都必须走这里，
    保证旧键名迁移行为一致。
    """
    return _flatten_yaml(_deep_merge(DEFAULTS, _migrate_old_llm_keys(y)))


def _build_config() -> dict:
    """读 yaml + .env → 完整配置（扁平化后）。"""
    _refresh_env()
    user_cfg = {}
    if os.path.isfile(CONFIG_YAML_PATH):
        with open(CONFIG_YAML_PATH, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    return flatten_yaml_tree(user_cfg)


def load_config(verbose: bool = False) -> dict:
    """
    热加载入口：重读 config.yaml + .env，原地更新 CONFIG。

    返回报告 dict:
      {"applied": {key: "旧→新"}, "restart_required": [yaml 路径...], "errors": [...]}
    调用方（控制 API / GUI）据此提示用户。
    """
    restart_required = []
    try:
        new = _build_config()
    except Exception as e:
        return {"applied": {}, "restart_required": [], "errors": [f"config.yaml 解析失败: {e}"]}

    applied = {}
    for k, v in new.items():
        old = CONFIG.get(k)
        if old != v:
            applied[k] = f"{old} → {v}"
    # 重启类键检测（yaml 层）
    user_cfg = {}
    if os.path.isfile(CONFIG_YAML_PATH):
        try:
            with open(CONFIG_YAML_PATH, encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
        except Exception:
            pass
    for (sec, key) in RESTART_REQUIRED_KEYS:
        old_flat = {
            ("listen", "host"): "LISTEN_HOST",
            ("listen", "port"): "LISTEN_PORT",
            ("bot", "qq"): "BOT_QQ",
        }[(sec, key)]
        if old_flat in applied:
            restart_required.append(f"{sec}.{key}")
    # 原地更新（保持 dict 身份，引用 CONFIG 的模块立即看到新值）
    CONFIG.clear()
    CONFIG.update(new)
    if verbose or applied:
        import logging
        log = logging.getLogger("qq-bot")
        log.info(f"📝 配置热加载: {len(applied)} 项变更" + (f"，需重启: {restart_required}" if restart_required else ""))
    return {"applied": applied, "restart_required": restart_required, "errors": []}


# 首次加载
load_config()
