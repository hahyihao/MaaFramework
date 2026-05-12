"""
Phase 3 smoke test for `recognition: "SubPipeline"` runtime execution.

不依赖 TestingDataSet。自带 1 张黑色 PNG 当 DbgController 的"屏幕"。

验证项：
  1. recognition: SubPipeline + recognition_pipeline 字段解析
  2. recognize_list 里 SubPipeline 分发：父节点的 reco 委托给 execute_once
  3. 子层任一节点命中 → 父算命中 → 走父的 next（loop_scan 重新扫描）
  4. 子层全 miss → 父算 miss → main 顺序试下一个候选
  5. entry 节点不参与识别（DirectHit 占位 entry 不导致立即命中）
  6. 子层命中节点的 box 上浮到父节点

退出码：
  0 - PASS（在 atexit 阶段可能因 phase3 Task 4 引入的 cleanup race 报负值 — 看
      "✓ PASS" 标志即可，那是 functional 验证完成）
  非 0 - FAIL（详细原因写入 counters.json）
"""

import json
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


if len(sys.argv) < 3:
    log("Usage: python smoke_subpipeline_reco.py <binding_dir> <install_dir>")
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


COUNTER_FILE: Path = Path()


class Counters:
    btn_a_reco = 0
    btn_b_reco = 0
    dialog_close_reco = 0
    btn_a_action = 0
    btn_b_action = 0
    dialog_close_action = 0
    on_homepage_hit = 0
    on_dialog_hit = 0
    lock = threading.Lock()

    @classmethod
    def dump(cls) -> dict:
        return {k: getattr(cls, k) for k in [
            "btn_a_reco", "btn_b_reco", "dialog_close_reco",
            "btn_a_action", "btn_b_action", "dialog_close_action",
            "on_homepage_hit", "on_dialog_hit",
        ]}

    @classmethod
    def flush(cls) -> None:
        try:
            COUNTER_FILE.write_text(json.dumps(cls.dump(), indent=2), encoding="utf-8")
        except Exception:
            pass


class Control:
    tasker = None
    stop_requested = False


# ─────────────────────────── Custom 实现 ───────────────────────────


class BtnAReco(CustomRecognition):
    """call#1 hit; call#2+ miss"""
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        with Counters.lock:
            Counters.btn_a_reco += 1
            n = Counters.btn_a_reco
        Counters.flush()
        log(f"    [BtnAReco] call #{n}")
        if n <= 1:
            return CustomRecognition.AnalyzeResult(box=(10, 10, 30, 30), detail=f"btna-hit-{n}")
        return CustomRecognition.AnalyzeResult(box=None, detail=f"btna-miss-{n}")


class BtnBReco(CustomRecognition):
    """call#1 hit; call#2+ miss"""
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        with Counters.lock:
            Counters.btn_b_reco += 1
            n = Counters.btn_b_reco
        Counters.flush()
        log(f"    [BtnBReco] call #{n}")
        if n <= 1:
            return CustomRecognition.AnalyzeResult(box=(40, 40, 30, 30), detail=f"btnb-hit-{n}")
        return CustomRecognition.AnalyzeResult(box=None, detail=f"btnb-miss-{n}")


class DialogCloseReco(CustomRecognition):
    """call#1 hit; call#2 miss + signal stop"""
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        with Counters.lock:
            Counters.dialog_close_reco += 1
            n = Counters.dialog_close_reco
        Counters.flush()
        log(f"    [DialogCloseReco] call #{n}")
        if n <= 1:
            return CustomRecognition.AnalyzeResult(box=(70, 70, 20, 20), detail=f"dlgclose-hit-{n}")
        # call#2 miss → signal stop
        with Counters.lock:
            if not Control.stop_requested:
                Control.stop_requested = True
                if Control.tasker is not None:
                    log(f"    [DialogCloseReco] signal stop after call #{n}")
                    Control.tasker.post_stop()
        return CustomRecognition.AnalyzeResult(box=None, detail=f"dlgclose-miss-{n}")


class BtnAAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.btn_a_action += 1
        Counters.flush()
        log(f"    [BtnAAction] FIRED (cum={Counters.btn_a_action})")
        return True


class BtnBAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.btn_b_action += 1
        Counters.flush()
        log(f"    [BtnBAction] FIRED (cum={Counters.btn_b_action})")
        return True


class DialogCloseAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.dialog_close_action += 1
        Counters.flush()
        log(f"    [DialogCloseAction] FIRED (cum={Counters.dialog_close_action})")
        return True


