#include "PipelineTask.h"

#include <random>
#include <stack>

#include "Component/Recognizer.h"
#include "Controller/ControllerAgent.h"
#include "Global/OptionMgr.h"
#include "MaaFramework/MaaMsg.h"
#include "MaaUtils/ImageIo.h"
#include "MaaUtils/JsonExt.hpp"
#include "MaaUtils/Logger.h"
#include "Resource/PipelineDumper.h"
#include "Resource/PipelineParser.h"
#include "Resource/ResourceMgr.h"
#include "Tasker/Tasker.h"

MAA_TASK_NS_BEGIN

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

bool PipelineTask::run_state_machine(const std::string& entry)
{
    std::stack<std::string> jumpback_stack;

    // there is no pretask for the entry, so we use the entry itself
    auto begin_opt = context_->get_pipeline_data(entry);
    if (!begin_opt) {
        LogError << "get_pipeline_data failed, task not exist" << VAR(entry);
        return false;
    }

    PipelineData node = std::move(*begin_opt);
    std::vector<MAA_RES_NS::NodeAttr> next = { { .name = entry } };

    bool error_handling = false;

    while (!next.empty() && !context_->need_to_stop()) {
        cur_node_ = node.name;
        auto run_result = run_next(next, node);
        const auto& node_detail = run_result.node_detail;

        if (context_->need_to_stop()) {
            LogWarn << "need_to_stop" << VAR(node.name);
            return true;
        }

        // 识别命中新节点
        if (node_detail.reco_id != MaaInvalidId) {
            error_handling = false;
            auto hit_opt = context_->get_pipeline_data(node_detail.name);
            if (!hit_opt) {
                LogError << "get_pipeline_data failed, task not exist" << VAR(node_detail.name);
                return false;
            }
            std::string pre_node_name = node.name;
            node = std::move(*hit_opt);

            if (node_detail.jump_back) {
                LogInfo << "push jumpback_stack:" << pre_node_name;
                jumpback_stack.emplace(pre_node_name);
            }

            if (node_detail.completed) {
                next = node.next;
            }
            else { // 动作执行失败了
                LogWarn << "node not completed, handle error" << VAR(node.name);
                error_handling = true;
                next = node.on_error;
                save_on_error(node.name);
            }
        }
        else if (error_handling) {
            LogError << "error handling loop detected" << VAR(node.name);
            next.clear();
            save_on_error(node.name);
        }
        else {
            LogWarn << "invalid node id, handle error" << VAR(node.name);
            error_handling = true;
            next = node.on_error;
            save_on_error(node.name);
        }

        if (next.empty() && !error_handling && !jumpback_stack.empty()) {
            auto top = std::move(jumpback_stack.top());
            LogInfo << "pop jumpback_stack:" << top;
            jumpback_stack.pop();

            auto top_opt = context_->get_pipeline_data(top);
            if (!top_opt) {
                LogError << "get_pipeline_data failed, task not exist" << VAR(top);
                return false;
            }
            node = std::move(*top_opt);

            next = node.next;
        }
    }

    return !error_handling;
}

bool PipelineTask::run_loop_scan(const std::string& entry)
{
    auto entry_data_opt = context_->get_pipeline_data(entry);
    if (!entry_data_opt) {
        LogError << "run_loop_scan: entry not found" << VAR(entry);
        return false;
    }
    const auto entry_data = *entry_data_opt;

    while (!context_->need_to_stop()) {
        (void)execute_once(entry, /*depth=*/ 0);

        if (context_->need_to_stop()) {
            return true;
        }

        std::this_thread::sleep_for(
            sample_delay(entry_data.cycle_delay, entry_data.cycle_delay_max));
    }

    return true;
}

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
    // 不论命中 / 兜底 / 无果，本层执行完一件事即返回上一层
    return result;
}

std::vector<MAA_RES_NS::NodeAttr> PipelineTask::build_chain(const std::string& entry)
{
    auto data_opt = context_->get_pipeline_data(entry);
    if (!data_opt) {
        LogError << "build_chain: entry not found" << VAR(entry);
        return {};
    }
    // entry 节点的 next 中，[Fallback] 节点已经在 parse 期被提取到 fallback_node 字段
    return data_opt->next;
}

