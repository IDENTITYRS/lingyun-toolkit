# -*- coding: utf-8 -*-
"""
一键映射提示词到左侧图片（prompt_reference_mapper · 作品级）

场景：用户手动把已推理好的图片/视频提示词粘贴进分镜，跳过了宿主的"推理"动作，
因此推理收尾的自动选中（人物/场景/道具 → 左侧参考图）没有发生。

实现方式（重要）：
本插件**不**使用宿主的 `zzdh_trigger_post_infer`——它对名字做朴素子串匹配，
会把"阿建"这种短名误命中在"阿建的父亲"里，造成乱选。

本插件自己算匹配：读取项目角色/场景/道具实体，对每个分镜提示词做
**最长名字优先 + 命中即抹除**的匹配（先匹配"阿建的父亲"并把这几个字从文本挖掉，
"阿建"在剩余文本里就不会再命中），再用 `zzdh_set_panel_entity_selections`
直接写回选中结果。宿主会把实体的历史候选图也展开到参考项，因此写回后
移除已匹配实体的非主图，只保留资产管理中当前选中的主图。

- 不重新生成提示词、不改文案、不生成图片、不删除内容。
- 核心逻辑在 async def execute(context)，UI 通过 handle_action -> ToolClient -> execute 复用同一套逻辑。
"""

import asyncio
import concurrent.futures
import ntpath
import re


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _to_int(value, default):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def _text(value):
    return str(value if value is not None else "").strip()


def _bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value in ("", None):
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "否", "不"}
    return bool(value)


def _normalize_args(raw):
    raw = raw or {}
    match_mode = _text(raw.get("match_mode")).lower()
    if match_mode not in {"full", "specified"}:
        match_mode = "full"
    return {
        "start_index": max(0, _to_int(raw.get("start_index"), 0)),
        "end_index": max(0, _to_int(raw.get("end_index"), 0)),
        "require_prompt": _bool(raw.get("require_prompt"), True),
        "match_mode": match_mode,
        "match_keywords": _split_match_keywords(raw.get("match_keywords")),
    }


def _split_match_keywords(raw):
    """把指定匹配词按顿号、逗号、分号或换行拆分并去重。"""
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = re.split(r"[、,，;；\r\n]+", _text(raw))
    keywords = []
    for value in values:
        keyword = _text(value)
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def _panel_index(panel, fallback):
    return _to_int(panel.get("panel_index") or panel.get("viewer_index") or panel.get("index"), fallback)


def _panel_text(panel):
    """用于匹配的文本：只取当前分镜自身的提示词字段。

    不读取 processed_prompt —— 宿主生成图片时会把前后分镜的角色/场景
    信息注入 processed_prompt，导致匹配到不属于本页的实体。仅使用用户
    原始填写的 prompt 和 video_prompt，确保匹配范围限定在当前页面。
    """
    parts = []
    for key in ("prompt", "video_prompt"):
        value = _text(panel.get(key))
        if value:
            parts.append(value)
    return "\n".join(parts)


def _keyword_lines(text, keywords):
    """仅保留包含任一指定词的原文行，行内仍使用完整匹配规则。"""
    if not keywords:
        return ""
    folded_keywords = [keyword.casefold() for keyword in keywords]
    lines = []
    for line in text.splitlines():
        folded_line = line.casefold()
        if any(keyword in folded_line for keyword in folded_keywords):
            lines.append(line)
    return "\n".join(lines)


def _matching_text(text, args):
    if args["match_mode"] == "specified":
        return _keyword_lines(text, args["match_keywords"])
    return text


def _has_prompt(panel):
    return bool(_text(panel.get("prompt")) or _text(panel.get("video_prompt")))


# ---------------------------------------------------------------------------
# 实体名匹配（最长优先 + 命中即抹除）
# ---------------------------------------------------------------------------

ENTITY_KINDS = ("character", "location", "item")
_ID_KEYS = {"character": "character_id", "location": "location_id", "item": "item_id"}
_SEL_KEYS = {"character": "character_ids", "location": "location_ids", "item": "item_ids"}
_KIND_LABELS = {"character": "角色", "location": "场景", "item": "道具"}


