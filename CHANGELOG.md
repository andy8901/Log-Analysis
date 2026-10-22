# Changelog

## v2.1.0 — 2026-10-22
- Organization overview rebuilt: filter by department, drone and period; every number and chart follows the
  selection. Front-page totals (uploads, flights analysed, active drones, crashes to date, users, logs today),
  today's highlights, department-wise table, flight averages (time, distance, range, altitude, max/min voltage,
  current, cold-start time), failsafe metrics (flight time at battery failsafe, distance at GCS/RC failsafe),
  issue counts (thrust loss, GPS, EKF, pre-arm, motor, crashes) and conclusions (pilot/system/yet to decide),
  with an issue chart split into those categories and a per-flight trend when one drone is selected.
- Department dashboards get the same panel, scoped as before.
- Drone photo collage on the dashboards; Neosky logo fixed (was cropped) and shown full-width in the sidebar.
- Admin bulk upload: pick a whole folder of logs, choose the department and how to find each MSN; files are
  uploaded one by one and analysed in the background with live progress, then feed every dashboard and the
  Multilog export (which can be limited to one batch).
- Admin action to (re)run the flight investigation on older logs.
- App version and build shown in the sidebar, on the login page and in the incident report.

## v2.0.0 — 2026-10-21
- Flight investigator: HUMAN ERROR / SYSTEM ERROR verdict with plain-language explanation, incident report
  on the Neosky letterhead, 3D replay fixes.
