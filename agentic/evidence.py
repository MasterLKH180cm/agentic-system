"""Source normalization, whole-JSON context packing and citation validation."""
import json
import math
import re


CITATION = re.compile(r"\[([^\]\n]+)\]")
SOURCE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,200}$")


def normalize_citations(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("Invalid citation list")
    output = {}
    for source in value:
        if not isinstance(source, dict):
            raise ValueError("Invalid citation")
        identifier, document, text = source.get("chunk_id"), source.get("document_id"), source.get("text")
        if not isinstance(identifier, str) or not SOURCE_ID.fullmatch(identifier):
            raise ValueError("Invalid source ID")
        if not isinstance(document, str) or not 1 <= len(document) <= 250 or not isinstance(text, str) or not text.strip():
            raise ValueError("Citation requires a document ID and source text")
        item = {"chunk_id": identifier, "document_id": document, "text": text[:4000],
                "text_truncated": len(text) > 4000 or bool(source.get("text_truncated"))}
        for key, limit in (("title", 200), ("source", 500), ("section", 200), ("revision", 100),
                           ("profile_id", 100), ("origin_source", 100), ("origin_chunk_id", 200)):
            if source.get(key) is not None:
                if not isinstance(source[key], str):
                    raise ValueError("Invalid source metadata")
                item[key] = source[key][:limit]
        for key in ("start_char", "end_char"):
            if source.get(key) is not None:
                if type(source[key]) is not int or source[key] < 0:
                    raise ValueError("Invalid source offset")
                item[key] = source[key]
        if source.get("source_run_id") is not None:
            if not isinstance(source["source_run_id"], str) or len(source["source_run_id"]) > 80:
                raise ValueError("Invalid memory provenance")
            item["source_run_id"] = source["source_run_id"]
        if "score" in source:
            if type(source["score"]) not in (int, float) or not math.isfinite(source["score"]):
                raise ValueError("Invalid source score")
            item["score"] = source["score"]
        if identifier in output and (output[identifier]["document_id"], output[identifier]["text"]) != (document, item["text"]):
            raise ValueError("Conflicting source identity")
        output[identifier] = item
    return list(output.values())


def extractive_answer(sources: list[dict], observations: list[dict] | None = None, limit: int = 12000) -> str:
    lines = []
    used = 0
    for source in sources:
        prefix = f"[{source['chunk_id']}] "
        # Source brackets are data; reserve citation brackets for validated IDs.
        excerpt = source["text"].replace("[", "(").replace("]", ")")
        remaining = limit - used - len(prefix) - 1
        if remaining <= 0:
            break
        line = prefix + excerpt[:remaining]
        lines.append(line)
        used += len(line) + 1
    for item in observations or []:
        line = f"{item['step']}: {item.get('expression', 'value')} = {item['value']}"
        if used + len(line) + 1 <= limit:
            lines.append(line)
            used += len(line) + 1
    return "\n".join(lines) if lines else "Insufficient evidence; no cited sources or arithmetic observations."


def pack_evidence(question: str, inputs: dict, limit: int = 14000) -> tuple[str, list[dict], list[dict]]:
    sources, numbers = {}, []

    def collect(name: str, result, depth: int = 0):
        if not isinstance(result, dict) or depth > 5:
            return
        for source in normalize_citations(result.get("citations", [])):
            existing = sources.get(source["chunk_id"])
            if existing and (existing["document_id"], existing["text"]) != (source["document_id"], source["text"]):
                raise ValueError("Conflicting source identity")
            sources[source["chunk_id"]] = source
        if type(result.get("value")) in (int, float) and math.isfinite(result["value"]):
            numbers.append({"step": name[:100], "value": result["value"],
                            "expression": str(result.get("expression", "value"))[:120]})
        if isinstance(result.get("outputs"), dict):
            for key, value in list(result["outputs"].items())[:24]:
                collect(name + "." + str(key), value, depth + 1)

    for name, result in inputs.items():
        collect(name, result)
    payload = {"question": question, "sources": [], "observations": []}
    packed = []
    for source in list(sources.values())[:20]:
        excerpt = source["text"][:2000]
        while excerpt:
            candidate = {"chunk_id": source["chunk_id"], "document_id": source["document_id"], "text": excerpt}
            trial = {**payload, "sources": [*payload["sources"], candidate]}
            if len(json.dumps(trial, ensure_ascii=False)) <= limit:
                payload = trial
                packed.append({**source, "text": excerpt, "text_truncated": source["text_truncated"] or len(excerpt) < len(source["text"])})
                break
            excerpt = excerpt[:len(excerpt) // 2] if len(excerpt) > 200 else ""
    packed_numbers = []
    for item in numbers[:24]:
        trial = {**payload, "observations": [*payload["observations"], item]}
        if len(json.dumps(trial, ensure_ascii=False)) > limit:
            break
        payload = trial
        packed_numbers.append(item)
    # Never slice serialized JSON: complete objects remain parseable at the cap.
    return json.dumps(payload, ensure_ascii=False), packed, packed_numbers


def validate_answer_citations(answer: str, sources: list[dict]) -> list[dict]:
    identifiers = set(CITATION.findall(answer))
    lookup = {source["chunk_id"]: source for source in sources}
    if identifiers - lookup.keys():
        raise ValueError("Generated answer contains an unknown citation")
    if sources and not identifiers:
        raise ValueError("Generated answer is missing evidence citations")
    return [source for source in sources if source["chunk_id"] in identifiers]
