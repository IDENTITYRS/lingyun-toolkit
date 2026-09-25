"""
批量导入段落到分镜（作品级）

把用户粘贴的一整段全文，按空行（空段落）拆分成若干节，依次写入各分镜的
目标字段（默认视频提示词 video_prompt）。当段落数多于现有分镜时，用
zzdh_add_panel 自动在末尾新增分镜补足。

- UI 触发：handle_action('import', {...}, context) —— 使用宿主注入的 panels 与同步 call_tool
- AI / 工具调用：async def execute(context) —— 使用 ToolClient 异步调用宿主工具

两条路径共用 _split_sections（拆分）与 _summary（结果汇总）等纯逻辑 helper。
"""

import asyncio
import re

# field -> (tool_name, payload_key)
FIELD_TOOL = {
    "paperwork": ("zzdh_update_paperwork", "paperwork"),
    "prompt": ("zzdh_update_prompt", "prompt"),
    "video_prompt": ("zzdh_update_video_prompt", "video_prompt"),
    "negative_prompt": ("zzdh_update_negative_prompt", "negative_prompt"),
}

FIELD_LABELS = {
    "paperwork": "文案",
    "prompt": "图片提示词",
    "video_prompt": "视频提示词",
    "negative_prompt": "负面提示词",
}

DEFAULT_FIELD = "video_prompt"


def get_info():
    return {
        "name": "批量导入段落到视频提示词",
        "description": "按空行把全文拆成多段，依次写入各分镜，分镜不够时自动新增。",
    }


# ---------------------------------------------------------------------------
# 纯逻辑 helper（两条入口共用）
# ---------------------------------------------------------------------------

