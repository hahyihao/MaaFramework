# Pipeline `SubPipeline` Recognition Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入新的 recognition 类型 `SubPipeline`，让父节点 reco 阶段委托给一个子流水线（"识别代理"语义），子层任一命中 → 父算命中 + box 上浮，全 miss → 父算 miss。

**Architecture:** 在 `Recognition::Type` 枚举里新增 `SubPipeline`，配套 `recognition_pipeline` 字段。运行时不走 `Vision/` 模块（避免反向依赖），而是在 `PipelineTask::recognize_list` 里特判 `SubPipeline` 类型，直接调用 `execute_once`。`execute_once` 签名从 `void` 改为返回 `ExecOnceResult { hit, hit_box, hit_detail }`，现有两处调用点（loop_scan 主循环 + sub_pipeline 字段递归）只忽略返回值、行为不变。

**Tech Stack:** C++20 / CMake (Ninja Multi-Config) / MSVC / Python 3 ctypes binding / TypeScript binding / meojson / JSON schema

**Spec：** `docs/superpowers/specs/2026-05-12-pipeline-subpipeline-recognition-design.md`

---

## 文件清单

| 文件 | 改动类型 | 负责的 task |
|------|---------|------------|
| `source/MaaFramework/Resource/PipelineTypes.h` | 加 enum 值 + Param 变体 + PipelineData 字段 | Task 1 |
| `source/MaaFramework/Resource/PipelineParser.cpp` | 解析 SubPipeline 类型 + recognition_pipeline 字段 + FQN qualify | Task 2 |
| `source/MaaFramework/Resource/PipelineChecker.cpp` | 校验 recognition_pipeline 引用合法 | Task 3 |
| `source/MaaFramework/Task/PipelineTask.h/cpp` | execute_once 改签名 + recognize_list 加 SubPipeline 分支 | Tasks 4-5 |
| `source/MaaFramework/Resource/PipelineDumper.cpp` | 序列化 SubPipeline 类型 + recognition_pipeline | Task 6 |
| `source/MaaFramework/Resource/PipelineTypesV2.h` | JPipelineData 加 recognition_pipeline | Task 7 |
| `source/binding/Python/maa/pipeline.py` | Python 类型同步 | Task 8 |
| `source/binding/NodeJS/src/apis/pipeline.d.ts` | TypeScript 类型同步 | Task 9 |
| `tools/pipeline.schema.json` | enum 加 SubPipeline + recognition_pipeline 字段 | Task 10 |
| `docs/zh_cn/3.1-任务流水线协议.md` | 中文文档 | Task 11 |
| `docs/en_us/3.1-PipelineProtocol.md` | 英文文档 | Task 12 |
| `test/python/pipeline_test.py` | 单元测试 | Task 13 |
| `test/python/smoke_loopscan.py` | 扩展为 v3 集成测试 | Task 14 |

---

## 构建命令（每个 task 验证用）

PowerShell 在 D:\MaaFramework 下：

```powershell
# 一次性配置（如未配置）
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake -S . -B build -G `"Ninja Multi-Config`" -DWITH_DBG_CONTROLLER=ON"

# 增量构建
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo"

# 安装到 install/ 目录（smoke test 用）
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"
```

Python 测试：

```powershell
python test/python/pipeline_test.py source/binding/Python install
python test/python/smoke_loopscan.py source/binding/Python install
```

---

## Task 1: 数据结构（PipelineTypes.h）

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineTypes.h`

- [ ] **Step 1: 在 `Recognition::Type` 枚举尾部加 `SubPipeline`**

`source/MaaFramework/Resource/PipelineTypes.h:23-36` 改为：

```cpp
enum class Type
{
    Invalid = 0,
    DirectHit,
    TemplateMatch,
    FeatureMatch,
    OCR,
    NeuralNetworkClassify,
    NeuralNetworkDetect,
    ColorMatch,
    And,
    Or,
    Custom,
    SubPipeline,
};
```

- [ ] **Step 2: 在 Recognition 命名空间里加 `SubPipelineParam` 结构**

在现有 `AndParam` / `OrParam` 旁边（约 65-76 行附近）新增：

```cpp
// SubPipeline recognition parameter: delegate the recognition phase to a sub-pipeline.
// The sub-pipeline runs single-pass via execute_once; hit ⇒ this node hits with hit_box bubbled up.
struct SubPipelineParam
{
    std::string recognition_pipeline;
};
```

- [ ] **Step 3: 把 `SubPipelineParam` 加进 `Recognition::Param` variant**

`source/MaaFramework/Resource/PipelineTypes.h:41-52` 改为：

```cpp
using Param = std::variant<
    std::monostate,
    MAA_VISION_NS::DirectHitParam,
    MAA_VISION_NS::TemplateMatcherParam,
    MAA_VISION_NS::FeatureMatcherParam,
    MAA_VISION_NS::OCRerParam,
    MAA_VISION_NS::NeuralNetworkClassifierParam,
    MAA_VISION_NS::NeuralNetworkDetectorParam,
    MAA_VISION_NS::ColorMatcherParam,
    std::shared_ptr<AndParam>,
    std::shared_ptr<OrParam>,
    MAA_VISION_NS::CustomRecognitionParam,
    SubPipelineParam>;
```

- [ ] **Step 4: 在 `kTypeMap` / `kTypeNameMap` 加映射**

`PipelineTypes.h:78-103`（kTypeMap）追加：

```cpp
    { "SubPipeline", Type::SubPipeline },
    { "subpipeline", Type::SubPipeline },
    { "sub_pipeline", Type::SubPipeline },
```

