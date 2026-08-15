# Resolved hypotheses

## H-3 — Google rejects the current Drive credential-propagation protocol (confirmed)

`colab drivemount` fails because Google's
`/tun/m/credentials-propagation/<endpoint>` endpoint rejects the final POST,
not because browser consent was skipped.

- Evidence: two consent-complete propagation attempts returned HTTP 400. After
  the CLI auth repair, non-ephemeral DriveFS also failed independently because
  its metadata lookup for `auth/user-id` returned HTTP 404, while direct Drive
  API access with the installed ADC succeeded.

## H-4 — Direct VM OAuth can be made reliable by honoring the stdin-hook contract (confirmed)

The fallback becomes reliable when the CLI follows the current Jupyter stdin
hook contract, explicitly sends the reply, and never persists verification
input.

- Evidence: focused tests passed, the patched compatibility CLI was reinstalled,
  and live auth completed 1.76 seconds after input. The VM then had valid ADC
  and gcloud user credentials, and local history stored only `<redacted>`.
