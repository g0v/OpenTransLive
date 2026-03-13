const socket = io();
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
let selectedDeviceId = null;
let micDeviceSelector = null;
let mediaStream = null;
let audioContext = null;
let audioProcessor = null;
let gainNode = null;
let compressorNode = null;
let analyserNode = null;
let levelAnimFrame = null;

socket.on('connect', function () {
  // Show pending state — auth is not yet confirmed by the server
  statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-yellow-400';
  statusText.textContent = 'Authenticating…';
  console.log('WebSocket connection opened');

  // Join the session room
  socket.emit('join_session', { session_id: sessionId, secret_key: user_secret_key });
});

socket.on('disconnect', function () {
  statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-red-500';
  statusText.textContent = 'Disconnected';
  console.log('WebSocket connection closed');
});

socket.on('connected', function (data) {
  console.log('WebSocket connected:', data);
});

socket.on('joined_session', function (data) {
  console.log('Joined session:', data);
  if (data.authorized) {
    statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-green-500';
    statusText.textContent = 'Connected to session: ' + data.session_id;
    socket.emit('realtime_connect', { session_id: sessionId });
  } else {
    statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-orange-500';
    statusText.textContent = 'Unauthorized';
    console.warn('join_session: not authorized for session', data.session_id);
  }
});

socket.on('error', function (data) {
  console.error('WebSocket error:', data);
  stopSession();
  setMicState('off');
});

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
      mediaStream.getTracks().forEach(track => track.stop());
      mediaStream = null;
      return;
    }
    const source = audioContext.createMediaStreamSource(mediaStream);

    // Dynamic range compressor to normalize loud/quiet audio
    compressorNode = audioContext.createDynamicsCompressor();
    compressorNode.threshold.setValueAtTime(-30, audioContext.currentTime);  // compress above -30 dB
    compressorNode.knee.setValueAtTime(20, audioContext.currentTime);        // soft knee
    compressorNode.ratio.setValueAtTime(6, audioContext.currentTime);        // 6:1 compression
    compressorNode.attack.setValueAtTime(0.005, audioContext.currentTime);   // fast attack
    compressorNode.release.setValueAtTime(0.15, audioContext.currentTime);   // moderate release

    // Gain node for make-up gain after compression
    gainNode = audioContext.createGain();
    gainNode.gain.setValueAtTime(2.0, audioContext.currentTime);  // +6 dB make-up gain

    // Analyser node for level metering (after gain, before worklet)
    analyserNode = audioContext.createAnalyser();
    analyserNode.fftSize = 256;
    analyserNode.smoothingTimeConstant = 0.5;

    audioProcessor = new AudioWorkletNode(audioContext, 'audio-processor');

    // Audio graph: source -> compressor -> gain -> analyser -> worklet -> destination
    source.connect(compressorNode);
    compressorNode.connect(gainNode);
    gainNode.connect(analyserNode);
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

  } catch (error) {
    console.error('Error accessing microphone:', error);
    stopSession();
  }
}

function startLevelMeter() {
  const container = document.getElementById('mic-level-container');
  const bar = document.getElementById('mic-level-bar');
  const dbLabel = document.getElementById('mic-level-db');
  if (!container || !bar || !dbLabel) return;

  container.style.display = '';
  const dataArray = new Float32Array(analyserNode.fftSize);

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

    bar.style.width = pct + '%';
    // Color: green below 70%, yellow 70-90%, red above 90%
    if (pct > 90) {
      bar.className = bar.className.replace(/bg-\w+-500/, 'bg-red-500');
    } else if (pct > 70) {
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
  if (compressorNode) {
    compressorNode.disconnect();
    compressorNode = null;
  }
  if (gainNode) {
    gainNode.disconnect();
    gainNode = null;
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
    // Stop current session
    setMicState('busy');
    await stopSession();
  }

  // Update selected device
  selectedDeviceId = micDeviceSelector.value || null;
  localStorage.setItem('selectedMicDeviceId', selectedDeviceId || '');

  if (wasRecording) {
    // Restart session with new device
    await startSession();
    recording = true;
    setMicState('on');
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
