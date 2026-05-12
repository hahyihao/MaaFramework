#pragma once

#include "TaskBase.h"

#include <optional>
#include <set>

#include "Common/Conf.h"
#include "Vision/OCRer.h"

MAA_RES_NS_BEGIN
struct NodeAttr;
MAA_RES_NS_END

MAA_TASK_NS_BEGIN

struct BatchOCREntry
{
    std::string name;
    MAA_VISION_NS::OCRerParam param;

    MEO_TOJSON(name);
};

class PipelineTask : public TaskBase
{
public:
    using TaskBase::TaskBase;

    virtual ~PipelineTask() override = default;

    virtual bool run() override;
    virtual void post_stop() override;

private:
    struct BatchOCRPlan
    {
        std::string model;
        std::set<std::string> node_names;
        std::vector<BatchOCREntry> entries;
    };

    struct OCRCollectContext
    {
        BatchOCRPlan plan;
        bool first = true;
    };

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
    RecoResult recognize_list(const cv::Mat& image, const std::vector<MAA_RES_NS::NodeAttr>& list);
    std::optional<BatchOCRPlan> prepare_batch_ocr(const std::vector<MAA_RES_NS::NodeAttr>& list);

    void try_add_ocr_node(OCRCollectContext& ctx, const std::string& name, const MAA_VISION_NS::OCRerParam& param);
    void collect_ocr_from_reco(
        OCRCollectContext& ctx,
        const std::string& name,
        MAA_RES_NS::Recognition::Type type,
        const MAA_RES_NS::Recognition::Param& param);
    void collect_ocr_from_sub_recognitions(OCRCollectContext& ctx, const std::vector<MAA_RES_NS::Recognition::SubRecognition>& subs);

    void save_on_error(const std::string& node_name);
};

MAA_TASK_NS_END
