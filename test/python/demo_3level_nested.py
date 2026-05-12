"""
3 层嵌套场景演示（不需要真实游戏 / 真实截图）：

Level 1 (main):
  - 不识别，纯任务优先级编排
  - 通过 SubPipeline reco 委托识别到 Level 2

Level 2 (找图找色层):
  - 子层入口（调度）+ 探针节点列表
  - 探针命中后通过 sub_pipeline 字段进入 Level 3

Level 3 (逻辑判定层):
  - 命中后跑的子例程：等待状态、确认、清理等
  - 用 Custom reco/action 模拟"业务逻辑"

跑出来的 log 完整展示三层进入和返回的全过程。
"""

import io, json, os, struct, sys, tempfile, threading, time, zlib
from pathlib import Path

def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()

if len(sys.argv) < 3:
    log("Usage: python demo_3level_nested.py <binding_dir> <install_dir>")
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


class Counters:
    # L2 探针层
    l2_homea_reco = 0; l2_homea_action = 0
    l2_homeb_reco = 0; l2_homeb_action = 0
    # L3 逻辑层
    l3_wait_reco = 0;  l3_wait_action = 0
    l3_confirm_reco = 0; l3_confirm_action = 0
    lock = threading.Lock()


class Control:
    tasker = None
    stop_requested = False


# ─────────────────────────── L2 探针（找图找色层）────────────────────────
class HomeBtnAReco(CustomRecognition):
    """call#1 hit, 之后 miss"""
    def analyze(self, context, argv):
        with Counters.lock:
            Counters.l2_homea_reco += 1
            n = Counters.l2_homea_reco
        log(f"      [L2 HomeBtnAReco] call #{n}")
        if n <= 1:
            return CustomRecognition.AnalyzeResult(box=(10, 10, 30, 30), detail=f"l2-a-hit-{n}")
        return CustomRecognition.AnalyzeResult(box=None, detail=f"l2-a-miss-{n}")

class HomeBtnBReco(CustomRecognition):
    def analyze(self, context, argv):
        with Counters.lock:
            Counters.l2_homeb_reco += 1
            n = Counters.l2_homeb_reco
        log(f"      [L2 HomeBtnBReco] call #{n}")
        if n <= 1:
            return CustomRecognition.AnalyzeResult(box=(40, 40, 30, 30), detail=f"l2-b-hit-{n}")
        # call#2 miss → stop
        with Counters.lock:
            if not Control.stop_requested:
                Control.stop_requested = True
                if Control.tasker is not None:
                    log(f"      [L2 HomeBtnBReco] signal stop")
                    Control.tasker.post_stop()
        return CustomRecognition.AnalyzeResult(box=None, detail=f"l2-b-miss-{n}")

class HomeBtnAAction(CustomAction):
    def run(self, context, argv):
        with Counters.lock:
            Counters.l2_homea_action += 1
        log(f"      [L2 HomeBtnAAction] FIRED (cum={Counters.l2_homea_action})")
        return True

class HomeBtnBAction(CustomAction):
    def run(self, context, argv):
        with Counters.lock:
            Counters.l2_homeb_action += 1
        log(f"      [L2 HomeBtnBAction] FIRED (cum={Counters.l2_homeb_action})")
        return True


# ─────────────────────────── L3 逻辑判定层 ──────────────────────────────
class WaitForLoadReco(CustomRecognition):
    """L3: 业务逻辑 — 等页面加载（每次都 hit 模拟"加载完成"）"""
    def analyze(self, context, argv):
        with Counters.lock:
            Counters.l3_wait_reco += 1
            n = Counters.l3_wait_reco
        log(f"        [L3 WaitForLoadReco] call #{n} → 模拟页面已加载")
        return CustomRecognition.AnalyzeResult(box=(60, 60, 20, 20), detail=f"l3-wait-{n}")

class WaitForLoadAction(CustomAction):
    def run(self, context, argv):
        with Counters.lock:
            Counters.l3_wait_action += 1
        log(f"        [L3 WaitForLoadAction] FIRED (cum={Counters.l3_wait_action})")
        return True

class ConfirmReco(CustomRecognition):
    """L3: 业务逻辑 — 确认按钮检测"""
    def analyze(self, context, argv):
        with Counters.lock:
            Counters.l3_confirm_reco += 1
            n = Counters.l3_confirm_reco
        log(f"        [L3 ConfirmReco] call #{n} → 模拟确认按钮已找到")
        return CustomRecognition.AnalyzeResult(box=(80, 80, 15, 15), detail=f"l3-confirm-{n}")

class ConfirmAction(CustomAction):
    def run(self, context, argv):
        with Counters.lock:
            Counters.l3_confirm_action += 1
        log(f"        [L3 ConfirmAction] FIRED (cum={Counters.l3_confirm_action})")
        return True


