"""GKP / the GST taxonomy (17 core groups + TG18 fallback): load, validate, embed once."""
from __future__ import annotations

from transport import tg_of, tsg_number
import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from psycopg2 import sql

REQUIRED = ("tsg_id", "name", "definition", "include", "exclude")


def _hash(v) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def load(path: Path) -> Tuple[dict, List[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    groups, subs, gids = data.get("groups", []), [], set()
    for g in groups:
        gid = g["tg_id"]
        if gid in gids:
            raise ValueError(f"Duplicate group ID: {gid}")
        gids.add(gid)
        for s in g.get("subgroups", []):
            e = dict(s, tg_id=gid, tg_name=g["name"])
            e.setdefault("key_provisions", [])
            e.setdefault("anchors", [])
            if not all(e.get(k) for k in REQUIRED):
                raise ValueError(f"Incomplete subgroup definition: {s.get('tsg_id')}")
            if tg_of(e["tsg_id"]) != gid:
                raise ValueError(f"Subgroup/group ID mismatch: {e['tsg_id']}")
            subs.append(e)
    if "TG18" not in gids or len({s["tsg_id"] for s in subs}) != len(subs) or len(groups) < 18:
        raise ValueError("Expected 17 core groups + TG18 and unique subgroup IDs")
    return data, subs


def embed_text(s: dict) -> str:
    """Positive retrieval text (exclusions guide the LLM, they are not mixed into the vector)."""
    t = (f"Group: {s['tg_name']}\nSubgroup: {s['name']}\nMeaning: {s['definition']}\n"
         f"Include: {'; '.join(s['include'])}")
    if s.get("key_provisions"):
        t += "\nKey provisions: " + "; ".join(s["key_provisions"])
    return t


def tg18_hash(subs: List[dict]) -> str:
    return _hash([(s["tsg_id"], s["definition"], s["include"]) for s in subs if s["tg_id"] == "TG18"])[:16]


def sync(conn, schema: str, subs: List[dict], embed: Callable, model: str, table: str = "gkp_taxonomy") -> int:
    """Embed each subgroup once; re-embed only when its text or the embedding model changes."""
    T = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (tsg_id text PRIMARY KEY, tg_id text NOT NULL,
            tg_name text NOT NULL, tsg_name text NOT NULL, definition text NOT NULL, include_json jsonb NOT NULL,
            exclude_json jsonb NOT NULL, embedding_input_hash text NOT NULL, embedding_model text NOT NULL,
            embedding vector NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())""").format(T))
        for col, typ in (("key_provisions", "jsonb"), ("anchors", "jsonb"), ("active", "boolean")):
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + typ).format(T, sql.Identifier(col)))
        cur.execute(sql.SQL("SELECT tsg_id, embedding_input_hash FROM {}").format(T))
        old = dict(cur.fetchall())
        cur.execute(sql.SQL("UPDATE {} SET active = (tsg_id = ANY(%s))").format(T), ([s["tsg_id"] for s in subs],))
    conn.commit()
    n = 0
    for s in subs:
        text = embed_text(s)
        fp = _hash({"text": text, "model": model})
        if old.get(s["tsg_id"]) == fp:
            with conn.cursor() as cur:   # metadata may change without the embedding text changing
                cur.execute(sql.SQL("UPDATE {} SET anchors=%s::jsonb, key_provisions=%s::jsonb, exclude_json=%s::jsonb, "
                                    "active=true WHERE tsg_id=%s").format(T),
                            (json.dumps(s["anchors"]), json.dumps(s["key_provisions"]), json.dumps(s["exclude"]), s["tsg_id"]))
            continue
        v = embed(text)
        if not v:
            raise RuntimeError("taxonomy embedding failed - Vertex unavailable?")
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""INSERT INTO {} (tsg_id, tg_id, tg_name, tsg_name, definition, include_json,
                exclude_json, embedding_input_hash, embedding_model, embedding, key_provisions, anchors, active)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::vector,%s::jsonb,%s::jsonb,true)
                ON CONFLICT (tsg_id) DO UPDATE SET tg_id=EXCLUDED.tg_id, tg_name=EXCLUDED.tg_name,
                tsg_name=EXCLUDED.tsg_name, definition=EXCLUDED.definition, include_json=EXCLUDED.include_json,
                exclude_json=EXCLUDED.exclude_json, embedding_input_hash=EXCLUDED.embedding_input_hash,
                embedding_model=EXCLUDED.embedding_model, embedding=EXCLUDED.embedding, key_provisions=EXCLUDED.key_provisions,
                anchors=EXCLUDED.anchors, active=true, updated_at=now()""").format(T),
                        (s["tsg_id"], s["tg_id"], s["tg_name"], s["name"], s["definition"], json.dumps(s["include"]),
                         json.dumps(s["exclude"]), fp, model, "[" + ",".join(str(float(x)) for x in v) + "]",
                         json.dumps(s["key_provisions"]), json.dumps(s["anchors"])))
        conn.commit()                      # keep paid embedding work even if a later call fails
        n += 1
    return n


def add_tg18_subgroup(path: Path, proposal: dict) -> str:
    """Human-approved TG18 proposal -> new TSG18_nn sub-group in the taxonomy file. Returns the new id."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    tg18 = next(g for g in data["groups"] if g["tg_id"] == "TG18")
    nums = [tsg_number(s["tsg_id"]) for s in tg18["subgroups"]]
    sid = f"TSG18_{max(nums + [0]) + 1:02d}"
    tg18["subgroups"].append({"tsg_id": sid, "name": proposal["name"], "definition": proposal["definition"],
                             "include": list(dict.fromkeys([proposal["name"]] + list(proposal["include"]))),
                             "exclude": list(proposal["exclude"]), "key_provisions": [], "anchors": [],
                             "from_proposal": proposal["proposal_id"]})
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return sid