`PipelineTypes.h:105-116`（kTypeNameMap）追加：

```cpp
    { Type::SubPipeline, "SubPipeline" },
```

注意：`"sub_pipeline"` 这个别名很容易和现有 `sub_pipeline`（字段名，子例程）混淆。仍然支持，但**文档主推 `"SubPipeline"`**。

- [ ] **Step 5: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。因为新增枚举值还没在任何 switch 里处理，但编译器 `-Werror` 对 switch 完备性可能报警 —— 如果报警需要在下一 task 处理 switch。先看是否过。

- [ ] **Step 6: Commit**

```powershell
git add source/MaaFramework/Resource/PipelineTypes.h
git commit -m "feat(pipeline): 加 SubPipeline recognition 类型数据结构

新增 Recognition::Type::SubPipeline 枚举值和 SubPipelineParam 结构体。
配套 recognition_pipeline 字段稍后在 PipelineData 上加。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: 解析层（PipelineParser）

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineParser.cpp`

- [ ] **Step 1: 在 `PipelineParser::parse_recognition` 的 switch 里加 SubPipeline 分支**

定位：`source/MaaFramework/Resource/PipelineParser.cpp:486` 起的 `parse_recognition` 函数体里有 `switch (out_type)` 分发各 reco 类型的解析。

在 switch 尾部、`Custom` 分支之后加：

```cpp
case Recognition::Type::SubPipeline: {
    Recognition::SubPipelineParam param;
    if (!get_and_check_value(input, "recognition_pipeline", param.recognition_pipeline, std::string {})) {
        LogError << "failed to get_and_check_value recognition_pipeline" << VAR(input);
        return false;
    }
    if (param.recognition_pipeline.empty()) {
        LogError << "recognition_pipeline is empty" << VAR(input);
        return false;
    }
    out_param = std::move(param);
    break;
}
```

（具体 switch case 的语法风格按文件内其他 case 一致 —— 在那个文件里既有 `case X: ... break;` 也有 `case X: { ... } break;` 形式，照搬最近的相邻分支风格即可。）

- [ ] **Step 2: 在 `parse_node` 末尾对 `recognition_pipeline` 做 FQN qualify**

定位：`source/MaaFramework/Resource/PipelineParser.cpp:259` 起的 `parse_node`。文件末尾已有 `if (data.fallback_node) data.fallback_node = qualify_name(*data.fallback_node, fqn_prefix);` 和 `if (data.sub_pipeline) data.sub_pipeline = qualify_name(*data.sub_pipeline, fqn_prefix);` 这种 qualify 调用。

在同一块加：

```cpp
// SubPipeline reco param: qualify recognition_pipeline 引用为 FQN
if (data.reco_type == Recognition::Type::SubPipeline) {
    auto* sp = std::get_if<Recognition::SubPipelineParam>(&data.reco_param);
    if (sp && !sp->recognition_pipeline.empty()) {
        sp->recognition_pipeline = qualify_name(sp->recognition_pipeline, fqn_prefix);
    }
}
```

- [ ] **Step 3: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。

- [ ] **Step 4: 手测解析（最小 JSON）**

创建临时验证：

```powershell
mkdir -Force test\_tmp_subreco_parse\resource\pipeline | Out-Null
@'
{
  "TryHome": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "HomeEntry",
    "next": []
  },
  "HomeEntry": {
    "recognition": "DirectHit",
    "next": []
  }
}
'@ | Out-File -Encoding utf8 test\_tmp_subreco_parse\resource\pipeline\test.json
```

不必跑实际加载（下一 task 校验器会拦），保留这个文件给 Task 13 单测复用。

- [ ] **Step 5: Commit**

```powershell
git add source/MaaFramework/Resource/PipelineParser.cpp
git commit -m "feat(pipeline): 解析 SubPipeline reco 类型 + recognition_pipeline 字段

parse_recognition switch 加 SubPipeline 分支提取 recognition_pipeline。
parse_node 末尾对 recognition_pipeline 做 FQN qualify，与 sub_pipeline / fallback_node 字段一致。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: 静态校验（PipelineChecker）

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineChecker.cpp`
- Modify: `source/MaaFramework/Resource/PipelineChecker.h`

- [ ] **Step 1: 在 `PipelineChecker.h` 里声明新静态函数**

`source/MaaFramework/Resource/PipelineChecker.h` 的 public 静态函数列表里加：

```cpp
static bool check_all_recognition_pipeline(const PipelineDataMap& data_map);
```

- [ ] **Step 2: 在 `check_all_validity` 里调用它**

`source/MaaFramework/Resource/PipelineChecker.cpp:13-19` 改为：

```cpp
bool PipelineChecker::check_all_validity(const PipelineDataMap& data_map)
{
    bool ret = check_all_next_list(data_map);
    ret &= check_all_regex(data_map);
    ret &= check_all_recognition_pipeline(data_map);

    return ret;
}
```

- [ ] **Step 3: 实现 `check_all_recognition_pipeline`**

在 `PipelineChecker.cpp` 末尾、`MAA_RES_NS_END` 之前加：

