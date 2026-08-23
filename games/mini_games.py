#!/usr/bin/env python3
"""
QQ 群机器人 - 小游戏模块
包含：掷骰子、猜拳、运势、脑筋急转弯、点歌
"""
import random

# ============ 脑筋急转弯题库 ============
RIDDLES = [
    {"q": "什么东西越洗越脏？", "a": "水"},
    {"q": "什么门永远关不上？", "a": "球门"},
    {"q": "什么布剪不断？", "a": "瀑布"},
    {"q": "什么瓜不能吃？", "a": "傻瓜"},
    {"q": "什么动物最没有方向感？", "a": "麋鹿（迷路）"},
    {"q": "什么书不可能在书店里买到？", "a": "秘书"},
    {"q": "什么东西有五个头，但人不觉得它怪？", "a": "人（五官）"},
    {"q": "什么车最不怕堵车？", "a": "风车"},
    {"q": "什么东西越用越多？", "a": "钱（花越多欠越多）"},
    {"q": "什么人最不怕冷？", "a": "雪人"},
    {"q": "什么动物最容易被贴在墙上？", "a": "海豹（海报）"},
    {"q": "什么鱼最值钱？", "a": "金鱼"},
    {"q": "什么东西越热越爱出来？", "a": "汗"},
    {"q": "什么动物最懂礼貌？", "a": "羊（因为羊先礼后兵/洋相）"},
    {"q": "什么动物最不怕冷？", "a": "北极熊"},
    {"q": "什么花夏天开、冬天也开？", "a": "心花"},
    {"q": "什么东西明明是你的，别人却用得比你多？", "a": "你的名字"},
    {"q": "什么东西打破了才能用？", "a": "鸡蛋"},
]

# 用户脑筋急转弯状态：{f"{group_id}:{user_id}": {"q_index": int, "timestamp": float}}
_RIDDLE_USER_STATE: dict[str, dict] = {}

# ============ 运势 ============
FORTUNES = [
    "🌟 今日运势：大吉！适合表白、考试、摸鱼。",
    "🌈 今日运势：中吉！遇到好事的概率 80%，建议出门。",
    "☀️ 今日运势：吉！心情大好，适合社交和点奶茶。",
    "🍀 今日运势：小吉！平淡但安心的一天。",
    "⚡ 今日运势：普通！注意别踩坑，稳中求胜。",
    "🌧️ 今日运势：小凶！容易遇到小麻烦，保持耐心。",
    "🔥 今日运势：凶！今天适合宅家，不要做大决定。",
    "💫 今日运势：大吉！适合搞钱，财神今天在线。",
    "🎯 今日运势：中吉！专注力满分，适合学习/工作。",
    "🍕 今日运势：吉！今天适合吃好吃的，犒劳自己。",
]

# ============ 星座运势 ============
ZODIAC = [
    "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座",
    "天秤座", "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座"
]

ZODIAC_TRAITS = {
    "白羊座": ["行动力 MAX", "热情似火", "直率可爱", "冲劲十足"],
    "金牛座": ["稳重可靠", "美食家", "理财小能手", "慢热但深情"],
    "双子座": ["社交达人", "脑洞大开", "好奇心 MAX", "双面性格"],
    "巨蟹座": ["温柔体贴", "家居达人", "记忆力好", "保护欲强"],
    "狮子座": ["王者气场", "慷慨大方", "领导力强", "爱面子"],
    "处女座": ["细节控", "追求完美", "理性分析", "洁癖晚期"],
    "天秤座": ["颜值正义", "社交高手", "犹豫不决", "审美在线"],
    "天蝎座": ["神秘迷人", "直觉敏锐", "占有欲强", "爱憎分明"],
    "射手座": ["自由灵魂", "乐观开朗", "旅行达人", "嘴比脑子快"],
    "摩羯座": ["工作狂", "野心勃勃", "责任感 MAX", "闷骚体质"],
    "水瓶座": ["脑洞清奇", "独立人格", "反传统", "忽冷忽热"],
    "双鱼座": ["浪漫幻想", "共情能力强", "艺术细胞", "恋爱脑"],
}

