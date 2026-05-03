# 🍪 Cookies Stopped Working - How to Refresh

## Problem
Your YouTube cookies are no longer authenticating for members-only videos, even though they haven't expired yet. This is normal - YouTube periodically invalidates session cookies for security.

## Solution: Re-export Fresh Cookies

### Method 1: Using Browser Extension (Recommended)

1. **Install Cookie Export Extension:**
   - Chrome/Edge: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)

2. **Export Cookies:**
   - Go to https://www.youtube.com in your browser
   - Make sure you're logged in to your channel
   - **Important:** Watch a members-only video to ensure session is active
   - Click the extension icon
   - Click "Export" or "Download"
   - Save as `youtube_cookies_new.txt`

3. **Replace Old Cookies:**
   ```bash
   mv youtube_cookies.txt youtube_cookies_backup.txt
   mv youtube_cookies_new.txt youtube_cookies.txt
   ```

4. **Verify Cookies Work:**
   ```bash
   python local_workflow.py MpE_jUZmWqE --members --dry-run
   ```

### Method 2: Manual Export with yt-dlp

```bash
# Let yt-dlp authenticate and save cookies
yt-dlp --cookies-from-browser chrome --cookies youtube_cookies_new.txt "https://www.youtube.com/watch?v=MpE_jUZmWqE"

# Replace old cookies
mv youtube_cookies.txt youtube_cookies_backup.txt
mv youtube_cookies_new.txt youtube_cookies.txt
```

### Method 3: Browser DevTools (Advanced)

1. Open YouTube in your browser
2. Watch a members-only video
3. Open DevTools (F12)
4. Go to Application → Cookies → https://www.youtube.com
5. Export all cookies to Netscape format
6. Save as `youtube_cookies.txt`

## Why This Happens

YouTube invalidates session cookies for security:
- After a certain period of inactivity
- When IP address changes significantly
- After password changes
- Periodically for members-only content (extra security)

## Prevention

Set a reminder to refresh cookies every **2-3 weeks** for members-only content access.

## After Refreshing

Once you have fresh cookies, your batch processing should work:

```bash
# Test with dry-run first
python batch_process_videos.py --max-videos 10 --dry-run

# Then process for real
python batch_process_videos.py --max-videos 100
```
