import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from app.live.config import VENV_MAPPING
from app.pipelines.base import Pipeline
from app.pipelines.utils import get_model_dir, get_torch_device
from app.utils.errors import InferenceError

logger = logging.getLogger(__name__)

class LiveVideoToVideoPipeline(Pipeline):
    def __init__(self, model_id: str):
        """Initialize with single or multiple model IDs"""
        self.model_ids = model_id.split(",") if "," in model_id else [model_id]
        self.model_dir = get_model_dir()
        self.torch_device = get_torch_device()
        self.infer_script_path = Path(__file__).parent.parent / "live" / "infer.py"
        self.process_manager = ProcessManager(self.model_ids, self.infer_script_path)

    def __call__(self, **kwargs):
        try:
            # Start processes for each model if not running
            for model_id in self.model_ids:
                self.process_manager.start_process(
                    model_id,
                    http_port=8888 + self.model_ids.index(model_id),  # Increment port number for each model process
                    subscribe_url=kwargs["subscribe_url"],
                    publish_url=kwargs["publish_url"],
                    initial_params=json.dumps(kwargs["params"]),
                    # model_dir=self.model_dir
                )
            
            logger.info(
                f"Starting stream with models {self.model_ids}, "
                f"subscribe={kwargs['subscribe_url']} publish={kwargs['publish_url']}"
            )
            return
        except Exception as e:
            raise InferenceError(original_exception=e)

    def __del__(self):
        if hasattr(self, 'process_manager'):
            self.process_manager.stop_all()

def log_output(f):
    for line in f:
        sys.stderr.write(line)


class ProcessManager:
    """Manages multiple pipeline processes"""

    def __init__(self, model_ids: List[str], infer_script_path: Path):
        self.model_ids = model_ids
        self.infer_script_path = infer_script_path
        self.processes: Dict[str, subprocess.Popen] = {}
        self.monitor_threads: Dict[str, threading.Thread] = {}
        self.log_threads: Dict[str, threading.Thread] = {}

    def start_process(self, model_id: str, **kwargs):
        venv_path = VENV_MAPPING.get(model_id)
        if not venv_path:
            raise ValueError(f"No virtual environment configured for {model_id}")

        # Build activate command based on shell type
        activate_cmd = f"source {venv_path}/bin/activate"
        python_path = f"{venv_path}/bin/python"

        # Construct command with venv activation
        cmd = [
            "/bin/bash", 
            "-c",
            f"{activate_cmd} && exec {python_path} {self.infer_script_path}"
        ]

        # Add arguments
        cmd_args = []
        kwargs["pipeline"] = model_id
        for key, value in kwargs.items():
            kebab_key = key.replace("_", "-")
            if isinstance(value, str):
                escaped_value = str(value).replace("'", "'\\''")
                cmd_args.extend([f"--{kebab_key}", f"{escaped_value}"])
            else:
                cmd_args.extend([f"--{kebab_key}", f"{value}"])

        cmd[2] += " " + " ".join(str(arg) for arg in cmd_args)

        # Set up environment
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = venv_path
        env["PATH"] = f"{venv_path}/bin:{env['PATH']}"
        env["PYTHONHOME"] = ""
        env["HUGGINGFACE_HUB_CACHE"] = kwargs.get("model_dir", "/models")

        try:
            process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT,
                text=True, 
                env=env,
                shell=False
            )
            self.processes[model_id] = process
            
            monitor_thread = threading.Thread(
                target=self.monitor_process, 
                args=(model_id,)
            )
            monitor_thread.start()
            self.monitor_threads[model_id] = monitor_thread

            log_thread = threading.Thread(
                target=log_output, 
                args=(process.stdout,)
            )
            log_thread.start() 
            self.log_threads[model_id] = log_thread

        except subprocess.CalledProcessError as e:
            raise InferenceError(f"Error starting infer.py for {model_id}: {e}")

    def monitor_process(self, model_id: str):
        process = self.processes.get(model_id)
        while process:
            return_code = process.poll()
            if return_code is not None:
                logger.info(f"Process {model_id} completed. Return code: {return_code}")
                if return_code != 0:
                    _, stderr = process.communicate()
                    logger.error(
                        f"Process {model_id} failed with code {return_code}. Error: {stderr}"
                    )
                break
            logger.info(f"Process {model_id} is running...")
            time.sleep(10)

    def stop_all(self):
        for model_id in self.processes:
            self.stop_process(model_id)

    def stop_process(self, model_id: str):
        if model_id in self.processes:
            self.processes[model_id].terminate()
            self.monitor_threads[model_id].join()
            self.log_threads[model_id].join()
            del self.processes[model_id]
            del self.monitor_threads[model_id]
            del self.log_threads[model_id]

