"""透明图层叠序校准台 —— 纯标准库计算核心。

- sRGB <-> 线性光分段转换（IEC 61966-2-1）
- 线性光空间内逐层 alpha 合成（Porter-Duff "over"）
- 枚举全部底到顶排列，按未取整 RGB 通道平方误差总和选优
- 同分时按 ID 序列字典序决胜
- 图层顺序确定后，可在 0.00～1.00（步长 0.01）网格上联合反推 1～2 个图层的不透明度
- 下一次背景测量建议：保留总误差不高于最优总误差 + 容差的叠序，
  在候选背景上渲染后以「任意两条预测色未取整 RGB 平方距离最小值」打分并推荐
- 实测复核：在选定候选背景上录入实测目标色与新测量误差上限，
  在容差合规叠序中再按「该背景未取整 RGB 平方误差 ≤ 上限」过滤
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from typing import Any

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

MAX_LAYERS = 7
MIN_LAYERS = 2
MAX_SAMPLES = 4
MIN_SAMPLES = 1
MAX_RELATIONS = 12
OPACITY_STEPS = 101  # 0.00～1.00，步长 0.01
MAX_ADJUST = 2
MIN_ADJUST = 1
MIN_CANDIDATES = 2
MAX_CANDIDATES = 12
MAX_SUGGEST_ORDERS = 12


class CalibrationError(Exception):
    """输入校验失败，errors 为可直接展示给用户的中文错误列表。"""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def _err(scope: str, message: str, index: int | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"scope": scope, "message": message}
    if index is not None:
        e["index"] = index
    return e


# ---------------------------------------------------------------- 颜色基础 --


def parse_hex(text: Any) -> tuple[str, tuple[float, float, float]]:
    """把 '#rgb' / '#rrggbb'（# 可省略）解析为 (规范 hex, 0~1 sRGB 三元组)。"""
    if not isinstance(text, str):
        raise ValueError("颜色必须是字符串")
    s = text.strip()
    m = _HEX_RE.fullmatch(s)
    if not m:
        raise ValueError(f"非法颜色 {text!r}：需要形如 #RRGGBB 的 HEX 值")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)
    return "#" + h.lower(), rgb


def srgb_to_linear(c: float) -> float:
    """sRGB 传递函数（0~1）-> 线性光。"""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    """线性光 -> sRGB 传递函数（0~1）。"""
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def over(
    bottom: tuple[float, float, float, float],
    top: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """线性光空间内的 Porter-Duff "over" 合成（两者均为预乘表示）。"""
    br, bg, bb, ba = bottom
    tr, tg, tb, ta = top
    out_a = ta + ba * (1.0 - ta)
    if out_a <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)

    def chan(tc: float, bc: float) -> float:
        return (tc * ta + bc * ba * (1.0 - ta)) / out_a

    return (chan(tr, br), chan(tg, bg), chan(tb, bb), out_a)


def render_stack(
    background_srgb: tuple[float, float, float],
    order: list[int],
    layer_colors_lin: list[tuple[float, float, float]],
    layer_alphas: list[float],
) -> tuple[float, float, float]:
    """以不透明背景打底，按底到顶 order 逐层合成，返回 sRGB(0~1) 结果色。"""
    r, g, b = (srgb_to_linear(v) for v in background_srgb)
    cur = (r, g, b, 1.0)
    for idx in order:
        lr, lg, lb = layer_colors_lin[idx]
        cur = over(cur, (lr, lg, lb, layer_alphas[idx]))
    out = tuple(_clamp01(linear_to_srgb(cur[c])) for c in range(3))
    return out  # type: ignore[return-value]


# ----------------------------------------------------------------- 环检测 --


def _find_cycle(adj: list[list[int]]) -> list[int] | None:
    """DFS 找有向图中的环，返回环上节点下标序列（首尾相同），无环返回 None。"""
    n = len(adj)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n
    parent = [-1] * n

    def dfs(u: int) -> list[int] | None:
        color[u] = GRAY
        for v in adj[u]:
            if color[v] == GRAY:  # 自环与回边均落在此处
                path = [u]
                cur = u
                while cur != v:
                    cur = parent[cur]
                    path.append(cur)
                path.reverse()
                path.append(v)
                return path
            if color[v] == WHITE:
                parent[v] = u
                found = dfs(v)
                if found is not None:
                    return found
        color[u] = BLACK
        return None

    for start in range(n):
        if color[start] == WHITE:
            found = dfs(start)
            if found is not None:
                return found
    return None


