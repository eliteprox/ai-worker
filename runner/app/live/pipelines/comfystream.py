import os
import json
import asyncio
import logging
import aiohttp
import subprocess
import sys
from typing import Union, Optional, Dict, Any
from pydantic import BaseModel, field_validator
import pathlib
from fractions import Fraction
from PIL import Image

from .interface import Pipeline
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, VideoStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay
from trickle import VideoFrame, AudioFrame, VideoOutput, AudioOutput, DEFAULT_WIDTH, DEFAULT_HEIGHT
import av
import numpy as np
import torch
import time

WARMUP_RUNS = 1
# Connection retry configuration - loosened for better WebRTC compatibility
MAX_RETRY_ATTEMPTS = 5  # Increased for WebRTC reliability
RETRY_DELAY_SECONDS = 2.0  # Reduced back for faster initial attempts
HEALTH_CHECK_INTERVAL = 30.0  # Back to more frequent health checks
CONNECTION_STABLE_THRESHOLD = 15.0  # Reduced - consider stable sooner
RECONNECTION_BACKOFF_MAX = 30.0  # Reduced maximum backoff time

# Connection state debouncing - much more aggressive for WebRTC
CONNECTION_STATE_DEBOUNCE_SECONDS = 1.0  # Reduced significantly for WebRTC responsiveness

_default_workflow_path = pathlib.Path(__file__).parent.absolute() / "comfyui_default_workflow.json"
with open(_default_workflow_path, 'r') as f:
    DEFAULT_WORKFLOW_JSON = json.load(f)


class ComfyStreamParams(BaseModel):
    class Config:
        extra = "forbid"

    prompt: Union[str, dict] = DEFAULT_WORKFLOW_JSON
    comfystream_url: str = "http://localhost:8889"
    auto_start_server: bool = False
    comfystream_workspace: Optional[str] = None
    comfystream_host: str = "localhost"
    comfystream_port: int = 8889
    comfystream_warm_pipeline: bool = False
    
    # NOTE: Dimensions must be maintained with the workflow dimensions and is shared with other pipelines
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT

    @field_validator('prompt')
    @classmethod
    def validate_prompt(cls, v) -> dict:
        if v == "":
            return DEFAULT_WORKFLOW_JSON

        if isinstance(v, dict):
            return v

        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if not isinstance(parsed, dict):
                    raise ValueError("Parsed prompt JSON must be a dictionary/object")
                return parsed
            except json.JSONDecodeError:
                raise ValueError("Provided prompt string must be valid JSON")

        raise ValueError("Prompt must be either a JSON object or such JSON object serialized as a string")


