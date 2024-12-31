#!/bin/bash 

#Check to ensure user is in the correct environment
if grep -q "/root/.pyenv/version" <<< "$(which python)" || ! which python | grep -q "comfyui"; then
    echo "Warning: You are using an incorrect Python environment. Please ensure you have activated the correct environment with 'deactivate' and 'conda activate comfyui' before running this script."
    exit 1
fi

cd /
# remove links to models and custom_nodes before clone
rm /comfyui/models /comfyui/custom_nodes
git clone https://github.com/comfyanonymous/ComfyUI.git /comfyui
cd /comfyui
rm -rf models custom_nodes
ln -sf /models/ComfyUI--models models
ln -sf /models/ComfyUI--nodes custom_nodes
pip install --upgrade pip==23.3.2 setuptools==69.5.1 wheel==0.43.0
pip install -r requirements.txt
pip install huggingface-hub==0.25.0

chown -R $USER:$USER /comfyui/models
chown -R $USER:$USER /comfyui/custom_nodes
