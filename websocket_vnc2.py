#!/usr/bin/env python3
"""
Silent WebSocket VNC - A high-performance, silent VNC server using WebSockets.
Can be stopped by creating a 'stop_vnc.flag' file.
"""
import asyncio
import json
import socket
import threading
import io
import os
import sys
import time
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Set
import http.server
import functools

import websockets

# Dependency Checks
try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False
try:
    import numpy as np
    import imagecodecs
    HAS_IMAGECODECS = True
except ImportError:
    HAS_IMAGECODECS = False
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False
try:
    from PIL import Image, ImageGrab
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
try:
    import win32api, win32con, win32gui, win32ui
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

# --- Setup Logging ---
logging.basicConfig(filename='vnc_debug.log', level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    filemode='w')

# --- Configuration ---
PORT = 8090 # Single port for all traffic
QUALITY = 85
STOP_FILE = "stop_vnc.flag"

# --- Embedded HTML Client ---
HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
    <title>WebSocket VNC</title>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; background: #000; }
        #screen { width: 100%; height: 100%; object-fit: contain; cursor: crosshair; }
        #status { position: fixed; bottom: 10px; left: 10px; color: #fff; background: rgba(0,0,0,.7); padding: 5px; border-radius: 5px; font-family: monospace; }
    </style>
</head>
<body>
    <canvas id="screen"></canvas>
    <div id="status">Connecting...</div>

<script>
    const canvas = document.getElementById('screen');
    const ctx = canvas.getContext('2d');
    const statusDiv = document.getElementById('status');
    const WSS_PROTOCOL = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const WSS_URL = `${WSS_PROTOCOL}://${window.location.host}`;

    let frameCount = 0;
    let lastTime = performance.now();

    function updateStatus(text) {
        statusDiv.textContent = text;
    }

    function connectVideo() {
        const videoSocket = new WebSocket(`${WSS_URL}/video`);
        videoSocket.binaryType = 'blob';
        videoSocket.onopen = () => updateStatus('Video Connected');
        videoSocket.onmessage = (event) => {
            frameCount++;
            const image = new Image();
            image.onload = () => {
                canvas.width = image.naturalWidth;
                canvas.height = image.naturalHeight;
                ctx.drawImage(image, 0, 0);
                URL.revokeObjectURL(image.src);
            };
            image.src = URL.createObjectURL(event.data);
        };
        videoSocket.onerror = () => updateStatus('Video Error');
        videoSocket.onclose = () => {
            updateStatus('Video Lost. Retrying...');
            setTimeout(connectVideo, 2000);
        };
    }

    function connectInput() {
        const inputSocket = new WebSocket(`${WSS_URL}/input`);
        inputSocket.onopen = () => updateStatus('Input Connected');
        inputSocket.onclose = () => {
            updateStatus('Input Lost. Retrying...');
            setTimeout(connectInput, 2000);
        };

        function sendEvent(event) {
            if (inputSocket.readyState === WebSocket.OPEN) {
                inputSocket.send(JSON.stringify(event));
            }
        }

        let isDragging = false;

        function getCanvasCoordinates(event) {
            const rect = canvas.getBoundingClientRect();
            if (!canvas.width || !canvas.height) return null;
            const viewAspectRatio = rect.width / rect.height;
            const canvasAspectRatio = canvas.width / canvas.height;
            let renderWidth, renderHeight, offsetX, offsetY;
            if (viewAspectRatio > canvasAspectRatio) {
                renderHeight = rect.height;
                renderWidth = renderHeight * canvasAspectRatio;
            } else {
                renderWidth = rect.width;
                renderHeight = renderWidth / canvasAspectRatio;
            }
            offsetX = (rect.width - renderWidth) / 2;
            offsetY = (rect.height - renderHeight) / 2;
            const x = (event.clientX - rect.left - offsetX) / renderWidth;
            const y = (event.clientY - rect.top - offsetY) / renderHeight;
            if (x < 0 || x > 1 || y < 0 || y > 1) return null;
            return { x, y };
        }
        
        document.addEventListener('contextmenu', e => e.preventDefault());
        document.addEventListener('mousedown', event => {
            isDragging = true;
            const coords = getCanvasCoordinates(event);
            if (coords) sendEvent({ action: 'click', x: coords.x, y: coords.y, button: event.button === 0 ? 'left' : 'right', state: 'down' });
        });
        document.addEventListener('mouseup', event => {
            isDragging = false;
            const coords = getCanvasCoordinates(event);
            if (coords) sendEvent({ action: 'click', x: coords.x, y: coords.y, button: event.button === 0 ? 'left' : 'right', state: 'up' });
        });
        document.addEventListener('mousemove', event => {
            const coords = getCanvasCoordinates(event);
            if (coords) sendEvent({ action: isDragging ? 'drag' : 'move', x: coords.x, y: coords.y });
        });
        document.addEventListener('wheel', event => {
            event.preventDefault();
            const coords = getCanvasCoordinates(event);
            if (coords) sendEvent({ action: 'scroll', deltaY: event.deltaY });
        });
        document.addEventListener('keydown', event => {
            event.preventDefault();
            sendEvent({ action: 'key', key: event.key, state: 'down' });
        });
        document.addEventListener('keyup', event => {
            event.preventDefault();
            sendEvent({ action: 'key', key: event.key, state: 'up' });
        });
    }

    setInterval(() => {
        const elapsedSeconds = (performance.now() - lastTime) / 1000;
        if (elapsedSeconds > 0) {
            const fps = (frameCount / elapsedSeconds).toFixed(1);
            updateStatus(`Connected | ${fps} FPS`);
        }
        frameCount = 0;
        lastTime = performance.now();
    }, 1000);

    connectVideo();
    connectInput();
