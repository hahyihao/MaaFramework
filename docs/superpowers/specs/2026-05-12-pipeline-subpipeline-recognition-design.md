# Pipeline `SubPipeline` Recognition Type — 设计文档

**日期：** 2026-05-12
**目标：** 为 MaaFramework pipeline 引擎引入一种新的 recognition 类型 `SubPipeline`，让父节点把"是否命中"的判定外包给一个子流水线（"识别代理"语义），从而支持"main 只做任务优先级编排、识别下放到子文件"这种使用模式。

---

## 1. 背景与动机

### 1.1 现状

会话上一阶段（phase1-loopscan-done / phase2-subpipeline-done）已经落地：

| 字段 | 语义 |
|------|------|
| `task_mode: "loop_scan"` | 父任务持续循环扫描（外层 while + 内层 execute_once） |
| `[Fallback]` 节点属性 | 当前层全 miss 时触发的兜底 |
| `sub_pipeline: "<entry>"` | **父节点命中后**调子例程（subroutine 语义） |
| 文件级 FQN | `battle/fight::兜底` 形式的自动命名空间注入 |

### 1.2 真实使用场景

```
main (loop_scan)
  next: [尝试主页面, 尝试弹窗, ...]

尝试主页面 → 子文件"主页面.json"  ── 子文件里编排识别 主页面_按钮A / 主页面_按钮B / ...
尝试弹窗   → 子文件"弹窗.json"    ── 子文件里编排识别 弹窗_关闭 / 弹窗_确认 / ...
```

期望行为：

- `main` 只负责"任务优先级编排"：先试主页面，再试弹窗
- 识别逻辑下放到子文件里（每个子文件是一组识别候选）
- **子文件任一命中 → 父算命中 → 走父的 next（loop_scan 重新扫描）**
- **子文件全 miss → 父算 miss → main 顺序走下一个候选**

### 1.3 GAP

现有 `sub_pipeline` 字段是 **"父命中后调子例程"** 语义 —— 父必须先自己 reco hit，子层只在命中后被调用，子层的命中/miss **不会反过来影响父的判定**。无法表达上述"识别外包"场景。

绕法 A（探针图）/ 绕法 B（扁平化）都不优雅：A 要重复识别一张代表性图、B 失去模块化。

### 1.4 目标 / 非目标

**目标：**

- 引入新的 recognition 类型 `SubPipeline`，配套字段 `recognition_pipeline`
- 子流水线 single-pass 扫描，结果 hit/miss 直接映射为父节点 reco hit/miss
- 子层命中节点的 box 上浮作为父节点 reco 的 box（便于父 action 复用）
- 子流水线本身就是"片段化的 main"，写法完全一致

**非目标：**

- ❌ 不修改现有 `sub_pipeline` 字段语义（保持子例程语义）
- ❌ 不引入显式参数传递 / 返回值（box 上浮够用）
- ❌ 不引入新的循环 / 重试机制（复用 `loop_scan`）
- ❌ 不引入子层独立的超时控制（复用 `kMaxNestingDepth=8`）

---

## 2. JSON 语法

### 2.1 字段

```json
{
  "尝试主页面": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "主页面入口",
    "action": "DoNothing",
    "next": ["..."]
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `recognition` | string | 新增枚举值 `"SubPipeline"` |
| `recognition_pipeline` | string | 必填。指向同文件内 / 其他文件的某个节点。支持裸名 + FQN，按现有 `sub_pipeline` / `fallback_node` 字段相同规则解析 |

### 2.2 与现有字段的关系

- `recognition: "SubPipeline"` 时 `template` / `expected` / `model` 等 reco 子字段被忽略
- `action` / `next` / `on_error` / `pre_delay` / `post_delay` 等节点字段语义不变
- 父节点**可以同时**声明 `sub_pipeline: "Y"`：`SubPipeline reco hit → 父 action → 进入 Y 子例程 → 然后走 next`

---

## 3. 执行语义

### 3.1 单次调用流程

```
父节点 (recognition: SubPipeline, recognition_pipeline: X) 的 reco 阶段：
  ├─ depth_guard：if (current_depth + 1 > kMaxNestingDepth) → return miss
  ├─ 调用 execute_once(X_entry, current_depth + 1)
  │     ├─ 子层从 X_entry 开始按现有 single-pass 逻辑扫描
  │     ├─ 子层节点的 reco / action 照常跑（命中那个节点的 action 会执行）
  │     ├─ 子层中再嵌套 recognition: SubPipeline 合法，深度继续 +1
  │     └─ 返回 SubExecResult { hit: bool, hit_box: optional<Rect>, hit_detail: ... }
  │
  ├─ hit  → 父 reco hit
  │         父节点 reco 结果 box = hit_box（子层命中节点的 box 上浮）
  │         父节点 reco 结果 detail = hit_detail
  │         → 父执行自己的 action（DoNothing / Click / Custom …）
  │         → 走父的 next（或 loop_scan 重新从头扫描）
  │
  └─ miss → 父 reco miss
            → main / 上层 next 顺序试下一个候选
