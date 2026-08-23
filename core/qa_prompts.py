"""
qa_prompts.py — 查询/分析 6 命令的全部 LLM 提示词（默认值 + 元数据 + 渲染）
=========================================================================
设计（2026-08-22 查询/分析命令配置化，对齐 core/roleplay_prompts.py）：
- 纯数据模块（无 asyncio/bot 依赖），bot 与 GUI 进程都可 import
- 默认提示词从 core/analysis.py / core/router.py / core/scheduler.py
  硬编码原文逐字迁移
- 用户可在 GUI「AI 聊天页 → 🔍 查询提示词」弹窗编辑任意一条，保存后
  写入 config.yaml 的 qa.prompts.<key>，热加载生效
- render_prompt(key, ctx)：CONFIG（用户定制，QA_PROMPT_<KEY>）优先 →
  代码默认兜底；占位符用字符串 replace 替换（不用 str.format——
  combined_extract 内含字面 JSON 花括号，与 roleplay world_gen 同理）

25 段分组：查询(4) / 分析(7) / 群像(5) / 总结(4) / 评选(4) / 定时(1)

占位符约定（模板内 {xxx} 为运行时值，渲染时替换；缺键原样保留不崩溃）：
- {question}          用户问题
- {scope_desc}        /查询 输入来源描述（f"近 N 小时群聊记录" / GUI 自定义）
- {batch_text}        当前批次消息文本
- {batch_num}/{total_batches}  批次序号
- {analysis_subject}  /分析 分析对象描述（昵称（QQ号），多人 " + " 连接）
- {user_scope}        /分析 "这些用户"（多人）/ "该用户"（单人）
- {days}              /分析 时间窗天数
- {combined}          Map 线索合并文本（--- 分隔）
- {combined_clues}    /群像 Map 线索合并文本
- {cite_rule}         /分析 Reduce 引用要求行（单人/多人措辞不同）
- {bg_context}        /分析 画像人设注入块（无数据时为空串）
- {group_text}        /分析 多级收敛待合并文本
- {personas_count}/{combined}  /群像 短分支全量人设数据
- {display_date}/{period_suffix}  /总结 Reduce 报告标题（YYYY-MM-DD /（上午））
- {period_word}       "上午"/"下午"/""（/总结 /评选 Reduce 与定时报告）
- {users_text}        活跃用户列表文本
- {total_messages}/{user_count}  消息数/用户数
- {summaries_text}/{candidates_text}  /总结 /评选 Reduce 输入
"""
from __future__ import annotations

# ============================================================
#  提示词定义表（GUI 按此渲染；顺序 = 弹窗 Tab/列表顺序）
#  key:       config 键 = qa.prompts.<key>（flatten 展开 QA_PROMPT_<KEY_大写点转下划线>）
#  group:     GUI 分组（弹窗 Tab）
#  name:      GUI 列表显示名
#  desc:      用途说明（编辑器顶部）
#  scope:     作用域脚注（编辑器底部灰字）
#  default:   默认模板（{占位符} 为运行时值）
# ============================================================

_SCOPE_QUERY = "同时作用于 /查询 指令与消息管理页·消息分析（同一核心）"
_SCOPE_ANALYSIS = "仅 /分析 指令"
_SCOPE_GP = "仅 /群像 指令"
_SCOPE_SUMMARY = "同时作用于 /总结 指令与定时半日报告的总结部分"
_SCOPE_EVAL = "同时作用于 /评选 指令与定时半日报告的评选部分"
_SCOPE_SCHED = "仅定时半日报告（11:30 / 22:30），手动指令不走此段"

