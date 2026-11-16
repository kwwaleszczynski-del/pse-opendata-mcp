# Test fixtures

Real responses of the PSE reports API `https://api.raporty.pse.pl/api`, used by the offline tests and recorded with
`scripts/record_fixtures.py`. Each file holds one query limited to one to three business days and to the fields the
tests need; the records are stored as the API returned them. `recorded_utc` in each file is the retrieval time.

Information obtained from PSE S.A. (api.raporty.pse.pl; reports `rce-pln`, `csdac-pln`, `kse-load`, `his-wlk-cal`,
`poze-redoze`, `przeplywy-mocy`), state as of 16 November 2026. The information has been processed in part:
selected days and fields only. It is not an official PSE publication. See the PSE conditions for re-use in the
main README.
