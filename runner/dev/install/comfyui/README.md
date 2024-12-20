When developing locally, you can use the existing `livepeer/live-app-comfyui` image and mapped nodes as a base to avoid complexity and increase initial setup speed

### Patch devcontainer.json and start Docker Dev container
1. From the `runner` directory, run:
```
cd .. && git apply ./runner/dev/patches/comfyui-dev.patch
```
2. Verify host path to `models` folder is correct in .devcontainer/devcontainer.json
3. Re-open Folder as Dev Container in VS Code
    - Use the `File` menu to select `Open Folder...` and navigate to the `runner` folder.
    - Once the folder is open, press `F1` to open the command palette.
    - Type `Dev Containers: Reopen in Container` and select it from the list. 
    - Wait for the container to build and start.

This will open the `runner` folder inside the Dev Container, allowing you to develop within the containerized environment.

### Install ComfyUI
This will download ComfyUI to /comfyui, preserving the existing models and custom_nodes folders, integrating your custom_nodes. 
Creates a new python environment `comfyui` to separate from existing comfystream installation.

Within the container, run:
```
./install-comfyui.sh
``

### Install all custom nodes from the ai-runner into your development instance of ComfyUI
Installs custom nodes into the comfyui instance
```
source /root/.pyenv/versions/comfyui/bin/activate
./install-nodes.sh
``

### Run ComfyUI Dev
```
cd /comfyui
python main.py --listen
```

### Run ComfyStream Dev
```
pyenv deactivate
cd /comfystream
python server/app.py --workspace /comfyui --media-ports=5678 --host=0.0.0.0
```


Make sure to set the appPort in devcontainer.json if needed to expose ports 