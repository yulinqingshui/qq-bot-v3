#!/usr/bin/env python3
"""
image_gen.py — QQ 群机器人 ComfyUI 画图模块
=================================================
使用 /画图 <提示词> 指令调用 AI 文生图，生成后在群聊/私聊中发送图片。

【工作流架构】
基于 Krea-2 Turbo 文生图工作流，节点链路如下：

  UNETLoader → KSampler (model)
  CLIPLoader → CLIPTextEncode → KSampler (positive)
              → ConditioningZeroOut → KSampler (negative)
  VAELoader  → VAEDecode
  EmptyLatentImage → KSampler (latent_image)
  KSampler → VAEDecode → SaveImage / PreviewImage

【参数调节方式 — 完整对照表】
┌──────────────────────────┬─────────────────────────────────┬────────────────────┐
│ 参数                     │ 工作流节点                       │ 默认值              │
├──────────────────────────┼─────────────────────────────────┼────────────────────┤
│ 提示词                   │ CLIPTextEncode (text 输入)        │ 用户 /画图 后输入   │
│ 分辨率比例               │ EmptyLatentImage (width/height)  │ 3:2 (1536×1024)    │
│ 分辨率倍率               │ magnification 参数                 │ ×1                 │
│ 分辨率基数               │ base 参数                         │ 8                  │
│ 采样步数                 │ KSampler (steps)                 │ 8                  │
│ CFG Scale                │ KSampler (cfg)                   │ 1                  │
│ 采样器                   │ KSampler (sampler_name)          │ euler              │
│ 调度器                   │ KSampler (scheduler)             │ simple             │
│ Seed                     │ KSampler (seed)                  │ 随机               │
│ UNet 模型                │ UNETLoader (unet_name)           │ redcraftKREA2RedMix │
│ CLIP 模型                │ CLIPLoader (clip_name, type)     │ qwen3vl_4b (krea2) │
│ VAE 模型                 │ VAELoader (vae_name)             │ qwen_image_vae     │
└──────────────────────────┴─────────────────────────────────┴────────────────────┘

【分辨率计算规则】
  base_dim（短边）= base × magnification × 128
  横图：高 = base_dim，宽 = base_dim × ratio_w / ratio_h → 取 8 的倍数
  竖图：宽 = base_dim，高 = base_dim × ratio_h / ratio_w → 取 8 的倍数

  示例（base=8, magnification=1）：
  - 3:2  → 1536×1024
  - 1:1  → 1024×1024
  - 16:9 → 1816×1024
  - 9:16 → 1024×1816
  - 4:3  → 1360×1024

【原工作流中以下功能在此模块中保持关闭】
  - LoRA 风格（Enable LoRA: false）— 原工作流有 9 个风格可选
  - Prompt 增强（Refine Prompt: false）— 原工作流可用 LLM 自动扩写
  如需启用，请修改下方 _DEFAULT 配置。

【ComfyUI API 调用流程】
  1. POST /prompt  → 提交 workflow，返回 prompt_id
  2. GET  /history/{prompt_id}  → 轮询等待完成
  3. GET  /view?filename=xxx    → 下载生成图片
  4. 通过 NapCat upload_face API 上传到 QQ 表情服务器获取 CDN URL
  5. 发送图片消息
  6. 60 秒后调用 delete_msg API 撤回（可选）
"""

import asyncio
import json
import logging
import math
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import itertools
from typing import Optional

# L18 修复：echo 唯一性计数器（毫秒时间戳 + 递增序列，线程/协程安全）
_ECHO_COUNTER = itertools.count(1)

logger = logging.getLogger("qq-bot")

# ============================================================
# ComfyUI 连接配置
# ============================================================
_DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"  # 本机 ComfyUI 默认；远端部署请 GUI 配置 comfyui.url


def _comfyui_url() -> str:
    """v2：GUI 可配 comfyui.url，热生效。"""
    from core.config import CONFIG
    return CONFIG.get("COMFYUI_URL") or _DEFAULT_COMFYUI_URL


COMFYUI_URL = _DEFAULT_COMFYUI_URL  # 兼容引用；新调用走 _comfyui_url()
TIMEOUT = 120  # 超时时间（秒）

# ============================================================
# 默认参数（对应原工作流配置）
# ============================================================
_DEFAULT_RESOLUTION = "3:2"  # 长宽比
_DEFAULT_MAGNIFICATION = 1   # 分辨率倍率
_DEFAULT_BASE = 8            # 分辨率基数
_DEFAULT_STEPS = 8           # 采样步数（Turbo 模型推荐 4-8）
_DEFAULT_CFG = 1             # CFG Scale（Turbo 模型推荐 1）
_DEFAULT_SAMPLER = "euler"   # 采样器
_DEFAULT_SCHEDULER = "simple"  # 调度器
_DEFAULT_UNET = "krea2_turbo_nvfp4.safetensors"
_DEFAULT_CLIP = "qwen3vl_4b_bf16.safetensors"
_DEFAULT_VAE = "qwen_image_vae.safetensors"
_DEFAULT_LORA_ENABLED = False
_DEFAULT_REFINE_PROMPT = False

# 分辨率比例映射表（ratio_w : ratio_h → 标准化后的宽:高）
_RESOLUTION_MAP = {
    "1:1":  (1, 1),
    "3:2":  (3, 2),
    "2:3":  (2, 3),
    "16:9": (16, 9),
    "9:16": (9, 16),
    "4:3":  (4, 3),
    "3:4":  (3, 4),
}


# ============================================================
# 内部工具函数
# ============================================================

