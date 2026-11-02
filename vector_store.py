"""Embeddings (Vertex text embedding model, permission.json llm.embedding) for MKPs, atoms and the taxonomy."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.oauth2 import service_account

from transport import clean

_embedding_client = None
_embedding_model = ""


def load_vector_config() -> dict:
	from kp_settings import build_paths, load
	cfg, paths = load(), build_paths()
	if paths["env_file"].exists():
		load_dotenv(paths["env_file"], override=False)
	v = cfg["vector"]
	return {
		"enabled": bool(v.get("enabled", True)),
		"embedding_model": str(v["embedding_model"]).strip(),
		"top_k": int(v.get("top_k", 8)),
		"min_score": float(v.get("min_score", 0.55)),
		"vertex_location": str(v.get("vertex_location") or os.getenv("VERTEX_LOCATION", "global")).strip(),
		"gemini_credentials": paths["gemini_credentials"],
	}


# Create the Gemini embedding client only when a vector operation needs it.
def get_embedding_client(config: Optional[dict] = None):
	global _embedding_client, _embedding_model
	config = config or load_vector_config()
	model = str(config["embedding_model"])

	if _embedding_client is not None and _embedding_model == model:
		return _embedding_client

	credential_file = Path(config["gemini_credentials"])
	if not credential_file.exists():
		raise FileNotFoundError(f"Gemini credential file not found: {credential_file}")

	data = json.loads(credential_file.read_text(encoding="utf-8"))
	project_id = clean(data.get("project_id"))
	if not project_id:
		raise ValueError(f"project_id is missing in {credential_file.name}")

	credentials = service_account.Credentials.from_service_account_file(
		str(credential_file),
		scopes=["https://www.googleapis.com/auth/cloud-platform"],
	)

	_embedding_client = genai.Client(
		vertexai=True,
		project=project_id,
		location=config["vertex_location"],
		credentials=credentials,
		http_options=types.HttpOptions(api_version="v1"),
	)
	_embedding_model = model
	return _embedding_client


# Convert one text value into an embedding vector.
def embed_text(text: str, task_type: str, config: Optional[dict] = None) -> List[float]:
	config = config or load_vector_config()
	text = clean(text)
	if not text:
		raise ValueError("Cannot embed empty text.")

	client = get_embedding_client(config)
	response = client.models.embed_content(
		model=config["embedding_model"],
		contents=text,
		config=types.EmbedContentConfig(task_type=task_type),
	)

	embeddings = getattr(response, "embeddings", None) or []
	if not embeddings:
		raise RuntimeError("Embedding API returned no vectors.")

	values = getattr(embeddings[0], "values", None)
	if not values:
		raise RuntimeError("Embedding API returned an empty vector.")

	return [float(value) for value in values]
