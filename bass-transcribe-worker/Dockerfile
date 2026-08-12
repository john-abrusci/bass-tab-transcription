FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

# ffmpeg is required -- Demucs shells out to it to decode anything that is not a wav.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fail here rather than on a GPU worker. The base image already ships
# torch 2.4.0+cu121; if pip decides to replace it with a PyPI wheel we silently
# gain ~2.5GB of nvidia-* packages and can end up with a CPU-only build that
# only reveals itself at inference time.
RUN python -c "import torch; \
    print('torch', torch.__version__, 'cuda', torch.version.cuda); \
    assert torch.__version__.startswith('2.4.0'), 'torch was replaced -- check the pin in requirements.txt'; \
    assert (torch.version.cuda or '').startswith('12.1'), 'CPU-only torch -- the CUDA wheel got swapped out'"

# Bake model weights into the image so cold starts do not download them.
# Phase 2: set TORCH_HOME to a network volume path instead and compare cold start.
ENV TORCH_HOME=/opt/torch
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
RUN python -c "import torch, torchcrepe; torchcrepe.load.model(torch.device('cpu'), 'full')"

COPY . .

CMD ["python", "-u", "handler.py"]
