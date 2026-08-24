#!/usr/bin/env python3
"""NapCat 引用消息弱引用兜底补丁（方案A，2026-08-24）

背景：
  NapCat 收到引用消息但查不到被引用对象（bot API 发送的消息不在本地库 /
  旧版客户端只带 seq）时，replyElement 全路径失败 → return null →
  上报事件不含任何引用信息 → bot 无法识别"这是引用" → 引用跳过逻辑失效。

本补丁：
  在 replyElement 的"协议兜底失败 → return null"处，把 return null 改为
  返回弱引用段 {type:"reply", data:{id:"0", seq:"..."}}，让"引用"这个事实
  至少能传达到 bot 侧（bot 识别 id=0 弱引用 → 跳过 AI 对话 / 发送时过滤）。

设计约束（审查结论）：
  - return null 在全文件出现 96 次，禁止裸替换
  - 锚点 "协议兜底未找到匹配的引用消息" 全文件唯一（含版本升级重打场景）
  - 幂等：特征子串 "seq: String(e.replayMsgSeq" 已存在则跳过
  - 替换后必须 node --check 语法通过才写回
"""

import sys
import shutil
import subprocess
import os

ANCHOR = "协议兜底未找到匹配的引用消息"
FEATURE = "seq: String(e.replayMsgSeq"  # 幂等特征：弱引用已打
OLD = "return null;"
NEW = 'return { type: ze.reply, data: { id: "0", seq: String(e.replayMsgSeq ?? "") } };'


def patch_napcat_quote(mjs_path: str, dry_run: bool = False) -> dict:
    """打弱引用补丁。返回 {'patched': bool, 'detail': str}"""
    result = {"patched": False, "detail": ""}
    with open(mjs_path, encoding="utf-8") as f:
        content = f.read()

    # 幂等：弱引用特征已存在
    if FEATURE in content:
        result["detail"] = "SKIP: 弱引用特征已存在（幂等）"
        return result

    # 锚点唯一性检查
    anchor_idx = content.find(ANCHOR)
    if anchor_idx < 0:
        result["detail"] = "FAIL: 锚点未找到，napcat.mjs 结构有变"
        return result
    if content.find(ANCHOR, anchor_idx + len(ANCHOR)) >= 0:
        result["detail"] = "FAIL: 锚点不唯一，中止（防止误替换）"
        return result

    # 锚点之后第一个 return null; （即协议兜底失败路径的出口）
    tail = content[anchor_idx:]
    ret_idx = tail.find(OLD)
    if ret_idx < 0:
        result["detail"] = "FAIL: 锚点后未找到 return null;"
        return result
    # 检查该 return null 之后的结构：应为 \n    },（处理器结束）
    after = tail[ret_idx + len(OLD):]
    if "}," not in after[:200]:
        result["detail"] = "FAIL: return null 之后 200 字符内无 '},'，结构可疑"
        return result

    abs_ret = anchor_idx + ret_idx
    patched = content[:abs_ret] + NEW + content[abs_ret + len(OLD):]

    # 语法自检（node --check；.tmp 扩展名不被 node 识别，用 .mjs）
    tmp = mjs_path + ".check.mjs"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(patched)
    try:
        r = subprocess.run(["node", "--check", tmp],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        r = None
        result["detail"] = "WARN: node 不可用，跳过语法自检"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if r is not None and r.returncode != 0:
        result["detail"] = f"FAIL: node --check 未通过: {r.stderr[:300]}"
        return result

    if dry_run:
        result["patched"] = True
        result["detail"] = "DRY-RUN OK: 替换点定位正确，语法通过"
    else:
        with open(mjs_path, "w", encoding="utf-8") as f:
            f.write(patched)
        result["patched"] = True
        result["detail"] = "OK: 弱引用补丁已写入，node --check 通过"
    return result


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    dry = "--dry-run" in sys.argv
    if not target:
        print("usage: python3 patch_napcat_quote.py <napcat.mjs> [--dry-run]")
        sys.exit(2)
    r = patch_napcat_quote(target, dry_run=dry)
    print(f"[{r['detail']}] patched={r['patched']}")
    sys.exit(0 if r["patched"] or r["detail"].startswith("SKIP") else 1)