```cpp
bool PipelineChecker::check_all_recognition_pipeline(const PipelineDataMap& data_map)
{
    for (const auto& [name, pipeline_data] : data_map) {
        if (pipeline_data.reco_type != Recognition::Type::SubPipeline) {
            continue;
        }
        const auto* sp = std::get_if<Recognition::SubPipelineParam>(&pipeline_data.reco_param);
        if (!sp) {
            LogError << "SubPipeline reco_param missing" << VAR(name);
            return false;
        }
        if (sp->recognition_pipeline.empty()) {
            LogError << "SubPipeline recognition_pipeline is empty" << VAR(name);
            return false;
        }
        if (PipelineResMgr::lookup_with_bare_fallback(data_map, sp->recognition_pipeline) == data_map.end()) {
            LogError << "Invalid recognition_pipeline reference"
                     << VAR(name) << VAR(sp->recognition_pipeline);
            return false;
        }
    }
    return true;
}
```

- [ ] **Step 4: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。

- [ ] **Step 5: Commit**

```powershell
git add source/MaaFramework/Resource/PipelineChecker.h source/MaaFramework/Resource/PipelineChecker.cpp
git commit -m "feat(pipeline): 校验 recognition_pipeline 引用合法

check_all_recognition_pipeline 在启动加载阶段扫描所有 SubPipeline reco 节点，
验证 recognition_pipeline 字段非空且引用的节点存在（用现有 lookup_with_bare_fallback 兼容裸名/FQN）。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: 重构 execute_once + run_next 签名（行为不变）

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.h`
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

**关键事实：** `NodeDetail`（在 `source/include/Common/TaskResultTypes.h:40`）**不含 box**，只有 `reco_id`。execute_once 想拿 box 上浮，必须让 `run_next` 的返回值带出 reco 信息。

策略：
- 引入内部结构 `RunNextResult { NodeDetail node_detail; std::optional<cv::Rect> reco_box; json::value reco_detail; }`
- 改 `run_next` 返回 `RunNextResult`，`run_state_machine` 调用点只读 `.node_detail`
- 改 `execute_once` 返回 `ExecOnceResult { hit, hit_box, hit_detail }`，从 `RunNextResult` 拿 reco_box / reco_detail
- **不修改 `NodeDetail` 公共结构**（在 `source/include/Common/TaskResultTypes.h`，跨 SDK 边界）

- [ ] **Step 1: 在 PipelineTask.h 里加 ExecOnceResult / RunNextResult 结构 + 改签名**

`source/MaaFramework/Task/PipelineTask.h:50-72` 区段改为：

```cpp
private:
    static constexpr int kMaxNestingDepth = 8;

    struct ExecOnceResult
    {
        bool hit = false;
        std::optional<cv::Rect> hit_box;
        json::value hit_detail;
    };

    struct RunNextResult
    {
        NodeDetail node_detail;
        std::optional<cv::Rect> reco_box;
        json::value reco_detail;
    };

    bool run_state_machine(const std::string& entry);
    bool run_loop_scan(const std::string& entry);

    // 单次穿透：扫一帧本层链 → 命中 action + 可选递归进入 sub_pipeline → 返回上一层
    // 返回 hit/miss + 命中节点的 box（供 SubPipeline reco 上浮）
    ExecOnceResult execute_once(const std::string& pipeline_entry, int depth);

    std::vector<MAA_RES_NS::NodeAttr> build_chain(const std::string& entry);
    void run_fallback(const std::string& fallback_node_name);
    static std::chrono::milliseconds sample_delay(
        std::chrono::milliseconds min_ms,
        std::chrono::milliseconds max_ms);

    struct ScanOptions
    {
        bool single_pass = false;
    };

    RunNextResult run_next(
        const std::vector<MAA_RES_NS::NodeAttr>& next,
        const PipelineData& pretask,
        ScanOptions opts = {});
```

确认头部 include：`<optional>` 已在 line 5；`cv::Rect` 通过 TaskBase.h → VisionTypes.h 链可拿到，编译失败再补 `#include "MaaUtils/NoWarningCVMat.hpp"`；`json::value` 通过 TaskBase.h → meojson 拿到。

- [ ] **Step 2: PipelineTask.cpp 里改 `run_next` 返回 RunNextResult**

定位 `source/MaaFramework/Task/PipelineTask.cpp:250` 起的 `run_next` 函数。改动：

A. 函数签名：`NodeDetail PipelineTask::run_next(...)` → `PipelineTask::RunNextResult PipelineTask::run_next(...)`

B. 函数内部所有 `NodeDetail result {...}` 构造点（两处，约 line 360 和 line 381）改为先构造 NodeDetail、再包进 RunNextResult：

```cpp
        NodeDetail node_detail {
            .node_id = node_id,
            .name = hit_name,
            .reco_id = reco.reco_id,
            .action_id = act.action_id,
            .completed = act.success,
            .jump_back = jump_back,
        };
        set_node_detail(node_detail.node_id, node_detail);

        node_cb_detail["node_details"] = node_detail;
        node_cb_detail["reco_details"] = reco;
        node_cb_detail["action_details"] = act;
        notify(act.success ? MaaMsg_Node_PipelineNode_Succeeded : MaaMsg_Node_PipelineNode_Failed, node_cb_detail);

        return RunNextResult {
            .node_detail = node_detail,
            .reco_box = reco.box,
            .reco_detail = reco.detail,
        };
```

和：

```cpp
    NodeDetail node_detail {
        .node_id = node_id,
        .completed = false,
    };
    set_node_detail(node_detail.node_id, node_detail);
    notify(MaaMsg_Node_PipelineNode_Failed, node_cb_detail);

    return RunNextResult { .node_detail = node_detail, .reco_box = std::nullopt, .reco_detail = {} };
```

C. `run_state_machine` 里的 `run_next` 调用点（约 line 62）：

旧：
```cpp
auto node_detail = run_next(next, node);
```

新：
```cpp
auto run_result = run_next(next, node);
auto& node_detail = run_result.node_detail;
```

