# Pipeline 循环扫描模式 + 子文件嵌套设计

- **日期**：2026-05-12
- **状态**：草案，待评审
- **影响范围**：`source/MaaFramework/Resource/`、`source/MaaFramework/Task/PipelineTask.cpp`、Pipeline JSON 协议

---

## 1. 背景

当前 `PipelineTask::run()` 是一个**状态机**：从 entry 节点出发沿 `next` 走，命中走 next，未命中走 `on_error`，链走完任务自然结束。

业务场景里常出现一类需求：**程序常驻、持续监控当前画面、看到什么就处理什么、看不到就等下一帧**。比如游戏机器人扫描整个 UI、识别当前页面、进入对应子流程。这类场景跟"状态机一次性走完"语义错位，强行用 `on_error` 兜底 + 任务重启会丢失节点级粒度。

仓库当前已有一份未提交的改动（见 `git diff`），用循环扫描器**替换**了原 `run()`，并硬编码了 `"兜底"` 节点名。这份改动方向对、实现糙，主要问题：

1. **直接替换破坏向后兼容** —— 所有依赖原状态机的现有 pipeline 行为静默改变
2. **兜底节点名硬编码中文字符串** —— 无法配置、i18n 失效
3. **`rate_limit` 字段语义被偷换** —— 同一字段在"截图轮询间隔"和"循环间延迟"两种概念上复用
4. **`scan_pretask.reco_timeout = 0`** —— hack 利用边界副作用实现单轮扫描
5. **`cur_node_` 永远等于 entry** —— 观察性彻底丢失
6. **未走 AGENTS.md 的"添加 Pipeline 字段"清单** —— PipelineTypesV2 / Dumper / Schema / Binding / Tests / Docs 全部缺失
7. **不支持"子 pipeline 嵌套"** —— 这是用户真正的核心诉求，当前改动完全没覆盖

本文档把这套需求拆成两期落地：

- **Phase 1**：清理当前改动 —— 循环扫描作为**可选模式**接入，兜底节点配置化，字段语义解耦
- **Phase 2**：在 Phase 1 基础上加 —— **文件级命名空间** + **子 pipeline 嵌套调用**

## 2. 目标

### 2.1 必须达成

1. 支持任务级配置切换"状态机模式 / 循环扫描模式"，**默认状态机，保持向后兼容**
2. 兜底节点改用 `[Fallback]` 节点属性标记，去除硬编码字符串
3. `rate_limit` 保留原义（截图轮询间隔），新增 `cycle_delay` / `cycle_delay_max` 表示循环间随机延迟
4. 每个 JSON 文件加载时自动获得文件路径作为命名空间前缀，多文件同名节点不冲突
5. 节点新增 `sub_pipeline` 字段，命中后递归进入对应文件作为子流程
6. 子流程**单次穿透**：执行一个动作（含兜底）后向上返回，最终回到 main 顶部重新扫一帧

### 2.2 不做

- 不替换默认状态机行为
- 不引入"模板继承 / extends"等节点级 OO 抽象
- 不引入"全局变量 / 任务内变量"，子流程通过现有 anchor / context 与父级通信

### 2.3 不破坏

- 现有 pipeline JSON 无需改动即可继续工作
- 现有 C API / Python / NodeJS 绑定签名不变（只新增字段）
- `on_error`、`[JumpBack]`、`[Anchor]`、`next` 优先级匹配等机制语义不变

## 3. Phase 1 设计：循环扫描 + 兜底配置化

### 3.1 执行模式切换（task_mode）

入口节点新增字段 `task_mode`，仅在入口节点起作用，子节点不读取。

```json
{
    "MyEntry": {
        "task_mode": "loop_scan",
        "next": ["page_a", "page_b"]
    }
}
```

| 值 | 含义 |
|---|---|
| `"state_machine"`（默认） | 现有状态机行为，链走完任务结束 |
| `"loop_scan"` | 循环扫描整条链，命中执行动作，全失败走兜底，永不主动结束 |

`PipelineTask::run()` 顶层做模式分发：

```cpp
bool PipelineTask::run() {
    auto entry_data = context_->get_pipeline_data(entry_);
    if (!entry_data) return false;

    switch (entry_data->task_mode) {
    case TaskMode::LoopScan:    return run_loop_scan(entry_);
    case TaskMode::StateMachine:
    default:                    return run_state_machine(entry_);
    }
}
```

