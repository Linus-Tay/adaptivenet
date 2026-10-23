/** ML feature adapter contract. No control/actuation permission implied. */
import type { CloudFrontTelemetry, IngestionFeatures, ModelProposalV1, ReviewV1, Region } from "./ingestion";

export type InferenceMode = "internal_only" | "combined";
export type ModelFeature = keyof IngestionFeatures;

export interface ModelInputV1 {
  contract_version: "adaptivenet.model_input.v1";
  snapshot_id: string;
  mode: InferenceMode;
  computed_at_unix: number;
  age_seconds: number;
  ready_for_inference: boolean;
  quality_reasons: string[];
  feature_columns: ModelFeature[];
  features: Record<Region, IngestionFeatures>;
  missing: Record<Region, Record<ModelFeature, boolean>>;
  quality: Record<Region, Record<string, unknown>>;
  provenance: Record<Region, Record<string, unknown>>;
  comparison: Record<string, number | null>;
  /** Optional global CDN telemetry; never treat as a region-a / region-b observation. */
  cloudfront?: Omit<CloudFrontTelemetry, "last_known">; // stale display-only values are excluded by build_model_input
  actions_enabled: false;
}

/** Predictor signature, as implemented by the ML team. */
export type PredictFn = (input: ModelInputV1) => Promise<ModelProposalV1> | ModelProposalV1;
/** Controller review is a separate step. Even a valid proposal is not executed. */
export type ShadowReviewFn = (proposal: ModelProposalV1) => Promise<ReviewV1> | ReviewV1;
