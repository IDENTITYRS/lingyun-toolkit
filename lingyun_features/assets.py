"""TypeTale project plugin: batch-import local asset images.

The plugin deliberately uses the host entity APIs for project writes.  The host
therefore remains responsible for copying media into the project, maintaining
characters.json/locations.json/items.json, and refreshing the asset managers.
"""

import ctypes
import glob
import ntpath
from ctypes import wintypes


PLUGIN_NAME = "batch_asset_image_importer"
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ENTITY_ORDER = ("character", "location", "item")
ENTITY_META = {
    "character": {
        "label": "角色",
        "folders": {"角色", "人物", "character", "characters", "role", "roles"},
    },
    "location": {
        "label": "场景",
        "folders": {"场景", "地点", "location", "locations", "scene", "scenes"},
    },
    "item": {
        "label": "道具",
        "folders": {"物品", "道具", "item", "items", "prop", "props", "object", "objects"},
    },
}


def get_info():
    return {
        "name": "批量导入资产图片",
        "description": "按角色、场景、物品目录批量导入图片，并使用文件名创建资产。",
    }


def handle_action(action, data, context=None):
    """Synchronous iframe bridge required by the TypeTale PluginSDK."""
    payload = data if isinstance(data, dict) else {}
    ctx = context or {}
    try:
        if action == "choose_folder":
            folder_path = _choose_folder(payload.get("initial_dir"))
            return {
                "ok": True,
                "cancelled": not bool(folder_path),
                "data": {"folder_path": folder_path},
            }
        if action == "scan_assets":
            result = _preview_assets(payload.get("folder_path"), ctx)
            return {"ok": True, "message": result["message"], "data": result}
        if action == "import_assets":
            result = _import_assets(
                payload.get("folder_path"),
                payload.get("entity_types"),
                ctx,
            )
            ok = result.get("failed", 0) == 0
            return {
                "ok": ok,
                "message": result["message"],
                "error": "" if ok else result["message"],
                "data": result,
            }
        return {"ok": False, "error": "未知动作: " + str(action)}
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _show_message(ctx, message, "error")
        return {"ok": False, "error": message}


def _choose_folder(initial_dir=None):
    """Open a native Windows folder picker from the managed plugin backend.

    The plugin iframe is cross-origin, so recent Electron versions intentionally
    hide absolute paths from ``<input webkitdirectory>``.  Using the Windows
    shell picker here returns only the directory explicitly chosen by the user.
    """
    if not hasattr(ctypes, "windll"):
        raise RuntimeError("当前系统不支持本地文件夹选择器，请直接填写文件夹路径。")

    initial = str(initial_dir or "").strip().strip('"').strip("'")
    if initial:
        initial = ntpath.normpath(initial)

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.LPARAM,
        wintypes.LPARAM,
    )

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", callback_type),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    initial_buffer = ctypes.create_unicode_buffer(initial or "")

    @callback_type
    def browse_callback(hwnd, message, _param, _data):
        if message == 1 and initial:
            user32.SendMessageW(
                hwnd,
                0x467,
                1,
                ctypes.cast(initial_buffer, ctypes.c_void_p).value,
            )
        return 0

    owner = user32.FindWindowW(None, "字字动画")
    display_name = ctypes.create_unicode_buffer(260)
    browse_info = BrowseInfo()
    browse_info.hwndOwner = owner
    browse_info.pidlRoot = None
    browse_info.pszDisplayName = ctypes.cast(display_name, wintypes.LPWSTR)
    browse_info.lpszTitle = "选择角色、场景、物品资产总文件夹"
    browse_info.ulFlags = 0x0001 | 0x0010 | 0x0040
    browse_info.lpfn = browse_callback
    browse_info.lParam = 0
    browse_info.iImage = 0
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = wintypes.LONG

    ole_result = ole32.OleInitialize(None)
    pidl = None
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
        if not pidl:
            return ""
        selected = ctypes.create_unicode_buffer(32768)
        if not shell32.SHGetPathFromIDListW(pidl, selected):
            raise RuntimeError("无法读取所选文件夹路径。")
        return ntpath.normpath(selected.value)
    finally:
        if pidl:
            ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        if ole_result >= 0:
            ole32.OleUninitialize()


