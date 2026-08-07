// Passing creds via the io() auth payload pre-verifies the socket on the
// server's connect handler, so events firing before join_session completes
// (e.g. mic_on right after a reconnect) don't race and see verified=False.
// Function form re-reads the current user_secret_key on every reconnect,
// which matters because it can be rotated by updateUserSecretKey().
const socket = io({
  auth: (cb) => cb({ session_id: sessionId, secret_key: user_secret_key }),
});
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
let selectedDeviceId = null;
let micDeviceSelector = null;
let mediaStream = null;
let audioContext = null;
let audioProcessor = null;
let analyserNode = null;
let levelAnimFrame = null;

// Two independent health signals share one indicator: the browser<->server socket,
// and the server<->Scribe transcription pipeline. Socket trouble outranks scribe
// trouble (no socket means no audio reaches the server at all), so keep them apart
// and re-render instead of letting whichever event fired last win.
let socketStatus = { cls: 'bg-gray-400', text: 'Connecting…' };
// null | 'connected' | 'reconnecting' | 'stopped' | 'misconfigured'. The last is
// folded in from the stop reason rather than tracked separately: it is the only stop
// the server never recovers from, and no other reason changes what we render.
let scribeState = null;
let micActive = false;   // mirrors the panel mic toggle; see onMicStateChange

function setSocketStatus(cls, text) {
  socketStatus = { cls: cls, text: text };
  renderStatus();
}

// Called by setMicState() in panel.html. 'stopped' means opposite things depending on
// the mic ('expected' after mic_off vs 'transcription died' while streaming), and
// nothing else re-renders when the operator toggles the mic.
function onMicStateChange(state) {
  micActive = (state === 'on');
  if (!micActive) scribeState = null;  // intentional stop: drop any stale alarm
  renderStatus();
}

// A recoverable server-side rejection is worth showing, but it neither stops the mic
// nor outranks a real socket/transcription alarm — so it lives on a timer and only
// paints when nothing more serious is on screen.
let transientNotice = null;
let transientNoticeTimer = null;

function showTransientNotice(text) {
  const unchanged = text === transientNotice;
  transientNotice = text;
  clearTimeout(transientNoticeTimer);
  transientNoticeTimer = setTimeout(function () {
    transientNotice = null;
    renderStatus();
  }, 5000);
  // A payload the server keeps rejecting produces one error per audio chunk; repainting
  // identical text dozens of times a second buys nothing. Extending the timer does.
  if (!unchanged) renderStatus();
}

function renderStatus() {
  let cls = socketStatus.cls;
  let text = socketStatus.text;
  // Socket trouble outranks all of these — no socket means no audio reaches the server
  // at all — so anything below only paints over a healthy socket.
  if (socketStatus.cls === 'bg-green-500') {
    if (micActive && scribeState === 'misconfigured') {
      // No ELEVENLABS_API_KEY: every replacement manager refuses the same way and no
      // amount of audio brings transcription back. Say so instead of promising a
      // reconnect that will never land.
      cls = 'bg-red-500';
      text = 'Transcription unavailable — server misconfigured';
    } else if (scribeState === 'reconnecting' || (scribeState === 'stopped' && micActive)) {
      // 'stopped' rides along with 'reconnecting': while audio is still flowing the
      // server rebuilds the Scribe manager on the next chunk, so an ordinary stop
      // (manager evicted, idle watchdog, socket closed) repairs itself — asking the
      // operator to toggle the mic only made them do by hand what was already happening.
      cls = 'bg-orange-500';
      text = 'Transcription interrupted — reconnecting…';
    } else if (transientNotice) {
      cls = 'bg-yellow-400';
      text = transientNotice;
    }
  }
  statusIndicator.className = 'shrink-0 inline-block w-3 h-3 rounded-full ' + cls;
  statusText.textContent = text;
}

function updateViewerCountDisplay(count) {
  const el = document.getElementById('viewer-count-display');
  if (!el || !Number.isFinite(count)) return;
  el.textContent = 'Viewers: ' + count;
}

// --- Server clock synchronization (NTP-style) ---
// Latency readouts subtract a server-generated end_time from Date.now(); without
// this, the viewer's clock skew vs the server is added straight into the number
// (it only looked right locally because browser and server shared one clock).
// We estimate serverClock - localClock and expose it for the flow panel to apply.
window.serverClockOffsetMs = 0;
let _clockSyncBestRtt = Infinity;

function sampleServerClock() {
  const t0 = Date.now();
  socket.emit('time_sync', { t0 }, function (resp) {
    if (!resp || typeof resp.t1 !== 'number') return;
    const rtt = Date.now() - t0;
    // Keep the lowest-RTT sample: least confounded by network jitter.
    if (rtt < _clockSyncBestRtt) {
      _clockSyncBestRtt = rtt;
      window.serverClockOffsetMs = resp.t1 - (t0 + rtt / 2);
    }
  });
}

