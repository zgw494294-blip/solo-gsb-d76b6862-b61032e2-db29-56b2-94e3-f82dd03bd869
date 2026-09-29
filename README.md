# 透明图层叠序校准台

本地 Web 应用：录入若干个纯色半透明图层与多组「背景色 / 目标色」样本，自动枚举所有底 → 顶叠放顺序，找出对全部样本整体拟合最好的图层顺序；顺序确定后还可以**从目标色反推 1～2 个图层的建议不透明度**；当容差内仍存在多条难分的叠序时，可进一步给出**下一次背景测量建议**——从候选背景中挑出最能把这些顺序的预测色拉开差距的一个。

- **后端**：Python + FastAPI（计算核心仅用标准库，便于审阅与测试）
- **前端**：原生 HTML / CSS / JavaScript，无任何构建步骤与外部 CDN 依赖
- **色彩管线**：标准 sRGB 分段函数 ↔ 线性光，在线性光空间做 Porter-Duff alpha 合成
- **择优准则**：所有样本、RGB 三通道在 **0–255 未取整** 尺度上的平方误差总和；总误差相同时，按图层 **ID 序列字典序**（反推时按待调参数元组）决胜

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。

```bash
docker compose up --build
```

启动后用浏览器访问：

```
http://localhost:8000
```

停止：在另一个终端执行 `docker compose down`（或 Ctrl+C 后再 `down` 清理容器）。

## 功能说明

| 区域 | 规则 |
| --- | --- |
| 图层 | 2～7 个；ID 必须唯一且非空；颜色支持 `#RGB` / `#RRGGBB`（`#` 可省略，大小写不限）；不透明度 0～1 |
| 样本 | 1～4 组「背景色 + 目标色」；所有样本共用同一底到顶顺序，误差跨样本求和 |
| 先后关系 | 0～12 条「某图层在更下方 / 另一图层在其上方」约束；形成有向环时明确提示冲突链，例如 `A → B → A` |
| 结果 | 最终顺序（带半透明色点、底→顶方向）、总平方误差、枚举/可行排列数；每个样本并排显示目标色与预测色、HEX、RGB 与 R/G/B 分通道平方误差 |
| 不透明度反推 | 顺序确定后，沿用当前图层、样本与先后关系：确认一条底→顶顺序并勾选 1～2 个待调图层，其余图层保留原不透明度。联合遍历待调图层 0.00～1.00、步长 0.01 的全部组合（原始参数组若落在网格外也额外纳入），按同一未取整 RGB 平方误差总和选最优；同分时按 **ID 字典序排列的待调参数元组从小到大** 决胜。页面展示原值 / 建议值、调整前后总误差、每组样本的目标色与调整前后预测色，可一键把建议值写回图层并重新求解 |
| 下一次背景测量建议 | 沿用当前图层、样本与先后关系，录入 **2～12 个**规范化后互不重复的候选背景 HEX 与**非负有限**的误差容差：枚举合规叠序，保留总误差 ≤ 最优总误差 ＋ 容差的顺序，按（总误差、ID 序列）排序取前 12 条展示，并给出符合容差的总数；在每个候选背景上按同一线性光规则渲染这些顺序，以**任意两条预测色未取整 RGB（0–255）平方距离的最小值**给背景打分，推荐得分最高者（同分取规范化 HEX 字典序最小者），并展示推荐背景上最难区分的一对顺序及其预测色；容差内不足两条顺序时提示无需追加测量 |

输入任意改动后自动重新求解（带防抖），非法颜色、重复 ID、越界不透明度等错误会在页面顶部以中文逐条列出，并暂停计算。反推与背景建议为手动触发；输入变化后，旧结果会在卡片内标记为「可能过期」。

### 色彩与合成细节

1. HEX → sRGB(0～1)；按 IEC 61966-2-1 分段函数转为线性光：
   - `c ≤ 0.04045`：`c / 12.92`
   - 否则：`((c + 0.055) / 1.055) ** 2.4`
2. 以不透明背景色（α=1）打底，按底到顶顺序逐层做 "over" 合成（预乘形式）。
3. 结果经逆函数转回 sRGB，误差比较使用未四舍五入的浮点 RGB × 255 值；页面上的预测 HEX 才是取整后的值。
4. 对 7 个图层最多枚举 7! = 5040 个排列，逐排列过滤约束并评估，响应为毫秒级。

## API

`POST /api/solve`

