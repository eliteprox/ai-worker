#!/bin/bash 

#Check to ensure user is in the correct environment
if grep -q "/root/.pyenv/version" <<< "$(which python)" || ! which python | grep -q "comfyui"; then
    echo "Warning: You are using an incorrect Python environment. Please ensure you have activated the correct environment with 'deactivate' and 'conda activate comfyui' before running this script."
    exit 1
fi

cd /comfyui
git init
git branch -m main
git remote add origin https://github.com/comfyanonymous/ComfyUI.git
git fetch origin
git sparse-checkout set "/*" "!models" "!inputs"
git checkout -b master origin/master -f

/root/miniconda3/envs/comfyui/bin/pip install -r requirements.txt && /root/miniconda3/envs/comfyui/bin/pip install torch torchvision torchaudio