#!/bin/bash 

cd /comfyui
git init
git branch -m main
git remote add origin https://github.com/comfyanonymous/ComfyUI.git
git fetch origin
git checkout -b master origin/master -f

pyenv install 3.10.15
pyenv virtualenv 3.10.15 comfyui
source /root/.pyenv/versions/comfyui/bin/activate && /root/.pyenv/versions/comfyui/bin/pip install -r requirements.txt && /root/.pyenv/versions/comfyui/bin/pip install torch torchvision torchaudio