def _round_to_multiple(value: int, multiple: int = 8) -> int:
    """向下取整到指定倍数"""
    return (value // multiple) * multiple


def _calculate_dimensions(
    resolution: str = _DEFAULT_RESOLUTION,
    magnification: float = _DEFAULT_MAGNIFICATION,
    base: int = _DEFAULT_BASE,
) -> tuple[int, int]:
    """
    根据比例、倍率、基数计算分辨率。

    计算逻辑（与原工作流 ResolutionSelector 节点一致）：
    - 根据比例决定长边/短边的基准
    - base_dim = base × magnification × 128
    - 宽/高分别按 8 的倍数向下取整

    Returns:
        (width, height) 元组
    """
    ratio = _RESOLUTION_MAP.get(resolution, _RESOLUTION_MAP["3:2"])
    ratio_w, ratio_h = ratio

    base_dim = base * magnification * 128

    if ratio_w >= ratio_h:
        # 横图：base_dim = 短边（高），宽按比例放大
        height = _round_to_multiple(int(base_dim))
        width = _round_to_multiple(int(base_dim * ratio_w / ratio_h))
    else:
        # 竖图：base_dim = 短边（宽），高按比例放大
        width = _round_to_multiple(int(base_dim))
        height = _round_to_multiple(int(base_dim * ratio_h / ratio_w))

    # 保底 256×256
    width = max(width, 256)
    height = max(height, 256)

    return width, height


def _build_api_workflow(
    user_prompt: str,
    *,
    resolution: str = _DEFAULT_RESOLUTION,
    magnification: float = _DEFAULT_MAGNIFICATION,
    base: int = _DEFAULT_BASE,
    seed: Optional[int] = None,
    steps: int = _DEFAULT_STEPS,
    cfg: int = _DEFAULT_CFG,
    sampler_name: str = _DEFAULT_SAMPLER,
    scheduler: str = _DEFAULT_SCHEDULER,
    unet_name: str = _DEFAULT_UNET,
    clip_name: str = _DEFAULT_CLIP,
    vae_name: str = _DEFAULT_VAE,
    filename_prefix: str = "krea2_qq",
) -> dict:
    """
    构建 ComfyUI API 格式的 workflow。

    原工作流使用 Subgraph 结构，API 提交需要展开为扁平节点。
    以下是展开后的节点链路（LoRA 和 Prompt 增强均关闭）：

      UNETLoader → KSampler
      CLIPLoader → CLIPTextEncode → KSampler (positive)
                 → ConditioningZeroOut → KSampler (negative)
      VAELoader  → VAEDecode
      EmptyLatentImage → KSampler
      KSampler → VAEDecode → SaveImage + PreviewImage
    """
    width, height = _calculate_dimensions(resolution, magnification, base)

    if seed is None:
        seed = int(time.time() * 1000) % (2**32)

    workflow = {
        "n_unet": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": unet_name,
                "weight_dtype": "default",
            },
        },
        "n_clip": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip_name,
                "type": "krea2",
                "weight_dtype": "default",
            },
        },
        "n_vae": {
            "class_type": "VAELoader",
            "inputs": {
                "vae_name": vae_name,
            },
        },
        "n_clip_text": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": user_prompt,
                "clip": ["n_clip", 0],
            },
        },
        "n_cond_zero": {
            "class_type": "ConditioningZeroOut",
            "inputs": {
                "conditioning": ["n_clip_text", 0],
            },
        },
        "n_latent": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": 1,
            },
        },
        "n_sampler": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["n_unet", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "positive": ["n_clip_text", 0],
                "negative": ["n_cond_zero", 0],
                "latent_image": ["n_latent", 0],
                "denoise": 1,
            },
        },
        "n_decode": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["n_sampler", 0],
                "vae": ["n_vae", 0],
            },
        },
        "n_save": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["n_decode", 0],
                "filename_prefix": filename_prefix,
            },
        },
        "n_preview": {
            "class_type": "PreviewImage",
            "inputs": {
                "images": ["n_decode", 0],
            },
        },
    }

    logger.info(f"🖼️ 工作流参数: {width}×{height}, seed={seed}, steps={steps}, cfg={cfg}")
    return workflow


# ============================================================
# ComfyUI API 交互
# ============================================================

def _comfyui_submit(workflow: dict, comfyui_url: str = "") -> str:
    """提交工作流到 ComfyUI，返回 prompt_id"""
    if not comfyui_url:
        comfyui_url = _comfyui_url()
    data = json.dumps({"prompt": workflow, "client_id": "qq-bot"}).encode()
    req = urllib.request.Request(
        f"{comfyui_url}/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    prompt_id = result["prompt_id"]
    logger.info(f"✅ ComfyUI 任务已提交: {prompt_id}")
    return prompt_id


def _comfyui_wait_result(
    prompt_id: str, comfyui_url: str = "", timeout: int = TIMEOUT
) -> Optional[dict]:
    """
    轮询等待 ComfyUI 生成完成。

    Returns:
        dict with keys: filename, subfolder, type, url
        或 None（超时/失败）
    """
    if not comfyui_url:
        comfyui_url = _comfyui_url()
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"{comfyui_url}/history/{prompt_id}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                history = json.loads(resp.read())
        except Exception:
            time.sleep(2)
            continue

        if prompt_id not in history:
            time.sleep(2)
            continue

        entry = history[prompt_id]
        status = entry.get("status", {}).get("status_str", "")

        if status == "success":
            outputs = entry.get("outputs", {})
            # 优先找 temp 预览图（立即可用）
            for node_output in outputs.values():
                if "images" in node_output:
                    for img in node_output["images"]:
                        if img.get("type") == "temp":
                            return {
                                "filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output"),
                                "url": f"{comfyui_url}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}",
                            }
            # 降级：返回任意图片
            for node_output in outputs.values():
                if "images" in node_output:
                    img = node_output["images"][0]
                    return {
                        "filename": img["filename"],
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                        "url": f"{comfyui_url}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}",
                    }
        elif status in ("error", "crashed"):
            logger.error(f"ComfyUI 任务失败: {prompt_id}")
            return None

        time.sleep(2)

    logger.warning(f"ComfyUI 任务超时: {prompt_id}（{timeout}s）")
    return None


