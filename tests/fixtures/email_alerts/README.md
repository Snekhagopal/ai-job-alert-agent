# Real job-alert email samples

One folder per platform (`linkedin/`, `naukri/`, …). Each sample is a pair:

- `<date>_<id>.html`: the alert email, saved by `scripts/capture_alert_samples.py` with
  personal data removed
- `<date>_<id>.expected.json`: the job cards a person checked against the email in Gmail
  (renamed from the generated `.cards.draft.json`)

`tests/test_email_alert_parser.py` parses every `.html` here and compares it with its
`.expected.json`. A platform should be enabled in `config/sources.yaml` and added to
`EMAIL_PARSER_VERIFIED` only after at least one real sample passes.
