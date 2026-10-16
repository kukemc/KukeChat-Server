"""商店商品样式的校验（docs/shop.md 2.2 气泡皮肤、2.4 头像框）。

style 不是任意 CSS，但客户端会把里面的值拼进样式里，所以这里按白名单逐个字段解析，
再用解析出来的片段重新拼一遍 —— 存进库里的永远是规范形式，原文里多出来的任何字符都带不进去。

纹样（pattern）和角饰（ornaments）是客户端内置的图形，样式里只有名字和颜色：名字不在下面两张表里的一律拒绝，
没有任何字段能放图片地址。客户端加了新图形之后，要在这里的表里也登记上才能用。

每个类别一个 validate_*_style，在 services/shop.py 的 CATEGORIES 里登记。
"""

from __future__ import annotations

import math
import re
from typing import Any

BUBBLE_STYLE_VERSION = 1
ANIMATIONS = ("none", "flow", "shimmer", "breathe")
# 客户端内置的纹样和角饰（契约 2.2）
PATTERNS = ("dots", "grid", "stripes", "waves", "stars", "hearts", "pixels", "clouds", "petals", "noise")
ORNAMENTS = ("sparkle", "star", "moon", "sun", "heart", "petal", "cloud", "paw", "leaf", "crown", "pixel_heart", "bolt", "snow")
CORNERS = ("tl", "tr", "bl", "br")
PATTERN_OPACITY_RANGE = (0, 0.6)
PATTERN_SCALE_RANGE = (0.5, 2)
ORNAMENTS_MAX = 2
ORNAMENT_SIZE_RANGE = (10, 28)
RADIUS_RANGE = (4, 22)
BORDER_WIDTH_RANGE = (0, 3)
GRADIENT_STOPS_RANGE = (2, 8)
SHADOW_MAX_LAYERS = 2
# 阴影的偏移 / 模糊 / 扩散各自允许的范围（px）
SHADOW_OFFSET_RANGE = (-24, 24)
SHADOW_BLUR_RANGE = (0, 48)
SHADOW_SPREAD_RANGE = (-12, 24)
_TEXT_MAX_LENGTH = 240

# —— 头像框（契约 2.4）。尺寸都按 40px 的头像计，客户端按头像实际大小等比缩放 ——
FRAME_STYLE_VERSION = 1
FRAME_ANIMATIONS = ("none", "spin", "pulse", "shimmer")
# 气泡的角饰头像框都能用，另外多几个只适合戴在头像上的
FRAME_ORNAMENTS = (*ORNAMENTS, "cat_ears", "halo", "horns", "laurel", "wings", "bow")
FRAME_POSITIONS = ("top", "tr", "right", "br", "bottom", "bl", "left", "tl")
FRAME_ORNAMENTS_MAX = 3
RING_WIDTH_RANGE = (2, 6)
RING_GAP_RANGE = (0, 3)
GLOW_BLUR_RANGE = (0, 16)

# 数字一律写 [0-9]：\d 会放进全角数字
_HEX = r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})"
_COLOR_RE = re.compile(_HEX)
_ANGLE_RE = re.compile(r"(-?[0-9]{1,3}(?:\.[0-9]{1,2})?)deg")
_STOP_RE = re.compile(rf"({_HEX})(?: ([0-9]{{1,3}}(?:\.[0-9]{{1,2}})?)%)?")
# 锥形渐变开头可选的 from <角度>deg
_FROM_RE = re.compile(r"from (-?[0-9]{1,3}(?:\.[0-9]{1,2})?)deg")
_GRADIENT_RE = re.compile(r"(linear|radial|conic)-gradient\(([^()]*)\)")
_LENGTH_RE = re.compile(r"(-?[0-9]{1,2}(?:\.[0-9])?)px|(0)")
# 渐变和阴影里可能出现的全部字符；别的（分号、引号、斜杠、换行……）直接拒绝
_SAFE_TEXT_RE = re.compile(r"[0-9a-zA-Z#%.,() -]*")

_BUBBLE_KEYS = ("version", "background", "text", "link", "border", "radius", "shadow", "animation", "pattern", "ornaments", "dark")
# 深色覆盖里只有配色相关的这几项；圆角、动效、角饰两种配色共用
_DARK_KEYS = ("background", "text", "link", "border", "shadow", "pattern")
_BORDER_KEYS = ("color", "width")
_PATTERN_KEYS = ("key", "color", "opacity", "scale")