def _preview_assets(folder_path, context, scan_override=None):
    scan = scan_override or _scan_folder(folder_path)
    existing = _load_existing(context)
    annotated = _annotate_scan(scan, existing)
    counts = annotated["counts"]
    message = (
        "扫描完成：角色 {character} 张，场景 {location} 张，道具 {item} 张；"
        "可导入 {importable} 张，已存在 {existing} 张。"
    ).format(
        character=counts["character"],
        location=counts["location"],
        item=counts["item"],
        importable=annotated["importable_count"],
        existing=annotated["existing_count"],
    )
    annotated["message"] = message
    _show_message(context, message, "info")
    return annotated


def _import_assets(folder_path, entity_types, context, scan_override=None):
    selected = _normalize_selected_types(entity_types)
    if not selected:
        raise ValueError("请至少勾选一种资产类型。")

    scan = scan_override or _scan_folder(folder_path)
    existing = _load_existing(context)
    annotated = _annotate_scan(scan, existing)
    queues = {
        kind: [entry for entry in annotated["categories"][kind]["files"] if entry["status"] == "ready"]
        for kind in selected
    }
    requested = sum(len(entries) for entries in queues.values())
    skipped_existing = sum(
        annotated["categories"][kind]["existing_count"] for kind in selected
    )

    if requested == 0:
        message = "没有需要导入的新资产；所选图片均已存在或目录中没有对应图片。"
        result = {
            "status": "ok",
            "message": message,
            "requested": 0,
            "created": 0,
            "failed": 0,
            "skipped_existing": skipped_existing,
            "categories": _empty_import_categories(),
            "errors": [],
            "scan": annotated,
        }
        _show_message(context, message, "info")
        return result

    call_tool = _require_call_tool(context)
    category_results = _empty_import_categories()
    errors = []
    completed = 0

    for kind in ENTITY_ORDER:
        if kind not in selected:
            continue
        entries = queues.get(kind, [])
        for start in range(0, len(entries), 100):
            chunk = entries[start : start + 100]
            entities = [
                {
                    "entity_type": kind,
                    "name": entry["name"],
                    "description": "",
                    "image_path": entry["path"],
                }
                for entry in chunk
            ]
            _progress(
                context,
                int(completed * 100 / max(requested, 1)),
                "正在导入{0} {1}/{2}".format(
                    ENTITY_META[kind]["label"], completed + 1, requested
                ),
            )
            try:
                raw_result = call_tool("zzdh_batch_create_entity", {"entities": entities})
                summary = _parse_batch_result(raw_result, chunk)
            except Exception as exc:
                summary = {
                    "created": 0,
                    "failed": len(chunk),
                    "errors": [
                        {
                            "entity_type": kind,
                            "name": entry["name"],
                            "path": entry["path"],
                            "error": str(exc) or exc.__class__.__name__,
                        }
                        for entry in chunk
                    ],
                }

            category_results[kind]["requested"] += len(chunk)
            category_results[kind]["created"] += summary["created"]
            category_results[kind]["failed"] += summary["failed"]
            errors.extend(summary["errors"])
            completed += len(chunk)

    created = sum(item["created"] for item in category_results.values())
    failed = sum(item["failed"] for item in category_results.values())
    if failed:
        message = "导入完成：成功 {0} 个，失败 {1} 个，跳过已存在 {2} 个。".format(
            created, failed, skipped_existing
        )
        status = "partial" if created else "error"
        message_type = "warning" if created else "error"
    else:
        message = "导入完成：成功 {0} 个，跳过已存在 {1} 个。".format(
            created, skipped_existing
        )
        status = "ok"
        message_type = "success"

    _progress(context, 100, message)
    _show_message(context, message, message_type)
    return {
        "status": status,
        "message": message,
        "requested": requested,
        "created": created,
        "failed": failed,
        "skipped_existing": skipped_existing,
        "categories": category_results,
        "errors": errors[:100],
        "scan": annotated,
    }


