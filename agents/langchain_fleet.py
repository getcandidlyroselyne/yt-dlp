"""
LangChain multi-agent fleet for the AI News Digest pipeline.

Three specialized agents run in parallel, each connected to the yt-dlp MCP server:

  IngestionAgent   — validates sources, fetches metadata and playlist items
  TranscriptAgent  — extracts transcripts and checks quality
  DigestAgent      — filters, scores, formats, and assembles the Slack digest

Each agent is a LangChain ReAct agent backed by the MCP tools. The fleet
supervisor runs them concurrently via asyncio and collects their outputs.

Usage:
    python3 -m agents.langchain_fleet

Environment variables:
    OPENAI_API_KEY        — required (used by the LLM backbone)
    YTDLP_MCP_HOST        — MCP server host (default: 127.0.0.1)
    YTDLP_MCP_PORT        — MCP server port (default: 8000)
    YTDLP_MCP_TRANSPORT   — stdio | streamable-http (default: streamable-http)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

_MCP_HOST = os.environ.get("YTDLP_MCP_HOST", "127.0.0.1")
_MCP_PORT = os.environ.get("YTDLP_MCP_PORT", "8000")
_MCP_TRANSPORT = os.environ.get("YTDLP_MCP_TRANSPORT", "streamable-http")
_MCP_URL = f"http://{_MCP_HOST}:{_MCP_PORT}/mcp"

_MCP_CONFIG = {
    "yt-dlp": {
        "url": _MCP_URL,
        "transport": _MCP_TRANSPORT,
    }
}

_LLM = ChatOpenAI(model="gpt-4o", temperature=0)

# Tool subsets each agent is allowed to use
_INGESTION_TOOLS = {"validate_source", "get_source_metadata", "get_playlist_items", "list_formats"}
_TRANSCRIPT_TOOLS = {"get_video_transcript", "get_podcast_stream_url", "get_podcast_transcript",
                     "check_transcript_quality", "extract_timestamped_segments"}
_DIGEST_TOOLS = {"keyword_filter", "score_relevance", "format_digest_item",
                 "build_digest", "check_duplicate"}


def _filter_tools(all_tools: list, allowed: set) -> list:
    return [t for t in all_tools if t.name in allowed]


# ─── Agent system prompts ───────────────────────────────────────────────────

_INGESTION_SYSTEM = """
You are the Ingestion Agent in the AI News Digest fleet.
Your job is to validate and inspect media sources before they enter the pipeline.
Use validate_source to check reachability and source type.
Use get_source_metadata to retrieve title, uploader, duration, and upload date.
Use get_playlist_items when the source is a channel or playlist.
Use list_formats only when explicitly asked about available formats.
Return a concise structured report for each URL you process.
""".strip()

_TRANSCRIPT_SYSTEM = """
You are the Transcript Agent in the AI News Digest fleet.
Your job is to extract transcripts and assess transcript quality for each media source.
Use check_transcript_quality first to determine if a source is worth ingesting.
If quality is 'ingest' or 'ingest_with_caution':
  - For YouTube videos: call get_video_transcript — it uses no disk space.
  - For podcasts: call get_podcast_stream_url — it returns a direct audio stream URL
    with zero disk usage. Pass that stream_url to your transcription service (Whisper etc.)
    without saving the file to disk. Only fall back to get_podcast_transcript if you
    explicitly need a local file and you have confirmed disk space is available.
Use extract_timestamped_segments when timestamped bullets are required.
Always report has_transcript, recommended_action, and stream_url or transcript_url in your output.
Never call get_podcast_transcript when disk space may be limited.
""".strip()

_DIGEST_SYSTEM = """
You are the Digest Agent in the AI News Digest fleet.
Your job is to filter, score, and format content into the daily Slack digest.
Step 1: Call keyword_filter with the transcript text and source keywords.
Step 2: Call score_relevance with the text, monitoring context, and matched keywords.
Step 3: If passes_threshold is true, write 5-8 timestamped bullets summarising the content,
        then call format_digest_item to produce the Slack-ready block.