def _split_aliases(raw):
    """把名称/别名按常见分隔符切成 token，长度 >= 2 才保留。"""
    tokens = []
    raw = _text(raw)
    if not raw:
        return tokens
    for token in re.split(r"[,，、/|;；\s]+", raw):
        token = token.strip()
        if len(token) >= 2 and token not in tokens:
            tokens.append(token)
    if len(raw) >= 2 and raw not in tokens:
        tokens.append(raw)
    return tokens


def _entity_tokens(entity, kind):
    """返回 (entity_id, [token, ...])。token 来自 name + alias。"""
    entity_id = _text(entity.get(_ID_KEYS[kind]) or entity.get("id"))
    if not entity_id:
        return None
    tokens = []
    for token in _split_aliases(entity.get("name")):
        if token not in tokens:
            tokens.append(token)
    for token in _split_aliases(entity.get("alias")):
        if token not in tokens:
            tokens.append(token)
    return entity_id, tokens


def _mask_quoted_text(text):
    """屏蔽中文/英文双引号中的内容，避免对白中的名字参与匹配。"""
    if not text:
        return text

    chars = list(text)
    quote_end = None
    for index, char in enumerate(chars):
        if quote_end is not None:
            # 保留原长度，避免引号两侧的文字被拼接成新的实体名。
            chars[index] = " "
            if char == quote_end:
                quote_end = None
            continue
        if char == "“":
            chars[index] = " "
            quote_end = "”"
        elif char == '"':
            chars[index] = " "
            quote_end = '"'
    return "".join(chars)


def _match_kind(text, entities, kind):
    """
    对某一类实体做匹配，返回 [(entity_id, name)]。
    忽略中英文双引号中的内容；最长 token 优先匹配；命中后把这段文本抹成等长占位符，
    避免短名字（如"阿建"）误命中在长名字（如"阿建的父亲"）内部。
    """
    pairs = []  # (token, entity_id, name)
    for entity in entities or []:
        info = _entity_tokens(entity, kind)
        if not info:
            continue
        entity_id, tokens = info
        name = _text(entity.get("name")) or entity_id
        for token in tokens:
            pairs.append((token, entity_id, name))
    # 长 token 先匹配
    pairs.sort(key=lambda p: len(p[0]), reverse=True)

    working = _mask_quoted_text(text)
    selected = []  # entity_id
    names = {}
    for token, entity_id, name in pairs:
        if token in working:
            # 无论是否已选，都把命中段抹掉，防止更短的名字钻空子
            working = working.replace(token, "　" * len(token))
            if entity_id not in selected:
                selected.append(entity_id)
                names[entity_id] = name
    return [(eid, names[eid]) for eid in selected]


def _image_path_key(path):
    """宿主返回 Windows 绝对路径，统一斜杠和大小写后比较。"""
    path = _text(path)
    return ntpath.normcase(ntpath.normpath(path)) if path else ""


def _entity_image_paths(entity):
    """返回当前主图及所有图片路径；候选顺序沿用宿主，不按文件时间猜测。"""
    paths = []
    primary = _text(entity.get("image_path"))
    if primary:
        paths.append(primary)
    for item in entity.get("reference_items") or []:
        if isinstance(item, dict) and item.get("media_type") == "image":
            path = _text(item.get("path"))
            if path:
                paths.append(path)
    for item in entity.get("image_candidates") or []:
        if isinstance(item, dict) and _text(item.get("path")):
            paths.append(_text(item["path"]))
    paths.extend(_text(path) for path in entity.get("extra_images") or [] if _text(path))
    return primary or (paths[0] if paths else ""), paths


def _historical_image_keys(matched, entities):
    primary_keys = set()
    candidate_keys = set()
    for kind in ENTITY_KINDS:
        selected = {eid for eid, _name in matched[kind]}
        for entity in entities[kind]:
            entity_id = _text(entity.get(_ID_KEYS[kind]) or entity.get("id"))
            if entity_id not in selected:
                continue
            primary, paths = _entity_image_paths(entity)
            if primary:
                primary_keys.add(_image_path_key(primary))
                candidate_keys.update(_image_path_key(path) for path in paths)
    # 某张历史图也可能是另一个已匹配资产的主图，此时仍须保留。
    return candidate_keys - primary_keys


