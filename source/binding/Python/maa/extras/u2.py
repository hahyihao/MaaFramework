"""
U2 — 把 openatx/uiautomator2 接进 MaaFramework 的 CustomRecognition

依赖(用户自己装):
    pip install uiautomator2
    python -m uiautomator2 init    # 把 atx-agent + apk 推到设备(一次性)

用法:
    import uiautomator2 as u2
    from maa.extras.u2 import U2Recognition

    device = u2.connect()  # 或 u2.connect("device-serial")
    resource.register_custom_recognition("U2", U2Recognition(device))

fixture(在 pipeline JSON 里):
    {
        "FindLoginBtn": {
            "recognition": "Custom",
            "custom_recognition": "U2",
            "custom_recognition_param": {
                "text": "登录",
                "className": "android.widget.Button",
                "clickable": true
            },
            "action": "Click",
            "target": true
        }
    }

支持的 custom_recognition_param 字段(直接透传给 openatx/uiautomator2 selector):
    - resourceId / text / textContains / textMatches
    - className / classNameMatches
    - description / descriptionContains / descriptionMatches
    - packageName / packageNameMatches
    - clickable / enabled / focusable / focused / checkable / checked
      / selected / scrollable / longClickable
    - instance: 多匹配时取第 N 个(默认 0)
    - xpath: 直接传 XPath 表达式(走 d.xpath(...).get())

字段命名跟 openatx/uiautomator2 一致(camelCase),不做 snake_case 映射。
"""

import json
from typing import Optional

from ..context import Context
from ..custom_recognition import CustomRecognition


_SELECTOR_KEYS = (
    "resourceId", "resourceIdMatches",
    "text", "textContains", "textMatches", "textStartsWith",
    "className", "classNameMatches",
    "description", "descriptionContains", "descriptionMatches", "descriptionStartsWith",
    "packageName", "packageNameMatches",
    "checkable", "checked", "clickable", "longClickable",
    "scrollable", "enabled", "focusable", "focused", "selected",
    "instance", "index",
)


class U2Recognition(CustomRecognition):
    """适配器:Pipeline reco 调到 openatx/uiautomator2"""

    def __init__(self, device):
        """
        Args:
            device: uiautomator2 设备对象,通常通过 `u2.connect(serial)` /
                    `u2.connect_usb()` / `u2.connect_wifi(host)` 获取
        """
        super().__init__()
        self._device = device

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Optional[CustomRecognition.AnalyzeResult]:
        try:
            params = json.loads(argv.custom_recognition_param) if argv.custom_recognition_param else {}
        except json.JSONDecodeError:
            return None

        if xpath := params.get("xpath"):
            try:
                el = self._device.xpath(xpath).get(timeout=0.1)
            except Exception:
                return None
            if not el:
                return None
            info = el.attrib if hasattr(el, "attrib") else {}
            bounds_str = info.get("bounds", "")
            box, bounds = _parse_xpath_bounds(bounds_str)
            if box is None:
                return None
            return CustomRecognition.AnalyzeResult(
                box=box,
                detail={
                    "resourceId": info.get("resource-id", ""),
                    "text": info.get("text", ""),
                    "className": info.get("class", ""),
                    "bounds": bounds,
                    "via": "xpath",
                },
            )

        kwargs = {k: params[k] for k in _SELECTOR_KEYS if k in params}
        if not kwargs:
            return None

        try:
            sel = self._device(**kwargs)
            if not sel.exists:
                return None
            info = sel.info
        except Exception:
            return None

        b = info.get("bounds") or {}
        left, top = int(b.get("left", 0)), int(b.get("top", 0))
        right, bottom = int(b.get("right", 0)), int(b.get("bottom", 0))
        if right <= left or bottom <= top:
            return None

        return CustomRecognition.AnalyzeResult(
            box=(left, top, right - left, bottom - top),
            detail={
                "resourceId": info.get("resourceName") or "",
                "text": info.get("text") or "",
                "className": info.get("className") or "",
                "description": info.get("contentDescription") or "",
                "packageName": info.get("packageName") or "",
                "bounds": [left, top, right, bottom],
                "via": "selector",
            },
        )


def _parse_xpath_bounds(bounds_str: str):
    """uiautomator XML 的 bounds 是 "[l,t][r,b]" 字符串格式"""
    if not bounds_str:
        return None, None
    try:
        s = bounds_str.replace("[", "").replace("]", ",").strip(",")
        parts = [int(x) for x in s.split(",") if x]
        if len(parts) != 4:
            return None, None
        left, top, right, bottom = parts
        if right <= left or bottom <= top:
            return None, None
        return (left, top, right - left, bottom - top), [left, top, right, bottom]
    except (ValueError, AttributeError):
        return None, None
