When developing locally, you can use the existing `livepeer/live-app-comfyui` image and mapped nodes as a base to avoid complexity and increase initial setup speed
## Devcontainer configuration

1. Patch devcontainer.json for comfyui-dev
```
cd /workspaces/ai-worker && git apply ./runner/dev/patches/comfyui-dev.patch
```
2. Create directory for conda environment:
```
mkdir $HOME/miniconda
```

You will also need these directories
```
mkdir /models/ComfyUI-nodes
mkdir /models/ComfyUI-models
```

3. Verify host path to `models` folder is correct in `.devcontainer/devcontainer.json`
4. Re-open the `runner` folder in VS Code as a devcontainer:
    - Use the `File` menu to select `Open Folder...` and navigate to the `runner` folder.
    - Press `F1` to open the command palette, type `Dev Containers: Reopen in Container` and select it from the list. 
    - Wait for the container to build and start.

### Install Conda Environment
Within the running container, from the `runner` directory:
```
deactivate
/workspaces/ai-worker/runner/dev/install/comfyui/install-conda.sh
```
This script will install miniconda to the mapped host volume `$HOME/miniconda3:/root/miniconda3` and also create two conda environments `comfystream` and `comfyui` which will persist container rebuilds.
- **Imporant**:  Open a new terminal for the conda installation to take full effect

## Installation
### Install ComfyUI
Create a new conda environment `comfyui` to separate from existing comfystream installation.
```
deactivate
eval "$(/root/miniconda3/bin/conda shell.bash hook)"
conda activate comfyui
/workspaces/ai-worker/runner/dev/install/comfyui/install-comfyui.sh
```

### Install custom nodes into ComfyUI
```
/workspaces/ai-worker/runner/install-comfyui-nodes.sh
```

### Run ComfyUI
Start a new terminal in the devcontainer, and run:
```
deactivate
eval "$(/root/miniconda3/bin/conda shell.bash hook)"
conda activate comfyui
cd /comfyui && python main.py --listen
```

### Install nodes into ComfyStream
Configure environment:
```
conda activate comfystream
```

### Install custom nodes into ComfyStream:
```
cd /comfystream  && python install.py --workspace ../comfyui
```

### Download models and build tensorrt
1. From the **host** system, navigate to the parent directory of the `models` folder mount:
```
cd ~/.lpData
pyenv local 3.11
pip install -U "huggingface_hub[cli,hf_transfer]"
```
2. Run the following command:
```
curl -s https://raw.githubusercontent.com/livepeer/ai-worker/main/runner/dl_checkpoints.sh | bash -s -- --tensorrt
```

## Running ComfyUI and ComfyStream

### Run ComfyStream
Start a new terminal in the devcontainer, and run:
```
deactivate
eval "$(/root/miniconda3/bin/conda shell.bash hook)"
conda activate comfystream
cd /comfystream && python server/app.py --workspace /comfyui --media-ports=5678 --host=0.0.0.0
```

## Troubleshooting
Check the `runner/.devcontainer/devcontainer.json` file to ensure the `appPort` and `mounts` are correct for your host environment