async def _remove_historical_images(tools, unique_name, historical_keys):
    if not historical_keys:
        return 0
    result = await tools.call_tool(
        "zzdh_get_edit_view_data", {"unique_name": unique_name}, timeout=45,
    )
    # 完整编辑数据中的 reference_images 是宿主解析后的绝对路径列表。
    data = result.get("data", result) if isinstance(result, dict) else None
    references = data.get("reference_images") if isinstance(data, dict) else None
    if not isinstance(references, list):
        raise RuntimeError("无法读取分镜参考项，未能移除历史候选图，请重新匹配。")
    kept = []
    removed = 0
    for item in references:
        if not isinstance(item, dict) or not _text(item.get("path")):
            raise RuntimeError("分镜参考项格式异常，未能移除历史候选图，请重新匹配。")
        if item.get("media_type", "image") == "image" and _image_path_key(item["path"]) in historical_keys:
            removed += 1
        else:
            kept.append(dict(item))
    if removed:
        saved = await tools.call_tool(
            "zzdh_save_reference_items", {"unique_name": unique_name, "items": kept}, timeout=45,
        )
        if isinstance(saved, dict) and "saved" in saved and saved["saved"] != len(kept):
            raise RuntimeError("宿主未完整保存参考项，请检查素材路径后重新匹配。")
    return removed


# ---------------------------------------------------------------------------
# 工具调用门面
# ---------------------------------------------------------------------------

def _tool_failed(result):
    if not isinstance(result, dict):
        return False
    if result.get("success") is False or result.get("ok") is False:
        return True
    return bool(result.get("error"))


class _ToolFacade:
    def __init__(self, context):
        self._async_tools = context.get("tools")
        self._sync_call = context.get("call_tool")

    async def call_tool(self, name, arguments=None, timeout=60, allow_error=False):
        arguments = arguments or {}
        if self._async_tools is not None:
            result = await asyncio.wait_for(self._async_tools.call_tool(name, arguments), timeout=timeout)
        elif self._sync_call is not None:
            result = await asyncio.to_thread(self._sync_call, name, arguments)
        else:
            raise RuntimeError("当前运行环境没有可用的工具调用能力")
        if not allow_error and _tool_failed(result):
            raise RuntimeError(_text(result.get("error")) or f"{name} 调用失败")
        return result


def _context_panels(context):
    panels = context.get("panels")
    if isinstance(panels, list):
        return [p for p in panels if isinstance(p, dict)]
    return []


async def _load_entities(tools):
    entities = {}
    for kind in ENTITY_KINDS:
        result = await tools.call_tool("zzdh_get_entity_list", {"entity_type": kind}, timeout=45)
        raw = result.get("entities") if isinstance(result, dict) else []
        entities[kind] = [e for e in (raw or []) if isinstance(e, dict)]
    return entities


def _select_targets(panels, args):
    start = args["start_index"] or 1
    end = args["end_index"]
    targets = []
    for ordinal, panel in enumerate(panels or [], start=1):
        if not isinstance(panel, dict) or not _text(panel.get("unique_name")):
            continue
        number = _panel_index(panel, ordinal)
        if number < start:
            continue
        if end > 0 and number > end:
            continue
        if args["require_prompt"] and not _has_prompt(panel):
            continue
        targets.append(panel)
    return targets


# ---------------------------------------------------------------------------
# 核心业务
# ---------------------------------------------------------------------------

