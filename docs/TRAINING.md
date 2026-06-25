# Fine-Tuning Your Own EQUIPA Model

Train a local LLM that actually knows how to be an agent. Not a generic chatbot — a model fine-tuned on real EQUIPA task executions using QLoRA.

## Overview

EQUIPA has two paths to better agent performance — and they complement each other:

- **Prompt optimization (Autoresearch)** — Mutate and benchmark agent prompts until they hit target success rates. Zero training infrastructure. Works with any model. See [CAPABILITIES.md](CAPABILITIES.md#autoresearch-automated-prompt-optimization) for details.
- **Weight optimization (QLoRA fine-tuning)** — Train a local model on successful agent executions. Requires GPU hardware. Produces a model that "thinks like an agent" from the start.

Start with Autoresearch — it's free, fast, and got 6/7 roles to 100% success. Fine-tuning is for when you want to run agents on local hardware at zero API cost, or when you need performance beyond what prompt engineering can deliver.

### The QLoRA Pipeline

The pipeline is straightforward:

1. **Forge Arena** generates training data by running agents on real tasks
2. **`prepare_training_data.py`** converts arena results into training format
<!-- drift-ignore -->
3. **`train_qlora.py` / `train_qlora_peft.py`** trains a QLoRA adapter on a base model (these scripts live in the separate training/benchmarks repo)
<!-- /drift-ignore -->
4. **Merge + convert** the adapter into a GGUF file for Ollama

You end up with a quantized model that runs locally, costs nothing per token, and is specifically tuned for agent work.

## Prerequisites

| Requirement | Details |
|-------------|---------|
| GPU | 11GB+ VRAM minimum (GTX 1080 Ti, RTX 3060, or better) |
| CUDA | 12.x installed and working |
| Python | 3.10+ with PyTorch (match your CUDA version) |
| Disk space | ~50GB for models, checkpoints, and training data |
| RAM | 32GB+ recommended (64GB for 27B+ models) |

Check your setup:

```bash
python3 -c "import torch; print(torch.cuda.get_device_name(0)); print(f'VRAM: {torch.cuda.get_device_properties(0).total_mem / 1e9:.1f}GB')"
```

If that doesn't print your GPU name, fix your CUDA/PyTorch install first. Nothing else will work without it.

### Python Dependencies

```bash
pip install torch transformers peft bitsandbytes datasets accelerate trl
```

Or if you prefer uv (you should):

```bash
uv pip install torch transformers peft bitsandbytes datasets accelerate trl
```

## Step 1: Generate Training Data with Forge Arena

Forge Arena is the data engine. It runs agents on real tasks, records every tool call, every decision, every output. Successful runs become training examples.

```bash
# Run arena on a project — generates task executions
python3 forge_arena.py --project myproject --iterations 20

# Run on multiple projects for diverse training data
python3 forge_arena.py --project project-a --iterations 20
python3 forge_arena.py --project project-b --iterations 20

# Run with specific roles to target training
python3 forge_arena.py --project myproject --iterations 10 --roles developer,tester
```

More iterations = more data. More projects = more diverse data. Both are good.

The Arena scores each run. High-scoring runs are gold. Low-scoring runs get filtered out later. Let it run overnight if you can.

## Step 2: Prepare Training Data

```bash
python3 prepare_training_data.py
```

This converts arena logs into instruction-response pairs. Each pair is one training example: "given this context and instruction, here's what the agent should do."

Output lands in `training_data/` as JSONL files.

### Filtering

By default, only successful runs (score above threshold) become training examples. You can adjust:

```bash
# Only use high-quality runs
python3 prepare_training_data.py --min-score 0.8

# Include a specific role only
python3 prepare_training_data.py --roles developer

# Check how many examples you have
wc -l training_data/*.jsonl
```

**Target: 10K+ examples minimum.** More is better. 50K+ is great. Under 5K and you'll see inconsistent results.

## Step 3: Train with QLoRA

Two scripts, same goal. Pick one.

### Option A: PEFT (Recommended)

Works on any GPU that can run PyTorch. More reliable. Slower but steady.

```bash
python3 train_qlora_peft.py \
  --base-model Qwen/Qwen3.5-9B \
  --epochs 3 \
  --lr 2e-4 \
  --batch-size 4 \
  --lora-r 16
```

### Option B: Unsloth (Faster)

Uses kernel optimizations for 2x speed. Not all GPUs/models supported.

```bash
python3 train_qlora.py \
  --base-model Qwen/Qwen3.5-9B \
  --epochs 3 \
  --lr 2e-4 \
  --batch-size 4 \
  --lora-r 16
```

If Unsloth crashes or doesn't support your GPU, fall back to PEFT. No shame in it.

### Key Parameters

| Parameter | Default | What It Does |
|-----------|---------|--------------|
| `--base-model` | — | Base model to fine-tune. Qwen3.5-9B recommended. |
| `--epochs` | 3 | Training passes over the data. 2-3 is usually enough. |
| `--lr` | 2e-4 | Learning rate. Lower = safer but slower convergence. |
| `--batch-size` | 4 | Samples per step. Lower if you run out of VRAM. |
| `--lora-r` | 16 | LoRA rank. Higher = more capacity but more VRAM and slower. |
| `--lora-alpha` | 32 | LoRA scaling factor. Usually 2x the rank. |
| `--max-length` | 4096 | Max token length per example. Longer = more VRAM. |

### Training Time Estimates

These assume ~50K training examples and default parameters:

| GPU | Qwen3.5-9B | Qwen3.5-27B |
|-----|------------|-------------|
| GTX 1080 Ti (11GB) | ~6 days | Not recommended |
| RTX 3060 (12GB) | ~4 days | Not recommended |
| RTX 3090 (24GB) | ~2 days | ~7 days |
| RTX 4090 (24GB) | ~1 day | ~3 days |
| 2x RTX 4090 | ~14 hours | ~1.5 days |

The 9B model is the sweet spot for most setups. Good quality, trainable on consumer GPUs, fast enough at inference to be practical.

### Monitoring Training

Watch the loss curve. It should drop steadily then flatten. If it spikes or oscillates, your learning rate is too high.

```bash
# Check training progress (if using wandb)
wandb login
# Then add --wandb to training command

# Or just watch the console output for loss values
```

### VRAM Troubleshooting

Out of memory? Try these in order:

1. Lower `--batch-size` to 2 or 1
2. Lower `--max-length` to 2048
3. Enable gradient checkpointing (usually on by default)
4. Use a smaller base model (4B instead of 9B)

## Step 4: Merge and Convert

Training produces a LoRA adapter — a small set of weight deltas. You need to merge it back into the base model, then convert to GGUF for Ollama.

### Merge the Adapter

```bash
python3 -c "
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

print('Loading base model...')
model = AutoModelForCausalLM.from_pretrained('Qwen/Qwen3.5-9B', torch_dtype='auto')
tokenizer = AutoTokenizer.from_pretrained('Qwen/Qwen3.5-9B')

print('Loading adapter...')
model = PeftModel.from_pretrained(model, 'lora_output/checkpoint-final')

print('Merging...')
model = model.merge_and_unload()

print('Saving merged model...')
model.save_pretrained('merged_model')
tokenizer.save_pretrained('merged_model')
print('Done.')
"
```

This needs enough RAM to hold the full model in memory. For 9B, that's ~20GB RAM. For 27B, ~60GB.

### Convert to GGUF

You need llama.cpp for this. Clone it if you don't have it:

```bash
git clone https://github.com/ggerganov/llama.cpp.git
pip install -r llama.cpp/requirements.txt
```

Then convert:

```bash
# Q4_K_M is a good balance of quality vs size
python3 llama.cpp/convert_hf_to_gguf.py merged_model \
  --outtype q4_k_m \
  --outfile equipa-9b-q4_k_m.gguf

# Q5_K_M if you have the VRAM and want slightly better quality
python3 llama.cpp/convert_hf_to_gguf.py merged_model \
  --outtype q5_k_m \
  --outfile equipa-9b-q5_k_m.gguf
```

### GGUF Size Reference

| Quantization | 9B Model Size | Quality |
|-------------|---------------|---------|
| Q4_K_M | ~5.5 GB | Good. Default choice. |
| Q5_K_M | ~6.5 GB | Better. Worth it if it fits. |
| Q6_K | ~7.5 GB | Near-lossless. |
| Q8_0 | ~9.5 GB | Overkill for most uses. |

## Step 5: Import to Ollama

Create a `Modelfile`:

```
FROM ./equipa-9b-q4_k_m.gguf

PARAMETER temperature 0.1
PARAMETER num_predict 4096
PARAMETER top_p 0.9

SYSTEM You are a EQUIPA agent. Follow instructions precisely and use tools efficiently. Think step by step. Be concise in your responses.
```

Low temperature is intentional. Agents need to be consistent and precise, not creative.

```bash
# Create the model
ollama create equipa -f Modelfile

# Quick test
ollama run equipa "Hello, test"

# Test with an agent-style prompt
ollama run equipa "Review this Python function for bugs: def add(a, b): return a - b"
```

## Step 6: Configure EQUIPA

Update `dispatch_config.json` to use your fine-tuned model:

```json
{
    "ollama_model": "equipa",
    "ollama_model_planner": "equipa"
}
```

You can also mix models — use the fine-tuned model for specific roles:

```json
{
    "ollama_model": "equipa",
    "ollama_model_planner": "qwen3.5:27b",
    "role_overrides": {
        "code-reviewer": "equipa",
        "developer": "equipa",
        "tester": "equipa"
    }
}
```

## Tips and Best Practices

**Data quality matters more than quantity.** 10K clean examples beat 100K noisy ones. Filter aggressively.

**Train on what you'll use it for.** The model gets good at the roles it sees in training. Heavy on developer data? Great developer agent. Want a good code reviewer? Feed it code review data.

**Start with PEFT.** It works everywhere. Only switch to Unsloth if you need the speed and your setup supports it.

**Test before deploying.** Run the fine-tuned model through a few Arena rounds before putting it in production. Compare scores against the base model.

**Iterate.** Your first fine-tune won't be perfect. Run more Arena iterations, gather more data, retrain. Each round gets better.

**Keep your base model.** Don't delete the original Qwen download. You'll want it for retraining when you have more data.

**Watch for overfitting.** If training loss keeps dropping but the model gets worse at new tasks, you've overtrained. Use fewer epochs or more data.

**Pascal GPU users (GTX 1080 Ti):** Pin PyTorch to 2.5.x and CUDA to 12.2. Newer versions dropped sm_61 support.

## Pre-Trained Model

Don't want to train your own? Use the Forgeborn pre-trained model when available:

```bash
# Via Ollama (easiest)
ollama pull forgeborn/equipa-qwen3.5-9b

# Or download the GGUF manually from HuggingFace
# https://huggingface.co/Forgeborn/equipa-qwen3.5-9b
```

The pre-trained model is fine-tuned on thousands of EQUIPA task executions across multiple projects. Good starting point even if you plan to fine-tune further on your own data.

---

*Copyright 2026 Forgeborn. Vibe coded with Claude.*