def make_black_png(width=200, height=200):
    sig = b"\x89PNG\r\n\x1a\n"
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def main() -> int:
    log("=" * 60)
    log("=== 3 层嵌套场景 demo ===")
    log("Level 1 main (任务编排) → SubPipeline reco")
    log("    └→ Level 2 子层 (找图找色) → sub_pipeline 字段")
    log("           └→ Level 3 (逻辑判定流程)")
    log("=" * 60)
    log(f"MaaFw Version: {Library.version()}")
    Toolkit.init_option(install_dir / "bin")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        screenshot_dir = tmp / "Screenshot"
        screenshot_dir.mkdir()
        (screenshot_dir / "frame.png").write_bytes(make_black_png(200, 200))

        pipeline_dir = tmp / "resource" / "pipeline"
        pipeline_dir.mkdir(parents=True)
        pipeline = {
            # ─── Level 1: main = 任务优先级编排（不识别）─────────────
            "Main": {
                "task_mode": "loop_scan",
                "cycle_delay": 80,
                "recognition": "DirectHit",
                "next": ["TryHomepage"]
            },
            # 这个节点把 reco 委托给 Level 2
            "TryHomepage": {
                "recognition": "SubPipeline",
                "recognition_pipeline": "L2_HomeProbes",
                "action": "DoNothing",
                "pre_delay": 0, "post_delay": 0,
            },

            # ─── Level 2: 子层 = 找图找色 ────────────────────────────
            "L2_HomeProbes": {
                "recognition": "DirectHit",  # entry 不参与识别，只是调度根
                "next": ["HomeBtnA", "HomeBtnB"]
            },
            "HomeBtnA": {
                "recognition": "Custom",
                "custom_recognition": "HomeBtnAReco",
                "action": "Custom",
                "custom_action": "HomeBtnAAction",
                "sub_pipeline": "L3_HomeAFlow",      # ← 命中后进 L3
                "pre_delay": 0, "post_delay": 0,
            },
            "HomeBtnB": {
                "recognition": "Custom",
                "custom_recognition": "HomeBtnBReco",
                "action": "Custom",
                "custom_action": "HomeBtnBAction",
                "sub_pipeline": "L3_HomeBFlow",
                "pre_delay": 0, "post_delay": 0,
            },

            # ─── Level 3: 命中后的逻辑判定流程 ──────────────────────
            "L3_HomeAFlow": {
                "recognition": "DirectHit",  # L3 入口，不识别
                "next": ["WaitForLoad", "ConfirmReady"]
            },
            "L3_HomeBFlow": {
                "recognition": "DirectHit",
                "next": ["WaitForLoad"]
            },
            "WaitForLoad": {
                "recognition": "Custom",
                "custom_recognition": "WaitForLoadReco",
                "action": "Custom",
                "custom_action": "WaitForLoadAction",
                "pre_delay": 0, "post_delay": 0,
            },
            "ConfirmReady": {
                "recognition": "Custom",
                "custom_recognition": "ConfirmReco",
                "action": "Custom",
                "custom_action": "ConfirmAction",
                "pre_delay": 0, "post_delay": 0,
            },
        }
        (pipeline_dir / "demo.json").write_text(
            json.dumps(pipeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        resource = Resource()
        recos = [HomeBtnAReco(), HomeBtnBReco(), WaitForLoadReco(), ConfirmReco()]
        actions = [HomeBtnAAction(), HomeBtnBAction(), WaitForLoadAction(), ConfirmAction()]
        resource.register_custom_recognition("HomeBtnAReco", recos[0])
        resource.register_custom_recognition("HomeBtnBReco", recos[1])
        resource.register_custom_recognition("WaitForLoadReco", recos[2])
        resource.register_custom_recognition("ConfirmReco", recos[3])
        resource.register_custom_action("HomeBtnAAction", actions[0])
        resource.register_custom_action("HomeBtnBAction", actions[1])
        resource.register_custom_action("WaitForLoadAction", actions[2])
        resource.register_custom_action("ConfirmAction", actions[3])

        resource.post_bundle(str(tmp / "resource")).wait()
        if not resource.loaded:
            log("FAIL: resource not loaded")
            return 1

        log(f"\nLoaded nodes: {sorted(resource.node_list)}\n")

        controller = DbgController(str(screenshot_dir))
        controller.post_connection().wait()

        tasker = Tasker()
        tasker.bind(resource, controller)
        Control.tasker = tasker

        log("─" * 60)
        log("=== START ===")
        log("─" * 60)
        job = tasker.post_task("Main", {})
        job.wait()
        time.sleep(0.2)

        log("\n" + "═" * 60)
        log("=== 最终 Counters ===")
        log("═" * 60)
        log(f"  L2 HomeBtnAReco  调用 = {Counters.l2_homea_reco}")
        log(f"  L2 HomeBtnAAction 触发 = {Counters.l2_homea_action}")
        log(f"  L2 HomeBtnBReco  调用 = {Counters.l2_homeb_reco}")
        log(f"  L2 HomeBtnBAction 触发 = {Counters.l2_homeb_action}")
        log(f"  L3 WaitForLoadReco   调用 = {Counters.l3_wait_reco}")
        log(f"  L3 WaitForLoadAction 触发 = {Counters.l3_wait_action}")
        log(f"  L3 ConfirmReco       调用 = {Counters.l3_confirm_reco}")
        log(f"  L3 ConfirmAction     触发 = {Counters.l3_confirm_action}")
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
