(function () {
    'use strict';

    var session = {
        sessionId: null,
        turns: 0,
        level: 'None',
        reportLevel: null,
        corrections: [],
        messages: [],
        pronunciationScores: [],
    };

    var state = {
        isRecording: false,
        mediaRecorder: null,
        audioChunks: [],
        asrModel: 'deepgram:nova-2',
        defaultPrompt: '',
        defaultGreeting: '',
        stream: null,
        asrSocket: null,
        streamingMode: false,
        finalHandled: false,
        liveMessage: null,
        liveTranscript: '',
        finalTranscript: '',
        waveform: [],
        waveformAnimation: null,
        audioContext: null,
        analyser: null,
        pendingAudioBuffers: [],
    };

    var els = {
        chatArea: document.getElementById('chat-area'),
        placeholder: document.getElementById('chat-placeholder'),
        micButton: document.getElementById('btn-mic'),
        micIcon: document.getElementById('mic-icon'),
        statusText: document.getElementById('status-text'),
        statusDot: document.getElementById('status-dot'),
        level: document.getElementById('level-val'),
        voiceSelect: document.getElementById('voice-select'),
        asrModelSelect: document.getElementById('asr-model-select'),
        promptTextarea: document.getElementById('prompt-textarea'),
        resetPromptButton: document.getElementById('btn-reset-prompt'),
        greetingTextarea: document.getElementById('greeting-textarea'),
        resetGreetingButton: document.getElementById('btn-reset-greeting'),
        endButton: document.getElementById('btn-end'),
        reportButton: document.getElementById('btn-generate'),
        aiSuggestion: document.getElementById('ai-suggestion'),
    };

    if (!els.micButton) {
        return;
    }

    // ── Conversation list ──────────────────────────────────────────────────

    var convListEl = document.getElementById('conv-list');
    var btnNewConv = document.getElementById('btn-new-conv');

    function loadConvList() {
        fetchJson('/api/sessions', {}, 10000).then(function (data) {
            var sessions = data.sessions || [];
            renderConvList(sessions);
            if (sessions.length && sessions[0].level && els.level) {
                els.level.innerText = sessions[0].level;
            }
        }).catch(function () {});
    }

    function renderConvList(convs) {
        if (!convListEl) { return; }
        convListEl.innerHTML = '';
        if (!convs.length) {
            convListEl.innerHTML = '<div class="conv-empty">No conversations yet</div>';
            return;
        }
        convs.forEach(function (conv) {
            var item = document.createElement('div');
            item.className = 'conv-item' + (conv.id === session.sessionId ? ' active' : '');
            item.innerHTML =
                '<i class="fa fa-comment conv-item-icon"></i>' +
                '<div class="conv-item-body">' +
                    '<div class="conv-item-title">' + escHtml(conv.title) + '</div>' +
                    '<div class="conv-item-date">' + formatConvDate(conv.updated_at) + '</div>' +
                '</div>' +
                '<button class="conv-delete-btn" title="Delete"><i class="fa fa-trash"></i></button>';
            item.querySelector('.conv-item-body').addEventListener('click', function () {
                if (state.isRecording) { return; }
                switchConversation(conv.id);
            });
            item.querySelector('.conv-item-title').addEventListener('dblclick', function (e) {
                e.stopPropagation();
                startTitleEdit(conv.id, item);
            });
            item.querySelector('.conv-delete-btn').addEventListener('click', function (e) {
                e.stopPropagation();
                if (state.isRecording) { return; }
                deleteConversation(conv.id);
            });
            convListEl.appendChild(item);
        });
    }

    function switchConversation(sessionId) {
        if (sessionId === session.sessionId) { return; }
        fetchJson('/api/sessions/' + sessionId, {}, 10000).then(function (data) {
            resetChatState(sessionId, { skipGreeting: true });
            session.turns = data.turns || 0;
            session.level = data.level || 'B';
            session.corrections = data.corrections || [];
            session.pronunciationScores = data.pronunciation_scores || [];
            session.messages = data.messages || [];
            els.level.innerText = session.level;
            data.messages.forEach(function (msg) {
                var role = msg.role === 'assistant' ? 'ai' : 'user';
                var audioOpts = msg.audio_url ? { audioUrl: msg.audio_url, audioLabel: role === 'ai' ? 'AI voice' : 'Your recording' } : {};
                var node = appendMessage(role, msg.text, audioOpts);
                if (role === 'ai' && msg.text && !msg.audio_url) {
                    appendListenButton(node, msg.text);
                }
                if (role === 'ai' && msg.corrections && msg.corrections.length) {
                    appendCorrections(msg.corrections);
                }
            });
            updateReportStats();
            renderCorrectionsPanel();
            loadConvList();
        }).catch(function () {});
    }

    function deleteConversation(sessionId) {
        if (!confirm('Delete this conversation and its report?')) { return; }
        fetchJson('/api/sessions/' + sessionId, { method: 'DELETE' }, 10000).then(function () {
            if (sessionId === session.sessionId) {
                resetChatState(null);
            }
            if (sessionId === aboutActiveId) { aboutActiveId = null; }
            if (sessionId === reportActiveId) { reportActiveId = null; }
            loadConvList();
            renderPanelConvList('about-conv-list', aboutActiveId, selectAboutSession);
            renderPanelConvList('report-conv-list', reportActiveId, selectReportSession);
        }).catch(function (error) {
            setStatus('Delete failed: ' + extractErrorMessage(error), false, true);
            loadConvList();
        });
    }

    function startTitleEdit(sessionId, itemEl) {
        var titleEl = itemEl.querySelector('.conv-item-title');
        if (!titleEl || titleEl.querySelector('input')) { return; }
        var original = titleEl.innerText;
        var input = document.createElement('input');
        input.className = 'conv-title-input';
        input.value = original;
        titleEl.innerText = '';
        titleEl.appendChild(input);
        input.focus();
        input.select();

        var saved = false;
        function save() {
            if (saved) { return; }
            saved = true;
            var newTitle = input.value.trim() || original;
            fetchJson('/api/sessions/' + sessionId + '/title', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: newTitle }),
            }, 5000).then(loadConvList).catch(loadConvList);
        }
        input.addEventListener('blur', save);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); save(); input.blur(); }
            if (e.key === 'Escape') { saved = true; input.value = original; loadConvList(); }
        });
    }

    function resetChatState(newSessionId, options) {
        options = options || {};
        session.sessionId = newSessionId;
        session.turns = 0;
        session.level = 'None';
        session.reportLevel = null;
        session.corrections = [];
        session.messages = [];
        session.pronunciationScores = [];
        Array.prototype.forEach.call(
            els.chatArea.querySelectorAll('.msg-row, .correction-box'),
            function (el) { el.parentNode.removeChild(el); }
        );
        if (els.placeholder) { els.placeholder.style.display = ''; }
        els.level.innerText = 'None';
        els.aiSuggestion.innerText = 'Complete a conversation session to receive personalized feedback from your AI coach.';
        updateReportStats();
        renderCorrectionsPanel();
        setStatus('Ready - press the mic to speak.', false);
        if (!newSessionId && !options.skipGreeting) {
            renderGreeting();
        }
    }

    function escHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function formatConvDate(isoStr) {
        if (!isoStr) { return ''; }
        var d = new Date(isoStr);
        var now = new Date();
        var diffMs = now - d;
        var dayMs = 86400000;
        if (diffMs < dayMs && d.getDate() === now.getDate()) {
            return 'Today ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        if (diffMs < 2 * dayMs) { return 'Yesterday'; }
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }

    if (btnNewConv) {
        btnNewConv.addEventListener('click', function () {
            if (state.isRecording) { return; }
            resetChatState(null);
            loadConvList();
        });
    }

    loadConvList();
    loadSettings();

    if (els.voiceSelect) {
        var savedVoice = localStorage.getItem('speakai_voice');
        if (savedVoice) {
            els.voiceSelect.value = savedVoice;
            if (!els.voiceSelect.value) {
                els.voiceSelect.value = 'en-US-AriaNeural';
                localStorage.setItem('speakai_voice', els.voiceSelect.value);
            }
        }
        els.voiceSelect.addEventListener('change', function () {
            localStorage.setItem('speakai_voice', els.voiceSelect.value);
        });
    }

    if (els.asrModelSelect) {
        var savedAsrModel = localStorage.getItem('speakai_asr_model');
        if (savedAsrModel) {
            els.asrModelSelect.value = savedAsrModel;
            if (!els.asrModelSelect.value) {
                els.asrModelSelect.value = state.asrModel;
            }
        }
        state.asrModel = els.asrModelSelect.value || state.asrModel;
        els.asrModelSelect.addEventListener('change', function () {
            state.asrModel = els.asrModelSelect.value;
            localStorage.setItem('speakai_asr_model', state.asrModel);
        });
    }

    if (els.promptTextarea) {
        var savedPrompt = localStorage.getItem('speakai_prompt');
        if (savedPrompt) {
            els.promptTextarea.value = savedPrompt;
        }
        els.promptTextarea.addEventListener('input', function () {
            localStorage.setItem('speakai_prompt', els.promptTextarea.value);
        });
    }

    if (els.resetPromptButton) {
        els.resetPromptButton.addEventListener('click', function () {
            els.promptTextarea.value = state.defaultPrompt || '';
            localStorage.setItem('speakai_prompt', els.promptTextarea.value);
        });
    }

    if (els.greetingTextarea) {
        var savedGreeting = localStorage.getItem('speakai_greeting');
        if (savedGreeting) {
            els.greetingTextarea.value = savedGreeting;
        }
        els.greetingTextarea.addEventListener('input', function () {
            localStorage.setItem('speakai_greeting', els.greetingTextarea.value);
            if (!session.sessionId && session.turns === 0) {
                renderGreeting();
            }
        });
    }

    if (els.resetGreetingButton) {
        els.resetGreetingButton.addEventListener('click', function () {
            els.greetingTextarea.value = state.defaultGreeting || '';
            localStorage.setItem('speakai_greeting', els.greetingTextarea.value);
            if (!session.sessionId && session.turns === 0) {
                renderGreeting();
            }
        });
    }

    function loadSettings() {
        fetchJson('/api/settings', {}, 10000).then(function (settings) {
            state.defaultPrompt = settings.default_prompt || '';
            state.defaultGreeting = settings.default_greeting || '';
            if (els.promptTextarea && !localStorage.getItem('speakai_prompt')) {
                els.promptTextarea.value = state.defaultPrompt;
            }
            if (els.greetingTextarea && !localStorage.getItem('speakai_greeting')) {
                els.greetingTextarea.value = state.defaultGreeting;
            }
            if (els.asrModelSelect && !localStorage.getItem('speakai_asr_model')) {
                els.asrModelSelect.value = settings.default_asr_model || state.asrModel;
                state.asrModel = els.asrModelSelect.value || state.asrModel;
            }
            if (els.voiceSelect && !localStorage.getItem('speakai_voice')) {
                els.voiceSelect.value = settings.default_voice || getSelectedVoice();
            }
            if (!session.sessionId && session.turns === 0) {
                renderGreeting();
            }
        }).catch(function () {});
    }

    function getSelectedVoice() {
        if (!els.voiceSelect || !els.voiceSelect.value) {
            return 'en-US-AriaNeural';
        }
        return els.voiceSelect.value;
    }

    function getSelectedAsrModel() {
        return els.asrModelSelect && els.asrModelSelect.value ? els.asrModelSelect.value : state.asrModel;
    }

    function getSelectedPrompt() {
        return els.promptTextarea && els.promptTextarea.value.trim() ? els.promptTextarea.value : state.defaultPrompt;
    }

    function getSelectedGreeting() {
        return els.greetingTextarea && els.greetingTextarea.value.trim() ? els.greetingTextarea.value.trim() : state.defaultGreeting;
    }

    function renderGreeting() {
        if (!els.chatArea) { return; }
        var text = getSelectedGreeting();
        Array.prototype.forEach.call(els.chatArea.querySelectorAll('.greeting-row'), function (node) {
            node.parentNode.removeChild(node);
        });
        if (!text) { return; }
        appendMessage('ai', text, { keepPlaceholder: true, greeting: true });
        if (els.placeholder) {
            els.placeholder.style.display = '';
        }
    }

    function getVoiceLabel(voice) {
        if (!els.voiceSelect) {
            return 'AI voice';
        }
        var option = els.voiceSelect.querySelector('option[value="' + (voice || getSelectedVoice()) + '"]');
        return option ? 'AI voice: ' + option.text : 'AI voice';
    }

    var aboutActiveId = null;
    var reportActiveId = null;

    function renderPanelConvList(listElId, activeId, onSelect) {
        var listEl = document.getElementById(listElId);
        if (!listEl) { return; }
        fetchJson('/api/sessions', {}, 10000).then(function (data) {
            var sessions = data.sessions || [];
            listEl.innerHTML = '';
            if (!sessions.length) {
                listEl.innerHTML = '<div class="conv-empty">No conversations yet</div>';
                return;
            }
            sessions.forEach(function (conv) {
                var item = document.createElement('div');
                item.className = 'conv-item' + (conv.id === activeId ? ' active' : '');
                item.dataset.sid = conv.id;
                item.innerHTML =
                    '<i class="fa fa-comment conv-item-icon"></i>' +
                    '<div class="conv-item-body">' +
                        '<div class="conv-item-title">' + escHtml(conv.title) + '</div>' +
                        '<div class="conv-item-date">' + formatConvDate(conv.updated_at) + '</div>' +
                    '</div>';
                item.addEventListener('click', function () {
                    Array.prototype.forEach.call(listEl.querySelectorAll('.conv-item'), function (el) {
                        el.classList.remove('active');
                    });
                    item.classList.add('active');
                    onSelect(conv.id, conv.title);
                });
                listEl.appendChild(item);
            });
            var toActivate = activeId
                ? listEl.querySelector('[data-sid="' + activeId + '"]')
                : listEl.querySelector('.conv-item');
            if (toActivate) { toActivate.click(); }
        }).catch(function () {});
    }

    function selectAboutSession(sessionId, title) {
        aboutActiveId = sessionId;
        fetchJson('/api/sessions/' + sessionId, {}, 10000).then(function (data) {
            renderCorrectionsPanelFromData(data.corrections || [], data.title || title);
        }).catch(function () {});
    }

    function selectReportSession(sessionId, title) {
        reportActiveId = sessionId;
        var titleEl = document.getElementById('report-conv-title');
        if (titleEl) { titleEl.innerText = title || ''; }
        var aiSugEl = document.getElementById('ai-suggestion');
        if (aiSugEl) { aiSugEl.innerText = 'Click “Generate Full Report” to get AI coach feedback for this conversation.'; }
        fetchJson('/api/sessions/' + sessionId, {}, 10000).then(function (data) {
            var corrections = data.corrections || [];
            var breakdown = { tense: 0, preposition: 0, article: 0, other: 0 };
            corrections.forEach(function (item) {
                var type = (item.type || 'other').toLowerCase();
                if (type.indexOf('tense') >= 0) { breakdown.tense += 1; }
                else if (type.indexOf('prep') >= 0) { breakdown.preposition += 1; }
                else if (type.indexOf('article') >= 0) { breakdown.article += 1; }
                else { breakdown.other += 1; }
            });
            renderSummary({
                turns: data.turns || 0,
                level: data.level || '-',
                total_errors: breakdown.tense + breakdown.preposition + breakdown.article + breakdown.other,
                error_breakdown: breakdown,
                pronunciation: aggregatePronunciation(data.pronunciation_scores || []),
            });
        }).catch(function () {});
    }

    var show1El = document.querySelector('.show-1');
    if (show1El) {
        show1El.addEventListener('click', function () {
            renderPanelConvList('about-conv-list', aboutActiveId, selectAboutSession);
        });
    }

    var show4El = document.querySelector('.show-4');
    if (show4El) {
        show4El.addEventListener('click', function () {
            var menu4 = document.getElementById('menu-4');
            var leftPanel = menu4 && menu4.querySelector('.chat-left-panel');
            if (leftPanel) { leftPanel.style.display = ''; }
            renderPanelConvList('report-conv-list', reportActiveId, selectReportSession);
        });
    }

    // ──────────────────────────────────────────────────────────────────────

    els.micButton.addEventListener('click', function () {
        if (state.isRecording) {
            stopRecording();
        } else {
            startRecording();
        }
    });

    els.endButton.addEventListener('click', function () {
        if (session.turns === 0) {
            setStatus('No conversation yet - record one turn first.', false, true);
            return;
        }
        updateReportStats();
        if (window.jQuery) {
            window.jQuery('.show-4').trigger('click');
        }
        var menu4 = document.getElementById('menu-4');
        var leftPanel = menu4 && menu4.querySelector('.chat-left-panel');
        if (leftPanel) { leftPanel.style.display = 'none'; }
        reportActiveId = session.sessionId;
        selectReportSession(session.sessionId);
        generateReportFeedback();
    });

    els.reportButton.addEventListener('click', function () {
        generateReportFeedback();
    });

    function generateReportFeedback() {
        var targetId = reportActiveId || session.sessionId;
        els.aiSuggestion.innerText = 'Generating feedback...';
        fetchJson('/api/report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: targetId,
                turns: session.turns,
                level: session.level,
                messages: session.messages,
                corrections: session.corrections,
                pronunciation_scores: session.pronunciationScores,
            }),
        }, 20000).then(function (data) {
            els.aiSuggestion.innerText = data.feedback || 'Keep practicing - consistency matters.';
            if (data.summary) {
                renderSummary(data.summary);
                session.reportLevel = data.summary.level || null;
                if (els.level) {
                    els.level.innerText = data.summary.level || 'None';
                }
            }
        }).catch(function (error) {
            els.aiSuggestion.innerText = extractErrorMessage(error);
        });
    }

    function startRecording() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setStatus('This browser does not support microphone capture.', false, true);
            return;
        }

        navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
            var mimeType = pickSupportedMimeType();
            state.mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType: mimeType }) : new MediaRecorder(stream);
            state.audioChunks = [];
            state.isRecording = true;
            state.stream = stream;
            state.finalHandled = false;
            state.liveTranscript = '';
            state.finalTranscript = '';
            state.waveform = [];
            state.pendingAudioBuffers = [];
            state.streamingMode = false;
            state.liveMessage = appendMessage('user', '', {});
            setLiveBubbleText('');
            attachLiveWave(state.liveMessage);
            startWaveformCapture(stream);

            state.mediaRecorder.ondataavailable = function (event) {
                if (event.data && event.data.size > 0) {
                    state.audioChunks.push(event.data);
                    if (state.streamingMode && state.asrSocket) {
                        event.data.arrayBuffer().then(function (buffer) {
                            if (state.asrSocket && state.asrSocket.readyState === WebSocket.OPEN) {
                                state.asrSocket.send(buffer);
                            } else {
                                state.pendingAudioBuffers.push(buffer);
                            }
                        });
                    }
                }
            };
            state.mediaRecorder.onstop = function () {
                if (state.streamingMode) {
                    handleStreamingStop(stream);
                } else {
                    handleAudioStop(stream);
                }
            };
            if (state.streamingMode) {
                openAsrSocket();
            }
            state.mediaRecorder.start(state.streamingMode ? 100 : undefined);

            els.micButton.classList.add('recording');
            els.micIcon.className = 'fa fa-stop';
            setStatus(state.streamingMode ? 'Listening live... click again to stop.' : 'Recording... click again to stop.', true);
        }).catch(function (error) {
            console.error(error);
            setStatus('Microphone access was denied.', false, true);
        });
    }

    function stopRecording() {
        state.isRecording = false;
        els.micButton.classList.remove('recording');
        els.micIcon.className = 'fa fa-microphone';
        setStatus(state.streamingMode ? 'Finishing live transcript...' : 'Uploading audio...', true);
        if (state.mediaRecorder && state.mediaRecorder.state !== 'inactive') {
            state.mediaRecorder.stop();
        }
    }

    function openAsrSocket() {
        var protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
        var socket = new WebSocket(protocol + window.location.host + '/ws/asr');
        state.asrSocket = socket;
        socket.binaryType = 'arraybuffer';
        socket.onopen = function () {
            while (state.pendingAudioBuffers.length && socket.readyState === WebSocket.OPEN) {
                socket.send(state.pendingAudioBuffers.shift());
            }
        };
        socket.onmessage = function (event) {
            var data;
            try {
                data = JSON.parse(event.data);
            } catch (_error) {
                return;
            }
            if (data.type === 'error') {
                state.streamingMode = false;
                setStatus(data.message || 'Live ASR unavailable; using upload mode.', false, true);
                return;
            }
            if (data.type === 'transcript') {
                updateLiveTranscript(data.transcript || '', Boolean(data.is_final));
            }
            if (data.type === 'final') {
                finishStreamingChat(data.transcript || state.liveTranscript, data.pronunciation || {});
            }
        };
        socket.onerror = function () {
            state.streamingMode = false;
            setStatus('Live ASR unavailable; using upload mode.', false, true);
        };
    }

    function handleStreamingStop(stream) {
        stopWaveformCapture();
        stream.getTracks().forEach(function (track) { track.stop(); });
        if (state.asrSocket && state.asrSocket.readyState === WebSocket.OPEN) {
            state.asrSocket.send(JSON.stringify({ type: 'stop' }));
            setTimeout(function () {
                if (!state.finalHandled && state.liveTranscript) {
                    finishStreamingChat(state.liveTranscript, {});
                }
            }, 4000);
        } else {
            state.streamingMode = false;
            handleAudioStop({ getTracks: function () { return []; } });
        }
    }

    function finishStreamingChat(transcript, pronunciation) {
        if (state.finalHandled) {
            return;
        }
        if (!transcript && !state.liveTranscript) {
            state.finalHandled = true;
            fallbackUploadFromLiveMessage();
            return;
        }
        state.finalHandled = true;
        stopWaveformCapture();
        mergeRealtimeAcoustics(pronunciation);

        var blob = new Blob(state.audioChunks, { type: state.audioChunks[0] ? state.audioChunks[0].type : 'audio/webm' });
        var userAudioUrl = blob.size ? URL.createObjectURL(blob) : null;
        state.liveTranscript = transcript || state.liveTranscript || '(no transcription)';
        var bubble = state.liveMessage && state.liveMessage.querySelector('.msg-bubble');
        if (bubble) {
            bubble.innerText = state.liveTranscript;
        }
        attachAudioReplay(state.liveMessage, {
            audioUrl: userAudioUrl,
            audioLabel: 'Your recording',
            waveform: state.waveform,
        });

        var streamingUserMsg = state.liveMessage;
        fetchJson('/api/chat_text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: session.sessionId,
                transcription: state.liveTranscript,
                pronunciation: pronunciation,
                voice: getSelectedVoice(),
                prompt: getSelectedPrompt(),
            }),
        }, 60000).then(function (data) {
            handleChatResponse(data, streamingUserMsg);
        }).catch(function (error) {
            console.error(error);
            if (getErrorCode(error) === 'gemini_quota_exceeded') {
                appendMessage('ai', extractErrorMessage(error), {});
            }
            setStatus(extractErrorMessage(error), false, true);
        });
    }

    function fallbackUploadFromLiveMessage() {
        stopWaveformCapture();
        var blob = new Blob(state.audioChunks, { type: state.audioChunks[0] ? state.audioChunks[0].type : 'audio/webm' });
        if (!blob.size) {
            updateLiveMessageText('No audio captured. Please try again.');
            setStatus('No audio captured. Please try again.', false, true);
            return;
        }
        var userAudioUrl = URL.createObjectURL(blob);
        setStatus('Live transcript unavailable - using full recording...', true);

        var formData = new FormData();
        formData.append('audio', blob, 'recording.webm');
        if (session.sessionId) {
            formData.append('session_id', session.sessionId);
        }
        formData.append('asr_model', getSelectedAsrModel());
        formData.append('voice', getSelectedVoice());
        formData.append('prompt', getSelectedPrompt());

        var fallbackUserMsg = state.liveMessage;
        fetchJson('/api/chat', {
            method: 'POST',
            body: formData,
        }, 60000).then(function (data) {
            updateLiveMessageText(data.transcription || '(no transcription)');
            attachAudioReplay(state.liveMessage, {
                audioUrl: userAudioUrl,
                audioLabel: 'Your recording',
                waveform: state.waveform,
            });
            handleChatResponse(data, fallbackUserMsg);
        }).catch(function (error) {
            console.error(error);
            setStatus(extractErrorMessage(error), false, true);
        });
    }

    function handleAudioStop(stream) {
        stream.getTracks().forEach(function (track) { track.stop(); });
        stopWaveformCapture();
        var blob = new Blob(state.audioChunks, { type: state.audioChunks[0] ? state.audioChunks[0].type : 'audio/webm' });
        if (!blob.size) {
            setStatus('No audio captured. Please try again.', false, true);
            return;
        }
        var userAudioUrl = URL.createObjectURL(blob);

        var formData = new FormData();
        formData.append('audio', blob, 'recording.webm');
        if (session.sessionId) {
            formData.append('session_id', session.sessionId);
        }
        formData.append('asr_model', getSelectedAsrModel());
        formData.append('voice', getSelectedVoice());
        formData.append('prompt', getSelectedPrompt());

        setStatus('Transcribing and generating reply...', true);
        fetchJson('/api/chat', {
            method: 'POST',
            body: formData,
        }, 60000).then(function (data) {
            var uploadUserMsg = state.liveMessage || appendMessage('user', '', {});
            state.liveMessage = uploadUserMsg;
            updateLiveMessageText(data.transcription || '(no transcription)');
            attachAudioReplay(uploadUserMsg, {
                audioUrl: userAudioUrl,
                audioLabel: 'Your recording',
                waveform: state.waveform,
            });
            handleChatResponse(data, uploadUserMsg);
        }).catch(function (error) {
            console.error(error);
            if (getErrorCode(error) === 'gemini_quota_exceeded') {
                appendMessage('ai', extractErrorMessage(error), {});
            }
            setStatus(extractErrorMessage(error), false, true);
        });
    }

    function handleChatResponse(data, userMessage) {
        var wasNew = !session.sessionId;
        session.sessionId = data.session_id || session.sessionId;
        if (wasNew) { loadConvList(); }
        var aiMessage = appendMessage('ai', data.reply || '', {
            audioUrl: data.audio_url,
            audioLabel: getVoiceLabel(data.voice),
            autoplay: Boolean(data.audio_url),
        });

        session.messages.push({ role: 'user', text: data.transcription || '' });
        session.messages.push({ role: 'assistant', text: data.reply || '' });

        var corrections = data.corrections || data.errors || [];
        if (corrections.length) {
            appendCorrections(corrections);
            session.corrections = session.corrections.concat(corrections);
            renderCorrectionsPanel();
        }

        if (data.pronunciation) {
            session.pronunciationScores.push(data.pronunciation);
            appendPronunciationBadge(userMessage || aiMessage, data.pronunciation);
        }

        session.turns = data.turns || (session.turns + 1);
        session.level = data.level || session.level;
        if (data.level && els.level) {
            els.level.innerText = data.level;
        }
        updateReportStats();
        loadConvList();

        if (data.audio_url) {
            setStatus('AI is speaking...', true);
        } else if (data.reply) {
            synthesizeReplyAsync(data.reply, aiMessage);
        } else {
            setStatus('Ready - press the mic to speak.', false);
        }
    }

    function synthesizeReplyAsync(text, messageNode) {
        setStatus('Reply ready - preparing voice...', true);
        fetchJson('/api/speak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text, voice: getSelectedVoice() }),
        }, 30000).then(function (data) {
            if (data.audio_url) {
                attachAudioReplay(messageNode, {
                    audioUrl: data.audio_url,
                    audioLabel: getVoiceLabel(data.voice),
                    autoplay: true,
                });
            } else {
                setStatus('Ready - press the mic to speak.', false);
            }
        }).catch(function (error) {
            console.error(error);
            setStatus('Reply ready - voice playback skipped.', false, true);
        });
    }

    function appendListenButton(messageNode, text) {
        var content = messageNode && messageNode.querySelector('.msg-content');
        if (!content) { return; }
        var btn = document.createElement('button');
        btn.className = 'btn-listen';
        btn.style.cssText = 'margin-top:4px; font-size:11px; padding:5px 12px;';
        btn.innerHTML = '<i class="fa fa-volume-up"></i> Listen';
        btn.addEventListener('click', function () {
            btn.disabled = true;
            btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
            fetchJson('/api/speak', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: text, voice: getSelectedVoice() }),
            }, 30000).then(function (data) {
                if (data.audio_url) {
                    btn.remove();
                    attachAudioReplay(messageNode, { audioUrl: data.audio_url, audioLabel: getVoiceLabel(data.voice), autoplay: true });
                } else {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa fa-volume-up"></i> Listen';
                }
            }).catch(function () {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa fa-volume-up"></i> Listen';
            });
        });
        content.appendChild(btn);
    }

    function appendMessage(role, text, options) {
        options = options || {};
        if (els.placeholder && !options.keepPlaceholder) {
            els.placeholder.style.display = 'none';
        }
        var row = document.createElement('div');
        row.className = 'msg-row ' + role + (options.greeting ? ' greeting-row' : '');
        row.innerHTML =
            '<div class="msg-avatar">' + (role === 'user' ? 'You' : 'AI') + '</div>' +
            '<div class="msg-content"><div class="msg-bubble"></div></div>';
        row.querySelector('.msg-bubble').innerText = text;
        if (options.keepPlaceholder && els.placeholder && els.placeholder.parentNode === els.chatArea) {
            els.chatArea.insertBefore(row, els.placeholder);
        } else {
            els.chatArea.appendChild(row);
        }
        attachAudioReplay(row, options);
        els.chatArea.scrollTop = els.chatArea.scrollHeight;
        return row;
    }

    function attachAudioReplay(messageNode, options) {
        if (!messageNode || !options || !options.audioUrl) {
            return null;
        }
        var content = messageNode.querySelector('.msg-content');
        if (!content) {
            return null;
        }

        var existingReplay = content.querySelector('.audio-replay');
        if (existingReplay) {
            existingReplay.parentNode.removeChild(existingReplay);
        }
        var liveWave = content.querySelector('.live-wave-box');
        if (liveWave) {
            liveWave.parentNode.removeChild(liveWave);
        }

        var replay = document.createElement('div');
        replay.className = 'audio-replay';

        var audio = document.createElement('audio');
        audio.controls = true;
        audio.preload = 'metadata';
        audio.src = options.audioUrl;
        if (options.audioLabel) {
            audio.setAttribute('aria-label', options.audioLabel);
        }
        replay.appendChild(audio);
        replay.appendChild(createSpeedControls(audio));
        var replayCanvas = document.createElement('canvas');
        replayCanvas.className = 'wave-canvas';
        replayCanvas.width = 640;
        replayCanvas.height = 70;
        replay.appendChild(replayCanvas);
        var waveColor = '#d8aa46';
        var animFrame = null;
        var realDuration = null;

        // Chrome MediaRecorder webm files report Infinity duration on first load.
        // Seek to a far point to force the browser to discover the actual duration.
        audio.addEventListener('loadedmetadata', function () {
            if (!isFinite(audio.duration)) {
                audio.currentTime = 1e10;
            }
        });
        audio.addEventListener('timeupdate', function fixDur() {
            if (isFinite(audio.duration) && audio.duration > 0) {
                realDuration = audio.duration;
                audio.removeEventListener('timeupdate', fixDur);
                if (audio.currentTime > 0.5) { audio.currentTime = 0; }
            }
        });

        function getProgress() {
            var dur = realDuration || (isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null);
            return dur ? audio.currentTime / dur : null;
        }

        function bindPlaybackEvents(waveValues) {
            audio.addEventListener('play', function () {
                if (animFrame) { cancelAnimationFrame(animFrame); }
                (function tick() {
                    drawWaveform(replayCanvas, waveValues, waveColor, getProgress());
                    if (!audio.paused && !audio.ended) {
                        animFrame = requestAnimationFrame(tick);
                    }
                }());
            });
            audio.addEventListener('pause', function () {
                if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
                drawWaveform(replayCanvas, waveValues, waveColor, getProgress());
            });
            audio.addEventListener('ended', function () {
                if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
                drawWaveform(replayCanvas, waveValues, waveColor, null);
            });
            audio.addEventListener('seeked', function () {
                if (!audio.paused) { return; }
                drawWaveform(replayCanvas, waveValues, waveColor, getProgress());
            });
        }

        if (options.waveform && options.waveform.length) {
            drawWaveform(replayCanvas, options.waveform, waveColor, null);
            bindPlaybackEvents(options.waveform);
        } else {
            drawWaveform(replayCanvas, [], waveColor, null);
            fetch(options.audioUrl)
                .then(function (r) { return r.arrayBuffer(); })
                .then(function (buffer) {
                    var actx = new (window.AudioContext || window.webkitAudioContext)();
                    return actx.decodeAudioData(buffer).then(function (decoded) {
                        actx.close();
                        var channelData = decoded.getChannelData(0);
                        var numBuckets = 220;
                        var bucketSize = Math.max(1, Math.floor(channelData.length / numBuckets));
                        var waveValues = [];
                        for (var i = 0; i < numBuckets; i++) {
                            var sum = 0;
                            for (var j = 0; j < bucketSize; j++) {
                                var s = channelData[i * bucketSize + j] || 0;
                                sum += s * s;
                            }
                            waveValues.push(Math.min(1, Math.sqrt(sum / bucketSize) * 4));
                        }
                        drawWaveform(replayCanvas, waveValues, waveColor, null);
                        bindPlaybackEvents(waveValues);
                    });
                })
                .catch(function () {});
        }

        content.appendChild(replay);
        els.chatArea.scrollTop = els.chatArea.scrollHeight;

        if (options.autoplay) {
            setStatus('AI is speaking...', true);
            audio.play().catch(function (error) {
                console.error(error);
                setStatus('Reply ready, but playback failed in the browser.', false, true);
            });
            audio.onended = function () {
                setStatus('Ready - press the mic to speak.', false);
            };
        }
        return audio;
    }

    function createSpeedControls(audio, speeds) {
        var controls = document.createElement('div');
        controls.className = 'speed-controls';
        (speeds || [1, 2, 3]).forEach(function (speed) {
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'speed-btn' + (speed === 1 ? ' active' : '');
            button.innerText = speed + 'x';
            button.addEventListener('click', function () {
                audio.playbackRate = speed;
                Array.prototype.forEach.call(controls.querySelectorAll('.speed-btn'), function (item) {
                    item.classList.toggle('active', item === button);
                });
            });
            controls.appendChild(button);
        });
        return controls;
    }

    function attachLiveWave(messageNode) {
        var content = messageNode && messageNode.querySelector('.msg-content');
        if (!content) { return; }
        var box = document.createElement('div');
        box.className = 'live-wave-box';
        box.innerHTML = '<div class="live-caption">Live transcript</div><canvas class="wave-canvas" width="640" height="70"></canvas>';
        content.appendChild(box);
        drawWaveform(box.querySelector('canvas'), state.waveform, '#d8aa46');
    }

    function updateLiveTranscript(text, isFinal) {
        if (!state.liveMessage) { return; }
        if (text) {
            if (isFinal) {
                state.finalTranscript = (state.finalTranscript + ' ' + text).trim();
                state.liveTranscript = state.finalTranscript;
            } else {
                state.liveTranscript = (state.finalTranscript + ' ' + text).trim();
            }
        }
        setLiveBubbleText(state.liveTranscript || '');
    }

    function updateLiveMessageText(text) {
        setLiveBubbleText(text);
    }

    function setLiveBubbleText(text) {
        var bubble = state.liveMessage && state.liveMessage.querySelector('.msg-bubble');
        if (bubble) {
            bubble.innerText = text || '';
            bubble.style.display = text ? '' : 'none';
        }
    }

    function mergeRealtimeAcoustics(pronunciation) {
        if (!pronunciation || !state.waveform || !state.waveform.length) {
            return;
        }
        var active = state.waveform.filter(function (value) { return value > 0.02; });
        var values = active.length ? active : state.waveform;
        var avg = average(values);
        var variance = average(values.map(function (value) {
            return Math.pow(value - avg, 2);
        }));
        var std = Math.sqrt(variance);
        var coefficient = avg ? std / avg : 1;
        var stability = Math.max(0, Math.min(100, Math.round(100 - coefficient * 100)));
        var quietRatio = 1 - (active.length / state.waveform.length);

        pronunciation.volume_stability = stability;
        pronunciation.quiet_ratio = Math.round(quietRatio * 100) / 100;
        pronunciation.avg_rms = Math.round(avg * 1000) / 1000;
        if (pronunciation.score != null) {
            pronunciation.score = Math.round(pronunciation.score * 0.85 + stability * 0.15);
        }
    }

    function average(values) {
        if (!values || !values.length) {
            return 0;
        }
        return values.reduce(function (sum, value) { return sum + value; }, 0) / values.length;
    }

    function startWaveformCapture(stream) {
        stopWaveformCapture();
        try {
            state.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            var source = state.audioContext.createMediaStreamSource(stream);
            state.analyser = state.audioContext.createAnalyser();
            state.analyser.fftSize = 1024;
            source.connect(state.analyser);
            var data = new Uint8Array(state.analyser.fftSize);
            var tick = function () {
                if (!state.isRecording || !state.analyser) { return; }
                state.analyser.getByteTimeDomainData(data);
                var sum = 0;
                for (var i = 0; i < data.length; i += 1) {
                    var centered = (data[i] - 128) / 128;
                    sum += centered * centered;
                }
                var rms = Math.sqrt(sum / data.length);
                state.waveform.push(Math.min(1, rms * 4));
                if (state.waveform.length > 220) {
                    state.waveform.shift();
                }
                var canvas = state.liveMessage && state.liveMessage.querySelector('.wave-canvas');
                if (canvas) {
                    drawWaveform(canvas, state.waveform, '#d8aa46');
                }
                state.waveformAnimation = requestAnimationFrame(tick);
            };
            tick();
        } catch (error) {
            console.error(error);
        }
    }

    function stopWaveformCapture() {
        if (state.waveformAnimation) {
            cancelAnimationFrame(state.waveformAnimation);
            state.waveformAnimation = null;
        }
        if (state.audioContext) {
            state.audioContext.close().catch(function () { return null; });
            state.audioContext = null;
        }
        state.analyser = null;
    }

    function drawWaveform(canvas, values, color, progress) {
        if (!canvas) { return; }
        var ctx = canvas.getContext('2d');
        var width = canvas.width;
        var height = canvas.height;
        var mid = height / 2;
        var list = values && values.length ? values : [0];
        var playedX = (progress != null && isFinite(progress)) ? Math.max(0, Math.min(1, progress)) * width : null;

        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = 'rgba(240, 242, 245, 0.65)';
        ctx.fillRect(0, 0, width, height);

        function drawSection(x1, x2, alpha) {
            if (x2 <= x1) { return; }
            ctx.save();
            ctx.beginPath();
            ctx.rect(x1, 0, x2 - x1, height);
            ctx.clip();
            ctx.strokeStyle = color || '#d8aa46';
            ctx.lineWidth = 3;
            ctx.globalAlpha = alpha;
            ctx.beginPath();
            for (var i = 0; i < list.length; i += 1) {
                var x = list.length === 1 ? 0 : i / (list.length - 1) * width;
                var y = mid - (list[i] || 0) * (height * 0.42);
                if (i === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
            }
            ctx.stroke();
            ctx.globalAlpha = alpha * 0.45;
            ctx.beginPath();
            for (var j = 0; j < list.length; j += 1) {
                var rx = list.length === 1 ? 0 : j / (list.length - 1) * width;
                var ry = mid + (list[j] || 0) * (height * 0.42);
                if (j === 0) { ctx.moveTo(rx, ry); } else { ctx.lineTo(rx, ry); }
            }
            ctx.stroke();
            ctx.restore();
        }

        if (playedX !== null) {
            drawSection(0, playedX, 1.0);
            drawSection(playedX, width, 0.25);
            if (playedX > 0 && playedX < width) {
                ctx.save();
                ctx.strokeStyle = '#555';
                ctx.lineWidth = 2;
                ctx.globalAlpha = 0.7;
                ctx.beginPath();
                ctx.moveTo(playedX, 4);
                ctx.lineTo(playedX, height - 4);
                ctx.stroke();
                ctx.restore();
            }
        } else {
            drawSection(0, width, 1.0);
        }
    }

    function renderCorrectionsPanelFromData(corrections, title) {
        var titleEl = document.getElementById('corrections-conv-title');
        if (titleEl) { titleEl.innerText = title || ''; }
        var panel = document.getElementById('corrections-panel');
        if (!panel) { return; }
        if (!corrections || !corrections.length) {
            panel.innerHTML = '<div style="text-align:center; color:#bbb; padding:40px 0;"><i class="fa fa-comment-o" style="font-size:36px; display:block; margin-bottom:10px;"></i>' + (title ? 'No grammar errors found in this conversation.' : 'Select a conversation to view its corrections.') + '</div>';
            return;
        }
        panel.innerHTML = '';
        corrections.forEach(function (item, index) {
            var card = document.createElement('div');
            card.className = 'correction-card';
            var typeText = (item.type || 'grammar');
            typeText = typeText.charAt(0).toUpperCase() + typeText.slice(1);
            card.innerHTML =
                '<div class="corr-label">Error #' + (index + 1) + ' &middot; ' + escHtml(typeText) + '</div>' +
                '<div class="corr-wrong"><s>' + escHtml(item.original || '') + '</s></div>' +
                '<div class="corr-right">&#10003; ' + escHtml(item.corrected || '') + '</div>' +
                '<div class="corr-reason">' + escHtml(item.reason || '') + '</div>' +
                '<button class="btn-listen" type="button"><i class="fa fa-volume-up"></i> Listen</button>' +
                '<div class="corr-audio-wrap"></div>';
            var listenBtn = card.querySelector('.btn-listen');
            var audioWrap = card.querySelector('.corr-audio-wrap');
            listenBtn.addEventListener('click', function () {
                listenBtn.disabled = true;
                listenBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Loading...';
                fetchJson('/api/speak', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: item.corrected || '', voice: getSelectedVoice() }),
                }, 30000).then(function (data) {
                    listenBtn.disabled = false;
                    listenBtn.innerHTML = '<i class="fa fa-volume-up"></i> Listen';
                    if (data.audio_url) {
                        audioWrap.innerHTML = '';
                        var audio = document.createElement('audio');
                        audio.controls = true;
                        audio.src = data.audio_url;
                        audio.title = getVoiceLabel(data.voice);
                        audioWrap.appendChild(audio);
                        audioWrap.appendChild(createSpeedControls(audio, [0.5, 1, 1.5, 2]));
                        audio.play().catch(function () {});
                    }
                }).catch(function (error) {
                    listenBtn.disabled = false;
                    listenBtn.innerHTML = '<i class="fa fa-volume-up"></i> Listen';
                    console.error(error);
                });
            });
            panel.appendChild(card);
        });
    }

    function renderCorrectionsPanel() {
        renderCorrectionsPanelFromData(session.corrections, '');
    }

    function appendCorrections(corrections) {
        corrections.forEach(function (item) {
            var box = document.createElement('div');
            box.className = 'correction-box';
            var typeText = (item.type || 'grammar');
            typeText = typeText.charAt(0).toUpperCase() + typeText.slice(1);
            box.innerHTML =
                '<span class="err-type-tag"></span>' +
                '<div class="err-original-line"><span class="err-x">✗</span> <span class="err-original"></span></div>' +
                '<div class="err-fixed-line"><span class="err-check">✓</span> <span class="err-fixed"></span></div>' +
                '<div class="err-reason"></div>';
            box.querySelector('.err-type-tag').innerText = typeText;
            box.querySelector('.err-original').innerText = item.original || '';
            box.querySelector('.err-fixed').innerText = item.corrected || '';
            box.querySelector('.err-reason').innerText = item.reason || '';
            els.chatArea.appendChild(box);
        });
        els.chatArea.scrollTop = els.chatArea.scrollHeight;
    }

    function updateReportStats() {
        var breakdown = { tense: 0, preposition: 0, article: 0, other: 0 };
        session.corrections.forEach(function (item) {
            var type = (item.type || 'other').toLowerCase();
            if (type.indexOf('tense') >= 0) {
                breakdown.tense += 1;
            } else if (type.indexOf('prep') >= 0) {
                breakdown.preposition += 1;
            } else if (type.indexOf('article') >= 0) {
                breakdown.article += 1;
            } else {
                breakdown.other += 1;
            }
        });
        renderSummary({
            turns: session.turns,
            level: session.reportLevel || '-',
            total_errors: breakdown.tense + breakdown.preposition + breakdown.article + breakdown.other,
            error_breakdown: breakdown,
            pronunciation: aggregatePronunciation(session.pronunciationScores),
        });
    }

    function aggregatePronunciation(scores) {
        if (!scores || !scores.length) {
            return {
                avg_score: 0,
                label: 'N/A',
                avg_speaking_rate_wpm: null,
                total_filler_count: 0,
                avg_pause_count: null,
                avg_pause_frequency_per_min: null,
                avg_volume_stability: null,
                avg_quiet_ratio: null,
            };
        }
        var totalScore = 0, wpmSum = 0, wpmCount = 0, fillerTotal = 0;
        var pauseSum = 0, pauseCount = 0, pauseFreqSum = 0, pauseFreqCount = 0;
        var volumeSum = 0, volumeCount = 0, quietSum = 0, quietCount = 0;
        scores.forEach(function (s) {
            totalScore += (s.score || 0);
            if (s.speaking_rate_wpm != null) { wpmSum += s.speaking_rate_wpm; wpmCount++; }
            fillerTotal += (s.filler_count || 0);
            if (s.pause_count != null) { pauseSum += s.pause_count; pauseCount++; }
            if (s.pause_frequency_per_min != null) { pauseFreqSum += s.pause_frequency_per_min; pauseFreqCount++; }
            if (s.volume_stability != null) { volumeSum += s.volume_stability; volumeCount++; }
            if (s.quiet_ratio != null) { quietSum += s.quiet_ratio; quietCount++; }
        });
        var avgScore = Math.round(totalScore / scores.length);
        var label = avgScore >= 85 ? 'Excellent' : avgScore >= 70 ? 'Good' : avgScore >= 55 ? 'Fair' : 'Needs Work';
        return {
            avg_score: avgScore,
            label: label,
            avg_speaking_rate_wpm: wpmCount ? Math.round(wpmSum / wpmCount) : null,
            total_filler_count: fillerTotal,
            avg_pause_count: pauseCount ? Math.round(pauseSum / pauseCount * 10) / 10 : null,
            avg_pause_frequency_per_min: pauseFreqCount ? Math.round(pauseFreqSum / pauseFreqCount * 10) / 10 : null,
            avg_volume_stability: volumeCount ? Math.round(volumeSum / volumeCount) : null,
            avg_quiet_ratio: quietCount ? Math.round((quietSum / quietCount) * 100) / 100 : null,
        };
    }

    function appendPronunciationBadge(messageNode, pron) {
        if (!messageNode || !pron) { return; }
        var content = messageNode.querySelector('.msg-content');
        if (!content) { return; }
        var score = pron.score != null ? pron.score : null;
        var color = score == null ? '#bbb' : score >= 70 ? '#27ae60' : score >= 55 ? '#e08030' : '#e74c3c';
        var parts = [];
        if (score != null) { parts.push('Score: ' + score); }
        if (pron.speaking_rate_wpm != null) { parts.push(pron.speaking_rate_wpm + ' WPM'); }
        if (pron.filler_count) { parts.push(pron.filler_count + ' filler' + (pron.filler_count > 1 ? 's' : '')); }
        if (!parts.length) { return; }
        var badge = document.createElement('div');
        badge.className = 'pron-badge';
        badge.innerHTML = '<span class="pron-dot" style="background:' + color + ';"></span>' + parts.join(' · ');
        content.appendChild(badge);
    }

    function renderPronunciation(pron) {
        if (!pron) { return; }
        var score = pron.avg_score || 0;
        setText('pron-score', score || '-');
        setText('pron-wpm', pron.avg_speaking_rate_wpm != null ? pron.avg_speaking_rate_wpm : '-');
        setText('pron-fillers', pron.total_filler_count != null ? pron.total_filler_count : '-');
        setText('pron-label', pron.label || '-');
        var pct = score + '%';
        setText('pron-score-pct', pct);
        var bar = document.getElementById('pron-score-bar');
        if (bar) {
            bar.style.width = pct;
            bar.style.background = score >= 70 ? '#27ae60' : score >= 55 ? '#e08030' : '#e74c3c';
        }
        renderAcousticMetrics(pron);
    }

    function renderAcousticMetrics(pron) {
        var paceScore = scorePace(pron.avg_speaking_rate_wpm);
        setText('pron-pace-val', pron.avg_speaking_rate_wpm != null ? pron.avg_speaking_rate_wpm + ' WPM' : '-');
        setMetricBar('pron-pace-bar', paceScore, paceScore >= 70 ? '#3498db' : '#e08030');

        var pauseScore = scorePause(pron.avg_pause_frequency_per_min);
        setText('pron-pause-val', pron.avg_pause_frequency_per_min != null ? pron.avg_pause_frequency_per_min + '/min' : '-');
        setMetricBar('pron-pause-bar', pauseScore, pauseScore >= 70 ? '#9b59b6' : '#e08030');

        var fillerScore = Math.max(0, 100 - (pron.total_filler_count || 0) * 10);
        setText('pron-filler-val', pron.total_filler_count != null ? pron.total_filler_count : '-');
        setMetricBar('pron-filler-bar', fillerScore, fillerScore >= 70 ? '#27ae60' : '#e08030');

        var volumeScore = pron.avg_volume_stability;
        setText('pron-volume-val', volumeScore != null ? volumeScore + '%' : '-');
        setMetricBar('pron-volume-bar', volumeScore || 0, volumeScore == null ? '#ccc' : volumeScore >= 70 ? '#27ae60' : '#e08030');
    }

    function scorePace(wpm) {
        if (wpm == null) { return 0; }
        if (wpm >= 100 && wpm <= 150) { return 100; }
        if (wpm < 100) { return Math.max(40, Math.round(wpm)); }
        return Math.max(40, Math.round((200 - wpm) / 50 * 100));
    }

    function scorePause(pausesPerMinute) {
        if (pausesPerMinute == null) { return 0; }
        return Math.max(0, Math.min(100, Math.round(100 - pausesPerMinute * 18)));
    }

    function setMetricBar(id, value, color) {
        var bar = document.getElementById(id);
        if (bar) {
            bar.style.width = Math.max(0, Math.min(100, value || 0)) + '%';
            bar.style.background = color;
        }
    }

    function renderSummary(summary) {
        setText('rpt-turns', summary.turns || 0);
        setText('rpt-errors', summary.total_errors || 0);
        setText('rpt-level', summary.level || '-');
        renderBar('rpt-tense', 'rpt-tense-bar', summary.error_breakdown.tense, summary.total_errors);
        renderBar('rpt-prep', 'rpt-prep-bar', summary.error_breakdown.preposition, summary.total_errors);
        renderBar('rpt-art', 'rpt-art-bar', summary.error_breakdown.article, summary.total_errors);
        renderBar('rpt-other', 'rpt-other-bar', summary.error_breakdown.other, summary.total_errors);
        if (summary.pronunciation) {
            renderPronunciation(summary.pronunciation);
        }
    }

    function renderBar(labelId, barId, value, total) {
        setText(labelId, value || 0);
        var percentage = total ? Math.round((value || 0) / total * 100) : 0;
        var bar = document.getElementById(barId);
        if (bar) {
            bar.style.width = percentage + '%';
        }
    }

    function setText(id, value) {
        var node = document.getElementById(id);
        if (node) {
            node.innerText = value;
        }
    }

    function setStatus(text, active, warning) {
        els.statusText.innerText = text;
        els.statusText.classList.toggle('status-warning', Boolean(warning));
        els.statusDot.classList.toggle('active', Boolean(active));
    }

    function pickSupportedMimeType() {
        var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
        for (var i = 0; i < types.length; i += 1) {
            if (window.MediaRecorder && MediaRecorder.isTypeSupported(types[i])) {
                return types[i];
            }
        }
        return null;
    }

    function fetchJson(url, options, timeoutMs) {
        var controller = new AbortController();
        var timer = setTimeout(function () {
            controller.abort();
        }, timeoutMs || 30000);

        var finalOptions = Object.assign({}, options || {}, { signal: controller.signal });
        return fetch(url, finalOptions).then(function (response) {
            clearTimeout(timer);
            return response.json().catch(function () {
                return {};
            }).then(function (payload) {
                if (!response.ok) {
                    var error = new Error((payload.error && payload.error.message) || ('Request failed with status ' + response.status));
                    error.payload = payload;
                    throw error;
                }
                return payload;
            });
        }).catch(function (error) {
            clearTimeout(timer);
            if (error.name === 'AbortError') {
                throw new Error('Request timed out. Please try again.');
            }
            throw error;
        });
    }

    function extractErrorMessage(error) {
        if (!error) {
            return 'Unknown error.';
        }
        return error.message || 'Request failed.';
    }

    function getErrorCode(error) {
        return error && error.payload && error.payload.error && error.payload.error.code;
    }

    // ── Live Conversation ──────────────────────────────────────────────────
    var LC = {
        active: false,
        phase: 'idle',
        sessionId: null,
        currentQuestion: '',
        stream: null,
        audioCtx: null,
        analyser: null,
        recorder: null,
        chunks: [],
        vadRaf: null,
        silenceStart: null,
        speechStart: null,
        speechDetected: false,
        currentAudio: null,
        THRESHOLD: 18,
        SILENCE_MS: 1500,
        MIN_SPEECH_MS: 600,
    };

    var LIVE_TEST_QUESTIONS = [
        'What did you do yesterday, and what did you enjoy most?',
        'Can you describe a place you like and explain why you like it?',
        'What is one habit you want to build this year?',
        'Tell me about a recent problem you solved.',
        'What kind of work or study do you find most interesting?',
        'If you had a free afternoon, how would you spend it?',
        'What is something you learned recently?',
        'Describe a person who has influenced you.',
        'What is your favorite way to practice English?',
        'Do you prefer learning alone or with other people? Why?',
        'What is one goal you want to achieve in the next three months?',
        'Tell me about a movie, book, or show you enjoyed recently.',
    ];
    var LIVE_TEST_INTRO = 'Are you ready for the English test?';

    var lcEl = {
        btn: document.getElementById('live-btn'),
        btnIcon: document.getElementById('live-btn-icon'),
        btnLabel: document.getElementById('live-btn-label'),
        orb: document.getElementById('live-orb'),
        statusLabel: document.getElementById('live-status-label'),
        statusSub: document.getElementById('live-status-sub'),
        chatArea: document.getElementById('live-chat-area'),
        placeholder: document.getElementById('live-placeholder'),
        vadFill: document.getElementById('live-vad-fill'),
        feedbackBtn: document.getElementById('live-feedback-btn'),
    };

    function lcSetPhase(phase) {
        LC.phase = phase;
        lcEl.orb.className = 'live-orb' + (phase !== 'idle' ? ' ' + phase.replace(/_/g, '-') : '');
        var info = {
            idle:        ['Ready',           'Press Start and speak naturally — no button pressing needed'],
            listening:   ['Listening...',    'Speak when ready, will auto-detect your voice'],
            recording:   ['Recording...',    'Keep speaking — will process after you pause'],
            processing:  ['Processing...',   'Transcribing and generating reply'],
            ai_speaking: ['AI Speaking...',  'Will resume listening automatically when done'],
        };
        var d = info[phase] || info.idle;
        lcEl.statusLabel.innerText = d[0];
        lcEl.statusSub.innerText = d[1];
    }

    function lcPickQuestion() {
        var index = Math.floor(Math.random() * LIVE_TEST_QUESTIONS.length);
        LC.currentQuestion = LIVE_TEST_QUESTIONS[index];
        return LC.currentQuestion;
    }

    function lcRenderPromptQuestion(forceNew) {
        if (!lcEl.chatArea || LC.sessionId) { return; }
        var existing = document.getElementById('live-test-question');
        if (existing) {
            existing.parentNode.removeChild(existing);
        }
        var question = forceNew || !LC.currentQuestion ? lcPickQuestion() : LC.currentQuestion;
        var row = document.createElement('div');
        row.className = 'live-msg ai';
        row.id = 'live-test-question';
        row.innerHTML = '<div class="msg-avatar">AI</div><div class="msg-bubble"></div>';
        row.querySelector('.msg-bubble').innerText = LIVE_TEST_INTRO + '\n\n' + question;
        if (lcEl.placeholder && lcEl.placeholder.parentNode === lcEl.chatArea) {
            lcEl.chatArea.insertBefore(row, lcEl.placeholder);
            lcEl.placeholder.style.display = '';
        } else {
            lcEl.chatArea.appendChild(row);
        }
        lcEl.chatArea.scrollTop = 0;
    }

    function lcStart() {
        if (LC.active) { return; }
        if (lcEl.feedbackBtn) { lcEl.feedbackBtn.style.display = 'none'; }
        LC.sessionId = null;
        if (lcEl.chatArea) {
            Array.prototype.forEach.call(
                lcEl.chatArea.querySelectorAll('.live-msg, .live-feedback-card'),
                function (el) { el.parentNode.removeChild(el); }
            );
        }
        if (lcEl.placeholder) { lcEl.placeholder.style.display = ''; }
        navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
            LC.active = true;
            lcRenderPromptQuestion(true);
            LC.stream = stream;
            LC.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var source = LC.audioCtx.createMediaStreamSource(stream);
            LC.analyser = LC.audioCtx.createAnalyser();
            LC.analyser.fftSize = 512;
            source.connect(LC.analyser);
            lcEl.btn.classList.add('active');
            lcEl.btnIcon.className = 'fa fa-stop';
            lcEl.btnLabel.innerText = 'Stop';
            lcSetPhase('listening');
            lcStartVad();
        }).catch(function () {
            lcEl.statusLabel.innerText = 'Microphone access denied';
        });
    }

    function lcStop() {
        LC.active = false;
        lcStopVad();
        lcStopRecorder();
        if (LC.currentAudio) { LC.currentAudio.pause(); LC.currentAudio = null; }
        if (LC.audioCtx) { LC.audioCtx.close().catch(function () {}); LC.audioCtx = null; }
        if (LC.stream) { LC.stream.getTracks().forEach(function (t) { t.stop(); }); LC.stream = null; }
        LC.analyser = null;
        LC.speechDetected = false;
        LC.silenceStart = null;
        LC.speechStart = null;
        if (!LC.sessionId) {
            lcRenderPromptQuestion(false);
        }
        lcEl.btn.classList.remove('active');
        lcEl.btnIcon.className = 'fa fa-play';
        lcEl.btnLabel.innerText = 'Start';
        lcEl.vadFill.style.width = '0%';
        lcSetPhase('idle');
        if (LC.sessionId && lcEl.feedbackBtn) {
            lcEl.feedbackBtn.style.display = 'flex';
            lcEl.feedbackBtn.disabled = false;
        }
    }

    function lcStartVad() {
        var freqData = new Uint8Array(LC.analyser ? LC.analyser.frequencyBinCount : 256);
        function tick() {
            if (!LC.active || !LC.analyser) { return; }
            LC.vadRaf = requestAnimationFrame(tick);
            LC.analyser.getByteFrequencyData(freqData);
            var sum = 0;
            for (var i = 0; i < freqData.length; i++) { sum += freqData[i]; }
            var avg = sum / freqData.length;
            var pct = Math.min(100, (avg / LC.THRESHOLD) * 40);
            lcEl.vadFill.style.width = pct + '%';
            lcEl.vadFill.style.background = avg > LC.THRESHOLD ? '#e74c3c' : '#27ae60';
            if (LC.phase === 'processing' || LC.phase === 'ai_speaking') { return; }
            if (avg > LC.THRESHOLD) {
                LC.silenceStart = null;
                if (!LC.speechDetected) {
                    LC.speechDetected = true;
                    LC.speechStart = Date.now();
                    lcBeginRecording();
                }
            } else if (LC.speechDetected) {
                if (!LC.silenceStart) {
                    LC.silenceStart = Date.now();
                } else if (Date.now() - LC.silenceStart >= LC.SILENCE_MS) {
                    var dur = LC.speechStart ? Date.now() - LC.speechStart : 0;
                    LC.speechDetected = false;
                    LC.silenceStart = null;
                    LC.speechStart = null;
                    if (dur >= LC.MIN_SPEECH_MS) {
                        lcEndAndProcess();
                    } else {
                        lcStopRecorder();
                        lcSetPhase('listening');
                    }
                }
            }
        }
        LC.vadRaf = requestAnimationFrame(tick);
    }

    function lcStopVad() {
        if (LC.vadRaf) { cancelAnimationFrame(LC.vadRaf); LC.vadRaf = null; }
    }

    function lcBeginRecording() {
        if (LC.recorder && LC.recorder.state !== 'inactive') { return; }
        LC.chunks = [];
        var mime = (function () {
            var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
            for (var i = 0; i < types.length; i++) {
                if (window.MediaRecorder && MediaRecorder.isTypeSupported(types[i])) { return types[i]; }
            }
            return null;
        }());
        LC.recorder = mime ? new MediaRecorder(LC.stream, { mimeType: mime }) : new MediaRecorder(LC.stream);
        LC.recorder.ondataavailable = function (e) { if (e.data && e.data.size > 0) { LC.chunks.push(e.data); } };
        LC.recorder.start(100);
        lcSetPhase('recording');
    }

    function lcStopRecorder() {
        if (LC.recorder && LC.recorder.state !== 'inactive') { LC.recorder.stop(); }
        LC.recorder = null;
        LC.chunks = [];
    }

    function lcEndAndProcess() {
        if (!LC.recorder || LC.recorder.state === 'inactive') { return; }
        lcSetPhase('processing');
        var recorder = LC.recorder;
        var chunks = LC.chunks;
        LC.recorder = null;
        LC.chunks = [];
        recorder.onstop = function () {
            var blob = new Blob(chunks, { type: chunks[0] ? chunks[0].type : 'audio/webm' });
            if (!blob.size) { if (LC.active) { lcSetPhase('listening'); } return; }
            lcUpload(blob);
        };
        recorder.stop();
    }

    function lcUpload(blob) {
        var formData = new FormData();
        formData.append('audio', blob, 'recording.webm');
        if (LC.sessionId) { formData.append('session_id', LC.sessionId); }
        formData.append('voice', getSelectedVoice());
        formData.append('asr_model', getSelectedAsrModel());
        formData.append('prompt', getSelectedPrompt());
        var userEl = lcAppendMsg('user', '...');
        fetch('/api/chat', { method: 'POST', body: formData })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                LC.sessionId = data.session_id || LC.sessionId;
                var bubble = userEl && userEl.querySelector('.msg-bubble');
                if (bubble) { bubble.innerText = data.transcription || '(no transcription)'; }
                if (data.reply) {
                    lcAppendMsg('ai', data.reply);
                    lcSpeak(data.reply);
                } else {
                    if (LC.active) { lcSetPhase('listening'); lcStartVad(); }
                }
            })
            .catch(function () {
                var bubble = userEl && userEl.querySelector('.msg-bubble');
                if (bubble) { bubble.innerText = '(error — please try again)'; }
                if (LC.active) { lcSetPhase('listening'); lcStartVad(); }
            });
    }

    function lcSpeak(text) {
        lcSetPhase('ai_speaking');
        lcStopVad();
        fetch('/api/speak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text, voice: getSelectedVoice() }),
        }).then(function (r) { return r.json(); })
          .then(function (data) {
              if (data.audio_url) {
                  var audio = new Audio(data.audio_url);
                  audio.title = getVoiceLabel(data.voice);
                  LC.currentAudio = audio;
                  audio.play().catch(function () {});
                  audio.onended = function () {
                      LC.currentAudio = null;
                      if (LC.active) { lcSetPhase('listening'); lcStartVad(); }
                  };
              } else {
                  if (LC.active) { lcSetPhase('listening'); lcStartVad(); }
              }
          })
          .catch(function () {
              if (LC.active) { lcSetPhase('listening'); lcStartVad(); }
          });
    }

    function lcAppendMsg(role, text) {
        if (lcEl.placeholder) { lcEl.placeholder.style.display = 'none'; }
        var row = document.createElement('div');
        row.className = 'live-msg ' + role;
        row.innerHTML = '<div class="msg-avatar">' + (role === 'user' ? 'You' : 'AI') + '</div>' +
                        '<div class="msg-bubble"></div>';
        row.querySelector('.msg-bubble').innerText = text;
        lcEl.chatArea.appendChild(row);
        lcEl.chatArea.scrollTop = lcEl.chatArea.scrollHeight;
        return row;
    }

    function lcGetFeedback() {
        if (!LC.sessionId || !lcEl.feedbackBtn) { return; }
        lcEl.feedbackBtn.disabled = true;
        lcEl.feedbackBtn.querySelector('span').innerText = 'Generating…';

        // Remove any previous feedback card
        var old = document.getElementById('live-feedback-card');
        if (old) { old.parentNode.removeChild(old); }

        var loadingRow = document.createElement('div');
        loadingRow.className = 'live-feedback-card';
        loadingRow.id = 'live-feedback-card';
        loadingRow.innerHTML = '<div style="color:#aaa; font-size:13px; text-align:center; padding:10px 0;"><i class="fa fa-spinner fa-spin"></i>&nbsp; Analyzing your conversation…</div>';
        lcEl.chatArea.appendChild(loadingRow);
        lcEl.chatArea.scrollTop = lcEl.chatArea.scrollHeight;

        fetch('/api/live-feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: LC.sessionId }),
        })
        .then(function (r) {
            if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
            return r.json();
        })
        .then(function (d) {
            var card = document.getElementById('live-feedback-card');
            if (!card) { return; }

            if (d.error) {
                card.innerHTML = '<div style="color:#c0392b; font-size:13px;">' + (d.error.message || 'Error generating feedback.') + '</div>';
                lcEl.feedbackBtn.disabled = false;
                lcEl.feedbackBtn.querySelector('span').innerText = 'Get AI Feedback';
                return;
            }

            var strengths = (d.strengths || []).map(function (s) {
                return '<div class="lfc-item strength"><span class="lfc-dot"></span><span>' + s + '</span></div>';
            }).join('');
            var improvements = (d.improvements || []).map(function (s) {
                return '<div class="lfc-item improve"><span class="lfc-dot"></span><span>' + s + '</span></div>';
            }).join('');

            card.innerHTML =
                '<div class="lfc-header">' +
                '  <div class="lfc-level-badge">' + (d.level || 'B1') + '</div>' +
                '  <div><div class="lfc-title">AI Coach Feedback</div>' +
                '  <div class="lfc-subtitle">Based on your live session</div></div>' +
                '</div>' +
                '<div class="lfc-overall">' + (d.overall || '') + '</div>' +
                '<div class="lfc-section"><div class="lfc-section-title">Strengths</div>' + strengths + '</div>' +
                '<div class="lfc-section"><div class="lfc-section-title">Areas to Improve</div>' + improvements + '</div>' +
                '<div class="lfc-tip"><span class="lfc-tip-icon"><i class="fa fa-lightbulb-o"></i></span>' +
                '<span>' + (d.tip || '') + '</span></div>';

            lcEl.chatArea.scrollTop = lcEl.chatArea.scrollHeight;
            lcEl.feedbackBtn.style.display = 'none';
        })
        .catch(function (err) {
            console.error('Live feedback error:', err);
            var card = document.getElementById('live-feedback-card');
            if (card) { card.innerHTML = '<div style="color:#c0392b; font-size:13px;">Could not generate feedback. Please try again.</div>'; }
            lcEl.feedbackBtn.disabled = false;
            lcEl.feedbackBtn.querySelector('span').innerText = 'Get AI Feedback';
        });
    }

    if (lcEl.btn) {
        lcRenderPromptQuestion(true);
        lcEl.btn.addEventListener('click', function () {
            if (!LC.active) { lcStart(); } else { lcStop(); }
        });
        // Stop live conv when navigating away
        document.querySelectorAll('.menu a').forEach(function (link) {
            if (!link.classList.contains('show-5')) {
                link.addEventListener('click', function () { if (LC.active) { lcStop(); } });
            }
        });
    }

    if (lcEl.feedbackBtn) {
        lcEl.feedbackBtn.addEventListener('click', lcGetFeedback);
    }
}());
