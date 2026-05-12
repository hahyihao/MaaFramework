# Pipeline 循环扫描 + 子文件嵌套 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `PipelineTask` 增加 `loop_scan` 任务模式（循环扫描整条链 + 配置化兜底节点），并加上文件级命名空间和 `sub_pipeline` 嵌套调用，所有改动以可选字段形式接入，默认行为完全不变。

**Architecture:** 两期推进。Phase 1 把 `run()` 拆成 `run_state_machine` + `run_loop_scan` 分发，新模式通过 entry 节点的 `task_mode: "loop_scan"` 启用，兜底用 `[Fallback]` 节点属性，新增 `cycle_delay`/`cycle_delay_max` 字段表示循环间随机延迟。Phase 2 在 `PipelineResMgr` 加载期注入文件路径 FQN 前缀（`battle/fight::entry`），节点引用在 parse 期解析为绝对 FQN，新增 `sub_pipeline` 字段触发递归 `execute_once`，深度限制 8 层。

**Tech Stack:** C++20 / json::value / cmake (Ninja Multi-Config) / Python 集成测试 (`test/python/pipeline_test.py`)

**Spec:** `docs/superpowers/specs/2026-05-12-pipeline-loopscan-subpipeline-design.md`

---

## 通用构建/测试命令

每次需要验证时使用：

```bash
# 在仓库根目录
cmake --preset NinjaMulti
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install

# 跑 Python 集成测试（pipeline 解析相关）
python test/python/pipeline_test.py source/binding/Python install
```

**期望输出**：脚本末尾打印 `All tests passed!` 或所有 `PASS:` 行无 `FAIL`。

如果只想验证编译通过、不跑测试，省去 install 步骤即可。

---

## Phase 1 · 循环扫描 + 兜底 配置化

### Task 1: Baseline — 备份当前未提交改动并恢复 `PipelineTask::run()` 原状

**目的：** 把当前 hack 版本备份到一个 WIP 分支，主分支恢复到 `git HEAD` 状态作为 Phase 1 干净起点。

**Files:**
- 工作区现有未提交改动：
  - `source/MaaFramework/Resource/PipelineParser.cpp`
  - `source/MaaFramework/Resource/PipelineTypes.h`
  - `source/MaaFramework/Task/PipelineTask.cpp`

- [ ] **Step 1.1：备份 WIP 到独立分支**

```bash
git stash push -m "wip-loopscan-hack-2026-05-12" -- \
  source/MaaFramework/Resource/PipelineParser.cpp \
  source/MaaFramework/Resource/PipelineTypes.h \
  source/MaaFramework/Task/PipelineTask.cpp
git stash list
```

期望：列表第一条是 `stash@{0}: On main: wip-loopscan-hack-2026-05-12`

- [ ] **Step 1.2：确认工作区已经回到 HEAD 干净状态**

```bash
git status --short source/MaaFramework/
```

期望：上述 3 个文件都不再出现在输出中。

- [ ] **Step 1.3：验证恢复后能正常构建**

```bash
cmake --build build --config Debug --target MaaFramework
```

期望：构建成功，0 错误 0 警告。

---

### Task 2: 数据结构 — 在 `PipelineTypes.h` 加 `TaskMode` / `NodeAttr::is_fallback` / 新字段

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineTypes.h`

- [ ] **Step 2.1：在 `MAA_RES_NS` 顶部增加 `TaskMode` 枚举**

在文件中找到 `namespace MAA_RES_NS`（搜 `MAA_RES_NS_BEGIN`），紧跟 `enum class Type` 那块之后插入：

```cpp
enum class TaskMode {
    StateMachine = 0,
    LoopScan = 1,
};
```

- [ ] **Step 2.2：在 `NodeAttr` 结构体加 `is_fallback`**

```cpp
struct NodeAttr {
    std::string name;
    bool jump_back = false;
    bool anchor = false;
    bool is_fallback = false;     // 新增
};
```

- [ ] **Step 2.3：在 `PipelineData` 加四个新字段**

`PipelineData` 末尾、`json::object attach;` 之前插入：

```cpp
    // Phase 1 新增（仅 entry 节点起作用，但放在 PipelineData 上以统一序列化）
    TaskMode task_mode = TaskMode::StateMachine;
    std::optional<std::string> fallback_node;
    std::chrono::milliseconds cycle_delay { 1000 };
    std::chrono::milliseconds cycle_delay_max { 0 };
```

- [ ] **Step 2.4：补 `kNodeAttr_Fallback` 常量**

在 `PipelineData` 内已有的 `kNodeAttr_*` 常量定义旁边补：

```cpp
    inline static constexpr std::string_view kNodeAttr_Fallback = "[Fallback]";
```

- [ ] **Step 2.5：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

期望：构建成功（仅新增字段，未使用时不会有警告）。

- [ ] **Step 2.6：Commit**

```bash
git add source/MaaFramework/Resource/PipelineTypes.h
git commit -m "feat(pipeline): 添加 TaskMode 枚举与 loop_scan 模式所需字段

为后续 loop_scan 任务模式与 [Fallback] 节点属性预留数据结构。"
```

---

### Task 3: Parser — 解析 `[Fallback]` 节点属性

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineParser.cpp` (位置 `parse_node_string_in_next` 函数附近，约第 1832-1866 行)

- [ ] **Step 3.1：在 `parse_node_string_in_next` 增加 `[Fallback]` 分支**

定位现有代码：

```cpp
        if (attr == PipelineData::kNodeAttr_JumpBack) {
            output.jump_back = true;
        }
        else if (attr == PipelineData::kNodeAttr_Anchor) {
            output.anchor = true;
        }
        else {
            LogWarn << "Unrecognized node attribute" << VAR(attr) << VAR(raw);
        }
```

改成：

```cpp
        if (attr == PipelineData::kNodeAttr_JumpBack) {
            output.jump_back = true;
        }
        else if (attr == PipelineData::kNodeAttr_Anchor) {
            output.anchor = true;
        }
        else if (attr == PipelineData::kNodeAttr_Fallback) {
            output.is_fallback = true;
        }
        else {
            LogWarn << "Unrecognized node attribute" << VAR(attr) << VAR(raw);
        }
```

- [ ] **Step 3.2：在 `parse_node` 解析完 `next` / `on_error` 后，把 `[Fallback]` 的目标提到 `fallback_node` 字段**

定位 `parse_node`（搜 `bool PipelineParser::parse_node`）的尾部、在 return true 之前，加入：

```cpp
    // 从 next 中提取 [Fallback] 节点：作为 entry 节点的全局兜底
    {
        auto it = std::ranges::find_if(
            data.next,
            [](const NodeAttr& n) { return n.is_fallback; });
        if (it != data.next.end()) {
            data.fallback_node = it->name;
            // 兜底节点不参与主链扫描，从 next 列表移除
            data.next.erase(
                std::remove_if(data.next.begin(), data.next.end(),
                    [](const NodeAttr& n) { return n.is_fallback; }),
                data.next.end());
        }
    }
```

> ⚠️ 这里使用 `<algorithm>` 的 `std::remove_if` + `std::ranges::find_if`，确认文件顶部已 `#include <algorithm>` 或 `#include <ranges>`（PipelineParser.cpp 已有相关包含，无需补）。

- [ ] **Step 3.3：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

期望：构建成功。

- [ ] **Step 3.4：Commit**

