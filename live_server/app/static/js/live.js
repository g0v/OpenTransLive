const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
const apiKeysForm = document.getElementById('api_keys_form');
const startSessionBtn = document.getElementById('start-session-btn');
const stopSessionBtn = document.getElementById('stop-session-btn');
const transcriptionsBlock = document.getElementById('transcriptions');

let openaiWs = null;
let audioContext = null;
let mediaStream = null;
let audioProcessor = null;

let user_openai_api_key = sessionStorage.getItem('user_openai_api_key');
if (user_openai_api_key) {
  document.querySelector('input[name="user_openai_api_key"]').value = user_openai_api_key;
}

let user_elevenlabs_api_key = sessionStorage.getItem('user_elevenlabs_api_key');
if (user_elevenlabs_api_key) {
  document.querySelector('input[name="user_elevenlabs_api_key"]').value = user_elevenlabs_api_key;
}

function connectBackend() {

  // Connect to WebSocket
  const socket = io();

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
  });

  socket.on('error', function (data) {
    console.error('WebSocket error:', data);
    statusIndicator.className = 'inline-block w-3 h-3 rounded-full bg-red-500';
    statusText.textContent = 'Error: ' + data.message;
  });
};

function stopSession() {
  if (openaiWs) {
    openaiWs.close();
    openaiWs = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach(track => track.stop());
    mediaStream = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (audioProcessor) {
    audioProcessor = null;
  }

  startSessionBtn.classList.remove('hidden');
  stopSessionBtn.classList.add('hidden');
}

stopSessionBtn.addEventListener('click', stopSession);

function connectOpenAIRealtime() {
  openaiWs = new WebSocket('wss://api.openai.com/v1/realtime?intent=transcription',
    [
      "realtime",
      // Auth
      "openai-insecure-api-key." + user_openai_api_key
    ]
  );
  const ws = openaiWs;
  ws.onopen = async () => {
    ws.send(JSON.stringify({
      "type": "session.update",
      "session": {
        "type": "transcription",
        "audio": {
          "input": {
            "format": {
              "type": "audio/pcm",
              "rate": 24000
            },
            "noise_reduction": {
              "type": "far_field"
            },
            "transcription": {
              "model": "gpt-4o-transcribe",
              "prompt": "",
              "language": "en"
            },
            "turn_detection": {
              "type": "server_vad",
              "threshold": 0.3,
              "prefix_padding_ms": 300,
              "silence_duration_ms": 500
            }
          }
        }
      }
    }));

    // Update UI
    startSessionBtn.classList.add('hidden');
    stopSessionBtn.classList.remove('hidden');

    try {
      audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
      await audioContext.audioWorklet.addModule('/static/js/audio-processor.js');

      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 24000 } });
      if (!openaiWs || openaiWs.readyState !== WebSocket.OPEN) {
        mediaStream.getTracks().forEach(track => track.stop());
        mediaStream = null;
        return;
      }
      const source = audioContext.createMediaStreamSource(mediaStream);
      audioProcessor = new AudioWorkletNode(audioContext, 'audio-processor');

      source.connect(audioProcessor);
      audioProcessor.connect(audioContext.destination);

      audioProcessor.port.onmessage = async (e) => {
        if (ws.readyState !== WebSocket.OPEN) return;

        const inputData = e.data;
        const buffer = new ArrayBuffer(inputData.length * 2);
        const view = new DataView(buffer);

        for (let i = 0; i < inputData.length; i++) {
          const s = Math.max(-1, Math.min(1, inputData[i]));
          view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        }

        const base64Audio = btoa(String.fromCharCode(...new Uint8Array(buffer)));

        ws.send(JSON.stringify({
          type: 'input_audio_buffer.append',
          audio: base64Audio
        }));

        await new Promise(resolve => setTimeout(resolve, 10));
      };

    } catch (error) {
      console.error('Error accessing microphone:', error);
      stopSession();
    }
  };

  ws.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    console.log(data);
    if (data?.type == "conversation.item.input_audio_transcription.completed") {
      transcriptionsBlock.innerHTML += `<p>${data.transcript}</p>`;
    }
  };

  ws.onclose = async () => {
    console.log('OpenAI WebSocket closed');
    stopSession();
  }
}

apiKeysForm.addEventListener('submit', function (event) {
  event.preventDefault();
  const formData = new FormData(apiKeysForm);
  user_openai_api_key = formData.get('user_openai_api_key');
  user_elevenlabs_api_key = formData.get('user_elevenlabs_api_key');
  sessionStorage.setItem('user_openai_api_key', user_openai_api_key);
  sessionStorage.setItem('user_elevenlabs_api_key', user_elevenlabs_api_key);
  transcriptionsBlock.innerHTML = '';
  connectOpenAIRealtime();
});

// Connect to the backend WebSocket immediately
connectBackend();