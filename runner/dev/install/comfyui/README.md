When developing locally, you can use the existing `livepeer/live-app-comfyui` image and mapped nodes as a base to avoid complexity and increase initial setup speed

### Patch devcontainer.json and start Docker Dev container
1. On the host system, from the `runner` directory:
```
cd .. && git apply ./runner/dev/patches/comfyui-dev.patch
```
2. Create directory for conda environment:
```
mkdir $HOME/miniconda3
```
3. Verify host path to `models` folder is correct in .devcontainer/devcontainer.json
4. Re-open Folder as Dev Container in VS Code
    - Use the `File` menu to select `Open Folder...` and navigate to the `runner` folder.
    - Once the folder is open, press `F1` to open the command palette.
    - Type `Dev Containers: Reopen in Container` and select it from the list. 
    - Wait for the container to build and start.

This will open the `runner` folder inside the Dev Container, allowing you to develop within the containerized environment.

### Install Conda Environment
Within the running container:
```
deactivate
cd dev/install/comfui
./install-conda.sh
```
This script will install miniconda to the mapped host volume `$HOME/miniconda3:/root/miniconda3` and also create two conda environments `comfystream` and `comfyui` which will persist container rebuilds

### Install ComfyUI
This will download ComfyUI to /comfyui, preserving the existing models and custom_nodes folders, integrating your custom_nodes. 
Creates a new python environment `comfyui` to separate from existing comfystream installation.

```
conda activate comfyui
cd dev/install/comfui
./install-comfyui.sh
```

### Install nodes into ComfyUI
```
./install-comfyui-nodes.sh
```

### Install nodes into ComfyStream
Configure environment:
```
conda activate comfystream
```

Install custom nodes into ComfyStream:
```
cd /comfystream  && python install.py --workspace ../comfyui
```

### Run ComfyUI
Start a new terminal in the devcontainer, and run:
```
deactivate
conda activate comfyui
cd /comfyui && python main.py --listen
```

### Download models and build tensorrt
1. From the **host** system, navigate to the parent directory of the `models` folder mount
2. Run the following command:
```
curl -s https://raw.githubusercontent.com/livepeer/ai-worker/main/runner/dl_checkpoints.sh | bash -s -- --tensorrt
```

### Run ComfyStream
Start a new terminal in the devcontainer, and run:
```
deactivate
conda activate comfystream
cd /comfystream && python server/app.py --workspace /comfyui --media-ports=5678 --host=0.0.0.0
```

### Troubleshooting
Check the `runner/.devcontainer/devcontainer.json` file to ensure the `appPort` and `mounts` are correct for your host environment
