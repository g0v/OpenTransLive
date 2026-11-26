/*
 * This file is part of g0v/OpenTransLive.
 * Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
 * Licensed under the GNU AGPL v3.0
 * See LICENSE for details.
 */
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
let audioContext = null;
let audioProcessor = null;
// Mixable active audio inputs ('mic' | 'tab' -> { stream, sourceNode, analyser }).
const activeInputs = new Map();
let captureGeneration = 0;
let levelAnimFrame = null;
let wakeLockSentinel = null;
let wakeLockListening = false;
let fatalAuthStopped = false;

// Two independent health signals share one indicator: the browser<->server socket,
// and the server<->Scribe transcription pipeline. Socket trouble outranks scribe
// trouble (no socket means no audio reaches the server at all), so keep them apart
// and re-render instead of letting whichever event fired last win.
let socketStatus = { cls: 'bg-gray-400', text: 'Connecting…' };
// null | 'connected' | 'reconnecting' | 'stopped' | 'misconfigured' | 'rejected'. The
// last two are folded in from the stop reason rather than tracked separately: the
// server does not retry them on its own right away, and no other reason changes what
// we render.
let scribeState = null;
// The provider's refusal message type while scribeState is 'rejected'.
let scribeRefusal = '';
let micActive = false;   // mirrors the panel mic toggle; see onMicStateChange

function setSocketStatus(cls, text) {
  socketStatus = { cls: cls, text: text };
  renderStatus();
}

// Called by syncCaptureButtons() in panel.html. 'stopped' means opposite things depending on
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

function showTransientNotice(text, durationMs = 5000) {
  const unchanged = text === transientNotice;
  transientNotice = text;
  clearTimeout(transientNoticeTimer);
  transientNoticeTimer = setTimeout(function () {
    transientNotice = null;
    renderStatus();
  }, durationMs);
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
    } else if (micActive && scribeState === 'rejected') {
      // The provider refused the credentials, quota, account terms or parameters. The
      // server stops retrying; mic off/on retries at once, and with the mic left on the
      // server tries again after about 5 minutes.
      cls = 'bg-red-500';
      text = 'Transcription refused by the provider — ' + refusalHint(scribeRefusal);
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

function refusalHint(refusal) {
  if (refusal === 'auth_error') return 'check its API key';
  if (refusal === 'quota_exceeded' || refusal === 'quota_exceeded_error') return 'quota exceeded';
  if (refusal === 'unaccepted_terms') return 'accept its terms in the provider account';
  return 'see the server log';
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
  fatalAuthStopped = false;
  // Show pending state — auth is not yet confirmed by the server
  setSocketStatus('bg-yellow-400', 'Authenticating…');
  console.log('WebSocket connection opened');

  // Join the session room
  socket.emit('join_session', { session_id: sessionId, secret_key: user_secret_key });
  // Establish the clock offset before latency numbers start rendering.
  syncServerClock(5, 200);
});

socket.on('disconnect', function (reason) {
  // Stale scribe state: the server may have stopped or restarted the session while
  // we were away. join_session replays the real state on reconnect.
  scribeState = null;
  setSocketStatus('bg-red-500', 'Disconnected');
  console.log('WebSocket connection closed:', reason);
  // Server-initiated disconnects (graceful shutdown or transient DB lookup retry)
  // do not auto-reconnect in Socket.IO; reconnect automatically unless auth failed.
  if (reason === 'io server disconnect' && !fatalAuthStopped) {
    setTimeout(function () {
      if (!socket.connected && !fatalAuthStopped) socket.connect();
    }, 1000);
  }
});

