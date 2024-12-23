#!/bin/bash 

# You will only need to run this script one time to install the conda environment
cd /root && wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
chmod +x Miniconda3-latest-Linux-x86_64.sh
./Miniconda3-latest-Linux-x86_64.sh -b
/root/miniconda3/bin/conda create -n comfystream python=3.11 -y
/root/miniconda3/bin/conda create -n comfyui python=3.11 -y