# --------------------------------------------------------- 输入解析（共用） --


@dataclass
class _Parsed:
    ids: list[str]
    hexes: list[str]
    lins: list[tuple[float, float, float]]
    alphas: list[float]
    seen_ids: dict[str, int]
    bg_hex: list[str]
    bg_srgb: list[tuple[float, float, float]]
    tg_hex: list[str]
    tg_srgb: list[tuple[float, float, float]]
    pairs: list[tuple[int, int]]


def _parse_inputs(payload: Any) -> tuple[_Parsed, list[dict[str, Any]]]:
    """解析 layers/samples/relations。结构性致命错误直接抛，字段错误收集后返回。"""
    if not isinstance(payload, dict):
        raise CalibrationError([_err("form", "请求体必须是 JSON 对象")])

    errors: list[dict[str, Any]] = []

    raw_layers = payload.get("layers")
    raw_samples = payload.get("samples")
    raw_relations = payload.get("relations", [])

    # ---- 图层 -------------------------------------------------------------
    if not isinstance(raw_layers, list):
        raise CalibrationError([_err("layers", "缺少 layers 列表或其格式不正确")])
    if not (MIN_LAYERS <= len(raw_layers) <= MAX_LAYERS):
        errors.append(
            _err("layers", f"图层数量必须在 {MIN_LAYERS}～{MAX_LAYERS} 个之间，当前 {len(raw_layers)} 个")
        )

    layer_ids: list[str] = []
    layer_hex: list[str] = []
    layer_lin: list[tuple[float, float, float]] = []
    layer_alpha: list[float] = []
    seen_ids: dict[str, int] = {}

    for i, item in enumerate(raw_layers):
        if not isinstance(item, dict):
            errors.append(_err("layers", "图层数据格式不正确，应为对象", i))
            continue

        lid = item.get("id")
        if not isinstance(lid, str) or not lid.strip():
            errors.append(_err("layers", "图层 ID 不能为空", i))
            lid = ""
        else:
            lid = lid.strip()
            if lid in seen_ids:
                errors.append(_err("layers", f"图层 ID 重复：{lid}", i))
            else:
                seen_ids[lid] = i

        try:
            norm, rgb = parse_hex(item.get("color"))
        except ValueError as exc:
            errors.append(_err("layers", str(exc), i))
            norm, rgb = "#000000", (0.0, 0.0, 0.0)

        opacity = item.get("opacity")
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not math.isfinite(opacity):
            errors.append(_err("layers", f"不透明度必须是 0～1 的数字（图层 {lid or i + 1}）", i))
            opacity = 1.0
        elif not (0.0 <= float(opacity) <= 1.0):
            errors.append(
                _err("layers", f"不透明度越界：{opacity}，必须在 0～1 之间（图层 {lid or i + 1}）", i)
            )
            opacity = 1.0

        layer_ids.append(lid)
        layer_hex.append(norm)
        layer_lin.append(tuple(srgb_to_linear(c) for c in rgb))  # type: ignore[arg-type]
        layer_alpha.append(float(opacity))

    # ---- 样本 -------------------------------------------------------------
    if not isinstance(raw_samples, list):
        raise CalibrationError([_err("samples", "缺少 samples 列表或其格式不正确")])
    if not (MIN_SAMPLES <= len(raw_samples) <= MAX_SAMPLES):
        errors.append(
            _err("samples", f"样本（背景/目标）组数必须在 {MIN_SAMPLES}～{MAX_SAMPLES} 之间，当前 {len(raw_samples)} 组")
        )

    samples_bg_hex: list[str] = []
    samples_bg_srgb: list[tuple[float, float, float]] = []
    samples_tg_hex: list[str] = []
    samples_tg_srgb: list[tuple[float, float, float]] = []

    for i, item in enumerate(raw_samples):
        if not isinstance(item, dict):
            errors.append(_err("samples", "样本数据格式不正确，应为对象", i))
            continue
        for field, label, sink_hex, sink_rgb in (
            ("background", "背景色", samples_bg_hex, samples_bg_srgb),
            ("target", "目标色", samples_tg_hex, samples_tg_srgb),
        ):
            try:
                norm, rgb = parse_hex(item.get(field))
            except ValueError:
                errors.append(_err("samples", f"{label}非法：需要形如 #RRGGBB 的 HEX 值，实际为 {item.get(field)!r}", i))
                norm, rgb = "#000000", (0.0, 0.0, 0.0)
            sink_hex.append(norm)
            sink_rgb.append(rgb)

    # ---- 先后关系 ---------------------------------------------------------
    if not isinstance(raw_relations, list):
        errors.append(_err("relations", "relations 字段格式不正确，应为数组"))
        raw_relations = []
    if len(raw_relations) > MAX_RELATIONS:
        errors.append(
            _err("relations", f"图层先后关系至多 {MAX_RELATIONS} 条，当前 {len(raw_relations)} 条")
        )

    id_set = {lid for lid in layer_ids if lid}
    pairs: list[tuple[int, int]] = []
    pair_seen: set[tuple[int, int]] = set()
    for i, item in enumerate(raw_relations[:MAX_RELATIONS]):
        if not isinstance(item, dict):
            errors.append(_err("relations", "关系数据格式不正确，应为对象", i))
            continue
        lower, upper = item.get("lower"), item.get("upper")
        if not isinstance(lower, str) or lower.strip() not in id_set:
            errors.append(_err("relations", f"关系下方图层 ID 不存在：{lower!r}", i))
            continue
        if not isinstance(upper, str) or upper.strip() not in id_set:
            errors.append(_err("relations", f"关系上方图层 ID 不存在：{upper!r}", i))
            continue
        lower, upper = lower.strip(), upper.strip()
        li, ui = seen_ids[lower], seen_ids[upper]
        key = (li, ui)
        if key in pair_seen:
            continue  # 重复关系直接去重
        pair_seen.add(key)
        pairs.append((li, ui))

    parsed = _Parsed(
        ids=layer_ids,
        hexes=layer_hex,
        lins=layer_lin,
        alphas=layer_alpha,
        seen_ids=seen_ids,
        bg_hex=samples_bg_hex,
        bg_srgb=samples_bg_srgb,
        tg_hex=samples_tg_hex,
        tg_srgb=samples_tg_srgb,
        pairs=pairs,
    )
    return parsed, errors