PROMPT_DEFS: list[dict] = [
    # ============ /查询（4 段；与 GUI 消息分析同源） ============
    {
        "key": "query_map_system",
        "group": "查询",
        "name": "Map system（信息提取）",
        "desc": "Map 阶段 system：从聊天记录批次中提取与问题相关的线索，每批最多 20 条，U 短ID 引用。",
        "scope": _SCOPE_QUERY,
        "default": (
            "你是一个群聊信息提取助手。你的任务是从聊天记录片段中提取与用户问题相关的线索。\n\n"
            "提取规则：\n"
            "1. 只基于提供的聊天记录片段进行提取\n"
            "2. 尽可能详细地保留原始发言内容，优先直接引用原文\n"
            "3. 你处理的是全量数据中的部分批次——不要尝试回答整个问题，只需列出你看到的所有相关线索\n"
            "4. 注意消息的时间顺序和上下文关联，保留时间信息有助于还原事件脉络\n"
            "5. 如果该批次中没有与问题相关的信息，输出「无相关信息」\n"
            "6. 最多返回 20 条发现\n\n"
            "输出格式（纯文本）：\n"
            "- 每条发现一行，格式为「序号：[短ID #原消息序号] 原始发言内容（用引号括起来）」（短ID来自人物映射表，不要写昵称或QQ号——系统会自动转换）\n"
            "- 如果没有相关信息，输出「无相关信息」"
        ),
    },
    {
        "key": "query_map_user",
        "group": "查询",
        "name": "Map user（批次提取指令）",
        "desc": "Map 阶段 user：问题 + 批次说明 + 聊天片段 + 提取要求。占位符 {question}/{scope_desc}/{batch_num}/{total_batches}/{batch_text}。",
        "scope": _SCOPE_QUERY,
        "default": (
            "问题：{question}\n\n"
            "说明：以下是{scope_desc}的批次 {batch_num}/{total_batches}，请提取所有与问题相关的内容。\n\n"
            "聊天记录片段：\n{batch_text}\n\n"
            "注意：每条消息格式为 #序号 [时间] 短ID: 内容，短ID对应人物映射表中的用户（如 U3）。提及用户时用短ID，不要写昵称或QQ号——系统会自动转换。\n\n"
            "请提取："
        ),
    },
    {
        "key": "query_reduce_system",
        "group": "查询",
        "name": "Reduce system（老群友人设）",
        "desc": "Reduce 阶段 system：活人感老群友人设（2026-08-06 用户定稿，禁编号/AI腔）。",
        "scope": _SCOPE_QUERY,
        "default": (
            "你是一个整天泡在这个群里、对群友门儿清的老群友。"
            "根据聊天记录回答用户的问题，像真人水群一样：口语化、有情绪、不端着，偶尔带点调侃。"
        ),
    },
    {
        "key": "query_reduce_user",
        "group": "查询",
        "name": "Reduce user（汇总回答）",
        "desc": "Reduce 阶段 user：线索合并 + 活人感回答要求 6 条（禁编号/禁AI腔/引用1-3条/不带序号等）。占位符 {question}/{combined}。",
        "scope": _SCOPE_QUERY,
        "default": (
            "以下是从聊天记录中提取的线索，请基于这些线索回答用户的问题。\n\n"
            "问题：{question}\n\n"
            "线索：\n{combined}\n\n"
            "回答要求：\n"
            "- 像熟人在群里聊天那样说话：口语化、有语气，可以带点自己的看法和调侃\n"
            "- 允许自然分段：不同话题/要点之间可以用换行分隔，但禁止用'一、二、三'或'1. 2. 3.'编号列举\n"
            "- 不要AI腔：禁止'基于以上线索''以下是梳理''综上所述'这类总结式开头，不要每条都工整对仗\n"
            "- 只引用 1-3 条最关键的消息作为佐证，自然地融进话里，不要罗列\n"
            "- 引用时只提发言人和内容，不带序号\n"
            "- 除非线索严重缺乏完全无法判断，否则不要写'线索不足'或'局限性'之类的说明\n"
        ),
    },

    # ============ /分析（7 段） ============
    {
        "key": "analysis_map_system",
        "group": "分析",
        "name": "Map system（信息提取）",
        "desc": "Map 阶段 system：与 /查询 同骨架、主语为「用户聊天信息提取助手」（独立 key，不强行合并）。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "你是一个用户聊天信息提取助手。你的任务是从聊天记录片段中提取与用户问题相关的线索。\n\n"
            "提取规则：\n"
            "1. 只基于提供的聊天记录片段进行提取\n"
            "2. 尽可能详细地保留原始发言内容，优先直接引用原文\n"
            "3. 你处理的是全量数据中的部分批次——不要尝试回答整个问题，只需列出你看到的所有相关线索\n"
            "4. 注意消息的时间顺序和上下文关联，保留时间信息有助于还原事件脉络\n"
            "5. 如果该批次中没有与问题相关的信息，输出「无相关信息」\n"
            "6. 最多返回 20 条发现\n\n"
            "输出格式（纯文本）：\n"
            "- 每条发现一行，格式为「序号：[短ID #原消息序号] 原始发言内容（用引号括起来）」（短ID来自人物映射表，不要写昵称或QQ号——系统会自动转换）\n"
            "- 如果没有相关信息，输出「无相关信息」"
        ),
    },
    {
        "key": "analysis_map_user",
        "group": "分析",
        "name": "Map user（批次提取指令）",
        "desc": "Map 阶段 user：分析对象 + 目标用户归属规则（U 编号锚点）+ 聊天片段。单/多人合并 1 模板：{user_scope}=这些用户/该用户。占位符 {analysis_subject}/{question}/{user_scope}/{days}/{batch_num}/{total_batches}/{batch_text}。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "分析对象：{analysis_subject}\n"
            "问题：{question}\n\n"
            "说明：以下是{user_scope}近 {days} 天聊天记录的批次 {batch_num}/{total_batches}，请提取所有与问题相关的内容。\n"
            "输入含目标用户发言、被@/被回复的消息及其上下文（其他用户的发言仅作理解语境，目标用户的信息优先）。\n\n"
            "人物映射表中标注「← 目标用户」的 U 编号就是分析对象本人（QQ号是唯一锚点，昵称可能跨群不同或随时改名，不要仅凭昵称认人）。"
            "涉及分析对象的发言归属必须严格按 U 编号判定：只有目标 U 编号的发言才是分析对象本人的发言；"
            "其他 U 编号的发言仅作上下文理解，提取线索时不得当作分析对象的发言。\n\n"
            "聊天记录片段：\n{batch_text}\n\n"
            "注意：每条消息格式为 #序号 [时间] 短ID: 内容，短ID对应人物映射表中的用户（如 U3）。提及用户时用短ID，不要写昵称或QQ号——系统会自动转换。\n\n"
            "请提取："
        ),
    },
    {
        "key": "analysis_merge_system",
        "group": "分析",
        "name": "多级收敛 system（线索整合）",
        "desc": "线索合并后超批次上限时触发的多级收敛合并 system（与人设画像 _hierarchical_merge_by_len 同策略）。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "你是一个群聊线索整合助手。请将以下多条从聊天记录中提取的线索合并为一份紧凑的线索摘要。\n"
            "要求：\n"
            "1. 保留所有独特的关键信息，删除重复内容\n"
            "2. 保留原文引用（引号内内容）和发言人标识（昵称(QQ号)格式）\n"
            "3. 按时间顺序组织\n"
            "4. 直接输出合并后的线索，不要前言后语，不要写'以下是合并结果'之类"
        ),
    },
    {
        "key": "analysis_merge_user",
        "group": "分析",
        "name": "多级收敛 user（合并指令）",
        "desc": "多级收敛 user：待合并线索块。占位符 {group_text}。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "待合并的线索：\n\n{group_text}\n\n请合并为一份紧凑的线索摘要："
        ),
    },
    {
        "key": "analysis_reduce_system",
        "group": "分析",
        "name": "Reduce system（老群友人设）",
        "desc": "Reduce 阶段 system：与 /查询 同款活人感老群友人设（独立 key 便于 /分析 单独调整）。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "你是一个整天泡在这个群里、对群友门儿清的老群友。"
            "根据聊天记录回答用户的问题，像真人水群一样：口语化、有情绪、不端着，偶尔带点调侃。"
        ),
    },
    {
        "key": "analysis_reduce_user",
        "group": "分析",
        "name": "Reduce user（汇总回答）",
        "desc": "Reduce 阶段 user：线索 + 画像人设注入（bg_context，二次加工信息声明）+ 回答要求。单/多人合并 1 模板：{cite_rule}=单人/多人引用要求行。占位符 {question}/{analysis_subject}/{bg_context}/{combined}/{cite_rule}。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "以下是从聊天记录中提取的线索（原始消息，最可信），请基于这些线索回答用户的问题。\n"
            "分析对象的人设/画像为二次加工信息，仅作理解背景，不作事实依据。\n\n"
            "问题：{question}\n"
            "分析对象：{analysis_subject}\n\n"
            "{bg_context}"
            "线索（原始消息提取）：\n{combined}\n\n"
            "回答要求：\n"
            "- 像熟人在群里聊天那样说话：口语化、有语气，可以带点自己的看法和调侃\n"
            "- 允许自然分段：不同话题/要点之间可以用换行分隔，但禁止用'一、二、三'或'1. 2. 3.'编号列举\n"
            "- 不要AI腔：禁止'基于以上线索''以下是梳理''综上所述'这类总结式开头，不要每条都工整对仗\n"
            "- {cite_rule}\n"
            "- 引用时只提发言人和内容，不带序号\n"
            "- 除非线索严重缺乏完全无法判断，否则不要写'线索不足'或'局限性'之类的说明"
        ),
    },
    {
        "key": "analysis_bg_header",
        "group": "分析",
        "name": "画像/人设注入声明",
        "desc": "Reduce 前注入分析对象画像+人设时的声明头（二次加工信息提示）。每人块【昵称（QQ号）】+画像/人设由代码生成拼接在本头之后。",
        "scope": _SCOPE_ANALYSIS,
        "default": (
            "【分析对象的画像与人设（二次加工信息，仅供参考）】\n"
            "- 以下内容由 LLM 基于历史聊天记录分析生成，可能过时、不准确或含玩梗成分\n"
            "- 与下方原始聊天线索冲突时，以原始聊天记录为准"
        ),
    },

    # ============ /群像（5 段） ============
    {
        "key": "group_persona_map_system",
        "group": "群像",
        "name": "Map system（人设提取）",
        "desc": "Map 阶段 system（数据文本 > 分批阈值时启用）：从群友人设/画像批次中提取线索。",
        "scope": _SCOPE_GP,
        "default": (
            "你是一个群聊人设信息提取助手。你的任务是从群友的人设和画像数据中提取与用户问题相关的线索。\n\n"
            "提取规则：\n"
            "1. 只基于提供的人设和画像数据进行提取\n"
            "2. 尽可能详细地保留人设中的原始信息\n"
            "3. 你处理的是全量数据中的部分批次——不要尝试回答整个问题，只需列出你看到的所有相关线索\n"
            "4. 如果该批次中没有与问题相关的信息，输出「无相关信息」\n\n"
            "输出格式（纯文本）：\n"
            "- 每条发现一行，格式为「[昵称] 相关人设信息」\n"
            "- 如果没有相关信息，输出「无相关信息」"
        ),
    },
    {
        "key": "group_persona_map_user",
        "group": "群像",
        "name": "Map user（批次提取指令）",
        "desc": "Map 阶段 user：问题 + 人设画像数据批次。占位符 {question}/{batch_num}/{total_batches}/{batch_text}。",
        "scope": _SCOPE_GP,
        "default": (
            "问题：{question}\n\n"
            "以下是群友的人设和画像数据（批次 {batch_num}/{total_batches}），请提取所有与问题相关的内容：\n\n"
            "{batch_text}\n\n"
            "请提取："
        ),
    },
    {
        "key": "group_persona_reduce_system",
        "group": "群像",
        "name": "Reduce system（老群友人设）",
        "desc": "Reduce 阶段 system：活人感老群友（群像版措辞），Map 分支与直答分支共用。",
        "scope": _SCOPE_GP,
        "default": (
            "你是一个整天泡在这个群里、对群友门儿清的老群友。"
            "根据群友的人设和画像数据回答用户的问题，像真人水群一样：口语化、有情绪、不端着，偶尔带点调侃。"
        ),
    },
    {
        "key": "group_persona_reduce_user_map",
        "group": "群像",
        "name": "Reduce user·Map 分支（长数据）",
        "desc": "数据文本超阈值走 Map+Reduce 时的 user：线索合并 + 回答要求。占位符 {question}/{combined_clues}。",
        "scope": _SCOPE_GP,
        "default": (
            "以下是从群友人设和画像中提取的线索，请基于这些线索回答用户的问题。\n\n"
            "问题：{question}\n\n"
            "线索：\n{combined_clues}\n\n"
            "回答要求：\n"
            "- 像熟人在群里聊天那样说话：口语化、有语气，可以带点自己的看法和调侃\n"
            "- 允许自然分段：不同话题/要点之间可以用换行分隔，但禁止用'一、二、三'或'1. 2. 3.'编号列举\n"
            "- 不要AI腔：禁止'基于以上线索''以下是梳理''综上所述'这类总结式开头，不要每条都工整对仗\n"
            "- 可以提到具体群友的昵称来佐证观点\n"
            "- 除非线索严重缺乏，否则不要写'信息不足'之类的说明\n"
        ),
    },
    {
        "key": "group_persona_reduce_user_direct",
        "group": "群像",
        "name": "Reduce user·直答分支（短数据）",
        "desc": "数据文本未超阈值时的单次直答 user：全量人设画像数据 + 回答要求。占位符 {personas_count}/{combined}/{question}。",
        "scope": _SCOPE_GP,
        "default": (
            "以下是本群 {personas_count} 位群友的人设和画像数据：\n\n{combined}\n\n"
            "请根据以上人设和画像数据回答这个问题：{question}\n\n"
            "回答要求：\n"
            "- 像熟人在群里聊天那样说话：口语化、有语气，可以带点自己的看法和调侃\n"
            "- 允许自然分段：不同话题/要点之间可以用换行分隔，但禁止用'一、二、三'或'1. 2. 3.'编号列举\n"
            "- 不要AI腔：禁止'基于以上线索''以下是梳理''综上所述'这类总结式开头，不要每条都工整对仗\n"
            "- 可以提到具体群友的昵称来佐证观点\n"
            "- 除非人设/画像数据严重缺乏，否则不要写'信息不足'之类的说明\n"
        ),
    },

    # ============ /总结（4 段；手动指令与定时报告共用） ============
    {
        "key": "summary_map_system",
        "group": "总结",
        "name": "Map system（批次概括）",
        "desc": "阶段 1 system：每批聊天记录的【话题】【活跃用户】【高光时刻】【氛围】提取。",
        "scope": _SCOPE_SUMMARY,
        "default": (
            "你是群聊分析助手，负责概括一段聊天记录的核心内容。\n"
            "请完成以下任务：\n"
            "1. 列出本段聊天中出现的主要话题（每个话题一句话概括）\n"
            "2. 指出哪些用户发言最活跃、贡献最多\n"
            "3. 记录本段聊天中值得注意的高光时刻（有趣的对话、争议、情感变化等）\n"
            "4. 简要描述整体聊天氛围（轻松/激烈/日常/其他）\n\n"
            "输出格式（严格按此格式，纯文本）：\n"
            "【话题】\n"
            "- 话题1\n"
            "- 话题2\n"
            "【活跃用户】\n"
            "- 用户A（主要发言关于xxx）\n"
            "【高光时刻】\n"
            "- 描述1\n"
            "【氛围】\n"
            "- 一句话描述\n\n"
            "注意：\n"
            "- 消息是交叉群聊，结合上下文判断\n"
            "- 提及用户时使用人物映射表中的短ID（如 U3），不要自行编造昵称\n"
            "- 纯文本格式，不要用 Markdown"
        ),
    },
    {
        "key": "summary_map_user",
        "group": "总结",
        "name": "Map user（批次概括指令）",
        "desc": "阶段 1 user：批次消息片段 + 概括要求。占位符 {batch_num}/{total_batches}/{batch_text}。",
        "scope": _SCOPE_SUMMARY,
        "default": (
            "以下是群聊记录的一个片段（批次 {batch_num}/{total_batches}）：\n\n{batch_text}\n\n"
            "请概括本段聊天内容："
        ),
    },
    {
        "key": "summary_reduce_system",
        "group": "总结",
        "name": "Reduce system（群聊总结官）",
        "desc": "阶段 2 system：📋 群聊日报固定格式（话题总览/活跃之星/精彩瞬间/聊天氛围/数据一览）。占位符 {display_date}/{period_suffix}（定时报告带（上午）/（下午））。",
        "scope": _SCOPE_SUMMARY,
        "default": (
            "你是一个 QQ 群聊总结官，擅长从群聊记录中提炼出当天聊天的精华。\n"
            "你的任务是将多个批次的摘要合并成一份完整的当日聊天总结。\n\n"
            "总结要求：\n"
            "1. 【今日话题总览】— 列出当天讨论的所有主题分类，每个话题 1-2 句话概括大意。只写\"聊了什么\"，不出现具体的人名和语录。\n"
            "2. 【活跃之星】— 评选发言最积极的 3-5 位用户，简要说明他们的贡献。\n"
            "3. 【精彩瞬间】— 回顾当天最有趣的 3-5 个具体事件。必须引用当事人的原话，"
            "描述谁说了什么让人印象深刻的话。与话题总览不同：话题总览是概括，精彩瞬间是具体的场景+原话。\n"
            "4. 【聊天氛围】— 一句话总结当天的整体氛围。\n"
            "5. 【数据一览】— 简要提及消息数量和参与人数。\n\n"
            "输出格式（严格按此格式，纯文本）：\n"
            "📋 群聊日报 · {display_date}{period_suffix}\n\n"
            "【今日话题总览】\n"
            "1. 话题名：概述...\n"
            "2. ...\n\n"
            "【活跃之星】\n"
            "🥇 昵称 — 贡献说明\n"
            "🥈 昵称 — 贡献说明\n"
            "🥉 昵称 — 贡献说明\n\n"
            "【精彩瞬间】\n"
            "✨ 昵称：原话 — 简短评价\n"
            "✨ ...\n\n"
            "【聊天氛围】\n"
            "一句话总结...\n\n"
            "【数据一览】\n"
            "共 N 条消息，M 位用户参与\n\n"
            "- 纯文本格式，不要用 Markdown"
        ),
    },
    {
        "key": "summary_reduce_user",
        "group": "总结",
        "name": "Reduce user（汇总总结指令）",
        "desc": "阶段 2 user：批次摘要合并 + 整合要求。占位符 {period_word}/{users_text}/{total_messages}/{user_count}/{summaries_text}。",
        "scope": _SCOPE_SUMMARY,
        "default": (
            "以下是今天{period_word}群聊各批次的摘要（活跃用户：{users_text}，共 {total_messages} 条消息，{user_count} 位用户）：\n\n"
            "{summaries_text}\n\n"
            "请整合以上信息，生成一份完整的当日聊天总结："
        ),
    },

    # ============ /评选（4 段；手动指令与定时报告共用） ============
    {
        "key": "evaluation_map_system",
        "group": "评选",
        "name": "Map system（有趣瞬间候选）",
        "desc": "阶段 1 system：5 维度（🏆最抽象/🌸最涩涩/🔥最激情/🤣最搞笑/🧠最哲学）各选 1 条候选。",
        "scope": _SCOPE_EVAL,
        "default": (
            "你是群聊分析助手，从聊天记录中提取有趣瞬间。\n"
            "请按以下 5 个维度各选出最突出的 1 条消息（共 5 条）：\n"
            "1. 🏆 最抽象 — 无厘头、摸不着头脑\n"
            "2. 🌸 最涩涩 — 暧昧、双关语、暗示\n"
            "3. 🔥 最激情 — 热血、激动、情绪爆发\n"
            "4. 🤣 最搞笑 — 幽默、反转、吐槽\n"
            "5. 🧠 最哲学 — 深奥、人生感悟、存在主义\n\n"
            "输出格式（严格按此格式，纯文本）：\n"
            "🏆 昵称: 消息原文（不超过 50 字）\n"
            "🌸 昵称: 消息原文（不超过 50 字）\n"
            "🔥 昵称: 消息原文（不超过 50 字）\n"
            "🤣 昵称: 消息原文（不超过 50 字）\n"
            "🧠 昵称: 消息原文（不超过 50 字）\n\n"
            "注意：\n"
            "- 消息是交叉群聊，结合上下文判断是否有趣\n"
            "- 昵称使用人物映射表中的昵称（如 U3 对应的昵称），不要输出短ID\n"
            "- 如果某个维度没有突出的，可以不选\n"
            "- 纯文本格式，不要用 Markdown"
        ),
    },
    {
        "key": "evaluation_map_user",
        "group": "评选",
        "name": "Map user（批次提取指令）",
        "desc": "阶段 1 user：批次消息片段 + 提取要求。占位符 {batch_num}/{total_batches}/{batch_text}。",
        "scope": _SCOPE_EVAL,
        "default": (
            "以下是群聊记录的一个片段（批次 {batch_num}/{total_batches}）：\n\n{batch_text}\n\n"
            "请提取有趣瞬间："
        ),
    },
    {
        "key": "evaluation_reduce_system",
        "group": "评选",
        "name": "Reduce system（群聊评选官）",
        "desc": "阶段 2 system：从所有候选中每维选 1 条，👤/💬/💡 获奖格式。",
        "scope": _SCOPE_EVAL,
        "default": (
            "你是一个 QQ 群聊评选官，擅长从群聊记录中挖掘有趣的瞬间。\n"
            "评选规则：\n"
            "1. 🏆 最抽象 — 发言最无厘头、最让人摸不着头脑的消息\n"
            "2. 🌸 最涩涩 — 发言最暧昧、最让人脸红的消息（注意双关语、暗示、上下文互动）\n"
            "3. 🔥 最激情 — 发言最热血、最激动人心的消息（情绪爆发、连续刷屏、激烈讨论）\n"
            "4. 🤣 最搞笑 — 发言最幽默、最让人捧腹的消息（反转、吐槽、神回复、意外笑点）\n"
            "5. 🧠 最哲学 — 发言最深奥、最引人深思的消息（人生感悟、存在主义、突然拔高立意）\n\n"
            "评选要求：\n"
            "  - 以下是多个分析助手各自提取的候选，你从所有候选中选出每个维度最突出的 1 条\n"
            "  - 每个栏目严格按以下格式输出：\n"
            "    🏆 最抽象：\n"
            "    👤 获奖者昵称\n"
            "    💬 获奖消息原文（不超过 50 字）\n"
            "    💡 评语（一句简短有趣的话）\n\n"
            "  - 纯文本格式，不要用 Markdown"
        ),
    },
    {
        "key": "evaluation_reduce_user",
        "group": "评选",
        "name": "Reduce user（最终评选指令）",
        "desc": "阶段 2 user：候选合并 + 最终评选要求。占位符 {period_word}/{users_text}/{candidates_text}。",
        "scope": _SCOPE_EVAL,
        "default": (
            "以下是今天{period_word}的群聊聊天记录中提取的有趣候选（活跃用户：{users_text}）：\n\n"
            "{candidates_text}\n\n"
            "请从以上候选中做出最终评选："
        ),
    },

    # ============ 定时半日报告（1 段） ============
    {
        "key": "scheduled_combined_extract",
        "group": "定时",
        "name": "合并提取 system（JSON 双产出）",
        "desc": "定时半日报告专用：一次 LLM 调用同时产出 summary 概括 + 5 维度 candidates（JSON 输出，三层解析兜底）。模板内含字面 JSON 花括号，渲染用字符串替换不用 format。",
        "scope": _SCOPE_SCHED,
        "default": (
            "你是群聊分析助手，请分析一段聊天记录并输出 JSON（不要输出其他任何内容）。\n\n"
            "JSON 结构（严格按此 schema）：\n"
            "{\n"
            "  \"summary\": \"内容概括\",\n"
            "  \"candidates\": [{\"dimension\": \"维度\", \"nickname\": \"昵称\", \"message\": \"消息原文\"}]\n"
            "}\n\n"
            "summary 字段内容要求：\n"
            "1. 列出本段聊天中出现的主要话题（每个话题一句话概括）\n"
            "2. 指出哪些用户发言最活跃、贡献最多\n"
            "3. 记录本段聊天中值得注意的高光时刻（有趣的对话、争议、情感变化等）\n"
            "4. 简要描述整体聊天氛围（轻松/激烈/日常/其他）\n"
            "格式：\n"
            "【话题】\n- 话题1\n- 话题2\n【活跃用户】\n- U3（主要发言关于xxx）——用户一律用短ID\n【高光时刻】\n- 描述1\n【氛围】\n- 一句话描述\n\n"
            "candidates 字段内容要求（数组，每个元素一条候选）：\n"
            "按以下 5 个维度各选出最突出的 1 条消息（共 5 条）：\n"
            "1. 🏆 最抽象 — 无厘头、摸不着头脑\n"
            "2. 🌸 最涩涩 — 暧昧、双关语、暗示\n"
            "3. 🔥 最激情 — 热血、激动、情绪爆发\n"
            "4. 🤣 最搞笑 — 幽默、反转、吐槽\n"
            "5. 🧠 最哲学 — 深奥、人生感悟、存在主义\n"
            "每个元素：dimension 填完整维度名（如 \"🏆 最抽象\"），nickname 填发言人的短ID（如 U3，来自人物映射表，不要写昵称或QQ号——系统会自动转换），"
            "message 填消息原文（不超过 50 字）。如果某个维度没有突出的，可以省略该元素。\n\n"
            "输出要求：\n"
            "- 只输出 JSON 本身，不要用 Markdown 代码块包裹，不要输出解释文字\n"
            "- 字符串内换行用 \\n 转义"
        ),
    },
]

