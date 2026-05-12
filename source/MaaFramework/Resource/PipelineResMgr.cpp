#include "PipelineResMgr.h"

#include <ranges>

#include "MaaUtils/Logger.h"
#include "MaaUtils/Platform.h"
#include "MaaUtils/StringMisc.hpp"
#include "PipelineChecker.h"
#include "PipelineParser.h"

MAA_RES_NS_BEGIN

bool PipelineResMgr::load(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr)
{
    LogFunc << VAR(path);

    if (!load_all_json(path, default_mgr)) {
        LogError << "load_all_json failed" << VAR(path);
        return false;
    }

    if (!PipelineChecker::check_all_validity(pipeline_data_map_)) {
        LogError << "check_all_validity failed" << VAR(path);
        return false;
    }

    paths_.emplace_back(path);

    return true;
}

bool PipelineResMgr::load_file(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr)
{
    LogFunc << VAR(path);

    std::set<std::string> existing_keys;
    if (!open_and_parse_file(path, existing_keys, default_mgr)) {
        LogError << "open_and_parse_file failed" << VAR(path);
        return false;
    }

    if (!PipelineChecker::check_all_validity(pipeline_data_map_)) {
        LogError << "check_all_validity failed" << VAR(path);
        return false;
    }

    paths_.emplace_back(path);

    return true;
}

void PipelineResMgr::clear()
{
    LogFunc;

    pipeline_data_map_.clear();
    paths_.clear();
}

PipelineDataMap::const_iterator PipelineResMgr::lookup_with_bare_fallback(
    const PipelineDataMap& map,
    const std::string& raw)
{
    // 直接精确匹配（覆盖 FQN 引用 / 老路径无前缀场景）
    auto direct = map.find(raw);
    if (direct != map.end()) {
        return direct;
    }
    // raw 已含 "::"：FQN 失败就是失败，不再回退
    if (raw.find("::") != std::string::npos) {
        return map.end();
    }
    // 全局唯一回退：扫描所有 *::raw 候选
    const std::string suffix = "::" + raw;
    PipelineDataMap::const_iterator hit = map.end();
    int hit_count = 0;
    for (auto it = map.begin(); it != map.end(); ++it) {
        if (it->first.size() > suffix.size() && it->first.ends_with(suffix)) {
            hit = it;
            if (++hit_count > 1) {
                return map.end();  // 多候选，模糊匹配视为未命中
            }
        }
    }
    return hit;
}

std::string PipelineResMgr::compute_fqn_prefix(
    const std::filesystem::path& json_file,
    const std::filesystem::path& pipeline_root)
{
    if (pipeline_root.empty()) {
        return {};
    }
    std::error_code ec;
    auto rel = std::filesystem::relative(json_file, pipeline_root, ec);
    if (ec || rel.empty()) {
        return {};
    }
    rel.replace_extension();  // 去掉 .json 后缀
    auto s = rel.generic_string();
    // 不在 root 下时 relative 会返回 ".." 开头的路径：回退到无命名空间
    if (s.starts_with("..")) {
        return {};
    }
    return s;
}

bool PipelineResMgr::load_all_json(const std::filesystem::path& path, const DefaultPipelineMgr& default_mgr)
{
    if (!std::filesystem::is_directory(path)) {
        LogError << "path is not directory" << VAR(path);
        return false;
    }

    bool valid = false;

    std::set<std::string> existing_keys;
    for (auto& entry : std::filesystem::recursive_directory_iterator(path)) {
        auto& entry_path = entry.path();
        if (entry.is_directory()) {
            LogDebug << "entry is directory" << VAR(entry_path);
            continue;
        }
        if (!entry.is_regular_file()) {
            LogWarn << "entry is not regular file, skip" << VAR(entry_path);
            continue;
        }
        auto relative = std::filesystem::relative(entry_path, path);
        if (std::ranges::any_of(relative, [](const auto& part) { return path_to_utf8_string(part).starts_with(kFilePrefix_Ignore); })) {
            LogWarn << "entry path contains component starting with '.', skip" << VAR(entry_path);
            continue;
        }

        auto ext = path_to_utf8_string(entry_path.extension());
        tolowers_(ext);
        if (ext != ".json" && ext != ".jsonc") {
            LogWarn << "entry is not *.json or *.jsonc, skip" << VAR(entry_path) << VAR(ext);
            continue;
        }

        // 计算 FQN 前缀（Phase 2 命名空间）
        auto fqn_prefix = compute_fqn_prefix(entry_path, path);

        bool parsed = open_and_parse_file(entry_path, existing_keys, default_mgr, fqn_prefix);
        if (!parsed) {
            LogError << "open_and_parse_file failed" << VAR(entry_path);
            return false;
        }

        valid = true;
    }

    return valid;
}