async def execute(context):
    args = _normalize_args(context.get("arguments"))
    if args["match_mode"] == "specified" and not args["match_keywords"]:
        raise ValueError("指定匹配模式请至少填写一个匹配词。")
    tools = _ToolFacade(context)
    show_message = context.get("show_message")
    update_progress = context.get("update_progress")
    clear_progress = context.get("clear_progress")
    logs = []

    def log(message):
        logs.append(message)

    def progress(percent, text):
        if callable(update_progress):
            update_progress(percent, text, "primary")

    def message(text, kind="info"):
        if callable(show_message):
            show_message(text, kind)

    progress(3, "读取角色/场景/道具")
    log("读取项目实体列表...")
    entities = await _load_entities(tools)
    log(f"实体：角色 {len(entities['character'])} / 场景 {len(entities['location'])} / 道具 {len(entities['item'])}。")

    progress(6, "读取分镜")
    panels = _context_panels(context)
    if not panels:
        panels_result = await tools.call_tool("zzdh_get_panels", {}, timeout=45)
        panels = panels_result.get("panels", []) if isinstance(panels_result, dict) else []
    log(f"读取到 {len(panels)} 个分镜。")

    targets = _select_targets(panels, args)
    range_label = f"{args['start_index'] or 1}-{args['end_index'] or '末尾'}"
    mode_label = "全文匹配" if args["match_mode"] == "full" else "指定匹配（" + "、".join(args["match_keywords"]) + "）"
    log(f"匹配模式：{mode_label}。")
    log(f"范围 {range_label} 内需映射的分镜：{len(targets)} 个。")

    if not targets:
        if callable(clear_progress):
            clear_progress()
        msg = "范围内没有可映射的分镜（可能都没有提示词，或序号范围为空）。"
        message(msg, "warning")
        return _summary("warning", msg, len(panels), [], [], range_label, logs)

    success = []
    failed = []
    total = len(targets)

    for i, panel in enumerate(targets, start=1):
        unique_name = _text(panel.get("unique_name"))
        panel_index = _panel_index(panel, i)
        progress(8 + int(i / total * 88), f"映射分镜 {i}/{total}")
        text = _matching_text(_panel_text(panel), args)
        try:
            matched = {kind: _match_kind(text, entities[kind], kind) for kind in ENTITY_KINDS}
            selection = {_SEL_KEYS[kind]: [eid for eid, _name in matched[kind]] for kind in ENTITY_KINDS}
            await tools.call_tool(
                "zzdh_set_panel_entity_selections",
                {"unique_name": unique_name, **selection},
                timeout=45,
            )
            removed = await _remove_historical_images(
                tools, unique_name, _historical_image_keys(matched, entities),
            )
            picked = {
                _KIND_LABELS[kind]: [name for _eid, name in matched[kind]]
                for kind in ENTITY_KINDS
            }
            success.append({
                "panel_index": panel_index,
                "unique_name": unique_name,
                "character_count": len(matched["character"]),
                "location_count": len(matched["location"]),
                "item_count": len(matched["item"]),
                "removed_history_count": removed,
                "picked": picked,
            })
            log(
                f"({i}/{total}) 分镜 {panel_index}："
                f"角色[{ '、'.join(picked['角色']) or '无' }] "
                f"场景[{ '、'.join(picked['场景']) or '无' }] "
                f"道具[{ '、'.join(picked['道具']) or '无' }]"
                + (f"；已排除 {removed} 张历史候选图" if removed else "")
            )
        except Exception as exc:
            error = _text(getattr(exc, "message", "")) or str(exc)
            failed.append({"panel_index": panel_index, "unique_name": unique_name, "error": error})
            log(f"({i}/{total}) 分镜 {panel_index} 写入失败：{error}")

    progress(100, "映射完成")
    if callable(clear_progress):
        clear_progress()

    status = "ok" if not failed else ("partial" if success else "error")
    msg = f"映射完成：成功 {len(success)} 个 / 失败 {len(failed)} 个（共 {total} 个）。"
    message(msg, "success" if status == "ok" else "warning")
    return _summary(status, msg, len(panels), success, failed, range_label, logs)