def _scan_folder(folder_path, candidate_paths=None, signature_checker=None):
    root = _clean_folder_path(folder_path)
    checker = signature_checker or _has_supported_signature

    if candidate_paths is None:
        escaped = glob.escape(root)
        if not glob.glob(escaped):
            raise ValueError("所选文件夹不存在或当前进程无法访问：" + root)
        candidate_paths = glob.iglob(ntpath.join(escaped, "**", "*"), recursive=True)

    categories = {
        kind: {
            "entity_type": kind,
            "label": ENTITY_META[kind]["label"],
            "files": [],
            "found_count": 0,
            "unique_count": 0,
            "source_duplicate_count": 0,
        }
        for kind in ENTITY_ORDER
    }
    seen_names = {kind: {} for kind in ENTITY_ORDER}
    source_duplicates = []
    invalid_count = 0
    uncategorized_count = 0
    supported_file_seen = False

    paths = sorted((str(path) for path in candidate_paths), key=lambda value: value.casefold())
    for raw_path in paths:
        path = ntpath.normpath(raw_path)
        extension = ntpath.splitext(path)[1].casefold()
        if extension not in SUPPORTED_EXTENSIONS:
            continue
        supported_file_seen = True
        kind = _kind_for_path(root, path)
        if not kind:
            uncategorized_count += 1
            continue
        if not checker(path, extension):
            invalid_count += 1
            continue

        name = ntpath.splitext(ntpath.basename(path))[0].strip()
        if not name:
            invalid_count += 1
            continue

        categories[kind]["found_count"] += 1
        name_key = _name_key(name)
        previous = seen_names[kind].get(name_key)
        if previous:
            categories[kind]["source_duplicate_count"] += 1
            source_duplicates.append(
                {
                    "entity_type": kind,
                    "name": name,
                    "path": path,
                    "kept_path": previous,
                }
            )
            continue
        seen_names[kind][name_key] = path
        categories[kind]["files"].append(
            {"entity_type": kind, "name": name, "path": path}
        )

    recognized_total = sum(item["found_count"] for item in categories.values())
    if not recognized_total:
        if supported_file_seen and uncategorized_count:
            raise ValueError(
                "找到了图片，但没有识别到分类目录。请把图片放入“角色”“场景”“物品（或道具）”子文件夹。"
            )
        raise ValueError(
            "没有找到可导入图片。支持 PNG、JPG、JPEG、WEBP、BMP，目录名应为角色、场景、物品或道具。"
        )

    for item in categories.values():
        item["unique_count"] = len(item["files"])

    return {
        "root": root,
        "categories": categories,
        "counts": {kind: categories[kind]["found_count"] for kind in ENTITY_ORDER},
        "found_count": recognized_total,
        "unique_count": sum(item["unique_count"] for item in categories.values()),
        "source_duplicate_count": len(source_duplicates),
        "source_duplicates": source_duplicates[:100],
        "invalid_count": invalid_count,
        "uncategorized_count": uncategorized_count,
    }


def _annotate_scan(scan, existing):
    categories = {}
    total_existing = 0
    total_importable = 0
    for kind in ENTITY_ORDER:
        source = scan["categories"][kind]
        known = existing.get(kind, set())
        files = []
        existing_count = 0
        for entry in source["files"]:
            copied = dict(entry)
            if _name_key(entry["name"]) in known:
                copied["status"] = "existing"
                existing_count += 1
            else:
                copied["status"] = "ready"
            files.append(copied)
        importable_count = len(files) - existing_count
        categories[kind] = {
            "entity_type": kind,
            "label": source["label"],
            "files": files,
            "found_count": source["found_count"],
            "unique_count": source["unique_count"],
            "source_duplicate_count": source["source_duplicate_count"],
            "existing_count": existing_count,
            "importable_count": importable_count,
        }
        total_existing += existing_count
        total_importable += importable_count

    result = dict(scan)
    result["categories"] = categories
    result["existing_count"] = total_existing
    result["importable_count"] = total_importable
    return result


def _load_existing(context):
    call_tool = _require_call_tool(context)
    existing = {}
    for kind in ENTITY_ORDER:
        raw_result = call_tool("zzdh_get_entity_list", {"entity_type": kind})
        if _tool_failed(raw_result):
            raise RuntimeError(
                "读取当前作品{0}失败：{1}".format(
                    ENTITY_META[kind]["label"], _tool_error(raw_result)
                )
            )
        names = set()
        for entity in _entities_from_result(raw_result, kind):
            name = str(entity.get("name") or "").strip()
            if name:
                names.add(_name_key(name))
            aliases = str(entity.get("alias") or entity.get("aliases") or "")
            for alias in aliases.replace("，", ",").split(","):
                alias = alias.strip()
                if alias:
                    names.add(_name_key(alias))
        existing[kind] = names
    return existing