```

### 3.2 子流水线即"片段化的 main"

子流水线**按和 main 完全一样的方式写**（pipeline 节点的全部字段都可用），唯一约束是：

- **single-pass**：子层不循环。即使 entry 节点声明了 `task_mode: "loop_scan"`，在 `SubPipeline reco` 语境下也**被忽略**。
- **`[Fallback]` 被忽略**：子层节点的 `[Fallback]` 属性在 SubPipeline 语境下不生效（fallback 必命中会让"探针"失去意义）。
- **entry 节点的 `fallback_node` 字段被忽略**：同上原因。
- **entry 节点照常参与识别**：不做特殊处理。**用户负责**别把 entry 写成 `DirectHit` 占位符——否则子层第一帧立即命中、根本走不到 `entry.next`。文档需提供正确范例 + 警告。

### 3.3 嵌套

- 任何深度的子层都可以再 `recognition: SubPipeline`，最大深度复用 `kMaxNestingDepth=8`
- 超过深度限制 → 父 reco miss + warn 日志
- 同一个 entry 在不同调用路径上被重复进入是允许的（如 main → 主页面 → 主页面，只要不超过深度）；**自递归**（A → A）会被 `kMaxNestingDepth` 在 8 层内拦截、父 reco miss，不做启动期静态环检测

### 3.4 box 上浮的细节

子层 `execute_once` 内部如果发生了 `SubPipeline reco`，递归调用的 box 上浮是**逐层传递**的：

```
最深层 reco hit (box B) → 子层 hit, hit_box = B
                       → 上一层 父 reco hit, box = B
                       → 再上一层 …
                       → main 最终拿到的 reco 结果 box = B