```json
{
  "layers": [
    { "id": "R", "color": "#ff0000", "opacity": 0.5 },
    { "id": "B", "color": "#0000ff", "opacity": 0.5 }
  ],
  "samples": [
    { "background": "#ffffff", "target": "#e189bc" }
  ],
  "relations": [
    { "lower": "R", "upper": "B" }
  ]
}
```

成功返回 `order`（底→顶 ID 序列）、`orderIndexes`、`layers`（同序）、`totalSse`、`permutationsChecked`、`feasibleCount` 及每个样本的 `target / predictedHex / predictedRgb / channelSse / sse`。

输入非法或约束成环时返回 HTTP 400：

```json
{ "errors": [ { "scope": "layers", "index": 0, "message": "不透明度越界：1.2，必须在 0～1 之间" } ] }
```

`scope` 取值为 `layers` / `samples` / `relations` / `form`。

### `POST /api/infer-opacity` — 不透明度反推

在已有图层、样本与先后关系不变的前提下，显式给出一条底→顶 `order`（必须**恰好包含全部图层 ID 且各一次**，并满足全部先后关系），再给出 `adjustIds`（**1～2 个**已存在、不重复的图层 ID，服务端按 ID 字典序排列作为参数元组序）。其余图层保留输入中的原不透明度。

```bash
curl -s -X POST http://localhost:8000/api/infer-opacity \
  -H "Content-Type: application/json" \
  -d '{
    "layers": [
      { "id": "R", "color": "#ff0000", "opacity": 0.5 },
      { "id": "B", "color": "#0000ff", "opacity": 0.5 }
    ],
    "samples": [
      { "background": "#ffffff", "target": "#e189bc" }
    ],
    "relations": [
      { "lower": "R", "upper": "B" }
    ],
    "order": ["R", "B"],
    "adjustIds": ["R", "B"]
  }'
```

成功返回（节选）：

```json
{
  "order": ["R", "B"],
  "orderIndexes": [0, 1],
  "adjustIds": ["B", "R"],
  "layers": [
    { "id": "R", "color": "#ff0000", "opacity": 0.5 },
    { "id": "B", "color": "#0000ff", "opacity": 0.5 }
  ],
  "adjustments": [
    { "id": "B", "index": 1, "originalOpacity": 0.5, "suggestedOpacity": 0.25 },
    { "id": "R", "index": 0, "originalOpacity": 0.5, "suggestedOpacity": 0.67 }
  ],
  "beforeTotalSse": 2745.3695,
  "afterTotalSse": 1.4198,
  "beforeSamples": [
    { "index": 0, "background": "#ffffff", "target": "#e189bc",
      "predictedHex": "#bc89e1", "predictedRgb": [187.52, 136.96, 224.61],
      "channelSse": [1405.05, 0.0, 1340.32], "sse": 2745.3695 }
  ],
  "afterSamples": [
    { "index": 0, "background": "#ffffff", "target": "#e189bc",
      "predictedHex": "#e188bb", "predictedRgb": [224.61, 136.33, 187.10],
      "channelSse": [0.15, 0.45, 0.82], "sse": 1.4198 }
  ],
  "combosChecked": 10201,
  "opacityGrid": { "min": 0.0, "max": 1.0, "step": 0.01 }
}
```

说明：

- 遍历组合数为 101（调 1 个）或 101² = 10201（调 2 个）；当原始参数组不在 0.01 网格上（如 0.333）时，会把这一个原始元组**额外**纳入评估，`combosChecked` 相应 +1，以保证建议不会差于原值。若原始元组本身最优，建议值就等于原值，页面标注「原值即最优」；应用回图层后与页面预测逐位一致。
- `beforeSamples` / `afterSamples` 结构与 `/api/solve` 的 `samples` 一致（`target / predictedHex / predictedRgb / channelSse / sse`），分别对应调整前、后的预测。
- 同分决胜：按 `adjustIds` 字典序排列的待调参数元组从小到大（如 `(0.0, 0.3)` 优于 `(0.0, 0.4)`，`(0.1, 0.0)` 优于 `(0.2, 0.0)`）。
- 典型 400 报错（HTTP 状态码仍为 400，`scope` 为 `order` / `adjust`，其余校验错误沿用原 scope）：

```json
{ "errors": [
  { "scope": "order",  "message": "order 必须恰好包含全部 3 个图层 ID（各一次），当前 2 个" },
  { "scope": "order",  "message": "order 中图层 ID 重复：R，每个图层必须且只能出现一次" },
  { "scope": "order",  "message": "所给顺序不符合先后关系：R 必须在 B 下方（底 → 顶）" },
  { "scope": "adjust", "message": "待调图层 ID 重复：R，同一图层只能选择一次" },
  { "scope": "adjust", "message": "待调图层 ID 不存在：'X'" }
] }
```

