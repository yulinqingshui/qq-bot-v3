#!/usr/bin/env python3
"""
QQ 群机器人 - 分级帮助菜单
============================
一级菜单：/帮助 → 显示功能大类
二级菜单：/帮助 [大类名] → 显示该分类下的具体指令
"""

# ============================================================
#  功能指令清单注入（@bot 聊天 prompt 用，2026-08-12）
# ============================================================

# ---- 查询/分析 6 命令：desc 从 qa 段动态渲染（2026-08-22 配置化）----
# CATEGORIES 保持静态基线（默认参数 = 现状逐字）；渲染时按前缀匹配覆写 desc，
# 读 CONFIG 活对象 → 热重载后帮助文案/指令注入自动同步。
_QA_CMD_PREFIXES = ("/评选", "/总结", "/活跃度", "/查询", "/分析", "/群像")


def _qa_cmd_descs() -> dict:
    """6 条查询/分析命令的动态 desc（默认参数下与静态基线逐字一致）。

    动态项：query_default_hours / query_hours_max / analysis_default_days /
    analysis_days_max（示例天数随上限收敛）/ activity_default_days /
    report_window_hours（24h≈当日，保持原文案；非 24 时显示具体小时）。
    """
    from .qa_prompts import qa_params
    p = qa_params()
    qh = int(p.get("query_default_hours", 24))
    qmax = int(p.get("query_hours_max", 120))
    ad = int(p.get("analysis_default_days", 15))
    amax = int(p.get("analysis_days_max", 90))
    act = int(p.get("activity_default_days", 15))
    wh = int(p.get("report_window_hours", 24))
    # 24h 时保持原文案措辞（评选=「当日」/总结=「当天」，逐字零漂移）；
    # 非 24h 时两命令统一显示「近 N 小时」
    _eval_window = "当日" if wh == 24 else f"近 {wh} 小时"
    _sum_window = "当天" if wh == 24 else f"近 {wh} 小时"
    ex_days = min(60, amax)  # /分析 用法示例天数（随上限收敛，避免示例超界误导）
    return {
        "评选": f"分析{_eval_window}聊天记录，评选 5 大有趣栏目（仅管理员）",
        "总结": f"总结概括{_sum_window}的群聊聊天内容（仅管理员）",
        "活跃度": f"显示近 n 天群聊活跃度排行（默认{act}天，如 /活跃度7、/活跃度 7）",
        "查询": f"基于近 {qh} 小时聊天记录回答你的问题（可用 /查询{qmax} 最多查 {qmax // 24} 天）",
        "分析": f"分析指定用户聊天记录回答你的问题（含@/回复/上下文，默认近 {ad} 天，可用 /分析{ex_days} 查 {ex_days} 天）",
        "群像": "基于本群所有用户人设回答关于群友的问题（如群友喜好比例）",
    }


def _apply_qa_descs(cmds: list) -> list:
    """渲染期覆写 6 条命令 desc（qa 段热生效；非匹配项原样返回）。"""
    try:
        desc_map = _qa_cmd_descs()
    except Exception:
        return cmds  # qa 段异常时回退静态基线，帮助菜单永不炸
    out = []
    for c in cmds:
        matched = None
        for prefix in _QA_CMD_PREFIXES:
            if c.get("cmd", "").startswith(prefix):
                matched = prefix[1:]
                break
        if matched and matched in desc_map:
            out.append({**c, "desc": desc_map[matched]})
        else:
            out.append(c)
    return out


def build_command_injection_text() -> str:
    """生成功能指令清单文本（注入 @bot 聊天 system prompt）。

    用户向 bot 询问功能指令用法时，bot 能给出准确指令（不编造）。
    与 /帮助 数据同源（CATEGORIES），改动指令时自动同步。
    """
    lines = [
        "【机器人功能指令】",
        "用户可以向你询问以下指令的准确用法（如「怎么玩真心话」「如何看活跃度」），"
        "请直接回答对应指令和说明，不要编造不存在的指令：",
    ]
    for category, data in CATEGORIES.items():
        cmds = data.get("commands", [])
        if not cmds:
            continue
        lines.append(f"[{data.get('icon', '')}{category}]")
        for c in _apply_qa_descs(cmds):
            lines.append(f"- {c['cmd']}：{c['desc']}")
    return "\n".join(lines)