```bash
git add source/MaaFramework/Resource/PipelineParser.cpp
git commit -m "feat(pipeline): 解析 [Fallback] 节点属性

新增 [Fallback] 前缀的节点属性。entry 节点的 next 中若出现 [Fallback]，
将该节点提取到 fallback_node 字段，并从主链 next 列表移除。"
```

---

### Task 4: Parser — 解析 `task_mode` 字段

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineParser.cpp`

- [ ] **Step 4.1：在 `parse_node` 解析 `enabled` 字段之后，加入 `task_mode` 解析**

定位 `parse_node` 中处理 `enabled` 的位置（搜 `"enabled"`），紧接着加入：

```cpp
    {
        std::string task_mode_str;
        if (!get_and_check_value(input, "task_mode", task_mode_str, std::string {})) {
            LogError << "failed to get_and_check_value task_mode" << VAR(input);
            return false;
        }
        if (task_mode_str.empty() || task_mode_str == "state_machine") {
            data.task_mode = TaskMode::StateMachine;
        }
        else if (task_mode_str == "loop_scan") {
            data.task_mode = TaskMode::LoopScan;
        }
        else {
            LogError << "invalid task_mode value (expected 'state_machine' or 'loop_scan')"
                     << VAR(task_mode_str) << VAR(input);
            return false;
        }
    }
```

- [ ] **Step 4.2：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

- [ ] **Step 4.3：Commit**

```bash
git add source/MaaFramework/Resource/PipelineParser.cpp
git commit -m "feat(pipeline): 解析 task_mode 字段

支持 entry 节点声明 'state_machine'（默认）或 'loop_scan' 模式。"
```

---

### Task 5: Parser — 解析 `cycle_delay` / `cycle_delay_max` + 修复 `rate_limit` 数组写法

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineParser.cpp`

- [ ] **Step 5.1：添加一个解析"标量或 [min, max] 数组"的辅助函数**

在文件顶部其他 `get_and_check_value_*` 模板下方插入：

```cpp
// Parse a duration field that accepts either a non-negative integer scalar
// or a [min, max] array of non-negative integers with min <= max.
// On scalar: out_min = value, out_max = 0.
// On array of 1 element: out_min = arr[0], out_max = 0.
// On array of 2 elements: out_min = arr[0], out_max = arr[1], asserts max >= min.
// On missing key: out_min = default_min, out_max = default_max.
static bool parse_duration_range(
    const json::value& input,
    const std::string& key,
    std::chrono::milliseconds& out_min,
    std::chrono::milliseconds& out_max,
    std::chrono::milliseconds default_min,
    std::chrono::milliseconds default_max)
{
    auto opt = input.find(key);
    if (!opt) {
        out_min = default_min;
        out_max = default_max;
        return true;
    }

    if (opt->is_array()) {
        const auto& arr = opt->as_array();
        if (arr.empty()) {
            LogError << "duration range array is empty" << VAR(key) << VAR(input);
            return false;
        }
        if (arr.size() > 2) {
            LogError << "duration range array must have 1 or 2 elements" << VAR(key) << VAR(input);
            return false;
        }
        for (const auto& e : arr) {
            if (!e.is_number()) {
                LogError << "duration range elements must be numbers" << VAR(key) << VAR(input);
                return false;
            }
            if (e.as_integer() < 0) {
                LogError << "duration range elements must be non-negative" << VAR(key) << VAR(input);
                return false;
            }
        }
        out_min = std::chrono::milliseconds(arr[0].as_integer());
        out_max = arr.size() == 2
                     ? std::chrono::milliseconds(arr[1].as_integer())
                     : std::chrono::milliseconds(0);
        if (out_max.count() > 0 && out_max < out_min) {
            LogError << "duration range max < min" << VAR(key) << VAR(input);
            return false;
        }
        return true;
    }

    if (opt->is_number()) {
        if (opt->as_integer() < 0) {
            LogError << "duration scalar must be non-negative" << VAR(key) << VAR(input);
            return false;
        }
        out_min = std::chrono::milliseconds(opt->as_integer());
        out_max = std::chrono::milliseconds(0);
        return true;
    }

    LogError << "duration field must be a number or [min,max] array" << VAR(key) << VAR(input);
    return false;
}
```

- [ ] **Step 5.2：替换现有 `rate_limit` 解析逻辑**

定位现有 `parse_node` 中的 `rate_limit` 解析块（搜 `"rate_limit"`，约第 255 行附近），替换为：

```cpp
    {
        std::chrono::milliseconds rate_min = default_value.rate_limit;
        std::chrono::milliseconds rate_max { 0 };  // rate_limit 暂不引入区间随机；仅修复解析
        if (!parse_duration_range(input, "rate_limit", rate_min, rate_max,
                                   default_value.rate_limit, std::chrono::milliseconds { 0 })) {
            LogError << "failed to parse rate_limit" << VAR(input);
            return false;
        }
        if (rate_max.count() > 0) {
            LogWarn << "rate_limit ignores max element (use cycle_delay for jitter)" << VAR(input);
        }
        data.rate_limit = rate_min;
    }
```

> 说明：保留 `rate_limit` 是单值语义（截图轮询间隔）。数组写法解析期接受但 max 被忽略并 warning，避免用户误以为 `rate_limit` 也支持随机。

- [ ] **Step 5.3：在 `parse_node` 中追加 `cycle_delay` / `cycle_delay_max` 解析**

紧接 rate_limit 解析之后插入：

```cpp
    if (!parse_duration_range(input, "cycle_delay",
                              data.cycle_delay, data.cycle_delay_max,
                              default_value.cycle_delay, default_value.cycle_delay_max)) {
        LogError << "failed to parse cycle_delay" << VAR(input);
        return false;
    }
```

- [ ] **Step 5.4：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

- [ ] **Step 5.5：Commit**

```bash
git add source/MaaFramework/Resource/PipelineParser.cpp
git commit -m "feat(pipeline): 解析 cycle_delay 区间字段并修复 rate_limit 数组解析

新增 parse_duration_range 辅助函数：接受标量或 [min,max] 数组并做边界校验。
cycle_delay 接入完整区间随机支持；rate_limit 保留单值语义，数组写法 max 段
被忽略并打 warning。"
```

---

### Task 6: PipelineTypesV2 — 同步序列化结构体

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineTypesV2.h`

- [ ] **Step 6.1：读取 PipelineTypesV2.h，找到对应 PipelineData 的序列化 J*** 结构体**

```bash
# 在编辑器中打开
source/MaaFramework/Resource/PipelineTypesV2.h
```

定位 `struct JPipelineData`（或同等命名）。

- [ ] **Step 6.2：在 JPipelineData 加新字段**

```cpp
struct JPipelineData {
    // ... 现有字段 ...

    // Phase 1 新增
    std::string task_mode = "state_machine";          // "state_machine" | "loop_scan"
    std::optional<std::string> fallback_node;
    json::value cycle_delay = 1000;                   // 可为 int 或 [int,int]
    std::optional<int64_t> cycle_delay_max;           // 仅在 cycle_delay 是标量+需要 max 时使用
    // ...

    MEO_JSONIZATION(
        // ... 现有字段透传 ...
        MEO_OPT task_mode,
        MEO_OPT fallback_node,
        MEO_OPT cycle_delay,
        MEO_OPT cycle_delay_max
        // ...
    );
};
```

> 实际字段顺序按文件原有风格补齐；`MEO_OPT` 是项目用的可选字段宏，参照该文件其它字段写法。

- [ ] **Step 6.3：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

- [ ] **Step 6.4：Commit**

```bash
git add source/MaaFramework/Resource/PipelineTypesV2.h
git commit -m "feat(pipeline): JPipelineData 同步 task_mode/fallback_node/cycle_delay"
```

---

### Task 7: PipelineDumper — 新字段输出

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineDumper.cpp`