function syncServerClock(samples, gapMs) {
  // Reset the best-RTT baseline so a fresh burst can re-converge after drift.
  _clockSyncBestRtt = Infinity;
  let n = 0;
  (function tick() {
    sampleServerClock();
    if (++n < samples) setTimeout(tick, gapMs);
  })();
}

// Re-measure periodically to track drift; bursts pick the cleanest sample.
setInterval(function () { if (socket.connected) syncServerClock(3, 200); }, 30000);

socket.on('connect', function () {
  // Show pending state — auth is not yet confirmed by the server
  setSocketStatus('bg-yellow-400', 'Authenticating…');
  console.log('WebSocket connection opened');

  // Join the session room
  socket.emit('join_session', { session_id: sessionId, secret_key: user_secret_key });
  // Establish the clock offset before latency numbers start rendering.
  syncServerClock(5, 200);
});

socket.on('disconnect', function () {
  // Stale scribe state: the server may have stopped or restarted the session while
  // we were away. join_session replays the real state on reconnect.
  scribeState = null;
  setSocketStatus('bg-red-500', 'Disconnected');
  console.log('WebSocket connection closed');
});

socket.on('connected', function (data) {
  console.log('WebSocket connected:', data);
});

socket.on('joined_session', function (data) {
  console.log('Joined session:', data);
  updateViewerCountDisplay(data.viewer_count);
  if (data.authorized) {
    setSocketStatus('bg-green-500', 'Connected: ' + data.session_id);
    socket.emit('realtime_connect', { session_id: sessionId });
  } else {
    setSocketStatus('bg-orange-500', 'Unauthorized');
    console.warn('join_session: not authorized for session', data.session_id);
  }
});

// Transcription pipeline health. Never touches the mic: a Scribe reconnect is
// recoverable, and killing the mic here would turn a gap into a dead session.
socket.on('scribe_status', function (data) {
  if (!data || data.session_id !== sessionId) return;
  console.log('Scribe status:', data);
  scribeState = data.reason === 'misconfigured' ? 'misconfigured' : data.state;
  renderStatus();
});

socket.on('viewer_count_update', function (data) {
  if (!data || data.session_id !== sessionId) return;
  updateViewerCountDisplay(data.viewer_count);
});

// The server emits `error` for everything from a rejected payload to a failed auth
// check. Only the auth family means this socket can no longer stream audio; the rest
// is recoverable, and treating it as fatal used to kill the operator's mic mid-
// broadcast over a single malformed packet or a join_session rate limit.
const FATAL_ERROR_CODES = ['unauthorized', 'realtime_token_required'];

socket.on('error', function (data) {
  console.error('WebSocket error:', data);
  const code = data && data.code;
  if (FATAL_ERROR_CODES.indexOf(code) !== -1) {
    setSocketStatus('bg-orange-500', 'Unauthorized — microphone stopped');
    stopRecording();
    return;
  }
  // Recoverable (invalid_payload, rate_limited, or an unknown code): keep streaming
  // and just surface it, so a transient rejection doesn't take the session down.
  showTransientNotice(code === 'rate_limited'
    ? 'Server throttled a request — still streaming'
    : 'Server rejected a message — still streaming');
});

// Returns false when nothing ended up streaming (denied permission, dead socket), so
// startRecording() can keep the mic button in step. Swallows its own errors, which is
// why this is a return value and not a throw.
async function startSession() {
  try {
    audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    await audioContext.audioWorklet.addModule('/static/js/audio-processor.js');

    const audioConstraints = {
      channelCount: 1,
      sampleRate: 16000,
      autoGainControl: true,
      noiseSuppression: true,
      echoCancellation: true
    };

    if (selectedDeviceId) {
      audioConstraints.deviceId = { exact: selectedDeviceId };
    }

    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    if (!socket.connected) {
      await stopSession();  // tears down the AudioContext the manual track-stop left open
      return false;
    }
    const source = audioContext.createMediaStreamSource(mediaStream);

    // Analyser node for level metering (after gain, before worklet)
    analyserNode = audioContext.createAnalyser();
    analyserNode.fftSize = 256;
    analyserNode.smoothingTimeConstant = 0.5;

    audioProcessor = new AudioWorkletNode(audioContext, 'audio-processor');

    // Audio graph: source -> analyser -> worklet -> destination
    source.connect(analyserNode);
    analyserNode.connect(audioProcessor);
    audioProcessor.connect(audioContext.destination);

    startLevelMeter();

    audioProcessor.port.onmessage = (e) => {
      if (!socket.connected) return;

      const inputData = e.data;
      const buffer = new ArrayBuffer(inputData.length * 2);
      const view = new DataView(buffer);

      for (let i = 0; i < inputData.length; i++) {
        const s = Math.max(-1, Math.min(1, inputData[i]));
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      }

      // Convert to base64 in chunks to avoid call stack overflow on large buffers.
      const bytes = new Uint8Array(buffer);
      let binary = '';
      for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
      }
      const base64Audio = btoa(binary);

      socket.emit('audio_buffer_append', {
        secret_key: user_secret_key,
        audio: base64Audio
      });
    };

    return true;
  } catch (error) {
    console.error('Error accessing microphone:', error);
    await stopSession();
    return false;
  }
}