# ============================================================
#  一级分类定义
# ============================================================
CATEGORIES = {
    "角色扮演": {
        "icon": "🎭",
        "description": "群体角色扮演游戏 — 创建房间、报名角色、旁白驱动的互动故事",
        "commands": [
            {"cmd": "/开始扮演 [背景描述]", "desc": "创建新房间，自动生成世界观"},
            {"cmd": "/开始扮演剧情 [剧情文本]", "desc": "直接用你的剧情开局（跳过世界观生成，适合即兴/轻松日常）"},
            {"cmd": "/重新生成世界观 [新描述]", "desc": "重新生成世界观（仅创建者）"},
            {"cmd": "/报名 角色名:描述", "desc": "加入游戏，创建自己的角色"},
            {"cmd": "/退场", "desc": "退出当前游戏"},
            {"cmd": "/开演", "desc": "开始游戏（Bot 生成开场旁白）"},
            {"cmd": "/状态", "desc": "查看房间状态、玩家列表"},
            {"cmd": "/继续", "desc": "催促当前玩家行动"},
            {"cmd": "/结束", "desc": "结束当前游戏"},
            {"cmd": "/剧本", "desc": "查看剧情总结"},
        ],
    },
    "卧底": {
        "icon": "🕵️",
        "description": "谁是卧底推理游戏 — 找出隐藏在平民中的卧底",
        "commands": [
            {"cmd": "/卧底", "desc": "创建游戏房间"},
            {"cmd": "/卧底 白板", "desc": "创建白板模式（4 人即可）"},
            {"cmd": "/卧底加入", "desc": "加入游戏"},
            {"cmd": "/卧底开始", "desc": "开始游戏"},
            {"cmd": "/卧底状态", "desc": "查看游戏状态"},
            {"cmd": "/卧底结束", "desc": "结束游戏"},
            {"cmd": "/卧底时间 X分", "desc": "设置发言限时（如 /卧底时间 5分）"},
            {"cmd": "/卧底判定 开/关", "desc": "开启/关闭 LLM 语义判定（默认开启）"},
            {"cmd": "/卧底统计", "desc": "查看历史使用记录统计"},
            {"cmd": "/卧底重置题库", "desc": "清空历史使用记录"},
            {"cmd": "/卧底帮助", "desc": "查看卧底游戏详细帮助"},
        ],
    },
    "真心话大冒险": {
        "icon": "🎮",
        "description": "真心话/大冒险/混合模式 — 骰子决定谁提问、谁接受挑战",
        "commands": [
            {"cmd": "/真心话", "desc": "开始真心话游戏"},
            {"cmd": "/大冒险", "desc": "开始大冒险游戏"},
            {"cmd": "/真心话大冒险", "desc": "混合模式（随机真心话或大冒险）"},
            {"cmd": "/加入", "desc": "加入当前游戏"},
            {"cmd": "/退出", "desc": "退出当前游戏（不影响其他人）"},
            {"cmd": "/骰", "desc": "手动投骰子（决定谁问谁答）"},
            {"cmd": "/下一轮", "desc": "自动投骰 + 抽题，进入下一轮"},
            {"cmd": "/抽题", "desc": "重新抽取一道题目"},
            {"cmd": "/概率 [0-100]", "desc": "查看/修改大冒险概率"},
            {"cmd": "/骰数 [1-12]", "desc": "修改骰子数量（支持'自动'）"},
            {"cmd": "/点数 [6-12]", "desc": "设置自己的骰子最大点数（仅自己生效）"},
            {"cmd": "/简化模式", "desc": "精简回复（仅显示问题和@输家）"},
            {"cmd": "/自动模式", "desc": "AI 根据输家画像自动出题（投骰+出题全自动）"},
            {"cmd": "/色色程度 [0-6]", "desc": "调整 AI 出题尺度（0 清水 - 6 深渊，默认4）"},
            {"cmd": "/自选模式", "desc": "不抽题，根据真心话/大冒险概率提示自由提问或发起大冒险"},
            {"cmd": "/完整模式", "desc": "完整回复（骰子结果+排名+问题和@输家）"},
            {"cmd": "/踢人 <昵称或QQ号>", "desc": "将指定玩家踢出游戏"},
            {"cmd": "/添加真心话 <题目>", "desc": "自定义真心话题目"},
            {"cmd": "/添加大冒险 <挑战>", "desc": "自定义大冒险挑战"},
            {"cmd": "/题库", "desc": "查看当前题库内容"},
            {"cmd": "/补充题库", "desc": "立即手动补充真心话大冒险题库"},
            {"cmd": "/做过", "desc": "查看自己做过的题目记录"},
            {"cmd": "/清空做过", "desc": "清空自己做过的题目历史"},
            {"cmd": "/游戏状态", "desc": "查看当前游戏状态"},
            {"cmd": "/结束", "desc": "结束游戏"},
        ],
    },
    "娱乐互动": {
        "icon": "🎲",
        "description": "掷骰子、猜拳、抽卡、运势、脑筋急转弯、点歌等轻松互动",
        "commands": [
            {"cmd": "/骰子 [数量]", "desc": "掷指定数量的骰子（默认1个）"},
            {"cmd": "/猜拳 石头/剪刀/布", "desc": "和 Bot 猜拳对决"},
            {"cmd": "/抽卡 [数量]", "desc": "抽卡（SSR/SR/R/N 随机稀有度）"},
            {"cmd": "/运势", "desc": "查看今日运势"},
            {"cmd": "/运势 [星座]", "desc": "查看指定星座运势"},
            {"cmd": "/脑筋急转弯", "desc": "出一道脑筋急转弯"},
            {"cmd": "/答案", "desc": "查看脑筋急转弯答案"},
            {"cmd": "/点歌", "desc": "随机推荐一首歌"},
            {"cmd": "/点歌 [歌名]", "desc": "指定歌曲"},
        ],
    },
    "谐音梗": {
        "icon": "🎯",
        "description": "看图猜谐音梗 — 两张图片对应一个词语，考验联想能力",
        "commands": [
            {"cmd": "/谐音梗", "desc": "出一道谐音梗题目（发送图片）"},
            {"cmd": "直接回复答案", "desc": "猜测谐音梗答案（Bot 会逐字反馈对错）"},
            {"cmd": "/答案", "desc": "公布当前谐音梗的答案"},
        ],
    },
    "海龟汤": {
        "icon": "🐢",
        "description": "海龟汤情境推理 — 根据汤面（谜面），用是/否问题推理出汤底（真相）",
        "commands": [
            {"cmd": "/海龟汤", "desc": "开始一局海龟汤游戏（随机出题）"},
            {"cmd": "@Bot [问题]", "desc": "向 Bot 提问（只能用是/否回答的问题）"},
            {"cmd": "/提交答案 [推理]", "desc": "提交你的汤底推理（每人一次机会）"},
            {"cmd": "/整理线索", "desc": "查看已确认的线索汇总"},
            {"cmd": "/提示", "desc": "获得一条提示（有限次数）"},
            {"cmd": "/结束", "desc": "手动结束当前游戏"},
            {"cmd": "/答案", "desc": "公布汤底（投降）"},
            {"cmd": "/海龟汤状态", "desc": "查看当前游戏进度"},
        ],
    },
    "猜老婆": {
        "icon": "🔍",
        "description": "看图猜角色 — 从 cosplay 图片中随机裁剪 1/8 面积，让大家猜是哪个角色（每人一次机会）",
        "commands": [
            {"cmd": "/猜老婆", "desc": "出一道猜老婆题目（发送裁剪图片+六个选项）"},
            {"cmd": "回复 A-F", "desc": "选择你的答案（每人只有一次机会）"},
            {"cmd": "/答案", "desc": "公布当前猜老婆题目的答案（含完整图片）"},
        ],
    },
    "图包搜索": {
        "icon": "🖼️",
        "description": "cosplay 图包自然语言搜索 — 用关键词从数据库中随机查找符合描述的图片",
        "commands": [
            {"cmd": "/找图 描述", "desc": "搜索并随机返回一张符合描述的图片（支持角色名、服装、场景、风格等）"},
            {"cmd": "/找图 银发女仆", "desc": "示例：搜索银发女仆装"},
            {"cmd": "/找图 初音未来 圣诞", "desc": "示例：搜索初音未来的圣诞主题"},
        ],
    },
    "AI画图": {
        "icon": "🎨",
        "description": "AI 文生图 — 用自然语言描述生成图片，30 秒后自动撤回",
        "commands": [
            {"cmd": "/画图 <提示词>", "desc": "AI 根据描述生成一张图片（3:2 比例，1536×1024）"},
            {"cmd": "/描述画图 <自然语言描述>", "desc": "先用 LLM 解析描述再绘图（适合角色名/作品名）"},
            {"cmd": "/修改描述 <修改意见>", "desc": "对上一轮 /描述画图 进行修改"},
            {"cmd": "/画图 赛博朋克风格的猫", "desc": "示例：生成赛博朋克风格的猫"},
            {"cmd": "/画图 夕阳下的海边少女", "desc": "示例：生成夕阳海边的少女"},
        ],
    },
    "AI对话": {
        "icon": "🤖",
        "description": "AI 聊天 — 直接 @Bot 对话，支持自定义人设角色扮演 + 用户人设管理",
        "commands": [
            {"cmd": "@Bot [消息]", "desc": "直接与 AI 对话"},
            {"cmd": "/人设 [角色描述]", "desc": "设置 AI 扮演的角色人设"},
            {"cmd": "/人设", "desc": "查看当前 AI 角色人设"},
            {"cmd": "/清除人设", "desc": "清除 AI 角色人设，恢复默认"},
            {"cmd": "/用户人设", "desc": "查看你自己的用户人设（含身份标签、兴趣、性格等 6 部分）"},
            {"cmd": "/修改人设 [描述]", "desc": "用自然语言修改人设（身份/兴趣/性格/关系/性经历/性癖好）"},
            {"cmd": "/临时人设 [描述]", "desc": "设置临时人设（如当前心情、状态），/恢复人设 可清除"},
            {"cmd": "/恢复人设", "desc": "清除临时人设，恢复正式人设"},
            {"cmd": "/更新人设 [昵称]", "desc": "从聊天记录自动提取该用户的结构化人设数据（仅管理员）"},
            {"cmd": "/更新全部人设", "desc": "为群内所有活跃用户批量提取/更新人设（仅管理员）"},
        ],
    },
    "群管理": {
        "icon": "📋",
        "description": "群聊管理与分析 — 评选有趣瞬间、总结聊天内容、屏蔽/解封用户、群投票、智能查询、用户聊天分析",
        "commands": [
            {"cmd": "/评选", "desc": "分析当日聊天记录，评选 5 大有趣栏目（仅管理员）"},
            {"cmd": "/总结", "desc": "总结概括当天的群聊聊天内容（仅管理员）"},
            {"cmd": "/活跃度[n]", "desc": "显示近 n 天群聊活跃度排行（默认15天，如 /活跃度7、/活跃度 7）"},
            {"cmd": "/模仿 <昵称> <内容>", "desc": "（管理员）模拟指定群友的口吻在群里发言"},
            {"cmd": "/查询 问题", "desc": "基于近 24 小时聊天记录回答你的问题（可用 /查询120 最多查 5 天）"},
            {"cmd": "/分析 <QQ号> <问题>", "desc": "分析指定用户聊天记录回答你的问题（含@/回复/上下文，默认近 15 天，可用 /分析60 查 60 天）"},
            {"cmd": "/群像 <问题>", "desc": "基于本群所有用户人设回答关于群友的问题（如群友喜好比例）"},
            {"cmd": "/投票 选项A 选项B ...", "desc": "发起群投票（120秒后公布结果）"},
            {"cmd": "/投票", "desc": "查看当前投票状态"},
            {"cmd": "/结束投票", "desc": "提前结束投票并公布结果"},
            {"cmd": "/黑名单", "desc": "（管理员）查看屏蔽名单"},
            {"cmd": "/拉黑 QQ号", "desc": "（管理员）将用户加入屏蔽名单"},
            {"cmd": "/解封 QQ号", "desc": "（管理员）将用户从屏蔽名单移除"},
            {"cmd": "/开启审查", "desc": "（管理员）开启内容审查，敏感词替换为拼音"},
            {"cmd": "/关闭审查", "desc": "（管理员）关闭内容审查"},
            {"cmd": "/审查状态", "desc": "查看当前内容审查开关状态"},
            {"cmd": "/暂停任务", "desc": "（管理员）暂停每日定时更新任务（人设、画像、真心话题库）"},
            {"cmd": "/恢复任务", "desc": "（管理员）恢复每日定时更新任务"},
            {"cmd": "/补充题库", "desc": "（管理员）立即手动补充真心话大冒险题库"},
            {"cmd": "/迁移群聊 <目标群号>", "desc": "（管理员）将当前群未加入目标群的成员邀请过去"},
        ],
    },
}

