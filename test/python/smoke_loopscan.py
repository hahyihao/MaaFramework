"""
Smoke test for loop_scan + [Fallback] + sub_pipeline runtime execution.

不依赖 TestingDataSet。自带 1 张黑色 PNG 当 DbgController 的"屏幕"。

验证项：
  1. task_mode='loop_scan' → 框架持续循环扫描，不会一次性结束
  2. FQN 命名空间自动注入（pipeline 加载后所有节点名变成 `<file>::<name>`）
  3. 裸名引用通过 lookup_with_bare_fallback 正确解析
  4. 命中节点声明 sub_pipeline 时 execute_once 递归进子层
  5. 子层 SubLeaf 识别 + 动作执行
  6. 主层全部未命中时触发 [Fallback]
  7. 任务可被 stop 干净结束（不发生 race）

退出码：
  0 - PASS
  非 0 - FAIL（详细原因写入 counters.json）
"""

import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

# 让 Python print 强制走 stderr 而非 stdout（MaaFw 的 logger 也用 stdout，会盖住我们）
def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


if len(sys.argv) < 3:
    log("Usage: python smoke_loopscan.py <binding_dir> <install_dir>")
    sys.exit(2)

binding_dir = Path(sys.argv[1]).resolve()
install_dir = Path(sys.argv[2]).resolve()
os.environ["MAAFW_BINARY_PATH"] = str(install_dir / "bin")
sys.path.insert(0, str(binding_dir))

from maa.library import Library
from maa.resource import Resource
from maa.controller import DbgController
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import Context


# 计数器（线程安全 + 持久化到文件）
COUNTER_FILE: Path = Path()  # 在 main 中赋值


class Counters:
    main_reco = 0
    main_action = 0
    sub_reco = 0
    sub_action = 0
    fallback = 0
    sub_pipeline_entries = 0  # 通过日志难拿到，这个由父层命中后 +1
    lock = threading.Lock()

    @classmethod
    def dump(cls) -> dict:
        return {
            "main_reco": cls.main_reco,
            "main_action": cls.main_action,
            "sub_reco": cls.sub_reco,
            "sub_action": cls.sub_action,
            "fallback": cls.fallback,
            "sub_pipeline_entries": cls.sub_pipeline_entries,
        }

    @classmethod
    def flush(cls) -> None:
        try:
            COUNTER_FILE.write_text(json.dumps(cls.dump(), indent=2), encoding="utf-8")
        except Exception:
            pass


# 由主线程注入，便于 reco 内通知 main 已经收集够数据
class Control:
    tasker = None  # type: Tasker | None
    target_main_hits = 2
    # miss 2 次：第一次 miss 让 framework 跑 fallback，第二次 miss 时 stop（确保 fallback 已经执行过）
    target_misses = 2
    misses_seen = 0
    stop_requested = False


# ─────────────────────────── Custom 实现 ───────────────────────────


class MainReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        with Counters.lock:
            Counters.main_reco += 1
            n = Counters.main_reco
        Counters.flush()
        log(f"  [MainReco] call #{n}")
        # 前 target_main_hits 次返回 hit；之后返回 miss 触发 [Fallback]
        if n <= Control.target_main_hits:
            return CustomRecognition.AnalyzeResult(box=(0, 0, 50, 50), detail=f"hit-{n}")
        # 之后 miss target_misses 次后请 framework 停下
        with Counters.lock:
            Control.misses_seen += 1
            done = Control.misses_seen >= Control.target_misses
        if done and not Control.stop_requested:
            Control.stop_requested = True
            if Control.tasker is not None:
                log(f"  [MainReco] signal stop after {n} reco calls")
                Control.tasker.post_stop()
        return CustomRecognition.AnalyzeResult(box=None, detail=f"miss-{n}")


class MainAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.main_action += 1
            Counters.sub_pipeline_entries += 1  # 命中并完成 action 后会触发 sub_pipeline 进入
            n = Counters.main_action
        Counters.flush()
        log(f"  [MainAction] call #{n}")
        return True


class SubReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        with Counters.lock:
            Counters.sub_reco += 1
            n = Counters.sub_reco
        Counters.flush()
        log(f"    [SubReco] call #{n}")
        return CustomRecognition.AnalyzeResult(box=(10, 10, 30, 30), detail=f"sub-{n}")


class SubAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.sub_action += 1
            n = Counters.sub_action
        Counters.flush()
        log(f"    [SubAction] call #{n}")
        return True


class FallbackAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.fallback += 1
        Counters.flush()
        log(f"  [FallbackAction] FIRED (cumulative={Counters.fallback})")
        return True


# ─────────────────────────── 工具 ───────────────────────────


