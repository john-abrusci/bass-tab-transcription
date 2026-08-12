FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

# ffmpeg is required -- Demucs shells out to it to decode anything that is not a wav.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake model weights into the image so cold starts do not download them.
# Phase 2: set TORCH_HOME to a network volume path instead and compare cold start.
ENV TORCH_HOME=/opt/torch
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
RUN python -c "import torch, torchcrepe; torchcrepe.load.model(torch.device('cpu'), 'full')"

COPY . .

CMD ["python", "-u", "handler.py"]