后续代码引用 `node_detail.*` 完全不变。

- [ ] **Step 3: 改 execute_once 实现，返回 ExecOnceResult**

定位 `source/MaaFramework/Task/PipelineTask.cpp:149-190`，改为：

```cpp
PipelineTask::ExecOnceResult PipelineTask::execute_once(const std::string& pipeline_entry, int depth)
{
    ExecOnceResult result;

    if (depth > kMaxNestingDepth) {
        LogError << "max nesting depth exceeded"
                 << VAR(pipeline_entry) << VAR(depth) << VAR(kMaxNestingDepth);
        return result;
    }

    auto entry_data_opt = context_->get_pipeline_data(pipeline_entry);
    if (!entry_data_opt) {
        LogError << "execute_once: entry not found" << VAR(pipeline_entry);
        return result;
    }
    const auto entry_data = *entry_data_opt;

    cur_node_ = pipeline_entry;
    auto chain = build_chain(pipeline_entry);
    if (chain.empty()) {
        LogWarn << "execute_once: empty chain" << VAR(pipeline_entry);
        return result;
    }

    auto run_result = run_next(chain, entry_data, ScanOptions { .single_pass = true });
    const auto& hit = run_result.node_detail;

    if (context_->need_to_stop()) {
        return result;
    }

    if (hit.reco_id != MaaInvalidId && hit.completed) {
        result.hit = true;
        result.hit_box = run_result.reco_box;
        result.hit_detail = run_result.reco_detail;

        // 命中成功后，若命中节点声明了 sub_pipeline，递归进入子层
        auto hit_data_opt = context_->get_pipeline_data(hit.name);
        if (hit_data_opt && hit_data_opt->sub_pipeline) {
            LogInfo << "entering sub_pipeline"
                    << VAR(hit.name) << VAR(*hit_data_opt->sub_pipeline) << VAR(depth);
            (void)execute_once(*hit_data_opt->sub_pipeline, depth + 1);
        }
    }
    else if (entry_data.fallback_node) {
        run_fallback(*entry_data.fallback_node);
    }
    return result;
}
```

- [ ] **Step 4: 改 run_loop_scan 调用点忽略返回值**

`source/MaaFramework/Task/PipelineTask.cpp:135-144` 把 `execute_once(entry, /*depth=*/ 0);` 改为 `(void)execute_once(entry, /*depth=*/ 0);`（C++ 允许丢弃 non-void 返回值，加 `(void)` 仅为意图清晰）。

- [ ] **Step 5: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。如果有 `run_next` 其他调用点（例如 RecognitionTask 或 ActionTask 之类）也调用了 PipelineTask::run_next，会编译失败 —— 检查 `grep -rn "run_next("`，逐个修。从 Grep 结果看 run_next 是 PipelineTask 私有方法，只在 PipelineTask.cpp 内部调用，不应有外部调用点。

- [ ] **Step 6: 跑 phase1/phase2 已有 smoke test 确认无回归**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"
python test/python/smoke_loopscan.py source/binding/Python install
```

Expected: `EXIT=0` + `✓ PASS: loop_scan + sub_pipeline + [Fallback] + FQN namespace all verified`。

- [ ] **Step 7: Commit**

```powershell
git add source/MaaFramework/Task/PipelineTask.h source/MaaFramework/Task/PipelineTask.cpp
git commit -m "refactor(pipeline): execute_once / run_next 返回扩展结构带 reco_box

引入内部结构 ExecOnceResult / RunNextResult，让 reco_box / reco_detail
从 run_next 一路上浮到 execute_once 调用者，供 SubPipeline reco 上浮父层 box。

NodeDetail（外部 SDK 结构）保持不变。run_state_machine / run_loop_scan 等
现有调用点只读 .node_detail，行为完全不变。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: 运行时分发（recognize_list 加 SubPipeline 分支）

**Files:**
- Modify: `source/MaaFramework/Task/PipelineTask.cpp`

**关键事实：** `Recognizer::generate_reco_id()` 是 `Recognizer` 类的 public static 方法（见 `source/MaaFramework/Task/Component/Recognizer.h:30,57` 和 `ActionHelper.cpp:129` 现有调用模式）。可在 PipelineTask 里直接调用分配 reco_id。

`Recognizer` 已在 PipelineTask.cpp 顶部 `#include "Component/Recognizer.h"`（line 6）。

**Task 4 已完成 hit_box / hit_detail 上浮逻辑** —— 通过 `RunNextResult` 把 `reco.box` / `reco.detail` 从 `run_next` 一路传给 `execute_once`。本 task 只需在 `recognize_list` 加分发分支。

- [ ] **Step 1: 在 recognize_list 的现有 for 循环内、`run_recognition` 调用之前加 SubPipeline 分支**

定位：`source/MaaFramework/Task/PipelineTask.cpp:393` 起的 `recognize_list`。在循环遍历 `list` 之前（即 `for (const auto& node : list)` 这一行之前）增加：

定位 `source/MaaFramework/Task/PipelineTask.cpp:423-471` 的 for 循环。在 `auto anchor_name = node.anchor ? ...` 这一行之前（约 line 452）插入：

