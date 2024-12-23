#!/bin/bash 

#TODO: Check to ensure user is in the correct environment (comfyui)

cd /comfyui
git init
git branch -m main
git remote add origin https://github.com/comfyanonymous/ComfyUI.git
git fetch origin
git sparse-checkout set "/*" "!models" "!inputs"
git checkout -b master origin/master -f

/root/miniconda3/envs/comfyui/bin/pip install -r requirements.txt && /root/miniconda3/envs/comfyui/bin/pip install torch torchvision torchaudio