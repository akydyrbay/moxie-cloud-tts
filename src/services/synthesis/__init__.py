"""Text-to-speech synthesis engines.

`base.py` defines the provider-agnostic interface; `openai_tts.py` is the
concrete implementation (OpenAI TTS). Keeping the interface separate lets
another provider be dropped in later without touching the pipeline.
"""