```cpp
        // SubPipeline reco：不走 Vision，递归调 execute_once 委托给子流水线
        if (pipeline_data.reco_type == MAA_RES_NS::Recognition::Type::SubPipeline) {
            const auto* sp = std::get_if<MAA_RES_NS::Recognition::SubPipelineParam>(&pipeline_data.reco_param);
            if (!sp || sp->recognition_pipeline.empty()) {
                LogError << "SubPipeline reco_param invalid" << VAR(pipeline_data.name);
                continue;
            }
            LogInfo << "SubPipeline reco delegate"
                    << VAR(pipeline_data.name) << VAR(sp->recognition_pipeline);

            auto sub_result = execute_once(sp->recognition_pipeline, /*depth=*/ 1);
            if (context_->need_to_stop()) {
                LogWarn << "need_to_stop after SubPipeline";
                break;
            }
            if (!sub_result.hit) {
                continue;  // 子层全 miss → 本候选视为未命中，试下一个
            }

            // 命中：构造 RecoResult，box / detail 自子层上浮
            RecoResult sp_result;
            sp_result.reco_id = Recognizer::generate_reco_id();
            sp_result.name = pipeline_data.name;
            sp_result.algorithm = "SubPipeline";
            sp_result.box = sub_result.hit_box;
            sp_result.detail = sub_result.hit_detail;

            context_->increment_hit_count(pipeline_data.name);
            notify(MaaMsg_Node_NextList_Succeeded, reco_list_cb_detail);
            return sp_result;
        }
```

**注意：** 这段插入位置在现有 `if (!pipeline_data.enabled) continue;` 和 `if (!context_->check_hit_count(pipeline_data)) continue;` 之后 —— SubPipeline reco 也尊重 enabled 和 hit_count 限制，与 vision-path 一致语义。

- [ ] **Step 2: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。

- [ ] **Step 3: 跑现有 smoke test 确认未破坏老场景**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"
python test/python/smoke_loopscan.py source/binding/Python install
```

Expected: `EXIT=0` + 老场景全绿（SubPipeline reco 没被任何旧 JSON 触发，新分支不会影响老路径）。

- [ ] **Step 4: Commit**

```powershell
git add source/MaaFramework/Task/PipelineTask.cpp
git commit -m "feat(pipeline): recognize_list 加 SubPipeline reco 分支

候选节点是 SubPipeline reco 类型时，直接调 execute_once 委托给子层。
子层任一命中 → 上浮命中节点的 box/detail 构造 RecoResult 返回。
子层全 miss → 该候选视为未命中、继续试 list 里下一个。

reco_id 通过 Recognizer::generate_reco_id() 分配，与 vision-path 一致。

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: Dumper（序列化回 JSON）

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineDumper.cpp`

- [ ] **Step 1: 在 dump_recognition 函数里加 SubPipeline case**

定位 `source/MaaFramework/Resource/PipelineDumper.cpp` 中负责把 `Recognition::Type` 序列化的函数（类似 `dump_reco_param` 或 switch 分发）。在 `Custom` case 后加：

```cpp
case Recognition::Type::SubPipeline: {
    const auto& param = std::get<Recognition::SubPipelineParam>(data.reco_param);
    obj["recognition_pipeline"] = param.recognition_pipeline;
    break;
}
```

`obj` 是当前序列化的目标 json 对象 — 看相邻 case 实际用的变量名照搬。

- [ ] **Step 2: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。

- [ ] **Step 3: Commit**

```powershell
git add source/MaaFramework/Resource/PipelineDumper.cpp
git commit -m "feat(pipeline): Dumper 输出 SubPipeline + recognition_pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 7: PipelineTypesV2 / JPipelineData

**Files:**
- Modify: `source/MaaFramework/Resource/PipelineTypesV2.h`

- [ ] **Step 1: 在 JPipelineData 里加 recognition_pipeline 字段**

定位 V2 类型定义文件 — 在 JPipelineData struct 里、`sub_pipeline` 字段附近加：

```cpp
std::optional<std::string> recognition_pipeline;
```

并在 `MEO_JSONIZATION(...)` 宏调用里追加 `MEO_OPT recognition_pipeline`，与 `MEO_OPT sub_pipeline` 同一行风格。

- [ ] **Step 2: 验证编译**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo --target MaaFramework"
```

Expected: 编译通过。

- [ ] **Step 3: Commit**

```powershell
git add source/MaaFramework/Resource/PipelineTypesV2.h
git commit -m "feat(pipeline): JPipelineData 加 recognition_pipeline 字段

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 8: Python binding

**Files:**
- Modify: `source/binding/Python/maa/pipeline.py`

- [ ] **Step 1: 在 JPipelineData 类里加 recognition_pipeline**

定位 `source/binding/Python/maa/pipeline.py`，在 `JPipelineData` 数据类里、`sub_pipeline` 字段附近加：

```python
recognition_pipeline: Optional[str] = None
```

并在 `from_dict` / `to_dict` 方法里加对应的解析 / 序列化分支：

```python
# from_dict
if "recognition_pipeline" in d:
    obj.recognition_pipeline = d["recognition_pipeline"]

# to_dict
if self.recognition_pipeline is not None:
    out["recognition_pipeline"] = self.recognition_pipeline
```

注意：现有 `sub_pipeline` 字段已经在文件里做了 from_dict / to_dict 处理 — 完全照搬同样模式。

- [ ] **Step 2: 验证 Python 语法**

```powershell
python -c "from maa.pipeline import JPipelineData; print('ok')" 
```

(在 source/binding/Python 路径下，需要 `$env:PYTHONPATH = 'source/binding/Python'` 或 `cd` 到那里)

Expected: 输出 `ok`，无 SyntaxError。

- [ ] **Step 3: Commit**

