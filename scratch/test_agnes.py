"""
Scratch script to test Agnes AI API connection using LiteLLM.
Run this using:
  uv run python scratch/test_agnes.py
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load env variables from .env in project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

def test_connection():
    print("Loading connection details...")
    model_provider = os.getenv("MODEL_PROVIDER")
    model_name = os.getenv("LITELLM_MODEL")
    api_base = os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("OPENAI_API_KEY")

    print(f"MODEL_PROVIDER: {model_provider}")
    print(f"LITELLM_MODEL: {model_name}")
    print(f"OPENAI_API_BASE: {api_base}")
    print(f"OPENAI_API_KEY: {'[SET]' if api_key and api_key != 'your_agnes_api_key_here' else '[NOT SET / DEFAULT]'}")

    if not api_key or api_key == "your_agnes_api_key_here":
        print("❌ Error: Please configure OPENAI_API_KEY in your .env file with your actual Agnes API Key.")
        return

    print("\nAttempting connection via litellm...")
    try:
        import litellm
        
        # Call litellm completion
        response = litellm.completion(
            model=model_name,
            messages=[{"role": "user", "content": "Say hello!"}],
            api_base=api_base,
            api_key=api_key,
        )
        print("✅ Success! Response:")
        print(response.choices[0].message.content)
    except Exception as e:
        print("❌ Failed to connect:")
        print(e)

if __name__ == "__main__":
    test_connection()
