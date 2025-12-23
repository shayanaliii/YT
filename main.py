from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, HttpUrl
from contextlib import asynccontextmanager
from starlette.background import BackgroundTask
import yt_dlp
import os
import uuid
import tempfile
import shutil
from pathlib import Path
import asyncio
from datetime import datetime

# 1. Use System Temp Directory
# This creates a hidden temp folder so you don't see the "duplicate" file
# The final file will only appear in your Windows Downloads folder via your Browser
TEMP_DIR = Path(tempfile.gettempdir()) / "yt_dlp_temp"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cleanup on startup"""
    # Create temp directory if not exists
    TEMP_DIR.mkdir(exist_ok=True)
    
    # Clean up old temp files on startup
    for file in TEMP_DIR.glob("*"):
        try:
            if file.is_file():
                file.unlink()
        except:
            pass
    yield
    # Cleanup on shutdown (optional)
    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass

app = FastAPI(title="YouTube Downloader - Ultra Fast", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "null"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_SIZE = 2000 * 1024 * 1024  # Increased to 2GB

downloads_db = {}

class AnalyzeRequest(BaseModel):
    url: HttpUrl

class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str
    output_format: str

def cleanup_file(path: str):
    """Function to delete file after browser downloads it"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Cleanup error: {e}")

@app.get("/")
async def root():
    return {"status": "Ultra Fast YouTube Downloader", "version": "3.1"}

@app.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest):
    """OPTIMIZED: Only extract useful formats"""
    
    def extract_info():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': 10,
            'noplaylist': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(str(request.url), download=False)
            
            useful_formats = []
            seen = set()
            target_heights = [144, 240, 360, 480, 720, 1080]
            
            for f in info.get('formats', []):
                height = f.get('height')
                ext = f.get('ext', 'mp4')
                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                
                if height not in target_heights:
                    continue
                
                if ext == 'mp4' and vcodec != 'none' and acodec != 'none':
                    key = f"{height}p"
                    if key in seen:
                        continue
                    seen.add(key)
                    
                    useful_formats.append({
                        'format_id': f.get('format_id'),
                        'ext': 'mp4',
                        'resolution': f"{height}p",
                        'filesize': f.get('filesize', 0),
                        'format_note': '⚡ Fast (video+audio)',
                        'has_video': True,
                        'has_audio': True,
                    })
            
            for f in info.get('formats', []):
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    if f.get('ext') in ['m4a', 'webm']:
                        useful_formats.append({
                            'format_id': f.get('format_id'),
                            'ext': f.get('ext', 'm4a'),
                            'resolution': 'audio only',
                            'filesize': f.get('filesize', 0),
                            'format_note': '🎵 Audio only',
                            'has_video': False,
                            'has_audio': True,
                        })
                        break
            
            return {
                'success': True,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'formats': useful_formats[:8]
            }
    
    try:
        result = await run_in_threadpool(extract_info)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")

async def download_video_task(url: str, format_id: str, output_format: str, download_id: str):
    
    def download():
        try:
            # Download to TEMP_DIR with UUID name
            # We don't rename here. We let the FileResponse handle the real filename later.
            output_template = f"{TEMP_DIR}/{download_id}.%(ext)s"
            
            ydl_opts = {
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'concurrent_fragment_downloads': 4,
                'http_chunk_size': 10485760,
                'socket_timeout': 30,
                'retries': 3,
            }
            
            if output_format == 'mp4':
                ydl_opts['format'] = f"{format_id}/best[ext=mp4]/best"
            elif output_format == 'm4a':
                ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio'
            elif output_format == 'webm':
                ydl_opts['format'] = 'bestaudio[ext=webm]/bestaudio'
            elif output_format == 'mp3':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            
            def progress_hook(d):
                if d['status'] == 'downloading':
                    percent = d.get('_percent_str', '0%').strip()
                    speed = d.get('_speed_str', '').strip()
                    downloads_db[download_id]['progress'] = percent
                    downloads_db[download_id]['speed'] = speed
                    print(f"Download {download_id}: {percent} @ {speed}")
            
            ydl_opts['progress_hooks'] = [progress_hook]
            
            downloads_db[download_id]['status'] = 'downloading'
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'video')
            
            # Find the file in TEMP folder
            actual_file = next(TEMP_DIR.glob(f"{download_id}.*"), None)
            
            if not actual_file:
                downloads_db[download_id]['status'] = 'failed'
                downloads_db[download_id]['error'] = 'Download failed'
                return
            
            file_size = actual_file.stat().st_size
            
            if file_size > MAX_FILE_SIZE:
                actual_file.unlink()
                downloads_db[download_id]['status'] = 'failed'
                downloads_db[download_id]['error'] = 'File too large'
                return

            # Clean the filename for the Browser to use
            sanitized_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ' or c=='-']).strip()
            final_filename = f"{sanitized_title}{actual_file.suffix}"

            # Success
            downloads_db[download_id].update({
                'status': 'completed',
                'filepath': str(actual_file),     # Path is in Temp
                'filename': final_filename,       # Name for the browser
                'filesize': file_size,
                'progress': '100%'
            })
            
        except Exception as e:
            downloads_db[download_id]['status'] = 'failed'
            downloads_db[download_id]['error'] = str(e)
            print(f"Download error: {e}")
    
    await run_in_threadpool(download)
    
    # Auto-cleanup logic (safety net)
    await asyncio.sleep(3600) 
    try:
        if download_id in downloads_db:
            path = downloads_db[download_id].get('filepath')
            if path and os.path.exists(path):
                os.remove(path)
            del downloads_db[download_id]
    except:
        pass

@app.post("/api/download")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    download_id = str(uuid.uuid4())
    
    downloads_db[download_id] = {
        'status': 'pending',
        'progress': '0%',
        'speed': '',
        'created_at': datetime.now(),
    }
    
    background_tasks.add_task(
        download_video_task,
        str(request.url),
        request.format_id,
        request.output_format,
        download_id
    )
    
    return {
        'success': True,
        'download_id': download_id,
        'message': 'Download started'
    }

@app.get("/api/status/{download_id}")
async def get_download_status(download_id: str):
    if download_id not in downloads_db:
        raise HTTPException(status_code=404, detail="Download not found")
    
    info = downloads_db[download_id]
    return {
        'download_id': download_id,
        'status': info.get('status'),
        'progress': info.get('progress', '0%'),
        'speed': info.get('speed', ''),
        'filename': info.get('filename', ''),
        'filesize': info.get('filesize', 0),
        'error': info.get('error', None)
    }

@app.get("/api/file/{download_id}")
async def get_file(download_id: str):
    """Serve file and delete from temp immediately after"""
    if download_id not in downloads_db:
        raise HTTPException(status_code=404, detail="File not found or expired")
    
    info = downloads_db[download_id]
    
    if info.get('status') != 'completed':
        raise HTTPException(
            status_code=400, 
            detail=f"Download not ready. Status: {info.get('status')}"
        )
    
    filepath = info.get('filepath')
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File expired")
    
    # CRITICAL FIX: 
    # 1. We serve the file from the hidden TEMP folder.
    # 2. We tell the browser the real 'filename' (Video Title.mp4).
    # 3. We use BackgroundTask to delete the temp file immediately after sending.
    # Result: Browser saves 1 copy to Downloads. Temp copy is deleted.
    
    return FileResponse(
        filepath,
        media_type='application/octet-stream',
        filename=info['filename'],
        background=BackgroundTask(cleanup_file, filepath)
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)