# 分类别名映射（方便用户用不同关键词找到分类）
CATEGORY_ALIASES = {
    "角色扮演": ["扮演", "rp", "rpg", "跑团", "跑", "冒险"],
    "卧底": ["卧底", "谁是卧底", "spy"],
    "真心话大冒险": ["真心话", "大冒险", "td", "真心话大冒险", "tord"],
    "娱乐互动": ["娱乐", "互动", "游戏", "玩"],
    "谐音梗": ["谐音梗", "谐音", "看图", "看图猜", "猜词"],
    "海龟汤": ["海龟汤", "海龟", "汤", "turtle", "turtle_soup"],
    "猜老婆": ["猜老婆", "猜角色", "老婆"],
    "图包搜索": ["找图", "搜图", "图包", "图"],
    "AI对话": ["ai", "对话", "聊天", "人设", "角色"],
    "群管理": ["管理", "群管", "屏蔽", "黑名单", "评选", "总结", "活跃度", "查询", "审查", "迁移群聊", "分析", "群像"],
}


def get_category_aliases_map() -> dict[str, str]:
    """构建 别名 → 分类名 的映射表"""
    alias_map = {}
    for category, aliases in CATEGORY_ALIASES.items():
        for alias in aliases:
            alias_map[alias.lower()] = category
    # 精确匹配分类名本身
    for category in CATEGORIES:
        alias_map[category.lower()] = category
    return alias_map