function startLevelMeter() {
  const container = document.getElementById('mic-level-container');
  const bar = document.getElementById('mic-level-bar');
  const dbLabel = document.getElementById('mic-level-db');
  if (!container || !bar || !dbLabel) return;

  container.style.display = '';
  const dataArray = new Float32Array(analyserNode.fftSize);

  let smoothPct = 0;
  const RISE_COEFF = 0.25;  // fast attack
  const FALL_COEFF = 0.025;  // slow decay

  function update() {
    if (!analyserNode) return;
    analyserNode.getFloatTimeDomainData(dataArray);

    // Compute RMS level
    let sum = 0;
    for (let i = 0; i < dataArray.length; i++) {
      sum += dataArray[i] * dataArray[i];
    }
    const rms = Math.sqrt(sum / dataArray.length);
    const db = rms > 0 ? 20 * Math.log10(rms) : -100;

    // Map dB to percentage: -60 dB = 0%, 0 dB = 100%
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));

    // Smooth: fast rise, slow fall
    if (pct > smoothPct) {
      smoothPct = smoothPct + (pct - smoothPct) * RISE_COEFF;
    } else {
      smoothPct = smoothPct + (pct - smoothPct) * FALL_COEFF;
    }

    bar.style.width = smoothPct + '%';
    // Color: green below 70%, yellow 70-90%, red above 90%
    if (smoothPct > 90) {
      bar.className = bar.className.replace(/bg-\w+-500/, 'bg-red-500');
      if (dbLabel) {
        console.log("peak");
        dbLabel.classList.remove('peak-active');
        dbLabel.classList.add('peak-active');
      }
    } else if (smoothPct > 70) {
      bar.className = bar.className.replace(/bg-\w+-500/, 'bg-yellow-500');
    } else {
      bar.className = bar.className.replace(/bg-\w+-500/, 'bg-green-500');
    }

    dbLabel.textContent = (db > -100 ? db.toFixed(0) : '--') + 'dB';
    levelAnimFrame = requestAnimationFrame(update);
  }
  levelAnimFrame = requestAnimationFrame(update);
}

function stopLevelMeter() {
  if (levelAnimFrame) {
    cancelAnimationFrame(levelAnimFrame);
    levelAnimFrame = null;
  }
  const container = document.getElementById('mic-level-container');
  const bar = document.getElementById('mic-level-bar');
  const dbLabel = document.getElementById('mic-level-db');
  if (container) container.style.display = 'none';
  if (bar) bar.style.width = '0%';
  if (dbLabel) dbLabel.textContent = '--dB';
}

async function stopSession() {
  stopLevelMeter();
  if (mediaStream) {
    mediaStream.getTracks().forEach(track => track.stop());
    mediaStream = null;
  }
  if (audioProcessor) {
    audioProcessor.disconnect();
    audioProcessor = null;
  }
  if (analyserNode) {
    analyserNode.disconnect();
    analyserNode = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
}

// startSession / stopSession are called by the mic toggle in panel.html

// Enumerate and populate microphone devices
async function enumerateDevices() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const audioInputs = devices.filter(device => device.kind === 'audioinput');

    if (!micDeviceSelector || audioInputs.length === 0) {
      return;
    }

    // Clear existing options except the first one
    micDeviceSelector.innerHTML = '<option value="">Default Microphone</option>';

    // Add all audio input devices
    audioInputs.forEach(device => {
      const option = document.createElement('option');
      option.value = device.deviceId;
      option.textContent = device.label || `Microphone ${micDeviceSelector.options.length}`;
      micDeviceSelector.appendChild(option);
    });

    // Show the selector if there are devices available
    if (audioInputs.length > 0) {
      micDeviceSelector.style.display = '';
    }

    // Restore previously selected device
    const savedDeviceId = localStorage.getItem('selectedMicDeviceId');
    if (savedDeviceId) {
      micDeviceSelector.value = savedDeviceId;
      selectedDeviceId = savedDeviceId;
    }
  } catch (error) {
    console.error('Error enumerating devices:', error);
  }
}

// Handle device change
async function handleDeviceChange() {
  const wasRecording = recording;

  if (wasRecording) {
    await stopRecording();
  }

  // Update selected device
  selectedDeviceId = micDeviceSelector.value || null;
  localStorage.setItem('selectedMicDeviceId', selectedDeviceId || '');

  if (wasRecording) {
    await startRecording();
  }
}

// Initialize device selector on load
document.addEventListener('DOMContentLoaded', async function () {
  micDeviceSelector = document.getElementById('mic-device-selector');

  if (micDeviceSelector) {
    // Check if microphone is available
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      // Request initial permission to get device labels
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(track => track.stop());
        await enumerateDevices();
      } catch (error) {
        console.warn('Microphone permission not granted:', error);
      }

      // Listen for device changes
      micDeviceSelector.addEventListener('change', handleDeviceChange);

      // Update device list when devices are added/removed
      navigator.mediaDevices.addEventListener('devicechange', enumerateDevices);
    }
  }
});