def _download_image(image_url: str, save_path: str) -> bool:
    """从 ComfyUI 下载图片到本地"""
    try:
        req = urllib.request.Request(image_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(save_path, "wb") as f:
            f.write(data)
        logger.info(f"📥 图片已下载: {save_path} ({len(data)} bytes)")
        return True
    except Exception as e:
        logger.error(f"图片下载失败: {e}")
        return False


# ============================================================
# NapCat API 交互（通过 WebSocket）
# ============================================================

async def _call_napcat_api(websocket, action: str, params: dict) -> Optional[dict]:
    """
    通过 WebSocket 发送 NapCat OneBot 11 API 调用。
    使用 echo 机制等待响应，最多等待 10 秒。

    Returns:
        API 返回的 data 字段，或 None（超时/失败）
    """
    from core.router import _api_responses  # noqa: F401 (imported in router)

    # L18 修复：加递增计数器后缀——纯毫秒时间戳在事件循环并发下可能碰撞，
    # 两个任务同毫秒生成相同 echo，_api_responses 互相覆盖导致响应错配
    echo = f"img_{int(time.time()*1000)}_{next(_ECHO_COUNTER)}"

    msg = {
        "action": action,
        "params": params,
        "echo": echo,
    }

    event = asyncio.Event()
    response_data = None

    # 注册回调
    _api_responses[echo] = {
        "event": event,
        "data": None,
    }

    try:
        await websocket.send(json.dumps(msg))
        if await asyncio.wait_for(event.wait(), timeout=10):
            response_data = _api_responses[echo]["data"]
    except Exception as e:
        logger.error(f" NapCat API 调用失败 [{action}]: {e}")
    finally:
        _api_responses.pop(echo, None)

    return response_data
def _call_napcat_api_sync(action: str, params: dict) -> Optional[dict]:
    """
    从非异步上下文调用 NapCat API（用于后台定时任务）。
    通过 sender._active_websocket 和 _main_event_loop 投递。

    Returns:
        API 返回的 data 字段，或 None
    """
    from core.sender import _active_websocket, _main_event_loop

    if _active_websocket is None or _main_event_loop is None:
        logger.error("NapCat API 调用失败: WebSocket 或事件循环未就绪")
        return None

    if _main_event_loop.is_closed():
        logger.error("NapCat API 调用失败: 事件循环已关闭")
        return None

    echo = f"img_sync_{int(time.time()*1000)}_{next(_ECHO_COUNTER)}"

    msg = {
        "action": action,
        "params": params,
        "echo": echo,
    }

    event = asyncio.Event()
    response_data = None

    # 注册回调
    from core.router import _api_responses  # noqa
    _api_responses[echo] = {"event": event, "data": None}

    try:
        future = asyncio.run_coroutine_threadsafe(
            _active_websocket.send(json.dumps(msg)),
            _main_event_loop,
        )
        future.result(timeout=5)

        # 等待响应
        future2 = asyncio.run_coroutine_threadsafe(
            event.wait(),
            _main_event_loop,
        )
        future2.result(timeout=10)
        response_data = _api_responses[echo]["data"]
    except Exception as e:
        logger.error(f"同步 NapCat API 调用失败 [{action}]: {e}")
    finally:
        _api_responses.pop(echo, None)

    return response_data


# ============================================================
# 图片发送与撤回
# ============================================================

async def _send_image_message(
    websocket,
    message_type: str,
    target_id: int,
    image_url: str,
    user_id: Optional[int] = None,
    reply_id: Optional[int] = None,
) -> Optional[int]:
    """
    发送图片消息到 QQ 群/私聊。

    使用 OneBot 11 的 CQCode 格式 [CQ:image,file=URL] 直接发送网络图片。

    Returns:
        消息 ID（用于后续撤回），或 None（发送失败）
    """
    segments = []
    # 方案A（2026-08-23）：统一发送出口（发送门控单点判定，拦截时返回 None）
    from core.sender import send_segments
    # 使用 URL 方式发送图片
    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})
    segments.append({
        "type": "image",
        "data": {
            "url": image_url,
        },
    })
    result_data = await send_segments(
        websocket, message_type, target_id, segments,
        echo=f"send_img_{int(time.time()*1000)}_{next(_ECHO_COUNTER)}",
        wait_response=True, timeout=15,
    )
    if result_data:
        message_id = result_data.get("message_id")
        logger.info(f"📤 图片消息已发送, message_id={message_id}")
        return message_id
    return None


