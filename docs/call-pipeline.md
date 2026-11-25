# The call pipeline: speech in, the model, speech out

Part of the [README](../README.md)'s reference.

## Voice call: speech in, the model and its tools

A call takes the caller's audio over WebRTC (Pipecat's SmallWebRTC transport) through voice
activity detection (Silero) and Deepgram's streaming speech-to-text to the agent, speaks its
lines ("Voice out and the call's life", below) and sends each turn's result to the call page
over the data channel.

- `GET /` serves the call page (`web/index.html`, built by `app/api/page.py`), in the
  console's colours and with its logo: a phone with the access code, Call and End call, a
  "relay only" switch, the call's time and a bar meter that follows the live audio (the
  assistant's or the caller's, read in the browser and never sent anywhere) and says who is
  speaking. Beside it, once the call talks, the conversation as text: what the model was
  given of each caller turn (the guard's text, or a "not passed to the assistant" mark for a
  swallowed or emergency turn), the lines said, "scripted line" where no model was asked, the
  tool steps, the output guard's rule when it withheld part of a reply, a hand-off, and the
  timings behind a switch; and the connection state, the ICE candidate types in use and why
  the call ended. The server's text is written as text, never as markup.
- The page is read once, when the app is built, with its two font files (IBM Plex, served
  from `/fonts/`; no other name is found). It is served with a Content-Security-Policy made
  from the page itself: `default-src 'none'`, its one inline script and one inline style
  allowed by their SHA-256, `connect-src 'self'`, `font-src 'self'`, no framing; and with
  `nosniff`, `Referrer-Policy: no-referrer` and a `Permissions-Policy` that gives the
  microphone to this origin only. The page loads nothing from another origin.
- `GET /ice-servers` returns the ICE server list with short-lived TURN credentials (coturn's
  shared-secret scheme; they expire `MAX_CALL_SECONDS` plus 60 seconds after they are issued).
- `POST /offer` takes an SDP offer (a JSON body of at most 32 KiB) and returns the answer.
- Both need `Authorization: Bearer <CALL_ACCESS_TOKEN>`. The token is checked before any of
  the body is read. When no token is configured (only allowed in dev without TURN) the two
  endpoints are open.
- At most `MAX_CONCURRENT_CALLS` calls at once (more get 503). How a call ends is in "Voice
  out and the call's life" below; one that has not connected within 30 seconds is closed, and
  the server stops a call still running 30 seconds after `MAX_CALL_SECONDS`. Without every
  vendor's settings (allowed in dev only) a call is refused at set-up.
