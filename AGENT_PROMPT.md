# Connected Agent Prompt — yt-dlp Fleet

Copy and paste the block below as your starting prompt to any Cursor background agent
that needs to ingest a source, produce a digest item, or monitor the fleet.

---

## Prompt: Ingest a single source and produce a digest item

```
You are connected to the yt-dlp MCP server. Your job is to ingest one media source,
score it, and return a formatted digest item ready to post to Slack.

TOOLS AVAILABLE (via MCP):
  Async ingestion (returns job_id immediately — you must poll):
    get_video_transcript(url, language="en")
    get_podcast_transcript(url, audio_format="mp3")
    get_source_metadata(url)
    check_transcript_quality(url)
    get_playlist_items(playlist_url, max_items=20)
    validate_source(url)

  Job coordination:
    get_job_status(job_id)         ← poll this every 10s until status == "done"
    list_queued_jobs(status="")    ← inspect fleet health
    retry_failed_job(job_id)       ← manually requeue a failed job

  Sync (run immediately, no polling needed):
    keyword_filter(text, keywords, monitoring_context)
    score_relevance(text, monitoring_context, keyword_matches, source_title)
    format_digest_item(title, source_url, uploader, source_type, bullets,
                       relevance_score, monitoring_context, upload_date, duration_seconds)
    check_duplicate(item_url, item_title, post_log)
    build_digest(items, issue_number, publish_date, max_items=10)

INSTRUCTIONS:
1. Call the appropriate ingestion tool for the URL below. It returns {job_id} immediately.
2. Poll get_job_status(job_id) every 10 seconds until status == "done" or "failed".
3. If "failed", call retry_failed_job(job_id) and resume polling.
4. Once done, read result.transcript_url (for video) or result.output_dir (for audio).
5. Fetch or read the transcript text content from that URL/path.
6. Call keyword_filter(text, keywords=[...], monitoring_context="<context>").
7. Call score_relevance(text, monitoring_context, keyword_matches=[...], source_title="<title>").
8. If passes_threshold is true, write 5–8 bullets summarising the content with timestamps.
9. Call format_digest_item(...) with those bullets.
10. Return the slack_formatted_block field as your final output.

SOURCE TO INGEST:
  url: <PASTE URL HERE>
  monitoring_context: <PASTE CONTEXT HERE e.g. "AI policy and regulation">
  keywords: [<PASTE KEYWORDS HERE e.g. "LLM", "GPT", "regulation", "OpenAI">]
  source_type: video | podcast   (choose one)
```

---

## Prompt: Run a full daily digest (batch of sources)

```
You are connected to the yt-dlp MCP server. Produce today's Daily Crumb digest.

SOURCES (add all URLs here):
  - url: <URL_1>  type: video   context: "AI policy"   keywords: ["AI", "regulation"]
  - url: <URL_2>  type: podcast context: "LLM tooling"  keywords: ["agent", "RAG"]
  - url: <URL_3>  type: video   context: "startup news" keywords: ["funding", "Series A"]

STEPS:
1. For each source, call the appropriate ingestion tool (get_video_transcript or
   get_podcast_transcript). Collect all job_ids.
2. Poll all jobs in parallel using get_job_status(job_id) every 15 seconds until
   every job is "done" or "failed". Retry any failed jobs with retry_failed_job.
3. For each completed job, fetch the transcript, run keyword_filter then score_relevance.
4. Keep only items where passes_threshold == true.
5. For each passing item, write 5–8 timestamped bullets and call format_digest_item.
6. Call build_digest(items=[...], issue_number=<N>, publish_date="<YYYY-MM-DD>").
7. Return the slack_message field as your final output.
```

---

## Prompt: Monitor fleet health

```
You are connected to the yt-dlp MCP server. Check the health of the agent fleet.

1. Call list_queued_jobs(status="queued")  — report how many jobs are waiting.
2. Call list_queued_jobs(status="running") — report jobs currently being processed.
3. Call list_queued_jobs(status="failed")  — list any failed jobs with their errors.
4. For each failed job, call retry_failed_job(job_id) to requeue it.
5. Call purge_completed_jobs(older_than_seconds=3600) to clean up old done jobs.
6. Report a one-paragraph fleet health summary.
```