```

---

## 4. 与现有特性的共存

### 4.1 `recognition: SubPipeline` + `sub_pipeline: "Y"` 字段同时声明

**合法、互不冲突**。两者职责正交：

| 阶段 | 字段 | 作用 |
|------|------|------|
| reco 阶段 | `recognition_pipeline: "X"` | 委托识别给 X 子流水线 |
| action 之后 | `sub_pipeline: "Y"` | 命中后调 Y 子例程（现有 phase2 语义） |

典型用法："父节点用 SubPipeline 确认当前界面是主页面，命中后跑主页面子例程"。

### 4.2 `recognition: SubPipeline` + `fallback_node` 字段

父节点自己的 `fallback_node` 在 **当前层全 miss 时** 触发（现有 phase1 语义），与 SubPipeline reco 无冲突 —— 父 reco miss 触发的是**上层**的 fallback / next 调度，父自己的 `fallback_node` 是它**自己作为某层根**时的兜底。两者层级不同。

### 4.3 `recognition: SubPipeline` + `task_mode: "loop_scan"`

合法。`task_mode` 作用在 **父节点作为任务入口** 时（外层 while 循环）；`SubPipeline reco` 作用在 **父节点的 reco 阶段**。组合起来即"loop_scan 主循环 + 每帧识别走 SubPipeline 委托"。

---

## 5. 错误处理

| 情况 | 行为 |
|------|------|
| `recognition_pipeline` 缺失或为空字符串 | 启动时 `PipelineChecker` 报错 |
| `recognition_pipeline` 指向不存在的节点 | 启动时 `PipelineChecker` 报错（同 `sub_pipeline` / `fallback_node` 校验路径） |
| 嵌套深度超过 `kMaxNestingDepth=8` | 运行时父 reco miss + warn 日志 |
| 子层自定义 reco / action 抛 C++ 异常 | 沿用现有 PipelineTask 异常处理；父 reco miss |
| 子层 execute_once 返回 hit 但 hit_box 为空 | 父 reco hit，box 默认空（与 DirectHit 行为一致） |

---

## 6. 命名空间 / FQN

`recognition_pipeline` 字段沿用 phase2 已有的 FQN 规则：

- 同文件内可写裸名（如 `"主页面入口"`），在 `parse_node` 里被自动 qualify 为 `<file>::主页面入口`
- 跨文件可写 FQN 全名（如 `"battle/fight::主页面入口"`）
- 运行时通过 `lookup_with_bare_fallback` 解析：精确匹配 → 不命中则在所有 FQN key 中查找唯一 `*::raw` 后缀

复用代码路径与 `sub_pipeline` / `fallback_node` 完全一致，**不需要新解析逻辑**。

---

## 7. 影响面（文件清单）

### 7.1 C++ 引擎

| 文件 | 修改 |
|------|------|
| `source/MaaFramework/Resource/PipelineTypes.h` | 在 `Recognition::Type` 枚举里加 `SubPipeline`；在 `PipelineData` 里加 `std::optional<std::string> recognition_pipeline` |
| `source/MaaFramework/Resource/PipelineParser.cpp` | `parse_recognition` 识别 `"SubPipeline"` 类型；解析 `recognition_pipeline` 字段并 qualify 为 FQN |
| `source/MaaFramework/Resource/PipelineChecker.cpp` | 校验 `recognition_pipeline` 引用合法 |
| `source/MaaFramework/Task/PipelineTask.cpp` | **核心改动**：在 reco 分发处加 `SubPipeline` 分支——绕过 `Vision` 模块，直接调 `execute_once`。详见 §8 |
| `source/MaaFramework/Resource/PipelineDumper.cpp` | 序列化 `SubPipeline` 类型和 `recognition_pipeline` 字段 |
| `source/MaaFramework/Resource/PipelineTypesV2.h` | `JPipelineData` 加 `recognition_pipeline` |

### 7.2 binding & schema

| 文件 | 修改 |
|------|------|
| `source/binding/Python/maa/pipeline.py` | `JPipelineData` 加 `recognition_pipeline`；recognition 字符串枚举加 `SubPipeline` |
| `source/binding/NodeJS/src/apis/pipeline.d.ts` | 同上，TypeScript 类型 |
| `tools/pipeline.schema.json` | `recognition` 字段 enum 加 `SubPipeline`；加 `recognition_pipeline` 字段定义 |

### 7.3 文档 & 测试

| 文件 | 修改 |
|------|------|
| `docs/zh_cn/3.1-任务流水线协议.md` | 在 recognition 章节加 `SubPipeline` 类型小节，含范例 + entry 写法警告 |
| `docs/en_us/3.1-PipelineProtocol.md` | 同上英文版 |
| `test/python/pipeline_test.py` | 加解析 / 校验单元测试 |
| `test/python/smoke_loopscan.py` | 扩展为 v3：增加"main 编排 + SubPipeline reco"场景验证 |

---

## 8. 关键技术挑战：跨模块调用

### 8.1 问题

现有 recognition 类型（TemplateMatch / OCR / FeatureMatch / Custom / DirectHit）都是 **无副作用**、由 `source/MaaFramework/Vision/` 模块实现。每个 reco 的 `analyze()` 只返回 box + detail，不触发 action、不嵌套调用 pipeline。

`SubPipeline reco` 不同：

- 涉及 `execute_once`，会执行子层 action（**有副作用**）
- 涉及节点 lookup、深度计数、状态机调度（**属于 Task 模块职责**）
- 如果放在 Vision 里实现，会形成 Vision → Task 的反向依赖

### 8.2 方案

**在 `PipelineTask::run_node` / `run_recognition` 里特判 `SubPipeline` 类型，绕过 Vision 模块、直接调用 `execute_once`。**

具体定位（伪代码）：

```cpp
// PipelineTask.cpp
NodeResult PipelineTask::run_recognition(const PipelineData& data, int depth) {
    if (data.recognition.type == Recognition::Type::SubPipeline) {
        // 不走 Vision，直接委托给 execute_once
        auto sub_result = execute_once(*data.recognition_pipeline, depth + 1);
        return NodeResult {
            .hit = sub_result.hit,
            .box = sub_result.hit_box,
            .detail = sub_result.hit_detail,
        };
    }
    // 其余类型走原有 Vision 调度路径
    return vision_recognize(data);
}
```

这样：
- 不破坏 Vision 模块的"无副作用 + 无 Pipeline 依赖"边界
- 所有"跑子层 → 取结果"的逻辑集中在 Task 层
- execute_once 已经具备深度计数 + 嵌套保护，复用即可

### 8.3 execute_once 返回值扩展

当前 `execute_once` 返回 `bool`（success / fail）。为了支持 box 上浮，需要返回 **结构体**：

```cpp
struct ExecOnceResult {
    bool hit = false;
    std::optional<cv::Rect> hit_box;
    std::string hit_detail;
};
```

**采用结构体返回**，不引入新的成员变量隐式状态。execute_once 的 `bool` 现有调用点（loop_scan 主循环、`sub_pipeline` 字段递归）只读取 `.hit` 字段、行为不变。

---

## 9. 测试策略

### 9.1 单元测试（`test/python/pipeline_test.py`）

- `_test_sub_pipeline_recognition_parse`：JSON 含 `recognition: SubPipeline + recognition_pipeline: X` 能正确解析
- `_test_sub_pipeline_recognition_invalid_ref`：`recognition_pipeline` 指向不存在的节点 → 启动失败
- `_test_sub_pipeline_recognition_fqn`：跨文件裸名引用 + FQN 引用都能解析

### 9.2 集成测试（扩展 `test/python/smoke_loopscan.py` → v3）

新场景：

```json
// main.json
{
  "MainEntry": {
    "task_mode": "loop_scan",
    "recognition": "DirectHit",
    "next": ["TryHomepage", "TryDialog"]
  },
  "TryHomepage": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "HomepageEntry",
    "action": "Custom",
    "custom_action": "OnHomepageHit"
  },
  "TryDialog": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "DialogEntry",
    "action": "Custom",
    "custom_action": "OnDialogHit"
  }
}