- Speech-to-text (`app/voice/pipeline.py`): Deepgram `nova-3-general`, `en-US`, punctuation
  and smart formatting on, numerals off (smart formatting already writes numbers as numerals),
  opted out of Deepgram's model improvement programme. Smart formatting writes "three three
  four four" as `3344` and a date as `March 14, 1985`; a spelled name stays letters (`J O H N
  S O N`); the input guard reads digits and number words either way. The caller is taken to
  have stopped after `VAD_STOP_SECONDS` of silence (0.5 by default); Deepgram is then asked to
  finalize, and only a finalized transcript makes a turn.
- Vocabulary hints (`app/voice/vocabulary.py`): Nova-3's keyterm prompting, one `keyterm`
  query parameter per term, so that "Lisinopril" is heard as the drug. The list is built once
  at start-up: the distinct medication names of the `medications` table cut to their drug word
  (`Lisinopril 10mg` is `Lisinopril`), the doctors' surnames and `Sunrise Family Clinic`;
  letters only, 2 to 40 characters a term, sorted, each once, at most 100 (Deepgram's limit).
  It is a vocabulary list with no link to any patient, and it is sent to Deepgram with every
  call. When the database cannot be read at start-up the list is the doctors and the clinic,
  and `stt_hints_fallback` is logged. Deepgram's page on keyterms says nothing of their cost
  in latency; it is not measured here.
- The turn (`app/agent/turn.py`, no Pipecat in it): the input guard decides first. Emergency:
  `emergency()` ([tools](tools.md)) moves the call to `emergency` and gives the emergency line at once, its
  event written in the background; no model request. Swallowed (the SSN
  step): the model sees nothing of the turn; when the guard says submit, code runs
  `submit_claim` and its line is the reply. Pass: a submit the guard asks for runs first, then
  one model turn with the guard's text. Turns run one at a time.
- A model turn sends the system prompt (`app/agent/prompts.py`: fixed text, no patient data,
  written for speech), the conversation so far and the tools `tools_for` allows right now,
  each with its argument model's JSON schema and a fixed description. Every tool call goes
  through `call_tool` ([tools](tools.md): a report of an emergency first, then `dispatch` and the
  automatic hand-off), and its outcome goes back to the model, unless it carries a scripted
  line: that line, with its values, is the reply, word for word, the model is told it was
  said, and tools asked
  for after it in the same reply do not run. Scripted lines join the conversation as the
  model's own; swallowed turns never join it. At most four tool rounds per turn.
- The model (`app/agent/model.py`): Claude through the official Anthropic SDK, async and
  streamed, `LLM_MODEL` (default `claude-haiku-5-5`), `max_tokens` 300, effort `low`, no
  `temperature` or other sampling parameter, no SDK retries. A refusal, a failed request, no
  first token within 8 seconds, a reply not finished 20 seconds after that, a reply that is
  neither an answer nor a tool request (cut off at `max_tokens`, say), or a fifth tool round:
  the fixed fallback line, and the call goes on. Two in one call, or a dispatch outcome that
  asks for one, raise the hand-off signal. Thinking is off in every request (`thinking:
  disabled`, which Claude Haiku 5.5 accepts at effort `low`): on a phone call it would delay
  the first word and use up the 300 tokens; a `LLM_MODEL` that refuses thinking off (Claude
  Opus 5.5 and Sonnet 5.5 do) gets the fallback line on every turn.
- `Deps` (sessions as the runtime role, the audit log, the clinic clock with `SIMULATED_NOW`,
  the claim hasher) is built once at start-up from the settings (`build_deps` in
  `app/main.py`, kept on `app.state.deps`).
- Per turn, logged as numbers only (`turn_measured`, `turn_first_audio`) and shown on the
  page, all from the moment the caller stopped speaking: the silence voice activity detection
  waits for, the final transcript, the model's first and last token, tool rounds, the turn's
  end, and the first audio of the reply, which is what the caller waits for. The vendor clients' loggers (`anthropic`, `httpx2`, `httpx`,
  `httpcore`, `websockets`, `deepgram`) keep only warnings and errors: below that they write
  request bodies and websocket frames.
- coturn runs as a Compose service built from `deploy/coturn`: one configuration file with
  allocation and bandwidth quotas, no relaying to private, loopback, link-local or unspecified
  IPv4 addresses, and its own logging switched off because it records caller addresses.
  Every TURN credential has its own username (`<expiry>:<random id>`), so coturn's per-user
  quota (4 allocations, one call's worth) limits each credential; the total quota is 9 per
  allowed call, and an allocation nobody refreshes ends within 120 seconds. The unspecified
  IPv6 address is not denied: coturn 4.19.2 matches that rule against IPv4 peers as well and
  then refuses every relayed call.

Limits and notes:

- The model's text reaches the caller and the page through the
  [output guard](output-guard.md). All data is synthetic.
- The hand-off (two model failures in a call, two blocked replies in a row, or a tool outcome
  that reaches the limit of failures or refusals) acts once, on the outcome that raises it,
  by one code path (`hand_off` in `app/agent/tools.py`): with the office open the transfer line
  is said in place of the turn's own and the call ends with `transferred`; with it closed the
  caller is offered a message (the fallback line) and the call goes on. The page shows that it
  was raised. The guard's flags (an attempt to override the instructions, a request
  about someone else) are not passed to the model.
- One call's conversation is kept in memory only, for the call's life.
- The verified line, said by code, puts the caller's first name in the conversation the model
  sees; tool results are the only other patient data it receives.
- Tests and CI never reach a vendor: the model is scripted, speech-to-text and speech out are
  replaced, and CI's forced-relay calls run with the vendor hosts pointed at the app's own
  loopback. The proof with the vendors is a real call on the server.

`scripts/relay_probe.py` (an aiortc client that sends a tone and reads the server's receiver
reports for it) runs in CI against the app forced through coturn, with only the networking changed
(`deploy/docker-compose.relay-test.yml`: app and coturn on 203.0.113.0/24, which coturn does
not deny, and the vendor hosts on the app's loopback).

## Voice out and the call's life

The agent speaks (`app/voice/speech.py`, `app/voice/pipeline.py`), and the call starts and ends
through the functions of "Messages, transfer, emergency and the end of the call"
([tools](tools.md)).

- Speech out: Pipecat's `ElevenLabsTTSService` (pipecat-ai 1.17.0), ElevenLabs' multi-stream
  WebSocket, `TTS_MODEL` (default `eleven_flash_v2_5`, Flash v2.5, ElevenLabs' lowest-latency
  model that endpoint serves; v3 and v4 models are not served there), `ELEVENLABS_VOICE_ID`,
  English, auto mode on (whole lines are sent), ElevenLabs' default voice settings. No voice is
  guessed: without the key and the voice the app does not start outside dev, and in dev a call
  is refused at set-up.
- What is spoken: only lines the agent releases (`app/agent/turn.py`), each whole: a scripted
  line at once, word for word; the model's sentences that passed the output guard as one line,
  once the reply has ended as a plain answer. The words of a reply that asks for a tool, is
  refused or is cut off are never spoken, so the model never announces an action its tool has
  not done. Speech starts at a complete sentence, never at a token; the cost is the time
  between the first sentence and the end of the reply (one or two short sentences).
- The greeting, the closed line and the emergency line play to the end. From the call's start
  until its opening line has played, and while one of those lines is to be played, the caller
  is muted: their audio goes on as silence to voice activity detection and speech-to-text, and
  whatever is reported anyway is neither an interruption nor a turn. Every other line can be
  interrupted: when the caller starts speaking over it, speech out stops, the lines not yet
  played are dropped, and the conversation keeps only the words ElevenLabs reported played of
  the line that was cut (counted from its word timestamps), none of the others. The page notes
  the interruption.
- The caller cannot be heard during the greeting, the closed line or the 911 line: anything
  they say then, an emergency included, is not acted on.
- An emergency is heard whatever the call is doing: every finished utterance goes through the
  input guard's emergency decision the moment it arrives, before the turn queue, while a turn
  runs, with turns queued, and while the call is closing for any reason (during the goodbye,
  the transfer line or the after-hours close). The running turn is cancelled (its reply, if it
  comes, is never said), queued turns are dropped, every line that can be interrupted stops,
  the 911 line is released in the same step of the event loop as the transcript arrived, and
  the call ends with `emergency`, whatever reason it was ending for and whatever timer fires
  meanwhile. Anything else said while the call is closing is not a turn. A closing call does
  not end while the caller is speaking or their words are being transcribed (5 seconds at
  most), so an emergency said over the last line is still heard.
- How the adapter knows what was played: each line goes to text-to-speech between two marks.
  Pipecat's text-to-speech service keeps frames it does not speak in order with its audio, and
  the transport sends them on only once the audio before them has been written to the caller,
  so the marks come out of the transport as the line starts and when it is over.
- The call starts (`start_call`, which writes `call_started`) when the caller connects; its
  opening is the greeting or the closed line. Tool calls go through `call_tool`; the input
  guard's emergency through `emergency()`.
- The call ends once (`end_call`, one `call_ended`): after the emergency line (`emergency`),
  the transfer line (`transferred`), the goodbye or the after-hours close (`completed`) has
  played; when the caller hangs up or the connection drops (`caller_hung_up`); after
  `IDLE_SECONDS` without the caller speaking the agent asks "Are you still there?", and after
  another `IDLE_SECONDS` says goodbye (`idle_timeout`); at `MAX_CALL_SECONDS` it lets a
  running turn finish (a reported emergency in it still ends the call with `emergency`), then
  says goodbye once the line playing is over (`max_duration`); a turn that fails, or a pipeline
  that ends
  without the call having decided to (a vendor that can no longer work), ends it with `error`.
  The caller speaking resets the idle count; the idle timer does not run while a line plays or
  a turn runs. Once a reason is decided it stands, even if the caller hangs up during the last
  line. Then the transport sends what is left, two seconds of silence, and closes; the page
  shows the reason.
- Filler: when a tool round's answer has not come 1.2 seconds after the model asked for the
  tools, the agent says "One moment while I check.", at most once per turn. Only once the
  round's tools have answered with no scripted line (so never just before one), and never
  while the SSN step is armed. A slow tool gets it as soon as it has answered.
- Speech out that cannot connect: the line still reaches the page; Pipecat waits three seconds
  for audio, then the line counts as over (so the caller is unmuted and the timers run).
  Pipecat writes the service off after three silent lines in a row, or at once when ElevenLabs
  rejects the key, the voice or the model, and the call then ends with `error`. In CI's relay
  test ElevenLabs' host points at the app's own loopback: the call starts, its lines fail this
  way, and the probe still passes.
- Why speech out refused (`app/voice/speech_check.py`): Pipecat's ElevenLabs service does not
  read ElevenLabs' error messages, so a voice the plan does not allow shows only as silent
  lines. When a call ends with `error` because speech out was written off, one request goes to
  ElevenLabs' plain text-to-speech endpoint (`POST /v1/text-to-speech/{voice_id}`, the
  configured voice and model, two words) in the background, given up after 5 seconds, at most
  one a minute for the whole process, never waited for by the call. One line is logged:
  `speech_refused status=<HTTP status> code=<ElevenLabs' error code>` (a fixed set of codes,
  anything else `other`; for example `status=402 code=paid_plan_required`, or `status=404
  code=voice_not_found` for a voice that does not exist), `speech_check_ok`, or
  `speech_check_failed error=<class>`; never the key, the voice id or ElevenLabs' message.
  The page is told the reason before the call closes, and for `error` a fixed hint: "The
  speech service refused to speak. Check the voice id and the plan." when speech out was
  written off, otherwise "The call ended because of an error."; it keeps them ("Why the call
  ended") when the connection then drops.
- Logs: event names, the call id, line ids, counts and milliseconds. Pipecat's own records from
  the modules that quote the text being spoken (its text-to-speech service and its word
  sequencer, at warning level) are not written; each one at warning or above leaves a line of
  ours in its place, `pipecat_record_withheld level=<level> logger=<module>`, with nothing of
  its message.
- Wording (`app/agent/prompts.py`): one short sentence for an answer about the caller's
  information, no trailing offer of help; "anything else?" only when a request is done and
  once per request; name and date of birth asked together, with no spelling (only the scripted
  fail line asks to spell); an off-topic remark gets one sentence naming what the agent can
  help with.

What the caller waits for is measured on each call rather than promised: from the moment the
caller stops speaking to the first audio of the reply, shown per turn on the page (`first audio
... after you stopped speaking`), logged as `turn_first_audio`, and stored with the turn's other
timings for the console's Metrics page ([call record](call-record.md)). It includes
`VAD_STOP_SECONDS`.

Limits:

- The conversation keeps what was heard as far as ElevenLabs reports it: its word timestamps
  run slightly ahead of or behind the audio the caller hears, so a word either side of the cut
  may be counted wrongly.
- ElevenLabs keeps its usual request logs (zero retention is an enterprise setting); the data
  is synthetic.
- A line that produced no audio (speech out that cannot connect) is recorded as heard in the
  conversation.
- If speech out takes more than 3 seconds to its first audio, Pipecat gives the line up as
  silent, so the caller can be unmuted before an uninterruptible line (the greeting, the closed
  line, the 911 line) has finished playing.
- On a real call the agent's own voice coming back through loudspeakers can be taken for the
  caller speaking and interrupt it: use headphones.