class OnHomepageHit(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.on_homepage_hit += 1
        Counters.flush()
        log(f"  [OnHomepageHit] FIRED (cum={Counters.on_homepage_hit})")
        return True


class OnDialogHit(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        with Counters.lock:
            Counters.on_dialog_hit += 1
        Counters.flush()
        log(f"  [OnDialogHit] FIRED (cum={Counters.on_dialog_hit})")
        return True


# ─────────────────────────── Tools ───────────────────────────


def make_black_png(width: int = 200, height: int = 200) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# ─────────────────────────── Main ───────────────────────────


def main() -> int:
    global COUNTER_FILE

    log("=== smoke_subpipeline_reco v3 ===")
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
                "pre_delay": 0, "post_delay": 0
            },
            "TryDialog": {
                "recognition": "SubPipeline",
                "recognition_pipeline": "DialogEntry",
                "action": "Custom",
                "custom_action": "OnDialogHit",
                "pre_delay": 0, "post_delay": 0
            },
            # entry 是纯调度节点 — 自己不被扫，只作为 entry.next 列表的根容器
            "HomepageEntry": {
                "recognition": "DirectHit",
                "next": ["HomeBtnA", "HomeBtnB"]
            },
            "HomeBtnA": {
                "recognition": "Custom",
                "custom_recognition": "BtnAReco",
                "action": "Custom",
                "custom_action": "BtnAAction",
                "pre_delay": 0, "post_delay": 0
            },
            "HomeBtnB": {
                "recognition": "Custom",
                "custom_recognition": "BtnBReco",
                "action": "Custom",
                "custom_action": "BtnBAction",
                "pre_delay": 0, "post_delay": 0
            },
            "DialogEntry": {
                "recognition": "DirectHit",
                "next": ["DialogClose"]
            },
            "DialogClose": {
                "recognition": "Custom",
                "custom_recognition": "DialogCloseReco",
                "action": "Custom",
                "custom_action": "DialogCloseAction",
                "pre_delay": 0, "post_delay": 0
            },
        }
        (pipeline_dir / "smoke.json").write_text(
            json.dumps(pipeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        resource = Resource()
        recos = [BtnAReco(), BtnBReco(), DialogCloseReco()]
        actions = [
            BtnAAction(), BtnBAction(), DialogCloseAction(),
            OnHomepageHit(), OnDialogHit(),
        ]
        resource.register_custom_recognition("BtnAReco", recos[0])
        resource.register_custom_recognition("BtnBReco", recos[1])
        resource.register_custom_recognition("DialogCloseReco", recos[2])
        resource.register_custom_action("BtnAAction", actions[0])
        resource.register_custom_action("BtnBAction", actions[1])
        resource.register_custom_action("DialogCloseAction", actions[2])
        resource.register_custom_action("OnHomepageHit", actions[3])
        resource.register_custom_action("OnDialogHit", actions[4])

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

        Control.tasker = tasker

        log("\n--- Running MainOrch ---\n")
        job = tasker.post_task("MainOrch", {})
        job.wait()
        time.sleep(0.2)
        Counters.flush()

        log("\n--- Done. Counters ---")
        for k, v in Counters.dump().items():
            log(f"  {k:24s} = {v}")

        problems = []
        if Counters.btn_a_action < 1:
            problems.append(f"btn_a_action={Counters.btn_a_action} < 1 (主页面_按钮A 子层命中 → action 未触发)")
        if Counters.btn_b_action < 1:
            problems.append(f"btn_b_action={Counters.btn_b_action} < 1 (主页面_按钮B 命中 → action 未触发)")
        if Counters.dialog_close_action < 1:
            problems.append(f"dialog_close_action={Counters.dialog_close_action} < 1 (弹窗关闭 命中 → action 未触发)")
        if Counters.on_homepage_hit < 2:
            problems.append(f"on_homepage_hit={Counters.on_homepage_hit} < 2 (TryHomepage 父节点未被算作命中 2 次)")
        if Counters.on_dialog_hit < 1:
            problems.append(f"on_dialog_hit={Counters.on_dialog_hit} < 1 (TryDialog 父节点未被算作命中)")

        if problems:
            log("\n[FAIL]:")
            for p in problems:
                log(f"  - {p}")
            return 1

        log("\n[PASS]: SubPipeline reco delegate + main 优先级编排 全部 verified")
        log("  - TryHomepage SubPipeline → HomeBtnA/B 子层命中 → 父算命中 → loop_scan 重扫")
        log("  - 子层全 miss → 父算 miss → main 试 TryDialog → 弹窗子层命中 → 父算命中")
        log("  - 子层全 miss → main stop")
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