// A refused handshake (e.g. the server's connection caps) is not retried by the
// client on its own; transport failures are (socket.active stays true).
socket.on('connect_error', function (err) {
  setSocketStatus('bg-red-500', 'Connection error: ' + ((err && err.message) || 'unknown'));
  console.warn('WebSocket connect_error:', err);
  if (!socket.active) setTimeout(function () { socket.connect(); }, 10000);
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
    if (typeof recording !== 'undefined' && recording) {
      socket.emit('mic_on', { session_id: sessionId, secret_key: user_secret_key });
    }
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
  scribeState = data.reason === 'misconfigured' || data.reason === 'rejected' ? data.reason : data.state;
  scribeRefusal = data.reason === 'rejected' ? (data.refusal || '') : '';
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
    fatalAuthStopped = true;
    setSocketStatus('bg-orange-500', 'Unauthorized — microphone stopped');
    stopRecording();
    return;
  }
  if (code === 'service_unavailable') {
    showTransientNotice('Credentials could not be verified — reconnecting…');
    return;
  }
  // Recoverable (invalid_payload, rate_limited, or an unknown code): keep streaming
  // and just surface it, so a transient rejection doesn't take the session down.
  showTransientNotice(code === 'rate_limited'
    ? 'Server throttled a request — still streaming'
    : 'Server rejected a message — still streaming');
});

const tabAudioSupported = !!(navigator.mediaDevices && typeof navigator.mediaDevices.getDisplayMedia === 'function');

class NoTabAudioError extends Error {}

async function openTabAudio() {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    // Chromium rejects audio-only requests; the video track is stopped right away.
    video: true,
    // Speech processing tuned for microphones damages already-mixed program audio.
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    // Chromium-only hints; other browsers ignore them.
    selfBrowserSurface: 'exclude',
    surfaceSwitching: 'include',
    systemAudio: 'include',
    monitorTypeSurfaces: 'include',
  });
  stream.getVideoTracks().forEach(track => track.stop());
  if (!stream.getAudioTracks().length) {
    stream.getTracks().forEach(track => track.stop());
    throw new NoTabAudioError('The share has no audio track');
  }
  return stream;
}

function openMicrophone() {
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
  return navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
}

function hasCaptureSource(source) {
  return activeInputs.has(source);
}

function activeCaptureCount() {
  return activeInputs.size;
}

// Keep mobile screens awake while capturing so locking doesn't kill the microphone;
// re-acquire on visibilitychange because browsers drop wake locks when hidden.
async function holdWakeLock() {
  if (!audioContext || activeInputs.size === 0 || wakeLockSentinel || document.visibilityState !== 'visible' || !('wakeLock' in navigator)) {
    return;
  }
  try {
    const sentinel = await navigator.wakeLock.request('screen');
    if (!audioContext || activeInputs.size === 0 || wakeLockSentinel) {
      sentinel.release().catch(() => {});
      return;
    }
    wakeLockSentinel = sentinel;
    sentinel.onrelease = function () {
      if (wakeLockSentinel === sentinel) wakeLockSentinel = null;
    };
  } catch (error) {
    console.warn('Screen wake lock not granted:', error);
  }
}

function releaseWakeLock() {
  if (wakeLockListening) {
    document.removeEventListener('visibilitychange', holdWakeLock);
    wakeLockListening = false;
  }
  if (wakeLockSentinel) {
    const sentinel = wakeLockSentinel;
    wakeLockSentinel = null;
    sentinel.release().catch(() => {});
  }
}

async function handleSourceInterrupted(source) {
  if (!recording) return;
  if (activeInputs.size > 1) {
    closeCaptureSource(source);
    showTransientNotice(
      source === 'tab'
        ? 'Tab audio sharing ended — microphone still active'
        : 'Microphone disconnected — tab audio still streaming',
      8000,
    );
    return;
  }

  await stopRecording();
  showTransientNotice(
    source === 'tab'
      ? 'Tab audio sharing ended'
      : 'Microphone disconnected — capture stopped',
    8000,
  );
}