- [ ] **Step 7.1：在 PipelineDumper 序列化函数中追加新字段**

打开 `PipelineDumper.cpp`，定位输出 `next` 列表的代码（处理 `[JumpBack]` / `[Anchor]` 前缀拼接的位置），按相同模式给 `is_fallback` 加前缀输出（用于把 `fallback_node` 还原到 next 列表的 `[Fallback]X` 形式）：

在 dump next 节点的字符串拼接处：

```cpp
std::string node_str;
if (attr.jump_back)  node_str += PipelineData::kNodeAttr_JumpBack;
if (attr.anchor)     node_str += PipelineData::kNodeAttr_Anchor;
if (attr.is_fallback) node_str += PipelineData::kNodeAttr_Fallback;
node_str += attr.name;
```

- [ ] **Step 7.2：dump entry 节点时把 `fallback_node` 还原回 next 列表末尾**

在 dump `PipelineData` 的主函数里、输出 `next` 字段之前插入：

```cpp
auto next_for_dump = data.next;
if (data.fallback_node) {
    NodeAttr fb { .name = *data.fallback_node, .is_fallback = true };
    next_for_dump.push_back(fb);
}
// 然后用 next_for_dump 替代 data.next 做后续序列化
```

- [ ] **Step 7.3：输出 task_mode / cycle_delay / cycle_delay_max 字段**

```cpp
json["task_mode"] = (data.task_mode == TaskMode::LoopScan) ? "loop_scan" : "state_machine";

if (data.cycle_delay_max.count() > 0) {
    json["cycle_delay"] = json::array {
        data.cycle_delay.count(),
        data.cycle_delay_max.count()
    };
}
else {
    json["cycle_delay"] = data.cycle_delay.count();
}
```

- [ ] **Step 7.4：构建确认**

```bash
cmake --build build --config Debug --target MaaFramework
```

- [ ] **Step 7.5：Commit**

```bash
git add source/MaaFramework/Resource/PipelineDumper.cpp
git commit -m "feat(pipeline): Dumper 输出 task_mode / cycle_delay / fallback_node"
```

---

### Task 9: PipelineTask — 拆分 `run()` 为模式分发

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.h`
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

- [ ] **Step 9.1：在 PipelineTask.h 声明两个新方法 + ScanOptions**

打开 `PipelineTask.h`，在 `class PipelineTask` 的 private 区域添加：

```cpp
private:
    bool run_state_machine(const std::string& entry);
    bool run_loop_scan(const std::string& entry);

    struct ScanOptions {
        bool single_pass = false;
    };
```

并修改现有 `run_next` 声明，增加 `ScanOptions` 参数（带默认值，保持调用兼容）：

```cpp
    NodeDetail run_next(
        const std::vector<MAA_RES_NS::NodeAttr>& next,
        const MAA_RES_NS::PipelineData& pretask,
        ScanOptions opts = {});
```

- [ ] **Step 9.2：在 PipelineTask.cpp 把原 `run()` 函数体改名为 `run_state_machine`**

```cpp
bool PipelineTask::run_state_machine(const std::string& entry)
{
    // 这里粘贴原来 run() 函数体内的全部代码
    // 把开头的 entry_ 引用改为参数 entry
    // 函数末尾的 return !error_handling 保留
}
```

> 注意：`run_state_machine` 内部对 `entry_` 的引用全部替换为参数 `entry`，方便后续 Phase 2 复用。

- [ ] **Step 9.3：写新的 `run()` 做模式分发**

```cpp
bool PipelineTask::run()
{
    if (!context_) {
        LogError << "context is null";
        return false;
    }

    LogFunc << VAR(entry_) << VAR(task_id_);

    auto begin_opt = context_->get_pipeline_data(entry_);
    if (!begin_opt) {
        LogError << "get_pipeline_data failed, task not exist" << VAR(entry_);
        return false;
    }

    switch (begin_opt->task_mode) {
    case MAA_RES_NS::TaskMode::LoopScan:
        return run_loop_scan(entry_);
    case MAA_RES_NS::TaskMode::StateMachine:
    default:
        return run_state_machine(entry_);
    }
}
```

- [ ] **Step 9.4：先写 `run_loop_scan` 桩，仅打日志直接返回 true**

```cpp
bool PipelineTask::run_loop_scan(const std::string& entry)
{
    LogInfo << "run_loop_scan (stub)" << VAR(entry);
    return true;
}
```

- [ ] **Step 9.5：构建确认 + 跑现有测试确保 state_machine 路径未坏**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

期望：所有现有测试 PASS（因为默认 `task_mode = StateMachine`，行为完全等同 HEAD）。

- [ ] **Step 9.6：Commit**

```bash
git add source/MaaFramework/Task/PipelineTask.h source/MaaFramework/Task/PipelineTask.cpp
git commit -m "refactor(pipeline): run() 拆分为 run_state_machine / run_loop_scan 分发

按 entry 节点的 task_mode 字段分发到对应实现；state_machine 路径保持原 run()
逻辑不变。loop_scan 暂为桩函数，下一 commit 实现。"
```

---

### Task 10: PipelineTask — 实现 `run_loop_scan` + `build_chain` + `run_fallback` + `ScanOptions`

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.h`
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

- [ ] **Step 10.1：在 PipelineTask.h 私有区添加辅助方法声明 + `<random>` include**

```cpp
private:
    std::vector<MAA_RES_NS::NodeAttr> build_chain(const std::string& entry);
    void run_fallback(const std::string& fallback_node_name);
    std::chrono::milliseconds sample_delay(
        std::chrono::milliseconds min_ms,
        std::chrono::milliseconds max_ms);
```

PipelineTask.cpp 顶部追加 `#include <random>`（若未存在）。

- [ ] **Step 10.2：实现 `sample_delay`**

```cpp
std::chrono::milliseconds PipelineTask::sample_delay(
    std::chrono::milliseconds min_ms,
    std::chrono::milliseconds max_ms)
{
    if (max_ms.count() <= 0 || max_ms <= min_ms) {
        return min_ms;
    }
    static thread_local std::mt19937 rng(
        static_cast<uint32_t>(std::chrono::steady_clock::now().time_since_epoch().count()));
    std::uniform_int_distribution<int64_t> dist(min_ms.count(), max_ms.count());
    return std::chrono::milliseconds(dist(rng));
}
```

- [ ] **Step 10.3：实现 `build_chain`**

```cpp
std::vector<MAA_RES_NS::NodeAttr> PipelineTask::build_chain(const std::string& entry)
{
    auto data_opt = context_->get_pipeline_data(entry);
    if (!data_opt) {
        LogError << "build_chain: entry not found" << VAR(entry);
        return {};
    }
    // entry 的 next 列表中 [Fallback] 节点已经在 parse 期被提取出去
    return data_opt->next;
}
```

- [ ] **Step 10.4：实现 `run_fallback`**

```cpp
void PipelineTask::run_fallback(const std::string& fallback_node_name)
{
    auto data_opt = context_->get_pipeline_data(fallback_node_name);
    if (!data_opt) {
        LogWarn << "fallback node not found" << VAR(fallback_node_name);
        return;
    }

    cur_node_ = fallback_node_name;

    cv::Mat image = screencap();
    if (image.empty()) {
        LogWarn << "fallback screencap empty";
        return;
    }

    RecoResult reco = run_recognition(image, *data_opt, std::nullopt, nullptr);
    if (reco.box) {
        run_action(reco, *data_opt);
    }
}
```

