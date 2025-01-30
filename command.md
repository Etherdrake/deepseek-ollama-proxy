sudo docker run -e TARGET_BASE_URL="api_base" -e LLM_PARAMS="model=deepseek-v1:7b, temperature=0.6" -p 5000:5000 openai-proxy