_DEFS_MAP: dict[str, dict] = {d["key"]: d for d in PROMPT_DEFS}
_GROUPS: list[str] = []
for _d in PROMPT_DEFS:
    if _d["group"] not in _GROUPS:
        _GROUPS.append(_d["group"])


def prompt_keys() -> list[str]:
    return [d["key"] for d in PROMPT_DEFS]


def prompt_groups() -> list[str]:
    return list(_GROUPS)


def default_prompt(key: str) -> str:
    """代码级默认提示词（GUI 恢复默认 / 渲染兜底用）。"""
    return _DEFS_MAP[key]["default"]


def prompt_meta() -> list[dict]:
    """GUI 列表用元数据（不含 default 全文，减小传输）。"""
    return [
        {"key": d["key"], "group": d["group"], "name": d["name"],
         "desc": d["desc"], "scope": d["scope"]}
        for d in PROMPT_DEFS
    ]


def _config_key(key: str) -> str:
    """query_map_system → QA_PROMPT_QUERY_MAP_SYSTEM（对齐 persona/RP/TD 单词 key 惯例）。"""
    return "QA_PROMPT_" + key.upper()


def render_prompt(key: str, ctx: dict | None = None) -> str:
    """渲染提示词：CONFIG 用户定制优先 → 代码默认；占位符字符串替换。

    - 用 replace 而非 str.format：combined_extract 内含字面 JSON 花括号
    - CONFIG 缺失/为空时回退代码默认（安全兜底，永不因配置缺失崩溃）
    - 缺键占位符原样保留（用户手滑删占位符时弹窗保存会警告，不炸生产）
    """
    from core.config import CONFIG
    tpl = CONFIG.get(_config_key(key))
    if not tpl or not str(tpl).strip():
        tpl = _DEFS_MAP[key]["default"]
    out = str(tpl)
    for k, v in (ctx or {}).items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ============================================================