// Opens and mixes `source` ('mic' | 'tab') into the shared AudioContext worklet.
// Resolves 'started', 'no-tab-audio', or 'failed'.
async function openCaptureSource(source = 'mic') {
  if (activeInputs.has(source)) return 'started';
  if (!socket.connected) return 'failed';
  const attempt = captureGeneration;
  const createdPipeline = !audioContext;
  let stream = null;
  try {
    // Open the input before any other await so the screen-share picker keeps transient user activation.
    stream = source === 'tab' ? await openTabAudio() : await openMicrophone();
    if (attempt !== captureGeneration || !socket.connected) {
      stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
      return 'failed';
    }
    if (!audioContext) {
      audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      await audioContext.audioWorklet.addModule('/static/js/audio-processor.js');
      if (attempt !== captureGeneration || !socket.connected) {
        stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
        await stopSession();
        return 'failed';
      }
      // The worklet reads channel 0 only; down-mix stereo tab audio instead of
      // dropping the right channel.
      audioProcessor = new AudioWorkletNode(audioContext, 'audio-processor', {
        channelCount: 1,
        channelCountMode: 'explicit',
      });
      audioProcessor.connect(audioContext.destination);
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
    } else if (audioContext.state === 'suspended') {
      await audioContext.resume();
    }

    const sourceNode = audioContext.createMediaStreamSource(stream);
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.5;
    sourceNode.connect(analyser);
    analyser.connect(audioProcessor);

    stream.getAudioTracks().forEach(track => track.addEventListener('ended', function () {
      const entry = activeInputs.get(source);
      if (entry && entry.stream === stream) void handleSourceInterrupted(source);
    }));

    activeInputs.set(source, { stream, sourceNode, analyser });
    startLevelMeter();
    if (!wakeLockListening) {
      document.addEventListener('visibilitychange', holdWakeLock);
      wakeLockListening = true;
    }
    void holdWakeLock();
    return 'started';
  } catch (error) {
    console.error(`Error opening ${source} audio capture:`, error);
    if (stream) stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
    if (createdPipeline && activeInputs.size === 0) await stopSession();
    return error instanceof NoTabAudioError ? 'no-tab-audio' : 'failed';
  }
}

function closeCaptureSource(source) {
  const input = activeInputs.get(source);
  if (!input) return;
  activeInputs.delete(source);
  input.stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
  input.sourceNode.disconnect();
  input.analyser.disconnect();
  resetLevelMeterUI(source);
  if (typeof syncCaptureButtons === 'function') syncCaptureButtons();
}

async function startSession(source = 'mic') {
  fatalAuthStopped = false;
  return openCaptureSource(source);
}

const meterSmooth = { mic: 0, tab: 0 };
const meterLastColor = { mic: null, tab: null };
const meterLastDbText = { mic: null, tab: null };

function meterElements(source) {
  const prefix = source === 'tab' ? 'tab-level' : 'mic-level';
  return {
    container: document.getElementById(prefix + '-container'),
    bar: document.getElementById(prefix + '-bar'),
    dbLabel: document.getElementById(prefix + '-db'),
  };
}

function resetLevelMeterUI(source) {
  meterSmooth[source] = 0;
  meterLastColor[source] = null;
  meterLastDbText[source] = null;
  const { container, bar, dbLabel } = meterElements(source);
  if (container) container.style.display = 'none';
  if (bar) bar.style.width = '0%';
  if (dbLabel) dbLabel.textContent = '--dB';
}

