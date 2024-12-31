#!/bin/bash 

#Check to ensure user is in the correct environment
if grep -q "/root/.pyenv/version" <<< "$(which python)" || ! which python | grep -q "comfystream"; then
    echo "Warning: You are using Python from /root/.pyenv. Please ensure you have activated the correct environment with 'deactivate' and 'conda activate comfystream' before running this script."
    exit 1
fi

cd /comfystream
/root/miniconda3/envs/comfystream/bin/pip install -r requirements.txt 
/root/miniconda3/envs/comfystream/bin/pip install .
/root/miniconda3/envs/comfystream/bin/pip install huggingface-hub==0.25.0
/root/miniconda3/envs/comfystream/bin/python install.py --workspace /comfyui
