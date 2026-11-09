/** AdaptiveNet v1 stable ingestion data contracts; never execute model output directly. */
export type Region = "region-a" | "region-b";
export type SourceQuality = "fresh" | "missing" | "loss_only" | "insufficient_or_stale";
export type MaybeNumber = number | null; // null = missing/expired, NOT zero

export interface IngestionFeatures {
  app_up: MaybeNumber;
  http_rps: MaybeNumber;
  http_5xx_rps: MaybeNumber;
  http_error_ratio: MaybeNumber;
  http_p95_ms: MaybeNumber;
  cpu_busy_pct: MaybeNumber;
  cpu_busy_ewma_pct: MaybeNumber;
  memory_available_bytes: MaybeNumber;
  memory_total_bytes: MaybeNumber;
  memory_available_pct: MaybeNumber;
  node_load1: MaybeNumber;
  network_rtt_p50_ms: MaybeNumber;
  network_rtt_p95_ms: MaybeNumber;
  packet_loss_pct: MaybeNumber;
  ripe_probes_reporting: number;
  http_p95_ewma_ms: MaybeNumber;
  http_p95_robust_z: MaybeNumber;
}

export interface RegionFeatureBundle {
  features: IngestionFeatures;
  quality: {
    internal: "fresh" | "missing";
    external: "fresh" | "loss_only" | "insufficient_or_stale";
    required_missing: string[];
    ripe_probes: number;
    ripe_minimum_probes: number;
    freshness_seconds: {aws_amp: number; ripe_atlas: number};
  };
  provenance: Record<string, unknown>;
}
/** Last real CloudWatch observation, display only; never an ML freshness substitute. */
export interface CloudFrontLastObserved {
  value: number;
  unit: string;
  observed_at: string;
  age_seconds: number;
  source: "aws_cloudwatch";
  display_only: true;
}

export interface CloudFrontTelemetry {
  status: "not_configured" | "fresh" | "no_fresh_metrics";
  distribution_id: string | null;
  aws_region: "us-east-1";
  metric_scope: "global";
  freshness_seconds: number;
  /** Live-only: stale values must be null, not zero. */
  features: Record<string, MaybeNumber>;
  provenance: Record<string, unknown>;
  /** Historical UI-only values, up to the local SQLite retention window. */
  last_known?: Record<string, CloudFrontLastObserved>;
}

export interface FeatureSnapshotV1 {
  contract_version: "adaptivenet.features.v1";
  snapshot_id: string;
  computed_at: string;
  computed_at_unix: number;
  pipeline_mode: "read_only";
  ready_for_model: boolean;
  /** Both regions have sufficient fresh AWS-only signals. Does not authorize actions. */
  ready_for_internal_only_model?: boolean;
  ready_for_automatic_action: false;
  quality_reasons: string[];
  regions: Record<Region, RegionFeatureBundle>;
  comparison: {
    rtt_p50_a_minus_b_ms: MaybeNumber;
    packet_loss_a_minus_b_pct: MaybeNumber;
    cpu_busy_a_minus_b_pct: MaybeNumber;
    memory_available_a_minus_b_pct: MaybeNumber;
    http_p95_a_minus_b_ms: MaybeNumber;
  };
  source_errors: string[];
  cloudfront?: CloudFrontTelemetry;
  model_notes: string[];
}
export type RecommendedAction = "hold" | "investigate" | "shift_traffic" | "scale_out";
export interface ModelProposalV1 {
  contract_version: "adaptivenet.proposal.v1";
  snapshot_id: string;
  action: RecommendedAction;
  confidence: number; // [0,1]
  affected_region?: Region; // required for scale_out; resolved through the controller allowlist
  target_region_a_percent?: number; // int [0,100], only meaningful for shift_traffic
  evidence?: string[];
}
export interface ReviewV1 {
  review_version: "adaptivenet.decision_review.v1";
  snapshot_id: string;
  requested_action: RecommendedAction;
  policy_valid: boolean;
  eligible_for_operator_review: boolean;
  approved_for_execution: false;
  executed: false;
  effective_action: "hold";
  rejection_reasons: string[];
  required_future_gates: string[];
}
