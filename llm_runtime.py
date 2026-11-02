from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from anthropic import Anthropic
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.oauth2 import service_account


BASE_DIR = Path(__file__).resolve().parent


# Convert any simple value into clean text.
def clean(value) -> str:
	return "" if value is None else str(value).strip()


# Read a required text file.
def load_text_file(path: Path, label: str) -> str:
	path = Path(path)
	if not path.exists():
		raise FileNotFoundError(f"{label} not found: {path}")
	return path.read_text(encoding="utf-8").strip()


# Return the first file that exists from the supplied choices.
def first_existing_file(*paths: Path) -> Path:
	for path in paths:
		path = Path(path)
		if path.exists():
			return path

	raise FileNotFoundError(
		"None of the expected files exist: "
		+ ", ".join(str(Path(path)) for path in paths)
	)


# Convert a project-relative path into an absolute path.
def resolve_project_path(value: str) -> Path:
	path = Path(str(value).strip())
	return path if path.is_absolute() else BASE_DIR / path


# Build credential paths from permission.json when paths are not supplied.
def load_runtime_paths() -> dict:
	from kp_settings import build_paths
	return build_paths()


def load_runtime_environment(paths: dict) -> None:
	env_file = Path(paths["env_file"])
	if env_file.exists():
		load_dotenv(env_file, override=False)


# Create the Gemini client from the configured service credential file.
def create_gemini_client(paths: dict):
	credential_file = Path(paths["gemini_credentials"])

	if not credential_file.exists():
		raise FileNotFoundError(
			f"Gemini credential file not found: {credential_file}"
		)

	data = json.loads(credential_file.read_text(encoding="utf-8"))
	project_id = clean(data.get("project_id"))

	if not project_id:
		raise ValueError(f"project_id is missing in {credential_file.name}")

	credentials = service_account.Credentials.from_service_account_file(
		str(credential_file),
		scopes=["https://www.googleapis.com/auth/cloud-platform"],
	)

	return genai.Client(
		vertexai=True,
		project=project_id,
		location=os.getenv("VERTEX_LOCATION", "global"),
		credentials=credentials,
		http_options=types.HttpOptions(api_version="v1"),
	)


# Create the Claude client from the configured credential file.
def create_claude_client(paths: dict):
	credential_file = Path(paths["claude_credentials"])

	if not credential_file.exists():
		raise FileNotFoundError(
			f"Claude credential file not found: {credential_file}"
		)

	data = json.loads(credential_file.read_text(encoding="utf-8"))
	api_key = clean(data.get("api_key"))

	if not api_key or api_key == "PASTE_YOUR_CLAUDE_API_KEY_HERE":
		raise ValueError(f"api_key is missing in {credential_file.name}")

	return Anthropic(api_key=api_key)


# Create one runtime for IKP or MKP.
def create_llm_runtime(provider: str, paths: Optional[dict] = None) -> dict:
	if paths is None:
		paths = load_runtime_paths()

	load_runtime_environment(paths)
	provider = clean(provider).upper()

	if provider == "GEMINI":
		return {
			"provider": "GEMINI",
			"model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
			"client": create_gemini_client(paths),
		}

	if provider == "CLAUDE":
		return {
			"provider": "CLAUDE",
			"model": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
			"client": create_claude_client(paths),
		}

	raise ValueError(f"Unsupported LLM provider: {provider}")


# Send one prompt to Gemini.

def call_gemini(client, model: str, prompt: str) -> str:
	response = client.models.generate_content(
		model=model,
		contents=prompt,
		config=types.GenerateContentConfig(
			temperature=0,
			candidate_count=1,
			seed=int(os.getenv("LLM_SEED", "17")),
			max_output_tokens=32768,
			automatic_function_calling=types.AutomaticFunctionCallingConfig(
				disable=True
			),
		),
	)

	text = clean(response.text)
	if not text:
		raise RuntimeError("Gemini returned an empty response.")
	return text



# Send one prompt to Claude.
def call_claude(client, model: str, prompt: str) -> str:
	parts: List[str] = []

	with client.messages.stream(
		model=model,
		max_tokens=32768,
		messages=[{"role": "user", "content": prompt}],
	) as stream:
		for chunk in stream.text_stream:
			if chunk:
				parts.append(chunk)

	text = "".join(parts).strip()
	if not text:
		raise RuntimeError("Claude returned an empty response.")
	return text


# Call the selected model and retry temporary failures.
def call_llm(runtime: dict, prompt: str) -> Tuple[str, int]:
	provider = clean(runtime.get("provider")).upper()
	model = clean(runtime.get("model"))
	client = runtime.get("client")
	max_retries = int(os.getenv("MAX_API_RETRIES", "3"))
	last_error: Optional[Exception] = None

	for attempt in range(1, max_retries + 1):
		try:
			if provider == "GEMINI":
				return call_gemini(client, model, prompt), attempt

			if provider == "CLAUDE":
				return call_claude(client, model, prompt), attempt

			raise ValueError(f"Unsupported provider: {provider}")

		except Exception as exc:
			last_error = exc
			if attempt < max_retries:
				time.sleep(attempt * 2)

	raise RuntimeError(
		f"{provider} call failed after {max_retries} attempts: {last_error}"
	)