`run_state_machine()` 是当前 `run()` 的**原样逻辑**（把它从当前未提交的改动里恢复出来）。新逻辑全部写在 `run_loop_scan()` 里。

### 3.2 兜底节点：`[Fallback]` 节点属性

复用 MaaFW 已有的节点属性体系（`[JumpBack]`、`[Anchor]`）。在 `next` 列表中**或独立节点声明上**用 `[Fallback]` 前缀标记：

```json
{
    "MyEntry": {
        "task_mode": "loop_scan",
        "next": ["page_a", "page_b", "[Fallback]GlobalFallback"]
    },
    "GlobalFallback": {
        "action": "Click",
        ...
    }
}
```

- `next` 数组里的 `[Fallback]NodeName` 在解析期被剥离前缀，目标节点名（`GlobalFallback`）记录到 entry 节点的 `fallback_node` 字段
- 当主链全部识别失败时，循环扫描器主动跳到 `fallback_node` 执行其 action
- `fallback_node` 不进入 main 链的扫描列表
- 一个 entry 节点最多一个 `[Fallback]`，多个时取第一个并记 warning

**为什么不直接复用现有 "兜底" 字符串：**
- i18n：英文用户不应该被强制起中文名
- 多文件 + 命名空间下，每个文件可以有自己的 `[Fallback]`
- 与 `[JumpBack]` / `[Anchor]` 风格一致

### 3.3 rate_limit 语义解耦

| 字段 | 含义 | 类型 |
|---|---|---|
| `rate_limit` | **保留原义**：截图轮询间隔（`run_next` 内部每次截图之间的最小 sleep） | `int` 或 `[min, max]` 数组 |
| `cycle_delay` | 循环扫描模式下，每完成一轮（命中执行或走兜底后）的等待时长 | `int` 或 `[min, max]` 数组 |
| `cycle_delay_max` | 同 `cycle_delay` 的上限，仅在 `cycle_delay` 是单值且 `cycle_delay_max > cycle_delay` 时启用区间随机 | `int` |

两个字段都支持两种写法：
- 标量 `1000`：固定 1000ms
- 数组 `[800, 1500]`：每次取 [800, 1500] 区间均匀采样

Parser 必须做以下校验，缺一者拒绝加载并打日志：

- 数组必须长度 2，元素必须是非负整数
- `arr[1] >= arr[0]`
- 标量必须非负整数

### 3.4 单轮扫描 API

当前实现把 `scan_pretask.reco_timeout = 0` 让 `run_next` 第一轮 timeout 退出，是 hack。

正确做法：`run_next` 加显式参数。

```cpp
struct ScanOptions {
    bool single_pass = false;       // true: 扫一遍未命中立即返回；false: 走 reco_timeout 内部 loop
};

NodeDetail PipelineTask::run_next(
    const std::vector<MAA_RES_NS::NodeAttr>& next,
    const PipelineData& pretask,
    ScanOptions opts = {});
```

`single_pass = true` 时跳过 `check_timeout_and_sleep` 的轮询循环，直接做一次截图 + 识别 + 命中处理后返回。

### 3.5 可观察性修复

`cur_node_` 应当反映**当前正在执行/识别的节点名**。循环扫描中：

- 进入 `run_loop_scan` 时 `cur_node_ = entry_`
- 命中节点 X 后，进入动作执行阶段前 `cur_node_ = X`
- 动作完成回到循环顶部时再设回 `entry_`

确保 `notify(MaaMsg_Node_*)` 上报的节点名是真实节点而非永远 entry。

### 3.6 循环扫描主流程

```cpp
bool PipelineTask::run_loop_scan(const std::string& entry) {
    auto entry_data_opt = context_->get_pipeline_data(entry);
    if (!entry_data_opt) return false;
    const auto entry_data = *entry_data_opt;

    auto chain = build_chain(entry);                       // 见 3.7

    while (!context_->need_to_stop()) {
        cur_node_ = entry;

        auto hit = run_next(chain, entry_data, ScanOptions{ .single_pass = true });

        if (context_->need_to_stop()) return true;

        if (hit.reco_id != MaaInvalidId && hit.completed) {
            // 命中并动作成功：什么都不需要额外做，hit 已经执行了 action
            // （Phase 2 在这里加 sub_pipeline 递归调用）
        }
        else if (entry_data.fallback_node) {
            run_fallback(*entry_data.fallback_node);       // 见 3.8
        }
        // 其他情况（识别命中但动作失败 / 无兜底）静默继续下一轮

        std::this_thread::sleep_for(
            sample_delay(entry_data.cycle_delay, entry_data.cycle_delay_max));
    }

    return true;
}
```

