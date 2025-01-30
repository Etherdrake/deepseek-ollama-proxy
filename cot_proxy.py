from flask import Flask, request, Response, stream_with_context
import requests
import re
import os
import logging
from typing import Any
from urllib.parse import urljoin

# Parameter type definitions
PARAM_TYPES = {
    # Float parameters (0.0 to 1.0 typically)
    'temperature': float,      # Randomness in sampling
    'top_p': float,           # Nucleus sampling threshold
    'presence_penalty': float, # Penalty for token presence
    'frequency_penalty': float,# Penalty for token frequency
    'repetition_penalty': float, # Penalty for repetition
    
    # Integer parameters
    'top_k': int,             # Top-k sampling parameter
    'max_tokens': int,        # Maximum tokens to generate
    'n': int,                 # Number of completions
    'seed': int,              # Random seed for reproducibility
    'num_ctx': int,           # Context window size
    'num_predict': int,       # Number of tokens to predict
    'repeat_last_n': int,     # Context for repetition penalty
    'batch_size': int,        # Batch size for generation
    
    # Boolean parameters
    'echo': bool,             # Whether to echo prompt
    'stream': bool,           # Whether to stream responses
    'mirostat': bool,         # Use Mirostat sampling
}

def convert_param_value(key: str, value: str) -> Any:
    """Convert parameter value to appropriate type based on parameter name."""
    if not value or value.lower() == 'null':
        return None
        
    param_type = PARAM_TYPES.get(key)
    if not param_type:
        return value  # Keep as string if not a known numeric param
        
    try:
        if param_type == bool:
            return value.lower() == 'true'
        return param_type(value)
    except (ValueError, TypeError):
        # If conversion fails, log warning and return original string
        logger.warning(f"Failed to convert parameter '{key}' value '{value}' to {param_type.__name__}")
        return value

app = Flask(__name__)

# Configure logging based on DEBUG environment variable
log_level = logging.DEBUG if os.getenv('DEBUG', 'true').lower() == 'true' else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure target URL
TARGET_BASE_URL = os.getenv('TARGET_BASE_URL', 'http://localhost:11434/api/chat')
if not TARGET_BASE_URL.endswith('/'):
    TARGET_BASE_URL += '/'  # Ensure trailing slash for urljoin

logger.debug(f"Starting proxy with target URL: {TARGET_BASE_URL}")
logger.debug(f"Debug mode: {log_level == logging.DEBUG}")

@app.route('/health')
def health_check():
    try:
        # Try to connect to the target URL
        response = requests.get(
            TARGET_BASE_URL,
            timeout=5,
            verify=True
        )
        logger.debug(f"Health check - Target URL: {TARGET_BASE_URL}")
        logger.debug(f"Health check - Status code: {response.status_code}")
        
        return Response(
            response='{"status": "healthy", "target_url": "' + TARGET_BASE_URL + '"}',
            status=200,
            content_type="application/json"
        )
    except Exception as e:
        error_msg = f"Health check failed: {str(e)}"
        logger.error(error_msg)
        return Response(
            response='{"status": "unhealthy", "error": "' + error_msg + '"}',
            status=503,
            content_type="application/json"
        )

