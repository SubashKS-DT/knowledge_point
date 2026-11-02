from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pandas as pd
from openpyxl.styles import Alignment, Font


# Convert any value to clean text.
def clean(value: Any) -> str:
	if value is None:
		return ""

	try:
		if pd.isna(value):
			return ""
	except Exception:
		pass

	return str(value).strip()


# Remove markdown code fences from an LLM response.
def strip_code_fences(value: str) -> str:
	text = clean(value)
	if not text:
		return ""

	text = re.sub(r"^```(?:text|txt|csv|json|markdown)?\s*", "", text, flags=re.IGNORECASE)
	text = re.sub(r"\s*```$", "", text)
	return text.strip()


# Make text safe for use as a file name.
def safe_filename(value: str) -> str:
	text = clean(value)
	text = re.sub(r"[<>:\"/\\|?*]+", "_", text)
	text = re.sub(r"\s+", "_", text)
	text = re.sub(r"_+", "_", text).strip("._ ")
	return text or "output"


# Split text only when the separator is outside quotes and brackets.
def _split_outside(value: str, separator: str) -> List[str]:
	parts: List[str] = []
	current: List[str] = []
	quote = ""
	depth = 0
	escaped = False

	for char in value:
		if escaped:
			current.append(char)
			escaped = False
			continue

		if char == "\\":
			current.append(char)
			escaped = True
			continue

		if quote:
			current.append(char)
			if char == quote:
				quote = ""
			continue

		if char in {'"', "'"}:
			quote = char
			current.append(char)
			continue

		if char in "[({":
			depth += 1
			current.append(char)
			continue

		if char in "]) }".replace(" ", ""):
			depth = max(0, depth - 1)
			current.append(char)
			continue

		if char == separator and depth == 0:
			parts.append("".join(current).strip())
			current = []
			continue

		current.append(char)

	parts.append("".join(current).strip())
	return parts


# Remove one matching pair of surrounding quotes.
def _unquote(value: str) -> str:
	text = clean(value)
	if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
		text = text[1:-1]
	text = text.replace(r'\"', '"').replace(r"\'", "'")
	return text.strip()


# Parse key:value fields from the RESULTS column without knowing the prompt schema.
def parse_results(value: str) -> Dict[str, str]:
	results: Dict[str, str] = {}
	text = clean(value)
	if not text:
		return results

	for part in _split_outside(text, ";"):
		if not part or ":" not in part:
			continue

		key, raw_value = part.split(":", 1)
		key = clean(key).lower().replace(" ", "_")
		if not key:
			continue

		results[key] = _unquote(raw_value)

	return results


# Read a field from a parsed row using case-insensitive aliases.
def get_field(row: dict, *names: str) -> str:
	if not isinstance(row, dict):
		return ""

	lookup = {str(key).strip().lower(): value for key, value in row.items()}

	for name in names:
		key = clean(name).lower()
		if key in lookup:
			value = clean(lookup[key])
			if value:
				return _unquote(value)

	results = row.get("RESULTS") or row.get("results")
	if isinstance(results, dict):
		result_lookup = {str(key).strip().lower(): value for key, value in results.items()}
	else:
		result_lookup = parse_results(clean(results))

	for name in names:
		key = clean(name).lower()
		if key in result_lookup:
			value = clean(result_lookup[key])
			if value:
				return _unquote(value)

	return ""


# Return the row TYPE in uppercase.
def find_row_type(row: dict) -> str:
	return get_field(row, "TYPE", "row_type", "type").upper()


# Return the row ELEMENT/key.
def row_element(row: dict) -> str:
	return get_field(row, "ELEMENT", "element", "key", "id")


# Convert a bracketed/list-like field into a Python list.
def parse_list(value: Any) -> List[str]:
	if isinstance(value, (list, tuple, set)):
		return [clean(item) for item in value if clean(item)]

	text = clean(value)
	if not text or text.upper() == "NA":
		return []

	if text.startswith("[") and text.endswith("]"):
		text = text[1:-1].strip()

	if not text:
		return []

	parts = _split_outside(text, ",")
	return [_unquote(part) for part in parts if _unquote(part)]


# Derive an element ID from a concept name.
def derive_id(prefix: str, concept: str) -> str:
	prefix = clean(prefix).upper()
	concept = clean(concept)
	if not prefix:
		raise ValueError("ID prefix is required.")
	if not concept:
		raise ValueError("Concept name is required.")

	tokens = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", concept)
	if not tokens:
		raise ValueError(f"Cannot derive ID from concept: {concept}")

	pieces: List[str] = []
	for token in tokens:
		plain = token.replace("-", "")
		if re.fullmatch(r"\d+", plain):
			pieces.append(plain)
		elif re.fullmatch(r"[IVXLCDM]+", plain.upper()):
			pieces.append(plain.upper())
		else:
			pieces.append(plain[0].upper())

	acronym = "".join(pieces)
	return f"{prefix}_{acronym}"


# Extract LAW ARC element IDs mentioned inside free text.
def extract_ids(
	value: Any,
	prefixes: Sequence[str] = ("A", "G", "LE", "PD", "CT", "M"),
) -> List[str]:
	text = clean(value)
	if not text:
		return []

	prefix_pattern = "|".join(re.escape(clean(prefix)) for prefix in prefixes if clean(prefix))
	if not prefix_pattern:
		return []

	pattern = rf"\b(?:{prefix_pattern})_[A-Za-z0-9_]+\b"
	found = re.findall(pattern, text)

	results: List[str] = []
	seen = set()
	for item in found:
		if item not in seen:
			seen.add(item)
			results.append(item)
	return results