class FramePublisher(VideoStreamTrack):
    """Custom video track that publishes frames to WHIP endpoint"""
    
    def __init__(self, timestamp_generator=None, webrtc_time_base=None):
        super().__init__()
        self.frame_queue = asyncio.Queue()
        self.running = True
        self.timestamp_generator = timestamp_generator
        self.webrtc_time_base = webrtc_time_base or Fraction(1, 90000)
    
    async def recv(self):
        """Receive frames from the queue to send via WHIP"""
        if not self.running:
            raise Exception("Track stopped")
        
        try:
            frame = await asyncio.wait_for(self.frame_queue.get(), timeout=1.0)
            return frame
        except asyncio.TimeoutError:
            # Return a dummy frame to keep the connection alive
            return self._create_dummy_frame()
    
    def _create_dummy_frame(self):
        """Create a dummy frame to keep WebRTC connection alive"""
        # Create dummy frame with proper format (use default dimensions)
        dummy_array = np.zeros((480, 640, 3), dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(dummy_array, format='rgb24')
        # Use sequential timestamp instead of wall-clock time
        if self.timestamp_generator:
            frame.pts = self.timestamp_generator()
        else:
            # Fallback to simple incrementing timestamp
            frame.pts = int(time.time() * 90000)
        frame.time_base = self.webrtc_time_base
        return frame
    
    async def add_frame(self, frame):
        """Add a frame to be published"""
        if self.running:
            await self.frame_queue.put(frame)
    
    def stop(self):
        """Stop the track"""
        self.running = False


class ComfyStream(Pipeline):
    def __init__(self):
        self.params: ComfyStreamParams
        self.video_incoming_frames: asyncio.Queue[VideoOutput] = asyncio.Queue()
        self.video_processed_frames: asyncio.Queue[VideoOutput] = asyncio.Queue()
        

        
        # WHIP publisher components
        self.whip_pc: Optional[RTCPeerConnection] = None
        self.whip_session: Optional[aiohttp.ClientSession] = None
        self.whip_resource_url: Optional[str] = None
        self.frame_publisher: Optional[FramePublisher] = None
        self.whip_connected = False
        
        # WHEP subscriber components
        self.whep_pc: Optional[RTCPeerConnection] = None
        self.whep_session: Optional[aiohttp.ClientSession] = None
        self.whep_resource_url: Optional[str] = None
        self.whep_connected = False
        
        # Server management
        self.server_process: Optional[subprocess.Popen] = None
        self.server_started_by_us: bool = False
        self.server_healthy: bool = False
        
        # Connection retry state
        self.connection_retry_count: int = 0
        self.last_connection_attempt: Optional[float] = None
        self.consent_expired_count: int = 0
        
        # Connection stability tracking
        self.connection_established_time: Optional[float] = None
        self.last_connection_state_change: Optional[float] = None
        self.connection_stable: bool = False
        self.reconnection_backoff: float = RETRY_DELAY_SECONDS
        
        # Frame processing success tracking
        self.last_successful_frame_time: Optional[float] = None
        self.frames_processed_successfully: int = 0
        
        # State change debouncing
        self.pending_whip_state_change: Optional[tuple] = None  # (state, timestamp)
        self.pending_whep_state_change: Optional[tuple] = None  # (state, timestamp)
        
        self.is_initialized = False
        self.processing_task: Optional[asyncio.Task] = None
        self.health_check_task: Optional[asyncio.Task] = None
        
        # Timestamp management (following encoder pattern)
        self.webrtc_time_base = Fraction(1, 90000)  # 90kHz standard for WebRTC
        self.frame_sequence = 0
        self.last_frame_time: Optional[float] = None
        
        # Server-side A/V sync compensation
        self.input_timestamp_buffer = {}  # Track when frames were sent to server
        self.processing_delay_estimate = 0.0  # Estimated processing delay
        
        # Frame processing optimization
        self.processing_optimization_enabled = True

    def _rescale_timestamp(self, pts: int, orig_tb: Fraction, dest_tb: Fraction) -> int:
        """Rescale timestamp from one time base to another (following encoder pattern)"""
        if orig_tb == dest_tb:
            return pts
        return int(round(float((Fraction(pts) * orig_tb) / dest_tb)))

    def _generate_sequential_timestamp(self) -> int:
        """Generate sequential timestamp for consistent timing (30fps target)"""
        self.frame_sequence += 1
        # Generate timestamp for 30fps (33.33ms per frame)
        return int(self.frame_sequence * (90000 / 30))  # 90kHz time base

    def _update_connection_stability(self):
        """Update connection stability tracking."""
        current_time = time.time()
        
        if self.whip_connected and self.whep_connected:
            if not self.connection_established_time:
                self.connection_established_time = current_time
                self.connection_stable = False
                logging.info("Both WebRTC connections established, starting stability timer")
            elif current_time - self.connection_established_time > CONNECTION_STABLE_THRESHOLD:
                if not self.connection_stable:
                    self.connection_stable = True
                    logging.info("WebRTC connections are now stable")
                    # Reset reconnection backoff on stable connection
                    self.reconnection_backoff = RETRY_DELAY_SECONDS
        else:
            # Reset stability immediately when connections are lost
            self.connection_established_time = None
            self.connection_stable = False

    def _should_attempt_reconnection(self) -> bool:
        """Determine if we should attempt reconnection based on stability and backoff."""
        current_time = time.time()
        
        # If we've processed frames successfully very recently, be more conservative about reconnecting
        if (self.last_successful_frame_time and 
            current_time - self.last_successful_frame_time < 5.0 and
            self.frames_processed_successfully > 0):
            logging.debug("Recent successful frame processing, being conservative about reconnection")
            # Still check backoff but be more lenient
            if (self.last_connection_attempt and 
                current_time - self.last_connection_attempt < self.reconnection_backoff * 2):
                return False
        
        # For ICE connection failures, allow more aggressive reconnection
        if self.consent_expired_count > 0:
            # ICE issues need immediate attention, but still rate limit slightly
            if (self.last_connection_attempt and 
                current_time - self.last_connection_attempt < 1.0):
                return False
            return True
        
        # For initial connection attempts (no established time), be more lenient
        if not self.connection_established_time:
            # Don't reconnect if we just attempted recently, but be more aggressive
            if (self.last_connection_attempt and 
                current_time - self.last_connection_attempt < max(2.0, self.reconnection_backoff * 0.5)):
                return False
            return True
        
        # For established connections, use backoff but be less restrictive
        if (self.last_connection_attempt and 
            current_time - self.last_connection_attempt < self.reconnection_backoff):
            return False
        
        # If we have a stable connection but recent state change, allow reconnection
        # but with minimal delay (reduced from previous 5 seconds)
        if (self.connection_stable and 
            self.last_connection_state_change and
            current_time - self.last_connection_state_change < 2.0):  # Reduced from 5.0
            return False
        
        return True

    def _calculate_reconnection_backoff(self):
        """Calculate exponential backoff for reconnection attempts."""
        # Use smaller multiplier for WebRTC connections (1.5x instead of 2x)
        self.reconnection_backoff = min(
            self.reconnection_backoff * 1.5,  # Gentler exponential backoff
            RECONNECTION_BACKOFF_MAX
        )
        logging.info(f"Reconnection backoff increased to {self.reconnection_backoff}s")

    async def _debounce_connection_state_changes(self):
        """Process debounced connection state changes."""
        current_time = time.time()
        
        # Process pending WHIP state changes
        if (self.pending_whip_state_change and 
            current_time - self.pending_whip_state_change[1] >= CONNECTION_STATE_DEBOUNCE_SECONDS):
            state, _ = self.pending_whip_state_change
            self.pending_whip_state_change = None
            await self._handle_whip_state_change(state)
        
        # Process pending WHEP state changes
        if (self.pending_whep_state_change and 
            current_time - self.pending_whep_state_change[1] >= CONNECTION_STATE_DEBOUNCE_SECONDS):
            state, _ = self.pending_whep_state_change
            self.pending_whep_state_change = None
            await self._handle_whep_state_change(state)

    async def _handle_whip_state_change(self, state: str):
        """Handle WHIP connection state changes after debouncing."""
        if state == "failed":
            self.whip_connected = False
            logging.error("WHIP connection failed after debouncing")
            if self._should_attempt_reconnection():
                asyncio.create_task(self._attempt_reconnection())
        elif state == "disconnected":
            self.whip_connected = False
            logging.warning("WHIP connection disconnected after debouncing")
            if self._should_attempt_reconnection():
                asyncio.create_task(self._attempt_reconnection())
        elif state == "connected":
            self.whip_connected = True
            logging.info("WHIP connection established successfully")
            self.connection_retry_count = 0
            self._update_connection_stability()
        elif state == "closed":
            self.whip_connected = False
            logging.info("WHIP connection closed")

    async def _handle_whep_state_change(self, state: str):
        """Handle WHEP connection state changes after debouncing."""
        if state == "failed":
            self.whep_connected = False
            logging.error("WHEP connection failed after debouncing")
            if self._should_attempt_reconnection():
                asyncio.create_task(self._attempt_reconnection())
        elif state == "disconnected":
            self.whep_connected = False
            logging.warning("WHEP connection disconnected after debouncing")
            if self._should_attempt_reconnection():
                asyncio.create_task(self._attempt_reconnection())
        elif state == "connected":
            self.whep_connected = True
            logging.info("WHEP connection established successfully")
            self.connection_retry_count = 0
            self._update_connection_stability()
        elif state == "closed":
            self.whep_connected = False
            logging.info("WHEP connection closed")

    async def _is_server_running(self) -> bool:
        """Check if the comfystream server is already running"""
        try:
            timeout = aiohttp.ClientTimeout(total=5)  # Increased timeout
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.params.comfystream_url}/health") as response:
                    is_healthy = response.status == 200
                    self.server_healthy = is_healthy
                    return is_healthy
        except Exception as e:
            logging.debug(f"Server health check failed: {e}")
            self.server_healthy = False
            return False

    async def _wait_for_server_ready(self, timeout: float = 30.0) -> bool:
        """Wait for the server to be ready with retries"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if await self._is_server_running():
                logging.info("ComfyStream server is ready")
                return True
            
            logging.info("Waiting for ComfyStream server to be ready...")
            await asyncio.sleep(2.0)
        
        logging.error(f"ComfyStream server not ready after {timeout} seconds")
        return False

    async def _start_server(self):
        """Start the comfystream server in a separate thread"""
        # Check if auto-start is disabled via environment variable
        if os.getenv("COMFYSTREAM_AUTO_START", "").lower() in ["false", "0", "no"]:
            logging.info("ComfyStream server auto-start disabled via COMFYSTREAM_AUTO_START environment variable")
            return
            
        if not self.params.auto_start_server:
            return

        if await self._is_server_running():
            logging.info(f"ComfyStream server already running at {self.params.comfystream_url}")
            return

        # Determine workspace
        workspace = self.params.comfystream_workspace
        if not workspace:
            # Try to find ComfyUI workspace from environment
            workspace = os.getenv("COMFY_UI_WORKSPACE")
            if not workspace:
                logging.error("No ComfyUI workspace specified. Set comfystream_workspace parameter or COMFY_UI_WORKSPACE environment variable.")
                return
        
        # Update params with resolved workspace
        self.params.comfystream_workspace = workspace
        
        logging.info("Starting ComfyStream server in separate thread...")
        logging.info(f"Configuration: workspace={workspace}, host={self.params.comfystream_host}, port={self.params.comfystream_port}")

    async def _setup_connections_with_retry(self, max_retries: int = MAX_RETRY_ATTEMPTS) -> bool:
        """Setup both WHIP and WHEP connections with retry logic"""
        for attempt in range(max_retries):
            try:
                logging.info(f"Attempting to setup connections (attempt {attempt + 1}/{max_retries})")
                
                # Check if server is ready first
                if not await self._wait_for_server_ready(timeout=10.0):
                    if attempt < max_retries - 1:
                        logging.warning(f"Server not ready, retrying in {RETRY_DELAY_SECONDS} seconds...")
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    else:
                        logging.error("Server not ready after all retry attempts")
                        return False
                
                # Setup WHIP publisher
                await self._setup_whip_publisher()
                
                # Setup WHEP subscriber
                await self._setup_whep_subscriber()
                
                # Wait for connections to be established
                logging.info("Waiting for WebRTC connections to establish...")
                await asyncio.sleep(2.0)  # Reduced from 3.0 for faster establishment
                
                # Check if connections are actually ready (check both internal state and WebRTC state)
                whip_ready = (self.whip_connected and 
                             self.whip_pc and 
                             self.whip_pc.connectionState == "connected")
                whep_ready = (self.whep_connected and 
                             self.whep_pc and 
                             self.whep_pc.connectionState == "connected")
                
                # Also update internal state if WebRTC reports connected but we missed it
                if self.whip_pc and self.whip_pc.connectionState == "connected" and not self.whip_connected:
                    logging.info("Syncing WHIP connection state - WebRTC reports connected")
                    self.whip_connected = True
                    whip_ready = True
                    
                if self.whep_pc and self.whep_pc.connectionState == "connected" and not self.whep_connected:
                    logging.info("Syncing WHEP connection state - WebRTC reports connected")
                    self.whep_connected = True
                    whep_ready = True
                
                if whip_ready and whep_ready:
                    logging.info("Both WHIP and WHEP connections established successfully")
                    self.connection_retry_count = 0  # Reset retry count on success
                    self._update_connection_stability()  # Update stability tracking
                    return True
                else:
                    logging.warning(f"Connections not ready: WHIP={whip_ready} (state={self.whip_pc.connectionState if self.whip_pc else 'None'}), WHEP={whep_ready} (state={self.whep_pc.connectionState if self.whep_pc else 'None'})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(RETRY_DELAY_SECONDS * 0.5)  # Shorter delay between attempts
                        continue
                        
            except Exception as e:
                logging.error(f"Connection setup attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                else:
                    logging.error("All connection setup attempts failed")
                    
        return False

    async def _health_check_loop(self):
        """Periodically check server health and reconnect if needed"""
        while self.is_initialized:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                
                # Process debounced connection state changes
                await self._debounce_connection_state_changes()
                
                # Update connection stability
                self._update_connection_stability()
                
                # Check server health
                if not await self._is_server_running():
                    logging.warning("Server health check failed, attempting reconnection")
                    await self._handle_server_disconnection()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in health check loop: {e}")

    async def _handle_server_disconnection(self):
        """Handle server disconnection and attempt reconnection"""
        logging.info("Handling server disconnection...")
        
        # Mark connections as disconnected
        self.whip_connected = False
        self.whep_connected = False
        self.connection_stable = False
        self.connection_established_time = None
        
        # Close existing connections
        if self.whip_pc:
            await self.whip_pc.close()
            self.whip_pc = None
        if self.whep_pc:
            await self.whep_pc.close()
            self.whep_pc = None
        
        # Attempt to reconnect
        await self._attempt_reconnection()

    async def _attempt_reconnection(self):
        """Attempt to reconnect to the server"""
        if not self._should_attempt_reconnection():
            logging.debug("Skipping reconnection due to backoff or stability rules")
            return
            
        if self.connection_retry_count >= MAX_RETRY_ATTEMPTS:
            logging.error("Max reconnection attempts reached, giving up")
            return
        
        self.connection_retry_count += 1
        self.last_connection_attempt = time.time()
        
        logging.info(f"Attempting reconnection (attempt {self.connection_retry_count}/{MAX_RETRY_ATTEMPTS}, backoff: {self.reconnection_backoff}s)")
        
        # Wait with backoff before retrying
        await asyncio.sleep(self.reconnection_backoff)
        
        # Try to setup connections again
        success = await self._setup_connections_with_retry(max_retries=1)
        
        if success:
            logging.info("Reconnection successful")
            self.connection_retry_count = 0
            self.reconnection_backoff = RETRY_DELAY_SECONDS  # Reset backoff
        else:
            logging.error("Reconnection failed")
            self._calculate_reconnection_backoff()  # Increase backoff for next attempt

    def _check_connection_health(self) -> tuple[bool, str]:
        """Check the overall health of WebRTC connections."""
        issues = []
        
        # Check WHIP connection
        if not self.whip_pc:
            issues.append("WHIP peer connection missing")
        elif self.whip_pc.connectionState != "connected":
            issues.append(f"WHIP connection state: {self.whip_pc.connectionState}")
        elif not self.whip_connected:
            issues.append("WHIP internal state not connected")
            
        # Check WHEP connection  
        if not self.whep_pc:
            issues.append("WHEP peer connection missing")
        elif self.whep_pc.connectionState != "connected":
            issues.append(f"WHEP connection state: {self.whep_pc.connectionState}")
        elif not self.whep_connected:
            issues.append("WHEP internal state not connected")
            
        # Check server health
        if not self.server_healthy:
            issues.append("Server not healthy")
            
        # Check frame publisher
        if not self.frame_publisher:
            issues.append("Frame publisher missing")
        elif not self.frame_publisher.running:
            issues.append("Frame publisher not running")
            
        is_healthy = len(issues) == 0
        status_msg = "Healthy" if is_healthy else f"Issues: {', '.join(issues)}"
        
        return is_healthy, status_msg

    async def _ensure_connections_ready(self) -> bool:
        """Ensure connections are ready, attempt reconnection if needed"""
        # First do a comprehensive health check
        is_healthy, health_msg = self._check_connection_health()
        
        if is_healthy:
            return True
            
        # Log health issues
        logging.debug(f"Connection health check failed: {health_msg}")
        
        # First sync internal state with actual WebRTC state before deciding to reconnect
        state_synced = False
        if self.whip_pc and self.whip_pc.connectionState == "connected" and not self.whip_connected:
            logging.info("Syncing WHIP connection state in ensure_connections_ready")
            self.whip_connected = True
            state_synced = True
            
        if self.whep_pc and self.whep_pc.connectionState == "connected" and not self.whep_connected:
            logging.info("Syncing WHEP connection state in ensure_connections_ready")
            self.whep_connected = True
            state_synced = True
        
        # If we synced state, check health again
        if state_synced:
            is_healthy, health_msg = self._check_connection_health()
            if is_healthy:
                logging.info("Connections became healthy after state sync")
                return True
        
        # Use new backoff logic instead of simple rate limiting
        if not self._should_attempt_reconnection():
            logging.debug("Skipping connection check due to backoff or stability rules")
            return False
        
        logging.info(f"Connections need repair: {health_msg}")
        self.last_connection_attempt = time.time()
        
        # Close existing connections first if they're in a bad state
        if self.whip_pc and self.whip_pc.connectionState not in ["connected", "connecting"]:
            logging.info(f"Closing WHIP connection in state: {self.whip_pc.connectionState}")
            await self.whip_pc.close()
            self.whip_pc = None
            self.whip_connected = False
        if self.whep_pc and self.whep_pc.connectionState not in ["connected", "connecting"]:
            logging.info(f"Closing WHEP connection in state: {self.whep_pc.connectionState}")
            await self.whep_pc.close()
            self.whep_pc = None
            self.whep_connected = False
        
        # Attempt reconnection
        return await self._setup_connections_with_retry(max_retries=2)

    async def initialize(self, **params):
        """Initialize the ComfyStream pipeline with given parameters."""
        self.params = ComfyStreamParams(**params)
        logging.info(f"Initializing ComfyStream Pipeline with URL: {self.params.comfystream_url}")
        logging.info(f"Environment variables - COMFY_UI_WORKSPACE: {os.getenv('COMFY_UI_WORKSPACE')}")
        logging.info(f"Pipeline parameters: {params}")
        
        # Start the server if needed
        await self._start_server()
        
        # Initialize HTTP sessions
        self.whip_session = aiohttp.ClientSession()
        self.whep_session = aiohttp.ClientSession()
        
        # Setup connections with retry logic
        if not await self._setup_connections_with_retry():
            logging.warning("Failed to establish connections after retries - will continue attempting in background")
            # Don't fail initialization completely, allow retry during streaming
        
        # Wait for processing pipeline to be ready
        if not await self._wait_for_processing_ready():
            logging.warning("Processing pipeline not ready, but continuing initialization")
        
        # Start processing task
        self.processing_task = asyncio.create_task(self._process_frames())
        
        # Start health check task
        self.health_check_task = asyncio.create_task(self._health_check_loop())
        
        # Skip warmup during initialization to avoid decoder errors
        # Warmup will happen naturally when first real frame is sent
        logging.info("Skipping dummy frame warmup to avoid decoder errors before stream starts")
        
        self.is_initialized = True
        logging.info("ComfyStream pipeline initialization complete")

    async def _setup_whip_publisher(self):
        """Setup WHIP connection to publish frames to comfystream"""
        try:
            # Enhanced ICE configuration with multiple STUN servers for better connectivity
            ice_servers = [
                RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun2.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun.cloudflare.com:3478"]),
                RTCIceServer(urls=["stun:stun.stunprotocol.org:3478"]),
            ]
            
            self.whip_pc = RTCPeerConnection(
                configuration=RTCConfiguration(
                    iceServers=ice_servers
                )
            )
            
            # Add connection state monitoring
            @self.whip_pc.on("connectionstatechange")
            async def on_whip_connection_state_change():
                if self.whip_pc:
                    state = self.whip_pc.connectionState
                    logging.info(f"WHIP connection state changed to: {state}")
                    
                    # Update last state change time
                    self.last_connection_state_change = time.time()
                    
                    # Handle successful connections immediately (no debouncing for success)
                    if state == "connected":
                        self.whip_connected = True
                        self.connection_retry_count = 0
                        self._update_connection_stability()
                        logging.info("WHIP connection established successfully (immediate update)")
                    elif state == "closed":
                        self.whip_connected = False
                        logging.info("WHIP connection closed (immediate update)")
                    else:
                        # Schedule debounced state change for failed/disconnected states
                        if state in ["failed", "disconnected"]:
                            self.pending_whip_state_change = (state, time.time())
                            logging.debug(f"Scheduled debounced WHIP state change: {state}")
            
            # Monitor ICE connection state for consent issues
            @self.whip_pc.on("iceconnectionstatechange")
            async def on_whip_ice_state_change():
                if self.whip_pc:
                    ice_state = self.whip_pc.iceConnectionState
                    logging.debug(f"WHIP ICE connection state: {ice_state}")
                    if ice_state == "failed":
                        self.consent_expired_count += 1
                        logging.warning(f"WHIP ICE connection failed (consent expired count: {self.consent_expired_count})")
                        # ICE failures need immediate reconnection, bypass debouncing
                        asyncio.create_task(self._attempt_reconnection())
            
            # Create frame publisher track with timestamp generator
            self.frame_publisher = FramePublisher(
                timestamp_generator=self._generate_sequential_timestamp,
                webrtc_time_base=self.webrtc_time_base
            )
            sender = self.whip_pc.addTrack(self.frame_publisher)
            
            # Force H264 codec preference
            try:
                from aiortc.rtcrtpsender import RTCRtpSender
                caps = RTCRtpSender.getCapabilities("video")
                prefs = [codec for codec in caps.codecs if codec.mimeType == "video/H264"]
                if prefs:
                    transceiver = next(t for t in self.whip_pc.getTransceivers() if t.sender == sender)
                    transceiver.setCodecPreferences(prefs)
                    logging.info("Set H264 codec preference for WHIP connection")
            except Exception as e:
                logging.warning(f"Could not set H264 codec preference: {e}")
            
            # Create offer
            offer = await self.whip_pc.createOffer()
            await self.whip_pc.setLocalDescription(offer)
            
            # Send WHIP request
            whip_url = f"{self.params.comfystream_url}/whip"
            prompts_param = json.dumps([self.params.prompt])
            
            logging.info(f"Setting up WHIP publisher for sequential timestamp delivery")
            
            if not self.whip_session:
                raise Exception("WHIP session not initialized")
            
            async with self.whip_session.post(
                whip_url,
                params={"prompts": prompts_param},
                headers={"Content-Type": "application/sdp"},
                data=offer.sdp
            ) as response:
                if response.status == 201:
                    answer_sdp = await response.text()
                    self.whip_resource_url = response.headers.get('Location')
                    
                    # Set remote description
                    answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
                    await self.whip_pc.setRemoteDescription(answer)
                    
                    logging.info(f"WHIP publisher connected, resource: {self.whip_resource_url}")
                else:
                    response_text = await response.text()
                    raise Exception(f"WHIP connection failed: {response.status} - {response_text}")
                    
        except Exception as e:
            logging.error(f"Error setting up WHIP publisher: {e}")
            raise

    async def _setup_whep_subscriber(self):
        """Setup WHEP connection to receive processed frames from comfystream"""
        try:
            # Enhanced ICE configuration with multiple STUN servers for better connectivity
            ice_servers = [
                RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun2.l.google.com:19302"]),
                RTCIceServer(urls=["stun:stun.cloudflare.com:3478"]),
                RTCIceServer(urls=["stun:stun.stunprotocol.org:3478"]),
            ]
            
            self.whep_pc = RTCPeerConnection(
                configuration=RTCConfiguration(
                    iceServers=ice_servers
                )
            )
            
            # Add connection state monitoring
            @self.whep_pc.on("connectionstatechange")
            async def on_whep_connection_state_change():
                if self.whep_pc:
                    state = self.whep_pc.connectionState
                    logging.info(f"WHEP connection state changed to: {state}")
                    
                    # Update last state change time
                    self.last_connection_state_change = time.time()
                    
                    # Handle successful connections immediately (no debouncing for success)
                    if state == "connected":
                        self.whep_connected = True
                        self.connection_retry_count = 0
                        self._update_connection_stability()
                        logging.info("WHEP connection established successfully (immediate update)")
                    elif state == "closed":
                        self.whep_connected = False
                        logging.info("WHEP connection closed (immediate update)")
                    else:
                        # Schedule debounced state change for failed/disconnected states
                        if state in ["failed", "disconnected"]:
                            self.pending_whep_state_change = (state, time.time())
                            logging.debug(f"Scheduled debounced WHEP state change: {state}")
            
            # Monitor ICE connection state for consent issues
            @self.whep_pc.on("iceconnectionstatechange")
            async def on_whep_ice_state_change():
                if self.whep_pc:
                    ice_state = self.whep_pc.iceConnectionState
                    logging.debug(f"WHEP ICE connection state: {ice_state}")
                    if ice_state == "failed":
                        self.consent_expired_count += 1
                        logging.warning(f"WHEP ICE connection failed (consent expired count: {self.consent_expired_count})")
                        # ICE failures need immediate reconnection, bypass debouncing
                        asyncio.create_task(self._attempt_reconnection())
            
            # Add transceiver for receiving video with H.264 preference
            transceiver = self.whep_pc.addTransceiver("video", direction="recvonly")
            
            # Force H264 codec preference for WHEP subscriber
            try:
                from aiortc.rtcrtpsender import RTCRtpSender
                caps = RTCRtpSender.getCapabilities("video")
                prefs = [codec for codec in caps.codecs if codec.mimeType == "video/H264"]
                if prefs:
                    transceiver.setCodecPreferences(prefs)
                    logging.info("Set H264 codec preference for WHEP connection")
            except Exception as e:
                logging.warning(f"Could not set H264 codec preference for WHEP: {e}")
            
            # Handle incoming tracks
            @self.whep_pc.on("track")
            def on_track(track):
                if track.kind == "video":
                    logging.info("Received video track from WHEP")
                    asyncio.create_task(self._handle_whep_frames(track))
            
            # Create offer
            offer = await self.whep_pc.createOffer()
            await self.whep_pc.setLocalDescription(offer)
            
            # Send WHEP request
            whep_url = f"{self.params.comfystream_url}/whep"
            
            if not self.whep_session:
                raise Exception("WHEP session not initialized")
            
            async with self.whep_session.post(
                whep_url,
                headers={"Content-Type": "application/sdp"},
                data=offer.sdp
            ) as response:
                if response.status == 201:
                    answer_sdp = await response.text()
                    self.whep_resource_url = response.headers.get('Location')
                    
                    # Set remote description
                    answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
                    await self.whep_pc.setRemoteDescription(answer)
                    
                    logging.info(f"WHEP subscriber connected, resource: {self.whep_resource_url}")
                else:
                    raise Exception(f"WHEP connection failed: {response.status}")
                    
        except Exception as e:
            logging.error(f"Error setting up WHEP subscriber: {e}")
            raise

    async def _handle_whep_frames(self, track):
        """Handle incoming processed frames from WHEP"""
        try:
            while True:
                frame = await track.recv()
                
                # Convert WebRTC frame to internal tensor format with minimal quality loss
                if hasattr(frame, 'to_ndarray'):
                    # Convert av.VideoFrame directly to numpy array in RGB format
                    frame_array = frame.to_ndarray(format='rgb24')
                    
                    # Convert directly to tensor without PIL intermediate step
                    # Normalize to [0, 1] range using float32 for precision
                    tensor_float = torch.from_numpy(frame_array.astype(np.float32)) / 255.0
                    
                    # Add batch dimension: (H, W, C) -> (B, H, W, C)
                    tensor = tensor_float.unsqueeze(0)
                    
                    # Get corresponding input frame with proper timestamp
                    if not self.video_incoming_frames.empty():
                        input_frame = await self.video_incoming_frames.get()
                        
                        # Create processed output preserving original timestamp and metadata
                        processed_output = input_frame.replace_tensor(tensor)
                        await self.video_processed_frames.put(processed_output)
                    
        except Exception as e:
            if "ended" not in str(e).lower():
                logging.error(f"Error handling WHEP frames: {e}")

    async def _process_frames(self):
        """Process frames between WHIP and WHEP"""
        try:
            while self.is_initialized:
                await asyncio.sleep(0.01)  # Small delay to prevent busy waiting
        except Exception as e:
            logging.error(f"Error in frame processing: {e}")

    async def _warmup_pipeline(self):
        """Warm up the pipeline with dummy frames (only when processing is ready)"""
        try:
            # Check if processing pipeline is ready before sending dummy frames
            status = await self._check_processing_status()
            if not status.get("processing_ready", False):
                logging.info("Skipping warmup - processing pipeline not ready yet")
                return
            
            # Ensure connections are established
            if not (self.whip_connected and self.whep_connected):
                logging.info("Skipping warmup - WebRTC connections not ready")
                return
                
            logging.info("Starting pipeline warmup with dummy frames")
            
            # Create dummy frame with proper format matching internal pipeline
            dummy_array = np.random.randint(0, 255, (self.params.height, self.params.width, 3), dtype=np.uint8)
            
            # Convert to proper format for WebRTC
            dummy_av_frame = av.VideoFrame.from_ndarray(dummy_array, format='rgb24')
            # Use sequential timestamp for consistent timing
            dummy_av_frame.pts = self._generate_sequential_timestamp()
            dummy_av_frame.time_base = self.webrtc_time_base
            
            # Send dummy frames for warmup with error handling
            for i in range(WARMUP_RUNS):
                if self.frame_publisher and self.whip_connected:
                    try:
                        await self.frame_publisher.add_frame(dummy_av_frame)
                        await asyncio.sleep(0.1)  # Small delay between frames
                    except Exception as frame_error:
                        logging.warning(f"Error sending warmup frame {i}: {frame_error}")
                        break
                else:
                    logging.warning("Frame publisher not available during warmup")
                    break
                    
            logging.info("Pipeline warmup complete")
        except Exception as e:
            logging.warning(f"Pipeline warmup failed (this is normal during initialization): {e}")

    async def _check_processing_status(self) -> Dict[str, Any]:
        """Check the processing status from ComfyStream server."""
        try:
            status_url = f"{self.params.comfystream_url}/processing/status"
            
            if not self.whip_session:
                raise Exception("WHIP session not initialized")
            
            async with self.whip_session.get(status_url) as response:
                if response.status == 200:
                    status = await response.json()
                    logging.debug(f"Processing status: {status}")
                    return status
                else:
                    logging.warning(f"Failed to get processing status: {response.status}")
                    return {
                        "processing_ready": False,
                        "message": f"Status check failed: {response.status}"
                    }
                    
        except Exception as e:
            logging.warning(f"Error checking processing status: {e}")
            return {
                "processing_ready": False,
                "message": f"Status check error: {str(e)}"
            }

    async def _wait_for_processing_ready(self, timeout: float = 30.0) -> bool:
        """Wait for the processing pipeline to be ready."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            status = await self._check_processing_status()
            
            if status.get("processing_ready", False):
                logging.info(f"Processing pipeline ready: {status.get('message', '')}")
                return True
            
            # Log status if not ready
            message = status.get("message", "Unknown status")
            logging.info(f"Waiting for processing pipeline: {message}")
            
            # Wait before next check
            await asyncio.sleep(1.0)
        
        logging.error(f"Processing pipeline not ready after {timeout} seconds")
        return False

    async def put_video_frame(self, frame: VideoFrame, request_id: str):
        """Receive input frame and publish it via WHIP"""
        try:
            # Ensure connections are ready, reconnect if needed
            if not await self._ensure_connections_ready():
                logging.debug(f"Could not establish connections for frame {request_id}, skipping")
                return
            
            # Check if connections are ready
            if not self.whip_connected:
                logging.debug(f"WHIP connection not ready for frame {request_id}, skipping")
                return
            
            # Check processing status before sending frames
            status = await self._check_processing_status()
            if not status.get("processing_ready", False):
                logging.debug(f"Processing not ready for frame {request_id}: {status.get('message', '')}")
                return
            
            # Validate input frame before processing
            if not self._validate_input_frame(frame, request_id):
                logging.warning(f"Invalid input frame {request_id}, skipping")
                return
            
            # Convert VideoFrame tensor to av.VideoFrame with minimal quality loss
            tensor = frame.tensor
            if tensor.is_cuda:
                tensor = tensor.cpu()
            
            # Remove batch dimension if present
            if tensor.dim() == 4:
                tensor = tensor.squeeze(0)  # Remove batch dimension
            
            # Ensure tensor is in [0, 1] range and convert to [0, 255] with proper clamping
            if tensor.max() <= 1.0:
                # Tensor is in [0, 1] range, convert to [0, 255] with clamping
                tensor_uint8 = torch.clamp(tensor * 255.0, 0, 255).to(torch.uint8)
            else:
                # Tensor is already in [0, 255] range, clamp and convert
                tensor_uint8 = torch.clamp(tensor, 0, 255).to(torch.uint8)
            
            # Convert directly to numpy without PIL intermediate step to reduce quality loss
            frame_array = tensor_uint8.numpy()
            
            # Validate frame array before creating av.VideoFrame
            if not self._validate_frame_array(frame_array, request_id):
                logging.warning(f"Invalid frame array for {request_id}, skipping")
                return
            
            # Create av.VideoFrame directly from numpy array
            av_frame = av.VideoFrame.from_ndarray(frame_array, format='rgb24')
            
            # Validate av.VideoFrame before encoding
            if not self._validate_av_frame(av_frame, request_id):
                logging.warning(f"Invalid av.VideoFrame for {request_id}, skipping")
                return
            
            # Set proper timestamp using sequential frame numbering (not wall-clock time)
            if hasattr(frame, 'timestamp') and hasattr(frame, 'time_base') and frame.timestamp is not None:
                # Rescale original frame timestamp to WebRTC time base
                av_frame.pts = self._rescale_timestamp(frame.timestamp, frame.time_base, self.webrtc_time_base)
                av_frame.time_base = self.webrtc_time_base
            else:
                # Generate sequential timestamp for consistent timing
                av_frame.pts = self._generate_sequential_timestamp()
                av_frame.time_base = self.webrtc_time_base
            
            # Publish frame via WHIP with sequence tracking
            if self.frame_publisher and self.whip_connected:
                # Add frame sequence metadata for temporal consistency tracking
                frame_seq = self.frame_sequence
                await self.frame_publisher.add_frame(av_frame)
                
                # Track successful frame processing
                self.last_successful_frame_time = time.time()
                self.frames_processed_successfully += 1
                
                logging.debug(f"Published frame {request_id} seq:{frame_seq} via WHIP (total: {self.frames_processed_successfully})")
                
                # Store input frame with sequence in timestamp buffer for ordered processing
                input_output = VideoOutput(frame, request_id)
                # Store sequence info for this frame using request_id as key
                self.input_timestamp_buffer[request_id] = {
                    'sequence': frame_seq,
                    'timestamp': time.time(),
                    'input_frame': input_output
                }
                await self.video_incoming_frames.put(input_output)
            else:
                logging.debug(f"Frame publisher not ready, skipping frame {request_id}")
            
        except Exception as e:
            logging.error(f"Error putting video frame: {e}")
            # If it's a connection-related error, trigger reconnection
            if "connection" in str(e).lower() or "consent" in str(e).lower():
                asyncio.create_task(self._attempt_reconnection())

    def _validate_input_frame(self, frame: VideoFrame, request_id: str) -> bool:
        """Validate input VideoFrame before processing."""
        try:
            # Check if frame has tensor
            if not hasattr(frame, 'tensor') or frame.tensor is None:
                logging.debug(f"Frame {request_id} has no tensor")
                return False
            
            tensor = frame.tensor
            
            # Check tensor dimensions
            if tensor.dim() < 3 or tensor.dim() > 4:
                logging.debug(f"Frame {request_id} has invalid tensor dimensions: {tensor.dim()}")
                return False
            
            # Check tensor shape
            if tensor.dim() == 4:
                batch_size, height, width, channels = tensor.shape
                if batch_size != 1:
                    logging.debug(f"Frame {request_id} has invalid batch size: {batch_size}")
                    return False
            else:
                height, width, channels = tensor.shape
            
            # Check frame dimensions
            if height <= 0 or width <= 0 or channels != 3:
                logging.debug(f"Frame {request_id} has invalid dimensions: {height}x{width}x{channels}")
                return False
            
            # Check if tensor has valid data
            if torch.isnan(tensor).any() or torch.isinf(tensor).any():
                logging.debug(f"Frame {request_id} contains NaN or Inf values")
                return False
            
            return True
            
        except Exception as e:
            logging.debug(f"Error validating input frame {request_id}: {e}")
            return False

    def _validate_frame_array(self, frame_array: np.ndarray, request_id: str) -> bool:
        """Validate numpy frame array before creating av.VideoFrame."""
        try:
            # Check array dimensions
            if frame_array.ndim != 3:
                logging.debug(f"Frame array {request_id} has invalid dimensions: {frame_array.ndim}")
                return False
            
            height, width, channels = frame_array.shape
            
            # Check frame dimensions
            if height <= 0 or width <= 0 or channels != 3:
                logging.debug(f"Frame array {request_id} has invalid shape: {height}x{width}x{channels}")
                return False
            
            # Check data type
            if frame_array.dtype != np.uint8:
                logging.debug(f"Frame array {request_id} has invalid dtype: {frame_array.dtype}")
                return False
            
            # Check for valid data range
            if frame_array.min() < 0 or frame_array.max() > 255:
                logging.debug(f"Frame array {request_id} has invalid value range: [{frame_array.min()}, {frame_array.max()}]")
                return False
            
            # Check for NaN or Inf values
            if np.isnan(frame_array).any() or np.isinf(frame_array).any():
                logging.debug(f"Frame array {request_id} contains NaN or Inf values")
                return False
            
            return True
            
        except Exception as e:
            logging.debug(f"Error validating frame array {request_id}: {e}")
            return False

    def _validate_av_frame(self, av_frame: av.VideoFrame, request_id: str) -> bool:
        """Validate av.VideoFrame before encoding."""
        try:
            # Check frame dimensions
            if av_frame.width <= 0 or av_frame.height <= 0:
                logging.debug(f"av.VideoFrame {request_id} has invalid dimensions: {av_frame.width}x{av_frame.height}")
                return False
            
            # Check format
            if av_frame.format.name != 'rgb24':
                logging.debug(f"av.VideoFrame {request_id} has invalid format: {av_frame.format.name}")
                return False
            
            # Check if frame has data
            if not hasattr(av_frame, 'planes') or not av_frame.planes:
                logging.debug(f"av.VideoFrame {request_id} has no planes")
                return False
            
            # Check plane data
            for i, plane in enumerate(av_frame.planes):
                if plane.buffer_size <= 0:
                    logging.debug(f"av.VideoFrame {request_id} plane {i} has invalid buffer size: {plane.buffer_size}")
                    return False
            
            return True
            
        except Exception as e:
            logging.debug(f"Error validating av.VideoFrame {request_id}: {e}")
            return False

    async def get_processed_video_frame(self):
        """Get processed video frame from WHEP"""
        try:
            # Wait for processed frame
            processed_frame = await self.video_processed_frames.get()
            return processed_frame
        except Exception as e:
            logging.error(f"Error getting processed video frame: {e}")
            raise



    async def update_params(self, **params):
        """Update pipeline parameters"""
        try:
            new_params = ComfyStreamParams(**params)
            logging.info(f"Updating ComfyStream Pipeline Prompt: {new_params.prompt}")
            
            # For now, we'll need to restart the WHIP connection with new prompts
            # This could be optimized in the future with a control channel
            if new_params.prompt != self.params.prompt:
                await self._restart_whip_with_new_prompts(new_params.prompt)
            
            self.params = new_params
            
        except Exception as e:
            logging.error(f"Error updating ComfyStream Pipeline parameters: {e}")
            raise

    async def _restart_whip_with_new_prompts(self, new_prompt):
        """Restart WHIP connection with new prompts"""
        try:
            # Close existing WHIP connection
            if self.whip_pc:
                await self.whip_pc.close()
                self.whip_pc = None
            
            # Update prompt
            old_prompt = self.params.prompt
            self.params.prompt = new_prompt
            
            # Setup new WHIP connection with retry logic
            success = await self._setup_connections_with_retry(max_retries=2)
            
            if success:
                logging.info("WHIP connection restarted with new prompts")
            else:
                logging.error("Failed to restart WHIP connection with new prompts")
                # Restore old prompt on failure
                self.params.prompt = old_prompt
                raise Exception("Failed to restart WHIP connection")
            
        except Exception as e:
            logging.error(f"Error restarting WHIP with new prompts: {e}")
            # Restore old prompt on failure
            self.params.prompt = old_prompt
            raise

    async def _reconnect_whip(self):
        """Reconnect WHIP connection after failure"""
        try:
            logging.info("Attempting to reconnect WHIP connection")
            
            # Close existing connection
            if self.whip_pc:
                await self.whip_pc.close()
                self.whip_pc = None
            
            # Wait a bit before reconnecting
            await asyncio.sleep(1)
            
            # Setup new WHIP connection
            await self._setup_whip_publisher()
            
            logging.info("WHIP connection reconnected successfully")
            
        except Exception as e:
            logging.error(f"Error reconnecting WHIP: {e}")
            # Will try again on next failure

    async def _reconnect_whep(self):
        """Reconnect WHEP connection after failure"""
        try:
            logging.info("Attempting to reconnect WHEP connection")
            
            # Close existing connection
            if self.whep_pc:
                await self.whep_pc.close()
                self.whep_pc = None
            
            # Wait a bit before reconnecting
            await asyncio.sleep(1)
            
            # Setup new WHEP connection
            await self._setup_whep_subscriber()
            
            logging.info("WHEP connection reconnected successfully")
            
        except Exception as e:
            logging.error(f"Error reconnecting WHEP: {e}")
            # Will try again on next failure

    async def stop(self):
        """Stop the ComfyStream pipeline"""
        try:
            logging.info("Stopping ComfyStream pipeline")
            
            self.is_initialized = False
            
            # Stop health check task
            if self.health_check_task:
                self.health_check_task.cancel()
                try:
                    await self.health_check_task
                except asyncio.CancelledError:
                    pass
            
            # Stop processing task
            if self.processing_task:
                self.processing_task.cancel()
                try:
                    await self.processing_task
                except asyncio.CancelledError:
                    pass
            
            # Stop frame publisher
            if self.frame_publisher:
                self.frame_publisher.stop()
            
            # Close WHIP connection
            if self.whip_resource_url and self.whip_session:
                try:
                    async with self.whip_session.delete(self.whip_resource_url) as response:
                        logging.info(f"WHIP resource deleted: {response.status}")
                except Exception as e:
                    logging.warning(f"Error deleting WHIP resource: {e}")
            
            if self.whip_pc:
                await self.whip_pc.close()
            
            # Close WHEP connection
            if self.whep_resource_url and self.whep_session:
                try:
                    async with self.whep_session.delete(self.whep_resource_url) as response:
                        logging.info(f"WHEP resource deleted: {response.status}")
                except Exception as e:
                    logging.warning(f"Error deleting WHEP resource: {e}")
            
            if self.whep_pc:
                await self.whep_pc.close()
            
            # Close HTTP sessions
            if self.whip_session:
                await self.whip_session.close()
            if self.whep_session:
                await self.whep_session.close()
            
            # Clear queues
            while not self.video_incoming_frames.empty():
                try:
                    self.video_incoming_frames.get_nowait()
                except asyncio.QueueEmpty:
                    break
            
            while not self.video_processed_frames.empty():
                try:
                    self.video_processed_frames.get_nowait()
                except asyncio.QueueEmpty:
                    break
            
            # Force CUDA cache clear
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
            
            # Reset timestamp sequence and buffers for clean restart
            self.frame_sequence = 0
            self.last_frame_time = None
            self.input_timestamp_buffer.clear()
            
            # Reset frame processing counters
            self.last_successful_frame_time = None
            self.frames_processed_successfully = 0
            
            logging.info("ComfyStream pipeline stopped")
            
        except Exception as e:
            logging.error(f"Error stopping ComfyStream pipeline: {e}") 

    async def get_connection_status(self) -> Dict[str, Any]:
        """Get the current connection status of the pipeline."""
        try:
            # Get processing status from server
            processing_status = await self._check_processing_status()
            
            # Calculate connection uptime
            connection_uptime = None
            if self.connection_established_time:
                connection_uptime = time.time() - self.connection_established_time
            
            # Get WebRTC connection states
            whip_state = None
            whep_state = None
            whip_ice_state = None
            whep_ice_state = None
            
            if self.whip_pc:
                whip_state = self.whip_pc.connectionState
                whip_ice_state = self.whip_pc.iceConnectionState
            
            if self.whep_pc:
                whep_state = self.whep_pc.connectionState
                whep_ice_state = self.whep_pc.iceConnectionState
            
            # Calculate connection health score (0-100)
            health_score = self._calculate_connection_health_score()
            
            status = {
                "initialized": self.is_initialized,
                "whip_connected": self.whip_connected,
                "whep_connected": self.whep_connected,
                "server_healthy": self.server_healthy,
                "processing_ready": processing_status.get("processing_ready", False),
                "processing_message": processing_status.get("message", "Unknown"),
                "connection_stable": self.connection_stable,
                "connection_uptime": connection_uptime,
                "health_score": health_score,
                
                # WebRTC connection details
                "whip_state": whip_state,
                "whep_state": whep_state,
                "whip_ice_state": whip_ice_state,
                "whep_ice_state": whep_ice_state,
                
                # Resource URLs
                "whip_resource_url": self.whip_resource_url,
                "whep_resource_url": self.whep_resource_url,
                
                # Queue status
                "incoming_frames_queue_size": self.video_incoming_frames.qsize(),
                "processed_frames_queue_size": self.video_processed_frames.qsize(),
                
                # Task status
                "processing_task_running": self.processing_task and not self.processing_task.done() if self.processing_task else False,
                "health_check_task_running": self.health_check_task and not self.health_check_task.done() if self.health_check_task else False,
                
                # Connection retry information
                "connection_retry_count": self.connection_retry_count,
                "consent_expired_count": self.consent_expired_count,
                "reconnection_backoff": self.reconnection_backoff,
                "last_connection_attempt": self.last_connection_attempt,
                "last_connection_state_change": self.last_connection_state_change,
                
                # Frame processing stats
                "frame_sequence": self.frame_sequence,
                "timestamp_buffer_size": len(self.input_timestamp_buffer),
                "frames_processed_successfully": self.frames_processed_successfully,
                "last_successful_frame_time": self.last_successful_frame_time,
                
                # Configuration
                "comfystream_url": self.params.comfystream_url if hasattr(self, 'params') else None,
                "width": self.params.width if hasattr(self, 'params') else None,
                "height": self.params.height if hasattr(self, 'params') else None,
            }
            
            # Add detailed processing status if available
            if "details" in processing_status:
                status["server_details"] = processing_status["details"]
            
            # Add recommendations based on health score
            status["recommendations"] = self._get_health_recommendations(health_score)
            
            return status
            
        except Exception as e:
            logging.error(f"Error getting connection status: {e}")
            return {
                "initialized": self.is_initialized,
                "error": str(e),
                "health_score": 0,
                "whip_connected": self.whip_connected,
                "whep_connected": self.whep_connected,
                "server_healthy": self.server_healthy,
                "connection_retry_count": self.connection_retry_count,
                "consent_expired_count": self.consent_expired_count,
                "recommendations": ["Check connection configuration", "Verify server status"]
            }

    def _calculate_connection_health_score(self) -> int:
        """Calculate a health score (0-100) based on connection state."""
        score = 0
        current_time = time.time()
        
        # Base score for initialization
        if self.is_initialized:
            score += 20
        
        # Connection status
        if self.whip_connected:
            score += 25
        if self.whep_connected:
            score += 25
        if self.server_healthy:
            score += 20
        
        # Stability bonus
        if self.connection_stable:
            score += 10
        
        # Successful frame processing bonus
        if self.frames_processed_successfully > 0:
            score += 5  # Base bonus for any successful frames
            
            # Recent success bonus
            if (self.last_successful_frame_time and 
                current_time - self.last_successful_frame_time < 10.0):
                score += 10  # Recent success is very good sign
            elif (self.last_successful_frame_time and 
                  current_time - self.last_successful_frame_time < 60.0):
                score += 5   # Recent-ish success is good
        
        # Penalty for retry attempts
        if self.connection_retry_count > 0:
            score -= min(self.connection_retry_count * 5, 20)
        
        # Penalty for consent expired
        if self.consent_expired_count > 0:
            score -= min(self.consent_expired_count * 3, 15)
        
        # Penalty for large queue backlogs
        if self.video_incoming_frames.qsize() > 10:
            score -= 5
        if self.video_processed_frames.qsize() > 10:
            score -= 5
        
        return max(0, min(100, score))

    def _get_health_recommendations(self, health_score: int) -> list:
        """Get recommendations based on health score."""
        recommendations = []
        current_time = time.time()
        
        if health_score < 30:
            recommendations.append("Connection is unhealthy - consider restarting the pipeline")
            recommendations.append("Check network connectivity to ComfyStream server")
            recommendations.append("Verify server is running and accessible")
        elif health_score < 60:
            recommendations.append("Connection is degraded - monitor for improvements")
            if self.connection_retry_count > 0:
                recommendations.append("Multiple connection retries detected - check network stability")
            if self.consent_expired_count > 0:
                recommendations.append("ICE consent expired - NAT/firewall issues possible")
        elif health_score < 80:
            recommendations.append("Connection is fair - minor issues detected")
            if self.video_incoming_frames.qsize() > 5:
                recommendations.append("Input frame queue is growing - processing may be slow")
            if self.video_processed_frames.qsize() > 5:
                recommendations.append("Output frame queue is growing - consumer may be slow")
        else:
            recommendations.append("Connection is healthy")
            if self.connection_stable:
                recommendations.append("Connection is stable and performing well")
        
        # Add frame processing specific recommendations
        if self.frames_processed_successfully == 0:
            recommendations.append("No frames processed yet - check if input stream is active")
        elif (self.last_successful_frame_time and 
              current_time - self.last_successful_frame_time > 30.0):
            recommendations.append("No recent frame processing - check input stream continuity")
        elif self.frames_processed_successfully > 0:
            if (self.last_successful_frame_time and 
                current_time - self.last_successful_frame_time < 10.0):
                recommendations.append(f"Frame processing is active ({self.frames_processed_successfully} frames processed)")
            else:
                recommendations.append(f"Frame processing history looks good ({self.frames_processed_successfully} total frames)")
        
        return recommendations 

    async def force_reconnection(self):
        """Force an immediate reconnection attempt, bypassing all checks and delays."""
        logging.info("Forcing immediate reconnection (bypassing all checks)")
        
        # Reset connection state
        self.whip_connected = False
        self.whep_connected = False
        self.connection_stable = False
        self.connection_established_time = None
        
        # Close existing connections
        if self.whip_pc:
            await self.whip_pc.close()
            self.whip_pc = None
        if self.whep_pc:
            await self.whep_pc.close()
            self.whep_pc = None
        
        # Reset backoff and retry count
        self.reconnection_backoff = RETRY_DELAY_SECONDS
        self.connection_retry_count = 0
        
        # Attempt immediate reconnection
        return await self._setup_connections_with_retry(max_retries=3) 