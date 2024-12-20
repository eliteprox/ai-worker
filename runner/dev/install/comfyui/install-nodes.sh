#!/bin/bash

# Set default base paths
COMFYUI_BASE_PATH=${1:-/comfyui}
COMFYSTREAM_BASE_PATH=${2:-/comfystream}

# Copy ComfyStream custom nodes to ComfyUI
cp -r $COMFYSTREAM_BASE_PATH/nodes/tensor_utils $COMFYUI_BASE_PATH/custom_nodes

cd $COMFYUI_BASE_PATH/custom_nodes
git clone https://github.com/ltdrdata/ComfyUI-Manager.git
git clone https://github.com/ryanontheinside/ComfyUI-Misc-Effects.git
for dir in */ ; do
    cd "$dir"
    if [ -f requirements.txt ]; then
        pip install -r requirements.txt
    fi
    cd ..
done

