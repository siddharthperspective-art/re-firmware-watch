# Tripper Dash firmware watch

Daily check for Royal Enfield Himalayan 450 Tripper Dash firmware releases, run for
free on GitHub Actions and pushed to your phone through [ntfy](https://ntfy.sh).

Royal Enfield publishes no firmware feed. Dash firmware ships over the air through
the Royal Enfield app, so this watches the signals that move when a release lands:

- Royal Enfield App version and release notes (App Store lookup API; same app as Android).
- Google News headlines about Himalayan 450 / Tripper Dash firmware, FOTA, or app updates.
- Reddit posts in r/Himalayan450 and r/royalenfield about updates.

An alert means "go check". The definitive check is on the bike: Royal Enfield app →
Connected devices → Infotainment settings → Software update.

## How it runs

- `.github/workflows/watch.yml` runs daily at 04:07 UTC (09:37 IST) and on demand
  (Actions tab → Tripper Dash firmware watch → Run workflow). Tick "Only send a test
  notification" there to check that alerts still reach your phone.
- The ntfy topic is the repository secret `NTFY_TOPIC` (the topic name is the password).
- After each run the workflow commits `state.json` if anything new was seen, so items
  are only alerted once. Delete `state.json` to start from a fresh baseline.
- If a run fails (for example the secret is missing), GitHub emails you.

## Files

| File | Purpose |
| --- | --- |
| `watch.py` | The checker (Python 3 standard library only) |
| `state.json` | What has already been seen |
| `config.json` | Local-only settings for running on a computer (gitignored) |

## Running locally

```sh
python3 watch.py --dry-run   # show what would alert; changes nothing
python3 watch.py --test      # send a test push (needs config.json or NTFY_TOPIC + WATCH_CHANNELS=ntfy)
```

Pull before running locally so your copy of `state.json` is current.
