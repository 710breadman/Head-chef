import json
from pathlib import Path
import socket
import tempfile
import unittest

from head_chef.comfyui import ComfyUIClient, ComfyUIError, _ComfyWebSocket
from head_chef.storage import ensure_state
from head_chef.visual import (
    create_visual_job,
    free_vram_mb,
    load_approved_workflow,
    load_visual_job,
    model_usability,
    run_visual_job,
    save_visual_job,
    validate_model_parameters,
)


class FakeComfyClient:
    def __init__(self, fail_first=False):
        self.fail_first = fail_first
        self.queues = 0
        self.cancelled = []

    def system_stats(self):
        return {"devices": [{"vram_free": 8 * 1024 * 1024 * 1024}]}

    def queue_prompt(self, workflow):
        self.queues += 1
        self.workflow = workflow
        return f"prompt-{self.queues}"

    def wait(self, prompt_id, timeout_seconds, on_progress=None):
        if self.fail_first and self.queues == 1:
            raise ComfyUIError("temporary failure")
        if on_progress:
            on_progress({"type": "progress", "data": {"value": 1, "max": 1}})
        return {
            "outputs": {
                "9": {
                    "images": [{
                        "filename": "HeadChef_00001_.png",
                        "subfolder": "",
                        "type": "output",
                    }],
                },
            },
        }

    def cancel(self, prompt_id):
        self.cancelled.append(prompt_id)

    def download_output(self, filename, subfolder="", kind="output"):
        return b"\x89PNG\r\n\x1a\nbounded-fixture"


class ComfyUITests(unittest.TestCase):
    def test_client_rejects_non_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            ComfyUIClient("http://192.168.1.10:8188")

    def test_model_inventory_reads_every_comfyui_category(self):
        client = ComfyUIClient()
        responses = {
            "/models": ["loras", "checkpoints", "loras"],
            "/models/checkpoints": ["z.safetensors", "a.safetensors"],
            "/models/loras": ["style.safetensors"],
        }
        client._value = lambda method, path, payload=None, timeout_seconds=None: responses[path]
        self.assertEqual(client.model_inventory(), {
            "checkpoints": ["a.safetensors", "z.safetensors"],
            "loras": ["style.safetensors"],
        })

    def test_model_inventory_rejects_unsafe_category(self):
        client = ComfyUIClient()
        with self.assertRaisesRegex(ComfyUIError, "Unsafe"):
            client.models_in_folder("../models")

    def test_model_usability_maps_only_approved_workflow_inputs(self):
        usability = model_usability({
            "checkpoints": ["sd_xl_base_1.0.safetensors"],
            "diffusion_models": ["flux.safetensors"],
        })
        checkpoint = next(item for item in usability if item["category"] == "checkpoints")
        flux = next(item for item in usability if item["category"] == "diffusion_models")
        self.assertTrue(checkpoint["usable"])
        self.assertEqual(checkpoint["uses"][0]["template"], "sdxl-text-to-image")
        self.assertFalse(flux["usable"])

    def test_visual_model_parameter_must_be_installed(self):
        with self.assertRaisesRegex(ValueError, "not installed"):
            validate_model_parameters(
                "sdxl-text-to-image",
                {"checkpoint": "missing.safetensors"},
                {"checkpoints": ["installed.safetensors"]},
            )

    def test_approved_template_replaces_only_declared_parameters(self):
        workflow, spec = load_approved_workflow("sdxl-text-to-image", {
            "checkpoint": "model.safetensors",
            "prompt": "A safe room",
            "seed": 12,
        })
        self.assertEqual(workflow["4"]["inputs"]["ckpt_name"], "model.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["seed"], 12)
        self.assertEqual(spec["minimum_free_vram_mb"], 4096)
        with self.assertRaisesRegex(ValueError, "not approved"):
            load_approved_workflow("sdxl-text-to-image", {
                "checkpoint": "x", "prompt": "x", "new_node": "evil",
            })

    def test_visual_job_round_trip_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = create_visual_job(
                root,
                "sdxl-text-to-image",
                {"checkpoint": "model.safetensors", "prompt": "room"},
                ["Room is readable"],
            )
            path = save_visual_job(job, root / "jobs")
            loaded = load_visual_job(path)
            with self.assertRaises(FileExistsError):
                save_visual_job(job, root / "jobs")
        self.assertEqual(loaded.template_id, "sdxl-text-to-image")

    def test_retry_cancellation_progress_and_output_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = ensure_state(root)
            job = create_visual_job(
                root,
                "sdxl-text-to-image",
                {"checkpoint": "model.safetensors", "prompt": "room"},
                ["Room is readable"],
                max_attempts=2,
            )
            client = FakeComfyClient(fail_first=True)
            result = run_visual_job(job, state, client)
            manifest = result["manifest"]
            output = Path(manifest["outputs"][0]["path"])
            saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(client.cancelled, ["prompt-1"])
        self.assertEqual(len(manifest["attempts"]), 2)
        self.assertEqual(manifest["progress"][0]["type"], "progress")
        self.assertTrue(output.name.endswith(".png"))
        self.assertEqual(saved["coordinator_review_required"], True)
        self.assertEqual(len(saved["outputs"][0]["sha256"]), 64)

    def test_free_vram_normalizes_bytes(self):
        self.assertEqual(
            free_vram_mb({"devices": [{"vram_free": 6 * 1024 * 1024 * 1024}]}),
            6144,
        )

    def test_websocket_text_progress_frame(self):
        left, right = socket.socketpair()
        try:
            websocket = _ComfyWebSocket("http://127.0.0.1:8188", "id", 1)
            websocket.sock = left
            payload = json.dumps({"type": "progress", "data": {"value": 2}}).encode("utf-8")
            right.sendall(bytes([0x81, len(payload)]) + payload)
            event = websocket.read_event(1)
        finally:
            left.close()
            right.close()
        self.assertEqual(event["data"]["value"], 2)


if __name__ == "__main__":
    unittest.main()