### 3.7 链构建（build_chain）

```cpp
std::vector<MAA_RES_NS::NodeAttr> PipelineTask::build_chain(const std::string& entry) {
    // entry 节点本身不进链；只展开它的 next 列表
    auto entry_data = context_->get_pipeline_data(entry);
    if (!entry_data) return {};

    std::vector<MAA_RES_NS::NodeAttr> chain;
    for (const auto& attr : entry_data->next) {
        // [Fallback] 前缀的节点不进 chain（它由 fallback_node 字段单独走）
        if (attr.is_fallback) continue;
        chain.push_back(attr);
    }
    return chain;
}
```

**关键差异（vs 当前实现）：**
- 不再沿 `next[0]` 递归展开成一长串扁平链
- entry 节点的 `next` 数组**就是**一帧要扫的所有候选
- 每个 next 节点的 `next` 是状态机模式的"下一步"，循环扫描模式下**不会**自动延伸进 chain
- 这样保留了 `next` 数组"同帧候选优先级"的原义

### 3.8 兜底动作执行（run_fallback）

```cpp
void PipelineTask::run_fallback(const std::string& fallback_node) {
    auto data_opt = context_->get_pipeline_data(fallback_node);
    if (!data_opt) {
        LogWarn << "fallback node not found" << VAR(fallback_node);
        return;
    }

    cur_node_ = fallback_node;

    // 兜底节点也走完整的识别 + 动作流程（DirectHit 节点动作直接执行）
    cv::Mat image = screencap();
    if (image.empty()) return;

    RecoResult reco = run_recognition(image, *data_opt, std::nullopt, nullptr);
    if (reco.box) {
        run_action(reco, *data_opt);
    }
}
```

兜底节点通常用 `DirectHit` 识别 + 任意动作，但**不强制**——用户可以让兜底节点也做识别（比如识别到"网络断开"图标才点重连按钮）。

### 3.9 Phase 1 数据结构变更

```cpp
// source/MaaFramework/Resource/PipelineTypes.h

enum class TaskMode {
    StateMachine,
    LoopScan,
};

struct NodeAttr {
    std::string name;
    bool jump_back = false;
    bool anchor = false;
    bool is_fallback = false;          // [Fallback] 标记
};

struct PipelineData {
    // ... 现有字段保留不动 ...

    // Phase 1 新增（只在 entry 节点用到，但放在 PipelineData 上以简化序列化）
    TaskMode task_mode = TaskMode::StateMachine;
    std::optional<std::string> fallback_node;
    std::chrono::milliseconds cycle_delay { 1000 };
    std::chrono::milliseconds cycle_delay_max { 0 };

    // rate_limit 保持原义和原默认值（已经存在）
};
```

## 4. Phase 2 设计：文件命名空间 + 子 pipeline 嵌套

### 4.1 文件级命名空间（自动前缀）

当前 `PipelineResMgr::parse_json` 把所有 JSON 文件加载到一个全局 `unordered_map<string, PipelineData>`，同名节点 forbidden。

Phase 2 改造：**加载时给每个节点名注入"文件路径前缀"**，作为全限定名（fully-qualified name, FQN）。

**前缀计算规则：**
- 文件路径：相对于该 bundle 的 `pipeline/` 根目录
- 去掉 `.json` 扩展名
- 路径分隔符统一为 `/`
- 拼接符号：`::`

例：
| 文件路径 | 节点原名 | FQN |
|---|---|---|
| `pipeline/main.json` | `entry` | `main::entry` |
| `pipeline/battle/fight.json` | `entry` | `battle/fight::entry` |
| `pipeline/battle/fight.json` | `兜底` | `battle/fight::兜底` |
| `pipeline/menu/login.json` | `兜底` | `menu/login::兜底` |

**最终 `pipeline_data_map_` 的 key 全部是 FQN。**

### 4.2 节点引用解析（作用域查找）

