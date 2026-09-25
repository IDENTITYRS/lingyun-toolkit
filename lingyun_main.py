"""凌云·字字实用插件大全：统一 UI 入口与三项本地功能。"""

import asyncio
from pathlib import Path
import sys

# 旧版宿主用 spec_from_file_location 加载入口，但不加入插件目录。
# 以入口文件定位随包模块，避免依赖宿主的工作目录。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from lingyun_features import assets, mapper, sections, license_manager
import lingyun_update
_UPDATE_MANAGER = lingyun_update.Manager(_PLUGIN_DIR)


def get_info():
    return {
        "name": "凌云·字字实用插件大全",
        "description": "下拉切换导入提示词、导入资产，并按全文或指定文字行匹配参考图片。",
    }


def handle_action(action, data, context=None):
    """UI 动作采用固定路由，不使用文件路径或动态插件名称。"""
    payload = data if isinstance(data, dict) else {}
    if action in ("update_status", "check_update", "install_update"):
        try:
            if action == "update_status":
                result = _UPDATE_MANAGER.status()
            else:
                result = _UPDATE_MANAGER.begin(install=action == "install_update",
                                               automatic=bool(payload.get("automatic")) and action == "check_update")
            return {"ok": True, "data": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    try:
        if action == "license_status":
            return {"ok": True, "data": license_manager.get_status()}
        if action == "activate_license":
            return {"ok": True, "data": license_manager.activate(payload.get("code"))}
        if action in ("import_sections", "choose_folder", "scan_assets", "import_assets", "map_images"):
            license_manager.require_active()
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "license_required": action not in ("license_status", "activate_license")}
    if action == "import_sections":
        return sections.handle_action("import", payload, context)
    if action == "choose_folder":
        return assets.handle_action("choose_folder", payload, context)
    if action == "scan_assets":
        return assets.handle_action("scan_assets", payload, context)
    if action == "import_assets":
        return assets.handle_action("import_assets", payload, context)
    if action == "map_images":
        return mapper.handle_action("run", payload, context)
    return {"ok": False, "error": "未知动作：" + str(action)}


async def execute(context):
    """工具调用与 UI 共用移植后的业务代码；资产扫描默认只读。"""
    ctx = dict(context or {})
    try:
        license_manager.require_active()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "license_required": True}
    args = ctx.get("arguments") or {}
    feature = args.get("feature", "sections")
    if feature == "sections":
        return await sections.execute(ctx)
    if feature == "mapper":
        return await mapper.execute(ctx)
    if feature != "assets":
        return {"status": "error", "message": "未知功能：" + str(feature)}

    action = args.get("action", "scan_assets")
    if action not in ("scan_assets", "import_assets"):
        return {"status": "error", "message": "资产动作仅支持 scan_assets / import_assets"}
    tools = ctx.get("tools")
    if tools is not None:
        loop = asyncio.get_running_loop()

        def call_tool(name, arguments):
            future = asyncio.run_coroutine_threadsafe(tools.call_tool(name, arguments), loop)
            try:
                return future.result(timeout=600)
            except BaseException:
                future.cancel()
                raise

        ctx["call_tool"] = call_tool
    result = await asyncio.to_thread(assets.handle_action, action, args, ctx)
    data = result.get("data") or {}
    return {
        "status": data.get("status") or ("ok" if result.get("ok") else "error"),
        "message": result.get("error") or result.get("message") or data.get("message", ""),
        "data": data,
    }


async def self_test(context):
    """原功能自检全部保留；仅分镜自检会读取宿主，不写入真实作品。"""
    checks = []
    for name, module in (("sections", sections), ("assets", assets), ("mapper", mapper)):
        try:
            result = await module.self_test(context or {})
            checks.extend({**check, "id": name + "." + check["id"]} for check in result["checks"])
        except Exception as exc:
            checks.append({"id": name + ".self_test", "ok": False, "message": str(exc)})
    ok = all(check["ok"] for check in checks)
    return {"ok": ok, "summary": "三合一插件自检" + ("通过" if ok else "未完全通过"),
            "checks": checks, "artifacts": []}