_FRAME_KEYS = ("version", "ring", "glow", "animation", "ornaments", "dark")
_RING_KEYS = ("background", "width", "gap")
_GLOW_KEYS = ("color", "blur")
# 深色覆盖里只能换环的底色和外发光；环的粗细、留白、动效、装饰两种配色共用
_FRAME_DARK_KEYS = ("ring", "glow")
_FRAME_DARK_RING_KEYS = ("background",)


class StyleError(ValueError):
    """样式不合法。field 是出问题的字段（嵌套的用点连接，例如 dark.border.width）。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _object(value: Any, field: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StyleError(field, "必须是一个对象")
    for key in value:
        if key not in allowed:
            prefix = "" if field == "style" else f"{field}."
            raise StyleError(f"{prefix}{key}", "不认识的字段")
    return value


def _color(value: Any, field: str) -> str:
    if not isinstance(value, str) or _COLOR_RE.fullmatch(value) is None:
        raise StyleError(field, "颜色只接受 #rgb、#rrggbb 或 #rrggbbaa")
    return value


def _number(value: Any, field: str, bounds: tuple[float, float], digits: int = 1) -> int | float:
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise StyleError(field, f"必须是 {low}–{high} 之间的数字")
    rounded = round(float(value), digits)
    return int(rounded) if rounded.is_integer() else rounded


def _choice(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    """只能是表里的名字。纹样和角饰的 key 走这里：不认识的名字（更不用说地址）一律拒绝。"""
    if not isinstance(value, str) or value not in allowed:
        raise StyleError(field, f"只能是 {' / '.join(allowed)}")
    return value


def _text(value: Any, field: str) -> str:
    """渐变、阴影这类带结构的字符串：先挡掉白名单之外的字符，再把连续空格压成一个。"""
    if not isinstance(value, str) or not value.strip(" "):
        raise StyleError(field, "不能为空")
    if len(value) > _TEXT_MAX_LENGTH or _SAFE_TEXT_RE.fullmatch(value) is None:
        raise StyleError(field, "含有不允许的字符或者太长")
    return " ".join(part for part in value.split(" ") if part)


def _background(value: Any, field: str, *, conic: bool = False) -> str:
    """一个颜色或者一个渐变。conic：是否放开 conic-gradient(...)（只有头像框的环放开，气泡的底色不放开）。"""
    if isinstance(value, str) and _COLOR_RE.fullmatch(value) is not None:
        return value
    text = _text(value, field)
    matched = _GRADIENT_RE.fullmatch(text)
    if matched is None or (matched.group(1) == "conic" and not conic):
        kinds = "linear-gradient(...) / radial-gradient(...)" + (" / conic-gradient(...)" if conic else "")
        raise StyleError(field, f"只接受一个颜色，或者一个 {kinds}")
    kind, inner = matched.groups()
    parts = [part.strip(" ") for part in inner.split(",")]
    pieces: list[str] = []
    # 角度只能在最前面：线性渐变直接写角度，锥形渐变写 from <角度>；径向渐变没有
    opening = {"linear": _ANGLE_RE, "conic": _FROM_RE}.get(kind)
    if opening is not None and parts and opening.fullmatch(parts[0]) is not None:
        angle = opening.fullmatch(parts[0]).group(1)
        if abs(float(angle)) > 360:
            raise StyleError(field, "角度要在 -360deg 到 360deg 之间")
        pieces.append(f"from {angle}deg" if kind == "conic" else f"{angle}deg")
        parts = parts[1:]
    low, high = GRADIENT_STOPS_RANGE
    if not low <= len(parts) <= high:
        raise StyleError(field, f"渐变要有 {low}–{high} 个颜色")
    for part in parts:
        stop = _STOP_RE.fullmatch(part)
        if stop is None:
            raise StyleError(field, "渐变里只能有角度、颜色和百分比")
        color, position = stop.groups()
        if position is None:
            pieces.append(color)
            continue
        if float(position) > 100:
            raise StyleError(field, "百分比不能超过 100%")
        pieces.append(f"{color} {position}%")
    return f"{kind}-gradient({', '.join(pieces)})"


def _length(token: str, field: str, bounds: tuple[int, int]) -> str:
    matched = _LENGTH_RE.fullmatch(token)
    if matched is None:
        raise StyleError(field, "长度写成 0 或者带 px 的数字，例如 2px")
    number = matched.group(1) or matched.group(2)
    if not bounds[0] <= float(number) <= bounds[1]:
        raise StyleError(field, f"长度超出范围（{bounds[0]}px 到 {bounds[1]}px）")
    return "0" if float(number) == 0 else f"{number}px"


def _shadow(value: Any, field: str) -> str:
    layers = _text(value, field).split(",")
    if len(layers) > SHADOW_MAX_LAYERS:
        raise StyleError(field, f"阴影最多 {SHADOW_MAX_LAYERS} 层")
    result: list[str] = []
    for layer in layers:
        tokens = layer.strip(" ").split(" ")
        # 偏移 x、偏移 y、模糊、[扩散]、颜色
        if len(tokens) not in (4, 5) or _COLOR_RE.fullmatch(tokens[-1]) is None:
            raise StyleError(field, "阴影写成「偏移 偏移 模糊 [扩散] 颜色」，例如 0 2px 8px #00000033")
        lengths = [
            _length(tokens[0], field, SHADOW_OFFSET_RANGE),
            _length(tokens[1], field, SHADOW_OFFSET_RANGE),
            _length(tokens[2], field, SHADOW_BLUR_RANGE),
        ]
        if len(tokens) == 5:
            lengths.append(_length(tokens[3], field, SHADOW_SPREAD_RANGE))
        result.append(" ".join([*lengths, tokens[-1]]))
    return ", ".join(result)


def _border(value: Any, field: str) -> dict[str, Any]:
    border = _object(value, field, _BORDER_KEYS)
    for key in _BORDER_KEYS:
        if border.get(key) is None:
            raise StyleError(f"{field}.{key}", "不能为空")
    return {"color": _color(border["color"], f"{field}.color"), "width": _number(border["width"], f"{field}.width", BORDER_WIDTH_RANGE)}


def _pattern(value: Any, field: str) -> dict[str, Any]:
    """叠在底色上的纹样：{ key, color, opacity, scale }，scale 可空（不写就是 1）。"""
    pattern = _object(value, field, _PATTERN_KEYS)
    for key in ("key", "color", "opacity"):
        if pattern.get(key) is None:
            raise StyleError(f"{field}.{key}", "不能为空")
    result: dict[str, Any] = {
        "key": _choice(pattern["key"], f"{field}.key", PATTERNS),
        "color": _color(pattern["color"], f"{field}.color"),
        "opacity": _number(pattern["opacity"], f"{field}.opacity", PATTERN_OPACITY_RANGE, digits=2),
    }
    if pattern.get("scale") is not None:
        result["scale"] = _number(pattern["scale"], f"{field}.scale", PATTERN_SCALE_RANGE, digits=2)
    return result


def _ornaments(
    value: Any,
    field: str,
    *,
    names: tuple[str, ...] = ORNAMENTS,
    place: str = "corner",
    places: tuple[str, ...] = CORNERS,
    maximum: int = ORNAMENTS_MAX,
) -> list[dict[str, Any]]:
    """贴在气泡角上的小装饰：最多 ORNAMENTS_MAX 个，一个角只能放一个。

    头像框的装饰是同一套规则，只是表不一样：位置字段叫 position（环上的八个方位），能用的图形和个数上限也各有一份。
    """
    if not isinstance(value, list):
        raise StyleError(field, "必须是一个数组")
    if len(value) > maximum:
        raise StyleError(field, f"角饰最多 {maximum} 个")
    result: list[dict[str, Any]] = []
    taken: set[str] = set()
    for index, raw in enumerate(value):
        name = f"{field}[{index}]"
        ornament = _object(raw, name, ("key", place, "color", "size", "animated"))
        for key in ("key", place, "color", "size"):
            if ornament.get(key) is None:
                raise StyleError(f"{name}.{key}", "不能为空")
        entry: dict[str, Any] = {
            "key": _choice(ornament["key"], f"{name}.key", names),
            place: _choice(ornament[place], f"{name}.{place}", places),
            "color": _color(ornament["color"], f"{name}.color"),
            "size": _number(ornament["size"], f"{name}.size", ORNAMENT_SIZE_RANGE),
        }
        if entry[place] in taken:
            raise StyleError(f"{name}.{place}", "同一个位置不能放两个角饰")
        taken.add(entry[place])
        animated = ornament.get("animated")
        if animated is not None:
            if not isinstance(animated, bool):
                raise StyleError(f"{name}.animated", "只能是 true 或 false")
            entry["animated"] = animated
        result.append(entry)
    return result


def _surface(source: dict[str, Any], prefix: str, required: bool) -> dict[str, Any]:
    """浅色和深色两套里都有的六个字段。required：background 和 text 是否必填（深色覆盖里都可以不写，不写就沿用浅色的）。"""
    result: dict[str, Any] = {}
    parsers = (("background", _background), ("text", _color), ("link", _color), ("border", _border), ("shadow", _shadow), ("pattern", _pattern))
    for key, parse in parsers:
        value = source.get(key)
        if value is None:
            if required and key in ("background", "text"):
                raise StyleError(f"{prefix}{key}", "不能为空")
            continue
        result[key] = parse(value, f"{prefix}{key}")
    return result


def _version(source: dict[str, Any], expected: int) -> int:
    version = source.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != expected:
        raise StyleError("version", f"目前只支持 version = {expected}")
    return expected


def validate_bubble_style(style: Any) -> dict[str, Any]:
    """校验一件气泡皮肤的样式，返回规范化之后的副本；不合法就抛 StyleError。

    可空的字段传 null 和不传是一回事，规范形式里都不带这个键。
    """
    source = _object(style, "style", _BUBBLE_KEYS)
    _version(source, BUBBLE_STYLE_VERSION)
    surface = _surface(source, "", required=True)
    result: dict[str, Any] = {"version": BUBBLE_STYLE_VERSION, "background": surface["background"], "text": surface["text"]}
    if "link" in surface:
        result["link"] = surface["link"]
    if "border" in surface:
        result["border"] = surface["border"]
    if source.get("radius") is not None:
        result["radius"] = _number(source["radius"], "radius", RADIUS_RANGE)
    if "shadow" in surface:
        result["shadow"] = surface["shadow"]
    animation = source.get("animation")
    if animation is not None:
        result["animation"] = _choice(animation, "animation", ANIMATIONS)
    if "pattern" in surface:
        result["pattern"] = surface["pattern"]
    if source.get("ornaments") is not None:
        ornaments = _ornaments(source["ornaments"], "ornaments")
        if ornaments:
            result["ornaments"] = ornaments
    if source.get("dark") is not None:
        dark = _surface(_object(source["dark"], "dark", _DARK_KEYS), "dark.", required=False)
        if dark:
            result["dark"] = dark
    return result


def _glow(value: Any, field: str) -> dict[str, Any]:
    """环外面的一圈发光：{ color, blur }。"""
    glow = _object(value, field, _GLOW_KEYS)
    for key in _GLOW_KEYS:
        if glow.get(key) is None:
            raise StyleError(f"{field}.{key}", "不能为空")
    return {"color": _color(glow["color"], f"{field}.color"), "blur": _number(glow["blur"], f"{field}.blur", GLOW_BLUR_RANGE)}


def validate_frame_style(style: Any) -> dict[str, Any]:
    """校验一件头像框的样式（契约 2.4），返回规范化之后的副本；不合法就抛 StyleError。

    和气泡一样：可空的字段传 null 和不传是一回事，规范形式里都不带这个键。
    """
    source = _object(style, "style", _FRAME_KEYS)
    result: dict[str, Any] = {"version": _version(source, FRAME_STYLE_VERSION)}
    if source.get("ring") is None:
        raise StyleError("ring", "不能为空")
    ring = _object(source["ring"], "ring", _RING_KEYS)
    for key in ("background", "width"):
        if ring.get(key) is None:
            raise StyleError(f"ring.{key}", "不能为空")
    result["ring"] = {
        "background": _background(ring["background"], "ring.background", conic=True),
        "width": _number(ring["width"], "ring.width", RING_WIDTH_RANGE),
    }
    if ring.get("gap") is not None:
        result["ring"]["gap"] = _number(ring["gap"], "ring.gap", RING_GAP_RANGE)
    if source.get("glow") is not None:
        result["glow"] = _glow(source["glow"], "glow")
    animation = source.get("animation")
    if animation is not None:
        result["animation"] = _choice(animation, "animation", FRAME_ANIMATIONS)
    if source.get("ornaments") is not None:
        ornaments = _ornaments(
            source["ornaments"], "ornaments", names=FRAME_ORNAMENTS, place="position", places=FRAME_POSITIONS, maximum=FRAME_ORNAMENTS_MAX
        )
        if ornaments:
            result["ornaments"] = ornaments
    if source.get("dark") is not None:
        override = _object(source["dark"], "dark", _FRAME_DARK_KEYS)
        dark: dict[str, Any] = {}
        if override.get("ring") is not None:
            background = _object(override["ring"], "dark.ring", _FRAME_DARK_RING_KEYS).get("background")
            if background is not None:
                dark["ring"] = {"background": _background(background, "dark.ring.background", conic=True)}
        if override.get("glow") is not None:
            dark["glow"] = _glow(override["glow"], "dark.glow")
        if dark:
            result["dark"] = dark
    return result