Step 4: Call check_duplicate before including any item in the final digest.
Step 5: When all items are processed, call build_digest to assemble the full Slack message.
Always include timestamps in bullets for video and podcast content per project spec.
""".strip()


# ─── Individual agent runners ────────────────────────────────────────────────

async def run_ingestion_agent(client: MultiServerMCPClient, urls: list[str]) -> str:
    tools = _filter_tools(client.get_tools(), _INGESTION_TOOLS)
    agent = create_react_agent(_LLM, tools, prompt=_INGESTION_SYSTEM)
    url_list = "\n".join(f"- {u}" for u in urls)
    result = await agent.ainvoke({
        "messages": [("user", f"Validate and inspect the following sources:\n{url_list}")]
    })
    return result["messages"][-1].content


async def run_transcript_agent(client: MultiServerMCPClient, urls: list[str]) -> str:
    tools = _filter_tools(client.get_tools(), _TRANSCRIPT_TOOLS)
    agent = create_react_agent(_LLM, tools, prompt=_TRANSCRIPT_SYSTEM)
    url_list = "\n".join(f"- {u}" for u in urls)
    result = await agent.ainvoke({
        "messages": [("user", f"Extract transcripts and assess quality for:\n{url_list}")]
    })
    return result["messages"][-1].content


async def run_digest_agent(
    client: MultiServerMCPClient,
    transcript_report: str,
    monitoring_context: str,
    keywords: list[str],
    issue_number: int,
    publish_date: str,
) -> str:
    tools = _filter_tools(client.get_tools(), _DIGEST_TOOLS)
    agent = create_react_agent(_LLM, tools, prompt=_DIGEST_SYSTEM)
    result = await agent.ainvoke({
        "messages": [(
            "user",
            f"Filter, score, and format this content into a digest.\n\n"
            f"Monitoring context: {monitoring_context}\n"
            f"Keywords: {', '.join(keywords)}\n"
            f"Issue number: {issue_number}\n"
            f"Publish date: {publish_date}\n\n"
            f"Transcript report from the Transcript Agent:\n{transcript_report}"
        )]
    })
    return result["messages"][-1].content


# ─── Fleet supervisor ────────────────────────────────────────────────────────

async def run_fleet(
    urls: list[str],
    monitoring_context: str,
    keywords: list[str],
    issue_number: int,
    publish_date: str,
) -> dict:
    """
    Run the full three-agent pipeline for a list of source URLs.
    Ingestion and Transcript agents run in parallel; Digest agent runs after.
    """
    async with MultiServerMCPClient(_MCP_CONFIG) as client:
        print("[fleet] connected to yt-dlp MCP server")

        # Ingestion + Transcript agents run in parallel
        print("[fleet] starting ingestion + transcript agents in parallel ...")
        ingestion_result, transcript_result = await asyncio.gather(
            run_ingestion_agent(client, urls),
            run_transcript_agent(client, urls),
        )
        print("[fleet] ingestion agent done")
        print("[fleet] transcript agent done")

        # Digest agent runs after transcripts are ready
        print("[fleet] starting digest agent ...")
        digest_result = await run_digest_agent(
            client,
            transcript_report=transcript_result,
            monitoring_context=monitoring_context,
            keywords=keywords,
            issue_number=issue_number,
            publish_date=publish_date,
        )
        print("[fleet] digest agent done")

    return {
        "ingestion_report": ingestion_result,
        "transcript_report": transcript_result,
        "digest": digest_result,
    }


# ─── CLI entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    # Example run — replace with your actual sources
    result = asyncio.run(run_fleet(
        urls=[
            "https://www.youtube.com/watch?v=EXAMPLE_VIDEO_ID",
        ],
        monitoring_context="AI policy and large language model regulation",
        keywords=["LLM", "GPT", "regulation", "OpenAI", "AI policy"],
        issue_number=1,
        publish_date="2026-06-30",
    ))

    print("\n" + "=" * 60)
    print("INGESTION REPORT")
    print("=" * 60)
    print(result["ingestion_report"])

    print("\n" + "=" * 60)
    print("TRANSCRIPT REPORT")
    print("=" * 60)
    print(result["transcript_report"])

    print("\n" + "=" * 60)
    print("DIGEST OUTPUT")
    print("=" * 60)
    print(result["digest"])