def _raise_if_cycle(parsed: _Parsed) -> None:
    n = len(parsed.ids)
    adj: list[list[int]] = [[] for _ in range(n)]
    for li, ui in parsed.pairs:
        adj[li].append(ui)
    cycle = _find_cycle(adj)
    if cycle is not None:
        chain = " → ".join(parsed.ids[x] for x in cycle)
        raise CalibrationError(
            [_err("relations", f"图层先后关系成环，存在冲突：{chain}（底 → 顶方向不能绕回自身）")]
        )


def _total_sse(
    parsed: _Parsed, order: list[int], alphas: list[float]
) -> float:
    """给定顺序与不透明度，计算全部样本未取整 RGB 平方误差总和。"""
    total = 0.0
    for s in range(len(parsed.bg_hex)):
        pred = render_stack(parsed.bg_srgb[s], order, parsed.lins, alphas)
        for c in range(3):
            diff = pred[c] * 255.0 - parsed.tg_srgb[s][c] * 255.0
            total += diff * diff
    return total


def _sample_details(
    parsed: _Parsed, order: list[int], alphas: list[float]
) -> tuple[list[dict[str, Any]], float]:
    """组装每个样本的预测色与分通道误差，并返回总 SSE。"""
    results: list[dict[str, Any]] = []
    total = 0.0
    for s in range(len(parsed.bg_hex)):
        pred = render_stack(parsed.bg_srgb[s], order, parsed.lins, alphas)
        pred255 = [_clamp01(pred[c]) * 255.0 for c in range(3)]
        tgt255 = [parsed.tg_srgb[s][c] * 255.0 for c in range(3)]
        channel_sse = [(pred255[c] - tgt255[c]) ** 2 for c in range(3)]
        sse = sum(channel_sse)
        total += sse
        rounded = [max(0, min(255, int(round(v)))) for v in pred255]
        results.append(
            {
                "index": s,
                "background": parsed.bg_hex[s],
                "target": parsed.tg_hex[s],
                "predictedRgb": pred255,
                "predictedHex": "#{:02x}{:02x}{:02x}".format(*rounded),
                "targetRgb": tgt255,
                "channelSse": channel_sse,
                "sse": sse,
            }
        )
    return results, total