- [ ] **Step 10.5：修改 `run_next`，加 `ScanOptions` 参数**

定位 `run_next` 现有实现的核心 `while (!context_->need_to_stop())` 循环。改动思路：当 `opts.single_pass == true` 时，截图失败或本帧无命中要构造 `NodeDetail{ .node_id = node_id, .completed = false }` 并立即 return，不进入 `check_timeout_and_sleep` 的轮询。完整的 single_pass 路径示意：

```cpp
    while (!context_->need_to_stop()) {
        auto current_clock = std::chrono::steady_clock::now();
        cv::Mat image = screencap();

        if (image.empty()) {
            LogWarn << "screencap failed, skip recognition" << VAR(pretask.name);
            if (opts.single_pass) {
                NodeDetail r { .node_id = node_id, .completed = false };
                set_node_detail(r.node_id, r);
                notify(MaaMsg_Node_PipelineNode_Failed, node_cb_detail);
                return r;
            }
            if (!check_timeout_and_sleep(current_clock)) break;
            continue;
        }

        RecoResult reco = recognize_list(image, next);

        if (context_->need_to_stop()) break;

        if (!reco.box) {
            if (opts.single_pass) {
                NodeDetail r { .node_id = node_id, .completed = false };
                set_node_detail(r.node_id, r);
                notify(MaaMsg_Node_PipelineNode_Failed, node_cb_detail);
                return r;
            }
            if (!check_timeout_and_sleep(current_clock)) break;
            continue;
        }

        // ... 命中分支保持不变 ...
    }
```

- [ ] **Step 10.6：实现真正的 `run_loop_scan`**

替换 Task 9 写的桩：

```cpp
bool PipelineTask::run_loop_scan(const std::string& entry)
{
    auto entry_data_opt = context_->get_pipeline_data(entry);
    if (!entry_data_opt) {
        LogError << "run_loop_scan: entry not found" << VAR(entry);
        return false;
    }
    const auto entry_data = *entry_data_opt;

    auto chain = build_chain(entry);
    if (chain.empty()) {
        LogError << "run_loop_scan: empty chain" << VAR(entry);
        return false;
    }

    while (!context_->need_to_stop()) {
        cur_node_ = entry;
        auto hit = run_next(chain, entry_data, ScanOptions { .single_pass = true });

        if (context_->need_to_stop()) return true;

        if (hit.reco_id != MaaInvalidId && hit.completed) {
            // 命中并执行成功：什么都不需要额外做
            // （Phase 2 在此处加 sub_pipeline 递归调用）
        }
        else if (entry_data.fallback_node) {
            run_fallback(*entry_data.fallback_node);
        }

        std::this_thread::sleep_for(
            sample_delay(entry_data.cycle_delay, entry_data.cycle_delay_max));
    }

    return true;
}
```

- [ ] **Step 10.7：构建 + 跑既有测试（确保 state_machine 没回归）**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

期望：所有现有测试 PASS。

- [ ] **Step 10.8：Commit**

```bash
git add source/MaaFramework/Task/PipelineTask.h source/MaaFramework/Task/PipelineTask.cpp
git commit -m "feat(pipeline): 实现 loop_scan 模式

新增 run_loop_scan / build_chain / run_fallback / sample_delay，run_next
增加 single_pass 选项以支持单帧扫描。循环扫描整条 next 链，未命中走
fallback_node，每轮间按 cycle_delay/cycle_delay_max 区间随机等待。"
```

---

### Task 11: 可观察性 — `cur_node_` 正确更新

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

- [ ] **Step 11.1：在 `run_next` 命中节点后、`run_action` 调用前，更新 `cur_node_`**

定位 `run_next` 中处理命中（`reco.box` 非空且 `hit_opt` 已取到）的位置，在 `auto act = run_action(reco, *hit_opt);` 之前插入：

```cpp
        cur_node_ = hit_name;
```

- [ ] **Step 11.2：构建 + 跑测试**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

- [ ] **Step 11.3：Commit**

```bash
git add source/MaaFramework/Task/PipelineTask.cpp
git commit -m "fix(pipeline): cur_node_ 反映真实命中节点

修复 loop_scan 模式下 cur_node_ 永远是 entry 的可观察性问题。"
```

---

### Task 12: Python 绑定 — pipeline.py 同步

**Files:**
- Modify: `source/binding/Python/maa/pipeline.py`

- [ ] **Step 12.1：在 JNodeAttr 加 is_fallback**

定位 `JNodeAttr`（约 336 行）：

```python
@dataclass
class JNodeAttr:
    name: str = ""
    jump_back: bool = False
    anchor: bool = False
    is_fallback: bool = False     # 新增
```

- [ ] **Step 12.2：在 JPipelineData 加新字段**

定位 `JPipelineData`，在合适位置添加：

```python
    task_mode: str = "state_machine"
    fallback_node: Optional[str] = None
    cycle_delay: Union[int, List[int]] = 1000
    cycle_delay_max: Optional[int] = None
```

- [ ] **Step 12.3：在 `_parse_node_attr_list` 解析新字段**

定位 `_parse_node_attr_list`（约 519 行）：

```python
@staticmethod
def _parse_node_attr_list(data: List[dict]) -> List[JNodeAttr]:
    return [
        JNodeAttr(
            name=item.get("name", ""),
            jump_back=item.get("jump_back", False),
            anchor=item.get("anchor", False),
            is_fallback=item.get("is_fallback", False),
        )
        for item in data
    ]
```

- [ ] **Step 12.4：在 from_dict 解析新字段**

定位 `JPipelineData.from_dict`（约 500 行附近），追加：

```python
task_mode=data.get("task_mode", "state_machine"),
fallback_node=data.get("fallback_node"),
cycle_delay=data.get("cycle_delay", 1000),
cycle_delay_max=data.get("cycle_delay_max"),
```

- [ ] **Step 12.5：Commit**

```bash
git add source/binding/Python/maa/pipeline.py
git commit -m "feat(binding/python): 同步 task_mode/fallback_node/cycle_delay/is_fallback"
```

---

### Task 13: NodeJS 绑定同步（仅 TypeScript 类型定义）

**Files:**
- Modify: `source/binding/NodeJS/src/apis/pipeline.d.ts`

> 说明：NodeJS binding 的 pipeline 数据通过 JSON 桥接传输，没有专门的 C++ 转换层。
> 只需要在 `.d.ts` 类型定义里加新字段即可。

- [ ] **Step 13.1：在 NodeAttr 类型加 `is_fallback` 字段**

定位 `pipeline.d.ts:440-448` 的 `NodeAttr` 类型，加字段：

```typescript
type NodeAttr<Mode> = RequiredIfStrict<
    {
        name?: string
        jump_back?: boolean
        anchor?: boolean
        is_fallback?: boolean
    },
    'name',
    Mode
>
```

- [ ] **Step 13.2：在 General 类型加新字段**

定位同文件 `General<Mode>` 类型（约第 460 行），追加：

```typescript
type General<Mode> = {
    // ... 现有字段保留 ...
    task_mode?: 'state_machine' | 'loop_scan'
    cycle_delay?: number | [number, number]
}
```

- [ ] **Step 13.3：构建 NodeJS binding 验证**

