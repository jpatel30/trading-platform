#!/bin/bash
# scripts/ensure_models.sh
# Run after docker compose up to ensure Ollama models are loaded.
# Safe to run repeatedly — ollama pull is idempotent.

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

echo "Checking Ollama models..."

# Wait for Ollama to be ready
for i in $(seq 1 30); do
    if curl -sf "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
        break
    fi
    echo "  Waiting for Ollama ($i/30)..."
    sleep 2
done

# Check which models are loaded
MODELS=$(curl -sf "$OLLAMA_HOST/api/tags" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(' '.join(m['name'] for m in data.get('models', [])))
" 2>/dev/null || echo "")

echo "Loaded models: ${MODELS:-none}"

# Pull qwen2.5:14b if not present
if echo "$MODELS" | grep -q "qwen2.5:14b"; then
    echo "✅ qwen2.5:14b already loaded"
else
    echo "Pulling qwen2.5:14b (~9GB, this may take a few minutes)..."
    docker exec trading_ollama ollama pull qwen2.5:14b
    echo "✅ qwen2.5:14b ready"
fi

# Pull nomic-embed-text if not present
if echo "$MODELS" | grep -q "nomic-embed-text"; then
    echo "✅ nomic-embed-text already loaded"
else
    echo "Pulling nomic-embed-text..."
    docker exec trading_ollama ollama pull nomic-embed-text
    echo "✅ nomic-embed-text ready"
fi

echo ""
echo "All models ready. Running test..."
python3 -c "
import requests
r = requests.post('http://localhost:11434/api/generate', json={
    'model': 'qwen2.5:14b',
    'prompt': 'Say OK',
    'stream': False,
    'options': {'num_predict': 5}
}, timeout=60)
print('Qwen test:', 'PASS ✅' if r.status_code == 200 else f'FAIL ❌ ({r.status_code})')
"