// homepage.json
{
  "HomepageEntry": {
    "recognition": "Custom",
    "custom_recognition": "HomepageReco",
    "next": ["HomeBtnA", "HomeBtnB"]
  },
  "HomeBtnA": { "recognition": "Custom", "custom_recognition": "BtnAReco", "action": "Custom", "custom_action": "BtnAAction" },
  "HomeBtnB": { "recognition": "Custom", "custom_recognition": "BtnBReco", "action": "Custom", "custom_action": "BtnBAction" }
}

// dialog.json
{
  "DialogEntry": {
    "recognition": "Custom",
    "custom_recognition": "DialogReco",
    "next": ["DialogClose"]
  },
  "DialogClose": { "recognition": "Custom", "custom_recognition": "DialogCloseReco", "action": "Custom", "custom_action": "DialogCloseAction" }
}
```

CustomRecognition 通过计数器和命中条件控制返回 hit/miss，断言：

| 阶段 | 验证 |
|------|------|
| 第 1 轮 | HomepageReco hit → BtnAReco hit → BtnAAction fired → OnHomepageHit fired → main 重新扫描 |
| 第 2 轮 | HomepageReco hit → BtnAReco miss → BtnBReco hit → BtnBAction fired → OnHomepageHit fired → main 重新扫描 |
| 第 3 轮 | HomepageReco miss → main 走 TryDialog → DialogReco hit → DialogCloseReco hit → ... |
| 第 4 轮 | HomepageReco miss → DialogReco miss → main 触发 stop |

---

## 10. 向后兼容

- ✅ 不修改任何现有字段 / 行为
- ✅ 新增 `recognition` 枚举值 `SubPipeline` 不影响其他值
- ✅ 新增 `recognition_pipeline` 字段仅在 `recognition == "SubPipeline"` 时生效
- ✅ 现有 `sub_pipeline` 字段语义不变

---

## 11. 待实施的实施计划

设计敲定后由 `superpowers:writing-plans` skill 把上述影响面拆成 bite-sized 任务表，按 phase1 / phase2 的同样节奏执行：

1. 数据结构（PipelineTypes.h）
2. 解析（PipelineParser）
3. 静态校验（PipelineChecker）
4. 运行时（PipelineTask::run_recognition 分支 + execute_once 返回结构体）
5. Dumper / V2 类型
6. binding（Python / NodeJS）
7. schema
8. 文档（中英）
9. 单元测试
10. 集成测试（smoke_loopscan v3）

每一步独立 commit，保持可追溯。