```bash
cd source/binding/NodeJS
npm install
npm run build
```

期望：构建成功，TypeScript 类型检查无错误。

- [ ] **Step 13.4：Commit**

```bash
git add source/binding/NodeJS/src/apis/pipeline.d.ts
git commit -m "feat(binding/nodejs): TypeScript 类型同步 task_mode/cycle_delay/is_fallback"
```

---

### Task 14: pipeline.schema.json

**Files:**
- Modify: `tools/pipeline.schema.json`

- [ ] **Step 14.1：在 PipelineData definitions 加新字段 schema**

```json
"task_mode": {
    "type": "string",
    "enum": ["state_machine", "loop_scan"],
    "default": "state_machine",
    "description": "任务执行模式。state_machine 走传统状态机；loop_scan 循环扫描整条 next 链，命中执行/未命中走兜底，永不主动结束。仅 entry 节点该字段起作用。"
},
"cycle_delay": {
    "oneOf": [
        { "type": "integer", "minimum": 0 },
        {
            "type": "array",
            "items": { "type": "integer", "minimum": 0 },
            "minItems": 1,
            "maxItems": 2
        }
    ],
    "default": 1000,
    "description": "loop_scan 模式下每轮循环结束的等待时长。可写为标量或 [min, max] 区间。"
}
```

> `fallback_node` 不暴露在 schema 中：用户应在 next 列表中通过 `[Fallback]NodeName` 间接声明。

- [ ] **Step 14.2：在 next 子节点 schema 加 is_fallback 不必要（它是解析结果，不是输入）**

无改动。

- [ ] **Step 14.3：Commit**

```bash
git add tools/pipeline.schema.json
git commit -m "feat(schema): 补 task_mode / cycle_delay 字段定义"
```

---

### Task 15: Python 集成测试 — Phase 1 新字段解析

**Files:**
- Modify: `test/python/pipeline_test.py`

- [ ] **Step 15.1：在 `_test_node_attributes` 之后新增测试函数**

在 PipelineTestRecognition 类中，于 `_test_node_attributes` 之后追加：

```python
    def _test_loop_scan_mode(self, context: Context):
        """测试 task_mode / [Fallback] / cycle_delay 字段解析"""
        print("\n--- _test_loop_scan_mode ---")

        # 1. task_mode 默认为 state_machine
        ok = context.override_pipeline(
            {
                "DefaultModeNode": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                }
            }
        )
        assert_true(ok, "override_pipeline default mode")
        obj = context.get_node_object("DefaultModeNode")
        assert_eq(obj.task_mode, "state_machine", "default task_mode")

        # 2. task_mode = loop_scan
        ok = context.override_pipeline(
            {
                "LoopScanNode": {
                    "task_mode": "loop_scan",
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                    "next": ["A", "B", "[Fallback]MyFallback"],
                    "cycle_delay": 500,
                },
                "A": {"recognition": "DirectHit", "action": "DoNothing"},
                "B": {"recognition": "DirectHit", "action": "DoNothing"},
                "MyFallback": {"recognition": "DirectHit", "action": "DoNothing"},
            }
        )
        assert_true(ok, "override_pipeline loop_scan")

        obj = context.get_node_object("LoopScanNode")
        assert_eq(obj.task_mode, "loop_scan", "task_mode")
        assert_eq(obj.fallback_node, "MyFallback", "fallback_node extracted")
        assert_eq(len(obj.next), 2, "next 应只剩 A、B（[Fallback] 已被移出）")
        assert_eq(obj.next[0].name, "A", "next[0]")
        assert_eq(obj.next[1].name, "B", "next[1]")
        assert_eq(obj.cycle_delay, 500, "cycle_delay scalar")

        # 3. cycle_delay 数组写法
        ok = context.override_pipeline(
            {
                "RangeDelayNode": {
                    "task_mode": "loop_scan",
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                    "next": ["A"],
                    "cycle_delay": [300, 800],
                },
                "A": {"recognition": "DirectHit", "action": "DoNothing"},
            }
        )
        assert_true(ok, "override_pipeline cycle_delay range")
        obj = context.get_node_object("RangeDelayNode")
        # cycle_delay 在 binding 端可能 dump 为 [min,max] 或保留原 list
        # 这里宽松检查：至少 min 部分正确
        if isinstance(obj.cycle_delay, list):
            assert_eq(obj.cycle_delay[0], 300, "cycle_delay min")
            assert_eq(obj.cycle_delay[1], 800, "cycle_delay max")
        else:
            assert_eq(obj.cycle_delay, 300, "cycle_delay min as scalar")

        # 4. 非法 task_mode 应当 override 失败
        ok = context.override_pipeline(
            {
                "BadModeNode": {
                    "task_mode": "no_such_mode",
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                }
            }
        )
        assert_eq(ok, False, "override should reject invalid task_mode")

        # 5. cycle_delay max < min 应当 override 失败
        ok = context.override_pipeline(
            {
                "BadRangeNode": {
                    "task_mode": "loop_scan",
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                    "next": ["A"],
                    "cycle_delay": [800, 300],
                }
            }
        )
        assert_eq(ok, False, "override should reject max<min")

        print("  PASS: loop_scan mode + [Fallback] + cycle_delay")
```

并在 `_run_context_tests` 中调用：

```python
        # 8.5. 测试 task_mode / [Fallback] / cycle_delay
        self._test_loop_scan_mode(context)
```

- [ ] **Step 15.2：跑测试**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

期望：新增测试 PASS。

- [ ] **Step 15.3：Commit**

```bash
git add test/python/pipeline_test.py
git commit -m "test(pipeline): 增加 task_mode / [Fallback] / cycle_delay 解析测试"
```

---

### Task 16: 文档（中英双语）

**Files:**
- Modify: `docs/zh_cn/3.1-任务流水线协议.md`
- Modify: `docs/en_us/3.1-pipeline.md`

- [ ] **Step 16.1：在中文文档"节点属性"小节加 `[Fallback]` 说明**

```markdown
### [Fallback] 兜底节点

将节点标记为任务全局兜底节点。仅在 entry 节点的 `next` 列表中可声明，最多一个。

当 entry 节点的 `task_mode: "loop_scan"` 时，整条 next 链一次扫描全部识别失败后，
框架会跳到 `[Fallback]` 标记的节点并执行其 action（任意识别类型），随后回到循环顶部
等待 `cycle_delay` 后开始下一轮。
```

- [ ] **Step 16.2：增加"任务模式 task_mode"小节**

```markdown
### task_mode 任务模式

| 取值 | 含义 |
|---|---|
| `state_machine`（默认） | 传统状态机：从 entry 出发沿 next 走，链走完任务结束 |
| `loop_scan` | 循环扫描整条 next 链；命中执行后继续下一轮；未命中走 `[Fallback]`；永不主动结束（需外部 stop） |

仅 entry 节点的该字段起作用，子节点声明会被忽略。

#### cycle_delay

`loop_scan` 模式下，每轮循环结束的等待时长。

- 标量 `1000`：固定 1000ms
- 数组 `[800, 1500]`：每轮在 [800, 1500]ms 区间均匀随机
```

- [ ] **Step 16.3：英文文档同步**