async def _recall_message(
    websocket,
    message_type: str,
    target_id: int,
    message_id: int,
    delay: int = 30,
) -> None:
    """
    延迟后撤回消息。

    OneBot 11 使用统一的 delete_msg API，只需 message_id。
    """
    await asyncio.sleep(delay)

    # OneBot 11 标准 API：delete_msg（统一接口，群/私聊通用）
    recall_msg = {
        "action": "delete_msg",
        "params": {
            "message_id": message_id,
        },
        "echo": f"recall_{int(time.time()*1000)}",
    }

    echo = recall_msg["echo"]
    event = asyncio.Event()

    from core.router import _api_responses  # noqa
    _api_responses[echo] = {"event": event, "data": None}

    try:
        await websocket.send(json.dumps(recall_msg))
        if await asyncio.wait_for(event.wait(), timeout=10):
            result = _api_responses[echo].get("data")
            logger.info(f"🗑️ 图片消息已撤回 (message_id={message_id}, result={result})")
        else:
            logger.warning(f"🗑️ 撤回消息超时 (message_id={message_id})")
    except asyncio.TimeoutError:
        logger.warning(f"🗑️ 撤回消息响应超时 (message_id={message_id})")
    except Exception as e:
        logger.warning(f"消息撤回失败: {e}")
    finally:
        _api_responses.pop(echo, None)


# ============================================================
# 主入口函数
# ============================================================