def _summary(status, msg, total_panels, success, failed, range_label, logs):
    return {
        "status": status,
        "message": msg,
        "total_panels": total_panels,
        "target_count": len(success) + len(failed),
        "success_count": len(success),
        "failed_count": len(failed),
        "panel_range": range_label,
        "success": success,
        "failed": failed,
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# UI 入口
# ---------------------------------------------------------------------------

def handle_action(action, data, context=None):
    if action != "run":
        return {"ok": False, "error": f"未知动作: {action}"}

    async def _run():
        from tool_plugins.tool_client import ToolClient

        client = ToolClient()
        try:
            runtime_context = {
                "tools": client,
                "arguments": data or {},
                "plugin_info": {
                    "name": "prompt_reference_mapper",
                    "display_name": "一键映射提示词到左侧图片",
                    "scope": "project",
                },
            }
            if context:
                for key in ("panels", "show_message", "update_progress", "clear_progress"):
                    if context.get(key) is not None:
                        runtime_context[key] = context[key]
            return await execute(runtime_context)
        finally:
            await client.close()

    timeout = max(60, min(1800, _to_int((data or {}).get("timeout_seconds"), 600)))
    try:
        return {"ok": True, "data": _run_coro_sync(_run(), timeout=timeout)}
    except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
        return {"ok": False, "error": "映射执行超时，请缩小序号范围后重试。"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_info():
    return {
        "name": "一键映射提示词到左侧图片",
        "description": "按全文或包含指定词的文字行匹配角色/场景/道具名称（最长优先、避免短名误命中）并写入左侧选中。",
    }


def _run_coro_sync(coro, timeout=600):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=timeout)
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

class _SelfTestTools:
    def __init__(self):
        self.calls = []
        self.references = [
            {"path": "C:/self-test/father-current.png", "media_type": "image"},
            {"path": "C:/self-test/father-old.png", "media_type": "image"},
        ]

    async def call_tool(self, name, arguments=None):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "zzdh_get_entity_list":
            kind = arguments.get("entity_type")
            if kind == "character":
                return {"entities": [
                    {"character_id": "c_aj", "name": "阿建"},
                    {"character_id": "c_fq", "name": "阿建的父亲",
                     "image_path": "C:/self-test/father-current.png",
                     "extra_images": ["C:/self-test/father-old.png"]},
                    {"character_id": "c_ls", "name": "李婶"},
                    {"character_id": "c_hy", "name": "海音", "alias": "音丫头"},
                    {"character_id": "c_xl", "name": "小丽"},
                    {"character_id": "c_xm", "name": "小明"},
                ]}
            if kind == "location":
                return {"entities": [
                    {"location_id": "l_yz", "name": "福海村村委会大院"},
                    {"location_id": "l_qz", "name": "福海村村委会大院签字桌前"},
                ]}
            return {"entities": []}
        if name == "zzdh_get_panels":
            return {"panels": [{
                "panel_index": 1, "unique_name": "p1",
                "prompt": "福海村村委会大院里，阿建的父亲笑了，李婶哭了。本节人物清单：李婶、阿建的父亲。小丽说：“小明今天去砍柴了”。小丽说:\"小明又回来了\"",
                "video_prompt": "",
            }]}
        if name == "zzdh_set_panel_entity_selections":
            return {"success": True}
        if name == "zzdh_get_edit_view_data":
            return {"reference_images": self.references}
        if name == "zzdh_save_reference_items":
            self.references = list(arguments["items"])
            return {"success": True, "saved": len(self.references)}
        return {"success": False, "error": f"unexpected tool: {name}"}


async def self_test(context):
    tools = _SelfTestTools()
    result = await execute({"tools": tools, "arguments": {"start_index": 1, "end_index": 0}})
    set_call = next((a for n, a in tools.calls if n == "zzdh_set_panel_entity_selections"), {})
    char_ids = set(set_call.get("character_ids") or [])
    loc_ids = set(set_call.get("location_ids") or [])

    checks = [
        {"id": "loads_entities", "ok": any(n == "zzdh_get_entity_list" for n, _ in tools.calls),
         "message": "读取了角色/场景/道具实体"},
        {"id": "longest_first_excludes_short", "ok": "c_aj" not in char_ids,
         "message": "‘阿建’未被‘阿建的父亲’误命中（最长优先生效）"},
        {"id": "matches_father", "ok": "c_fq" in char_ids,
         "message": "‘阿建的父亲’正确命中"},
        {"id": "matches_lishen", "ok": "c_ls" in char_ids,
         "message": "‘李婶’正确命中"},
        {"id": "ignores_quoted_name", "ok": "c_xl" in char_ids and "c_xm" not in char_ids,
         "message": "忽略中英文双引号对白中的‘小明’，仍命中引号外的‘小丽’"},
        {"id": "alias_not_oversensitive", "ok": "c_hy" not in char_ids,
         "message": "‘海音/音丫头’未误命中"},
        {"id": "matches_location", "ok": "l_yz" in loc_ids,
         "message": "场景‘福海村村委会大院’命中"},
        {"id": "writes_selection", "ok": bool(set_call) and tools.references == [
            {"path": "C:/self-test/father-current.png", "media_type": "image"}],
         "message": "写回选中并排除历史候选图，仅保留当前主图"},
        {"id": "status_ok", "ok": result.get("status") == "ok",
         "message": "流程返回 ok"},
    ]
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "summary": "一键映射提示词到左侧图片 自检通过" if ok else "一键映射提示词到左侧图片 自检失败",
        "checks": checks,
        "artifacts": [],
    }
