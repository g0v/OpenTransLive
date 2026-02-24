const socket = io();
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');

socket.on('connect', function () {
  statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-green-500';
  statusText.textContent = 'Connected';
  console.log('WebSocket connection opened');

  // Join the session room
  socket.emit('join_session', { session_id: sessionId });
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
  statusText.textContent = 'Connected to session: ' + data.session_id;
  socket.emit('realtime_connect', { session_id: sessionId });
});

async function startSession() {
  try {
    audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    await audioContext.audioWorklet.addModule('/static/js/audio-processor.js');

    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } });
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

document.querySelector('#start-btn').addEventListener('click', startSession);
document.querySelector('#stop-btn').addEventListener('click', stopSession);