# Parse the LLM pipe-table output into reusable Python rows.
def parse_model_output(raw: str) -> List[dict]:
	text = strip_code_fences(raw)
	if not text:
		return []

	lines = [line.strip() for line in text.splitlines() if line.strip()]
	if not lines:
		return []

	header_index = -1
	headers: List[str] = []

	for index, line in enumerate(lines):
		parts = [clean(part).upper() for part in line.split("|")]
		if len(parts) >= 4 and "TYPE" in parts and "ELEMENT" in parts and "RESULTS" in parts:
			header_index = index
			headers = [clean(part) for part in line.split("|")]
			break

	if header_index < 0:
		return []

	rows: List[dict] = []
	column_count = len(headers)

	for line in lines[header_index + 1:]:
		if set(line) <= {"-", "|", ":", " "}:
			continue

		parts = line.split("|", column_count - 1)
		if len(parts) != column_count:
			continue

		row = {headers[index]: clean(parts[index]) for index in range(column_count)}
		result_fields = parse_results(row.get("RESULTS", ""))

		for key, value in result_fields.items():
			if key not in {str(existing).lower() for existing in row.keys()}:
				row[key] = value

		rows.append(row)

	return rows


# Group each MKP row with its following phrase rows.

def mkp_bundles(rows: Sequence[dict]) -> List[dict]:
	bundles: List[dict] = []
	by_key: Dict[str, dict] = {}

	for row in rows:
		if find_row_type(row) != "MKP":
			continue

		key = row_element(row)
		concept = get_field(row, "concept", "concept_name", "name")
		if not key or not concept:
			continue

		mkp_id = get_field(row, "mkp_id") or derive_id("M", concept)

		bundle = {
			"key": key,
			"mkp_id": mkp_id,
			"mkp_row": row,
			"sp_rows": [],
		}
		bundles.append(bundle)
		by_key[key] = bundle

	for row in rows:
		if find_row_type(row) != "SP":
			continue

		key = row_element(row)
		if key in by_key:
			by_key[key]["sp_rows"].append(row)

	return bundles


# Apply simple readable formatting to an Excel sheet.
def format_sheet(sheet) -> None:
	if sheet.max_row < 1:
		return

	for cell in sheet[1]:
		cell.font = Font(bold=True)
		cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

	for row in sheet.iter_rows(min_row=2):
		for cell in row:
			cell.alignment = Alignment(vertical="top", wrap_text=True)

	for column_cells in sheet.columns:
		letter = column_cells[0].column_letter
		max_length = 0
		for cell in column_cells:
			value = clean(cell.value)
			if value:
				max_length = max(max_length, max(len(line) for line in value.splitlines()))
		sheet.column_dimensions[letter].width = min(max(max_length + 2, 12), 70)

	sheet.freeze_panes = "A2"
	sheet.auto_filter.ref = sheet.dimensions


# Write MKP exactly as its four-column prompt table.
def write_rows_excel(path: Path, rows: Sequence[dict], sheet_name: str) -> None:
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)

	rows = list(rows)
	if not rows:
		df = pd.DataFrame(columns=["CLUSTER", "TYPE", "ELEMENT", "RESULTS"])
	elif all(
		any(str(key).upper() == "CLUSTER" for key in row.keys())
		and any(str(key).upper() == "TYPE" for key in row.keys())
		and any(str(key).upper() == "ELEMENT" for key in row.keys())
		and any(str(key).upper() == "RESULTS" for key in row.keys())
		for row in rows
	):
		output_rows = []
		for row in rows:
			lookup = {str(key).upper(): value for key, value in row.items()}
			output_rows.append({
				"CLUSTER": lookup.get("CLUSTER", ""),
				"TYPE": lookup.get("TYPE", ""),
				"ELEMENT": lookup.get("ELEMENT", ""),
				"RESULTS": lookup.get("RESULTS", ""),
			})
		df = pd.DataFrame(output_rows, columns=["CLUSTER", "TYPE", "ELEMENT", "RESULTS"])
	else:
		columns: List[str] = []
		seen = set()
		for row in rows:
			for key in row.keys():
				key = str(key)
				if key not in seen:
					seen.add(key)
					columns.append(key)
		df = pd.DataFrame(rows, columns=columns)

	with pd.ExcelWriter(path, engine="openpyxl") as writer:
		df.to_excel(writer, sheet_name=sheet_name, index=False)
		format_sheet(writer.book[sheet_name])


# ---------------------------------------------------------------- taxonomy ids (TG01 / TSG01_03)

def tg_of(tsg_id: str) -> str:
	"""TSG01_03 -> TG01 ('' when the id is not a TSG id)."""
	m = re.fullmatch(r"TSG(\d\d)_(\d\d)", str(tsg_id or "").strip())
	return f"TG{m.group(1)}" if m else ""


def tsg_number(tsg_id: str) -> int:
	"""TSG18_02 -> 2 (0 when the id is not a TSG id)."""
	m = re.fullmatch(r"TSG\d\d_(\d\d)", str(tsg_id or "").strip())
	return int(m.group(1)) if m else 0

