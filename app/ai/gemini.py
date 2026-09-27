"""Thin Gemini client wrapper. Configure with GEMINI_API_KEY (and optionally GEMINI_MODEL)
in the environment; unset means AI features are disabled. Never hardcode a key here.
"""

from flask import current_app
from google import genai


class AIError(Exception):
    """Raised when the Gemini call can't be made or fails."""


def _client():
    api_key = current_app.config.get("GEMINI_API_KEY")
    if not api_key:
        raise AIError("GEMINI_API_KEY isn't set. Add it to your environment to enable AI features.")
    return genai.Client(api_key=api_key)


def generate(prompt, **kwargs):
    """Return the model's text response to a single prompt."""
    try:
        response = _client().models.generate_content(
            model=current_app.config["GEMINI_MODEL"], contents=prompt, **kwargs
        )
    except AIError:
        raise
    except Exception as exc:  # the SDK raises assorted google.genai.errors subclasses
        raise AIError(str(exc)) from exc
    return response.text