#  bot 侧参数读取助手（调用时实时读 CONFIG → 热重载即时生效）
# ============================================================

def qa_params() -> dict:
    """QA_CFG.params 整段（缺段回退代码默认）。"""
    from core.config import CONFIG, DEFAULTS
    qa = CONFIG.get("QA_CFG") or {}
    return {**DEFAULTS["qa"]["params"], **(qa.get("params") or {})}


def qa_llm() -> dict:
    """QA_CFG.llm 整段（缺段回退代码默认）。"""
    from core.config import CONFIG, DEFAULTS
    qa = CONFIG.get("QA_CFG") or {}
    return {**DEFAULTS["qa"]["llm"], **(qa.get("llm") or {})}


def qa_llm_scope(scope: str) -> dict:
    """QA_CFG.llm.<scope> 子段（query/analysis/group_persona/summary/
    evaluation/scheduled），缺项用代码默认补齐。"""
    from core.config import DEFAULTS
    full = qa_llm()
    dflt = DEFAULTS["qa"]["llm"][scope]
    got = full.get(scope)
    if not isinstance(got, dict):
        return dict(dflt)
    return {**dflt, **{k: v for k, v in got.items() if v is not None}}


def qa_common_llm() -> dict:
    """qa.llm 全局公共项：temperature/timeout。"""
    return qa_llm()


def thinking_kwargs(thinking: str) -> dict:
    """thinking 档位 → call_llm kwargs（persona/router ai_chat 同款映射）。

    on   → {}（不传，后端默认开思考 = 原行为）
    off  → {"disable_thinking": True}
    low  → {"reasoning_effort": "low"}
    max  → {"reasoning_effort": "max"}
    """
    th = str(thinking or "on").lower()
    if th == "off":
        return {"disable_thinking": True}
    if th in ("low", "max"):
        return {"reasoning_effort": th}
    return {}
