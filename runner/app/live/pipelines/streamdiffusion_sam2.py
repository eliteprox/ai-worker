# app/live/pipelines/streamdiffusion_sam2.py

import logging
import threading
from typing import List, Optional, Dict
import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel

from .interface import Pipeline
from .segment_anything_2 import Sam2Live
from .streamdiffusion import StreamDiffusion

logger = logging.getLogger(__name__)

class StreamDiffusionSam2Params(BaseModel):
    class Config:
        extra = "forbid"

    # SAM2 Parameters
    sam2_model_id: str = "facebook/sam2-hiera-tiny"
    point_coords: List[List[int]] = [[256, 256]]
    point_labels: List[int] = [1]
    show_point: bool = False

    # StreamDiffusion Parameters
    sd_model_id: str = "KBlueLeaf/kohaku-v2.1"
    prompt: str = "talking head, cyberpunk, ultra-realistic"
    use_lcm_lora: bool = True
    acceleration: str = "tensorrt"

    def __init__(self, **data):
        super().__init__(**data)

class StreamDiffusionSam2(Pipeline):
    def __init__(self, **params):
        super().__init__(**params)
        self.sam2_pipe: Optional[Sam2Live] = None
        self.sd_pipe: Optional[StreamDiffusion] = None
        self.first_frame = True
        self.update_params(**params)

    def update_params(self, **params):
        new_params = StreamDiffusionSam2Params(**params)
        
        # Initialize SAM2 pipeline
        sam2_params = {
            "model_id": new_params.sam2_model_id,
            "point_coords": new_params.point_coords,
            "point_labels": new_params.point_labels,
            "show_point": new_params.show_point
        }
        if not self.sam2_pipe:
            self.sam2_pipe = Sam2Live(**sam2_params)
        else:
            self.sam2_pipe.update_params(**sam2_params)

        # Initialize StreamDiffusion pipeline
        sd_params = {
            "model_id": new_params.sd_model_id,
            "prompt": new_params.prompt,
            "use_lcm_lora": new_params.use_lcm_lora,
            "acceleration": new_params.acceleration
        }
        if not self.sd_pipe:
            self.sd_pipe = StreamDiffusion(**sd_params)
        else:
            self.sd_pipe.update_params(**sd_params)

        self.params = new_params
        self.first_frame = True

    def process_frame(self, frame: Image.Image, **params) -> Image.Image:
        """Process frame through both SAM2 and StreamDiffusion"""
        try:
            # Update parameters if provided
            if params:
                self.update_params(**params)

            # First process through SAM2
            mask_frame = self.sam2_pipe.process_frame(frame)

            # Then process through StreamDiffusion
            final_frame = self.sd_pipe.process_frame(mask_frame)

            return final_frame

        except Exception as e:
            logger.error(f"Error processing frame: {str(e)}")
            return frame