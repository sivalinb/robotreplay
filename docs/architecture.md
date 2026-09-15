# Architecture and boundaries

The first release runs one FastAPI process with one durable worker. SQLite transactions claim jobs and reserve model budgets. Run only one application worker per data directory; horizontal multi-worker operation is not supported.

## Data flow

Uploads require an explicit permission basis and a source reference. A generated UUID determines every file path; the client cannot choose filesystem paths or ask the server to fetch video URLs. The media worker receives a small environment without model credentials. It checks container signatures and decoding limits, removes audio from browser playback, and records normalized marker coordinates and observations.

Green-marker tracking is a calibration baseline. Multiple green objects or no marker produces missing evidence. Camera motion, lighting, marker identity and occlusion affect results; this is not general robot recognition. Image-plane distances are not physical distances. A mentor can add observations that preserve authorship and source timestamps.

The queue survives restarts. Recovered jobs rebuild computed samples/evidence transactionally while preserving mentor observations. Jobs do not automatically retry a failing provider. Clip deletion removes the source directory, derivatives, samples and FTS records; access is revoked even if disk cleanup reports a problem. Filesystem backups need a separate expiry policy.

## Investigation graph

The graph executes a fixed sequence: input policy → scoped retrieval → read-only comparison/concept lookup → optional model selection → output validation. Tool counts and graph depth are bounded. The model chooses from project-authored teaching questions and existing evidence IDs. This product boundary makes validation inspectable.

FTS5 searches within authorized clips. Hybrid mode embeds at most 48 computed observations and the redacted question using a configured API, then merges lexical and cosine-similarity rankings with reciprocal-rank fusion. There is no persistent shared embedding cache. Provider/model choice and a held-out retrieval evaluation remain necessary before claiming semantic improvements.

Console import accepts lines printed by student code in the format **RR|elapsed_ms|message**. An explicit offset and uncertainty align them to video. This is a project format, not the native VEX protocol. See the [VEXcode IQ console reference](https://kb.vex.com/hc/en-us/articles/4410478121364-Using-the-Print-Console-in-VEXcode-IQ-with-Blocks).

## Authentication and security

Passwords use salted scrypt; session tokens are random and only their SHA-256 digests are stored. Cookies are HttpOnly and SameSite Strict. State-changing authenticated requests require a session-bound CSRF token, and supplied Origin headers must match the app origin. Login attempts are bounded. Every private resource query includes team scope.

Authorization is never delegated to the LLM or NeMo. Model responses must validate as an exact schema with an allowed question and known evidence IDs. The browser escapes source text and uses a restrictive Content Security Policy. APIs do not expose shell execution, robot control or arbitrary external URL fetching.

The subprocess is not a hardened hostile-media sandbox on a developer laptop. The container adds a non-root user, dropped capabilities, a read-only root filesystem, memory/CPU/PID limits and a private data volume. Shared use needs TLS, explicit host/proxy trust, account lifecycle controls, backup/retention review, and real-data adversarial evaluation.

Only configured text-model requests leave the application. Raw media and human annotations stay local by default. Question redaction removes common email/number patterns; it is not a general PII detector. Keep identifying child information out of model questions.

Hosted providers have fixed matching endpoint defaults. Credentials can load from separate server-side files; the app never exposes them through the browser. Fireworks uses opaque keyed team identifiers for session affinity and cache isolation. Inference events contain bounded outcome categories and usage numbers, with team scope and retention. Published synthetic provider experiments use a separate reviewed artifact and do not launch requests from the dashboard.

The optional NeMo input rail runs a registered application-policy action without a generation model. This verifies the integration, not broad semantic jailbreak detection. LangSmith tracing is explicitly disabled around the graph to avoid exporting graph inputs through ambient environment settings.

## Extension points

- Replace the marker baseline with a separately evaluated detector or vision-language model.
- Add reviewed, season-versioned teaching/rules material and a labeled retrieval dataset.
- Replace local storage/identity only after a shared-use requirement appears.
- Add GPU-serving experiments without coupling replay to GPU availability.
- Add OIDC, queue leases, object storage and tenant-specific key management before scaling beyond the documented local boundary.