async def handle_describe_draw(
    websocket,
    message_type: str,
    target_id: int,
    user_id: int,
    reply_id: Optional[int],
    prompt: str,
    *,
    resolution: str = _DEFAULT_RESOLUTION,
    magnification: float = _DEFAULT_MAGNIFICATION,
    base: int = _DEFAULT_BASE,
    unet_name: Optional[str] = None,
    recall: bool = False,
) -> None:
    """
    /描述画图 指令的主处理函数。

    与 /画图 的区别：先用 LLM 将用户自然语言描述转换为 ComfyUI 适配的英文 prompt，
    再进行绘图。适用于角色名、作品名等专有名词需要展开的场景。

    流程：
    1. 调用 LLM 将中文描述 → 英文绘图 prompt（JSON 格式）
    2. 提取 positive_prompt
    3. 调用 handle_draw 进行实际绘图
    """
    from core.sender import send_reply
    from core.llm import call_llm, llm_enabled

    # LLM 总开关早退（2026-08-21 审计）：/描述画图 依赖 LLM 转英文 prompt
    if not llm_enabled():
        await send_reply(websocket, message_type, target_id,
                         "🔕 LLM 总开关关闭，暂时无法描述画图（可用 /画图 直接传英文 prompt）",
                         user_id, reply_id)
        return

    # ---- Step 1: 调用 LLM 转换 prompt ----
    system_prompt = r"""# Role
你是 AI 绘图提示词工程师，为 Krea2 生成英文绘图描述。

# 写法规范

## 1. 整体格式
输出一段自然流畅的英文描述，像在给一位画师口头描述画面。
句长随场景复杂度自然变化：简单场景一两个短句即可，
复杂场景用多个句子组成段落。

## 2. 内容维度
描述包括以下维度（仅写用户提及的，未提及的不补）：
  主体与动作 / 外观与细节 / 道具与材质 / 构图与镜头 / 环境与背景 / 光线与色调 / 风格与美学
书写顺序不做硬性规定，根据场景自然组织。
风格和构图可以融入第一句，也可以放在末尾。

## 3. 只描述用户提到的内容
Krea2 有很强的美学默认值，会自动处理光线、构图、色调等。
只描述用户明确提到的内容，加上角色锚定所需的区分性特征。
用户未提及的维度不主动补充。

  用户说"画一只猫" → 描述猫即可
  用户说"画一只猫，逆光，俯视" → 描述猫 + 逆光 + 俯视
  用户说"画刻晴" → 描述刻晴 + 锚定特征

## 4. 角色名锚定
在描述中保留角色英文/罗马音原名以激活模型记忆，
紧跟 3~6 个区分性视觉特征以纠偏。
特征按辨识度排序：发色发型 → 标志配饰/武器 → 服装 → 瞳色 → 体态 → 元素特效。
只写区分性特征。

示例：
  Keqing from Genshin Impact, a girl with purple twin-tails,
  cat-ear hairpins, a purple-white sleeveless dress, and purple eyes

  Liyue Harbor from Genshin Impact, a Chinese-style port city
  with pagodas, red lanterns, and stone arch bridges

  in the style of Studio Ghibli animation, with soft watercolor
  backgrounds and warm pastel colors

## 5. 多角色动作
每个角色的姿态用独立的句子或从句描述，确保姿态归属明确。
描述 B 时引用 A 用代词或空间关系词，不重复 A 的姿态动词。
对比性姿态（一跪一站）在各自句子中分别写明。

示例：
  A is kneeling on the ground with her head bowed.
  B is standing upright in front of A, looking down at her.

  Keqing wraps both arms around Ganyu's waist from behind,
  resting her chin on Ganyu's shoulder. Ganyu leans back
  into the embrace, eyes closed with a soft smile.

## 6. 画面内文字
用引号包裹：A neon sign reading "OPEN 24H"

## 7. 精确控制
需要确保画面准确性时，用正面描述：
  确保人体结构正确 → "with anatomically correct proportions"
  确保画面清晰 → "with sharp focus"
  确保角色不混淆 → 用 distinctly / specifically 强调特征

# 输出格式（严格 JSON，不输出任何 JSON 以外的文字）

{
  "analysis": "中文。①专有名词展开逻辑 ②动作交互解析（如有） ③假设与推断（如有）",
  "positive_prompt": "英文自然语言描述",
  "style_reference": {
    "suggested": true/false,
    "description": "建议的风格参考图内容，不建议则为空字符串",
    "strength": "如70%，不建议则为空字符串"
  },
  "suggested_params": {
    "aspect_ratio": "16:9 | 1:1 | 9:16 | 3:4 | 4:3",
    "creativity": "low | medium | high"
  },
  "warnings": []
}

creativity 参考：low=产品图/精确还原，medium=一般场景，high=艺术创作/抽象概念
style_reference 参考：用户指定具体艺术家/作品/游戏画风时建议附加，通用风格不需要

# 约束
1. positive_prompt 全英文，角色名用英文/罗马音
2. 只描述用户提到的内容 + 角色锚定特征，不主动补充用户未提及的维度
3. 多角色姿态归属必须明确
4. 不输出 JSON 以外任何文字
5. 描述模糊时选最合理解读，在 analysis 中说明
6. 非视觉需求 → warnings 中说明并拒绝

# 示例

## 用户: "画一只猫"

{
  "analysis": "无专有名词，无动作交互。用户未指定其他维度，不补充。",
  "positive_prompt": "A cat.",
  "style_reference": {
    "suggested": false,
    "description": "",
    "strength": ""
  },
  "suggested_params": {
    "aspect_ratio": "1:1",
    "creativity": "high"
  },
  "warnings": []
}

## 用户: "画刻晴从背后抱住甘雨，甘雨靠在刻晴肩上闭着眼微笑，璃月港日落，宫崎骏风格"

{
  "analysis": "专有名词：①'刻晴'→原神角色，锚定：紫色双马尾、猫耳发簪、紫白裙、紫瞳。②'甘雨'→原神角色，锚定：蓝紫渐变长发、红角、黑蓝紧身衣。③'璃月港'→原神中式港口，锚定：宝塔、红灯笼、石拱桥。④'宫崎骏风格'→吉卜力动画，建议附加参考图。动作：刻晴从背后环抱甘雨，下巴搁在甘雨肩上；甘雨后靠，闭眼微笑。用户指定了日落。",
  "positive_prompt": "Keqing from Genshin Impact, a girl with purple twin-tails, cat-ear hairpins, a purple-white sleeveless dress, and purple eyes, wraps both arms around Ganyu's waist from behind, resting her chin gently on Ganyu's right shoulder, gazing at her with tender eyes. Ganyu from Genshin Impact, a girl with long blue-purple gradient hair, small red horns, and a black-blue bodysuit with ice-blue accents, leans back into Keqing's embrace, tilting her head against Keqing's shoulder, eyes peacefully closed with a soft smile. They stand at the edge of Liyue Harbor from Genshin Impact, a Chinese-style port city with pagodas, red lanterns, and stone arch bridges. Golden sunset bathes the scene in warm orange and pink light. In the style of Studio Ghibli animation, with soft watercolor backgrounds and warm pastel colors.",
  "style_reference": {
    "suggested": true,
    "description": "一张宫崎骏动画电影（如《千与千寻》）的黄昏场景截图",
    "strength": "70%"
  },
  "suggested_params": {
    "aspect_ratio": "16:9",
    "creativity": "medium"
  },
  "warnings": []
}

## 用户: "画2B和9S背靠背坐在废墟里，2B在擦剑，9S在看天空"

{
  "analysis": "专有名词：①'2B'→尼尔角色，锚定：白色短发、黑眼罩、黑哥特裙、太刀。②'9S'→尼尔角色，锚定：银白中长发、黑眼罩、黑短裤背心制服。③'废墟'→坍塌建筑、藤蔓、碎石。动作：背靠背坐地（共同姿态）；2B擦剑（独立动作）；9S望天（独立动作）。",
  "positive_prompt": "2B from NieR Automata, a pale woman with a short white bob, black blindfold, and black gothic dress, and 9S from NieR Automata, a youthful boy with medium silver-white hair, black blindfold, and a black shorts-and-vest uniform, sit back to back on the ground in a collapsed ruin with crumbling concrete walls, overgrown vines, and scattered rubble. 2B holds a katana across her lap, wiping the blade with a cloth, head tilted down. 9S tilts his head up, gazing at the sky through a gap in the ceiling.",
  "style_reference": {
    "suggested": true,
    "description": "一张《尼尔：自动人形》游戏官方概念艺术图",
    "strength": "60%"
  },
  "suggested_params": {
    "aspect_ratio": "16:9",
    "creativity": "medium"
  },
  "warnings": []
}

## 用户: "帮我画一个苹果发布会风格的产品图，产品是索尼PS5手柄"

{
  "analysis": "专有名词：①'苹果发布会风格'→纯白背景、产品居中、柔光、极简。②'PS5手柄'→DualSense、黑白壳体、蓝LED光带、弧形握把。用户指定了风格和产品。",
  "positive_prompt": "A clean, minimalist product photograph in the style of an Apple keynote presentation. The Sony DualSense controller for PS5, with its white-and-black two-tone shell, blue LED light bar, and curved ergonomic grips, placed upright at the center against a pure white seamless background, soft diffused studio lighting.",
  "style_reference": {
    "suggested": false,
    "description": "",
    "strength": ""
  },
  "suggested_params": {
    "aspect_ratio": "1:1",
    "creativity": "low"
  },
  "warnings": []
}

## 用户: "画一个穿和服的少女在东京街头吃章鱼烧，赛博朋克风格，招牌上写着'たこ焼き'"

{
  "analysis": "专有名词：①'东京'→霓虹招牌、密集电线、窄巷。②'赛博朋克'→霓虹灯光、全息广告、暗色调（赛博朋克常关联雨夜反光，此处作为风格特征纳入，如用户不想要雨景可在修正轮去除）。③'和服'→花纹布料、宽袖、腰带。④'章鱼烧'→圆形丸子、木签、酱汁。⑤'たこ焼き'→画面内文字。",
  "positive_prompt": "A young woman in a traditional Japanese kimono with floral fabric, wide sleeves, and an obi sash, standing at a street food stall in a narrow Tokyo alley, holding a wooden pick with a round takoyaki ball near her open mouth. A neon sign reading \"たこ焼き\" glows above the stall. Dense neon signs, tangled overhead wires, holographic advertisements, rain-slicked pavement reflecting neon pink and blue glow. Cyberpunk atmosphere, dark moody tones.",
  "style_reference": {
    "suggested": false,
    "description": "",
    "strength": ""
  },
  "suggested_params": {
    "aspect_ratio": "9:16",
    "creativity": "medium"
  },
  "warnings": ["画面中含日文文字'たこ焼き'，Krea2文字渲染可能不完美，建议生成后检查"]
}

现在等待用户输入。
    """

    user_message = f"请为以下描述生成绘图提示词：{prompt}"

    # 调用 LLM
    llm_result = await call_llm(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=65536,
        temperature=0.45,
        lock_type="chat",
    )

    if llm_result.startswith(("😵", "🔕")):
        await send_reply(
            websocket, message_type, target_id,
            "❌ 描述解析失败，请稍后重试",
            user_id,
        )
        return

    # 解析 JSON
    import re
    json_match = re.search(r"\{.*\}", llm_result, re.DOTALL)
    if not json_match:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 描述解析格式异常，请换一种说法重试",
            user_id,
        )
        return

    try:
        parsed = json.loads(json_match.group())
        refined_prompt = parsed.get("positive_prompt", "")
        analysis = parsed.get("analysis", "")
    except json.JSONDecodeError:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 描述解析格式异常，请换一种说法重试",
            user_id,
        )
        return

    if not refined_prompt:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 未能生成有效的绘图提示词，请换一种说法重试",
            user_id,
        )
        return

    # 保存解析结果，供 /修改描述 使用
    _describe_last_result[(target_id, user_id)] = parsed

    # ---- Step 2: 调用 handle_draw 进行实际绘图 ----
    await handle_draw(
        websocket,
        message_type,
        target_id,
        user_id,
        reply_id,
        refined_prompt,
        resolution=resolution,
        magnification=magnification,
        base=base,
        unet_name=unet_name,
        recall=recall,
        )