def _entities_from_result(value, kind):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    plural = {"character": "characters", "location": "locations", "item": "items"}[kind]
    for key in ("entities", plural, "items", "data", "result"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            found = _entities_from_result(nested, kind)
            if found:
                return found
    return []


def _parse_batch_result(raw_result, chunk):
    result = _unwrap_result(raw_result)
    raw_errors = result.get("errors") if isinstance(result, dict) else None
    raw_errors = raw_errors if isinstance(raw_errors, list) else []
    raw_created = result.get("results") if isinstance(result, dict) else None
    raw_created = raw_created if isinstance(raw_created, list) else []

    count_value = result.get("count") if isinstance(result, dict) else None
    try:
        created = int(count_value) if count_value is not None else len(raw_created)
    except (TypeError, ValueError):
        created = len(raw_created)

    if _tool_failed(raw_result) and not raw_created and count_value is None:
        created = 0
        raw_errors = raw_errors or [{"error": _tool_error(raw_result)}]
    elif count_value is None and not raw_created and not raw_errors and not _tool_failed(raw_result):
        # The documented batch contract always returns count/results.  Do not
        # claim success if a different/older host returns an ambiguous payload.
        created = 0
        raw_errors = [{"error": "宿主未返回可确认的创建数量"}]

    created = max(0, min(created, len(chunk)))
    failed = max(len(chunk) - created, len(raw_errors))
    failed = min(failed, len(chunk))
    errors = []
    used_indexes = set()
    for item in raw_errors:
        error_item = item if isinstance(item, dict) else {"error": str(item)}
        try:
            index = int(error_item.get("index"))
        except (TypeError, ValueError):
            index = -1
        entry = chunk[index] if 0 <= index < len(chunk) else None
        if entry is not None:
            used_indexes.add(index)
        errors.append(
            {
                "entity_type": (entry or {}).get("entity_type", ""),
                "name": (entry or {}).get("name", ""),
                "path": (entry or {}).get("path", ""),
                "error": str(error_item.get("error") or error_item.get("message") or error_item),
            }
        )

    while len(errors) < failed:
        index = next((i for i in range(len(chunk)) if i not in used_indexes), None)
        if index is None:
            break
        used_indexes.add(index)
        entry = chunk[index]
        errors.append(
            {
                "entity_type": entry["entity_type"],
                "name": entry["name"],
                "path": entry["path"],
                "error": "宿主未确认该资产已创建",
            }
        )
    return {"created": created, "failed": failed, "errors": errors}


def _unwrap_result(value):
    current = value
    for _ in range(3):
        if not isinstance(current, dict):
            return {}
        nested = current.get("data")
        if isinstance(nested, dict) and any(
            key in nested for key in ("count", "results", "errors", "success", "ok")
        ):
            current = nested
            continue
        nested = current.get("result")
        if isinstance(nested, dict) and any(
            key in nested for key in ("count", "results", "errors", "success", "ok")
        ):
            current = nested
            continue
        break
    return current if isinstance(current, dict) else {}


def _tool_failed(result):
    if not isinstance(result, dict):
        return True
    if result.get("success") is False or result.get("ok") is False:
        return True
    if result.get("error"):
        return True
    nested = _unwrap_result(result)
    if nested is not result:
        return nested.get("success") is False or nested.get("ok") is False or bool(nested.get("error"))
    return False


def _tool_error(result):
    if isinstance(result, dict):
        nested = _unwrap_result(result)
        return str(
            result.get("error")
            or nested.get("error")
            or result.get("message")
            or nested.get("message")
            or "未知错误"
        )
    return str(result or "宿主未返回结果")


def _require_call_tool(context):
    call_tool = (context or {}).get("call_tool")
    if callable(call_tool):
        return call_tool
    tools = (context or {}).get("tools")
    sync_call = getattr(tools, "call_tool_sync", None)
    if callable(sync_call):
        return sync_call
    raise RuntimeError("当前动作需要在已打开的作品中运行。")


def _clean_folder_path(value):
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("请先选择资产总文件夹。")
    return ntpath.normpath(text)


def _kind_for_path(root, path):
    root_kind = _folder_kind(ntpath.basename(root.rstrip("\\/")))
    if root_kind:
        return root_kind

    root_normalized = ntpath.normcase(ntpath.normpath(root)).rstrip("\\/")
    path_normalized = ntpath.normcase(ntpath.normpath(path))
    prefix = root_normalized + "\\"
    if not path_normalized.startswith(prefix):
        return None
    relative = path_normalized[len(prefix) :]
    parts = relative.replace("/", "\\").split("\\")[:-1]
    for part in parts:
        kind = _folder_kind(part)
        if kind:
            return kind
    return None


def _folder_kind(folder_name):
    key = str(folder_name or "").strip().casefold()
    for kind, meta in ENTITY_META.items():
        if key in {name.casefold() for name in meta["folders"]}:
            return kind
    return None


def _has_supported_signature(path, extension):
    try:
        with open(path, "rb") as stream:
            header = stream.read(16)
    except (OSError, ValueError):
        return False
    if extension == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if extension == ".bmp":
        return header.startswith(b"BM")
    return False


def _name_key(value):
    return str(value or "").strip().casefold()


def _normalize_selected_types(value):
    if value is None:
        return list(ENTITY_ORDER)
    if not isinstance(value, (list, tuple, set)):
        return []
    requested = set(str(item) for item in value)
    return [kind for kind in ENTITY_ORDER if kind in requested]


def _empty_import_categories():
    return {
        kind: {
            "entity_type": kind,
            "label": ENTITY_META[kind]["label"],
            "requested": 0,
            "created": 0,
            "failed": 0,
        }
        for kind in ENTITY_ORDER
    }


def _show_message(context, text, level):
    callback = (context or {}).get("show_message")
    if callable(callback):
        try:
            callback(text, level)
        except Exception:
            pass


def _progress(context, percent, text):
    callback = (context or {}).get("update_progress")
    if not callable(callback):
        return
    try:
        callback(percent, text)
    except TypeError:
        try:
            callback({"percent": percent, "text": text})
        except Exception:
            pass
    except Exception:
        pass


async def self_test(context):
    fake_root = r"C:\asset-import-test"
    candidates = [
        fake_root + r"\角色\陈平.png",
        fake_root + r"\角色\新角色.jpg",
        fake_root + r"\场景\城门.webp",
        fake_root + r"\物品\长剑.bmp",
        fake_root + r"\未分类\忽略.png",
    ]
    scan = _scan_folder(
        fake_root,
        candidate_paths=candidates,
        signature_checker=lambda path, extension: True,
    )
    calls = []

    def fake_call_tool(name, arguments):
        calls.append({"name": name, "arguments": arguments})
        if name == "zzdh_get_entity_list":
            if arguments.get("entity_type") == "character":
                return {"success": True, "entities": [{"name": "陈平", "alias": ""}]}
            return {"success": True, "entities": []}
        if name == "zzdh_batch_create_entity":
            entities = arguments.get("entities") or []
            return {
                "success": True,
                "count": len(entities),
                "results": [{"name": item["name"]} for item in entities],
                "errors": [],
            }
        return {"success": False, "error": "unexpected tool"}

    preview = _preview_assets(fake_root, {"call_tool": fake_call_tool}, scan_override=scan)
    result = _import_assets(
        fake_root,
        list(ENTITY_ORDER),
        {"call_tool": fake_call_tool},
        scan_override=scan,
    )
    batch_calls = [call for call in calls if call["name"] == "zzdh_batch_create_entity"]
    imported = [
        entity
        for call in batch_calls
        for entity in (call["arguments"].get("entities") or [])
    ]
    imported_names = {item["name"] for item in imported}
    checks = [
        {
            "id": "classifies_three_asset_types",
            "ok": scan["counts"] == {"character": 2, "location": 1, "item": 1},
            "message": "角色、场景、物品目录分类正确",
        },
        {
            "id": "ignores_uncategorized_images",
            "ok": scan["uncategorized_count"] == 1,
            "message": "未分类图片不会误导入",
        },
        {
            "id": "reads_all_entity_lists",
            "ok": len([call for call in calls if call["name"] == "zzdh_get_entity_list"]) == 6,
            "message": "预览和导入均读取三类现有资产",
        },
        {
            "id": "skips_existing_name",
            "ok": preview["existing_count"] == 1 and "陈平" not in imported_names,
            "message": "已有同名资产会被跳过",
        },
        {
            "id": "passes_names_and_image_paths",
            "ok": imported_names == {"新角色", "城门", "长剑"}
            and all(item.get("image_path") for item in imported),
            "message": "文件名和绝对图片路径均传给宿主",
        },
        {
            "id": "reports_created_count",
            "ok": result["created"] == 3 and result["failed"] == 0,
            "message": "批量创建结果统计正确",
        },
        {
            "id": "unknown_action_fails",
            "ok": handle_action("unknown", {}, {}).get("ok") is False,
            "message": "未知 UI 动作会明确报错",
        },
    ]
    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "summary": "批量导入资产图片自测" + ("通过" if ok else "失败"),
        "checks": checks,
        "artifacts": [],
    }