</script>
</body>
</html>
"""

# --- Screen Capturer Thread ---
class ScreenCapturer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.latest_frame_jpeg = None
        self.frame_lock = threading.Lock()
        self.is_running = False

    def run(self):
        self.is_running = True
        sct = None
        if HAS_MSS:
            try:
                sct = mss.mss()
            except Exception:
                sct = None
        
        while self.is_running:
            try:
                frame = self._grab_screen(sct)
                if frame:
                    jpeg_bytes = self._encode_frame(frame)
                    if jpeg_bytes:
                        with self.frame_lock:
                            self.latest_frame_jpeg = jpeg_bytes
            except Exception as e:
                logging.error("Exception in ScreenCapturer run loop", exc_info=True)
            time.sleep(1/60)
        
        if sct:
            sct.close()

    def _grab_screen(self, sct):
        if sct:
            try:
                sct_img = sct.grab(sct.monitors[0])
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            except mss.exception.ScreenShotError as e:
                logging.warning("mss.grab failed", exc_info=True)
                pass
        if WIN32_AVAILABLE:
            try:
                return self._grab_screen_win32()
            except Exception as e:
                logging.warning("win32grab failed", exc_info=True)
                pass
        if HAS_PIL:
            try:
                return ImageGrab.grab()
            except Exception as e:
                logging.warning("PIL ImageGrab.grab failed", exc_info=True)
                pass
        logging.error("All screen capture methods failed.")
        return None

    def _grab_screen_win32(self):
        hdesktop = win32gui.GetDesktopWindow()
        width = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        height = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        desktop_dc_handle, mem_dc, bmp = None, None, None
        try:
            desktop_dc_handle = win32gui.GetWindowDC(hdesktop)
            desktop_dc = win32ui.CreateDCFromHandle(desktop_dc_handle)
            mem_dc = desktop_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(desktop_dc, width, height)
            mem_dc.SelectObject(bmp)
            mem_dc.BitBlt((0, 0), (width, height), desktop_dc, (left, top), win32con.SRCCOPY)
            signed_ints_array = bmp.GetBitmapBits(True)
            return Image.frombuffer('RGB', (width, height), signed_ints_array, 'raw', 'BGRX', 0, 1)
        finally:
            if bmp and bmp.GetHandle(): win32gui.DeleteObject(bmp.GetHandle())
            if mem_dc and mem_dc.GetSafeHdc(): mem_dc.DeleteDC()
            if desktop_dc and desktop_dc.GetSafeHdc(): desktop_dc.DeleteDC()
            if desktop_dc_handle: win32gui.ReleaseDC(hdesktop, desktop_dc_handle)

    def _encode_frame(self, frame):
        try:
            if HAS_IMAGECODECS:
                return imagecodecs.jpeg_encode(np.array(frame), level=QUALITY)
            else:
                buffer = io.BytesIO()
                frame.save(buffer, format='JPEG', quality=QUALITY)
                return buffer.getvalue()
        except Exception as e:
            logging.error("Failed to encode frame", exc_info=True)
            return None

    def get_frame(self):
        with self.frame_lock:
            return self.latest_frame_jpeg

    def stop(self):
        self.is_running = False

# --- WebSocket Server ---
class VNCServer:
    def __init__(self, capturer: ScreenCapturer):
        self.capturer = capturer
        self.video_clients: Set[websockets.WebSocketServerProtocol] = set()
        self.stop_event = asyncio.Event()
        self.loop = None

    async def main_handler(self, websocket: websockets.WebSocketServerProtocol):
        """Handles incoming connections and routes them."""
        # This handler is now only for actual WebSocket connections.
        # The path routing is implicitly handled by how the client connects.
        if websocket.path == "/video":
            await self.video_stream_handler(websocket)
        elif websocket.path == "/input":
            await self.input_event_handler(websocket)

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        asyncio.create_task(self.broadcast_frames())

        # With websockets 11, process_request is the way to handle HTTP.
        async def process_request(path, request_headers):
            if request_headers.get("Upgrade") != "websocket":
                if path == '/':
                    return http.HTTPStatus.OK, {"Content-Type": "text/html"}, HTML_CONTENT.encode()
                else:
                    return http.HTTPStatus.NOT_FOUND, {}, b"Not Found"
            # If it is a websocket upgrade, return None to let websockets handle it.
            return None

        server = await websockets.serve(
            self.main_handler,
            "0.0.0.0",
            PORT,
            process_request=process_request,
        )
        await self.stop_event.wait()
        server.close()
        await server.wait_closed()

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    async def video_stream_handler(self, websocket: websockets.WebSocketServerProtocol):
        self.video_clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self.video_clients.remove(websocket)

    async def broadcast_frames(self):
        while not self.stop_event.is_set():
            frame = self.capturer.get_frame()
            if frame and self.video_clients:
                try:
                    await asyncio.gather(*[ws.send(frame) for ws in self.video_clients])
                except websockets.exceptions.ConnectionClosed:
                    pass
            await asyncio.sleep(1/60)

    async def input_event_handler(self, websocket: websockets.WebSocketServerProtocol):
        async for message in websocket:
            try:
                event = json.loads(message)
                self.process_event(event)
            except Exception:
                pass

    def process_event(self, event):
        action = event.get('action')
        try:
            if action in ['click', 'move', 'drag']:
                self._handle_mouse_event(event)
            elif action == 'key':
                self._handle_key_event(event)
            elif action == 'scroll':
                self._handle_scroll_event(event)
        except Exception:
            pass

    def _handle_mouse_event(self, event):
        x, y = event['x'], event['y']
        try:
            screen_width, screen_height = pyautogui.size()
        except Exception:
            screen_width, screen_height = 1920, 1080
        target_x = max(0, min(int(x * screen_width), screen_width - 1))
        target_y = max(0, min(int(y * screen_height), screen_height - 1))
        pyautogui.moveTo(target_x, target_y, duration=0)
        if event['action'] == 'click':
            pyautogui.mouseDown(button=event['button']) if event['state'] == 'down' else pyautogui.mouseUp(button=event['button'])

    def _handle_key_event(self, event):
        pyautogui.keyDown(event['key']) if event['state'] == 'down' else pyautogui.keyUp(event['key'])

    def _handle_scroll_event(self, event):
        pyautogui.scroll(int(event['deltaY'] * -1))

# --- Stop Signal Handler ---
def stop_signal_handler(vnc_server, capturer):
    while not vnc_server.stop_event.is_set():
        if os.path.exists(STOP_FILE):
            try:
                capturer.stop()
                vnc_server.stop()
                os.remove(STOP_FILE)
            except Exception:
                pass
            break
        time.sleep(1)

# --- Main ---
def main():
    if not (HAS_PYAUTOGUI and (HAS_MSS or WIN32_AVAILABLE or HAS_PIL)):
        # Consider logging this failure
        sys.exit(1)

    capturer = ScreenCapturer()
    capturer.start()
    
    vnc_server = VNCServer(capturer)

    stop_thread = threading.Thread(target=stop_signal_handler, args=(vnc_server, capturer), daemon=True)
    stop_thread.start()

    try:
        asyncio.run(vnc_server.serve())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        capturer.stop()


if __name__ == "__main__":
    main() 