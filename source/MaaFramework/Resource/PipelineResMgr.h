#pragma once

#include <filesystem>
#include <set>
#include <unordered_map>

#include <meojson/json.hpp>

#include "Common/Conf.h"
#include "DefaultPipelineMgr.h"
#include "MaaUtils/NonCopyable.hpp"
#include "PipelineTypes.h"

MAA_RES_NS_BEGIN

class PipelineResMgr : public NonCopyable
{
public:
    inline static constexpr std::string_view kFilePrefix_Ignore = ".";

public:
    bool load(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr);
    bool load_file(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr);
    void clear();

    // Phase 2: 把 JSON 文件路径转换为命名空间前缀
    // 例如 pipeline_root=<bundle>/pipeline, json_file=<bundle>/pipeline/battle/fight.json
    //   → "battle/fight"
    // pipeline_root 为空 / json_file 不在其下时返回空串（兼容老路径）
    static std::string compute_fqn_prefix(
        const std::filesystem::path& json_file,
        const std::filesystem::path& pipeline_root);

    // Phase 2: 裸名全局唯一回退查找。raw 不含 "::" 时扫描 map 找所有 *::raw 候选：
    //   - 恰好一条候选 → 返回该 iterator
    //   - 零条或多条 → 返回 map.end()
    // raw 含 "::" 时直接当 FQN 查找。
    static PipelineDataMap::const_iterator lookup_with_bare_fallback(
        const PipelineDataMap& map,
        const std::string& raw);

    const std::vector<std::filesystem::path>& get_paths() const { return paths_; }

    const PipelineDataMap& get_pipeline_data_map() const { return pipeline_data_map_; }

    PipelineDataMap& get_pipeline_data_map() { return pipeline_data_map_; }

    std::vector<std::string> get_node_list() const;

public:
    bool parse_and_override(
        const json::value& input,
        std::set<std::string>& existing_keys,
        const DefaultPipelineMgr& default_mgr,
        const std::string& fqn_prefix = {});

private:
    bool load_all_json(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr);
    bool open_and_parse_file(
        const std::filesystem::path& path,
        std::set<std::string>& existing_keys,
        const DefaultPipelineMgr& default_mgr,
        const std::string& fqn_prefix = {});
    bool parse_and_override_once(
        const json::object& input,
        std::set<std::string>& existing_keys,
        const DefaultPipelineMgr& default_mgr,
        const std::string& fqn_prefix = {});

private:
    std::vector<std::filesystem::path> paths_;
    PipelineDataMap pipeline_data_map_;
};

MAA_RES_NS_END
