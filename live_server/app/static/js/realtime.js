const socket = io();
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
let selectedDeviceId = null;
let micDeviceSelector = null;

socket.on('connect', function () {
  // Show pending state — auth is not yet confirmed by the server
  statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-yellow-400';
  statusText.textContent = 'Authenticating…';
  console.log('WebSocket connection opened');

  // Join the session room
  socket.emit('join_session', { session_id: sessionId, secret_key: user_secret_key, user_uid: user_uid });
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
      sampleRate: 16000
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
    audioProcessor = new AudioWorkletNode(audioContext, 'audio-processor');

    source.connect(audioProcessor);
    audioProcessor.connect(audioContext.destination);

    audioProcessor.port.onmessage = async (e) => {
      if (!socket.connected) return;

      const inputData = e.data;
      const buffer = new ArrayBuffer(inputData.length * 2);
      const view = new DataView(buffer);

      for (let i = 0; i < inputData.length; i++) {
        const s = Math.max(-1, Math.min(1, inputData[i]));
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      }

      const base64Audio = btoa(String.fromCharCode(...new Uint8Array(buffer)));

      socket.emit('audio_buffer_append', {
        secret_key: user_secret_key,
        user_uid: user_uid,
        audio: base64Audio
      });

      await new Promise(resolve => setTimeout(resolve, 10));
    };

  } catch (error) {
    console.error('Error accessing microphone:', error);
    stopSession();
  }
}

async function stopSession() {
  if (mediaStream) {
    mediaStream.getTracks().forEach(track => track.stop());
    mediaStream = null;
  }
  if (audioProcessor) {
    audioProcessor.disconnect();
    audioProcessor = null;
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