**所有引用解析在加载期完成**：解析器在 parse JSON 时，把每个 `next` / `on_error` / `[Fallback]` / `sub_pipeline` 引用转换成 FQN，存进 `PipelineData`。运行期 `get_pipeline_data` 直接按 FQN 查找，**无运行期作用域开销**。

引用是否已经是 FQN，按字符串是否包含 `::` 判断：

| 用户写法 | 判定 | 解析过程 |
|---|---|---|
| 含 `::` —— 如 `"battle/fight::attack"` | **绝对引用** | 直接当 FQN，验证在全局 map 存在；不存在则加载失败 |
| 不含 `::` —— 如 `"attack"` | **相对引用** | 先在当前文件作用域找：拼成 `{current_prefix}::attack` 查找；找到即用此 FQN |
| 相对引用在当前作用域找不到 | **全局唯一匹配回退** | 遍历 map 找所有 key 形如 `*::attack` 的条目，若**恰好一条**则用它（兼容跨文件无歧义引用）；若多条或零条，加载失败并列出候选 |

PipelineParser 在 parse 每个 JSON 文件时记录其 FQN 前缀，把上述解析结果写回 `NodeAttr.name` / `PipelineData::on_error[...].name` / `PipelineData::fallback_node` / `PipelineData::sub_pipeline`。**Parse 完成后所有引用都是绝对 FQN，运行期不再做作用域查找。**

**举例：**

```
全局 map keys：
  main::Start
  main::check_battle
  battle/fight::entry
  battle/fight::attack
  battle/fight::兜底
  menu/login::兜底
```

| 文件 | 用户写 | 解析为 |
|---|---|---|
| `main.json` | `"check_battle"` | `main::check_battle` ✅（当前作用域命中） |
| `main.json` | `"battle/fight::entry"` | `battle/fight::entry` ✅（绝对引用） |
| `battle/fight.json` | `"兜底"` | `battle/fight::兜底` ✅（当前作用域命中） |
| `battle/fight.json` | `"attack"` | `battle/fight::attack` ✅ |
| `battle/fight.json` | `"check_battle"` | `main::check_battle` ✅（当前作用域无，但全局唯一匹配） |
| `main.json` | `"兜底"` | ❌ 加载失败：当前作用域 `main::兜底` 不存在，全局有两条候选 `battle/fight::兜底` / `menu/login::兜底` |

**用户体验对比：**

```json
// battle/fight.json —— 用户视角：跟以前一样
{
    "entry": {
        "task_mode": "loop_scan",
        "next": ["click_attack", "[Fallback]兜底"]
    },
    "click_attack": { "action": "Click", ... },
    "兜底": { "action": "DoNothing" }
}
```

- 同一份 JSON 里同名节点冲突——保留现有"forbidden"检查（但只在**同一文件内**冲突时报错）
- 跨文件同名（`main::兜底` vs `battle/fight::兜底`）——合法、互不影响

### 4.3 子 pipeline 触发：`sub_pipeline` 字段

节点新增字段：

```cpp
std::optional<std::string> sub_pipeline;     // 引用另一个 JSON 文件的 entry 节点 FQN
```

JSON 示例：

```json
// main.json
{
    "MainEntry": {
        "task_mode": "loop_scan",
        "next": ["check_battle", "check_menu", "[Fallback]MainFallback"]
    },
    "check_battle": {
        "recognition": "TemplateMatch",
        "template": "battle_icon.png",
        "sub_pipeline": "battle/fight::entry"
    },
    "check_menu": {
        "recognition": "TemplateMatch",
        "template": "menu_icon.png",
        "sub_pipeline": "menu/login::entry"
    },
    "MainFallback": {
        "action": "Click", "target": [100, 100, 50, 50]
    }
}
```

```json
// battle/fight.json
{
    "entry": {
        "task_mode": "loop_scan",
        "next": ["click_attack", "claim_reward", "[Fallback]兜底"]
    },
    "click_attack": { "action": "Click", ... },
    "claim_reward": {
        "sub_pipeline": "battle/reward::entry"
    },
    "兜底": { "action": "DoNothing" }
}
```

### 4.4 递归执行模型

把 `run_loop_scan` 重构成 `execute_once(pipeline_name, depth)` + 顶层 while。

