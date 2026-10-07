# Nexus Approvals: spec v0 (DRAFT for Trainer's adversarial design review, before any code)

Author: Desk (Phoenix, phoenix-claude), 2026-10-07. Status: DESIGN ONLY, nothing built.
Origin: governed-commit v1/v2 (C:\projects\fleet-hardening\GOVERNED-COMMIT-REVIEW-20261007.md) was rejected twice by Trainer's independent review (4 + 5 HIGH findings). The root cause was trusting the approval path to a tool run inside the agent's own session. Aern 10/07: approvals must work from his phone (he's remote ~50% of the time); "broadest scope possible for the frictions we've discussed"; passkey OK; bundle the ssh fix.

## 1. Goal and threat model
**Goal:** any action that today ends in "Aern pastes a command" becomes **propose → Aern reviews on his phone → passkey tap → a sealed executor does exactly what he saw**.

**Defend against:** a confused, steered or prompt-injected agent on ANY seat (Phoenix, Trainer, Ashaman: all three have seats), including one with elevated ssh to Ashaman today.
**Do not claim to defend against:** a deliberate multi-step admin-level attacker on Ashaman. The bar is that any bypass needs an obviously malicious chain that the existing guard, classifier, deny rules and cross-seat hash checks catch or make loud.

**Invariants (each must hold, and each is a review target):**
- I1. **What Aern sees is what runs.** The review page renders from the stored, immutable proposal artifact (a patch or an op spec); the executor consumes that same artifact by content hash. Agent prose never sits in the approval path.
- I2. **Only Aern can approve.** A WebAuthn passkey assertion over a challenge that binds `proposal_id + artifact_sha256 + action + expiry`. No agent can produce one. The public key is registered once by Aern; revocable, and more than one allowed (phone + backup).
- I3. **The executor is sealed.** Code baked into an image, root-owned and read-only (the relay `/app` pattern); it verifies the assertion ITSELF (it does not trust a Nexus "approved" flag); pinned config; git run with system/global config disabled and a pinned config; its own clone, which agents never touch.
- I4. **Agents can't reach the executor's trust inputs**: image, passkey public keys, push credential, clone. Enforced by the bundled ssh change (§6) plus guard patterns plus a cross-seat hash tripwire.
- I5. **Everything is visible**: proposed, approved, rejected, expired, executed and failed proposals stay listed on the page and in an append-only log.

## 2. Components
| Component | Where | Notes |
|---|---|---|
| `POST /api/proposals` | aernhome (Nexus) | Agents submit: `action`, `artifact` (patch / op spec), `base` (commit/hash), `reason`, `created_by`. Stored immutably with `artifact_sha256`. |
| `/nexus/approve` | aernhome | Phone-first list + detail view. Server-rendered diff/op from the stored artifact. Lint banners (§4). Approve / Reject buttons. |
| WebAuthn | aernhome | Registration (once, Aern) + assertion per approval. Python lib `webauthn` (py_webauthn, Duo Labs): new dependency, pin the exact version, official PyPI only (dependency doctrine). Origin `https://ashaman.tail125d67.ts.net`. Tailscale serve gives the HTTPS WebAuthn requires. |
| Executor | new container `approvals-executor` on Ashaman | Polls approved proposals, re-verifies the assertion against the registered keys, executes the action by type (§3), writes the result back. No inbound port. |
| Tripwire | Trainer (fleet-assure / Fleet Sentinel) | Daily: executor image digest + registered-key fingerprints vs pinned values in fleet-hardening. Mismatch = P1 to_aern + Signal. |

## 3. Action types (v1 = broadest scope for the frictions discussed 10/06-07)
| Action | Executor does | Notes / still Aern's paste |
|---|---|---|
| `governed-commit` | Applies the patch to its own clean clone of fleet-config or discord-claude-relay at `base` (`git apply --check` first), commits with an `Approved-via: nexus <id> <sha>` trailer, pushes. | ALL paths allowed, code included, but code and skill files raise lint banners (§4). Deletes allowed as an explicit op in the patch (the passkey tap IS Aern's hand). |
| `relay-prompt-deploy` | Runs Ashaman's `tools/relay-deploy.ps1` (pull + scope guard + in-flight wait + hash verify). | Follows a governed-commit on `workspace-locked/`. |
| `relay-rebuild` | Preflight (weekend pitch #2: checkout == origin, hot-patch hash diff, Dockerfile COPY lint, Node-major parity, creds shape), then build a candidate image, smoke test, swap, verify `relay_alive`. | Today classifier-blocked; Aern fires it by hand. In-flight bot run check before the swap. |
| `relay-restart` | In-flight check, restart, verify. | |
| `workspace-fileop` | Curator-style moves/deletes in `/workspace`: the op spec lists every path recursively and the page shows the full list. | Replaces "seat verifies + stages, Aern executes" (rules/projects.md). |
| `scheduled-task` | Create/change/run a task on ASHAMAN only, from an op spec. | Phoenix/Trainer-local actions are out of scope: the executor lives on Ashaman. |
| OUT of v1 | credentials; sending to anyone not Aern; money (LetterTrack, purchases); fence installs on seats (local UAC); finance.db writes; anything on Phoenix/Trainer locally. | Hard tier or wrong host. |

## 4. Lint banners (server-side, shown above the artifact; warnings, Aern decides)
- Code file (`.ps1/.py/.sh/.js/Dockerfile/compose`): **"Code change. You can't verify this by reading. Wait for the attached independent review."** A `governed-commit` touching code needs a `review` attachment: a verdict posted by a DIFFERENT seat to the proposal, shown on the page. No review = no Approve button.
- Skill frontmatter changed, `allowed-tools`, hooks, `!`-command lines, `@`-imports, new SKILL.md.
- Lines in Hard rules / Security / FERPA / Fence sections, or heading lines added/removed in rules/*.md.
- Gate files: guard, managed-settings, hooks, sync-seat, pre-push, this subsystem's own code.
- Deletes: count plus a full path list.

## 5. Proposal lifecycle
`proposed → (reviewed) → approved | rejected | expired(48h) → executing → done | failed`. Approval binds `artifact_sha256`; the executor recomputes it and refuses on mismatch. Base drift (`base` != current HEAD) = refuse and ask for re-proposal; never auto-rebase. One executor at a time (lock). Results and log tails attach to the proposal.

## 6. Bundled: agents lose elevated ssh to Ashaman
- Today every seat's `ssh ashaman` lands ELEVATED (rules/devices.md), so any seat's agent can rewrite Ashaman's fence, the executor, or Nexus code.
- Change: a non-admin local account on Ashaman (`agent`) for seat keys; Aern's admin key separate and not on any agent-readable path. Docker access via the `docker-users` group is still needed for reads (`docker ps/logs/exec` reads). That group is effectively root on the host. **OPEN QUESTION for review**: can read-only docker be given without docker-users (a read-only socket proxy)?
- Blast radius: inventory every skill/rule that does `ssh ashaman <admin thing>` (Start-ScheduledTask, schtasks /Create, relay-deploy, docker exec writes). Each becomes an approval action or stays Aern's. This is the biggest work item and the riskiest; it changes every seat.
- Install is Aern's (elevated, on Ashaman).

## 7. Build order (each step reviewable and reversible)
1. Proposals API + review page, read-only (no executor), with lint. Agents start proposing; Aern still pastes. Proves I1 and the lint.
2. WebAuthn registration + approve flow (approval recorded, nothing executes).
3. Executor for `governed-commit` only. Tripwire live.
4. §6 ssh change + inventory.
5. Remaining action types, one at a time.
Each step: Trainer adversarial review of the diff before Aern's install.

## 8. Questions for the design review
- Is I3 achievable on Docker Desktop on Windows when the agent seat on Ashaman runs as the same user that owns Docker?
- WebAuthn over Tailscale serve from Android Chrome / Pixel: any origin or RP-ID trap with a `ts.net` name?
- The review attachment for code: can a steered seat forge a "different seat" review? (Bind it to the seat's ssh identity / created_by plus a separate key?)
- Anything in §3 that should not be in v1.