void PipelineTask::run_fallback(const std::string& fallback_node_name)
{
    auto data_opt = context_->get_pipeline_data(fallback_node_name);
    if (!data_opt) {
        LogWarn << "fallback node not found" << VAR(fallback_node_name);
        return;
    }

    cur_node_ = fallback_node_name;

    LogInfo << "run_fallback" << VAR(fallback_node_name);

    // 兜底节点单独走识别 + 动作流程
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

void PipelineTask::post_stop()
{
    if (!context_) {
        LogError << "context is null";
        return;
    }
    context_->need_to_stop() = true;
}

PipelineTask::RunNextResult PipelineTask::run_next(
    const std::vector<MAA_RES_NS::NodeAttr>& next,
    const PipelineData& pretask,
    ScanOptions opts)
{
    if (!context_) {
        LogError << "context is null";
        return { };
    }

    bool valid = std::ranges::any_of(next, [&](const MAA_RES_NS::NodeAttr& node) {
        auto data_opt = context_->get_pipeline_data(node);
        return data_opt && data_opt->enabled;
    });
    if (!valid) {
        LogInfo << "no valid/enabled node in next" << VAR(next);
        return { };
    }

    auto node_id = generate_node_id();
    const auto start_clock = std::chrono::steady_clock::now();

    auto cur_opt = context_->get_pipeline_data(cur_node_);
    if (!cur_opt) {
        LogError << "get_pipeline_data failed, node not exist" << VAR(cur_node_);
        return { };
    }

    const auto& cur_node = *cur_opt;

    json::value node_cb_detail {
        { "task_id", task_id() },
        { "node_id", node_id },
        { "name", cur_node_ },
        { "focus", cur_node.focus },
    };

    notify(MaaMsg_Node_PipelineNode_Starting, node_cb_detail);

    auto check_timeout_and_sleep = [&](std::chrono::steady_clock::time_point current_clock) {
        if (pretask.reco_timeout >= std::chrono::milliseconds(0) && duration_since(start_clock) > pretask.reco_timeout) {
            LogWarn << "Task timeout" << VAR(pretask.name) << VAR(duration_since(start_clock)) << VAR(pretask.reco_timeout);
            return false;
        }

        LogDebug << "sleep_until" << VAR(pretask.rate_limit);
        std::this_thread::sleep_until(current_clock + pretask.rate_limit);
        return true;
    };

    while (!context_->need_to_stop()) {
        auto current_clock = std::chrono::steady_clock::now();
        cv::Mat image = screencap();

        if (image.empty()) {
            LogWarn << "screencap failed, skip recognition" << VAR(pretask.name);
            if (opts.single_pass) {
                break;  // single_pass：截图失败也算本帧结束，走 fallthrough 返回 completed=false
            }
            if (!check_timeout_and_sleep(current_clock)) {
                break;
            }
            continue;
        }

        RecoResult reco = recognize_list(image, next);

        if (context_->need_to_stop()) {
            LogWarn << "need_to_stop" << VAR(pretask.name);
            break;
        }

        if (!reco.box) {
            if (opts.single_pass) {
                break;  // single_pass：本帧未命中即返回 completed=false
            }
            if (!check_timeout_and_sleep(current_clock)) {
                break;
            }
            continue;
        }

        std::string hit_name = reco.name;
        auto hit_opt = context_->get_pipeline_data(hit_name);
        if (!hit_opt) {
            LogError << "get_pipeline_data failed, node not exist" << VAR(hit_name);

            notify(MaaMsg_Node_PipelineNode_Failed, node_cb_detail);

            return { };
        }

        // Resolve jump_back BEFORE action execution (anchors are still intact at this point)
        bool jump_back = std::ranges::any_of(next, [&](const MAA_RES_NS::NodeAttr& n) {
            if (!n.jump_back) {
                return false;
            }
            auto data_opt = context_->get_pipeline_data(n);
            return data_opt && data_opt->name == hit_name;
        });

        // 让 cur_node_ 反映真实命中节点，使 notify/log 中的 node 名是真实进度
        cur_node_ = hit_name;

        auto act = run_action(reco, *hit_opt);

        for (const auto& [anchor, target] : hit_opt->anchor) {
            context_->set_anchor(anchor, target);
        }

        NodeDetail node_detail {
            .node_id = node_id,
            .name = hit_name,
            .reco_id = reco.reco_id,
            .action_id = act.action_id,
            .completed = act.success,
            .jump_back = jump_back,
        };

        LogInfo << "PipelineTask node done" << VAR(node_detail) << VAR(task_id_);
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
    }

    NodeDetail node_detail {
        .node_id = node_id,
        .completed = false,
    };
    LogWarn << "PipelineTask bad next" << VAR(node_detail) << VAR(task_id_);
    set_node_detail(node_detail.node_id, node_detail);

    notify(MaaMsg_Node_PipelineNode_Failed, node_cb_detail);

    return RunNextResult { .node_detail = node_detail };
}

RecoResult PipelineTask::recognize_list(const cv::Mat& image, const std::vector<MAA_RES_NS::NodeAttr>& list)
{
    LogFunc << VAR(cur_node_) << VAR(list);

    if (!context_) {
        LogError << "context is null";
        return { };
    }

    auto cur_opt = context_->get_pipeline_data(cur_node_);
    if (!cur_opt) {
        LogError << "get_pipeline_data failed, node not exist" << VAR(cur_node_);
        return { };
    }

    const auto& cur_node = *cur_opt;

    const json::value reco_list_cb_detail {
        { "task_id", task_id() },
        { "name", cur_node_ },
        { "list", list },
        { "focus", cur_node.focus },
    };

    notify(MaaMsg_Node_NextList_Starting, reco_list_cb_detail);

    auto batch_plan = prepare_batch_ocr(list);
    auto ocr_cache = batch_plan ? std::make_shared<MAA_VISION_NS::OCRCache>() : nullptr;
    bool batch_triggered = false;

    for (const auto& node : list) {
        if (context_->need_to_stop()) {
            LogWarn << "need_to_stop";
            break;
        }

        auto node_opt = context_->get_pipeline_data(node);
        if (!node_opt) {
            LogError << "get_pipeline_data failed, node not exist" << VAR(node);
            continue;
        }
        const auto& pipeline_data = *node_opt;

        if (batch_plan && !batch_triggered && batch_plan->node_names.contains(pipeline_data.name)) {
            batch_triggered = true;

            Recognizer recognizer(tasker_, *context_, image, ocr_cache);
            recognizer.prefetch_batch_ocr(batch_plan->entries);
        }

        if (!pipeline_data.enabled) {
            LogDebug << "node disabled" << pipeline_data.name << VAR(pipeline_data.enabled);
            continue;
        }

        if (!context_->check_hit_count(pipeline_data)) {
            continue;
        }

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

        auto anchor_name = node.anchor ? std::optional { node.name } : std::nullopt;
        RecoResult result = run_recognition(image, pipeline_data, std::move(anchor_name), ocr_cache);

        if (result.box) {
            LogInfo << "reco hit" << VAR(result.name) << VAR(result.box);
            context_->increment_hit_count(pipeline_data.name);
        }

        if (context_->need_to_stop()) {
            LogWarn << "need_to_stop";
            break;
        }
        if (!result.box) {
            continue;
        }

        notify(MaaMsg_Node_NextList_Succeeded, reco_list_cb_detail);

        return result;
    }

    notify(MaaMsg_Node_NextList_Failed, reco_list_cb_detail);

    return { };
}

std::optional<PipelineTask::BatchOCRPlan> PipelineTask::prepare_batch_ocr(const std::vector<MAA_RES_NS::NodeAttr>& list)
{
    using namespace MAA_RES_NS::Recognition;

    if (!context_) {
        return std::nullopt;
    }

    OCRCollectContext ctx;

    for (const auto& node : list) {
        auto data_opt = context_->get_pipeline_data(node);
        if (!data_opt) {
            continue;
        }
        const auto& data = *data_opt;

        if (!data.enabled) {
            continue;
        }

        if (!context_->check_hit_count(data)) {
            continue;
        }

        collect_ocr_from_reco(ctx, data.name, data.reco_type, data.reco_param);
    }

    if (ctx.plan.entries.size() < 2) {
        LogDebug << "batch OCR not needed, eligible OCR nodes < 2" << VAR(ctx.plan.entries.size());
        return std::nullopt;
    }

    LogInfo << "prepared batch OCR plan" << VAR(ctx.plan.node_names) << VAR(ctx.plan.model);
    return ctx.plan;
}

void PipelineTask::try_add_ocr_node(OCRCollectContext& ctx, const std::string& name, const MAA_VISION_NS::OCRerParam& param)
{
    if (param.roi_target.type == MAA_VISION_NS::TargetType::PreTask) {
        const auto& ref_name = std::get<std::string>(param.roi_target.param);
        if (ctx.plan.node_names.contains(ref_name)) {
            LogDebug << "batch OCR skipping node with PreTask ROI dependency" << VAR(name) << VAR(ref_name);
            return;
        }
    }

    if (param.only_rec) {
        // 这玩意 Batch 出来结果顺序可能是乱的，不知道哪个是哪个
        // 我猜的，没试过，后面有空再看看
        return;
    }

    if (!param.color_filter.empty()) {
        // color_filter 需要对每个 ROI 单独做颜色二值化，无法与其他节点共享 mask 图
        return;
    }

    if (ctx.first) {
        ctx.plan.model = param.model;
        ctx.first = false;
    }
    else if (param.model != ctx.plan.model) {
        LogDebug << "batch OCR skipping node due to model mismatch" << VAR(name) << VAR(param.model) << VAR(ctx.plan.model);
        return;
    }

    if (ctx.plan.node_names.emplace(name).second) {
        ctx.plan.entries.emplace_back(BatchOCREntry { .name = name, .param = param });
    }
}

void PipelineTask::collect_ocr_from_reco(
    OCRCollectContext& ctx,
    const std::string& name,
    MAA_RES_NS::Recognition::Type type,
    const MAA_RES_NS::Recognition::Param& param)
{
    using namespace MAA_RES_NS::Recognition;

    if (type == Type::OCR) {
        try_add_ocr_node(ctx, name, std::get<MAA_VISION_NS::OCRerParam>(param));
    }
    else if (type == Type::And) {
        const auto& and_param = std::get<std::shared_ptr<AndParam>>(param);
        if (!and_param) {
            LogError << "Bad AND param" << VAR(name);
            return;
        }
        collect_ocr_from_sub_recognitions(ctx, and_param->all_of);
    }
    else if (type == Type::Or) {
        const auto& or_param = std::get<std::shared_ptr<OrParam>>(param);
        if (!or_param) {
            LogError << "Bad OR param" << VAR(name);
            return;
        }
        collect_ocr_from_sub_recognitions(ctx, or_param->any_of);
    }
}

void PipelineTask::collect_ocr_from_sub_recognitions(
    OCRCollectContext& ctx,
    const std::vector<MAA_RES_NS::Recognition::SubRecognition>& subs)
{
    using namespace MAA_RES_NS::Recognition;

    for (const auto& sub : subs) {
        if (auto* node_name = std::get_if<std::string>(&sub)) {
            auto sub_opt = context_->get_pipeline_data(*node_name);
            if (!sub_opt) {
                LogError << "Bad sub ref" << VAR(*node_name);
                continue;
            }
            collect_ocr_from_reco(ctx, sub_opt->name, sub_opt->reco_type, sub_opt->reco_param);
        }
        else {
            const auto& inline_sub = std::get<InlineSubRecognition>(sub);
            collect_ocr_from_reco(ctx, inline_sub.sub_name, inline_sub.type, inline_sub.param);
        }
    }
}

void PipelineTask::save_on_error(const std::string& node_name)
{
    const auto& option = MAA_GLOBAL_NS::OptionMgr::get_instance();

    if (!option.save_on_error()) {
        return;
    }

    if (!controller()) {
        LogError << "controller is null";
        return;
    }

    auto image = controller()->cached_image();
    if (image.empty()) {
        LogError << "cached_image is empty";
        return;
    }

    std::string filename = std::format("{}_{}.png", format_now_for_filename(), node_name);
    auto filepath = option.log_dir() / "on_error" / path(filename);
    imwrite(filepath, image);
    LogInfo << "save on error to" << filepath;
}

MAA_TASK_NS_END
