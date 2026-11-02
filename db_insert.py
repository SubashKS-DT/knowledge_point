"""PostgreSQL connection for every LAW ARC step (settings in Credentials/.env).

LEGAL_DB_HOST / LEGAL_DB_PORT may default (localhost / 5432); LEGAL_DB_NAME, LEGAL_DB_USER and LEGAL_DB_PASSWORD
must be set. The first connection of a run also upgrades old table names (db_migrate.py).
"""
from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv


def load_project_environment() -> None:
	from kp_settings import build_paths
	try:
		env_file = build_paths()["env_file"]
	except Exception:
		env_file = None
	if env_file is not None and env_file.exists():
		load_dotenv(env_file, override=False)
	else:
		load_dotenv(override=False)


def get_db_config() -> dict:
	def req(key: str) -> str:
		value = os.getenv(key)
		if not value:
			raise KeyError(f"Required environment variable '{key}' not set in Credentials/.env or the environment")
		return value
	return {
		"host": os.getenv("LEGAL_DB_HOST", "localhost"),
		"port": int(os.getenv("LEGAL_DB_PORT", "5432")),
		"dbname": req("LEGAL_DB_NAME"),
		"user": req("LEGAL_DB_USER"),
		"password": req("LEGAL_DB_PASSWORD"),
	}


def get_db_schema() -> str:
	return os.getenv("LEGAL_DB_SCHEMA", "public")


def get_connection():
	load_project_environment()
	conn = psycopg2.connect(**get_db_config())
	from db_migrate import migrate
	migrate(conn, get_db_schema())
	return conn