```markdown
### [Fallback] Global Fallback Node

Marks a node as the task-level fallback. Only declarable inside the entry node's
`next` list, max one occurrence.

When entry's `task_mode` is `"loop_scan"` and a full scan over the next chain
fails to match any node, the framework jumps to the `[Fallback]` node and runs
its action, then returns to the top of the loop and waits for `cycle_delay`.

### task_mode

| Value | Behavior |
|---|---|
| `state_machine` (default) | Traditional FSM: walks `next` from entry until the chain ends. |
| `loop_scan` | Continuously scans the entire `next` chain; runs action on hit; runs `[Fallback]` on full miss; never terminates by itself. |

Only the entry node's `task_mode` is honored.

#### cycle_delay

Wait time between loop iterations (loop_scan mode only).

- Scalar `1000`: fixed 1000ms
- Array `[800, 1500]`: uniformly sampled from [800, 1500] ms each round
```

- [ ] **Step 16.4：Commit**

```bash
git add docs/zh_cn/3.1-任务流水线协议.md docs/en_us/3.1-pipeline.md
git commit -m "docs(pipeline): 补 task_mode / [Fallback] / cycle_delay 中英文档"
```

---

### Phase 1 里程碑

到这里 Phase 1 完成，应有约 13 个 commit。功能上你可以：
- 写一份 `task_mode: "loop_scan"` 的 pipeline JSON 跑起来
- `[Fallback]` 兜底节点替换原"兜底"硬编码
- 默认行为完全不变（state_machine 路径未被触碰）

**建议在此处打 tag：**

```bash
git tag phase1-loopscan-done
```

---

## Phase 2 · 文件命名空间 + 子 pipeline 嵌套

### Task 17: 数据结构 — `sub_pipeline` 字段

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineTypes.h`
- Modify: `source/MaaFramework/Resource/PipelineTypesV2.h`
- Modify: `source/binding/Python/maa/pipeline.py`

- [ ] **Step 17.1：PipelineTypes.h 加字段**

在 `PipelineData` 中加入：

```cpp
    std::optional<std::string> sub_pipeline;
```

- [ ] **Step 17.2：PipelineTypesV2.h JPipelineData 同步**

```cpp
std::optional<std::string> sub_pipeline;
// ...
MEO_JSONIZATION(
    // ...
    MEO_OPT sub_pipeline
);
```

- [ ] **Step 17.3：Python binding 同步**

```python
sub_pipeline: Optional[str] = None
# 在 from_dict 中：
sub_pipeline=data.get("sub_pipeline"),
```

- [ ] **Step 17.4：Commit**

```bash
git add source/MaaFramework/Resource/PipelineTypes.h \
        source/MaaFramework/Resource/PipelineTypesV2.h \
        source/binding/Python/maa/pipeline.py
git commit -m "feat(pipeline): 为 sub_pipeline 嵌套调用预留字段"
```

---

### Task 18: PipelineResMgr — `compute_fqn_prefix` 实现

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineResMgr.h`
- Modify: `source/MaaFramework/Resource/PipelineResMgr.cpp`

- [ ] **Step 18.1：PipelineResMgr.h 加 static 工具函数**

```cpp
static std::string compute_fqn_prefix(
    const std::filesystem::path& json_file,
    const std::filesystem::path& pipeline_root);
```

- [ ] **Step 18.2：实现 `compute_fqn_prefix`**

PipelineResMgr.cpp 中：

```cpp
std::string PipelineResMgr::compute_fqn_prefix(
    const std::filesystem::path& json_file,
    const std::filesystem::path& pipeline_root)
{
    auto rel = std::filesystem::relative(json_file, pipeline_root);
    rel.replace_extension();  // 去掉 .json 后缀
    std::string s = rel.generic_string();  // 用 '/' 作为分隔
    return s;  // 例：battle/fight
}
```

- [ ] **Step 18.3：Commit**

```bash
git add source/MaaFramework/Resource/PipelineResMgr.h \
        source/MaaFramework/Resource/PipelineResMgr.cpp
git commit -m "feat(resource): 加 compute_fqn_prefix 工具函数

把 JSON 文件相对路径转换为命名空间前缀（如 battle/fight.json → battle/fight）。"
```

---

### Task 19: PipelineResMgr — 加载期 FQN 注入 + 引用解析

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineResMgr.cpp`
- Modify: `source/MaaFramework/Resource/PipelineParser.cpp`（或新增 helper 在 ResMgr）

- [ ] **Step 19.1：在加载流程中传入文件前缀给 PipelineParser**

定位 `PipelineResMgr` 加载单个文件的入口（约第 70 行附近的 `recursive_directory_iterator` 循环），在调用 PipelineParser::parse_pipeline 之前计算 prefix 并传入：

```cpp
auto fqn_prefix = compute_fqn_prefix(json_file, pipeline_root);
// 把 prefix 传给 PipelineParser，让其在 parse 期把节点名和引用换成 FQN
PipelineParser parser;
parser.set_fqn_prefix(fqn_prefix);
auto map = parser.parse_pipeline_with_fqn(json_value);
// map 的 key 已经是 FQN
```

- [ ] **Step 19.2：在 PipelineParser 加 `fqn_prefix_` 成员 + setter**

```cpp
class PipelineParser {
    // ...
private:
    std::string fqn_prefix_;

public:
    void set_fqn_prefix(const std::string& prefix) { fqn_prefix_ = prefix; }
};
```

- [ ] **Step 19.3：parse 期把节点名和引用换成 FQN**

提供 helper：

```cpp
std::string PipelineParser::qualify(const std::string& raw) const
{
    if (fqn_prefix_.empty()) return raw;     // 兼容老路径
    if (raw.find("::") != std::string::npos) return raw;  // 已经是 FQN
    return fqn_prefix_ + "::" + raw;
}
```

在 parse_node 处理每个引用字段（`next` / `on_error` / `fallback_node`）时调用 `qualify(name)`。

`pipeline_data_map_` 的 key 全部存为 FQN。

- [ ] **Step 19.4：加载完所有文件后做一次"全局唯一回退"修正**

在 `PipelineResMgr` 加载完所有文件、合并到全局 map 之后，遍历所有 PipelineData 的引用，对仍然是裸名（不含 `::`）的引用：

```cpp
auto resolve_name = [&](const std::string& raw, const std::string& current_prefix) -> std::string {
    if (raw.find("::") != std::string::npos) {
        // 绝对引用：直接验证存在
        if (!pipeline_data_map_.contains(raw)) {
            LogError << "Reference not found: " << raw;
            throw std::runtime_error("Reference not found");
        }
        return raw;
    }

    // 当前作用域优先
    auto scoped = current_prefix + "::" + raw;
    if (pipeline_data_map_.contains(scoped)) return scoped;

    // 全局唯一回退
    std::vector<std::string> candidates;
    for (const auto& [key, _] : pipeline_data_map_) {
        if (key.size() > raw.size() && key.ends_with("::" + raw)) {
            candidates.push_back(key);
        }
    }
    if (candidates.size() == 1) return candidates[0];
    if (candidates.empty()) {
        LogError << "Reference not found: " << raw << " in scope " << current_prefix;
        throw std::runtime_error("Reference not found");
    }

    std::string list;
    for (const auto& c : candidates) list += "\n  - " + c;
    LogError << "Reference ambiguous: " << raw << " has multiple candidates:" << list;
    throw std::runtime_error("Reference ambiguous");
};
```

- [ ] **Step 19.5：构建 + 跑现有测试（确认所有现有 pipeline 仍能加载）**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

> ⚠️ 这一步是 Phase 2 最大的兼容性风险点。所有现有 pipeline 节点名经过 FQN 注入会变成 `<file>::<name>`。Python 测试依赖具体节点名（`"TestBasic"` 等），可能失败。
>
> **对策**：让外部 API（`get_node_data` / `get_node_object` / `override_pipeline`）支持裸名查找——内部做"全局唯一回退"，与 19.4 同样的逻辑。

- [ ] **Step 19.6：让 Resource/Context 的 get_node_data / override_pipeline 支持裸名**

在 Resource API 层 + Context API 层（搜 `get_node_data` 实现），如果传入名字不含 `::`，先做全局唯一回退查找；找到唯一匹配则用 FQN 实际查询，否则按现有逻辑（找不到返回 nullopt）。

具体调整点在：
- `source/MaaFramework/API/MaaResource.cpp`
- `source/MaaFramework/Tasker/Context.cpp`（或类似）

- [ ] **Step 19.7：构建 + 跑测试**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

期望：所有现有测试 PASS。

- [ ] **Step 19.8：Commit**

```bash
git add source/MaaFramework/
git commit -m "feat(resource): 文件级命名空间 FQN 注入