```powershell
git add source/binding/Python/maa/pipeline.py
git commit -m "feat(binding/python): JPipelineData 加 recognition_pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 9: NodeJS binding (TypeScript)

**Files:**
- Modify: `source/binding/NodeJS/src/apis/pipeline.d.ts`

- [ ] **Step 1: 在 General 接口里加 recognition_pipeline**

定位 `source/binding/NodeJS/src/apis/pipeline.d.ts`，在 `General` 接口（或对应的字段聚合接口）里、`sub_pipeline?: string` 之后加：

```typescript
recognition_pipeline?: string
```

并在 `recognition?: ` 联合类型里加 `"SubPipeline"`：

```typescript
recognition?: "DirectHit" | "TemplateMatch" | "FeatureMatch" | "ColorMatch" | "OCR" | "NeuralNetworkClassify" | "NeuralNetworkDetect" | "And" | "Or" | "Custom" | "SubPipeline"
```

（实际字符串列表照搬文件里现有的 reco 类型联合类型 — 加 `"SubPipeline"` 进去。）

- [ ] **Step 2: TypeScript 编译检查（如有 tsc 配置）**

```powershell
cd source/binding/NodeJS; npm run build 2>&1 | Select-String -Pattern error | Select-Object -First 5
```

如果项目没有独立的 tsc 配置或没装 node_modules，跳过 — 类型文件本身不需要构建。

- [ ] **Step 3: Commit**

```powershell
git add source/binding/NodeJS/src/apis/pipeline.d.ts
git commit -m "feat(binding/nodejs): TypeScript 类型同步 SubPipeline + recognition_pipeline

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 10: JSON Schema

**Files:**
- Modify: `tools/pipeline.schema.json`

- [ ] **Step 1: 在 recognition 字段的 enum 里加 SubPipeline**

定位 `tools/pipeline.schema.json` 中 `recognition` 字段的 enum 定义（搜 `"DirectHit"` 找到）。在 enum 数组末尾加：

```json
"SubPipeline"
```

并保留 `recognition_pipeline` 字段 — 在节点 properties 里加（紧挨 `sub_pipeline` 定义之后）：

```json
"recognition_pipeline": {
  "type": "string",
  "description": "When recognition is 'SubPipeline', delegate the recognition phase to this sub-pipeline entry node (single-pass; bare name or FQN form supported)."
}
```

- [ ] **Step 2: JSON 语法检查**

```powershell
python -c "import json; json.load(open('tools/pipeline.schema.json', encoding='utf-8')); print('ok')"
```

Expected: 输出 `ok`。

- [ ] **Step 3: Commit**

```powershell
git add tools/pipeline.schema.json
git commit -m "feat(schema): 补 SubPipeline reco 类型 + recognition_pipeline 字段

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 11: 中文文档

**Files:**
- Modify: `docs/zh_cn/3.1-任务流水线协议.md`

- [ ] **Step 1: 在 "识别算法" 章节末尾加 SubPipeline 类型小节**

定位 `docs/zh_cn/3.1-任务流水线协议.md` 中 `recognition` 列出各种算法（DirectHit / TemplateMatch / ... / Custom）的部分。在 `Custom` 之后追加：

````markdown
### SubPipeline

把识别阶段**委托**给一个子流水线。子层任一节点命中 → 本节点视为命中（命中节点的 box 上浮）；子层全 miss → 本节点视为 miss。

子流水线写法和 main 完全一样（pipeline 节点全部字段都可用），唯一约束是 **single-pass**（不循环）。

#### 字段

- `recognition_pipeline`: `string`，必填。指向一个 entry 节点名（同文件裸名或 `<file>::<node>` FQN 全名）。

#### 示例

```json
{
  "main": {
    "task_mode": "loop_scan",
    "next": ["尝试主页面", "尝试弹窗"]
  },
  "尝试主页面": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "主页面入口",
    "action": "DoNothing"
  },
  "主页面入口": {
    "recognition": "Custom",
    "custom_recognition": "MainPageReco",
    "next": ["主页面_按钮A", "主页面_按钮B"]
  },
  "主页面_按钮A": { "recognition": "TemplateMatch", "template": "btn_a.png", "action": "Click" },
  "主页面_按钮B": { "recognition": "TemplateMatch", "template": "btn_b.png", "action": "Click" }
}
```

#### ⚠️ 警告

- **entry 节点照常参与识别**。不要把它写成 `DirectHit` —— 否则子层第一帧立即命中、根本走不到 entry.next 里的实际探针。
- **子层的 `[Fallback]` 节点和 entry 的 `fallback_node` 字段在 SubPipeline 语境下被忽略**（fallback 必命中违反"探针"语义）。
- **子层节点声明的 `task_mode: "loop_scan"` 被忽略**（SubPipeline 强制 single-pass）。
- 嵌套深度限制为 **8 层**。超过则父节点视为 miss。

#### 与 `sub_pipeline` 字段的区别

- `recognition: "SubPipeline"`：reco 阶段委托 — **子层结果决定父是否命中**
- `sub_pipeline: "X"` 字段：父命中后调子例程 — 父已经决定命中后才进子层

两者可以**同时**声明：先用 SubPipeline 确认进入了主页面、命中后再跑主页面子例程。
````

- [ ] **Step 2: Commit**

```powershell
git add docs/zh_cn/3.1-任务流水线协议.md
git commit -m "docs(pipeline): 补 SubPipeline reco 类型中文文档

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 12: 英文文档

**Files:**
- Modify: `docs/en_us/3.1-PipelineProtocol.md`

- [ ] **Step 1: 在 "Recognition" 章节末尾加 SubPipeline 类型小节**

定位 `docs/en_us/3.1-PipelineProtocol.md` 中 recognition 算法列表的位置。在 `Custom` 之后追加：

