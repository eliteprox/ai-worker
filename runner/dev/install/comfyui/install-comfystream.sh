#!/bin/bash 

#TODO: Check to ensure user is in the correct environment (comfystream)

cd /comfystream
/root/miniconda3/envs/comfystream/bin/pip install -r requirements.txt 
/root/miniconda3/envs/comfystream/bin/pip install .
/root/miniconda3/envs/comfystream/bin/python install.py --workspace /comfyui