@app.route('/', defaults={'path': ''}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route('/<path:path>', methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy(path):
    # Construct the target URL dynamically using the base URL and client's path
    target_url = urljoin(TARGET_BASE_URL, path)
    
    # Forward all headers (except "Host" to avoid conflicts)
    headers = {
        key: value 
        for key, value in request.headers 
        if key.lower() != "host"
    }
    
    # Forward query parameters from the original request
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"

    # Log request details
    logger.debug(f"Forwarding {request.method} request to: {target_url}")
    logger.debug(f"Headers: {headers}")
    
    try:
        # Get JSON body if present
        json_body = request.get_json(silent=True) if request.is_json else None
        logger.debug(f"Request JSON body: {json_body}")
        
        # Try to connect with a timeout
        try:
            api_response = requests.request(
                method=request.method,
                url=target_url,
                headers=headers,
                json=json_body,
                stream=True,
                timeout=int(os.getenv('API_REQUEST_TIMEOUT', 120)),  # Timeout in seconds, default 120
                verify=True  # Verify SSL certificates
            )
            logger.debug(f"Connected to target URL: {target_url}")
            logger.debug(f"Target response time: {api_response.elapsed.total_seconds()}s")
        except requests.exceptions.Timeout:
            error_msg = f"Connection to {target_url} timed out"
            logger.error(error_msg)
            return Response(
                response='{"error": "' + error_msg + '"}',
                status=504,
                content_type="application/json"
            )
        except requests.exceptions.SSLError as e:
            error_msg = f"SSL verification failed: {str(e)}"
            logger.error(error_msg)
            return Response(
                response='{"error": "' + error_msg + '"}',
                status=502,
                content_type="application/json"
            )
        except requests.exceptions.ConnectionError as e:
            error_msg = f"Failed to connect to {target_url}: {str(e)}"
            logger.error(error_msg)
            return Response(
                response='{"error": "' + error_msg + '"}',
                status=502,
                content_type="application/json"
            )
        
        # For error responses, return them directly without streaming
        if api_response.status_code >= 400:
            error_content = api_response.content.decode('utf-8')
            logger.error(f"Target server error: {api_response.status_code}")
            logger.error(f"Error response: {error_content}")
            return Response(
                error_content,
                status=api_response.status_code,
                content_type=api_response.headers.get("Content-Type", "application/json")
            )
                
    except requests.exceptions.RequestException as e:
        error_msg = f"Failed to forward request: {str(e)}"
        logger.error(error_msg)
        return Response(
            response='{"error": "' + error_msg + '"}',
            status=502,
            content_type="application/json"
        )

    # Buffer for handling think tags across chunks
    class StreamBuffer:
        def __init__(self):
            self.buffer = ""
            self.leading_newlines_removed = False  # Track if leading newlines have been removed

        def process_chunk(self, chunk):
            # Decode and add to buffer
            decoded_chunk = chunk.decode("utf-8", errors="replace")
            self.buffer += decoded_chunk

            output = ""

            # Remove leading newlines (before the first number or letter) if not already done
            if not self.leading_newlines_removed:
                self.buffer = re.sub(r'^\s+', '', self.buffer)
                self.leading_newlines_removed = True  # Mark as done

            while True:
                # Find the next potential tag start (both regular and Unicode-encoded)
                start = min(
                    self.buffer.find("<think>"),
                    self.buffer.find("\\u003cthink\\u003e"),
                    key=lambda x: x if x != -1 else float('inf')
                )

                if start == -1:
                    # No more think tags, output all except last few chars
                    if len(self.buffer) > 1024:
                        output += self.buffer[:-1024]
                        self.buffer = self.buffer[-1024:]
                    break

                # Output content before the tag
                if start > 0:
                    output += self.buffer[:start]
                    self.buffer = self.buffer[start:]
                    start = 0  # Tag is now at start of buffer

                # Look for end tag (both regular and Unicode-encoded)
                end = min(
                    self.buffer.find("</think>", start),
                    self.buffer.find("\\u003c/think\\u003e", start),
                    key=lambda x: x if x != -1 else float('inf')
                )

                if end == -1:
                    # No end tag yet, keep in buffer
                    break

                # Remove the complete think tag and its content
                if self.buffer.startswith("<think>"):
                    end += len("</think>")
                else:
                    end += len("\\u003c/think\\u003e")
                self.buffer = self.buffer[end:]

            return output.encode("utf-8") if output else b""

        def flush(self):
            # Output remaining content and ensure leading newlines are removed
            if not self.leading_newlines_removed:
                self.buffer = re.sub(r'^\s+', '', self.buffer)
                self.leading_newlines_removed = True
            output = self.buffer
            self.buffer = ""
            return output.encode("utf-8")

    # Check if response should be streamed
    is_stream = json_body.get('stream', False) if json_body else False
    logger.debug(f"Stream mode: {is_stream}")

    if not is_stream:
        # For non-streaming responses, return the full content
        content = api_response.content
        decoded = content.decode("utf-8", errors="replace")

        # Strip both regular and Unicode-encoded <think> tags
        filtered = re.sub(r'(<think>.*?</think>|\\u003cthink\\u003e.*?\\u003c/think\\u003e)', '', decoded,
                          flags=re.DOTALL)

        # Remove leading newlines (before the first number or letter)
        filtered = re.sub(r'^\s+', '', filtered)

        logger.debug(f"Non-streaming response content: {filtered}")
        return Response(
            filtered.encode("utf-8"),
            status=api_response.status_code,
            headers=[(name, value) for name, value in api_response.headers.items() if name.lower() != "content-length"],
            content_type=api_response.headers.get("Content-Type", "application/json")
        )
    else:
        # Stream the filtered response back to the client
        def generate_filtered_response():
            buffer = StreamBuffer()
            for chunk in api_response.iter_content(chunk_size=8192):
                # Process chunk through buffer
                output = buffer.process_chunk(chunk)
                if output:
                    logger.debug(f"Streaming chunk: {output.decode('utf-8', errors='replace')}")
                    yield output
            
            # Flush any remaining content
            final_output = buffer.flush()
            if final_output:
                logger.debug(f"Final streaming chunk: {final_output.decode('utf-8', errors='replace')}")
                yield final_output

        # Log response details
        logger.debug(f"Response status: {api_response.status_code}")
        logger.debug(f"Response headers: {dict(api_response.headers)}")
        
        return Response(
            stream_with_context(generate_filtered_response()),
            status=api_response.status_code,
            headers=[(name, value) for name, value in api_response.headers.items() if name.lower() != "content-length"],
            content_type=api_response.headers.get("Content-Type", "application/json")
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
