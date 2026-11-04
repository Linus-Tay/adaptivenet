# Experiment episodes and verified labels

There is no independently verified incident training dataset bundled with this starter. Never turn a fault button click into a positive outage label. A healthy sample is not established merely by missing error data.

Example JSON SHAPE ONLY (not a real incident):

~~~json
{
  "episode_id": "example-not-a-real-incident",
  "scenario": "healthy_control | service_stop | network_delay | packet_loss | partial_outage | transient | recovery",
  "affected_region": "region-a | region-b | both | none | unknown",
  "requested_at_utc": null,
  "verified_onset_utc": null,
  "verified_end_utc": null,
  "verified_recovery_utc": null,
  "outcome_label": null,
  "label_confidence": null,
  "independent_evidence_refs": [],
  "verifier": null,
  "reviewed": false,
  "data_window_start_utc": null,
  "data_window_end_utc": null,
  "notes": "Fixture only: never count as real training evidence"
}
~~~

Use original UTC timestamps. Separate requested AWS/SSM fault actions from independently verified impact/recovery, including HTTP and host/service evidence. For partial/widespread Internet conclusions, collect independent external vantage points; a single Mac witness is inadequate. Unknown remains null / insufficient_evidence.

For each prediction cutoff t, use only source measurements already observed AND available by t, respecting both observed_at and receipt/collected_at. Labels can describe future outcomes only for a predeclared prediction horizon; features cannot. Entire episodes and nearby windows (with embargo) must remain in one train/validation/test partition. Do not commit raw sensitive records, SSO cache or credentials.