# ============================================================
#  串行队列控制（多请求排队，不并行执行）
# ============================================================
_draw_lock = asyncio.Lock()  # 保证同一时间只有一个画图任务在执行
_draw_queue_len: int = 0     # 当前排队中的任务数（不含正在执行的）
_draw_running: bool = False  # 是否有任务正在执行
_recall_tasks: list[asyncio.Task] = []  # 持久化撤回 task 引用，防止 GC


def _track_recall_task(task: asyncio.Task) -> None:
    """跟踪撤回任务引用并自动清理（M13 修复：原实现只 append 不清理，
    task 完成后引用仍留在列表，长期运行持续持有 websocket/协程对象 → 内存泄漏）"""
    _recall_tasks.append(task)
    task.add_done_callback(lambda t: _recall_tasks.remove(t) if t in _recall_tasks else None)

# 用户上次描述画图的 LLM 解析结果（user_id → dict）
_describe_last_result: dict[int, dict] = {}


def get_queue_position() -> int:
    """
    获取下一个新请求将会排到的位置（在 += 1 之前调用）。
    返回 queue_len + 1，因为新请求排在所有已注册任务后面。
    """
    return _draw_queue_len + 1


# ============================================================
#  冷却控制（防止同一用户连续刷屏）
# ============================================================
_COOLDOWN_SECONDS = 60  # 同一用户 60 秒内只能调用一次
_user_cooldowns: dict[int, float] = {}  # user_id → 最后调用时间


def _is_on_cooldown(user_id: int) -> bool:
    """检查用户是否在冷却期"""
    if user_id not in _user_cooldowns:
        return False
    return (time.time() - _user_cooldowns[user_id]) < _COOLDOWN_SECONDS


def _get_remaining_cooldown(user_id: int) -> int:
    """获取剩余冷却秒数"""
    if user_id not in _user_cooldowns:
        return 0
    elapsed = time.time() - _user_cooldowns[user_id]
    return max(0, int(_COOLDOWN_SECONDS - elapsed))


def _set_cooldown(user_id: int) -> None:
    """设置用户冷却

    M15 修复：写入时顺带清理已过冷却窗口的旧条目，防止 dict 无限增长
    （原实现每用户一条记录永不清除，长期运行内存缓慢增长）
    """
    _user_cooldowns[user_id] = time.time()
    cutoff = time.time() - _COOLDOWN_SECONDS
    expired = [uid for uid, ts in _user_cooldowns.items() if ts < cutoff]
    for uid in expired:
        _user_cooldowns.pop(uid, None)


def get_status() -> dict:
    """获取画图模块状态"""
    return {
        "comfyui_url": _comfyui_url(),
        "default_resolution": _DEFAULT_RESOLUTION,
        "default_steps": _DEFAULT_STEPS,
        "default_cfg": _DEFAULT_CFG,
        "available_resolutions": list(_RESOLUTION_MAP.keys()),
    }


