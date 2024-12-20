When developing locally, you can use the existing `livepeer/live-app-comfyui` image and mapped nodes as a base to avoid complexity and increase initial setup speed

### Install ComfyUI
This will download ComfyUI to /comfyui, preserving the existing models and custom_nodes folders, integrating your custom_nodes. 
Creates a new python environment `comfyui` to separate from existing comfystream installation
```
cd install/comfyui
./install-comfyui.sh
``

### Install all custom nodes from the ai-runner into your development instance of ComfyUI
Installs custom nodes into the 
```
source /root/.pyenv/versions/comfyui/bin/activate
./install-nodes.sh
``

### Run ComfyUI Dev
```
cd /comfyui
python main.py --listen
```

Make sure to set the appPort in devcontainer.json if needed to expose ports 