### `POST /api/suggest-background` — 下一次背景测量建议

在 `/api/solve` 请求体的基础上增加两个字段，其余字段（`layers` / `samples` / `relations`）含义与校验完全沿用：

- `candidateBackgrounds`：**2～12 个**候选背景 HEX（`#RGB` / `#RRGGBB`，`#` 可省略），**规范化后互不重复**；
- `tolerance`：**非负有限**数字的误差容差。

服务端枚举合规叠序，保留总误差 ≤ 最优总误差 ＋ 容差的顺序，按（总误差、ID 序列）排序取前 12 条；再在每个候选背景上按同一线性光规则渲染这些顺序，以任意两条预测色**未取整 RGB（0–255 尺度）平方距离的最小值**给背景打分，推荐得分最高者（同分取规范化 HEX 字典序最小者）。

```bash
curl -s -X POST http://localhost:8000/api/suggest-background \
  -H "Content-Type: application/json" \
  -d '{
    "layers": [
      { "id": "R", "color": "#ff0000", "opacity": 0.5 },
      { "id": "B", "color": "#0000ff", "opacity": 0.5 }
    ],
    "samples": [
      { "background": "#ffffff", "target": "#e189bc" }
    ],
    "relations": [],
    "candidateBackgrounds": ["#ffffff", "#000000", "#808080"],
    "tolerance": 10000
  }'
```

成功返回（节选）：

```json
{
  "tolerance": 10000.0,
  "bestSse": 0.3876,
  "feasibleCount": 2,
  "withinCount": 2,
  "orders": [
    { "order": ["B", "R"], "orderIndexes": [1, 0], "totalSse": 0.3876 },
    { "order": ["R", "B"], "orderIndexes": [0, 1], "totalSse": 2745.3695 }
  ],
  "needsMeasurement": true,
  "candidates": [
    { "hex": "#ffffff", "score": 2751.9816,
      "pair": { "a": { "order": ["B", "R"], "predictedHex": "#e189bc",
                       "predictedRgb": [224.61, 136.96, 187.52] },
                "b": { "order": ["R", "B"], "predictedHex": "#bc89e1",
                       "predictedRgb": [187.52, 136.96, 224.61] } } },
    { "hex": "#000000", "score": 5111.7888, "pair": { "a": "…", "b": "…" } },
    { "hex": "#808080", "score": 4331.6991, "pair": { "a": "…", "b": "…" } }
  ],
  "recommended": { "hex": "#000000", "score": 5111.7888, "pair": { "a": "…", "b": "…" } }
}
```

说明：

- `withinCount` 为符合容差的叠序总数（可能多于 12），`orders` 只含排序后的前 12 条；`bestSse` 与 `/api/solve` 的 `totalSse` 逐位一致。
- `needsMeasurement` 为 `false`（容差内不足两条顺序）时，`candidates` 为空、`recommended` 为 `null`，表示当前样本已能唯一区分叠序，无需追加测量。
- 每个候选背景的 `pair` 是得分来源——该背景上最难区分的一对顺序及其未取整预测色（`predictedRgb` 为 0–255 浮点，`predictedHex` 为取整后的展示值）；`recommended` 是得分最高的候选，同分时取规范化 HEX 字典序最小者。
- 典型 400 报错（`scope` 为 `candidates` / `tolerance`，其余校验错误沿用原 scope）：

```json
{ "errors": [
  { "scope": "candidates", "index": 1, "message": "候选背景色重复：#ffffff（与第 1 个规范化后相同）" },
  { "scope": "candidates", "index": 2, "message": "候选背景色非法：需要形如 #RRGGBB 的 HEX 值，实际为 '#gg0000'" },
  { "scope": "candidates", "message": "候选背景数量必须在 2～12 个之间，当前 1 个" },
  { "scope": "tolerance",  "message": "误差容差不能为负：-1" },
  { "scope": "tolerance",  "message": "误差容差必须是非负有限的数字，实际为 'abc'" }
] }
```

## 不用 Docker 的本地运行方式（可选）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

计算核心零第三方依赖，可直接 `python3 -c "from app import core; ..."` 单测。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── core.py        # sRGB 转换 / alpha 合成 / 环检测 / 枚举择优 / 不透明度反推 / 背景测量建议（纯标准库）
│   └── main.py        # FastAPI：/api/solve、/api/infer-opacity、/api/suggest-background 与静态页面托管
├── static/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```