bool PipelineResMgr::open_and_parse_file(
    const std::filesystem::path& path,
    std::set<std::string>& existing_keys,
    const DefaultPipelineMgr& default_mgr,
    const std::string& fqn_prefix)
{
    LogFunc << VAR(path) << VAR(fqn_prefix);

    auto json_opt = json::open(path, true, true);
    if (!json_opt) {
        LogError << "json::open failed" << VAR(path);
        return false;
    }
    const auto& json = *json_opt;

    if (!json.is_object()) {
        LogError << "json is not object";
        return false;
    }

    if (!parse_and_override(json.as_object(), existing_keys, default_mgr, fqn_prefix)) {
        LogError << "parse_config failed" << VAR(path) << VAR(json);
        return false;
    }

    return true;
}

std::vector<std::string> PipelineResMgr::get_node_list() const
{
    auto k = pipeline_data_map_ | std::views::keys;
    return std::vector(k.begin(), k.end());
}

bool PipelineResMgr::parse_and_override(
    const json::value& input,
    std::set<std::string>& existing_keys,
    const DefaultPipelineMgr& default_mgr,
    const std::string& fqn_prefix)
{
    bool ret = false;
    if (input.is_object()) {
        ret = parse_and_override_once(input.as_object(), existing_keys, default_mgr, fqn_prefix);
    }
    else if (input.is_array()) {
        ret = !input.empty();
        for (const auto& val : input.as_array()) {
            if (!val.is_object()) {
                LogError << "input is not json array of object" << VAR(input);
                return false;
            }
            ret &= parse_and_override_once(val.as_object(), existing_keys, default_mgr, fqn_prefix);
        }
    }
    else {
        LogError << "input is invalid" << VAR(input);
        return false;
    }

    return ret;
}

bool PipelineResMgr::parse_and_override_once(
    const json::object& input,
    std::set<std::string>& existing_keys,
    const DefaultPipelineMgr& default_mgr,
    const std::string& fqn_prefix)
{
    // 用户在 JSON 里写的是裸名（如 "兜底"），按 fqn_prefix 注入命名空间得到
    // full_key（如 "battle/fight::兜底"），用作 pipeline_data_map_ 的 key。
    // 这样多个文件可以拥有同名节点而不冲突。
    // 引用解析：bare-name 查找时通过 lookup_with_bare_fallback 全局唯一匹配回退；
    // 文件内部引用（fallback_node / sub_pipeline）在 parse_node 里直接 qualify 为 FQN。
    // override_pipeline 路径 fqn_prefix 为空，行为与老路径完全一致（裸名 key）。
    for (const auto& [key, value] : input) {
        if (key.empty()) {
            LogError << "key is empty" << VAR(key);
            return false;
        }
        if (key.starts_with(PipelineData::kNodePrefix_Ignore)) {
            LogInfo << "key starts with '$', skip" << VAR(key);
            continue;
        }
        std::string full_key = fqn_prefix.empty() ? key : (fqn_prefix + "::" + key);
        if (existing_keys.contains(full_key)) {
            LogError << "key already exists" << VAR(full_key);
            return false;
        }
        if (!value.is_object()) {
            LogError << "value is not object" << VAR(key) << VAR(value);
            return false;
        }

        PipelineData result;
        const auto& default_result =
            pipeline_data_map_.contains(full_key) ? pipeline_data_map_.at(full_key) : default_mgr.get_pipeline();
        bool ret = PipelineParser::parse_node(full_key, value, result, default_result, default_mgr, fqn_prefix);
        if (!ret) {
            LogError << "parse_task failed" << VAR(full_key) << VAR(value);
            return false;
        }

        existing_keys.emplace(full_key);
        pipeline_data_map_.insert_or_assign(full_key, std::move(result));
    }

    return true;
}

MAA_RES_NS_END