# --------------------------------------------------- 容差叠序枚举（共用） --


def _parse_nonneg_finite(raw: Any, scope: str, label: str) -> tuple[float | None, dict[str, Any] | None]:
    """校验「非负有限数字」。返回 (值, 错误)，二者恰有一个为 None。bool 视为非法。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None, _err(scope, f"{label}必须是非负有限的数字，实际为 {raw!r}")
    if raw < 0:
        return None, _err(scope, f"{label}不能为负：{raw}")
    return float(raw), None


def _enumerate_feasible_orders(
    parsed: _Parsed,
) -> tuple[list[tuple[float, tuple[str, ...], tuple[int, ...]]], float]:
    """枚举全部满足先后关系的叠序，返回 [(旧总误差, ID 序列, 排列), ...] 与最优总误差。

    复核必须在全部合规叠序上进行，不能只看建议页展示的前 12 条。
    """
    n = len(parsed.ids)
    pairs = parsed.pairs

    def feasible(perm: tuple[int, ...]) -> bool:
        pos = [0] * n
        for p, idx in enumerate(perm):
            pos[idx] = p
        return all(pos[li] < pos[ui] for li, ui in pairs)

    evaluated: list[tuple[float, tuple[str, ...], tuple[int, ...]]] = []
    best_sse = math.inf
    for perm in itertools.permutations(range(n)):
        if not feasible(perm):
            continue
        sse = _total_sse(parsed, list(perm), parsed.alphas)
        best_sse = min(best_sse, sse)
        evaluated.append((sse, tuple(parsed.ids[idx] for idx in perm), perm))
    return evaluated, best_sse


# ------------------------------------------------------------------- 求解 --


def solve(payload: Any) -> dict[str, Any]:
    parsed, errors = _parse_inputs(payload)
    if errors:
        raise CalibrationError(errors)
    _raise_if_cycle(parsed)

    n = len(parsed.ids)
    pairs = parsed.pairs

    # ---- 枚举排列并选优 ---------------------------------------------------
    def feasible(perm: tuple[int, ...]) -> bool:
        pos = [0] * n
        for p, idx in enumerate(perm):
            pos[idx] = p
        return all(pos[li] < pos[ui] for li, ui in pairs)

    best_sse = math.inf
    best_key: tuple[float, tuple[str, ...]] | None = None
    best_perm: tuple[int, ...] | None = None
    feasible_count = 0
    total_count = math.factorial(n)

    for perm in itertools.permutations(range(n)):
        if not feasible(perm):
            continue
        feasible_count += 1
        total_sse = _total_sse(parsed, list(perm), parsed.alphas)
        id_seq = tuple(parsed.ids[idx] for idx in perm)
        if best_key is None or (total_sse, id_seq) < best_key:
            best_sse = total_sse
            best_key = (total_sse, id_seq)
            best_perm = perm

    # 有环已在上面拦截；这里仅作防御
    if best_perm is None:  # pragma: no cover
        raise CalibrationError([_err("relations", "约束过强，没有任何可行的图层顺序")])

    assert best_perm is not None
    # 与 infer_opacities 的「调整前」走同一明细汇总，保证两边 totalSse 逐位一致
    sample_results, total_sse = _sample_details(parsed, list(best_perm), parsed.alphas)

    return {
        "order": [parsed.ids[idx] for idx in best_perm],
        "orderIndexes": list(best_perm),
        "layers": [
            {"id": parsed.ids[i], "color": parsed.hexes[i], "opacity": parsed.alphas[i]}
            for i in best_perm
        ],
        "totalSse": total_sse,
        "permutationsChecked": total_count,
        "feasibleCount": feasible_count,
        "samples": sample_results,
    }


# --------------------------------------------------------- 不透明度反推 --


def infer_opacities(payload: Any) -> dict[str, Any]:
    """顺序固定、其余图层不透明度保留原值，网格联合反推 1～2 个图层的不透明度。"""
    parsed, errors = _parse_inputs(payload)
    n = len(parsed.ids)
    id_set = {lid for lid in parsed.ids if lid}

    # ---- 底到顶顺序 -------------------------------------------------------
    raw_order = payload.get("order") if isinstance(payload, dict) else None
    order_ids: list[str] = []
    order_valid = True
    if not isinstance(raw_order, list):
        errors.append(
            _err("order", "缺少 order 列表或其格式不正确：必须按底→顶顺序给出全部图层 ID")
        )
        order_valid = False
    else:
        if len(raw_order) != n:
            errors.append(
                _err("order", f"order 必须恰好包含全部 {n} 个图层 ID（各一次），当前 {len(raw_order)} 个")
            )
            order_valid = False
        seen_order: set[str] = set()
        for x in raw_order:
            if not isinstance(x, str) or not x.strip():
                errors.append(_err("order", f"order 中的图层 ID 非法：{x!r}（必须是已存在的图层 ID）"))
                order_valid = False
                continue
            x = x.strip()
            if x not in id_set:
                errors.append(_err("order", f"order 中的图层 ID 不存在：{x!r}"))
                order_valid = False
            elif x in seen_order:
                errors.append(
                    _err("order", f"order 中图层 ID 重复：{x}，每个图层必须且只能出现一次")
                )
                order_valid = False
            else:
                seen_order.add(x)
            order_ids.append(x)

    # ---- 待调图层 ---------------------------------------------------------
    raw_adjust = payload.get("adjustIds") if isinstance(payload, dict) else None
    adjust_ids: list[str] = []
    if not isinstance(raw_adjust, list):
        errors.append(
            _err("adjust", "缺少 adjustIds 列表或其格式不正确：必须选择 1～2 个待反推图层 ID")
        )
    else:
        if not (MIN_ADJUST <= len(raw_adjust) <= MAX_ADJUST):
            errors.append(
                _err("adjust", f"待调图层必须为 {MIN_ADJUST}～{MAX_ADJUST} 个，当前 {len(raw_adjust)} 个")
            )
        seen_adjust: set[str] = set()
        for x in raw_adjust:
            if not isinstance(x, str) or not x.strip() or x.strip() not in id_set:
                errors.append(_err("adjust", f"待调图层 ID 不存在：{x!r}"))
                continue
            x = x.strip()
            if x in seen_adjust:
                errors.append(_err("adjust", f"待调图层 ID 重复：{x}，同一图层只能选择一次"))
                continue
            seen_adjust.add(x)
            adjust_ids.append(x)

    if errors:
        raise CalibrationError(errors)
    _raise_if_cycle(parsed)

    order_idx = [parsed.seen_ids[x] for x in order_ids]

    # ---- 顺序必须符合先后关系 ---------------------------------------------
    pos = [0] * n
    for p, idx in enumerate(order_idx):
        pos[idx] = p
    for li, ui in parsed.pairs:
        if pos[li] >= pos[ui]:
            raise CalibrationError(
                [
                    _err(
                        "order",
                        f"所给顺序不符合先后关系：{parsed.ids[li]} 必须在 {parsed.ids[ui]} 下方（底 → 顶）",
                    )
                ]
            )

    # 待调图层按 ID 字典序排列，元组决胜均以此顺序为准
    adjust_ids = sorted(adjust_ids)
    adjust_idx = [parsed.seen_ids[x] for x in adjust_ids]
    k = len(adjust_idx)
    original_tuple = tuple(round(parsed.alphas[i], 10) for i in adjust_idx)
    original_on_grid = all(abs(a - round(a, 2)) <= 1e-9 for a in original_tuple)

    # ---- 联合遍历 0.00～1.00（步长 0.01），并额外纳入原始参数组 -----------
    best_sse = math.inf
    best_tuple: tuple[float, ...] | None = None

    def consider(values: tuple[float, ...], total_sse: float) -> None:
        nonlocal best_sse, best_tuple
        if best_tuple is None or (total_sse, values) < (best_sse, best_tuple):
            best_sse = total_sse
            best_tuple = values

    alphas = parsed.alphas.copy()
    for steps in itertools.product(range(OPACITY_STEPS), repeat=k):
        combo = tuple(s / 100.0 for s in steps)
        for j, ai in enumerate(adjust_idx):
            alphas[ai] = combo[j]
        consider(combo, _total_sse(parsed, order_idx, alphas))

    if not original_on_grid:
        for j, ai in enumerate(adjust_idx):
            alphas[ai] = original_tuple[j]
        consider(original_tuple, _total_sse(parsed, order_idx, alphas))

    combos_checked = OPACITY_STEPS**k + (0 if original_on_grid else 1)
    assert best_tuple is not None

    # ---- 调整前后明细（建议值即最优元组；若原始参数组最优则建议=原值）-------
    before_samples, before_total = _sample_details(parsed, order_idx, parsed.alphas)

    after_alphas = parsed.alphas.copy()
    for j, ai in enumerate(adjust_idx):
        after_alphas[ai] = best_tuple[j]
    after_samples, after_total = _sample_details(parsed, order_idx, after_alphas)

    adjustments = [
        {
            "id": adjust_ids[j],
            "index": adjust_idx[j],
            "originalOpacity": parsed.alphas[adjust_idx[j]],
            "suggestedOpacity": round(best_tuple[j], 10),
        }
        for j in range(k)
    ]

    return {
        "order": [parsed.ids[i] for i in order_idx],
        "orderIndexes": order_idx,
        "adjustIds": adjust_ids,
        "layers": [
            {"id": parsed.ids[i], "color": parsed.hexes[i], "opacity": parsed.alphas[i]}
            for i in order_idx
        ],
        "adjustments": adjustments,
        "beforeTotalSse": before_total,
        "afterTotalSse": after_total,
        "beforeSamples": before_samples,
        "afterSamples": after_samples,
        "combosChecked": combos_checked,
        "opacityGrid": {"min": 0.0, "max": 1.0, "step": 0.01},
    }


# --------------------------------------------------------- 背景测量建议 --


def suggest_background(payload: Any) -> dict[str, Any]:
    """下一次背景测量建议。

    沿用当前图层、样本与先后关系：枚举合规叠序，保留总误差不高于
    最优总误差 + 容差的顺序（按总误差、ID 序列排序取前 12 条），再在每个
    候选背景上按现有线性光规则渲染这些顺序，以任意两条预测色未取整
    RGB（0–255 尺度）平方距离的最小值给背景打分；推荐得分最高者，
    同分取规范化 HEX 字典序最小者。容差内不足两条顺序时无需追加测量。
    """
    parsed, errors = _parse_inputs(payload)
    assert isinstance(payload, dict)  # _parse_inputs 已保证是 dict

    # ---- 候选背景 ---------------------------------------------------------
    raw_candidates = payload.get("candidateBackgrounds")
    if not isinstance(raw_candidates, list):
        errors.append(_err("candidates", "缺少 candidateBackgrounds 列表或其格式不正确"))
        raw_candidates = []
    if not (MIN_CANDIDATES <= len(raw_candidates) <= MAX_CANDIDATES):
        errors.append(
            _err(
                "candidates",
                f"候选背景数量必须在 {MIN_CANDIDATES}～{MAX_CANDIDATES} 个之间，当前 {len(raw_candidates)} 个",
            )
        )

    cand_hex: list[str] = []
    cand_srgb: list[tuple[float, float, float]] = []
    seen_cand: dict[str, int] = {}
    for i, item in enumerate(raw_candidates[:MAX_CANDIDATES]):
        try:
            norm, rgb = parse_hex(item)
        except ValueError:
            errors.append(
                _err("candidates", f"候选背景色非法：需要形如 #RRGGBB 的 HEX 值，实际为 {item!r}", i)
            )
            continue
        if norm in seen_cand:
            errors.append(
                _err("candidates", f"候选背景色重复：{norm}（与第 {seen_cand[norm] + 1} 个规范化后相同）", i)
            )
            continue
        seen_cand[norm] = i
        cand_hex.append(norm)
        cand_srgb.append(rgb)

    # ---- 误差容差 ----------------------------------------------------------
    raw_tol = payload.get("tolerance")
    tolerance: float
    tol_val, tol_err = _parse_nonneg_finite(raw_tol, "tolerance", "误差容差")
    if tol_err is not None:
        errors.append(tol_err)
        tolerance = 0.0
    else:
        assert tol_val is not None
        tolerance = tol_val

    if errors:
        raise CalibrationError(errors)
    _raise_if_cycle(parsed)

    # ---- 枚举合规叠序，按容差保留（枚举全部，展示才截取前 12 条）----------
    evaluated, best_sse_raw = _enumerate_feasible_orders(parsed)
    feasible_count = len(evaluated)
    limit = best_sse_raw + tolerance
    kept = [e for e in evaluated if e[0] <= limit]
    kept.sort(key=lambda e: (e[0], e[1]))
    within_count = len(kept)
    top = kept[:MAX_SUGGEST_ORDERS]

    # 展示用总误差走 _sample_details 汇总，与 /api/solve 的 totalSse 逐位一致
    best_display_perm = min(evaluated, key=lambda e: (e[0], e[1]))[2] if evaluated else None
    best_display_sse = (
        _sample_details(parsed, list(best_display_perm), parsed.alphas)[1]
        if best_display_perm is not None
        else best_sse_raw
    )

    # 展示用总误差走 _sample_details 汇总，与 /api/solve 的 totalSse 逐位一致
    orders: list[dict[str, Any]] = []
    for _sse, id_seq, perm in top:
        _, display_sse = _sample_details(parsed, list(perm), parsed.alphas)
        orders.append(
            {
                "order": list(id_seq),
                "orderIndexes": list(perm),
                "totalSse": display_sse,
            }
        )

    result: dict[str, Any] = {
        "tolerance": tolerance,
        "bestSse": best_display_sse,
        "feasibleCount": feasible_count,
        "withinCount": within_count,
        "orders": orders,
        "needsMeasurement": len(orders) >= 2,
        "candidates": [],
        "recommended": None,
    }

    if len(orders) < 2:
        return result  # 容差内不足两条顺序：当前样本已能唯一区分，无需追加测量

    # ---- 在每个候选背景上渲染并打分 ----------------------------------------
    order_perms = [list(perm) for _sse, _ids, perm in top]

    def pred_entry(k: int, preds255: list[tuple[float, float, float]]) -> dict[str, Any]:
        p = preds255[k]
        rounded = [max(0, min(255, int(round(v)))) for v in p]
        return {
            "order": orders[k]["order"],
            "orderIndexes": orders[k]["orderIndexes"],
            "predictedRgb": [p[0], p[1], p[2]],
            "predictedHex": "#{:02x}{:02x}{:02x}".format(*rounded),
        }

    recommended: dict[str, Any] | None = None
    for hex_norm, bg_srgb in zip(cand_hex, cand_srgb):
        preds255: list[tuple[float, float, float]] = []
        for perm in order_perms:
            pred = render_stack(bg_srgb, perm, parsed.lins, parsed.alphas)
            preds255.append((pred[0] * 255.0, pred[1] * 255.0, pred[2] * 255.0))

        # 任意两条顺序预测色的平方距离最小值即该背景得分；同分保留先枚举到的一对
        min_dist = math.inf
        min_pair = (0, 1)
        for i in range(len(preds255)):
            ai = preds255[i]
            for j in range(i + 1, len(preds255)):
                aj = preds255[j]
                d = (ai[0] - aj[0]) ** 2 + (ai[1] - aj[1]) ** 2 + (ai[2] - aj[2]) ** 2
                if d < min_dist:
                    min_dist = d
                    min_pair = (i, j)

        entry = {
            "hex": hex_norm,
            "score": min_dist,
            "pair": {"a": pred_entry(min_pair[0], preds255), "b": pred_entry(min_pair[1], preds255)},
        }
        result["candidates"].append(entry)
        if (
            recommended is None
            or entry["score"] > recommended["score"]
            or (entry["score"] == recommended["score"] and entry["hex"] < recommended["hex"])
        ):
            recommended = entry

    result["recommended"] = recommended
    return result


# ------------------------------------------------------------- 实测复核 --


def verify_measurement(payload: Any) -> dict[str, Any]:
    """实测颜色复核。

    沿用当前图层、旧样本与先后关系，以及建议阶段的容差：
    1. 枚举全部合规叠序，保留旧样本总误差 ≤ 最优总误差 + 容差的全部顺序
       （建议页只展示前 12 条，但复核必须覆盖其余叠序，不能漏）；
    2. 在选定的候选背景上以未取整 0～255 RGB 渲染每条顺序，与实测目标色
       计算平方误差，只保留不超过新测量误差上限者；
    3. 按（新测量误差、旧总误差、ID 序列）排序，返回前 12 条，同时给出
       过滤前/后的总数。无匹配时说明实测与现有候选不相容；有匹配且旧样本
       不足 4 组时，可由调用方把该测量追加为样本并触发原求解。
    """
    parsed, errors = _parse_inputs(payload)
    assert isinstance(payload, dict)  # _parse_inputs 已保证是 dict

    # ---- 选定的候选背景 ---------------------------------------------------
    raw_background = payload.get("background")
    bg_hex = "#000000"
    bg_srgb: tuple[float, float, float] = (0.0, 0.0, 0.0)
    try:
        bg_hex, bg_srgb = parse_hex(raw_background)
    except ValueError:
        errors.append(
            _err(
                "background",
                f"复核背景色非法：需要形如 #RRGGBB 的 HEX 值，实际为 {raw_background!r}",
            )
        )

    # ---- 实测目标色 -------------------------------------------------------
    raw_measured = payload.get("measuredTarget")
    measured_hex = "#000000"
    measured_srgb: tuple[float, float, float] = (0.0, 0.0, 0.0)
    try:
        measured_hex, measured_srgb = parse_hex(raw_measured)
    except ValueError:
        errors.append(
            _err(
                "measurement",
                f"实测目标色非法：需要形如 #RRGGBB 的 HEX 值，实际为 {raw_measured!r}",
            )
        )

    # ---- 容差（沿用建议阶段）与新测量误差上限 -----------------------------
    raw_tol = payload.get("tolerance")
    tolerance: float
    tol_val, tol_err = _parse_nonneg_finite(raw_tol, "tolerance", "误差容差")
    if tol_err is not None:
        errors.append(tol_err)
        tolerance = 0.0
    else:
        assert tol_val is not None
        tolerance = tol_val

    raw_bound = payload.get("measurementBound")
    measurement_bound: float
    bound_val, bound_err = _parse_nonneg_finite(raw_bound, "bound", "新测量误差上限")
    if bound_err is not None:
        errors.append(bound_err)
        measurement_bound = 0.0
    else:
        assert bound_val is not None
        measurement_bound = bound_val

    if errors:
        raise CalibrationError(errors)
    _raise_if_cycle(parsed)

    # ---- 第一步：旧样本容差筛选（全部合规叠序，不取前 12）----------------
    evaluated, best_sse_raw = _enumerate_feasible_orders(parsed)
    feasible_count = len(evaluated)
    limit = best_sse_raw + tolerance
    within = [e for e in evaluated if e[0] <= limit]
    within.sort(key=lambda e: (e[0], e[1]))
    within_count = len(within)

    # 展示用旧总误差走 _sample_details 汇总，与 /api/solve 的 totalSse 逐位一致
    best_perm = min(evaluated, key=lambda e: (e[0], e[1]))[2] if evaluated else None
    best_sse = (
        _sample_details(parsed, list(best_perm), parsed.alphas)[1]
        if best_perm is not None
        else best_sse_raw
    )

    # ---- 第二步：选定背景上的新测量误差筛选 -------------------------------
    tgt255 = tuple(measured_srgb[c] * 255.0 for c in range(3))
    matched: list[dict[str, Any]] = []
    for _raw_sse, id_seq, perm in within:
        pred = render_stack(bg_srgb, list(perm), parsed.lins, parsed.alphas)
        pred255 = (pred[0] * 255.0, pred[1] * 255.0, pred[2] * 255.0)
        new_sse = sum((pred255[c] - tgt255[c]) ** 2 for c in range(3))
        if new_sse > measurement_bound:
            continue
        _, old_sse = _sample_details(parsed, list(perm), parsed.alphas)
        rounded = [max(0, min(255, int(round(v)))) for v in pred255]
        matched.append(
            {
                "order": list(id_seq),
                "orderIndexes": list(perm),
                "totalSse": old_sse,
                "measurementSse": new_sse,
                "predictedRgb": [pred255[0], pred255[1], pred255[2]],
                "predictedHex": "#{:02x}{:02x}{:02x}".format(*rounded),
            }
        )

    matched_count = len(matched)
    # 按（新测量误差、旧总误差、ID 序列）排序；页面只取前 12，总数不受影响
    matched.sort(key=lambda m: (m["measurementSse"], m["totalSse"], tuple(m["order"])))
    orders = matched[:MAX_SUGGEST_ORDERS]

    return {
        "background": bg_hex,
        "measuredTarget": measured_hex,
        "tolerance": tolerance,
        "measurementBound": measurement_bound,
        "bestSse": best_sse,
        "feasibleCount": feasible_count,
        "withinCount": within_count,
        "matchedCount": matched_count,
        "sampleCount": len(parsed.bg_hex),
        "maxSamples": MAX_SAMPLES,
        "canAppendSample": matched_count > 0 and len(parsed.bg_hex) < MAX_SAMPLES,
        "compatible": matched_count > 0,
        "orders": orders,
    }
