#!/usr/bin/env python3
"""Run real HTTP MCP fixture acceptance; output metadata only, never JWTs."""
import argparse
import asyncio
import json
import hashlib
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def data(result):
    if getattr(result, "isError", False):
        raise AssertionError("MCP tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", []):
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise AssertionError("No structured tool response")


async def run(args):
    # This harness only signs for synthetic local testing; never send tokens elsewhere.
    p = urlsplit(args.endpoint)
    if p.scheme != "http" or p.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Synthetic smoke tokens may only target loopback HTTP")
    identity = args.identity.resolve()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "brain-auth-smoke", "version": "1"}}}
    failures = []
    cases = {}
    async with httpx2.AsyncClient(timeout=30, trust_env=False) as raw:
        for label in ["missing", "expired", "wrong-audience", "wrong-issuer", "unlisted-user"]:
            headers = {"Accept": "application/json, text/event-stream", "Host": "127.0.0.1:8000"}
            if label != "missing":
                headers["Authorization"] = "Bearer " + (identity / f"{label}.token").read_text().strip()
            response = await raw.post(args.endpoint, json=init, headers=headers)
            cases[label] = response.status_code
            if response.status_code not in {401, 403}:
                failures.append(f"{label} was not denied")
    bearer = (identity / "valid.token").read_text().strip()
    headers = {"Authorization": "Bearer " + bearer, "Host": "127.0.0.1:8000"}
    queries = json.loads(args.golden.read_text())
    measurements = []
    async with httpx2.AsyncClient(headers=headers, timeout=120, trust_env=False) as client:
        async with streamable_http_client(args.endpoint, http_client=client) as streams:
            async with ClientSession(*streams, read_timeout_seconds=120) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                if not {"search", "fetch"}.issubset(names):
                    failures.append("required MCP tools missing")
                matched = 0
                answered = 0
                for item in queries["queries"]:
                    if item.get("unanswerable"):
                        continue
                    started = time.monotonic()
                    result = data(await session.call_tool("search", {"query": item["query"], "limit": queries["top_k"], "collection": "pilot"}))
                    results = result.get("results", [])
                    ids = {r["id"] for r in results}
                    expected = {str(uuid.uuid5(uuid.NAMESPACE_URL, "company-brain:fixture:" + i)) for i in item["expected"]}
                    hit = bool(ids & expected)
                    answered += 1
                    matched += int(hit)
                    measurements.append({"query": item["query"], "hit": hit, "seconds": round(time.monotonic() - started, 3),
                                         "result_ids": [r["id"] for r in results]})
                recall = matched / answered
                if recall < queries["minimum_recall"]:
                    failures.append("retrieval recall below target")
                known_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "company-brain:fixture:decision-offline"))
                fetched = data(await session.call_tool("fetch", {"document_id": known_id}))
                if "Mira Test" not in fetched.get("text", "") or not fetched.get("url", "").startswith("https://example.invalid/"):
                    failures.append("known source fetch/citation failed")
    report = {"scope": "synthetic corpus via actual local HTTP MCP; not real ChatGPT/Claude acceptance",
              "corpus_sha256": hashlib.sha256(Path("fixtures/pilot.json").read_bytes()).hexdigest(),
              "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
              "embedding_revision": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
              "auth_statuses": cases, "tools": names, "retrieval_recall_at_5": recall,
              "answerable_queries": answered, "measurements": measurements,
              "unknown_answer_checks": "pending actual AI client; retrieval alone cannot prove abstention",
              "failures": failures, "passed": not failures}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "measurements"}, ensure_ascii=False))
    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--identity", type=Path, default=Path("runtime/demo/identity"))
    parser.add_argument("--golden", type=Path, default=Path("evals/golden.json"))
    parser.add_argument("--output", type=Path, default=Path("runtime/demo/acceptance.json"))
    args = parser.parse_args()
    raise SystemExit(int(asyncio.run(run(args))))


if __name__ == "__main__":
    main()