加载期把每个节点名 + 引用改成 file_prefix::name 形式；解析支持当前作用域优先、
全局唯一回退、绝对引用三种方式。外部 API 兼容裸名查找。"
```

---

### Task 20: PipelineTask — `run_loop_scan` 拆成 `execute_once`

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.h`
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

- [ ] **Step 20.1：PipelineTask.h 加 `execute_once` 声明 + 常量**

```cpp
private:
    static constexpr int kMaxNestingDepth = 8;
    void execute_once(const std::string& pipeline_entry, int depth);
```

- [ ] **Step 20.2：PipelineTask.cpp 实现 `execute_once`**

```cpp
void PipelineTask::execute_once(const std::string& pipeline_entry, int depth)
{
    if (depth > kMaxNestingDepth) {
        LogError << "max nesting depth exceeded"
                 << VAR(pipeline_entry) << VAR(depth);
        return;
    }

    auto entry_data_opt = context_->get_pipeline_data(pipeline_entry);
    if (!entry_data_opt) {
        LogError << "execute_once: entry not found" << VAR(pipeline_entry);
        return;
    }
    const auto entry_data = *entry_data_opt;

    cur_node_ = pipeline_entry;
    auto chain = build_chain(pipeline_entry);
    if (chain.empty()) {
        LogWarn << "execute_once: empty chain" << VAR(pipeline_entry);
        return;
    }

    auto hit = run_next(chain, entry_data, ScanOptions { .single_pass = true });

    if (context_->need_to_stop()) return;

    if (hit.reco_id != MaaInvalidId && hit.completed) {
        auto hit_data_opt = context_->get_pipeline_data(hit.name);
        if (hit_data_opt && hit_data_opt->sub_pipeline) {
            LogInfo << "entering sub_pipeline"
                    << VAR(hit.name) << VAR(*hit_data_opt->sub_pipeline) << VAR(depth);
            execute_once(*hit_data_opt->sub_pipeline, depth + 1);
        }
    }
    else if (entry_data.fallback_node) {
        run_fallback(*entry_data.fallback_node);
    }
}
```

- [ ] **Step 20.3：把 `run_loop_scan` 重写为顶层 while + 调用 `execute_once`**

```cpp
bool PipelineTask::run_loop_scan(const std::string& entry)
{
    auto entry_data_opt = context_->get_pipeline_data(entry);
    if (!entry_data_opt) {
        LogError << "run_loop_scan: entry not found" << VAR(entry);
        return false;
    }
    const auto entry_data = *entry_data_opt;

    while (!context_->need_to_stop()) {
        execute_once(entry, /*depth=*/ 0);
        std::this_thread::sleep_for(
            sample_delay(entry_data.cycle_delay, entry_data.cycle_delay_max));
    }

    return true;
}
```

- [ ] **Step 20.4：构建 + 跑测试**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

- [ ] **Step 20.5：Commit**

```bash
git add source/MaaFramework/Task/PipelineTask.h source/MaaFramework/Task/PipelineTask.cpp
git commit -m "feat(pipeline): execute_once 递归实现子 pipeline 嵌套

run_loop_scan 重构为顶层 while + execute_once；命中节点有 sub_pipeline
时递归进入子层，单次穿透后返回；递归深度上限 8 层。"
```

---

