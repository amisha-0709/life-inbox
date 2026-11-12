"""Only organizer inputs and explicit contract/model versions define eligibility."""
import hashlib
import json
import os

CONTRACT_VERSION = "delta-v1"


def analysis_version():
    return f"{CONTRACT_VERSION}:{os.getenv('OPENAI_MODEL', 'gpt-5-mini')}"


def input_fingerprint(item):
    def value(name):
        return item.get(name) if isinstance(item, dict) else getattr(item, name)
    payload = [value("id"), value("item_type"), value("extracted_text") or value("raw_text") or "",
               value("source_url"), value("time_reference_at"), value("time_reference_timezone")]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