# ============ 歌曲库 ============
SONGS = [
    {"name": "晴天", "artist": "周杰伦", "genre": "流行"},
    {"name": "七里香", "artist": "周杰伦", "genre": "流行"},
    {"name": "告白气球", "artist": "周杰伦", "genre": "流行"},
    {"name": "稻香", "artist": "周杰伦", "genre": "流行"},
    {"name": "夜曲", "artist": "周杰伦", "genre": "流行"},
    {"name": "富士山下", "artist": "陈奕迅", "genre": "粤语"},
    {"name": "十年", "artist": "陈奕迅", "genre": "粤语"},
    {"name": "孤勇者", "artist": "陈奕迅", "genre": "流行"},
    {"name": "起风了", "artist": "买辣椒也用券", "genre": "流行"},
    {"name": "年少有为", "artist": "李荣浩", "genre": "流行"},
    {"name": "模特", "artist": "李荣浩", "genre": "流行"},
    {"name": "乌梅子酱", "artist": "李荣浩", "genre": "流行"},
    {"name": "光年之外", "artist": "邓紫棋", "genre": "流行"},
    {"name": "泡沫", "artist": "邓紫棋", "genre": "流行"},
    {"name": "句号", "artist": "邓紫棋", "genre": "流行"},
    {"name": "消愁", "artist": "毛不易", "genre": "民谣"},
    {"name": "像我这样的人", "artist": "毛不易", "genre": "民谣"},
    {"name": "红玫瑰", "artist": "陈奕迅", "genre": "粤语"},
    {"name": "白玫瑰", "artist": "陈奕迅", "genre": "粤语"},
    {"name": "爱情转移", "artist": "陈奕迅", "genre": "粤语"},
    {"name": "K歌之王", "artist": "陈奕迅", "genre": "粤语"},
]

# ============ 骰子工具 ============

def _roll_dice(dice_count: int, max_face: int = 6) -> tuple[int, list[int]]:
    """投掷骰子，返回 (总点数, 各骰子点数列表)"""
    rolls = [random.randint(1, max_face) for _ in range(dice_count)]
    return sum(rolls), rolls

def _get_auto_dice_count(player_count: int) -> int:
    """根据人数自动计算骰子数量"""
    if player_count <= 0:
        return 1
    return max(1, min(player_count, 6))

# ============ 小游戏处理器 ============

def handle_dice(text: str) -> str:
    """掷骰子（text 是参数部分，不含命令）"""
    dice_count = 1
    if text.strip().isdigit():
        dice_count = max(1, min(int(text.strip()), 10))
    total, rolls = _roll_dice(dice_count)
    return f"🎲 你掷了 {dice_count} 个骰子，总和 {total}，各骰子点数：{rolls}"

def handle_rps(text: str) -> str:
    """猜拳（text 是参数部分，不含命令）"""
    rps = {"石头": "🪨", "剪刀": "✂️", "布": "🖐️"}
    choices = list(rps.keys())
    user_choice = text.strip()
    if user_choice not in rps:
        return f"🤔 没找到「{user_choice}」，试试 石头/剪刀/布"
    bot_choice = random.choice(choices)
    user_emoji = rps[user_choice]
    bot_emoji = rps[bot_choice]
    if user_choice == bot_choice:
        result = "平局"
    elif (user_choice == "石头" and bot_choice == "剪刀") or \
         (user_choice == "剪刀" and bot_choice == "布") or \
         (user_choice == "布" and bot_choice == "石头"):
        result = "你赢了"
    else:
        result = "我赢了"
    return f"✊ 猜拳\n\n{user_emoji} 你出了 {user_choice}\n{bot_emoji} 我出了 {bot_choice}\n\n{result}"

def handle_fortune(text: str) -> str:
    """每日运势（text 是参数部分，不含命令）"""
    constellation = text.strip()
    if constellation and constellation in ZODIAC_TRAITS:
        traits = random.sample(ZODIAC_TRAITS[constellation], 2)
        lucky_num = random.randint(1, 99)
        lucky_color = random.choice(["红色", "粉色", "紫色", "蓝色", "绿色", "金色"])
        fortune = random.choice(FORTUNES)
        return f"🔮 {constellation} 运势\n\n{fortune}\n\n✨ 性格关键词：{', '.join(traits)}\n🍀 幸运数字：{lucky_num}\n🎨 幸运色：{lucky_color}"
    return "🔮 运势查询\n\n发送「运势」查看每日运势\n发送「运势 星座名」查看星座运势"

def handle_riddle(group_id: int = 0, user_id: int = 0) -> str:
    """脑筋急转弯"""
    riddle = random.choice(RIDDLES)
    # 保存用户当前题目索引，用于后续验证答案
    user_key = f"{group_id}:{user_id}"
    _RIDDLE_USER_STATE[user_key] = {
        "q_index": RIDDLES.index(riddle),
        "timestamp": __import__("time").time(),
    }
    return f"🧠 脑筋急转弯：\n\n{riddle['q']}\n\n（回复「答案」查看）"