```cpp
constexpr int kMaxNestingDepth = 8;

bool PipelineTask::run_loop_scan(const std::string& entry) {
    auto entry_data_opt = context_->get_pipeline_data(entry);
    if (!entry_data_opt) return false;
    const auto entry_data = *entry_data_opt;

    while (!context_->need_to_stop()) {
        execute_once(entry, /*depth=*/ 0);
        std::this_thread::sleep_for(
            sample_delay(entry_data.cycle_delay, entry_data.cycle_delay_max));
    }
    return true;
}

void PipelineTask::execute_once(const std::string& pipeline_entry, int depth) {
    if (depth > kMaxNestingDepth) {
        LogError << "max nesting depth exceeded" << VAR(pipeline_entry) << VAR(depth);
        return;
    }

    auto entry_data_opt = context_->get_pipeline_data(pipeline_entry);
    if (!entry_data_opt) return;
    const auto entry_data = *entry_data_opt;

    cur_node_ = pipeline_entry;
    auto chain = build_chain(pipeline_entry);
    auto hit = run_next(chain, entry_data, ScanOptions{ .single_pass = true });

    if (context_->need_to_stop()) return;

    if (hit.reco_id != MaaInvalidId && hit.completed) {
        auto hit_data_opt = context_->get_pipeline_data(hit.name);
        if (hit_data_opt && hit_data_opt->sub_pipeline) {
            execute_once(*hit_data_opt->sub_pipeline, depth + 1);   // 递归进子层
        }
    }
    else if (entry_data.fallback_node) {
        run_fallback(*entry_data.fallback_node);
    }
    // 函数返回 → 调用方（父层 execute_once 或顶层 run_loop_scan）继续向上返回
}
```

**关键性质：**

1. **递归只发生在"命中且 sub_pipeline 非空"时** —— 否则就是普通的"扫一帧、做一件事、返回"
2. **子层永不自己循环** —— 子层的 `execute_once` 跑完就 return，不存在子死循环
3. **退出路径唯一** —— 顶层 while + 函数自然返回，"扫一帧重新开始"自动达成
4. **递归保护** —— `kMaxNestingDepth=8`，防止配置错误导致 A→B→A 死循环

### 4.5 子流程共享 context

`Tasker::Context` 是任务级单例，所有递归层共享：

- `set_anchor` / `get_anchor` 跨层可见（这是特性，子流程可以读父级设的锚点）
- `need_to_stop` 跨层共享，顶层 stop 信号能立即让所有层退出
- `task_id` 整个递归过程使用同一个

### 4.6 Phase 2 数据结构变更

```cpp
// source/MaaFramework/Resource/PipelineTypes.h（在 Phase 1 基础上）

struct PipelineData {
    // ... Phase 1 字段 ...

    // Phase 2 新增
    std::optional<std::string> sub_pipeline;   // 命中后递归进入的子 pipeline FQN
};
```

```cpp
// source/MaaFramework/Resource/PipelineResMgr.h（新增）

class PipelineResMgr {
    // ... 现有 ...

    // 把 JSON 文件路径转换成命名空间前缀
    static std::string compute_fqn_prefix(
        const std::filesystem::path& json_file,
        const std::filesystem::path& pipeline_root);

    // 把裸节点名解析为 FQN（按当前文件作用域 + 全局回退）
    std::string resolve_name(
        const std::string& raw_name,
        const std::string& current_fqn_prefix) const;
};
```

## 5. JSON 配置完整示例

`pipeline/main.json`：
```json
{
    "Start": {
        "task_mode": "loop_scan",
        "cycle_delay": [800, 1500],
        "next": [
            "check_main_menu",
            "check_battle",
            "[Fallback]restart_app"
        ]
    },
    "check_main_menu": {
        "recognition": "TemplateMatch",
        "template": "main_menu.png",
        "action": "DoNothing",
        "sub_pipeline": "menu::entry"
    },
    "check_battle": {
        "recognition": "TemplateMatch",
        "template": "battle.png",
        "action": "DoNothing",
        "sub_pipeline": "battle::entry"
    },
    "restart_app": {
        "action": "StopApp",
        "package": "com.example.game"
    }
}
```

`pipeline/menu.json`：
```json
{
    "entry": {
        "task_mode": "loop_scan",
        "next": ["click_start", "claim_daily", "[Fallback]兜底"]
    },
    "click_start": {
        "recognition": "TemplateMatch", "template": "start_btn.png",
        "action": "Click"
    },
    "claim_daily": {
        "recognition": "TemplateMatch", "template": "daily_reward.png",
        "action": "Click"
    },
    "兜底": {
        "action": "Swipe", "begin": [500, 500], "end": [500, 200]
    }
}
```