function startLevelMeter() {
  for (const source of ['mic', 'tab']) {
    const { container } = meterElements(source);
    if (container) container.style.display = activeInputs.has(source) ? '' : 'none';
  }
  if (levelAnimFrame) return;

  const dataArray = new Float32Array(256);
  const RISE_COEFF = 0.25;  // fast attack
  const FALL_COEFF = 0.025;  // slow decay

  function update() {
    if (!audioContext || activeInputs.size === 0) {
      levelAnimFrame = null;
      return;
    }
    for (const source of ['mic', 'tab']) {
      const input = activeInputs.get(source);
      const { bar, dbLabel } = meterElements(source);
      if (!input || !bar || !dbLabel) continue;
      input.analyser.getFloatTimeDomainData(dataArray);

      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        sum += dataArray[i] * dataArray[i];
      }
      const rms = Math.sqrt(sum / dataArray.length);
      const db = rms > 0 ? 20 * Math.log10(rms) : -100;
      const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));

      meterSmooth[source] += (pct - meterSmooth[source]) * (pct > meterSmooth[source] ? RISE_COEFF : FALL_COEFF);
      bar.style.width = meterSmooth[source] + '%';

      const color = meterSmooth[source] > 90 ? 'bg-red-500' : meterSmooth[source] > 70 ? 'bg-yellow-500' : 'bg-green-500';
      if (color !== meterLastColor[source]) {
        bar.className = bar.className.replace(/bg-\w+-500/, color);
        meterLastColor[source] = color;
      }
      dbLabel.classList.toggle('peak-active', meterSmooth[source] > 90);

      const dbText = (db > -100 ? db.toFixed(0) : '--') + 'dB';
      if (dbText !== meterLastDbText[source]) {
        dbLabel.textContent = dbText;
        meterLastDbText[source] = dbText;
      }
    }
    levelAnimFrame = requestAnimationFrame(update);
  }
  levelAnimFrame = requestAnimationFrame(update);
}

function stopLevelMeter() {
  if (levelAnimFrame) {
    cancelAnimationFrame(levelAnimFrame);
    levelAnimFrame = null;
  }
  resetLevelMeterUI('mic');
  resetLevelMeterUI('tab');
}

async function stopSession() {
  captureGeneration++;
  releaseWakeLock();
  stopLevelMeter();
  for (const source of ['mic', 'tab']) {
    closeCaptureSource(source);
  }
  if (audioProcessor) {
    audioProcessor.port.onmessage = null;  // chunks the worklet already posted must not reach the socket after stop
    audioProcessor.disconnect();
    audioProcessor = null;
  }
  if (audioContext) {
    const ctx = audioContext;
    audioContext = null;
    await ctx.close().catch(() => {});
  }
}

// startSession / stopSession are called by the source toggles in panel.html

// Enumerate and populate audio input devices (microphones only; tab audio has its own toggle).
async function enumerateDevices() {
  if (!micDeviceSelector) return;
  let audioInputs = [];
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    // Without microphone permission the browser lists a placeholder with no id.
    audioInputs = devices.filter(device => device.kind === 'audioinput' && device.deviceId);
  } catch (error) {
    console.error('Error enumerating devices:', error);
  }
  if (audioInputs.length === 0) return;

  micDeviceSelector.innerHTML = '<option value="">Default Microphone</option>';

  audioInputs.forEach(device => {
    const option = document.createElement('option');
    option.value = device.deviceId;
    option.textContent = device.label || `Microphone ${micDeviceSelector.options.length}`;
    micDeviceSelector.appendChild(option);
  });
  micDeviceSelector.style.display = '';

  // Restore previously selected microphone, ignoring the legacy 'tab-audio' value.
  const savedDeviceId = localStorage.getItem('selectedMicDeviceId');
  if (savedDeviceId && savedDeviceId !== 'tab-audio') {
    micDeviceSelector.value = savedDeviceId;
    selectedDeviceId = savedDeviceId;
  }
}

// Switch microphone in place without disturbing tab audio or restarting Scribe.
async function handleDeviceChange() {
  selectedDeviceId = micDeviceSelector.value || null;
  localStorage.setItem('selectedMicDeviceId', selectedDeviceId || '');

  if (!recording || !hasCaptureSource('mic')) return;

  closeCaptureSource('mic');
  const status = await openCaptureSource('mic');
  if (typeof syncCaptureButtons === 'function') syncCaptureButtons();
  if (status !== 'started' && activeInputs.size === 0) {
    await stopRecording();
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
      } catch (error) {
        console.warn('Microphone permission not granted:', error);
      }
      // Tab or system audio needs no microphone permission, so list sources either way.
      await enumerateDevices();

      // Listen for device changes
      micDeviceSelector.addEventListener('change', handleDeviceChange);

      // Update device list when devices are added/removed
      navigator.mediaDevices.addEventListener('devicechange', enumerateDevices);
    }
  }
});