def handle_answer_riddle(group_id: int = 0, user_id: int = 0) -> str:
    """脑筋急转弯答案"""
    import time
    user_key = f"{group_id}:{user_id}"
    state = _RIDDLE_USER_STATE.get(user_key)
    if state:
        # M18 修复：状态过期（出题超过 10 分钟）则视为新一题——题面早已随机更换，
        # 原实现返回的旧题答案驴唇不对马嘴。过期同时清理该条目。
        if time.time() - state.get("timestamp", 0) > 600:
            _RIDDLE_USER_STATE.pop(user_key, None)
            return "💡 题目已过期，请先发送「脑筋急转弯」玩一题再来看答案～"
        riddle = RIDDLES[state["q_index"]]
        return f"💡 答案：{riddle['a']}"
    # L17 修复：没有进行中的题目时不再随机给答案（原实现随机给一题答案，
    # 与题面对不上，纯误导）——提示用户先出题
    return "💡 你还没有进行中的脑筋急转弯，请先发送「脑筋急转弯」"

def handle_song(text: str) -> str:
    """点歌（text 是参数部分，不含命令）"""
    if not text.strip():
        song = random.choice(SONGS)
        return f"🎵 随机推荐：\n\n《{song['name']}》- {song['artist']}（{song['genre']}）"
    song_name = text.strip()
    matched = [s for s in SONGS if song_name in s["name"] or s["name"] in song_name]
    if matched:
        song = matched[0]
        return f"🎵 正在播放：\n\n《{song['name']}》- {song['artist']}（{song['genre']}）\n\n祝你听得开心~"
    return f"🔍 没找到「{song_name}」，试试其他歌名"

# ============ 抽卡 ============
CARD_RARITY = [
    ("SSR", 0.02),
    ("SR", 0.08),
    ("R", 0.30),
    ("N", 0.60),
]
CARD_NAMES = {
    "SSR": [
        "✨ 传说之剑·Excalibur", "🐉 远古巨龙之魂", "👑 神王之冠",
        "🌟 星辰之力", "⚡ 雷神之锤 Mjolnir", "🔮 时间沙漏",
        "🦁 圣兽·白虎", "🌊 海神之三叉戟", "🔥 凤凰之羽",
    ],
    "SR": [
        "🗡️ 骑士银剑", "🛡️ 守护之盾", "🏹 精灵之弓",
        "💎 蓝宝石护符", "🌙 月神之泪", "⭐ 流星碎片",
    ],
    "R": [
        "📜 魔法卷轴", "🧪 初级治疗药水", "🗝️ 古铜钥匙",
        "🎭 伪装面具", "🔑 地牢钥匙", "📖 基础魔法书",
    ],
    "N": [
        "🪨 普通石头", "🥖 干面包", "🧦 旧袜子",
        "🍳 平底锅", "🪵 木棍", "🧲 磁铁",
        "🎈 气球", "📎 回形针", "🍬 水果糖",
    ],
}

def handle_card(text: str) -> str:
    """抽卡（text 是参数部分，不含命令）"""
    count = 1
    if text.strip().isdigit():
        count = min(max(int(text.strip()), 1), 5)

    cards = []
    for _ in range(count):
        rand_val = random.random()
        cumulative = 0
        rarity = "N"
        for r, prob in CARD_RARITY:
            cumulative += prob
            if rand_val <= cumulative:
                rarity = r
                break

        card_name = random.choice(CARD_NAMES[rarity])
        cards.append(f"{rarity} | {card_name}")

    card_str = "\n".join(cards)

    return f"🎴 抽卡 x{count}\n{card_str}"

def handle_help() -> str:
    """帮助信息 — 重定向到分级菜单系统"""
    from core import help_menu
    return help_menu.get_first_level_menu()

# ============ 命令路由 ============
COMMANDS = {
    "dice": handle_dice,
    "rps": handle_rps,
    "card": handle_card,
    "fortune": handle_fortune,
    "riddle": handle_riddle,
    "answer": handle_answer_riddle,
    "song": handle_song,
    "help": handle_help,
}

# 别名映射
ALIASES = {
    "骰子": "dice",
    "掷骰子": "dice",
    "猜拳": "rps",
    "石头": "rps",
    "剪刀": "rps",
    "布": "rps",
    "抽卡": "card",
    "运势": "fortune",
    "今日运势": "fortune",
    "星座": "fortune",
    "急转弯": "riddle",
    "脑筋急转弯": "riddle",
    "答案": "answer",
    "点歌": "song",
    "帮助": "help",
    "菜单": "help",
}
# 注：TD_COMMANDS 和 TD_ALIASES 在 entertainment.py 中管理

