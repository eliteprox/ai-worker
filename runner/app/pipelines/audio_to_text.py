from enum import Enum
import logging
import os
from typing import List

import torch
from app.pipelines.base import Pipeline
from app.pipelines.utils import get_model_dir, get_torch_device
from app.pipelines.utils.audio import AudioConverter
from fastapi import File, UploadFile
from huggingface_hub import file_download
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
from transformers.utils import is_torch_sdpa_available

from app.pipelines.utils.utils import get_model_path

logger = logging.getLogger(__name__)


MODEL_INCOMPATIBLE_EXTENSIONS = {
    "openai/whisper-large-v3": ["mp4", "m4a", "ac3"],
    "openai/whisper-medium": ["mp4", "m4a", "ac3"],
    "distil-whisper/distil-large-v3": ["mp4", "m4a", "ac3"]
}

class ModelName(Enum):
    """Enumeration mapping model names to their corresponding IDs."""

    WHISPER_LARGE_V3 = "openai/whisper-large-v3"
    WHISPER_MEDIUM = "openai/whisper-medium"
    WHISPER_DISTIL_LARGE_V3 = "distil-whisper/distil-large-v3"

    @classmethod
    def list(cls):
        """Return a list of all model IDs."""
        return list(map(lambda c: c.value, cls))

class AudioToTextPipeline(Pipeline):
    def __init__(self, model_id: str):
        self.model_id = model_id
        kwargs = {}

        torch_device = get_torch_device()
        folder_name = file_download.repo_folder_name(
            repo_id=model_id, repo_type="model"
        )
        folder_path = os.path.join(get_model_dir(), folder_name)


        MODEL_OPT_DEFAULTS = {
            ModelName.WHISPER_LARGE_V3: {
                "torch_dtype": torch.float16 if torch.cuda.is_available() else torch.float32,
                # "bach_size": "",
            },
            ModelName.WHISPER_MEDIUM: 
            {
                "torch_dtype": torch.float32,
            },
            ModelName.WHISPER_DISTIL_LARGE_V3:
            {
                "torch_dtype": torch.float16 if torch.cuda.is_available() else torch.float32,
            }
        }
        
        # Map model_id to ModelName enum
        
        model_name_enum = next(key for key, value in ModelName.__members__.items() if value.value == model_id)
        model_type = ModelName[model_name_enum]

        # Retrieve torch_dtype from MODEL_OPT_DEFAULTS
        #torch_dtype = MODEL_OPT_DEFAULTS[model_name_enum].get("torch_dtype", torch.float16)   

        kwargs["torch_dtype"] = MODEL_OPT_DEFAULTS[model_type].get("torch_dtype", torch.float16)
        # kwargs["torch_dtype"] = MODEL_OPT_DEFAULTS[ModelName.list[model_id]].get("torch_dtype", torch.float16)

        if torch_device != "cpu" and kwargs["torch_dtype"] == torch.float16:
            logger.info("AudioToText loading %s variant for fp16", model_id)
            
        elif torch_device != "cpu" and kwargs["torch_dtype"] == torch.float32:
            kwargs["variant"] = "fp32"
            logger.info("AudioToText loading %s variant for %s", kwargs["variant"], model_id)

        # if bfloat16_enabled:
        #     logger.info("AudioToTextPipeline using bfloat16 precision for %s", model_id)
        #     kwargs["torch_dtype"] = torch.bfloat16

        if is_torch_sdpa_available() and ModelName.WHISPER_DISTIL_LARGE_V3 == model_type:
            kwargs["attn_implementation"]="sdpa"

        # kwargs["use_cuda"] = torch_device != "cpu"
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_id,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            cache_dir=get_model_dir(),
            **kwargs,
        ).to(torch_device)

        processor = AutoProcessor.from_pretrained(model_id, cache_dir=get_model_dir())

        self.ldm = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            max_new_tokens=128,
            chunk_length_s=30,
            batch_size=16,
            return_timestamps=True,
            **kwargs,
        )

    def __call__(self, audio: UploadFile, **kwargs) -> List[File]:
        # Convert M4A/MP4 files for pipeline compatibility.
        if (
            os.path.splitext(audio.filename)[1].lower().lstrip(".")
            in MODEL_INCOMPATIBLE_EXTENSIONS[self.model_id]
        ):
            audio_converter = AudioConverter()
            converted_bytes = audio_converter.convert(audio, "mp3")
            audio_converter.write_bytes_to_file(converted_bytes, audio)
        
        return self.ldm(audio.file.read(), **kwargs)

    def __str__(self) -> str:
        return f"AudioToTextPipeline model_id={self.model_id}"