````markdown
### SubPipeline

Delegates the **recognition phase** to a sub-pipeline. If any node in the sub-pipeline hits, this node is considered a hit (with the hit node's `box` bubbled up). If all sub-pipeline candidates miss, this node is considered a miss.

The sub-pipeline is written exactly like `main` (all pipeline node fields apply). The only constraint: **single-pass** (no looping).

#### Field

- `recognition_pipeline`: `string`, required. Points to an entry node name (bare name or `<file>::<node>` FQN).

#### Example

```json
{
  "main": {
    "task_mode": "loop_scan",
    "next": ["TryHomepage", "TryDialog"]
  },
  "TryHomepage": {
    "recognition": "SubPipeline",
    "recognition_pipeline": "HomepageEntry",
    "action": "DoNothing"
  },
  "HomepageEntry": {
    "recognition": "Custom",
    "custom_recognition": "MainPageReco",
    "next": ["HomeBtnA", "HomeBtnB"]
  },
  "HomeBtnA": { "recognition": "TemplateMatch", "template": "btn_a.png", "action": "Click" },
  "HomeBtnB": { "recognition": "TemplateMatch", "template": "btn_b.png", "action": "Click" }
}
```

#### ⚠️ Notes

- **The entry node participates in recognition normally**. Do **not** write it as `DirectHit` — that would cause the sub-pipeline to hit immediately on the first scan and skip the real probes in `entry.next`.
- **`[Fallback]` node attribute and entry's `fallback_node` field are ignored** under SubPipeline context (a always-hit fallback would break the "probe" semantics).
- **`task_mode: "loop_scan"` on sub-pipeline nodes is ignored** (SubPipeline forces single-pass).
- Maximum nesting depth is **8**. Beyond this, the parent node misses.

#### Difference from the `sub_pipeline` field

- `recognition: "SubPipeline"`: delegates the recognition phase — **sub-pipeline result determines parent hit/miss**
- `sub_pipeline: "X"` field: subroutine called **after** parent hits — parent already hit, then enters the sub-pipeline

Both can be declared simultaneously: use SubPipeline to confirm "we're on the homepage", then run the homepage subroutine after the hit.
````

- [ ] **Step 2: Commit**

```powershell
git add docs/en_us/3.1-PipelineProtocol.md
git commit -m "docs(pipeline): SubPipeline recognition type English documentation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 13: 解析层单元测试（pipeline_test.py）

**Files:**
- Modify: `test/python/pipeline_test.py`

- [ ] **Step 1: 加 SubPipeline reco 解析测试**

在 `test/python/pipeline_test.py` 里加新测试方法。模仿现有 `_test_sub_pipeline_field` 的模式：

```python
def _test_sub_pipeline_recognition(self):
    """Test that recognition: SubPipeline + recognition_pipeline parses + validates."""
    pipeline = {
        "TryHome": {
            "recognition": "SubPipeline",
            "recognition_pipeline": "HomeEntry",
            "next": []
        },
        "HomeEntry": {
            "recognition": "DirectHit",
            "next": ["HomeLeaf"]
        },
        "HomeLeaf": {
            "recognition": "DirectHit",
        }
    }
    resource = self._make_resource(pipeline)
    assert resource.loaded, "resource should load with SubPipeline reco"

    # node_list 里应该包含三个 FQN 节点
    nodes = sorted(resource.node_list)
    assert any(n.endswith("::TryHome") or n == "TryHome" for n in nodes), nodes
    assert any(n.endswith("::HomeEntry") or n == "HomeEntry" for n in nodes), nodes

def _test_sub_pipeline_recognition_invalid_ref(self):
    """recognition_pipeline 指向不存在的节点 → 加载失败"""
    pipeline = {
        "TryHome": {
            "recognition": "SubPipeline",
            "recognition_pipeline": "NoSuchNode",
            "next": []
        }
    }
    resource = self._make_resource(pipeline)
    assert not resource.loaded, "should fail to load when recognition_pipeline points to missing node"
```

并在 `run_all` / `__main__` 主调度处把这两个方法加进去。具体调度风格参考文件里其他 `_test_xxx` 方法的注册方式（看 phase1 / phase2 已加的方法是怎么注册的）。

`_make_resource` 这个辅助函数如果文件里已有就直接用；如果没有，参考现有测试方法里搭 tempdir + 写 JSON + load resource 的代码模式自己写一个本测试用的小版本。

- [ ] **Step 2: 运行单元测试**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"
python test/python/pipeline_test.py source/binding/Python install
```

Expected: 所有测试通过（包括新加的两个）。

- [ ] **Step 3: Commit**

```powershell
git add test/python/pipeline_test.py
git commit -m "test(pipeline): 增加 SubPipeline reco 解析 + 校验测试

_test_sub_pipeline_recognition: 正例
_test_sub_pipeline_recognition_invalid_ref: 引用不存在节点应加载失败

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 14: 集成测试（smoke_loopscan v3）

**Files:**
- Modify: `test/python/smoke_loopscan.py`

- [ ] **Step 1: 扩展 smoke test，加 SubPipeline reco 场景**

在现有 `test/python/smoke_loopscan.py`（v2）里加新场景。新场景独立成一个函数，main 调度时按顺序跑（先现有 loop_scan + sub_pipeline + Fallback 场景、再 SubPipeline reco 场景）。

新场景 pipeline JSON：

```python
SUB_RECO_PIPELINE = {
    "MainOrch": {
        "task_mode": "loop_scan",
        "cycle_delay": 80,
        "recognition": "DirectHit",
        "next": ["TryHomepage", "TryDialog"]
    },
    "TryHomepage": {
        "recognition": "SubPipeline",
        "recognition_pipeline": "HomepageEntry",
        "action": "Custom",
        "custom_action": "OnHomepageHit",
        "pre_delay": 0,
        "post_delay": 0
    },
    "TryDialog": {
        "recognition": "SubPipeline",
        "recognition_pipeline": "DialogEntry",
        "action": "Custom",
        "custom_action": "OnDialogHit",
        "pre_delay": 0,
        "post_delay": 0
    },
    "HomepageEntry": {
        "recognition": "Custom",
        "custom_recognition": "HomepageReco",
        "next": ["HomeBtnA", "HomeBtnB"]
    },
    "HomeBtnA": {
        "recognition": "Custom",
        "custom_recognition": "BtnAReco",
        "action": "Custom",
        "custom_action": "BtnAAction",
        "pre_delay": 0,
        "post_delay": 0
    },
    "HomeBtnB": {
        "recognition": "Custom",
        "custom_recognition": "BtnBReco",
        "action": "Custom",
        "custom_action": "BtnBAction",
        "pre_delay": 0,
        "post_delay": 0
    },
    "DialogEntry": {
        "recognition": "Custom",
        "custom_recognition": "DialogReco",
        "next": ["DialogClose"]
    },
    "DialogClose": {
        "recognition": "Custom",
        "custom_recognition": "DialogCloseReco",
        "action": "Custom",
        "custom_action": "DialogCloseAction",
        "pre_delay": 0,
        "post_delay": 0
    }
}
```

CustomRecognition 行为编排（实施时新增类，结构同 v2 已有的 MainReco / SubReco）：

| 类 | 第 1 帧 | 第 2 帧 | 第 3 帧 | 第 4 帧 |
|---|---|---|---|---|
| HomepageReco | hit | hit | miss | miss |
| BtnAReco | hit | miss | — | — |
| BtnBReco | — | hit | — | — |
| DialogReco | — | — | hit | miss → stop |
| DialogCloseReco | — | — | hit | — |

CustomActions：`BtnAAction` / `BtnBAction` / `DialogCloseAction` / `OnHomepageHit` / `OnDialogHit` 各自 +1 自己的 counter。

- [ ] **Step 2: 加断言**

新 `Counters` 子段：

```python
class SubRecoCounters:
    homepage_reco_hit = 0
    homepage_reco_miss = 0
    btn_a_action = 0
    btn_b_action = 0
    on_homepage_hit = 0
    dialog_close_action = 0
    on_dialog_hit = 0
    lock = threading.Lock()
```

最终断言（end of run）：

```python
assert SubRecoCounters.btn_a_action == 1, "BtnA 应在第 1 帧命中"
assert SubRecoCounters.btn_b_action == 1, "BtnB 应在第 2 帧命中"
assert SubRecoCounters.on_homepage_hit == 2, "OnHomepageHit 应在第 1+2 帧各 fire 一次"
assert SubRecoCounters.dialog_close_action == 1, "第 3 帧 main 应回退到 TryDialog 并触发 DialogClose"
assert SubRecoCounters.on_dialog_hit == 1
```

- [ ] **Step 3: 运行集成测试**

```powershell
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"
python test/python/smoke_loopscan.py source/binding/Python install
```

Expected:
- 老场景仍 PASS（loop_scan + sub_pipeline + [Fallback] + FQN）
- 新场景 PASS（SubPipeline reco + 父层任务编排）
- `EXIT=0`

如果断言失败，根据失败信息回头看 Task 5 的 RecoResult 构造是否正确（最常见问题：`reco_id` 没填 / box 没上浮 → 父无法识别为命中）。

- [ ] **Step 4: Commit**

```powershell
git add test/python/smoke_loopscan.py
git commit -m "test(pipeline): smoke_loopscan v3 增加 SubPipeline reco 场景

新场景验证 main 编排"试主页面 / 试弹窗"模式：
- 主页面子层命中 → 父 reco hit → 走父 next
- 主页面子层全 miss → main 顺序走下一个候选（TryDialog）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## 最终验收

所有 task 完成后：

```powershell
# 完整 rebuild + install
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --build build --config RelWithDebInfo"
cmd /c "`"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat`" -arch=x64 && cmake --install build --config RelWithDebInfo --prefix install"

# 所有测试
python test/python/pipeline_test.py source/binding/Python install
python test/python/smoke_loopscan.py source/binding/Python install
```

Expected: 全绿。

打 phase3 milestone tag（本地）：

```powershell
git tag phase3-subpipeline-reco-done
```

不推 origin（沿用 phase1/phase2 的本地 milestone 策略）。

---

## 任务依赖图

```
Task 1 (PipelineTypes.h)
  ↓
Task 2 (Parser) ─── Task 3 (Checker)
  ↓
Task 4 (execute_once 重构) ── 验证: 老 smoke_loopscan PASS
  ↓
Task 5 (recognize_list SubPipeline 分支) ── 验证: 老 smoke_loopscan PASS
  ↓
Task 6 (Dumper)    Task 7 (V2)    Task 8 (Python)    Task 9 (NodeJS)    Task 10 (Schema)
                            ↓
                  Task 11 (zh docs)    Task 12 (en docs)
                            ↓
                  Task 13 (单元测试)    Task 14 (集成测试 v3)
```

Tasks 6-10 之间彼此独立、可乱序。
Tasks 11-12 之间彼此独立。
Tasks 13-14 需要 Task 5 完成才能跑通新场景。