`pipeline/battle.json`：
```json
{
    "entry": {
        "task_mode": "loop_scan",
        "next": ["attack", "[Fallback]兜底"]
    },
    "attack": {
        "recognition": "TemplateMatch", "template": "attack_btn.png",
        "action": "Click"
    },
    "兜底": {
        "action": "DoNothing"
    }
}
```

**运行时行为：**
1. 启动 `Start` 任务 → 进入 main 循环
2. 扫描 `[check_main_menu, check_battle]` 同帧候选
3. 若 `check_main_menu` 命中 → 进入 `menu.json` 的 `entry`（FQN `menu::entry`）→ 单次扫描 `[click_start, claim_daily]` → 命中执行 / 全失败走 `menu::兜底` → 函数返回到 main
4. main 等待 `cycle_delay` 后扫下一帧
5. 若所有页面都没识别到 → main 触发 `MainFallback`（重启 app）→ 等待 → 重扫

## 6. 兼容性

| 项 | 兼容性 |
|---|---|
| 现有 pipeline JSON（无 `task_mode` 字段） | ✅ 默认 `state_machine`，行为完全不变 |
| 现有 `next` / `on_error` 写法 | ✅ 不变 |
| 现有 `[JumpBack]` / `[Anchor]` 节点属性 | ✅ 不变，`[Fallback]` 与之并列 |
| 现有 `rate_limit` 字段 | ✅ 单值写法保留原义；新增 `[min, max]` 写法是可选扩展 |
| 现有 Python / NodeJS 绑定 | ✅ 现有 API 签名不变，只新增字段（属于附加） |
| 单文件 pipeline | ✅ 单文件时 FQN 前缀只是多了一层"文件名前缀"，裸名查找仍然命中 |
| 多 bundle 加载 | ✅ 不同 bundle 之间的同名 JSON 文件依然合并（按相对路径前缀区分） |

**唯一的潜在破坏点：**
- 用户如果在 JSON 里用了形如 `foo::bar` 的节点名，Phase 2 会被误解析为 FQN。需要在 release notes 明确"`::` 是保留分隔符"。建议加载时检测裸名包含 `::` 时打 warning。

## 7. 落地清单（按 AGENTS.md 规范）

### Phase 1
- [ ] `source/MaaFramework/Resource/PipelineTypes.h`：加 `TaskMode` 枚举、`NodeAttr::is_fallback`、`PipelineData::{task_mode, fallback_node, cycle_delay, cycle_delay_max}`
- [ ] `source/MaaFramework/Resource/PipelineTypesV2.h`：相应 `J*` 序列化结构体加字段
- [ ] `source/MaaFramework/Resource/PipelineParser.cpp`：
  - 解析 `task_mode`
  - 解析 `[Fallback]` 节点属性，记入 entry 的 `fallback_node`
  - 解析 `cycle_delay` / `cycle_delay_max`（标量 / 数组两种形态）
  - 修复 `rate_limit` 数组解析的空数组分支与类型校验
- [ ] `source/MaaFramework/Resource/PipelineDumper.cpp`：所有新字段加 dump 逻辑
- [ ] `source/MaaFramework/Resource/DefaultPipelineMgr.{h,cpp}`：新字段加默认值
- [ ] `source/MaaFramework/Task/PipelineTask.{h,cpp}`：
  - 把当前未提交的 `run()` 改动**全部回退**
  - 新增 `run_state_machine()`（即原 `run()`）+ `run_loop_scan()` + `build_chain()` + `run_fallback()`
  - `run()` 顶层做模式分发
  - `run_next` 加 `ScanOptions{ single_pass }` 参数
  - `cur_node_` 维护时机修复
- [ ] `source/binding/Python/maa/pipeline.py`：新字段加到对应 dataclass
- [ ] `source/binding/NodeJS/src/apis/`：相应 `.h/.cpp/.d.ts` 同步
- [ ] `tools/pipeline.schema.json`：新字段加 schema
- [ ] `test/python/pipeline_test.py`：
  - `task_mode` 解析 + 默认值
  - `[Fallback]` 解析 + 在 `next` 中的位置（首尾、缺失）
  - `cycle_delay` 标量 / 数组形态 + 边界校验（负数、空数组、min>max）
  - `rate_limit` 数组写法解析
