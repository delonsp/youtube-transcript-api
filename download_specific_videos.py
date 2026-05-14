"""Baixa transcripts apenas de IDs específicos (lê de missing_2023_2024.json).

Reaproveita autenticação (token_captions.pickle) e helpers do download_via_api.py.
Atualiza .progress_api.json após cada sucesso.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import download_via_api as dv  # noqa: usa authenticate, get_captions_list, etc.
from googleapiclient.errors import HttpError


def get_captions_list_safe(youtube, video_id):
    """Lista captions, mas RE-RAISE em quota/auth errors (ao invés de retornar [] como o helper original).

    Sem isso, quotaExceeded vira "sem captions" e contamina o cache.
    """
    try:
        return youtube.captions().list(part="snippet", videoId=video_id).execute().get("items", [])
    except HttpError as e:
        reason = ""
        try:
            import json as _json
            content = _json.loads(e.content.decode("utf-8"))
            reason = content.get("error", {}).get("errors", [{}])[0].get("reason", "")
        except Exception:
            pass
        if reason in ("quotaExceeded", "userRateLimitExceeded", "rateLimitExceeded"):
            raise
        if e.resp.status == 403:
            return []
        return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", default="missing.json")
    parser.add_argument("--output", default="./transcripts")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max", type=int, default=10_000)
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = output_dir / ".progress_api.json"
    no_caps_file = output_dir / ".no_captions.json"
    downloaded_ids: set[str] = set()
    if progress_file.exists():
        downloaded_ids = set(json.load(open(progress_file)).get("downloaded", []))
    no_caps: set[str] = set()
    if no_caps_file.exists():
        no_caps = set(json.load(open(no_caps_file)))

    # Also skip IDs that already have .txt files on disk (belt-and-suspenders)
    existing_on_disk: set[str] = set()
    import re
    _pat = re.compile(r"^[^_]+_([A-Za-z0-9_-]{11})_")
    for f in output_dir.glob("*.txt"):
        m = _pat.match(f.name)
        if m:
            existing_on_disk.add(m.group(1))

    todo = json.load(open(args.list))
    before = len(todo)
    todo = [
        v for v in todo
        if v["id"] not in downloaded_ids
        and v["id"] not in no_caps
        and v["id"] not in existing_on_disk
    ][: args.max]
    print(f"alvos: {len(todo)} (skip {len(downloaded_ids)} feitos + {len(no_caps)} sem legenda + {len(existing_on_disk)} em disco, filtered {before - len(todo)} redundant)")

    youtube = dv.authenticate()
    print("autenticado")

    success = 0
    failed = 0
    new_no_caps = 0

    for i, v in enumerate(todo, 1):
        video = {
            "video_id": v["id"],
            "title": v["title"],
            "published_at": v["published_at"],
        }
        print(f"[{i}/{len(todo)}] {video['title'][:60]}")
        try:
            caps = get_captions_list_safe(youtube, video["video_id"])
        except HttpError as e:
            print(f"  ⛔ quota/rate exhausted — interrompendo. ({e.resp.status})")
            break
        if not caps:
            print("  sem captions")
            no_caps.add(video["video_id"])
            new_no_caps += 1
            with open(no_caps_file, "w") as fh:
                json.dump(sorted(no_caps), fh)
            time.sleep(args.delay)
            continue
        target = None
        for c in caps:
            if c["snippet"]["language"] in ("pt", "pt-BR"):
                target = c
                break
        if not target:
            for c in caps:
                if c["snippet"]["language"] in ("en", "en-US"):
                    target = c
                    break
        if not target:
            target = caps[0]
        content = dv.download_caption(youtube, target["id"])
        if not content:
            failed += 1
            time.sleep(args.delay)
            continue
        lang = target["snippet"]["language"]
        dv.save_transcript(output_dir, video, content, lang)
        downloaded_ids.add(video["video_id"])
        with open(progress_file, "w") as fh:
            json.dump({"downloaded": sorted(downloaded_ids)}, fh)
        success += 1
        print(f"  ok ({lang})")
        time.sleep(args.delay)

    print(f"\nfim: ok={success} no_caps={new_no_caps} failed={failed} of {len(todo)}")


if __name__ == "__main__":
    main()