### Task 21: PipelineDumper — `sub_pipeline` 输出

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineDumper.cpp`

- [ ] **Step 21.1：dump PipelineData 时输出 sub_pipeline**

```cpp
if (data.sub_pipeline) {
    json["sub_pipeline"] = *data.sub_pipeline;
}
```

- [ ] **Step 21.2：Commit**

```bash
git add source/MaaFramework/Resource/PipelineDumper.cpp
git commit -m "feat(pipeline): Dumper 输出 sub_pipeline 字段"
```

---

### Task 22: pipeline.schema.json — sub_pipeline + 命名空间说明

**Files:**
- Modify: `tools/pipeline.schema.json`

- [ ] **Step 22.1：加 sub_pipeline 字段定义**

```json
"sub_pipeline": {
    "type": "string",
    "pattern": "^[^\\s]+::[^\\s]+$|^[^:\\s]+$",
    "description": "命中本节点后递归进入的子 pipeline 入口节点名（FQN 形式如 'battle/fight::entry'，或裸名走全局唯一回退）。"
}
```

- [ ] **Step 22.2：Commit**

```bash
git add tools/pipeline.schema.json
git commit -m "feat(schema): 补 sub_pipeline 字段"
```

---

### Task 23: NodeJS 绑定同步 sub_pipeline

**Files:**
- Modify: `source/binding/NodeJS/src/apis/pipeline.d.ts`

- [ ] **Step 23.1：在 General 类型加 sub_pipeline 字段**

```typescript
type General<Mode> = {
    // ... 现有字段保留 ...
    sub_pipeline?: string
}
```

- [ ] **Step 23.2：构建 NodeJS binding**

```bash
cd source/binding/NodeJS
npm run build
```

期望：TypeScript 类型检查通过。

- [ ] **Step 23.3：Commit**

```bash
git add source/binding/NodeJS/src/apis/pipeline.d.ts
git commit -m "feat(binding/nodejs): TypeScript 类型同步 sub_pipeline"
```

---

### Task 24: Python 集成测试 — 文件命名空间 + sub_pipeline

**Files:**
- Modify: `test/python/pipeline_test.py`

- [ ] **Step 24.1：增加测试函数**

在 PipelineTestRecognition 中追加：

```python
    def _test_fqn_namespace(self, context: Context):
        """测试文件 FQN 命名空间与裸名/绝对引用解析"""
        print("\n--- _test_fqn_namespace ---")

        # 注入两个"文件"，同名节点 兜底 共存
        # override_pipeline 在 Resource 层视为"虚拟文件"，需要 binding 支持 ::
        ok = context.override_pipeline(
            {
                "main_a::entry": {
                    "task_mode": "loop_scan",
                    "next": ["check_x", "[Fallback]兜底"],
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "main_a::check_x": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "main_a::兜底": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "battle/fight::entry": {
                    "task_mode": "loop_scan",
                    "next": ["attack", "[Fallback]兜底"],
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "battle/fight::attack": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "battle/fight::兜底": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
            }
        )
        assert_true(ok, "override_pipeline FQN namespace")

        # 1. 用 FQN 取节点
        obj = context.get_node_object("main_a::entry")
        assert_not_none(obj, "main_a::entry exists")
        assert_eq(obj.fallback_node, "main_a::兜底", "fallback resolved to scoped name")

        obj2 = context.get_node_object("battle/fight::entry")
        assert_eq(obj2.fallback_node, "battle/fight::兜底", "scoped fallback")

        # 2. 裸名"兜底"应当报告歧义（不返回任一）
        ambiguous = context.get_node_object("兜底")
        assert_eq(ambiguous, None, "ambiguous bare name should not resolve")

        print("  PASS: FQN namespace")

    def _test_sub_pipeline_field(self, context: Context):
        """测试 sub_pipeline 字段解析"""
        print("\n--- _test_sub_pipeline_field ---")

        ok = context.override_pipeline(
            {
                "main::trigger": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                    "sub_pipeline": "child::entry",
                },
                "child::entry": {
                    "task_mode": "loop_scan",
                    "next": ["leaf", "[Fallback]兜底"],
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "child::leaf": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
                "child::兜底": {
                    "recognition": "DirectHit",
                    "action": "DoNothing",
                },
            }
        )
        assert_true(ok, "override sub_pipeline")

        obj = context.get_node_object("main::trigger")
        assert_eq(obj.sub_pipeline, "child::entry", "sub_pipeline FQN preserved")

        print("  PASS: sub_pipeline field")
```

在 `_run_context_tests` 中调用：

```python
        self._test_fqn_namespace(context)
        self._test_sub_pipeline_field(context)
```

- [ ] **Step 24.2：跑测试**

```bash
cmake --build build --config Debug --target MaaFramework
cmake --install build --config Debug --prefix install
python test/python/pipeline_test.py source/binding/Python install
```

期望：新增测试 PASS。

- [ ] **Step 24.3：Commit**

```bash
git add test/python/pipeline_test.py
git commit -m "test(pipeline): 增加 FQN 命名空间与 sub_pipeline 字段测试"
```

---

### Task 25: 文档 — FQN + sub_pipeline 中英文档

**Files:**
- Modify: `docs/zh_cn/3.1-任务流水线协议.md`
- Modify: `docs/en_us/3.1-pipeline.md`

- [ ] **Step 25.1：加"文件级命名空间（FQN）"小节**

中文：

```markdown
## 文件级命名空间（FQN）

从 v5.x 开始，pipeline JSON 文件加载时自动以文件路径作为命名空间前缀，
完整节点名形如 `battle/fight::attack`。

### 命名规则

| 文件路径 | 节点原名 | 完整 FQN |
|---|---|---|
| `pipeline/main.json` | `entry` | `main::entry` |
| `pipeline/battle/fight.json` | `attack` | `battle/fight::attack` |
| `pipeline/battle/fight.json` | `兜底` | `battle/fight::兜底` |

### 引用解析规则

`next` / `on_error` / `[Fallback]` / `sub_pipeline` 中的引用，按以下顺序解析：

1. **绝对引用**（含 `::`）：直接当 FQN 查找，不存在则加载失败
2. **当前文件作用域**：把当前文件前缀拼到名字前面（`battle/fight::xxx`）查找
3. **全局唯一回退**：在全局查找所有 `*::name` 候选，恰好一条则匹配；多条或零条加载失败

> ⚠️ `::` 是保留分隔符，节点裸名不应包含 `::`，否则加载时会打 warning。

## sub_pipeline 嵌套

节点新增 `sub_pipeline` 字段，类型 string，指向另一文件的入口节点（FQN 或裸名）。

当 `loop_scan` 模式下节点被命中并 action 执行完成后，框架递归进入指定子 pipeline
执行**单次穿透**：

- 子 pipeline 自身按 loop_scan 单次扫描整条 chain
- 命中节点执行 action → 函数返回上一层
- 全部未命中 → 触发子层兜底 → 函数返回上一层
- 子 pipeline 本身**不循环**，单次穿透后回到父层

最大递归深度 8 层，超过即放弃当前递归层并打日志。
```

- [ ] **Step 25.2：英文同步翻译**

- [ ] **Step 25.3：Commit**

```bash
git add docs/zh_cn/3.1-任务流水线协议.md docs/en_us/3.1-pipeline.md
git commit -m "docs(pipeline): 补 FQN 命名空间 + sub_pipeline 嵌套说明"
```

---

### Phase 2 里程碑

到此 Phase 2 完成，约再加 9 个 commit。功能完整后：

- 写 `main.json` + `battle/fight.json` 两个文件，每个都有自己的 `兜底`
- main entry 命中 `check_battle` 节点 + `sub_pipeline: "battle/fight::entry"` 后递归进子层
- 子层命中或走兜底后自动返回 main

**建议在此处打 tag：**

```bash
git tag phase2-subpipeline-done
```

---

## 自检清单

实施过程中每完成一个 Phase 自检一次：

### Phase 1 自检
- [ ] 默认 task_mode 不指定时，所有现有 pipeline 行为不变
- [ ] `task_mode: "loop_scan"` 能正常进入循环扫描分支
- [ ] `[Fallback]` 在 next 中被正确提取到 fallback_node
- [ ] `cycle_delay` 标量 / 数组 写法都解析正确
- [ ] 非法 task_mode / cycle_delay 配置加载失败并报错
- [ ] `cur_node_` 反映真实命中节点（非 entry）
- [ ] Python / NodeJS / Schema / Docs 同步完成
- [ ] 现有 Python 测试全部 PASS
- [ ] 新增 Python 测试 PASS

### Phase 2 自检
- [ ] 多文件同名节点（如多个 `兜底`）加载不冲突
- [ ] 裸名引用在当前文件作用域命中优先
- [ ] 全局唯一回退命中，多候选时加载失败
- [ ] 绝对 FQN 引用（含 `::`）直接查找
- [ ] `sub_pipeline` 命中后递归进入子文件
- [ ] 子层执行后正常返回父层
- [ ] 嵌套 ≥9 层时打 LogError 并退出当前递归
- [ ] `get_node_object` 等外部 API 支持裸名查找
- [ ] 所有测试 PASS

---

## 风险点提醒

| 风险 | 任务 | 缓解 |
|---|---|---|
| Phase 2 FQN 注入破坏所有现有测试节点名查找 | Task 19 | Step 19.6 给外部 API 加裸名→FQN 自动解析 |
| `cycle_delay` 配错（min>max）静默无效 | Task 5 | parse_duration_range 强校验，加载失败 |
| 递归深度超限静默忽略 | Task 20 | execute_once 顶部 LogError，可考虑加 notify 上报 |
| `::` 字符与已有节点名冲突 | Task 19 | release notes 明确保留字符 + 加载期 warning |
| NodeJS binding 同步遗漏 | Task 13/23 | 每次 Phase 结束前手动 `cd source/binding/NodeJS && npm run build` |

---

## 执行总流程图

```
Phase 1
─────────
Task 1: stash + revert        → 干净基线
Task 2~5: types + parser      → 协议层接入
Task 6~8: V2/Dumper/Default   → 序列化闭环
Task 9~11: PipelineTask 重构  → 运行时实现 loop_scan
Task 12~14: 绑定 + Schema     → 周边同步
Task 15: 测试                 → 验证
Task 16: 文档                 → 收尾
  ╰─► [tag: phase1-loopscan-done]

Phase 2
─────────
Task 17: sub_pipeline 字段
Task 18~19: FQN + 引用解析
Task 20: execute_once 递归
Task 21~23: Dumper/Schema/NodeJS
Task 24: 测试
Task 25: 文档
  ╰─► [tag: phase2-subpipeline-done]
```