def _split_sections(text):
    """按空行（一行或多行空白）拆分文本，返回去掉首尾空白后的非空段落列表。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    parts = re.split(r"\n[ \t　]*\n+", normalized)
    sections = []
    for part in parts:
        cleaned = part.strip()
        if cleaned:
            sections.append(cleaned)
    return sections


def _normalize_field(field):
    field = str(field or "").strip()
    return field if field in FIELD_TOOL else DEFAULT_FIELD


def _normalize_start_index(value):
    try:
        idx = int(value)
    except Exception:
        idx = 1
    return idx if idx >= 1 else 1


def _panels_sorted(panels):
    items = [p for p in (panels or []) if isinstance(p, dict) and p.get("unique_name")]

    def _key(panel):
        try:
            return int(panel.get("panel_index") or 0)
        except Exception:
            return 0

    return sorted(items, key=_key)


def _tool_failed(result):
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return True
    return bool(result.get("error"))


def _added_unique_name(result):
    if not isinstance(result, dict):
        return None
    name = result.get("unique_name")
    if not name and isinstance(result.get("result"), dict):
        name = result["result"].get("unique_name")
    return name or None


def _summary(written, added, errors, field, sample_raw=None):
    label = FIELD_LABELS.get(field, field)
    message = f"已写入 {written} 段到「{label}」，新增分镜 {added} 个。"
    if errors:
        message += f" {len(errors)} 段失败。"
        if sample_raw is not None:
            message += f"\n后端返回示例：{sample_raw}"
    return {
        "ok": not errors,
        "message": message,
        "data": {
            "written": written,
            "added_panels": added,
            "field": field,
            "field_label": label,
            "errors": errors[:20],
            "error_total": len(errors),
            "sample_raw": sample_raw,
        },
    }


def _no_sections_result():
    return {
        "ok": False,
        "error": "没有可导入的内容：请粘贴文本，并用空行分隔每一段。",
    }


def _run_coro_sync(coro, timeout=600):
    """在同步函数（handle_action）中安全地跑完一个协程。"""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running and running.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=timeout)
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# UI 路径（同步）：handle_action
# ---------------------------------------------------------------------------

def _run_with_call_tool(call_tool, panels, sections, field, start_index):
    panels = _panels_sorted(panels)
    last_unique = panels[-1]["unique_name"] if panels else None
    tool_name, payload_key = FIELD_TOOL[field]

    written = 0
    added = 0
    errors = []
    sample_raw = None

    for offset, section in enumerate(sections):
        target_pos = (start_index - 1) + offset  # 0-based 目标分镜位置
        if target_pos < len(panels):
            unique_name = panels[target_pos]["unique_name"]
        else:
            add_args = {"after_unique_name": last_unique} if last_unique else {}
            add_result = call_tool("zzdh_add_panel", add_args)
            unique_name = _added_unique_name(add_result)
            if not unique_name:
                errors.append(f"第 {offset + 1} 段：新增分镜失败（{add_result}）")
                if sample_raw is None:
                    sample_raw = repr(add_result)
                continue
            added += 1

        write_result = call_tool(tool_name, {"unique_name": unique_name, payload_key: section})
        if _tool_failed(write_result):
            err = write_result.get("error") if isinstance(write_result, dict) else write_result
            errors.append(f"第 {offset + 1} 段：写入失败（{err}）")
            if sample_raw is None:
                sample_raw = repr(write_result)
            continue

        written += 1
        last_unique = unique_name  # 后续新增分镜接在最新分镜之后

    return _summary(written, added, errors, field, sample_raw)


def handle_action(action, data, context=None):
    if action != "import":
        return {"ok": False, "error": f"未知动作: {action}"}

    payload = data or {}
    ctx = context or {}

    sections = _split_sections(payload.get("text"))
    if not sections:
        return _no_sections_result()

    field = _normalize_field(payload.get("field"))
    start_index = _normalize_start_index(payload.get("start_index"))
    snapshot_panels = ctx.get("panels", [])

    # 注意：UI 路径注入的同步 call_tool 在本版本不支持 zzdh_update_video_prompt /
    # zzdh_add_panel（返回“不支持的工具”）。因此这里统一走 ToolClient(HTTP)，
    # 它与 execute 路径一致，支持全部内置工具。
    async def _run():
        from tool_plugins.tool_client import ToolClient
        client = ToolClient(timeout_seconds=600)
        try:
            panels = snapshot_panels
            if not _panels_sorted(panels):
                panels_result = await client.call_tool("zzdh_get_panels", {})
                panels = panels_result.get("panels", []) if isinstance(panels_result, dict) else []
            return await _run_with_tools(client, panels, sections, field, start_index)
        finally:
            await client.close()

    try:
        return _run_coro_sync(_run(), timeout=600)
    except Exception as exc:
        return {"ok": False, "error": f"执行失败: {exc}"}


# ---------------------------------------------------------------------------
# AI / 工具路径（异步）：execute
# ---------------------------------------------------------------------------

async def _run_with_tools(tools, panels, sections, field, start_index):
    panels = _panels_sorted(panels)
    last_unique = panels[-1]["unique_name"] if panels else None
    tool_name, payload_key = FIELD_TOOL[field]

    written = 0
    added = 0
    errors = []
    sample_raw = None

    for offset, section in enumerate(sections):
        target_pos = (start_index - 1) + offset
        if target_pos < len(panels):
            unique_name = panels[target_pos]["unique_name"]
        else:
            add_args = {"after_unique_name": last_unique} if last_unique else {}
            add_result = await tools.call_tool("zzdh_add_panel", add_args)
            unique_name = _added_unique_name(add_result)
            if not unique_name:
                errors.append(f"第 {offset + 1} 段：新增分镜失败（{add_result}）")
                if sample_raw is None:
                    sample_raw = repr(add_result)
                continue
            added += 1

        write_result = await tools.call_tool(tool_name, {"unique_name": unique_name, payload_key: section})
        if _tool_failed(write_result):
            err = write_result.get("error") if isinstance(write_result, dict) else write_result
            errors.append(f"第 {offset + 1} 段：写入失败（{err}）")
            if sample_raw is None:
                sample_raw = repr(write_result)
            continue

        written += 1
        last_unique = unique_name

    return _summary(written, added, errors, field, sample_raw)


async def execute(context):
    tools = context["tools"]
    args = context.get("arguments") or {}

    sections = _split_sections(args.get("text"))
    if not sections:
        return {"status": "error", "message": _no_sections_result()["error"]}

    field = _normalize_field(args.get("field"))
    start_index = _normalize_start_index(args.get("start_index"))

    panels_result = await tools.call_tool("zzdh_get_panels", {})
    panels = panels_result.get("panels", []) if isinstance(panels_result, dict) else []

    result = await _run_with_tools(tools, panels, sections, field, start_index)
    return {
        "status": "ok" if result["ok"] else "warning",
        "message": result["message"],
        **result["data"],
        "section_count": len(sections),
    }


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

async def self_test(context):
    checks = []

    # 1. 拆分逻辑：空行（含多空行、含中文全角空格行）正确分段
    sample = "第一段第一行\n第一段第二行\n\n第二段\n\n\n第三段"
    sections = _split_sections(sample)
    checks.append({
        "id": "local.split",
        "ok": sections == ["第一段第一行\n第一段第二行", "第二段", "第三段"],
        "message": "按空行拆分得到 3 段且保留段内换行",
    })

    # 2. 分发逻辑：2 个分镜 + 3 段 -> 1 次新增、3 次写入（用假 call_tool）
    calls = []

    def fake_call_tool(tool_name, arguments):
        calls.append((tool_name, arguments or {}))
        if tool_name == "zzdh_add_panel":
            return {"success": True, "unique_name": "new_panel_1", "panel_index": 3}
        return {"ok": True}

    fake_panels = [
        {"unique_name": "p1", "panel_index": 1},
        {"unique_name": "p2", "panel_index": 2},
    ]
    dist = _run_with_call_tool(fake_call_tool, fake_panels, sections, "video_prompt", 1)
    add_calls = [c for c in calls if c[0] == "zzdh_add_panel"]
    write_calls = [c for c in calls if c[0] == "zzdh_update_video_prompt"]
    checks.append({
        "id": "local.distribute",
        "ok": (
            dist.get("ok") is True
            and dist["data"]["written"] == 3
            and dist["data"]["added_panels"] == 1
            and len(add_calls) == 1
            and len(write_calls) == 3
            and write_calls[2][1].get("unique_name") == "new_panel_1"
        ),
        "message": "2 分镜 + 3 段时自动新增 1 个分镜并写入 3 段",
    })

    # 3. 宿主接口连通性：zzdh_get_panels（只读，不改动作品）
    try:
        tools = context["tools"]
        panels_result = await tools.call_tool("zzdh_get_panels", {})
        panels_ok = isinstance(panels_result, dict) and isinstance(panels_result.get("panels"), list)
        checks.append({
            "id": "host.zzdh_get_panels",
            "ok": panels_ok,
            "message": "zzdh_get_panels 返回 panels 数组" if panels_ok else "zzdh_get_panels 返回结构异常",
        })
    except Exception as exc:
        checks.append({
            "id": "host.zzdh_get_panels",
            "ok": False,
            "message": f"zzdh_get_panels 调用失败: {exc}",
        })

    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "summary": "批量导入段落自检通过" if ok else "批量导入段落自检未完全通过",
        "checks": checks,
        "artifacts": [],
    }
