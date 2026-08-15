# VM auth and Drive mount investigation

## Symptom

1. `colab drivemount` reaches browser consent, then the final credential
   propagation request returns `HTTP 400 Bad Request`; `drive.mount()` raises
   `ValueError: mount failed`.
2. `colab auth` accepts the pasted verification code locally but remains `busy`
   because the code never reaches the VM.
3. The CLI writes the submitted verification code in plaintext to
   `~/.config/colab-cli/history/<session>.jsonl`.

## Observed conditions

- Occurs: version `0.1.dev86+g9cc61d15a`, T4 Colab runtime, OAuth2 control
  plane, Google Drive consent completed in Firefox.
- Does not occur: ordinary durable Python execution and kernel probes complete
  successfully on the same runtime.
- First observed: 2026-08-15 during the shared-drive setup.
- Last known good: unknown.

## Evidence collected before iteration 1

- Two independent Drive consent attempts ended in final propagation HTTP 400.
- The direct auth input was logged as `input_reply`, proving local collection
  but not VM delivery.
- After more than 600 seconds, session status remained `busy` until local
  interruption.
- Post-interruption VM check: Colab's ADC file was absent; the gcloud user token
  check failed; the generic metadata credential remained available.
- Remote `google.colab.auth._gcloud_login()` calls
  `gcloud_process.communicate(code.strip())` with no timeout.
- The local plaintext verification code was replaced with `<redacted>`.

## Iteration 1

- Technique: differential diagnosis plus source inspection.
- Commands: live `drivemount`, live `auth`, session status/probe, bounded ADC
  diagnostic, upstream comparison.
- Result: H-5 eliminated; the initial H-1 elimination was invalidated by a
  later contract-level observation. H-2 and H-3 survived provisionally.
- Next falsifying test: deterministic timeout reproduction around the VM-side
  auth subprocess boundary.

## Iteration 2

- Technique: minimal reproduction and dependency-contract inspection.
- Test: invoke the same VM-side gcloud command with deliberately invalid input.
- Exact result: process completed in 0.619 seconds with return code 1 and
  `invalid_grant: Malformed auth code`.
- Unexpected observation: `jupyter_client._async_execute_interactive()` calls a
  custom stdin hook with the full message, ignores a non-awaitable return value,
  and expects the hook to call `client.input()` itself.
- Result: H-2 eliminated. H-1 restored and positively confirmed.

## Root Cause (Confirmed)

`ColabRuntime.execute_code()` implemented the obsolete stdin-hook contract. Its
wrapper accepted a value named `prompt`, called local `input(prompt)`, logged
the returned secret, and returned the string. Current `jupyter_client` supplies
an `input_request` dictionary and ignores the returned string, so no
`input_reply` reached the VM.

## Evidence

- Live prompt rendered as the raw Jupyter message dictionary.
- Local history contained the submitted value, but the VM retained no gcloud
  user credential or ADC file.
- The dependency's default hook explicitly calls `self.input(raw_data)`;
  custom-hook returns are ignored.
- Pre-fix regression test failed because `client.input()` was never called.
- Post-fix runtime tests pass and assert explicit send plus redaction.
- With the patched CLI installed, the live auth command completed 1.76 seconds
  after code submission. `/content/.adc/adc.json` existed, the active gcloud
  user could mint a token, and the history contained only `<redacted>`.

## Reproduction case

Call `ColabRuntime.execute_code(..., allow_stdin=True)` against code that invokes
`input()`. Supply a verification value. On the pre-fix implementation, local
history records the value but the remote kernel remains blocked.

## Fix

Use a synchronous message-shaped hook, parse `message["content"]`, prompt with
`input` or `getpass`, check for newer stdin/shell messages, explicitly call the
kernel client's `input(response)`, and store only a redacted history marker.

## Live Drive result

The auth fix does not repair Colab's separate proprietary Drive credential
propagation endpoint. Ephemeral propagation still returned HTTP 400, while the
non-ephemeral DriveFS metadata lookup returned HTTP 404 for
`instance/guest-attributes/auth/user-id`. As a bounded workaround, the VM's
valid ADC was used directly to locate the single shared drive named `Data` and
mount it read-only at `/content/drive/Shareddrives/Data`. The mount and `ro`
mount option were verified independently.

## Next step

Run the full test and lint suites, then commit the compatibility-runtime fix.