# 预生成别名映射
_ALIAS_MAP = get_category_aliases_map()


def resolve_category(query: str) -> str | None:
    """
    根据用户输入查找对应的分类名。
    返回分类名，找不到返回 None。
    """
    if not query:
        return None
    key = query.strip().lower()
    return _ALIAS_MAP.get(key)


def get_first_level_menu() -> str:
    """
    生成一级帮助菜单 — 只显示功能大类。
    """
    lines = ["🤖 QQ 群机器人 - 功能菜单", ""]

    for name, info in CATEGORIES.items():
        icon = info["icon"]
        desc = info["description"]
        cmd_count = len(info["commands"])
        lines.append(f"{icon} {name}（{cmd_count} 个功能）")
        lines.append(f"   {desc}")
        lines.append("")

    lines.append("💡 发送「/帮助 [分类名]」查看具体指令")
    lines.append("   例如：/帮助 角色扮演、/帮助 真心话大冒险")
    lines.append("   支持简写：/帮助 扮演、/帮助 娱乐、/帮助 AI")

    return "\n".join(lines)


def get_second_level_menu(category_name: str) -> str | None:
    """
    生成二级帮助菜单 — 显示指定分类下的所有具体指令。

    参数：
        category_name: 分类名或别名
    返回：
        帮助文本，如果分类不存在则返回 None
    """
    resolved = resolve_category(category_name)
    if not resolved or resolved not in CATEGORIES:
        return None

    info = CATEGORIES[resolved]
    icon = info["icon"]

    lines = [f"{icon} {resolved} - 功能详情", ""]

    for cmd_info in _apply_qa_descs(info["commands"]):
        cmd = cmd_info["cmd"]
        desc = cmd_info["desc"]
        # 缩进格式：指令靠左，说明靠右
        lines.append(f"  {cmd}")
        lines.append(f"    → {desc}")

    lines.append("")
    lines.append("💡 发送「/帮助」返回功能列表")

    return "\n".join(lines)


def handle_help(text: str) -> str:
    """
    统一帮助入口函数。

    - 无参数 → 一级菜单
    - 有参数 → 尝试解析为分类名 → 二级菜单

    参数：
        text: 用户输入文本（如 "/帮助" 或 "/帮助 角色扮演"）
    返回：
        帮助文本
    """
    # 提取分类关键词（去掉 /帮助 或 /help 前缀）
    query = text.strip()
    if query.startswith("/帮助"):
        query = query[3:].strip()
    elif query.startswith("/help"):
        query = query[5:].strip()

    if not query:
        return get_first_level_menu()

    result = get_second_level_menu(query)
    if result:
        return result

    # 分类未找到，提示可用分类
    category_names = "、".join(f"'{name}'" for name in CATEGORIES)
    return (
        f"🤔 未找到分类「{query}」\n\n"
        f"可用的功能分类：{category_names}\n\n"
        f"💡 发送「/帮助 [分类名]」查看具体指令"
    )
