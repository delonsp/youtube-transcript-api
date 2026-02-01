#!/usr/bin/env python3
"""
Download transcripts using YouTube Data API directly (captions endpoint).
This bypasses the rate limiting on transcript scraping.
"""

import os
import pickle
import json
import time
import re
from datetime import datetime
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

SCOPES = [
    'https://www.googleapis.com/auth/youtube.force-ssl',
    'https://www.googleapis.com/auth/youtube.readonly'
]

def authenticate():
    """Authenticate with broader YouTube scopes for captions access."""
    creds = None
    token_file = 'token_captions.pickle'
    
    if os.path.exists(token_file):
        with open(token_file, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secrets.json', SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        with open(token_file, 'wb') as token:
            pickle.dump(creds, token)
    
    return build('youtube', 'v3', credentials=creds)

def get_channel_videos(youtube, max_results=100):
    """Get videos from authenticated user's channel (excludes scheduled/live videos)."""
    print("🔍 Fetching videos from your channel...")

    # Get uploads playlist
    channels = youtube.channels().list(
        part='contentDetails',
        mine=True
    ).execute()

    uploads_playlist = channels['items'][0]['contentDetails']['relatedPlaylists']['uploads']

    all_video_ids = []
    next_page = None

    # First, get all video IDs from playlist
    while len(all_video_ids) < max_results:
        response = youtube.playlistItems().list(
            part='contentDetails',
            playlistId=uploads_playlist,
            maxResults=min(50, max_results - len(all_video_ids)),
            pageToken=next_page
        ).execute()

        for item in response.get('items', []):
            all_video_ids.append(item['contentDetails']['videoId'])

        next_page = response.get('nextPageToken')
        if not next_page:
            break

        print(f"   Found {len(all_video_ids)} videos...")

    # Now get video details to filter out scheduled/live videos
    videos = []
    for i in range(0, len(all_video_ids), 50):  # Process in batches of 50
        batch_ids = all_video_ids[i:i+50]
        video_response = youtube.videos().list(
            part='snippet,liveStreamingDetails',
            id=','.join(batch_ids)
        ).execute()

        for item in video_response.get('items', []):
            snippet = item['snippet']
            live_status = snippet.get('liveBroadcastContent', 'none')

            # Skip upcoming (scheduled) and currently live videos
            if live_status in ['upcoming', 'live']:
                print(f"   ⏭️ Skipping scheduled/live: {snippet['title'][:40]}...")
                continue

            videos.append({
                'video_id': item['id'],
                'title': snippet['title'],
                'published_at': snippet['publishedAt']
            })

    print(f"✅ Total videos found: {len(videos)} (filtered from {len(all_video_ids)})")
    return videos

def get_captions_list(youtube, video_id):
    """Get list of available captions for a video."""
    try:
        response = youtube.captions().list(
            part='snippet',
            videoId=video_id
        ).execute()
        return response.get('items', [])
    except Exception as e:
        print(f"   ⚠️ Cannot access captions: {e}")
        return []

def download_caption(youtube, caption_id):
    """Download caption content."""
    try:
        # Download caption in SRT format
        caption = youtube.captions().download(
            id=caption_id,
            tfmt='srt'
        ).execute()
        return caption.decode('utf-8')
    except Exception as e:
        print(f"   ❌ Failed to download caption: {e}")
        return None

def parse_srt_to_text(srt_content):
    """Parse SRT content to plain text."""
    lines = srt_content.split('\n')
    text_lines = []
    
    for line in lines:
        # Skip index numbers and timestamps
        if re.match(r'^\d+$', line.strip()):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}', line.strip()):
            continue
        if line.strip():
            text_lines.append(line.strip())
    
    return ' '.join(text_lines)

def sanitize_filename(title):
    """Convert title to safe filename."""
    safe = re.sub(r'[<>:"/\\|?*]', '', title)
    safe = re.sub(r'\s+', '_', safe)
    return safe[:100]

def save_transcript(output_dir, video, content, language):
    """Save transcript to files."""
    date_str = video['published_at'][:10]
    safe_title = sanitize_filename(video['title'])
    base_name = f"{date_str}_{video['video_id']}_{safe_title}"
    
    # Save as TXT
    txt_path = output_dir / f"{base_name}.txt"
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(f"Title: {video['title']}\n")
        f.write(f"Video ID: {video['video_id']}\n")
        f.write(f"URL: https://youtube.com/watch?v={video['video_id']}\n")
        f.write(f"Published: {video['published_at']}\n")
        f.write(f"Language: {language}\n")
        f.write("-" * 60 + "\n\n")
        f.write(parse_srt_to_text(content))
    
    # Save as SRT
    srt_path = output_dir / f"{base_name}.srt"
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return base_name

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Download transcripts via YouTube API')
    parser.add_argument('--max', type=int, default=50, help='Max videos to process')
    parser.add_argument('--output', default='./transcripts', help='Output directory')
    parser.add_argument('--delay', type=float, default=1.0, help='Delay between requests (seconds)')
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load progress
    progress_file = output_dir / '.progress_api.json'
    downloaded_ids = set()
    if progress_file.exists():
        with open(progress_file, 'r') as f:
            downloaded_ids = set(json.load(f).get('downloaded', []))
        print(f"📂 Resuming: {len(downloaded_ids)} already downloaded")
    
    print("=" * 60)
    print("YOUTUBE CAPTIONS API DOWNLOADER")
    print("=" * 60)
    
    youtube = authenticate()
    print("✅ Authenticated")
    
    videos = get_channel_videos(youtube, args.max)
    
    # Filter already downloaded
    videos_to_process = [v for v in videos if v['video_id'] not in downloaded_ids]
    print(f"\n📥 Processing {len(videos_to_process)} videos (skipping {len(videos) - len(videos_to_process)} already done)")
    
    success = 0
    failed = 0
    no_captions = 0
    
    for i, video in enumerate(videos_to_process, 1):
        print(f"\n[{i}/{len(videos_to_process)}] {video['title'][:50]}...")
        
        # Get available captions
        captions = get_captions_list(youtube, video['video_id'])
        
        if not captions:
            print("   ⚠️ No captions available")
            no_captions += 1
            time.sleep(args.delay)
            continue
        
        # Find Portuguese or English caption
        target_caption = None
        for cap in captions:
            lang = cap['snippet']['language']
            if lang in ['pt', 'pt-BR']:
                target_caption = cap
                break
        
        if not target_caption:
            for cap in captions:
                lang = cap['snippet']['language']
                if lang in ['en', 'en-US']:
                    target_caption = cap
                    break
        
        if not target_caption and captions:
            target_caption = captions[0]  # Use first available
        
        if target_caption:
            content = download_caption(youtube, target_caption['id'])
            if content:
                lang = target_caption['snippet']['language']
                filename = save_transcript(output_dir, video, content, lang)
                downloaded_ids.add(video['video_id'])
                
                # Save progress
                with open(progress_file, 'w') as f:
                    json.dump({'downloaded': list(downloaded_ids)}, f)
                
                print(f"   ✅ Saved: {filename}")
                success += 1
            else:
                failed += 1
        else:
            no_captions += 1
        
        time.sleep(args.delay)
    
    print("\n" + "=" * 60)
    print("COMPLETE")
    print("=" * 60)
    print(f"✅ Success: {success}")
    print(f"❌ Failed: {failed}")
    print(f"⚠️ No captions: {no_captions}")
    print(f"📂 Output: {output_dir.absolute()}")

if __name__ == '__main__':
    main()