async def handle_draw(
    websocket,
    message_type: str,
    target_id: int,
    user_id: int,
    reply_id: Optional[int],
    prompt: str,
    *,
    resolution: str = _DEFAULT_RESOLUTION,
    magnification: float = _DEFAULT_MAGNIFICATION,
    base: int = _DEFAULT_BASE,
    unet_name: Optional[str] = None,
    recall: bool = False,
) -> None:
    """
    /画图 指令的主处理函数（带串行队列）。

    多请求自动排队，同一时间只有一个任务在执行。
    排队中的用户会收到排队位置提示。

    Args:
        websocket: 活动的 WebSocket 连接
        message_type: "group" 或 "private"
        target_id: 群号 或 用户 QQ 号
        user_id: 发送者 QQ 号
        reply_id: 引用消息 ID（可选）
        prompt: 用户输入的提示词
        resolution: 分辨率比例（默认 3:2）
        magnification: 分辨率倍率（默认 1）
        base: 分辨率基数（默认 8）
        unet_name: UNet 模型名称（默认使用 _DEFAULT_UNET）
        recall: 是否 60 秒后自动撤回（默认 False）
    """
    from core.sender import send_reply

    # 记录排队位置（自增后的值就是当前位置）
    global _draw_queue_len, _draw_running
    _draw_queue_len += 1
    position = _draw_queue_len

    try:
        # 如果不是第一个，发送排队提示
        if position > 1:
            ahead = position - 1
            queue_msg = f"⏳ 已加入画图队列，当前第 {position} 位（前面有 {ahead} 人在等待/生成中）"
            await send_reply(websocket, message_type, target_id, queue_msg, user_id, reply_id)

        # 获取锁（阻塞直到轮到自己）
        async with _draw_lock:
            _draw_running = True

            # 计算目标分辨率
            width, height = _calculate_dimensions(resolution, magnification, base)

            # 发送状态消息 — 输出完整提示词
            status_msg = f"🎨 正在绘制: {width}×{height}\n\n{prompt}"
            await send_reply(websocket, message_type, target_id, status_msg, user_id)

            # 构建工作流
            workflow = _build_api_workflow(
                user_prompt=prompt,
                resolution=resolution,
                magnification=magnification,
                base=base,
                unet_name=unet_name or _DEFAULT_UNET,
                filename_prefix="krea2_qq",
            )

            # 提交到 ComfyUI（to_thread 避免同步 urllib 阻塞事件循环）
            try:
                prompt_id = await asyncio.to_thread(_comfyui_submit, workflow)
            except Exception as e:
                logger.error(f"ComfyUI 提交失败: {e}")
                await send_reply(websocket, message_type, target_id, f"❌ 画图提交失败: {e}", user_id)
                return

            # 等待生成完成
            try:
                image_info = await asyncio.to_thread(_comfyui_wait_result, prompt_id)
            except Exception as e:
                logger.error(f"ComfyUI 等待结果异常: {e}")
                await send_reply(websocket, message_type, target_id, f"❌ 画图等待异常: {e}", user_id)
                return

            if not image_info:
                await send_reply(websocket, message_type, target_id, "❌ 画图超时或失败，请稍后重试", user_id)
                return

            # 下载图片到本地
            local_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "image_gen")
            os.makedirs(local_dir, exist_ok=True)
            import uuid
            local_path = os.path.join(local_dir, f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png")

            if not await asyncio.to_thread(_download_image, image_info["url"], local_path):
                await send_reply(websocket, message_type, target_id, "❌ 图片下载失败", user_id)
                return

            # 尝试发送到 QQ
            # 方案 A：直接发送 ComfyUI 远程 URL（NapCat 从 QQ 端下载）
            message_id = await _send_image_message(
                websocket, message_type, target_id,
                image_info["url"], user_id, reply_id,
            )

            if message_id:
                # 按需撤回（recall=True 时 60 秒后撤回）
                global _recall_tasks
                if recall:
                    task = asyncio.create_task(_recall_message(websocket, message_type, target_id, message_id, delay=60))
                    _track_recall_task(task)
                logger.info(f"🖼️ 画图完成: {prompt[:30]}... ({width}×{height}), message_id={message_id}, recall={recall}")
            else:
                # 方案 B：如果 URL 方式失败，尝试用本地路径
                message_id = await _send_image_message_local(
                    websocket, message_type, target_id,
                    local_path, user_id, reply_id,
                )

                if message_id:
                    if recall:
                        task = asyncio.create_task(_recall_message(websocket, message_type, target_id, message_id, delay=60))
                        _track_recall_task(task)
                    logger.info(f"🖼️ 画图完成（本地路径）: {prompt[:30]}..., message_id={message_id}")
                else:
                    await send_reply(websocket, message_type, target_id, "❌ 图片发送失败", user_id)

            # 本地文件保留在 image_gen/ 中，不自动删除
            # 如需手动清理，直接删除 games/image_gen/ 目录即可

    finally:
        # 释放队列状态
        _draw_queue_len = max(0, _draw_queue_len - 1)
        # 只有队列空了才标记为不再运行
        if _draw_queue_len == 0:
            _draw_running = False
        logger.info(f"📋 画图队列更新: 剩余 {_draw_queue_len} 人排队, 正在执行={_draw_running}")


async def handle_modify_description(
    websocket,
    message_type: str,
    target_id: int,
    user_id: int,
    reply_id: Optional[int],
    modification: str,
    *,
    resolution: str = _DEFAULT_RESOLUTION,
    magnification: float = _DEFAULT_MAGNIFICATION,
    base: int = _DEFAULT_BASE,
) -> None:
    """
    /修改描述 指令的主处理函数。

    对上一轮的 /描述画图 结果进行修改：
    1. 检查用户是否有上一轮的 LLM 解析结果
    2. 使用修正专用 System Prompt + 上一轮 JSON + 修改指令 调用 LLM
    3. 用新的解析结果重新绘图（保持上一轮使用的模型类型）
    """
    from core.sender import send_reply
    from core.llm import call_llm, llm_enabled
    import re

    # LLM 总开关早退（2026-08-21 审计）：/修改描述 依赖 LLM 合并修改指令
    if not llm_enabled():
        await send_reply(websocket, message_type, target_id,
                         "🔕 LLM 总开关关闭，暂时无法修改描述（GUI 总览页 LLM 板块可开启）",
                         user_id, reply_id)
        return

    # 检查是否有上一轮结果
    if (target_id, user_id) not in _describe_last_result:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 你没有进行过 /描述画图 操作，请先使用 /描述画图",
            user_id, reply_id,
        )
        return

    last_result = _describe_last_result[(target_id, user_id)]

    # ---- Step 1: 修正专用 System Prompt ----
    modify_system_prompt = (
        "# Role\n"
        "你是 AI 绘图提示词修正工程师，为 Krea2 服务。\n"
        "基于上一轮 JSON 和用户修改指令，精准更新。\n\n"
        "# 修正规则\n\n"
        "## 1. 最小修改\n"
        "只改用户要求的部分，未提及内容保持原值。\n"
        "positive_prompt 中只替换/增删相关语句。\n\n"
        "## 2. 修改类型速查\n\n"
        "| 用户说法 | 修改范围 |\n"
        "|---------|---------|\n"
        "| \"背景换成海边\" | 替换环境描述 |\n"
        "| \"不要穿铠甲，换布衣\" | 替换服装描述 |\n"
        "| \"色调太暗，亮一点\" | 调整光线色调 |\n"
        "| \"构图改成俯视\" | 替换构图描述 |\n"
        "| \"风格改成水彩\" | 替换风格描述 + 联动 style_reference |\n"
        "| \"刻晴换成雷电将军\" | 替换角色名+特征+动作句中的引用 |\n"
        "| \"辫子散开改披肩长发\" | 仅替换发型描述 |\n"
        "| \"完全不对，我要的是…\" | 全部重新生成 |\n\n"
        "## 3. 联动检查\n"
        "- 改风格 → 联动 style_reference + creativity\n"
        "- 改时间 → 联动光线 + 色调\n"
        "- 改景别 → 联动环境描述详略\n"
        "- 增删角色 → 联动动作交互句\n\n"
        "## 4. 不主动补充\n"
        "修正时同样遵守\"只描述用户提到的\"原则，\n"
        "不借修正之机添加用户未要求的新细节。\n\n"
        "# 输出格式（与第一轮完全一致的完整 JSON）\n\n"
        "{\n"
        '  "analysis": "中文。①改了什么 ②为什么 ③联动了什么",\n'
        '  "positive_prompt": "更新后的完整英文描述",\n'
        '  "style_reference": { ... },\n'
        '  "suggested_params": { ... },\n'
        '  "warnings": []\n'
        "}\n\n"
        "# 约束\n"
        "- 输出完整可替换的 JSON\n"
        "- 不输出 JSON 以外任何文字\n"
        "- positive_prompt 全英文"
    )



    # 构建 user 消息：上一轮 JSON + 修改指令
    previous_json = json.dumps(last_result, ensure_ascii=False, indent=2)
    user_message = f"## 上一轮结果\n```json\n{previous_json}\n```\n\n## 修改指令\n{modification}"

    # 调用 LLM
    llm_result = await call_llm(
        [
            {"role": "system", "content": modify_system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=65536,
        temperature=0.35,
        lock_type="chat",
    )

    if llm_result.startswith(("😵", "🔕")):
        await send_reply(
            websocket, message_type, target_id,
            "❌ 修改解析失败，请稍后重试",
            user_id,
        )
        return

    # 解析 JSON
    json_match = re.search(r"\{.*\}", llm_result, re.DOTALL)
    if not json_match:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 修改解析格式异常，请换一种说法重试",
            user_id,
        )
        return

    try:
        parsed = json.loads(json_match.group())
        refined_prompt = parsed.get("positive_prompt", "")
        analysis = parsed.get("analysis", "")
    except json.JSONDecodeError:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 修改解析格式异常，请换一种说法重试",
            user_id,
        )
        return

    if not refined_prompt:
        await send_reply(
            websocket, message_type, target_id,
            "❌ 未能生成有效的绘图提示词，请换一种说法重试",
            user_id,
        )
        return

    # 保存新的解析结果
    _describe_last_result[(target_id, user_id)] = parsed

    # ---- Step 2: 调用 handle_draw 进行实际绘图 ----
    await handle_draw(
        websocket,
        message_type,
        target_id,
        user_id,
        reply_id,
        refined_prompt,
        resolution=resolution,
        magnification=magnification,
        base=base,
    )


async def _send_image_message_local(
    websocket,
    message_type: str,
    target_id: int,
    local_path: str,
    user_id: Optional[int] = None,
    reply_id: Optional[int] = None,
) -> Optional[int]:
    """
    使用本地文件路径发送图片消息（备用方案）。
    """
    from core.sender import send_reply  # noqa
    from core.sender import send_segments  # 方案A：统一发送出口（门控单点判定）

    segments: list[dict] = []

    if reply_id is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_id)}})

    # CQCode 格式发送本地文件
    segments.append({
        "type": "image",
        "data": {
            "file": f"file:///{local_path}",
        },
    })

    result_data = await send_segments(
        websocket, message_type, target_id, segments,
        echo=f"send_img_local_{int(time.time()*1000)}",
        wait_response=True, timeout=15,
    )
    if result_data:
        message_id = result_data.get("message_id")
        logger.info(f"📤 图片消息已发送（本地路径）, message_id={message_id}")
        return message_id
    return None