def make_black_png(width: int = 100, height: int = 100) -> bytes:
    """Hand-craft 一张全黑 RGB PNG，避免依赖 PIL。"""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# ─────────────────────────── 主流程 ───────────────────────────


def main() -> int:
    global COUNTER_FILE

    log(f"=== smoke_loopscan v2 ===")
    log(f"MaaFw Version: {Library.version()}")
    Toolkit.init_option(install_dir / "bin")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        COUNTER_FILE = tmp / "counters.json"

        screenshot_dir = tmp / "Screenshot"
        screenshot_dir.mkdir()
        (screenshot_dir / "frame.png").write_bytes(make_black_png(200, 200))

        pipeline_dir = tmp / "resource" / "pipeline"
        pipeline_dir.mkdir(parents=True)
        pipeline = {
            "MainEntry": {
                "task_mode": "loop_scan",
                "cycle_delay": 80,
                "recognition": "DirectHit",
                "action": "DoNothing",
                "next": ["TriggerSub", "[Fallback]MainFallback"],
            },
            "TriggerSub": {
                "recognition": "Custom",
                "custom_recognition": "MainReco",
                "action": "Custom",
                "custom_action": "MainAction",
                "sub_pipeline": "SubEntry",
                "pre_delay": 0,
                "post_delay": 0,
            },
            "MainFallback": {
                "recognition": "DirectHit",
                "action": "Custom",
                "custom_action": "FallbackAction",
                "pre_delay": 0,
                "post_delay": 0,
            },
            "SubEntry": {
                "recognition": "DirectHit",
                "next": ["SubLeaf"],
            },
            "SubLeaf": {
                "recognition": "Custom",
                "custom_recognition": "SubReco",
                "action": "Custom",
                "custom_action": "SubAction",
                "pre_delay": 0,
                "post_delay": 0,
            },
        }
        (pipeline_dir / "smoke.json").write_text(
            json.dumps(pipeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Resource
        resource = Resource()
        # 保留实例引用以防 GC（Custom 注册的是回调指针，Python 端不持引用会有 race）
        recos = [MainReco(), SubReco()]
        actions = [MainAction(), SubAction(), FallbackAction()]
        resource.register_custom_recognition("MainReco", recos[0])
        resource.register_custom_recognition("SubReco", recos[1])
        resource.register_custom_action("MainAction", actions[0])
        resource.register_custom_action("SubAction", actions[1])
        resource.register_custom_action("FallbackAction", actions[2])

        resource.post_bundle(str(tmp / "resource")).wait()
        if not resource.loaded:
            log("FAIL: resource not loaded")
            return 1

        log(f"  Loaded nodes: {sorted(resource.node_list)}")

        controller = DbgController(str(screenshot_dir))
        controller.post_connection().wait()

        tasker = Tasker()
        tasker.bind(resource, controller)
        if not tasker.inited:
            log("FAIL: tasker not inited")
            return 1

        Control.tasker = tasker  # 给 reco 用于"够了就 stop"

        log("\n--- Running MainEntry ---\n")
        job = tasker.post_task("MainEntry", {})
        job.wait()  # 任务运行直到 MainReco 触发 post_stop 后自然结束
        # 不再人为 post_stop，避免与 in-flight callback race
        time.sleep(0.2)  # 给清理时间

        Counters.flush()

        log("\n--- Done. Counters ---")
        for k, v in Counters.dump().items():
            log(f"  {k:20s} = {v}")

        # 验证
        problems = []
        if Counters.main_reco < Control.target_main_hits + Control.target_misses:
            problems.append(f"main_reco={Counters.main_reco} < expected {Control.target_main_hits + Control.target_misses}")
        if Counters.main_action < Control.target_main_hits:
            problems.append(f"main_action={Counters.main_action} < expected {Control.target_main_hits}")
        if Counters.sub_reco < Control.target_main_hits:
            problems.append(f"sub_reco={Counters.sub_reco} < expected {Control.target_main_hits} (sub_pipeline 没递归进去)")
        if Counters.sub_action < Control.target_main_hits:
            problems.append(f"sub_action={Counters.sub_action} < expected {Control.target_main_hits}")
        if Counters.fallback < 1:
            problems.append(f"fallback={Counters.fallback} < 1 ([Fallback] 没触发)")

        if problems:
            log("\n✗ FAIL:")
            for p in problems:
                log(f"  - {p}")
            return 1

        log("\n✓ PASS: loop_scan + sub_pipeline + [Fallback] + FQN namespace all verified")
        return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception as e:
        log(f"EXCEPTION: {e!r}")
        import traceback
        log(traceback.format_exc())
    finally:
        sys.exit(rc)