- [ ] `docs/zh_cn/3.1-任务流水线协议.md` + `docs/en_us/3.1-pipeline.md`：补 task_mode / [Fallback] / cycle_delay / rate_limit 数组写法的说明

### Phase 2
- [ ] `source/MaaFramework/Resource/PipelineResMgr.{h,cpp}`：
  - 加 `compute_fqn_prefix()` 把文件路径转成命名空间前缀
  - 加载时把节点名 + 所有引用名换成 FQN
  - 同文件内同名冲突仍然 forbidden，跨文件同名合法
  - 裸名包含 `::` 时打 warning
- [ ] `source/MaaFramework/Resource/PipelineTypes.h`：`PipelineData::sub_pipeline` 字段
- [ ] `source/MaaFramework/Resource/PipelineTypesV2.h` + `PipelineParser.cpp` + `PipelineDumper.cpp`：`sub_pipeline` 序列化
- [ ] `source/MaaFramework/Task/PipelineTask.{h,cpp}`：
  - 把 `run_loop_scan` 重构成 `execute_once(name, depth)` + 顶层 while
  - 加 `kMaxNestingDepth` 保护
- [ ] `source/binding/Python/maa/pipeline.py` + NodeJS：`sub_pipeline` 字段
- [ ] `tools/pipeline.schema.json`：`sub_pipeline` 字段
- [ ] `test/python/pipeline_test.py`：
  - 单文件 FQN 前缀注入
  - 多文件同名节点不冲突
  - 跨文件显式 FQN 引用
  - `sub_pipeline` 递归调用 + 深度限制
  - 子层 fallback 不影响父层
- [ ] `docs/zh_cn/3.1-*` + `docs/en_us/3.1-*`：补命名空间规则、`sub_pipeline` 字段、嵌套示例

## 8. 风险与开放问题

### 8.1 已识别风险

| 风险 | 缓解措施 |
|---|---|
| `::` 作为分隔符与已有节点名冲突 | 加载时 warning；release notes 明确保留字符 |
| 递归深度超限静默失败 | `kMaxNestingDepth` 触发时打 `LogError`，可考虑通过 `notify` 上报 |
| 子层 stop 信号传播延迟 | `need_to_stop` 是 context 级共享，每次 `execute_once` 入口和 chain 扫描中都检查 |
| `cycle_delay` 配错（如 `[1500, 800]`）导致行为异常 | Parser 强校验 + 加载失败 |
| 单文件 pipeline 加了 FQN 前缀影响外部工具 | Dumper 默认输出 FQN，外部工具看到的就是 FQN；如有需要可加 dump 选项"输出短名" |

### 8.2 待定问题

- **子层是否允许有自己的 `task_mode`？** 当前设计：子层入口节点的 `task_mode` 字段被**忽略**——子层永远是"单次穿透"。是否需要支持"嵌套时切回状态机模式跑完整条 next 链"？默认不支持，待真实需求出现再加。
- **`sub_pipeline` 引用一个不存在的 FQN：** 当前设计加载期失败；运行期再次查找时如果还是没有就 LogError 跳过。是否要更激进（任务整体失败退出）？倾向保守路线：跳过 + 上报。
- **`[Fallback]` 出现在非 entry 节点的 `next` 中：** 当前只在 entry 节点的 `next` 解析时识别 `[Fallback]`。非 entry 节点的 `next` 出现 `[Fallback]` 时只剥离前缀按普通节点处理（或打 warning）？倾向 warning。

## 9. 实施顺序建议

1. **Step 0**：把当前未提交的改动整理成一个 WIP commit 备份
2. **Step 1**：把 `PipelineTask::run()` 恢复到 `git HEAD`，确保 `state_machine` 路径完整
3. **Step 2**：Phase 1 全部落地（按上面清单），单元测试通过
4. **Step 3**：手动写一个 `loop_scan` 模式的 demo pipeline 跑通
5. **Step 4**：Phase 2 命名空间改造（PipelineResMgr）
6. **Step 5**：Phase 2 `sub_pipeline` 字段 + 递归执行
7. **Step 6**：跑多文件嵌套 demo
8. **Step 7**：补完文档、绑定、Schema

每一 Step 完成应至少：单元测试 + 一次 demo 手验 + commit。
