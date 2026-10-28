# Data sources

CareHarbor AI uses open, redistributable sources. It never uses real patient records.

| Folder | Source | What it gives CareHarbor AI | Licence |
|---|---|---|---|
| `synthea/synthea_sample_data_csv_latest.zip` | [Synthea sample data](https://synthetichealth.github.io/synthea-sample-data/), MITRE | Realistic synthetic EHR histories: ages, sex, allergies, chronic conditions, outpatient visit reasons and medicines | Apache-2.0 (synthetic data, no real patients) |
| `medlineplus/*.xml` (raw) → `../content/medlineplus/*.md` | [MedlinePlus health topics](https://medlineplus.gov/) via the NLM Web Service | Plain-language reference summaries, retrieved and cited by the triage/FAQ agents | Public domain (U.S. Government work). Attribution: "Information from MedlinePlus.gov" |

Clinic-specific content (`content/leaflets`, `content/policies`) was written for this project.

## Why not real patient datasets?

Credentialed clinical datasets such as MIMIC-IV or eICU need individual approval and a data use agreement that forbids redistribution. They can't be shipped in a portable zip, pushed to GitHub or used in a public demo. Synthea is generated from real population health statistics, so its records have realistic shapes (common chronic conditions, typical medicines, visit patterns) while carrying zero privacy risk.

## How the data is used

- `scripts/fetch_sources.py` downloads both sources (with retries; safe to re-run).
- `scripts/seed.py` → `careharbor/synthea.py` reads the Synthea zip directly and does the following:
  - **Keeps** age, sex, allergies, conditions, visit reasons and outpatient medicines (with the prescription's strength and form).
  - **Drops** names, SSNs, addresses and every other US identifier. Names and phone numbers are localised for the fictional Bengaluru clinic.
  - **Excludes** opioids, injections, contraceptives, oncology and hospital-only drugs.
  - **Adds** a conventional dosing schedule (`SIG_TABLE`) for each supported medicine, because Synthea records the drug but not the schedule.
  - **Skips** penicillin-class drugs for patients with a recorded penicillin allergy.
  - **Re-anchors** each patient's timeline so their latest visit was 3–40 days before "today".
- Every row records its provenance in `source_ref`: `synthea:<patient-id-prefix>`, `synthea:medications.csv:<RxNorm code>` or `demo-persona`.

MedlinePlus content is general reference information, not clinic advice. CareHarbor AI shows the source link whenever it cites it.
