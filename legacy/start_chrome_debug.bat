@echo off
REM Launch a real Chrome with remote debugging for fallback crawling mode.
REM After it opens, log in to Xiaohongshu manually, then set crawl.cdp_url
REM in config.yaml to http://127.0.0.1:9222
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found.
  pause
  exit /b 1
)
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\xhs-cdp-profile" https://www.xiaohongshu.com/explore
echo Chrome started with debugging port 9222.
echo Log in manually, then set crawl.cdp_url to http://127.0.0.1:9222
